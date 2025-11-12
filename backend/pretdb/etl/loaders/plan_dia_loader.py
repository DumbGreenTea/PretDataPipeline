from __future__ import annotations
from typing import Dict, Any
from decimal import Decimal, InvalidOperation

from django.db import transaction
from pretdb.models import Pod, PlanDia, PlanDiaRestricciones  # <- tus modelos

def _to_int(x):
    if x is None:
        return None
    try:
        s = str(x).strip()
        if s == "":
            return None
        return int(float(s))
    except Exception:
        return None

def _to_decimal(x):
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None

def persist_plan_dia(pod: Pod, out: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """
    Persiste el resultado de PlanDiaTransformer.
    Requiere:
      - out["flat_rows"]: List[dict] con los campos del transformer.
    """
    rows = out.get("flat_rows") or []
    stats = {
        "mode": "dry" if dry_run else "write",
        "rows_in": len(rows),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "restricciones_created": 0,
    }

    if not rows:
        return stats

    def upsert_row(row: Dict[str, Any]):
        """
        Lookup idempotente: (pod, fecha, id_p6, descripcion_item).
        Si fecha es None, igual aplica (Django filtra por NULL).
        """
        numero_pod = _to_int(row.get("nro_pod"))
        defaults = {
            "numero_pod": numero_pod,
            "tipo_plan": row.get("tipo_tc"),
            "fecha": row.get("fecha"),
            "responsable": row.get("responsable"),
            "id_p6": row.get("id_p6"),
            "ruta_critica": row.get("ruta_critica"),
            "area_trabajo": row.get("area_trabajo"),
            "descripcion_item": row.get("descripcion_item"),
            "unidad": row.get("unidad"),
            "cantidad_programada": _to_decimal(row.get("cant_programada")),
            "cantidad_proyectada_dia": _to_decimal(row.get("cant_proyectada_dia")),
            "riesgos_criticos": row.get("riesgos_criticos"),
        }

        lookup = dict(
            pod=pod,
            fecha=row.get("fecha"),
            id_p6=row.get("id_p6") or "",
            descripcion_item=row.get("descripcion_item") or "",
        )

        if dry_run:
            # Simular existencia: si tiene suficientes llaves lo contamos como update; si no, created
            # (si quisieras, aquí podrías hacer una query para mayor precisión incluso en dry)
            return "created", None

        obj, created = PlanDia.objects.update_or_create(defaults=defaults, **lookup)
        return ("created" if created else "updated"), obj

    @transaction.atomic
    def run():
        for row in rows:
            # Saltar filas que realmente no tienen identificación mínima
            if not (row.get("id_p6") or row.get("descripcion_item")):
                stats["skipped"] += 1
                continue

            status, obj = upsert_row(row)
            stats[status] += 1

            # Restricciones (si viene texto o responsable)
            restr_desc = row.get("restricciones")
            restr_resp = row.get("restricciones_resp")

            if restr_desc or restr_resp:
                if not dry_run and obj:
                    _, created_r = PlanDiaRestricciones.objects.get_or_create(
                        plan_dia=obj,
                        tipo=(restr_resp or "N/A"),
                        descripcion=(restr_desc or ""),
                    )
                    if created_r:
                        stats["restricciones_created"] += 1

    run()
    return stats