# pretdb/etl/loaders/dotacion_maquinaria_loader.py
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

def _to_decimal(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        try:
            return Decimal(str(val))
        except:
            return None
    try:
        return Decimal(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return None

def _to_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        try:
            return int(val)
        except:
            return None
    try:
        return int(float(str(val).replace(',', '.')))
    except (ValueError, TypeError):
        return None

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s


class DotacionMaquinariaLoader:
    """
    Carga datos de Dotación y Maquinaria desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.DotacionPersonal = _get_model(pretdb, "DotacionPersonal")
        self.MaquinariaEquipo = _get_model(pretdb, "MaquinariaEquipo")
        self.CompromisoMaquinaria = _get_model(pretdb, "CompromisoMaquinaria")

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
        Persiste los datos de Dotación y Maquinaria
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.DotacionPersonal or not self.MaquinariaEquipo:
            error_msg = "Modelos requeridos no encontrados"
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
            warnings["sin_datos"] = "No hay datos de Dotación y Maquinaria para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        dotacion_procesada = 0
        maquinaria_procesada = 0
        compromisos_procesados = 0

        try:
            for row_data in flat_rows:
                # Determinar si es dotación personal o maquinaria
                es_dotacion = bool(row_data.get("cargo"))
                es_maquinaria = bool(row_data.get("equipo"))
                
                if dry_run:
                    if es_dotacion:
                        logger.debug(f"🔶 DRY RUN - Dotación: {row_data.get('cargo')}")
                    elif es_maquinaria:
                        logger.debug(f"🔶 DRY RUN - Maquinaria: {row_data.get('equipo')}")
                    continue

                # 1. Procesar dotación personal
                if es_dotacion:
                    try:
                        dotacion_obj, created = self.DotacionPersonal.objects.update_or_create(
                            pod=pod,
                            cargo=_truncate(row_data.get("cargo"), 100),
                            defaults={
                                'turno_a': _to_int(row_data.get("turno_a")),
                                'turno_b': _to_int(row_data.get("turno_b")),
                                'total': self._calcular_total_personal(row_data),
                            }
                        )
                        if created:
                            dotacion_procesada += 1
                            total_rows += 1
                    except Exception as e:
                        cargo = row_data.get('cargo', 'Sin cargo')
                        warnings.setdefault("dotacion", []).append(f"'{cargo}': {e}")

                # 2. Procesar maquinaria y equipos
                if es_maquinaria:
                    try:
                        maquinaria_obj, created = self.MaquinariaEquipo.objects.update_or_create(
                            pod=pod,
                            equipo=_truncate(row_data.get("equipo"), 100),
                            defaults={
                                'peak': _to_int(row_data.get("peak")),
                                'proyectado': _to_int(row_data.get("proyectado")),
                                'total_en_obra': _to_int(row_data.get("total_en_obra")),
                                'equipos_en_falla': _to_int(row_data.get("equipos_en_falla")),
                                'equipo_en_mantencion': _to_int(row_data.get("equipo_en_mantencion")),
                                'operadores_disponibles': _to_int(row_data.get("operadores_disponibles")),
                                'equipos_operativos': _to_int(row_data.get("equipos_operativos")),
                                'reserva': _to_int(row_data.get("reserva")),
                                'observacion': _truncate(row_data.get("observacion"), 200),
                                'descripcion_falla': _truncate(row_data.get("descripcion_falla"), 500),
                            }
                        )
                        if created:
                            maquinaria_procesada += 1
                            total_rows += 1

                        # 3. Procesar compromisos de maquinaria si existen
                        compromiso_creado = self._procesar_compromisos(
                            maquinaria_obj, row_data, warnings
                        )
                        if compromiso_creado:
                            compromisos_procesados += 1
                            total_rows += 1
                            
                    except Exception as e:
                        equipo = row_data.get('equipo', 'Sin equipo')
                        warnings.setdefault("maquinaria", []).append(f"'{equipo}': {e}")

            # Resumen final
            stats = {
                "dotacion_procesada": dotacion_procesada,
                "maquinaria_procesada": maquinaria_procesada,
                "compromisos_procesados": compromisos_procesados,
                "total_filas": len(flat_rows),
                "cargos_unicos": len(set(r.get("cargo") for r in flat_rows if r.get("cargo"))),
                "equipos_unicos": len(set(r.get("equipo") for r in flat_rows if r.get("equipo"))),
            }
            
            if not dry_run:
                logger.info(f"✅ Dotación y Maquinaria procesada: {dotacion_procesada} dotaciones, {maquinaria_procesada} equipos, {compromisos_procesados} compromisos")
            else:
                logger.info(f"🔶 DRY RUN - Dotación y Maquinaria: {len(flat_rows)} filas a procesar")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de Dotación y Maquinaria: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _calcular_total_personal(self, row_data: Dict[str, Any]) -> Optional[int]:
        """Calcula el total de personal sumando turno A y B"""
        turno_a = _to_int(row_data.get("turno_a"))
        turno_b = _to_int(row_data.get("turno_b"))
        
        if turno_a is None and turno_b is None:
            return None
        
        total = 0
        if turno_a is not None:
            total += turno_a
        if turno_b is not None:
            total += turno_b
            
        return total if total > 0 else None

    def _procesar_compromisos(self, maquinaria_obj: Any, row_data: Dict[str, Any], warnings: Dict[str, Any]) -> bool:
        """Procesa los compromisos de equipo y operador"""
        try:
            compromiso_equipo = row_data.get("compromiso_equipo")
            fecha_compromiso_equipo = _to_date(row_data.get("fecha_compromiso_equipo"))
            compromiso_operador = row_data.get("compromiso_operador")
            fecha_compromiso_operador = _to_date(row_data.get("fecha_compromiso_operador"))
            
            # Solo crear compromiso si hay datos
            if not any([compromiso_equipo, fecha_compromiso_equipo, compromiso_operador, fecha_compromiso_operador]):
                return False
            
            if self.CompromisoMaquinaria:
                compromiso_obj, created = self.CompromisoMaquinaria.objects.update_or_create(
                    maquinaria_equipo=maquinaria_obj,
                    defaults={
                        'compromiso_equipo': _truncate(compromiso_equipo, 200),
                        'fecha_compromiso_equipo': fecha_compromiso_equipo,
                        'compromiso_operador': _truncate(compromiso_operador, 200),
                        'fecha_compromiso_operador': fecha_compromiso_operador,
                    }
                )
                return created
            return False
            
        except Exception as e:
            equipo = row_data.get('equipo', 'Sin equipo')
            warnings.setdefault("compromisos", []).append(f"'{equipo}': {e}")
            return False

    def _crear_resumen_dotacion(self, pod: Any, stats: Dict[str, Any]) -> None:
        """Crea un registro de resumen de dotación y maquinaria"""
        try:
            from pretdb.models import ResumenDotacionMaquinaria
            resumen, created = ResumenDotacionMaquinaria.objects.update_or_create(
                pod=pod,
                defaults={
                    'total_personal': stats.get("dotacion_procesada", 0),
                    'total_equipos': stats.get("maquinaria_procesada", 0),
                    'total_compromisos': stats.get("compromisos_procesados", 0),
                    'cargos_unicos': stats.get("cargos_unicos", 0),
                    'equipos_unicos': stats.get("equipos_unicos", 0),
                }
            )
            if created:
                logger.info(f"✅ Resumen Dotación/Maquinaria creado para POD {pod.numero_pod}")
        except Exception as e:
            logger.warning(f"No se pudo crear resumen de Dotación/Maquinaria: {e}")

# Registro en el sistema de loaders
def register_loader():
    from etl.registry import register_loader
    register_loader("dotacion_y_maquinaria")(DotacionMaquinariaLoader)