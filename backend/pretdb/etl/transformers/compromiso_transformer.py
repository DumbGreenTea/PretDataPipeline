from __future__ import annotations
from typing import Dict, Any, List, Optional
import logging, re, unicodedata, hashlib
from datetime import datetime, date

import pandas as pd
import numpy as np

from pretdb.etl.transformers.registry import register_transformer

logger = logging.getLogger(__name__)

# ------------------------------- utilidades -------------------------------

def _norm(s: Any) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios."""
    if s is None:
        return ""
    if isinstance(s, float) and np.isnan(s):
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def _parse_date_cell(x: Any) -> Optional[date]:
    """Convierte celdas a date (acepta ISO y día/mes primero)."""
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x
    s = (str(x) if x is not None else "").strip()
    if not s or s.lower() in ("na", "nan", "<na>", "none"):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        dt = pd.to_datetime(s, errors="coerce", dayfirst=False)
    else:
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if pd.isna(dt) else dt.date()

def _status_canonical(raw: Optional[str]) -> str:
    """
    Normaliza estado → {'abierto','cerrado','informativo','desconocido'}.
    'INF' o 'informativo' se tratan como informativo (en métricas lo contamos como abierto operativo).
    """
    t = _norm(raw)
    if not t:
        return "desconocido"
    if t.startswith("cerrad"):
        return "cerrado"
    if t.startswith("abiert"):
        return "abierto"
    if t.startswith("inf"):  # INF, informativo
        return "informativo"
    return "desconocido"

def _safe_str(row: pd.Series, idx: Optional[int]) -> Optional[str]:
    if idx is None:
        return None
    if idx >= len(row):
        return None
    v = row[idx]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s else None

def _row_id(categoria: Optional[str], item: int, descripcion: str) -> str:
    base = f"{categoria or ''}|{item}|{_norm(descripcion)}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ------------------------------ clase principal ------------------------------

@register_transformer("compromisos")
class CompromisosTransformer:
    """
    Extrae compromisos desde la hoja 'Compromisos' en sus variantes
    (una tabla o múltiples bloques por disciplina).
    Devuelve estructura estándar: flat_rows, unified, by_tag, blocks.
    """
    sheet_key = "compromisos"

    # -------------------------- API p/orquestador --------------------------

    def run_df(
        self,
        df_raw: pd.DataFrame,
        meta: dict,
        dry_run: bool = False,
        **kwargs,
    ) -> dict:
        logger.debug("CompromisosTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        compromisos = self._extract_compromisos(df)  # lista plana dicts
        flat_rows, unified_rows = self._to_flat_and_unified(compromisos)
        summary = self._build_summary(flat_rows)

        out = {
            "sheet": self.sheet_key,
            "rows": len(flat_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "unified": unified_rows,
            "horarios_inicio": [],
            "by_tag": {"compromisos": flat_rows},
            "blocks": {"compromisos": unified_rows},
        }
        return out

    # --------------------------- helpers internos ---------------------------

    def _find_header_rows(self, df: pd.DataFrame) -> List[int]:
        """
        Detecta filas de encabezado (contengan 'Ítem'/'Item' en alguna celda).
        Limita búsqueda a primeras ~80 filas.
        """
        headers: List[int] = []
        max_rows = min(len(df), 80)
        for r in range(max_rows):
            row = df.iloc[r]
            if any(isinstance(v, str) and "item" in _norm(v) for v in row):
                headers.append(r)
        logger.debug("Compromisos: header_rows=%s", headers)
        return headers

    def _build_colmap(self, df: pd.DataFrame, header_row: int) -> Dict[str, Optional[int]]:
        """
        Mapea nombre lógico -> índice de columna. Tolera variaciones.
        """
        row = df.iloc[header_row]
        ncols = df.shape[1]

        item_col = None
        desc_col = None
        fecha_toma_col = None
        fecha_cierre_proj_col = None
        fecha_cierre_efec_col = None
        responsable_col = None
        status_col = None
        obs_col = None

        for c, val in enumerate(row):
            if not isinstance(val, str):
                continue
            nv = _norm(val)
            if "item" in nv and item_col is None:
                item_col = c
            if ("descripcion" in nv or "descripción" in nv or "descripcion del compromiso" in nv) and desc_col is None:
                desc_col = c
            if "fecha toma" in nv and fecha_toma_col is None:
                fecha_toma_col = c
            if "cierre" in nv and "proyect" in nv and fecha_cierre_proj_col is None:
                fecha_cierre_proj_col = c
            if "cierre" in nv and "efect" in nv and fecha_cierre_efec_col is None:
                fecha_cierre_efec_col = c
            if "responsable" in nv and responsable_col is None:
                responsable_col = c
            if "status" in nv and status_col is None:
                status_col = c
            if "observacion" in nv and obs_col is None:
                obs_col = c

        # Fallback: descripción a la derecha de Ítem
        if desc_col is None and item_col is not None and item_col + 1 < ncols:
            desc_col = item_col + 1

        colmap = {
            "item": item_col,
            "descripcion": desc_col,
            "fecha_toma": fecha_toma_col,
            "fecha_cierre_proyectada": fecha_cierre_proj_col,
            "fecha_cierre_efectiva": fecha_cierre_efec_col,
            "responsable": responsable_col,
            "status": status_col,
            "observacion": obs_col,
        }
        logger.debug("Compromisos: colmap(header_row=%s)=%s", header_row, colmap)
        return colmap

    def _find_categoria_for_header(self, df: pd.DataFrame, header_row: int) -> Optional[str]:
        """
        Detecta categoría/disciplina del bloque mirando 1–3 filas hacia arriba
        (preferentemente columna 0, pero si está vacía, escanea toda la fila).
        """
        for r in range(header_row - 1, max(-1, header_row - 4), -1):
            if r < 0:
                break
            # preferir col 0
            candidates = [df.iat[r, 0]]
            # si col 0 vacío, mira el resto
            if not isinstance(candidates[0], str) or not candidates[0].strip():
                candidates = [v for v in df.iloc[r] if isinstance(v, str)]
            for v in candidates:
                nv = _norm(v)
                if not nv:
                    continue
                if nv.startswith("item") or "compromisos pod" in nv or "cc 006" in nv:
                    continue
                return v.strip()
        return None

    def _extract_compromisos(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Recorre todas las tablas detectadas y arma lista plana de compromisos.
        """
        compromisos: List[Dict[str, Any]] = []
        header_rows = self._find_header_rows(df)
        if not header_rows:
            logger.warning("Compromisos: no se encontraron filas de encabezado.")
            return compromisos

        nrows = len(df)

        for idx, h in enumerate(header_rows):
            colmap = self._build_colmap(df, h)
            categoria = self._find_categoria_for_header(df, h)

            item_col = colmap["item"]
            desc_col = colmap["descripcion"]
            ft_col = colmap["fecha_toma"]
            fcp_col = colmap["fecha_cierre_proyectada"]
            fce_col = colmap["fecha_cierre_efectiva"]
            resp_col = colmap["responsable"]
            stat_col = colmap["status"]
            obs_col = colmap["observacion"]

            if item_col is None or desc_col is None:
                logger.warning("Compromisos: header_row=%s sin columnas mínimas (item/descripcion).", h)
                continue

            end_row = header_rows[idx + 1] if idx + 1 < len(header_rows) else nrows

            for r in range(h + 1, end_row):
                row = df.iloc[r]

                # Saltar filas que parecen nuevos headers
                if any(isinstance(v, str) and "item" in _norm(v) for v in row):
                    continue

                item_val = row[item_col] if item_col < len(row) else None
                desc_val = row[desc_col] if desc_col < len(row) else None

                # Filas vacías
                if ((item_val is None or (isinstance(item_val, float) and pd.isna(item_val)))
                    and (desc_val is None or (isinstance(desc_val, float) and pd.isna(desc_val)))):
                    continue

                # Filas de título sin descripción (p.ej. un subtítulo intermedio)
                if isinstance(item_val, str) and not item_val.strip().isdigit() and (
                    desc_val is None or (isinstance(desc_val, float) and pd.isna(desc_val))
                ):
                    continue

                # Ítem debe ser entero o string de dígitos
                if isinstance(item_val, (int, float)) and not pd.isna(item_val):
                    item_num = int(item_val)
                elif isinstance(item_val, str) and item_val.strip().isdigit():
                    item_num = int(item_val.strip())
                else:
                    continue

                # Descripción obligatoria
                if not isinstance(desc_val, str) or not desc_val.strip():
                    continue

                def get(col_idx: Optional[int]):
                    return _safe_str(row, col_idx)

                fecha_toma = _parse_date_cell(get(ft_col))
                fecha_cierre_proj = _parse_date_cell(get(fcp_col))
                fecha_cierre_efec = _parse_date_cell(get(fce_col))
                responsable = get(resp_col)
                status_raw = get(stat_col)
                observacion = get(obs_col)

                estado = _status_canonical(status_raw)

                compromisos.append(
                    {
                        "categoria": categoria,
                        "item": item_num,
                        "descripcion": str(desc_val).strip(),
                        "fecha_toma": fecha_toma,
                        "fecha_cierre_proyectada": fecha_cierre_proj,
                        "fecha_cierre_efectiva": fecha_cierre_efec,
                        "responsable": responsable,
                        "status_raw": status_raw,
                        "estado": estado,            # normalizado
                        "observacion": observacion,
                    }
                )

        logger.debug("Compromisos: total_compromisos=%s", len(compromisos))
        return compromisos

    def _to_flat_and_unified(self, compromisos: List[Dict[str, Any]]):
        """Construye flat_rows/unified con row_id estable."""
        flat_rows: List[Dict[str, Any]] = []
        unified_rows: List[Dict[str, Any]] = []

        for c in compromisos:
            row_id = _row_id(c.get("categoria"), c["item"], c["descripcion"])

            flat = {
                "row_id": row_id,
                "categoria": c.get("categoria"),
                "item": c["item"],
                "descripcion": c["descripcion"],
                "fecha_toma": c.get("fecha_toma"),
                "fecha_cierre_proyectada": c.get("fecha_cierre_proyectada"),
                "fecha_cierre_efectiva": c.get("fecha_cierre_efectiva"),
                "responsable": c.get("responsable"),
                "status_raw": c.get("status_raw"),
                "estado": c.get("estado"),
                "observacion": c.get("observacion"),
            }
            flat_rows.append(flat)

            unified = {
                "row_id": row_id,
                "meta": {
                    "categoria": c.get("categoria"),
                    "item": c["item"],
                },
                "compromiso": {
                    "descripcion": c["descripcion"],
                    "fechas": {
                        "toma": c.get("fecha_toma"),
                        "cierre_proyectada": c.get("fecha_cierre_proyectada"),
                        "cierre_efectiva": c.get("fecha_cierre_efectiva"),
                    },
                    "responsable": c.get("responsable"),
                    "estado": {
                        "raw": c.get("status_raw"),
                        "canonical": c.get("estado"),
                    },
                    "observacion": c.get("observacion"),
                },
            }
            unified_rows.append(unified)

        return flat_rows, unified_rows

    def _build_summary(self, flat_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Métricas:
        - total
        - por estado (raw y canonical)
        - por categoría
        - por responsable/empresa (n, abiertos, cerrados)
        """
        from collections import Counter, defaultdict

        total = len(flat_rows)
        por_estado_raw: Counter[str] = Counter()
        por_estado_canon: Counter[str] = Counter()
        por_categoria: Counter[str] = Counter()
        por_empresa = defaultdict(lambda: {"numero_compromiso": 0, "abierto": 0, "cerrado": 0})

        for r in flat_rows:
            por_estado_raw[r.get("status_raw") or ""] += 1
            por_estado_canon[r.get("estado") or "desconocido"] += 1
            por_categoria[r.get("categoria") or ""] += 1

            empresa = (r.get("responsable") or "").strip()
            if empresa:
                por_empresa[empresa]["numero_compromiso"] += 1
                if r.get("estado") == "cerrado":
                    por_empresa[empresa]["cerrado"] += 1
                elif r.get("estado") in ("abierto", "informativo", "desconocido"):
                    por_empresa[empresa]["abierto"] += 1

        return {
            "n_compromisos": total,
            "por_estado_raw": dict(por_estado_raw),
            "por_estado": dict(por_estado_canon),
            "por_categoria": dict(por_categoria),
            "por_empresa": dict(por_empresa),
            "samples": {
                "flat": flat_rows[:2]
            }
        }

# ---------- Shim opcional: delegar persistencia al loader ----------

def persist_compromisos(out: dict, pod=None, dry_run: bool = False) -> dict:
    """
    Shim fino para mantener compatibilidad: delega en el loader dedicado.
    """
    try:
        from pretdb.etl.loaders import get_loader
        loader = get_loader("compromisos")
        if not loader:
            return {
                "mode": "dry" if dry_run else "write",
                "skipped": len(out.get("flat_rows") or []),
                "warning": "No loader registrado para 'compromisos'"
            }
        return loader.persist(pod, out, dry_run=dry_run)
    except Exception as e:
        logger.exception("Error delegando persistencia de compromisos: %s", e)
        return {"error": str(e), "mode": "dry" if dry_run else "write"}


def run_and_persist_compromisos(df_raw: pd.DataFrame, meta: dict, pod, dry_run: bool = False) -> Dict[str, Any]:
    """
    Conveniencia: transformar + persistir en una sola llamada (útil en management command).
    """
    tr = CompromisosTransformer()
    out = tr.run_df(df_raw, meta=meta, dry_run=dry_run)
    stats = persist_compromisos(out, pod=pod, dry_run=dry_run)
    return {
        "sheet": tr.sheet_key,
        "rows": out.get("rows"),
        "dry_run": dry_run,
        "summary": out.get("summary"),
        "persist_result": stats,
    }
