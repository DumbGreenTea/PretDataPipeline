from __future__ import annotations
from typing import Dict, Any, Optional
import logging
from django.db import transaction
from django.apps import apps

logger = logging.getLogger(__name__)

# ----------------- helpers pequeños y tolerantes -----------------

def _has_field(model, name: str) -> bool:
    try:
        return any(getattr(f, "name", None) == name for f in model._meta.get_fields())
    except Exception:
        return False

def _pick_field(model, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if _has_field(model, c):
            return c
    return None

def _first_non_empty(d: dict, keys: list[str]):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "<NA>"):
            return v
    return None

def _build_defaults(model, source: dict, mapping: Dict[str, list[str]]) -> Dict[str, Any]:
    """
    mapping: {dest_field_en_modelo: [candidatos_en_source]}
    Solo incluye campos que EXISTEN en el modelo y que tienen valor en source.
    """
    out: Dict[str, Any] = {}
    for dest, src_keys in mapping.items():
        if not _has_field(model, dest):
            continue
        v = _first_non_empty(source, src_keys)
        if v is not None:
            out[dest] = v
    return out


class CompromisosLoader:
    """
    Persiste lo producido por CompromisosTransformer.
    Tolera variaciones de modelos/campos:
      - Modelo principal: Compromiso / Compromisos / RegistroCompromiso / CompromisoPOD
      - Resumen por empresa (opcional): CompromisoResumen / CompromisosResumen
    Clave natural preferida:
      (pod, row_id) si el modelo tiene 'row_id'|'uid'|'hash_id'|'slug'.
      Si no, (pod, item, descripcion) mapeando a nombres existentes.
    """
    def __init__(self) -> None:
        pretdb = "pretdb"
        # Modelos principales (elige el primero que exista)
        self.Pod = apps.get_model(pretdb, "Pod")
        self.Model = (
            apps.get_model(pretdb, "Compromiso", require_ready=False)
            or apps.get_model(pretdb, "Compromisos", require_ready=False)
            or apps.get_model(pretdb, "RegistroCompromiso", require_ready=False)
            or apps.get_model(pretdb, "CompromisoPOD", require_ready=False)
        )
        # Resumen (opcional)
        self.Resumen = (
            apps.get_model(pretdb, "CompromisoResumen", require_ready=False)
            or apps.get_model(pretdb, "CompromisosResumen", require_ready=False)
        )

        # Campo clave de identidad por-fila si existe
        self.key_hash_field = None
        if self.Model:
            self.key_hash_field = _pick_field(self.Model, ["row_id", "uid", "hash_id", "slug"])

        # Alternativas para item y descripcion (si no hay hash_id)
        self.item_field = self.Model and _pick_field(self.Model, ["item", "numero", "n_item"])
        self.desc_field = self.Model and _pick_field(self.Model, ["descripcion", "detalle", "texto"])

        # Campo de categoría/área (opcional)
        self.categoria_field = self.Model and _pick_field(self.Model, ["categoria", "disciplina", "area", "area_tematica"])

        # Campo responsable/empresa (opcional)
        self.responsable_field = self.Model and _pick_field(self.Model, ["responsable", "empresa", "owner", "asignado_a"])

        # Campos de fechas (opcionales)
        self.fecha_toma_field       = self.Model and _pick_field(self.Model, ["fecha_toma", "fecha_compromiso", "fecha_inicio"])
        self.fecha_cierre_proj_field= self.Model and _pick_field(self.Model, ["fecha_cierre_proyectada", "fecha_cierre_plan", "fecha_cierre_planificada"])
        self.fecha_cierre_eff_field = self.Model and _pick_field(self.Model, ["fecha_cierre_efectiva", "fecha_cierre_real", "fecha_cierre"])

        # Campos de estado
        self.estado_raw_field   = self.Model and _pick_field(self.Model, ["status_raw", "estado_raw"])
        self.estado_canon_field = self.Model and _pick_field(self.Model, ["estado", "estado_canon", "estado_normalizado", "status"])

        # Observación
        self.obs_field = self.Model and _pick_field(self.Model, ["observacion", "observaciones", "nota", "comentario"])

    @transaction.atomic
    def persist(self, pod, out: dict, dry_run: bool = False) -> dict:
        stats = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "resumen_upserted": 0,
            "mode": "dry" if dry_run else "write",
        }

        rows = out.get("flat_rows") or []
        if not rows:
            return stats

        # Validaciones mínimas
        if pod is None or not self.Pod:
            logger.warning("CompromisosLoader: 'pod' no provisto o modelo Pod inexistente. Skip.")
            stats["skipped"] = len(rows)
            return stats
        if not self.Model:
            logger.warning("CompromisosLoader: modelo de compromisos no encontrado. Skip.")
            stats["skipped"] = len(rows)
            return stats

        # -------- upsert por fila --------
        for r in rows:
            try:
                # Construcción de clave natural
                key_kwargs = {}
                if _has_field(self.Model, "pod"):
                    key_kwargs["pod"] = pod

                if self.key_hash_field and r.get("row_id"):
                    key_kwargs[self.key_hash_field] = r["row_id"]
                else:
                    # Fallback: (pod, item, descripcion) usando los nombres que existan
                    if self.item_field and r.get("item") is not None:
                        key_kwargs[self.item_field] = r["item"]
                    if self.desc_field and r.get("descripcion"):
                        key_kwargs[self.desc_field] = r["descripcion"]

                # Si no tenemos ninguna clave además de 'pod', crearemos duplicados:
                if len(key_kwargs) == (1 if "pod" in key_kwargs else 0):
                    # evitar desastre: si no hay clave, saltar en modo write (en dry contamos como created simulado)
                    if dry_run:
                        stats["created"] += 1
                        continue
                    stats["skipped"] += 1
                    continue

                # Defaults tolerantes
                defaults_map = {
                    # Campos básicos
                    self.categoria_field or "categoria": ["categoria"],
                    self.responsable_field or "responsable": ["responsable"],
                    self.fecha_toma_field or "fecha_toma": ["fecha_toma"],
                    self.fecha_cierre_proj_field or "fecha_cierre_proyectada": ["fecha_cierre_proyectada"],
                    self.fecha_cierre_eff_field or "fecha_cierre_efectiva": ["fecha_cierre_efectiva"],
                    self.estado_raw_field or "status_raw": ["status_raw"],
                    self.estado_canon_field or "estado": ["estado"],
                    self.obs_field or "observacion": ["observacion"],
                }
                # Quitar claves None (cuando el field no existe en el modelo)
                defaults_map = {k: v for k, v in defaults_map.items() if isinstance(k, str) and _has_field(self.Model, k)}

                if dry_run:
                    # Simulación simple: asumimos 'created' si no hay hash -> created, si hay -> updated
                    stats["created" if self.key_hash_field else "updated"] += 1
                    continue

                defaults = _build_defaults(self.Model, r, defaults_map)
                obj, created = self.Model.objects.update_or_create(defaults=defaults, **key_kwargs)
                stats["created" if created else "updated"] += 1

            except Exception as e:
                logger.debug("Compromiso upsert saltado por error (%s)", e)
                stats["skipped"] += 1

        # -------- resumen por empresa (opcional) --------
        if self.Resumen and isinstance(out.get("summary"), dict):
            resumen_src = (out["summary"] or {}).get("por_empresa") or {}
            for empresa, payload in resumen_src.items():
                try:
                    key = {}
                    if _has_field(self.Resumen, "pod"): key["pod"] = pod
                    # campo empresa
                    empresa_field = _pick_field(self.Resumen, ["empresa", "responsable", "owner"])
                    if empresa_field:
                        key[empresa_field] = empresa
                    else:
                        # si no hay campo empresa, no podemos persistir resumen
                        continue

                    defaults = {}
                    for dest, src in {
                        "numero_compromiso": "numero_compromiso",
                        "abierto": "abierto",
                        "cerrado": "cerrado",
                    }.items():
                        if _has_field(self.Resumen, dest) and (src in payload):
                            defaults[dest] = payload[src]

                    if dry_run:
                        stats["resumen_upserted"] += 1
                    else:
                        _, _created = self.Resumen.objects.update_or_create(defaults=defaults, **key)
                        stats["resumen_upserted"] += 1
                except Exception as e:
                    logger.debug("Resumen por empresa omitido (%s)", e)

        return stats