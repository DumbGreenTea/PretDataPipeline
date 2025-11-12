from __future__ import annotations
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class MatrizCNCLoader:
    """
    Loader para persistir la Matriz CNC (Primaria -> Secundarias).
    API: persist(pod, transformed, dry_run=False) -> stats dict
    """

    def persist(self, pod: Optional[Any], transformed: Dict[str, Any], dry_run: bool = False) -> Dict[str, int]:
        rows = transformed.get("flat_rows", []) or []
        if not rows:
            logger.info("MatrizCNCLoader: no hay filas para persistir.")
            return {"created": 0, "updated": 0, "skipped": 0}

        try:
            from pretdb import models as mdl
        except Exception:
            logger.warning("MatrizCNCLoader: no se pudo importar pretdb.models; se omite persistencia.")
            return {"created": 0, "updated": 0, "skipped": len(rows)}

        # Resolver modelos (nombres flexibles)
        PrimModel = (
            getattr(mdl, "CausaPrimaria", None)
            or getattr(mdl, "CNCPrimaria", None)
            or getattr(mdl, "MatrizCNCPrimaria", None)
        )
        SecModel = (
            getattr(mdl, "CausaSecundaria", None)
            or getattr(mdl, "CNCSubcausa", None)
            or getattr(mdl, "MatrizCNCSecundaria", None)
        )

        if PrimModel is None or SecModel is None:
            logger.info("MatrizCNCLoader: no se encontraron modelos Primaria/Secundaria; se omite persistencia.")
            return {"created": 0, "updated": 0, "skipped": len(rows)}

        prim_cache: Dict[str, Any] = {}
        created_sec, updated_sec, skipped = 0, 0, 0

        # Si dry_run, no hacemos writes; devolvemos conteos estimados
        if dry_run:
            # contar filas válidas
            valid = 0
            for row in rows:
                cod_sec = row.get("causa_secundaria_id")
                desc_sec = row.get("causa_secundaria_desc")
                if (cod_sec is None or str(cod_sec).strip() == "") and (desc_sec is None or str(desc_sec).strip() == ""):
                    continue
                valid += 1
            logger.info("MatrizCNCLoader: dry_run -> filas válidas=%s", valid)
            return {"created": 0, "updated": 0, "skipped": len(rows) - valid}

        # Persistencia real
        try:
            from django.db import transaction
        except Exception:
            transaction = None

        if transaction is not None:
            tx_ctx = transaction.atomic()
        else:
            class _DummyCtx:
                def __enter__(self):
                    return None
                def __exit__(self, *args):
                    return False
            tx_ctx = _DummyCtx()

        with tx_ctx:
            for row in rows:
                cod_prim = row.get("causa_primaria_id")
                desc_prim = row.get("causa_primaria_desc")
                cod_sec = row.get("causa_secundaria_id")
                desc_sec = row.get("causa_secundaria_desc")

                if (cod_sec is None or str(cod_sec).strip() == "") and (desc_sec is None or str(desc_sec).strip() == ""):
                    skipped += 1
                    continue

                key = (str(cod_prim or "").upper().strip())
                prim_obj = prim_cache.get(key)
                if prim_obj is None:
                    defaults = {"descripcion": desc_prim} if desc_prim is not None else {}
                    try:
                        prim_obj, _ = PrimModel.objects.update_or_create(codigo=cod_prim, defaults=defaults)
                    except Exception:
                        # Intentar sin campo 'codigo' - último recurso: crear/obtener por descripción
                        try:
                            prim_obj, _ = PrimModel.objects.update_or_create(descripcion=desc_prim, defaults={})
                        except Exception as e:
                            logger.warning("MatrizCNCLoader: fallo creando primaria (%s/%s): %s", cod_prim, desc_prim, e)
                            skipped += 1
                            continue
                    prim_cache[key] = prim_obj

                try:
                    obj, was_created = SecModel.objects.update_or_create(
                        primaria=prim_obj,
                        codigo=cod_sec,
                        defaults={"descripcion": desc_sec}
                    )
                    if was_created:
                        created_sec += 1
                    else:
                        updated_sec += 1
                except Exception as e:
                    logger.warning("MatrizCNCLoader: fallo guardando sec (%s/%s): %s", cod_sec, desc_sec, e)
                    skipped += 1

        logger.info("MatrizCNCLoader: secundarias creadas=%s, actualizadas=%s, saltadas=%s.", created_sec, updated_sec, skipped)
        return {"created": created_sec, "updated": updated_sec, "skipped": skipped}
