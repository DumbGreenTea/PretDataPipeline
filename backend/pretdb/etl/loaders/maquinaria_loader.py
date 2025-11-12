from __future__ import annotations
from typing import Dict, Any, Optional
import logging

from django.db import transaction
from django.apps import apps

logger = logging.getLogger(__name__)

# ---------- helpers pequeños ----------

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

def _build_defaults(model, source: dict, mapping: Dict[str, list[str]]) -> Dict[str, Any]:
    """
    Toma un mapping {dest_field: [candidatos_en_source]} y produce un dict solo con
    los campos que EXISTEN en el modelo y están presentes en source.
    """
    out: Dict[str, Any] = {}
    for dest, src_keys in mapping.items():
        if not _has_field(model, dest):
            continue
        for k in src_keys:
            if k in source and source[k] is not None and source[k] != "":
                out[dest] = source[k]
                break
    return out


class DotacionMaquinariaLoader:
    """
    Persiste lo producido por DotacionMaquinariaTransformer.
    Tolera variaciones de modelos/fields:
      - Dotación:  DotacionPersonal / DotacionEquipo
      - Equipos:   DotacionEquipos / MaquinariaEquipo / Maquinaria / EquipoCompromiso (adjunto)
    Clave natural aproximada:
      - Dotación:  (pod, cargo)
      - Equipos:   (pod, <key_field: descripcion_equipo|equipo|nombre>)
    """
    def __init__(self) -> None:
        # Resolve modelos de forma flexible
        pretdb = "pretdb"
        self.Pod = apps.get_model(pretdb, "Pod")
        self.DotacionModel = (
            apps.get_model(pretdb, "DotacionPersonal", require_ready=False)
            or apps.get_model(pretdb, "DotacionEquipo", require_ready=False)
        )
        self.EquiposModel = (
            apps.get_model(pretdb, "DotacionEquipos", require_ready=False)
            or apps.get_model(pretdb, "MaquinariaEquipo", require_ready=False)
            or apps.get_model(pretdb, "Maquinaria", require_ready=False)
            or apps.get_model(pretdb, "EquipoCompromiso", require_ready=False)  # fallback muy laxo
            or apps.get_model(pretdb, "DotacionEquipo", require_ready=False)
        )
        # Relación de compromisos (si existe)
        self.EquipoCompromiso = apps.get_model(pretdb, "EquipoCompromiso", require_ready=False)

        # Campo clave de equipos (elegimos lo que exista primero)
        self.equipo_key_field = None
        if self.EquiposModel:
            self.equipo_key_field = _pick_field(self.EquiposModel, ["descripcion_equipo", "equipo", "nombre"])

    @transaction.atomic
    def persist(self, pod, out: dict, dry_run: bool = False) -> dict:
        stats = {
            "created_dotacion": 0,
            "updated_dotacion": 0,
            "created_equipos": 0,
            "updated_equipos": 0,
            "created_compromisos": 0,
            "skipped_rows": 0,
            "mode": "dry" if dry_run else "write",
        }

        rows = out.get("flat_rows") or []
        if not rows:
            return stats

        # Validaciones mínimas
        if pod is None or not self.Pod:
            logger.warning("DotacionMaquinariaLoader: 'pod' no provisto o modelo Pod inexistente. Skip.")
            stats["skipped_rows"] = len(rows)
            return stats

        # ---------- DOTACIÓN ----------
        for row in rows:
            cargo = (row.get("cargo") or "").strip()
            turno_a = row.get("turno_a")
            turno_b = row.get("turno_b")
            equipo  = (row.get("equipo") or "").strip()

            # 1) Dotación (si hay algo relacionado a personal)
            if self.DotacionModel and (cargo or turno_a is not None or turno_b is not None):
                if dry_run:
                    # simulamos upsert
                    stats["created_dotacion"] += 1
                else:
                    try:
                        defaults = _build_defaults(
                            self.DotacionModel,
                            row,
                            {
                                "cargo": ["cargo"],
                                "turno_a": ["turno_a"],
                                "turno_b": ["turno_b"],
                            },
                        )
                        # Clave natural: (pod, cargo) si el modelo tiene ambos
                        key = {}
                        if _has_field(self.DotacionModel, "pod"): key["pod"] = pod
                        if _has_field(self.DotacionModel, "cargo"): key["cargo"] = cargo or None
                        obj, created = self.DotacionModel.objects.update_or_create(defaults=defaults, **key)
                        stats["created_dotacion" if created else "updated_dotacion"] += 1
                    except Exception as e:
                        logger.debug("Dotacion upsert saltado (%s)", e)
                        stats["skipped_rows"] += 1

            # 2) Equipos (si hay equipo o métricas)
            has_metrics = any(row.get(k) not in (None, "") for k in [
                "peak", "proyectado", "total_en_obra", "equipos_en_falla", "equipo_en_mantencion",
                "operadores_disponibles", "equipos_operativos", "reserva",
                "observacion", "descripcion_falla", "compromiso_equipo", "fecha_compromiso_equipo",
                "compromiso_operador", "fecha_compromiso_operador",
            ])
            if self.EquiposModel and (equipo or has_metrics):
                if not self.equipo_key_field:
                    self.equipo_key_field = _pick_field(self.EquiposModel, ["descripcion_equipo", "equipo", "nombre"])
                key_field = self.equipo_key_field

                # construir defaults en función de fields existentes del modelo
                defaults_map = {
                    # nombres "estándar"
                    "equipo": ["equipo"],
                    "descripcion_equipo": ["equipo"],
                    "nombre": ["equipo"],
                    "peak": ["peak"],
                    "proyectado": ["proyectado"],
                    "proyectado_rev2": ["proyectado"],
                    "total_en_obra": ["total_en_obra"],
                    "total_obra": ["total_en_obra"],
                    "equipos_en_falla": ["equipos_en_falla"],
                    "en_falla": ["equipos_en_falla"],
                    "equipo_en_mantencion": ["equipo_en_mantencion"],
                    "en_mantencion": ["equipo_en_mantencion"],
                    "operadores_disponibles": ["operadores_disponibles"],
                    "equipos_operativos": ["equipos_operativos"],
                    "operativos": ["equipos_operativos"],
                    "reserva": ["reserva"],
                    "observacion": ["observacion"],
                    "descripcion_falla": ["descripcion_falla"],
                    "compromiso_equipo": ["compromiso_equipo"],
                    "fecha_compromiso_equipo": ["fecha_compromiso_equipo"],
                    "compromiso_operador": ["compromiso_operador"],
                    "fecha_compromiso_operador": ["fecha_compromiso_operador"],
                }
                if dry_run:
                    stats["created_equipos"] += 1
                else:
                    try:
                        defaults = _build_defaults(self.EquiposModel, row, defaults_map)
                        key = {}
                        if _has_field(self.EquiposModel, "pod"): key["pod"] = pod
                        if key_field and equipo and _has_field(self.EquiposModel, key_field):
                            key[key_field] = equipo

                        obj, created = self.EquiposModel.objects.update_or_create(defaults=defaults, **key)
                        stats["created_equipos" if created else "updated_equipos"] += 1

                        # Adjuntar compromisos si existe modelo y hay datos
                        if self.EquipoCompromiso and obj is not None and (
                            row.get("descripcion_falla") or row.get("compromiso_equipo")
                            or row.get("compromiso_operador")
                        ):
                            comp_defaults = _build_defaults(
                                self.EquipoCompromiso,
                                row,
                                {
                                    "descripcion_falla": ["descripcion_falla"],
                                    "compromiso_equipo": ["compromiso_equipo"],
                                    "fecha_equipo": ["fecha_compromiso_equipo"],
                                    "compromiso_operador": ["compromiso_operador"],
                                    "fecha_operador": ["fecha_compromiso_operador"],
                                },
                            )
                            fk_name = _pick_field(self.EquipoCompromiso, ["equipo", "dotacion_equipo", "maquinaria", "parent"])
                            if fk_name:
                                comp = self.EquipoCompromiso(**{fk_name: obj}, **comp_defaults)
                                comp.save()
                                stats["created_compromisos"] += 1
                    except Exception as e:
                        logger.debug("Equipos upsert saltado (%s)", e)
                        stats["skipped_rows"] += 1

        # Mensajes informativos si falta algo
        if not self.DotacionModel:
            logger.info("DotacionMaquinariaLoader: Modelo de dotación no encontrado; no se insertó personal.")
        if not self.EquiposModel:
            logger.info("DotacionMaquinariaLoader: Modelo de equipos no encontrado; no se insertó maquinaria.")

        return stats