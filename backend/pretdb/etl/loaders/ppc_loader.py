# pretdb/etl/loaders/ppc_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime
from decimal import Decimal
from django.db import transaction
from django.apps import apps

logger = logging.getLogger(__name__)

def _get_model(app_label: str, model_name: str):
    try:
        return apps.get_model(app_label, model_name)
    except LookupError:
        return None

def _pick(d: Dict[str, Any], *keys: str, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", "<NA>"):
            return d[k]
    return default

def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        import pandas as pd
        dt = pd.to_datetime(str(v), errors="coerce", dayfirst=True)
        return None if pd.isna(dt) else dt.date()
    except Exception:
        return None

def _to_decimal(val: Any, decimal_places: int = 2) -> Optional[Decimal]:
    """Convierte a Decimal con precisión específica"""
    if val is None:
        return None
    try:
        # Convertir a string y luego a Decimal
        if isinstance(val, float):
            # Para floats, usar formato para evitar problemas de precisión
            return Decimal(f"{val:.{decimal_places}f}")
        elif isinstance(val, (int, Decimal)):
            return Decimal(str(val))
        else:
            # Para strings, limpiar y convertir
            cleaned = str(val).replace(',', '.').strip()
            return Decimal(cleaned)
    except:
        return None

def _to_ratio(val: Any) -> Optional[Decimal]:
    """Convierte a ratio PPC (0-1) como Decimal"""
    if val is None:
        return None
    try:
        # Si ya es un float entre 0-1
        if isinstance(val, float) and 0 <= val <= 1:
            return Decimal(f"{val:.4f}")
        
        # Si es string con porcentaje
        s = str(val).strip()
        if '%' in s:
            # Extraer número y dividir por 100
            num_str = s.replace('%', '').replace(',', '.').strip()
            num = float(num_str)
            return Decimal(f"{(num / 100.0):.4f}")
        
        # Si es número > 1, asumir porcentaje
        num = float(s.replace(',', '.'))
        if num > 1:
            return Decimal(f"{(num / 100.0):.4f}")
        else:
            return Decimal(f"{num:.4f}")
            
    except:
        return None

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s


class PPCLoader:
    """
    Carga datos de PPC (Percent Plan Complete) desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.PPCSemanal = _get_model(pretdb, "PPCSemanal")
        self.PPCDiario = _get_model(pretdb, "PPCDiario")

    @transaction.atomic
    def persist(
        self,
        pod,  # Objeto Pod ya creado desde portada
        out: dict,
        *,
        dry_run: bool = False,
        run_id: Optional[int] = None,
    ) -> Tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
        """
        Persiste los datos de PPC
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.PPCDiario:
            error_msg = "Modelos Pod o PPCDiario no encontrados"
            logger.error(error_msg)
            return 0, {}, {"models": error_msg}

        if not pod or not isinstance(pod, self.Pod):
            error_msg = "Pod no proporcionado o inválido"
            logger.error(error_msg)
            return 0, {}, {"pod": error_msg}

        # Extraer datos del transformer
        flat_rows = out.get("flat_rows", [])
        summary = out.get("summary", {})
        
        if not flat_rows:
            warnings["sin_datos"] = "No hay datos de PPC para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        ppc_diarios_procesados = 0
        ppc_semanales_procesados = 0

        try:
            # Procesar datos diarios de PPC
            for row_data in flat_rows:
                fecha = _to_date(row_data.get("fecha"))
                dia_semana = _truncate(row_data.get("dia_semana"), 20)
                programadas = row_data.get("programadas")
                realizadas = row_data.get("realizadas")
                ppc_porcentaje = row_data.get("ppc_porcentaje")

                if not fecha:
                    warnings.setdefault("fechas_invalidas", []).append(
                        f"Fila sin fecha válida: {row_data}"
                    )
                    continue

                if dry_run:
                    logger.debug(f"🔶 DRY RUN - PPC: {fecha} - Prog: {programadas}, Real: {realizadas}, PPC: {ppc_porcentaje}")
                    ppc_diarios_procesados += 1
                    continue

                # 1. Crear/actualizar PPC diario
                try:
                    ppc_diario_obj, created = self.PPCDiario.objects.update_or_create(
                        pod=pod,
                        fecha=fecha,
                        defaults={
                            'dia_semana': dia_semana,
                            'actividades_programadas': _to_decimal(programadas, 2),
                            'actividades_realizadas': _to_decimal(realizadas, 2),
                            'ppc_porcentaje': _to_ratio(ppc_porcentaje),
                            'comentarios': None,  # Por si acaso hay campo de comentarios
                        }
                    )
                    if created:
                        ppc_diarios_procesados += 1
                        total_rows += 1
                        logger.debug(f"✅ PPC diario creado: {fecha} - {ppc_porcentaje}")
                        
                except Exception as e:
                    fecha_str = fecha.isoformat() if fecha else "Fecha desconocida"
                    warnings.setdefault("ppc_diario", []).append(f"'{fecha_str}': {e}")

            # 2. Calcular y crear PPC semanal (resumen)
            if not dry_run and ppc_diarios_procesados > 0:
                try:
                    self._procesar_ppc_semanal(pod, flat_rows, warnings)
                    ppc_semanales_procesados = 1  # Solo creamos un registro semanal
                    total_rows += 1
                except Exception as e:
                    warnings["ppc_semanal"] = f"No se pudo crear PPC semanal: {e}"

            # Resumen final
            stats = {
                "ppc_diarios_procesados": ppc_diarios_procesados,
                "ppc_semanales_procesados": ppc_semanales_procesados,
                "total_dias": len(flat_rows),
                "rango_fechas": self._obtener_rango_fechas(flat_rows),
                "ppc_promedio": self._calcular_ppc_promedio(flat_rows),
            }
            
            if not dry_run:
                logger.info(f"✅ PPC procesado: {ppc_diarios_procesados} días, {ppc_semanales_procesados} resumen semanal")
                if stats["ppc_promedio"]:
                    logger.info(f"   - PPC promedio: {stats['ppc_promedio']:.2%}")
            else:
                logger.info(f"🔶 DRY RUN - PPC: {len(flat_rows)} días a procesar")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de PPC: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _procesar_ppc_semanal(self, pod: Any, flat_rows: List[Dict[str, Any]], warnings: Dict[str, Any]) -> None:
        """Calcula y crea el resumen semanal de PPC"""
        if not self.PPCSemanal:
            return

        try:
            # Calcular promedios y totales semanales
            total_programadas = 0
            total_realizadas = 0
            ppc_promedio = 0
            ppc_valores = []
            
            for row in flat_rows:
                prog = row.get("programadas") or 0
                real = row.get("realizadas") or 0
                ppc = row.get("ppc_porcentaje")
                
                total_programadas += prog
                total_realizadas += real
                if ppc is not None:
                    ppc_valores.append(ppc)

            # Calcular PPC promedio
            if ppc_valores:
                ppc_promedio = sum(ppc_valores) / len(ppc_valores)

            # Obtener rango de fechas
            fechas = [row.get("fecha") for row in flat_rows if row.get("fecha")]
            if fechas:
                fecha_inicio = min(fechas)
                fecha_fin = max(fechas)
                
                # Crear registro semanal
                ppc_semanal_obj, created = self.PPCSemanal.objects.update_or_create(
                    pod=pod,
                    fecha_inicio=fecha_inicio,
                    fecha_fin=fecha_fin,
                    defaults={
                        'total_actividades_programadas': _to_decimal(total_programadas, 2),
                        'total_actividades_realizadas': _to_decimal(total_realizadas, 2),
                        'ppc_promedio': _to_ratio(ppc_promedio),
                        'dias_con_datos': len(flat_rows),
                    }
                )
                
                if created:
                    logger.info(f"✅ PPC semanal creado: {fecha_inicio} a {fecha_fin} - PPC: {ppc_promedio:.2%}")
                    
        except Exception as e:
            warnings["ppc_semanal_calc"] = f"Error en cálculo PPC semanal: {e}"

    def _obtener_rango_fechas(self, flat_rows: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Obtiene el rango de fechas de los datos PPC"""
        fechas = [row.get("fecha") for row in flat_rows if row.get("fecha")]
        if not fechas:
            return None
            
        fecha_inicio = min(fechas)
        fecha_fin = max(fechas)
        
        return {
            "inicio": fecha_inicio.isoformat(),
            "fin": fecha_fin.isoformat(),
            "dias": len(fechas)
        }

    def _calcular_ppc_promedio(self, flat_rows: List[Dict[str, Any]]) -> Optional[float]:
        """Calcula el PPC promedio de todos los días"""
        ppc_valores = [row.get("ppc_porcentaje") for row in flat_rows if row.get("ppc_porcentaje") is not None]
        if not ppc_valores:
            return None
            
        return sum(ppc_valores) / len(ppc_valores)

    def _crear_resumen_ppc(self, pod: Any, stats: Dict[str, Any]) -> None:
        """Crea un registro de resumen de PPC"""
        try:
            from pretdb.models import ResumenPPC
            resumen, created = ResumenPPC.objects.update_or_create(
                pod=pod,
                defaults={
                    'total_dias': stats.get("total_dias", 0),
                    'ppc_promedio': _to_ratio(stats.get("ppc_promedio")),
                    'dias_con_ppc': stats.get("ppc_diarios_procesados", 0),
                }
            )
            if created:
                logger.info(f"✅ Resumen PPC creado para POD {pod.numero_pod}")
        except Exception as e:
            logger.warning(f"No se pudo crear resumen de PPC: {e}")

# Registro en el sistema de loaders
def register_loader():
    from etl.registry import register_loader
    register_loader("ppc")(PPCLoader)