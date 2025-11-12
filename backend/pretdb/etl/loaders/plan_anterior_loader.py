from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import re
from django.db import transaction

from pretdb.models import (
    Pod,
    PlanAnterior,
    PlanAnteriorHoras,
    PlanAnteriorCnc,
    PlanAnteriorFront,
)

# ========= utilidades locales (solo loader) =========

def _to_decimal_2(val) -> Optional[Decimal]:
    if val is None: return None
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None

def _to_decimal_pct(val) -> Optional[Decimal]:
    if val is None: return None
    try:
        v = float(val)
        if 0 <= v <= 1: v *= 100.0
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None: return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s

def _parse_hhmm(s: Optional[str]) -> Optional[str]:
    if not s: return None
    s = s.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m: return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None

class PlanAnteriorLoader:
    """
    Idempotente + transaccional.
    Clave natural PlanAnterior:
      (pod, id_p6, area_trabajo, descripcion_item, fecha)
    """
    def __init__(self) -> None:
        pass

    @transaction.atomic
    def persist(self, pod: Pod, tr_out: Dict[str, Any], dry_run: bool = False) -> Dict[str, int]:
        stats = dict(created_plan=0, updated_plan=0, created_horas=0, created_cnc=0, created_fronts=0, skipped_rows=0, mode=("dry" if dry_run else "write"))

        flat_rows: List[Dict[str, Any]] = tr_out.get("flat_rows") or []
        horarios: List[Dict[str, Any]] = tr_out.get("horarios_inicio") or []

        # --- PlanAnterior / Horas / CNC ---
        for row in flat_rows:
            fecha = row.get("fecha") or pod.fecha
            key = dict(
                pod=pod,
                id_p6=_truncate(row.get("id_p6"), 50),
                area_trabajo=_truncate(row.get("area_trabajo"), 30),
                descripcion_item=_truncate(row.get("descripcion_item"), 50),
                fecha=fecha,
            )

            defaults = dict(
                numero_pod=int(row["nro_pod"]) if (row.get("nro_pod") and str(row.get("nro_pod")).isdigit()) else None,
                tipo_plan=_truncate(row.get("tipo_tc"), 30),
                responsable=_truncate(row.get("responsable"), 100),
                unidad=_truncate(row.get("unidad"), 10),
                cantidad_programada=_to_decimal_2(row.get("cant_programada")),
                cantidad_real=_to_decimal_2(row.get("cant_real")),
                cumplimiento=_to_decimal_pct(row.get("cumplimiento")),
                realizada=row.get("realizada"),  # True/False/None
            )

            if dry_run:
                exists = PlanAnterior.objects.filter(**key).exists()
                stats["updated_plan" if exists else "created_plan"] += 1
            else:
                pa, created = PlanAnterior.objects.update_or_create(defaults=defaults, **key)
                stats["created_plan" if created else "updated_plan"] += 1

                # Horas (1 por plan)
                sem_prog = row.get("sem_cant_prog")
                sem_real = row.get("sem_cant_real")
                sem_avance = row.get("sem_avance")

                horas_defaults = dict(
                    cantidad_programada=_to_decimal_2(sem_prog if sem_prog is not None else row.get("cant_programada")),
                    cantidad_real=_to_decimal_2(sem_real if sem_real is not None else row.get("cant_real")),
                    horas_hombre_programadas=None,
                    horas_hombre_reales=None,
                    porcentaje_avance=_to_decimal_pct(sem_avance if sem_avance is not None else row.get("cumplimiento")),
                )

                pah, created_h = PlanAnteriorHoras.objects.get_or_create(plan_anterior=pa, defaults=horas_defaults)
                if created_h:
                    stats["created_horas"] += 1
                else:
                    # update si cambió algo
                    PlanAnteriorHoras.objects.filter(id=pah.id).update(**horas_defaults)

                # CNC (solo si hay info útil)
                cnc_fields = (
                    row.get("cnc_causa"),
                    row.get("cnc_subcausa"),
                    row.get("cnc_tipo"),
                    row.get("cnc_descripcion"),
                    row.get("cnc_responsable"),
                )
                if any(v not in (None, "", "<NA>", "-") for v in cnc_fields):
                    _, created_c = PlanAnteriorCnc.objects.get_or_create(
                        plan_anterior=pa,
                        causa=_truncate(row.get("cnc_causa"), 100),
                        subcausa=_truncate(row.get("cnc_subcausa"), 100),
                        tipo=_truncate(row.get("cnc_tipo"), 30),
                        descripcion_cnc=_truncate(row.get("cnc_descripcion"), 200),
                        defaults={"responsable": _truncate(row.get("cnc_responsable"), 100)},
                    )
                    if created_c:
                        stats["created_cnc"] += 1

        # --- Frentes "Horario de inicio" ---
        if horarios:
            if dry_run:
                stats["created_fronts"] += len([h for h in horarios if (h.get("frente") or _parse_hhmm(h.get("hora_inicio")))])
            else:
                holder_pa, _ = PlanAnterior.objects.get_or_create(
                    pod=pod,
                    id_p6=None,
                    area_trabajo=None,
                    descripcion_item="Horario de inicio del día",
                    fecha=pod.fecha,
                    defaults=dict(
                        numero_pod=None, tipo_plan=None, responsable=None, unidad=None,
                        cantidad_programada=None, cantidad_real=None, cumplimiento=None, realizada=None,
                    ),
                )
                for fr in horarios:
                    frente = _truncate(fr.get("frente"), 50)
                    hora_str = _parse_hhmm(fr.get("hora_inicio"))
                    if (frente is None or frente == "") and hora_str is None:
                        continue
                    _, created_f = PlanAnteriorFront.objects.get_or_create(
                        plan_anterior=holder_pa, frente=frente,
                        defaults={"hora_inicio_efectivo": hora_str, "comentario": None},
                    )
                    if created_f:
                        stats["created_fronts"] += 1

        return stats