# pretdb/etl/loaders/sso_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, timedelta

from django.db import transaction
from pretdb.models import Sso, SsoCruzSeguridad, SsoEstadoDia, SsoReflexion, Pod

logger = logging.getLogger(__name__)


class SsoLoader:
    """Loader for SSO persistence.

    Persist signature: persist(pod: Pod, result: dict, dry_run: bool=False)
    Returns: (rows_upserted:int, warnings:Dict, errors:Optional[Dict])
    """

    def __init__(self) -> None:
        pass

    @transaction.atomic
    def persist(self, pod: Pod, result: Dict[str, Any], dry_run: bool = False) -> Tuple[int, Dict, Optional[Dict]]:
        warnings: Dict[str, Any] = {}
        errors: Optional[Dict[str, Any]] = None

        if not pod:
            warnings["pod_id"] = "Pod is required"
            return 0, warnings, errors

        target_date: date = pod.fecha

        dias: List[Dict[str, Any]] = result.get("dias_detail") or []
        summ: Dict[str, Any] = result.get("summary") or {}
        mes = summ.get("mes")
        anio = summ.get("anio")
        if not mes or not anio:
            warnings["periodo"] = "No se pudo determinar mes/año en SSO; no se insertará estado."
            self._upsert_reflexion_only(pod, target_date, summ, warnings, dry_run=dry_run)
            return 0, warnings, errors

        dates_map: Dict[int, date] = {}
        for d in dias:
            dia_num = d.get("dia")
            if dia_num:
                try:
                    d_fecha = date(anio, mes, dia_num)
                    dates_map[dia_num] = d_fecha
                except Exception:
                    pass

        estado_target: Optional[int] = None
        fecha_target: Optional[date] = None

        if target_date.month == mes and target_date.year == anio and target_date.day in dates_map:
            fecha_target = dates_map[target_date.day]
            day_entry = next((x for x in dias if x.get("dia") == target_date.day), None)
            if day_entry:
                estado_target = day_entry.get("estado")

        if fecha_target is None or estado_target is None:
            candidates = []
            for x in dias:
                dnum = x.get("dia")
                if not dnum:
                    continue
                f = dates_map.get(dnum)
                if not f:
                    continue
                candidates.append((f, x.get("estado")))
            if candidates:
                same_wd = [(f, st) for (f, st) in candidates if f.weekday() == target_date.weekday()]
                pool = same_wd or candidates
                f_best, st_best = min(pool, key=lambda t: abs((t[0] - target_date).days))
                if abs((f_best - target_date).days) <= 3:
                    fecha_target = f_best
                    estado_target = st_best

        if fecha_target is None:
            warnings["fecha_match"] = f"No se encontró día en la cruz para {target_date} (ni cercano ±3d)."
            self._upsert_reflexion_only(pod, target_date, summ, warnings, dry_run=dry_run)
            return 0, warnings, errors

        if estado_target is None:
            color_encontrado = "N/A"
            dia_obj = next((d for d in dias if d.get("dia") == fecha_target.day), None)
            if dia_obj:
                color_encontrado = dia_obj.get("color")
            warnings["estado_color"] = (
                f"El día {fecha_target} (color={color_encontrado}) no tiene "
                "color mapeado en COLOR_A_ESTADO; no se insertará estado."
            )
            self._upsert_reflexion_only(pod, target_date, summ, warnings, dry_run=dry_run)
            return 0, warnings, errors

        rows_upserted = 0

        if dry_run:
            # simulate
            rows_upserted = 1
            self._upsert_reflexion_only(pod, target_date, summ, warnings, dry_run=True)
            return rows_upserted, warnings, errors

        sso, _ = Sso.objects.get_or_create(pod=pod)

        # month bounds computation (simple, uses first/last day for mes)
        mes_start = date(anio, mes, 1)
        if mes == 12:
            mes_end = date(anio + 1, 1, 1) - timedelta(days=1)
        else:
            mes_end = date(anio, mes + 1, 1) - timedelta(days=1)

        cr, _ = SsoCruzSeguridad.objects.get_or_create(
            sso=sso, fecha_inicio=mes_start, fecha_fin=mes_end
        )

        SsoEstadoDia.objects.update_or_create(
            sso_cruz_seguridad=cr,
            fecha=fecha_target,
            defaults={"estado_dia": estado_target},
        )
        rows_upserted += 1

        self._upsert_reflexion_only(pod, target_date, summ, warnings, dry_run=False)

        return rows_upserted, warnings, errors

    def _upsert_reflexion_only(self, pod: Pod, fecha: date, summ: Dict[str, Any], warnings: Dict[str, Any], dry_run: bool = False) -> None:
        hall = summ.get("hallazgos")
        tv = summ.get("tarjeta_verde")
        poli = summ.get("policlinico")
        if hall is None and tv is None and poli is None:
            return
        try:
            if dry_run:
                return
            sso, _ = Sso.objects.get_or_create(pod=pod)
            SsoReflexion.objects.update_or_create(
                sso=sso,
                fecha=fecha,
                defaults={
                    "hallazgos": int(hall) if hall is not None else None,
                    "tarjeta_verde": int(tv) if tv is not None else None,
                    "traslado_policlinico": int(poli) if poli is not None else None,
                },
            )
        except Exception as e:
            warnings["reflexion"] = f"No se pudo upsert SsoReflexion: {e}"
