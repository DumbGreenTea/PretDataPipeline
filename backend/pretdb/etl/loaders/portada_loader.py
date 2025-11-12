from __future__ import annotations
from typing import Dict, Any, Optional
import logging
from datetime import date, datetime
from django.db import transaction, connection
from django.apps import apps

logger = logging.getLogger(__name__)

def _get_model(app_label: str, *names: str):
    for name in names:
        try:
            return apps.get_model(app_label, name)
        except LookupError:
            continue
    return None

def _has_field(model, name: str) -> bool:
    try:
        return any(getattr(f, "name", None) == name for f in model._meta.get_fields())
    except Exception:
        return False

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

def _assign_defaults(model, source: Dict[str, Any], mapping: Dict[str, list[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for dest, srcs in mapping.items():
        if not _has_field(model, dest):
            continue
        val = _pick(source, *srcs)
        if dest == "fecha":
            val = _to_date(val)
        if val is not None:
            out[dest] = val
    return out

class PortadaLoader:
    """Crea/actualiza Pod y (si existen modelos) enlaza Contrato y Contratista."""
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Contrato = _get_model(pretdb, "Contrato", "ContratoObra")
        self.Contratista = _get_model(pretdb, "Contratista", "Empresa", "Proveedor")

        self.pod_num_field = self.Pod and next((f for f in ["numero_pod","nro_pod","pod_numero","pod_nro","numero"]
                                                if _has_field(self.Pod, f)), None)
        self.pod_fecha_field = self.Pod and ("fecha" if _has_field(self.Pod, "fecha") else None)
        self.pod_contrato_fk = self.Pod and next((f for f in ["contrato","contrato_id"] if _has_field(self.Pod, f)), None)
        self.pod_contratista_fk = self.Pod and next((f for f in ["contratista","empresa"] if _has_field(self.Pod, f)), None)

    @transaction.atomic
    def persist(self, out: dict, *, dry_run: bool = False, run_id: Optional[int] = None) -> Dict[str, Any]:
        stats = {"mode": "dry" if dry_run else "write","pod_id": None,"created": False,"updated": False,
                 "linked_contrato": False,"linked_contratista": False,"etl_run_updated": False,"skipped": 0,"warnings": []}

        if not self.Pod:
            stats["warnings"].append("Modelo Pod no encontrado.")
            stats["skipped"] = 1
            return stats

        rows = out.get("flat_rows") or out.get("flat") or []
        if isinstance(rows, dict): rows = [rows]
        if not rows:
            stats["warnings"].append("PortadaLoader: flat_rows vacío.")
            stats["skipped"] = 1
            return stats

        src = rows[0]

        fecha_val = _to_date(_pick(src, "fecha", "fecha_pod", "dia"))
        nro_val   = _pick(src, "nro_pod", "numero_pod", "pod", "pod_numero", "n_pod")
        contrato_codigo   = _pick(src, "contrato", "codigo_contrato", "contrato_codigo", "id_contrato")
        contrato_nombre   = _pick(src, "contrato_nombre", "obra", "obra_nombre", "faena", "proyecto")
        contratista_nombre= _pick(src, "contratista", "empresa", "razon_social")

        key_kwargs = {}
        if self.pod_fecha_field and fecha_val: key_kwargs[self.pod_fecha_field] = fecha_val
        if self.pod_num_field and nro_val not in (None, ""): key_kwargs[self.pod_num_field] = nro_val
        if not key_kwargs:
            stats["warnings"].append("No hay clave natural (fecha/numero_pod) para upsert; se omite.")
            stats["skipped"] = 1
            return stats

        extras_map = {
            "proyecto": ["proyecto", "faena", "obra"],
            "turno": ["turno", "jornada"],
            "clima": ["clima", "meteorologia", "meteorología"],
            "superintendente": ["superintendente", "jefe_turno", "responsable"],
            "ubicacion": ["ubicacion", "ubicación", "locacion", "localizacion", "localización"],
        }
        defaults = _assign_defaults(self.Pod, src, {k:v for k,v in extras_map.items() if _has_field(self.Pod, k)})

        contrato_obj = None
        contratista_obj = None

        if self.Contratista and contratista_nombre:
            name_field = next((f for f in ["nombre","razon_social","descripcion"] if _has_field(self.Contratista, f)), None)
            if name_field:
                try:
                    contratista_obj, _ = self.Contratista.objects.get_or_create(**{name_field: contratista_nombre})
                    stats["linked_contratista"] = True
                except Exception as e:
                    stats["warnings"].append(f"No se pudo upsert Contratista: {e}")

        if self.Contrato and (contrato_codigo or contrato_nombre):
            code_field = next((f for f in ["codigo","nro","identificador"] if _has_field(self.Contrato, f)), None)
            name_field = next((f for f in ["nombre","descripcion","obra","proyecto"] if _has_field(self.Contrato, f)), None)
            fk_contratista = next((f for f in ["contratista","empresa","proveedor"] if _has_field(self.Contrato, f)), None)
            try:
                query = {}
                if code_field and contrato_codigo: query[code_field] = contrato_codigo
                elif name_field and contrato_nombre: query[name_field] = contrato_nombre
                if query:
                    defaults_contrato = {}
                    if code_field in query and name_field and contrato_nombre:
                        defaults_contrato[name_field] = contrato_nombre
                    if fk_contratista and contratista_obj:
                        defaults_contrato[fk_contratista] = contratista_obj
                    contrato_obj, _ = self.Contrato.objects.update_or_create(defaults=defaults_contrato, **query)
                    stats["linked_contrato"] = True
            except Exception as e:
                stats["warnings"].append(f"No se pudo upsert Contrato: {e}")

        if self.pod_contrato_fk and contrato_obj is not None:
            defaults[self.pod_contrato_fk] = contrato_obj
        if self.pod_contratista_fk and contratista_obj is not None:
            defaults[self.pod_contratista_fk] = contratista_obj

        try:
            if dry_run:
                stats["created"] = True
            else:
                pod_obj, created = self.Pod.objects.update_or_create(defaults=defaults, **key_kwargs)
                stats["pod_id"] = pod_obj.id
                stats["created"] = bool(created)
                stats["updated"] = not created
                if run_id is not None:
                    try:
                        with connection.cursor() as cur:
                            cur.execute("UPDATE etl_run SET pod_id=%s WHERE id=%s", [pod_obj.id, run_id])
                            if cur.rowcount > 0:
                                stats["etl_run_updated"] = True
                    except Exception as e:
                        stats["warnings"].append(f"No se pudo actualizar etl_run.pod_id: {e}")
        except Exception as e:
            logger.exception("Error upsert Pod: %s", e)
            stats["warnings"].append(str(e))
            stats["skipped"] += 1

        return stats