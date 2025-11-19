# pretdb/etl/loaders/portada_loader.py - VERSIÓN CORREGIDA
from __future__ import annotations
from typing import Dict, Any, Optional, List
import logging
import unicodedata
from datetime import date, datetime
from django.db import transaction, connection
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

def _normalize_name(nombre: str) -> Optional[str]:
    if not nombre:
        return None
    nombre_limpio = " ".join(str(nombre).split())
    return nombre_limpio or None

def _name_compare_key(nombre: Optional[str]) -> str:
    if not nombre:
        return ""
    base = " ".join(str(nombre).split()).lower()
    nfkd = unicodedata.normalize("NFD", base)
    sin_tildes = "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")
    return sin_tildes

def _pick_best_trabajador(candidates: List[Any]) -> Optional[Any]:
    if not candidates:
        return None

    def score(t):
        return (
            1 if getattr(t, "codigo_trabajador", None) else 0,
            1 if getattr(t, "empresa", None) else 0,
            1 if getattr(t, "cargo", None) else 0,
            1 if getattr(t, "correo", None) else 0,
            -getattr(t, "id", 0),
        )

    return sorted(candidates, key=score, reverse=True)[0]

def _get_or_create_trabajador(nombre: str, Trabajador_model):
    """Busca o crea un Trabajador por nombre (ignorando mayúsculas y espacios)."""
    if not nombre or not Trabajador_model:
        return None

    nombre_limpio = _normalize_name(nombre)
    if not nombre_limpio:
        return None

    try:
        candidatos = list(Trabajador_model.objects.filter(nombre__iexact=nombre_limpio))
        if not candidatos:
            key_objetivo = _name_compare_key(nombre_limpio)
            primer_token = nombre_limpio.split(" ")[0] if nombre_limpio else ""
            if primer_token and len(primer_token) >= 3:
                qs = Trabajador_model.objects.filter(nombre__istartswith=primer_token[:3])
            else:
                qs = Trabajador_model.objects.all()
            for candidato in qs:
                if _name_compare_key(getattr(candidato, "nombre", "")) == key_objetivo:
                    candidatos.append(candidato)

        trabajador_existente = _pick_best_trabajador(candidatos)
        if trabajador_existente:
            if trabajador_existente.nombre != nombre_limpio:
                trabajador_existente.nombre = nombre_limpio
                trabajador_existente.save(update_fields=["nombre"])
            logger.debug(f"✅ Trabajador encontrado: {nombre_limpio} (id={trabajador_existente.id})")
            return trabajador_existente

        trabajador = Trabajador_model.objects.create(nombre=nombre_limpio)
        logger.info(f"✅ Trabajador creado: {nombre_limpio}")
        return trabajador
    except Exception as e:
        logger.error(f"❌ Error creando/buscando trabajador {nombre_limpio}: {e}")
        return None

@register_loader("portada")
class PortadaLoader:
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Trabajador = _get_model(pretdb, "Trabajador")

    @transaction.atomic
    def persist(
        self,
        pod,  # se ignora, pero se deja para compatibilidad
        out: dict,
        *,
        dry_run: bool = False,
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "mode": "dry" if dry_run else "write",
            "pod_id": None,
            "pod_obj": None,
            "numero_pod": None,
            "created": False,
            "updated": False,
            "skipped": 0,
            "warnings": [],
        }

        if not self.Pod:
            stats["warnings"].append("Modelo Pod no encontrado.")
            stats["skipped"] = 1
            return stats

        rows = out.get("flat_rows") or []
        if isinstance(rows, dict):
            rows = [rows]

        if not rows:
            stats["warnings"].append("PortadaLoader: flat_rows vacío.")
            stats["skipped"] = 1
            return stats

        src = rows[0]

        # Extraer datos
        fecha_val = _to_date(_pick(src, "fecha"))
        nro_val = _pick(src, "pod_numero")
        turno_val = _pick(src, "turno")
        inicio_periodo_val = _to_date(_pick(src, "inicio_periodo_pod"))
        fin_periodo_val = _to_date(_pick(src, "termino_periodo_pod"))
        jefe_codelco_nombre = _pick(src, "jefe_turno_codelco")
        jefe_contratista_nombre = _pick(src, "jefe_turno_contratista")

        # Validar campos obligatorios
        if not nro_val:
            stats["warnings"].append("Falta pod_numero")
            stats["skipped"] = 1
            return stats

        if not fecha_val:
            stats["warnings"].append("Falta fecha")
            stats["skipped"] = 1
            return stats

        # ✅ CREAR TRABAJADORES ANTES de asignarlos al POD
        jefe_codelco_obj = None
        jefe_contratista_obj = None
        
        if jefe_codelco_nombre and self.Trabajador:
            jefe_codelco_obj = _get_or_create_trabajador(jefe_codelco_nombre, self.Trabajador)
            if not jefe_codelco_obj:
                stats["warnings"].append(f"No se pudo crear trabajador: {jefe_codelco_nombre}")
        
        if jefe_contratista_nombre and self.Trabajador:
            jefe_contratista_obj = _get_or_create_trabajador(jefe_contratista_nombre, self.Trabajador)
            if not jefe_contratista_obj:
                stats["warnings"].append(f"No se pudo crear trabajador: {jefe_contratista_nombre}")

        # Preparar datos para Pod
        key_kwargs = {"numero_pod": nro_val}
        defaults = {
            "fecha": fecha_val,
            "turno": turno_val,
            "inicio_periodo": inicio_periodo_val,
            "fin_periodo": fin_periodo_val,
            # ✅ Ahora SÍ son objetos Trabajador
            "jefe_turno_codelco": jefe_codelco_obj,
            "jefe_turno_contratista": jefe_contratista_obj,
        }

        # Remover valores None
        defaults = {k: v for k, v in defaults.items() if v is not None}

        try:
            if dry_run:
                stats["created"] = True
                stats["numero_pod"] = nro_val
                stats["pod_data"] = {"key": key_kwargs, "defaults": defaults}
                logger.info(f"🔶 DRY RUN - Pod: {nro_val} - {fecha_val}")
            else:
                # ⭐⭐ CREAR O ACTUALIZAR EL POD ⭐⭐
                pod_obj, created = self.Pod.objects.update_or_create(
                    defaults=defaults,
                    **key_kwargs,
                )
                stats["pod_id"] = pod_obj.id
                stats["pod_obj"] = str(pod_obj)
                stats["numero_pod"] = getattr(pod_obj, "numero_pod", nro_val)
                stats["created"] = bool(created)
                stats["updated"] = not created

                logger.info(f"✅ {'Creado' if created else 'Actualizado'} POD: {pod_obj}")

                if run_id is not None:
                    try:
                        with connection.cursor() as cur:
                            cur.execute(
                                "UPDATE etl_run SET pod_id=%s WHERE id=%s",
                                [pod_obj.id, run_id],
                            )
                            if cur.rowcount > 0:
                                stats["etl_run_updated"] = True
                    except Exception as e:
                        stats["warnings"].append(f"No se pudo actualizar etl_run.pod_id: {e}")

        except Exception as e:
            logger.exception("Error upsert Pod: %s", e)
            stats["warnings"].append(str(e))
            stats["skipped"] = 1

        return stats
