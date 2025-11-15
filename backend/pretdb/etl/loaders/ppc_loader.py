# pretdb/etl/loaders/ppc_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
from django.db import transaction
from django.apps import apps

from pretdb.etl.loaders.registry import register_loader

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

def _to_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val)))
    except (TypeError, ValueError):
        return None


@register_loader("ppc")
class PPCLoader:
    """
    Carga datos de PPC (Percent Plan Complete) desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.PpcSemanal = _get_model(pretdb, "PpcSemanal")
        self.PpcDiario = _get_model(pretdb, "PpcDiario")

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
        
        if not self.Pod or not self.PpcDiario or not self.PpcSemanal:
            error_msg = "Modelos Pod/Ppc no encontrados"
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
            registros: List[Dict[str, Any]] = []
            fechas_validas: List[date] = []
            total_programadas = 0
            total_realizadas = 0
            ratios: List[Decimal] = []

            for row_data in flat_rows:
                fecha = _to_date(row_data.get("fecha"))
                if not fecha:
                    warnings.setdefault("fechas_invalidas", []).append(f"Fila sin fecha válida: {row_data}")
                    continue

                dia_semana = _truncate(row_data.get("dia_semana"), 20)
                programadas = _to_int(row_data.get("programadas"))
                realizadas = _to_int(row_data.get("realizadas"))
                ratio = _to_ratio(row_data.get("ppc_porcentaje"))

                if programadas:
                    total_programadas += programadas
                if realizadas:
                    total_realizadas += realizadas
                if ratio is not None:
                    ratios.append(ratio)

                registros.append(
                    {
                        "fecha": fecha,
                        "dia_semana": dia_semana,
                        "programadas": programadas,
                        "realizadas": realizadas,
                        "ratio": ratio,
                    }
                )
                fechas_validas.append(fecha)

            if dry_run:
                ppc_diarios_procesados = len(registros)
            else:
                semanal_obj = None
                if self.PpcSemanal and fechas_validas:
                    fecha_inicio = min(fechas_validas)
                    fecha_fin = max(fechas_validas)
                    promedio = sum(ratios) / len(ratios) if ratios else None
                    try:
                        semanal_obj, created = self.PpcSemanal.objects.update_or_create(
                            pod=pod,
                            fecha_inicio=fecha_inicio,
                            fecha_fin=fecha_fin,
                            defaults={
                                "ppc_total": promedio,
                                "programadas_total": total_programadas,
                                "realizadas_total": total_realizadas,
                            },
                        )
                        if created:
                            ppc_semanales_procesados = 1
                            total_rows += 1
                    except Exception as exc:
                        warnings["ppc_semanal"] = str(exc)

                if self.PpcDiario and semanal_obj:
                    for item in registros:
                        try:
                            _, created = self.PpcDiario.objects.update_or_create(
                                ppc=semanal_obj,
                                fecha=item["fecha"],
                                defaults={
                                    "dia_nombre": item["dia_semana"],
                                    "programadas": item["programadas"],
                                    "realizadas": item["realizadas"],
                                    "ppc_dia": item["ratio"],
                                },
                            )
                            if created:
                                ppc_diarios_procesados += 1
                                total_rows += 1
                        except Exception as exc:
                            warnings.setdefault("ppc_diario", []).append(f"{item['fecha']}: {exc}")

            stats = {
                "ppc_diarios_procesados": ppc_diarios_procesados,
                "ppc_semanales_procesados": ppc_semanales_procesados,
                "total_dias": len(registros),
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
