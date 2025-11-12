from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class AsistenciaLoader:
    """
    Loader para persistir la hoja 'asistencia'.
    API: persist(pod, transformed, dry_run=False) -> (rows_upserted:int, warnings:dict, errors:Optional[dict])
    """

    def persist(self, pod: Optional[Any], transformed: Dict[str, Any], dry_run: bool = False) -> Tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
        warnings: Dict[str, Any] = {}
        errors: Optional[Dict[str, Any]] = None

        if pod is None:
            warnings["pod"] = "pod is None, cannot persist asistencia"
            return 0, warnings, errors

        target_date = getattr(pod, "fecha", None)
        if target_date is None:
            warnings["pod_fecha"] = "pod.fecha is None; skipping asistencia persist"
            return 0, warnings, errors

        # contrato
        c = transformed.get("contrato") or {}
        numero, nombre = c.get("numero"), c.get("nombre")

        # fechas y persons
        dates_map: Dict[str, Optional[Any]] = transformed.get("dates_map") or {}
        # match exact
        dates_filtered: Dict[str, Any] = {k: d for k, d in dates_map.items() if d == target_date}

        if not dates_filtered:
            candidates = [(k, d) for k, d in dates_map.items() if d]
            if candidates:
                same_wd = [(k, d) for k, d in candidates if d.weekday() == target_date.weekday()]
                pool = same_wd or candidates
                k_best, d_best = min(pool, key=lambda kd: abs((kd[1] - target_date).days))
                if abs((d_best - target_date).days) <= 3:
                    dates_filtered = {k_best: d_best}
                else:
                    warnings["dates"] = f"No se encontró fecha igual o cercana a {target_date} en hoja Asistencia; no se insertará detalle."

        persons: List[Dict[str, Any]] = transformed.get("persons_detail") or []

        if not persons or not dates_filtered:
            logger.info("AsistenciaLoader: nothing to persist (persons=%s, dates=%s)", len(persons), len(dates_filtered))
            return 0, warnings, errors

        # si dry_run, no escribimos: contamos cuántas filas se hubieran upserteado
        if dry_run:
            rows_est = 0
            for p in persons:
                empresa = (p.get("empresa") or "").strip()
                codigo = (p.get("codigo") or "").strip()
                if not (empresa and codigo):
                    continue
                for _ in dates_filtered.items():
                    val = p.get(_[0])
                    if val is None:
                        continue
                    rows_est += 1
            logger.info("AsistenciaLoader dry_run -> rows_est=%s", rows_est)
            return rows_est, warnings, errors

        # Persistencia real: encapsular en transacción
        try:
            from django.db import transaction
            from pretdb import models as mdl
        except Exception as e:
            logger.warning("AsistenciaLoader: cannot import Django or models: %s", e)
            return 0, warnings, {"import_error": str(e)}

        Trabajador = getattr(mdl, "Trabajador", None)
        Asistencia = getattr(mdl, "Asistencia", None)
        AsistenciaDetalle = getattr(mdl, "AsistenciaDetalle", None)
        Contrato = getattr(mdl, "Contrato", None)

        rows_upserted = 0

        with transaction.atomic():
            # contrato header
            if (numero or nombre) and Contrato is not None:
                try:
                    nombre_codigo, nombre_contrato = (None, None)
                    if nombre:
                        # simple split fallback
                        parts = str(nombre).split("-", 1)
                        if len(parts) == 2:
                            nombre_codigo = parts[0].strip()
                            nombre_contrato = parts[1].strip()
                        else:
                            nombre_contrato = nombre
                    Contrato.objects.update_or_create(
                        pod=pod,
                        nombre_contrato=nombre_contrato,
                        nombre_codigo=nombre_codigo,
                        defaults={}
                    )
                except Exception as e:
                    warnings["contrato_header"] = f"No se pudo upsert contrato desde asistencia: {e}"

            for p in persons:
                empresa_raw = (p.get("empresa") or "").strip()
                empresa = empresa_raw[:15] if empresa_raw else None
                codigo_raw = (p.get("codigo") or "").strip()
                codigo = codigo_raw[:2] if codigo_raw else None
                if not (empresa and codigo):
                    continue

                nombre = (p.get("nombre") or "").strip()[:30]
                cargo = ((p.get("cargo") or "") or None)
                cargo = cargo.strip()[:20] if cargo else None
                correo = ((p.get("correo") or "") or None)
                correo = correo.strip()[:30] if correo else None

                if Trabajador is None:
                    warnings.setdefault("models", []).append("Trabajador model not found; skipping person creation")
                    continue

                try:
                    trab, _ = Trabajador.objects.update_or_create(
                        codigo_trabajador=codigo,
                        empresa=empresa,
                        defaults={
                            "nombre": nombre,
                            "cargo": cargo,
                            "correo": correo,
                        }
                    )
                except Exception as e:
                    logger.warning("AsistenciaLoader: fallo upsert trabajador %s/%s: %s", codigo, empresa, e)
                    warnings.setdefault("trab_error", []).append(str(e))
                    continue

                if Asistencia is None:
                    warnings.setdefault("models", []).append("Asistencia model not found; skipping")
                    continue

                try:
                    asis, _ = Asistencia.objects.get_or_create(pod=pod, trabajador=trab)
                except Exception as e:
                    logger.warning("AsistenciaLoader: fallo get_or_create Asistencia: %s", e)
                    warnings.setdefault("asis_error", []).append(str(e))
                    continue

                for dkey, fecha in dates_filtered.items():
                    val = p.get(dkey)
                    if val is None:
                        continue
                    presente = 1 if float(val) > 0 else 0
                    if AsistenciaDetalle is None:
                        warnings.setdefault("models", []).append("AsistenciaDetalle model not found; skipping detail")
                        continue
                    try:
                        AsistenciaDetalle.objects.update_or_create(
                            asistencia=asis,
                            fecha=fecha,
                            defaults={
                                "dia_nombre": dkey[:8],
                                "presente": presente,
                            }
                        )
                        rows_upserted += 1
                    except Exception as e:
                        logger.warning("AsistenciaLoader: fallo guardando detalle %s %s: %s", asis, fecha, e)
                        warnings.setdefault("detail_error", []).append(str(e))

        return rows_upserted, warnings, errors
