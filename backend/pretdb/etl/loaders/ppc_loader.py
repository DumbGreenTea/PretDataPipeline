from __future__ import annotations
from typing import Dict, Any, Optional
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.db import transaction

logger = logging.getLogger(__name__)


def _qdec(val: Optional[float], places: int) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        d = Decimal(str(val))
        q = Decimal("1").scaleb(-places)
        return d.quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


class PPCLoader:
    """Loader for PPC data. Persists one row per fecha.

    Interface: persist(pod, out: dict, dry_run: bool=False) -> Dict[str, Any]
    """

    PROGRAMADAS_PLACES = 2
    REALIZADAS_PLACES = 2
    PPC_PLACES = 4

    def __init__(self) -> None:
        pass

    @transaction.atomic
    def persist(self, pod, out: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        flat_rows = out.get("flat_rows", []) or []
        stats = {
            "mode": "dry" if dry_run else "write",
            "rows_in": len(flat_rows),
            "created": 0,
            "updated": 0,
            "skipped": 0,
        }

        if not flat_rows:
            return stats

        try:
            from pretdb import models as mdl
        except Exception as e:
            logger.warning("PPCLoader: no se pudo importar pretdb.models: %s", e)
            stats["skipped"] = len(flat_rows)
            return stats

        # Resolve Pod
        PodModel = getattr(mdl, "Pod", None)
        pod_obj = pod

        if pod_obj is None:
            logger.info("PPCLoader: sin Pod asociado; se omite persistencia.")
            stats["skipped"] = len(flat_rows)
            return stats

        # Find PPC model
        PPCModel = None
        for name in ("PpcSemanal", "PpcDiario", "PPCDia", "PPC", "PpcDia"):
            PPCModel = getattr(mdl, name, None)
            if PPCModel is not None:
                break

        if PPCModel is None:
            logger.info("PPCLoader: no se encontró modelo PPC en pretdb.models; se omite persistencia.")
            stats["skipped"] = len(flat_rows)
            return stats

        created = 0
        updated = 0

        for row in flat_rows:
            fecha = row.get("fecha")
            if not isinstance(fecha, date):
                stats["skipped"] += 1
                continue

            programadas = _qdec(row.get("programadas"), self.PROGRAMADAS_PLACES)
            realizadas = _qdec(row.get("realizadas"), self.REALIZADAS_PLACES)
            ppc_ratio = _qdec(row.get("ppc_porcentaje"), self.PPC_PLACES)

            defaults = {
                # These keys may vary per model; try common names
                "programadas": programadas,
                "realizadas": realizadas,
                "ppc_ratio": ppc_ratio,
            }

            if dry_run:
                # a simple heuristic: count as created if not exists
                try:
                    exists = PPCModel.objects.filter(pod=pod_obj, fecha=fecha).exists()
                    if exists:
                        updated += 1
                    else:
                        created += 1
                except Exception:
                    stats["skipped"] += 1
                continue

            try:
                obj, was_created = PPCModel.objects.update_or_create(pod=pod_obj, fecha=fecha, defaults=defaults)
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as e:
                logger.warning("PPCLoader: fallo al guardar fecha=%s: %s", fecha, e)
                stats["skipped"] += 1

        stats["created"] = created
        stats["updated"] = updated
        return stats
