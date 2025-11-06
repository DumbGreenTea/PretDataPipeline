# backend/pretdb/etl/transformers/compromiso_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

logger = logging.getLogger(__name__)

# =============================== utilidades ===============================

def _norm(s: Any) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios/punct."""
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s.strip().lower())
    return s

def _is_na_like(x: Any) -> bool:
    """True si x es None/NaN/NA o string vacío/guiones."""
    if pd.isna(x):
        return True
    if isinstance(x, str):
        s = x.strip()
        if s == "" or s in {"-", "—"} or s.lower() in {"na", "nan", "<na>", "none"}:
            return True
    return False

def _parse_date_cell(x: Any) -> Optional[date]:
    """Convierte celdas a date; soporta ISO, d/m/Y y timestamps de Excel."""
    if _is_na_like(x):
        return None
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x).strip()
    # ISO primero (year-first)
    dtt = pd.to_datetime(s, errors="coerce", dayfirst=False)
    if not pd.isna(dtt):
        return dtt.date()
    # luego día/mes primero
    dtt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    if not pd.isna(dtt):
        return dtt.date()
    return None

def _safe_str(df: pd.DataFrame, r: int, c: Optional[int]) -> Optional[str]:
    """Getter seguro de string (limpia <NA>/nan y blanks)."""
    if c is None or c < 0 or c >= df.shape[1] or r < 0 or r >= len(df):
        return None
    v = df.iat[r, c]
    if _is_na_like(v):
        return None
    s = str(v).strip()
    return s if s != "" else None

def _safe_int(df: pd.DataFrame, r: int, c: Optional[int]) -> Optional[int]:
    """Extrae un entero tolerante a texto (toma el primer número que aparezca)."""
    if c is None or c < 0 or c >= df.shape[1] or r < 0 or r >= len(df):
        return None
    v = df.iat[r, c]
    if _is_na_like(v):
        return None
    m = re.search(r"[-+]?\d+", str(v))
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None

def _is_blank_row(row: pd.Series) -> bool:
    """Fila completamente vacía/NA-like."""
    for v in row.values:
        if not _is_na_like(v):
            return False
    return True

def _row_id(item: Optional[int], desc: Optional[str], responsable: Optional[str], categoria: Optional[str], file_name: Optional[str]) -> str:
    base = "|".join([
        str(item) if item is not None else "",
        _norm(desc or ""),
        _norm(responsable or ""),
        _norm(categoria or ""),
        _norm(file_name or ""),
    ])
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================ patrones de header ============================

HEADER_PATS: Dict[str, re.Pattern] = {
    "item": re.compile(r"\b(i|í)tem\b", re.I),
    "descripcion": re.compile(r"\bdescrip(cion|ción)\b", re.I),
    "fecha_toma": re.compile(r"fecha\s*toma(\s*de)?\s*compromiso", re.I),
    "fecha_cierre_proyectada": re.compile(r"fecha\s*de?\s*cierre\s*proyectada", re.I),
    "fecha_cierre_efectiva": re.compile(r"fecha\s*(de\s*)?cierre\s*efectiva", re.I),
    "responsable": re.compile(r"\bresponsable\b", re.I),
    "status": re.compile(r"\bstatus\b|\bestatus\b", re.I),
    "observacion": re.compile(r"\bobservaci(o|ó)n\b", re.I),
}
REQUIRED_FOR_HEADER = {"item", "descripcion", "status"}  # mínimos para validar encabezado

# ============================= clase transformador =============================

class CompromisosTransformer:
    """
    Extrae la tabla de 'Compromisos' (ignorando el panel 'ESTATUS DE COMPROMISOS'
    de la derecha). Soporta variaciones con títulos de sección y re-encabezados.
    """
    sheet_key = "compromisos"

    # ---------------------- API para el orquestador ----------------------
    def run(self, reader, dry_run: bool = False, **kwargs) -> dict:
        """
        El orquestador llama run(reader). Aquí resolvemos df y meta desde el reader,
        y delegamos a run_df(...) como en tus otros transformers.
        """
        df, meta = self._get_df_and_meta(reader)
        return self.run_df(df, meta, dry_run=dry_run, **kwargs)

    # ---------------------- API estilo otros transformers ----------------------
    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("CompromisosTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar el encabezado en 'Compromisos'.")

        parsed_rows: List[Dict[str, Any]] = self._parse_rows(df, header_row, colmap, meta)

        # ---- Salidas: unified & flat ----
        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []

        for r in parsed_rows:
            rid = _row_id(r.get("item"), r.get("descripcion"), r.get("responsable"), r.get("categoria"), meta.get("file_name"))
            unified = {
                "row_id": rid,
                "meta": {"categoria": r.get("categoria"), "nro_pod": meta.get("nro_pod")},
                "compromiso": {
                    "item": r.get("item"),
                    "descripcion": r.get("descripcion"),
                    "responsable": r.get("responsable"),
                    "status": r.get("status"),
                    "observacion": r.get("observacion"),
                },
                "fechas": {
                    "toma": r.get("fecha_toma").isoformat() if isinstance(r.get("fecha_toma"), date) else None,
                    "cierre_proyectada": r.get("fecha_cierre_proyectada").isoformat() if isinstance(r.get("fecha_cierre_proyectada"), date) else None,
                    "cierre_efectiva": r.get("fecha_cierre_efectiva").isoformat() if isinstance(r.get("fecha_cierre_efectiva"), date) else None,
                },
            }
            unified_rows.append(unified)

            flat = {
                "row_id": rid,
                "item": r.get("item"),
                "descripcion": r.get("descripcion"),
                "fecha_toma": r.get("fecha_toma"),
                "fecha_cierre_proyectada": r.get("fecha_cierre_proyectada"),
                "fecha_cierre_efectiva": r.get("fecha_cierre_efectiva"),
                "responsable": r.get("responsable"),
                "status": r.get("status"),
                "observacion": r.get("observacion"),
                "categoria": r.get("categoria"),
                "nro_pod": meta.get("nro_pod"),
            }
            flat_rows.append(flat)

        # ---- Resúmenes ----
        status_counts = pd.Series([r.get("status") for r in flat_rows], dtype="object").value_counts(dropna=True).to_dict()
        cat_counts = pd.Series([r.get("categoria") or "(sin categoria)" for r in flat_rows], dtype="object").value_counts().to_dict()

        summary = {
            "header_row": header_row,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "counts": {
                "total_filas": len(flat_rows),
                "status": status_counts,
                "by_categoria": cat_counts,
            },
            "samples": {
                "unified": unified_rows[:2],
                "flat": flat_rows[:2],
            },
            "input_meta": meta,
        }

        out = {
            "sheet": self.sheet_key,
            "rows": len(flat_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "unified": unified_rows,
            "by_tag": {
                "compromisos": unified_rows,
                "compromisos_abiertos": [u for u in unified_rows if (u["compromiso"].get("status") == "Abierto")],
                "compromisos_cerrados": [u for u in unified_rows if (u["compromiso"].get("status") == "Cerrado")],
            },
            "blocks": {
                "compromisos": flat_rows,
                "by_status": {
                    "Abierto": [f for f in flat_rows if f.get("status") == "Abierto"],
                    "Cerrado": [f for f in flat_rows if f.get("status") == "Cerrado"],
                },
                "by_categoria": {
                    k: [f for f in flat_rows if (f.get("categoria") or "(sin categoria)") == k]
                    for k in sorted(set((f.get("categoria") or "(sin categoria)") for f in flat_rows))
                },
            },
        }
        return out

    # ----------------------------- detección header -----------------------------
    def _find_header_and_columns(self, df: pd.DataFrame) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
        """
        Busca la fila de encabezado y mapea columnas clave.
        Requiere al menos: item, descripcion, status.
        """
        nrows = min(len(df), 35)
        ncols = min(df.shape[1], 32)

        best_row: Optional[int] = None
        best_map: Dict[str, Optional[int]] = {}
        best_score = -1

        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            colmap: Dict[str, Optional[int]] = {k: None for k in HEADER_PATS.keys()}

            for c, raw in enumerate(row_vals):
                s = raw
                s_norm = _norm(raw)
                for k, pat in HEADER_PATS.items():
                    if colmap[k] is None and (pat.search(s) or pat.search(s_norm)):
                        colmap[k] = c

            # exigir mínimos
            if not all(colmap.get(k) is not None for k in REQUIRED_FOR_HEADER):
                continue

            score = sum(1 for v in colmap.values() if v is not None)
            if score > best_score:
                best_score = score
                best_row = r
                best_map = colmap

        if best_row is None:
            return None, {}
        logger.debug("Compromisos header at row %s | colmap=%s", best_row, best_map)
        return best_row, best_map

    # ----------------------------- parseo de filas -----------------------------
    def _parse_rows(self, df: pd.DataFrame, header_row: int, colmap: Dict[str, Optional[int]], meta: dict) -> List[Dict[str, Any]]:
        """
        Itera filas posteriores al header. Soporta:
         - Títulos de sección (solo texto en 'Item' y vacío en el resto).
         - Re-encabezados internos (otra fila que repite 'Ítem', 'Descripción', etc.).
         - Bloque lateral 'ESTATUS DE COMPROMISOS' → se ignora.
        """
        start = header_row + 1
        nrows = len(df)
        consecutive_blank = 0

        rows: List[Dict[str, Any]] = []
        categoria_actual: Optional[str] = None

        def is_reheader(row: pd.Series) -> bool:
            txts = [_norm(v) for v in row.values if not _is_na_like(v)]
            return any(t == "item" for t in txts) and any("descripcion" in t for t in txts)

        for r in range(start, nrows):
            row = df.iloc[r, :]

            if _is_blank_row(row):
                consecutive_blank += 1
                if consecutive_blank >= 3:
                    break
                continue
            consecutive_blank = 0

            # ignorar el panel de estatus si aparece en esta fila
            joined_norm = _norm(" ".join(str(v) for v in row.values if not _is_na_like(v)))
            if "estatus de compromisos" in joined_norm:
                continue

            # re-encabezado
            if is_reheader(row):
                logger.debug("Compromisos: re-encabezado detectado en fila %s", r)
                continue

            # ¿título de sección? → hay texto en Item, resto vacío (desc/status/resp)
            c_item = colmap.get("item")
            c_desc = colmap.get("descripcion")
            c_status = colmap.get("status")

            maybe_title = _safe_str(df, r, c_item)
            has_desc = _safe_str(df, r, c_desc)
            has_status = _safe_str(df, r, c_status)
            has_resp = _safe_str(df, r, colmap.get("responsable"))

            if maybe_title and not (has_desc or has_status or has_resp):
                categoria_actual = maybe_title.strip()
                continue

            # fila de dato real
            item = _safe_int(df, r, c_item)
            desc = _safe_str(df, r, c_desc)
            status_raw = _safe_str(df, r, c_status)
            status = None
            if status_raw:
                t = _norm(status_raw)
                if "abiert" in t:
                    status = "Abierto"
                elif "cerrad" in t:
                    status = "Cerrado"
                else:
                    status = status_raw.strip()

            # si no hay desc ni status, saltar
            if (desc is None or desc == "") and status is None:
                continue

            fecha_toma = _parse_date_cell(_safe_str(df, r, colmap.get("fecha_toma")))
            fecha_cierre_p = _parse_date_cell(_safe_str(df, r, colmap.get("fecha_cierre_proyectada")))
            fecha_cierre_e = _parse_date_cell(_safe_str(df, r, colmap.get("fecha_cierre_efectiva")))
            responsable = _safe_str(df, r, colmap.get("responsable"))
            observacion = _safe_str(df, r, colmap.get("observacion"))

            rows.append({
                "excel_row_index": r,
                "item": item,
                "descripcion": desc,
                "fecha_toma": fecha_toma,
                "fecha_cierre_proyectada": fecha_cierre_p,
                "fecha_cierre_efectiva": fecha_cierre_e,
                "responsable": responsable,
                "status": status,
                "observacion": observacion,
                "categoria": categoria_actual,
            })

        return rows

    # ------------------------------- IO del reader -------------------------------
    def _get_df_and_meta(self, reader) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Obtiene el DataFrame de 'compromisos' desde tu ExcelReader, siguiendo el
        mismo flujo que ya está funcionando para otras hojas.
        """
        df = None
        raw_name = None

        # 1) cache completo por clave normalizada
        if hasattr(reader, "read_all_sheets_keyed"):
            try:
                keyed = reader.read_all_sheets_keyed()
                if isinstance(keyed, dict) and self.sheet_key in keyed:
                    df = keyed[self.sheet_key]
                    if hasattr(reader, "normalized_sheet_map"):
                        try:
                            mapping = reader.normalized_sheet_map()
                            raw_name = mapping.get(self.sheet_key)
                        except Exception:
                            raw_name = None
            except Exception:
                pass

        # 2) métodos directos por clave normalizada
        if df is None:
            for meth in ("get_detected_df", "get_df_by_key", "get_df"):
                if hasattr(reader, meth):
                    try:
                        got = getattr(reader, meth)(self.sheet_key)
                        if isinstance(got, tuple) and isinstance(got[0], pd.DataFrame):
                            df = got[0]
                            break
                        if isinstance(got, pd.DataFrame):
                            df = got
                            break
                    except Exception:
                        continue

        # 3) mapping normalizado → crudo y leer por nombre de hoja
        if df is None and hasattr(reader, "normalized_sheet_map") and hasattr(reader, "get_sheet_df"):
            try:
                mapping = reader.normalized_sheet_map()
                raw = mapping.get(self.sheet_key)
                if raw:
                    try:
                        df = reader.get_sheet_df(raw, header=None)
                        raw_name = raw
                    except TypeError:
                        df = reader.get_sheet_df(raw)
                        raw_name = raw
            except Exception:
                pass

        if df is None or not isinstance(df, pd.DataFrame):
            raise ValueError("No se pudo leer la hoja 'compromisos' desde el reader.")

        meta: Dict[str, Any] = {
            "raw_sheet_name": raw_name,
            "file_name": getattr(reader, "file_name", None) or getattr(reader, "filename", None) or self._meta_get(reader, "file_name"),
            "file_hash": getattr(reader, "file_hash", None) or self._meta_get(reader, "file_hash"),
            "source_path": getattr(reader, "source_path", None) or getattr(reader, "path", None) or self._meta_get(reader, "source_path"),
            "nro_pod": self._meta_get(reader, "nro_pod"),
            "fecha": self._meta_get(reader, "fecha"),
        }
        return df, meta

    @staticmethod
    def _meta_get(reader, key: str) -> Any:
        for attr in ("meta", "metadata", "_meta"):
            if hasattr(reader, attr):
                obj = getattr(reader, attr)
                if isinstance(obj, dict) and key in obj:
                    return obj.get(key)
        return None