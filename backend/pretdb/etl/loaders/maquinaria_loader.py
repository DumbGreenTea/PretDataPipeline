# pretdb/etl/loaders/dotacion_maquinaria_loader.py
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


@register_loader("dotacion_y_maquinaria")
class DotacionMaquinariaLoader:
    """
    Persiste DotacionEquipo/EquipoCompromiso desde la hoja de dotación y maquinaria.
    """

    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.DotacionEquipo = _get_model(pretdb, "DotacionEquipo")
        self.EquipoCompromiso = _get_model(pretdb, "EquipoCompromiso")

    @transaction.atomic
    def persist(
        self,
        pod,
        out: dict,
        *,
        dry_run: bool = False,
        run_id: Optional[int] = None,
    ) -> Tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
        warnings: Dict[str, List[str]] = {}

        if not self.Pod or not self.DotacionEquipo:
            msg = "Modelos Pod o DotacionEquipo no encontrados"
            logger.error(msg)
            return 0, {}, {"models": msg}

        if not pod or not isinstance(pod, self.Pod):
            msg = "Pod no proporcionado o inválido"
            logger.error(msg)
            return 0, {}, {"pod": msg}

        flat_rows = out.get("flat_rows", []) or []
        if not flat_rows:
            warnings["sin_datos"] = ["Sin filas en dotación y maquinaria"]
            return 0, warnings, None

        total_rows = 0
        equipos_procesados = 0
        compromisos_procesados = 0

        try:
            for row_data in flat_rows:
                descripcion_equipo = _truncate(row_data.get("equipo"), 100)
                if not descripcion_equipo:
                    continue

                if dry_run:
                    equipos_procesados += 1
                    logger.debug("🔶 DRY RUN - Equipo: %s", descripcion_equipo)
                    continue

                defaults = {
                    "fecha": _to_date(row_data.get("fecha")) or pod.fecha,
                    "proyectado_rev2": _to_int(row_data.get("peak") or row_data.get("proyectado")),
                    "total_obra": _to_int(row_data.get("total_en_obra")),
                    "en_falla": _to_int(row_data.get("equipos_en_falla")),
                    "en_mantencion": _to_int(row_data.get("equipo_en_mantencion")),
                    "operadores_disponibles": _to_int(row_data.get("operadores_disponibles")),
                    "operativos": _to_int(row_data.get("equipos_operativos")),
                    "reserva": _to_int(row_data.get("reserva")),
                    "observacion": _truncate(row_data.get("observacion"), 200),
                }

                try:
                    equipo_obj, created = self.DotacionEquipo.objects.update_or_create(
                        pod=pod,
                        descripcion_equipo=descripcion_equipo,
                        defaults=defaults,
                    )
                    if created:
                        equipos_procesados += 1
                    total_rows += 1

                    if self._persist_compromiso(equipo_obj, row_data, warnings):
                        compromisos_procesados += 1
                        total_rows += 1
                except Exception as exc:
                    warnings.setdefault("equipos", []).append(f"{descripcion_equipo}: {exc}")

            stats = {
                "equipos_procesados": equipos_procesados,
                "compromisos_procesados": compromisos_procesados,
                "total_filas": len(flat_rows),
            }

            if not dry_run:
                logger.info(
                    "✅ Dotación/Maquinaria: %s equipos, %s compromisos",
                    equipos_procesados,
                    compromisos_procesados,
                )
            else:
                logger.info("🔶 DRY RUN - Dotación/Maquinaria: %s filas", len(flat_rows))

            return total_rows, {**warnings, **stats}, None
        except Exception as exc:
            logger.exception("Error en persistencia de Dotación y Maquinaria: %s", exc)
            return 0, warnings, {"persist": str(exc)}

    def _persist_compromiso(
        self,
        equipo_obj: Any,
        row_data: Dict[str, Any],
        warnings: Dict[str, List[str]],
    ) -> bool:
        if not self.EquipoCompromiso or not equipo_obj:
            return False

        compromiso_equipo = row_data.get("compromiso_equipo")
        compromiso_operador = row_data.get("compromiso_operador")
        fecha_equipo = _to_date(row_data.get("fecha_compromiso_equipo"))
        fecha_operador = _to_date(row_data.get("fecha_compromiso_operador"))
        descripcion_falla = row_data.get("descripcion_falla")

        if not any([compromiso_equipo, compromiso_operador, fecha_equipo, fecha_operador, descripcion_falla]):
            return False

        try:
            self.EquipoCompromiso.objects.update_or_create(
                equipo=equipo_obj,
                defaults={
                    "descripcion_falla": _truncate(descripcion_falla, 100),
                    "compromiso_equipo": _truncate(compromiso_equipo, 200),
                    "fecha_equipo": fecha_equipo,
                    "compromiso_operador": _truncate(compromiso_operador, 100),
                    "fecha_operador": fecha_operador,
                },
            )
            return True
        except Exception as exc:
            warnings.setdefault("compromisos", []).append(f"{equipo_obj.descripcion_equipo}: {exc}")
            return False
