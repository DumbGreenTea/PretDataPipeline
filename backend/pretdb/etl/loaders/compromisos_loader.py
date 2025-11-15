# pretdb/etl/loaders/compromisos_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime
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

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s

def _normalizar_estado(estado_raw: Optional[str]) -> str:
    """Normaliza el estado del compromiso"""
    if not estado_raw:
        return "desconocido"
    
    estado = str(estado_raw).strip().lower()
    if estado.startswith("cerrad"):
        return "cerrado"
    elif estado.startswith("abiert"):
        return "abierto"
    elif estado.startswith("inf"):  # INF, informativo
        return "informativo"
    else:
        return "desconocido"


@register_loader("compromisos")
class CompromisosLoader:
    """
    Carga datos de Compromisos desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Compromiso = _get_model(pretdb, "Compromiso")

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
        Persiste los datos de Compromisos
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings: Dict[str, List[str]] = {}
        errors = None
        
        if not self.Pod or not self.Compromiso:
            error_msg = "Modelos Pod o Compromiso no encontrados"
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
            warnings["sin_datos"] = "No hay datos de Compromisos para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        compromisos_procesados = 0

        try:
            for row_data in flat_rows:
                item_numero = _truncate(row_data.get("item"), 50)
                descripcion = row_data.get("descripcion")
                responsable = _truncate(row_data.get("responsable"), 100)
                estado_raw = row_data.get("status_raw")
                estado_normalizado = _normalizar_estado(estado_raw)

                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Compromiso: {item_numero} - {descripcion or ''}")
                    continue

                try:
                    compromiso_obj, created = self.Compromiso.objects.update_or_create(
                        pod=pod,
                        item=item_numero,
                        defaults={
                            'descripcion': descripcion,
                            'fecha_toma_compromiso': _to_date(row_data.get("fecha_toma")),
                            'fecha_cierre_proyectada': _to_date(row_data.get("fecha_cierre_proyectada")),
                            'fecha_cierre_efectiva': _to_date(row_data.get("fecha_cierre_efectiva")),
                            'responsable': responsable,
                            'status': estado_normalizado,
                                'observacion': row_data.get("observacion"),
                        }
                    )
                    if created:
                        compromisos_procesados += 1
                        total_rows += 1
                except Exception as e:
                    desc = descripcion or "Sin descripción"
                    warnings.setdefault("compromisos", []).append(f"Item {item_numero} - '{desc}': {e}")

            # Resumen final
            stats = {
                "compromisos_procesados": compromisos_procesados,
                "total_compromisos": len(flat_rows),
                "por_estado": summary.get("por_estado", {}),
                "por_categoria": summary.get("por_categoria", {}),
                "por_empresa": summary.get("por_empresa", {}),
            }
            
            if not dry_run:
                logger.info(f"✅ Compromisos procesados: {compromisos_procesados} registros actualizados")
                for estado, cantidad in stats["por_estado"].items():
                    logger.info(f"   - {estado}: {cantidad}")
            else:
                logger.info(f"🔶 DRY RUN - Compromisos: {len(flat_rows)} compromisos a procesar")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de Compromisos: %s", e)
            return 0, warnings, {"persist": str(e)}
