# pretdb/etl/transformers/matriz_cnc_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

from pretdb.etl.transformers.registry import register_transformer

logger = logging.getLogger(__name__)

# =============================== utilidades ===============================

def _norm(s: Any) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios."""
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\s\W]+", " ", s.strip().lower())
    return s

def _parse_date_cell(x: Any):
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("na", "nan", "<na>", "none"):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        dtt = pd.to_datetime(s, errors="coerce", dayfirst=False)
        return None if pd.isna(dtt) else dtt.date()
    dtt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if pd.isna(dtt) else dtt.date()

def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    s = str(val).strip()
    if "/" in s:
        s = s.split("/")[0]
    if s == "" or s in ("-", "—") or s.lower() in ("na", "nan", "<na>"):
        return None
    m = re.findall(r"[-+]?\d*\.?\d+", s.replace(",", "."))
    if m:
        try:
            return float(m[0])
        except Exception:
            return None
    return None

def _safe_str(df: pd.DataFrame, r: int, c: Optional[int]) -> Optional[str]:
    if c is None or c >= df.shape[1] or r >= len(df) or r < 0 or c < 0:
        return None
    v = df.iat[r, c]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if s == "" or s.upper() in ("<NA>", "NA", "NAN", "NONE"):
        return None
    return s

def _safe_float(df: pd.DataFrame, r: int, c: Optional[int]) -> Optional[float]:
    if c is None or c >= df.shape[1] or r >= len(df) or r < 0 or c < 0:
        return None
    return _to_float(df.iat[r, c])

def _empty_text(x: Optional[str]) -> bool:
    if x is None:
        return True
    s = str(x).strip()
    return s == "" or s.upper() in ("<NA>", "NA", "NAN")

def _looks_like_code_id(txt: Optional[str]) -> bool:
    """
    True si se parece a un código de causa: '1', '1.1', '1,1', '1-1', 'RC03', etc.
    """
    if _empty_text(txt):
        return False
    s = str(txt).strip()
    # patrones numéricos tipo 1.1 / 1,1 / 1-1 / 01 / 1.10
    if re.fullmatch(r"\d+([.,-]\d+)*", s):
        return True
    # patrón alfanumérico corto tipo RC03, ME01, etc.
    if re.fullmatch(r"[A-Za-z]{1,4}\d{1,4}", s):
        return True
    return False

def _norm_code_id(txt: Optional[str]) -> Optional[str]:
    """
    Normaliza códigos '1,1' / '1-1' -> '1.1'. Mantiene alfanuméricos.
    """
    if _empty_text(txt):
        return None
    s = str(txt).strip()
    # Si es algo tipo RC03, mantener
    if re.fullmatch(r"[A-Za-z]{1,4}\d{1,4}", s):
        return s.upper()
    # Reemplazar separadores por '.'
    s = s.replace(",", ".").replace("-", ".")
    # Compactar múltiples puntos
    s = re.sub(r"\.+", ".", s)
    return s

def _is_phantom_row(row_data: dict) -> bool:
    """
    Filas fantasma si NO hay secundaria (id y desc vacíos).
    Además, ignora filas donde la secundaria es el mismo texto que la primaria
    pero la secundaria desc está vacía (artefacto de merges).
    """
    no_sec_id = _empty_text(row_data.get("causa_secundaria_id"))
    no_sec_desc = _empty_text(row_data.get("causa_secundaria_desc"))
    if no_sec_id and no_sec_desc:
        return True
    if no_sec_desc and (row_data.get("causa_primaria_id") == row_data.get("causa_secundaria_id")):
        return True
    return False

def _row_key(row: dict) -> str:
    base = str(row.get("causa_secundaria_id") or _norm(row.get("causa_secundaria_desc") or ""))
    base += "|" + str(row.get("causa_primaria_id") or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


def _normalize_gestion_attr(val: Optional[str]) -> Optional[str]:
    """
    Normaliza valores de gestión atribuible a 'SI'/'NO'.
    """
    if _empty_text(val):
        return None
    s = _norm(val)
    if s in ("si", "sí", "s", "true", "1"):
        return "SI"
    if s in ("no", "n", "false", "0"):
        return "NO"
    return val.strip() if val else None

# ============================ patrones de header ============================

HEADER_HINTS: Dict[str, re.Pattern] = {
    "causa_primaria_desc": re.compile(r"\bcausa\s*primaria\b", re.I),
    "causa_secundaria_desc": re.compile(r"\bcausa\s*secundaria\b", re.I),
    # Ids: N°, NRO, N, ID, etc. (una sola celda con ese texto)
    "id_code": re.compile(r"^(nro|n|id|n[.°º]?)$", re.I),
    "gestion_atribuible": re.compile(r"gestion|atribuible", re.I),
}

# ============================= clase transformador =============================

@register_transformer("matriz_cnc")
class MatrizCNCTransformer:
    """
    Extrae la Matriz de Causas de No Cumplimiento (CNC).
    Soporta:
    1) Formato Plano (POD 288): | N° | Causa Primaria | N° | Causa Secundaria |
    2) Formato Agrupado (POD 131): | Causa Primaria (ID,Desc) ... Causa Secundaria (ID,Desc) |
    """
    sheet_key = "matriz_cnc"

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("MatrizCNCTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Matriz CNC'.")

        parsed_rows_list = self._parse_rows(df, header_row, colmap)

        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []

        for row in parsed_rows_list:
            row_id = _row_key(row)
            flat = {
                "row_id": row_id,
                "causa_primaria_id": row.get("causa_primaria_id"),
                "causa_primaria_desc": row.get("causa_primaria_desc"),
                "causa_secundaria_id": row.get("causa_secundaria_id"),
                "causa_secundaria_desc": row.get("causa_secundaria_desc"),
                "gestion_atribuible": row.get("gestion_atribuible"),
            }
            flat_rows.append(flat)

            unified = {
                "row_id": row_id,
                "meta": {
                    "causa_primaria_id": row.get("causa_primaria_id"),
                    "causa_primaria_desc": row.get("causa_primaria_desc"),
                    "gestion_atribuible": row.get("gestion_atribuible"),
                },
                "causa_secundaria": {
                    "causa_secundaria_id": row.get("causa_secundaria_id"),
                    "causa_secundaria_desc": row.get("causa_secundaria_desc"),
                }
            }
            unified_rows.append(unified)

        summary = {
            "header_row": header_row,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "counts": {"causas_secundarias_encontradas": len(unified_rows)},
            "samples": {"unified": unified_rows[:2], "flat": flat_rows[:2]},
        }

        out = {
            "sheet": self.sheet_key,
            "rows": len(unified_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "horarios_inicio": [],
            "unified": unified_rows,
            "by_tag": {"matriz_cnc": flat_rows},
            "blocks": {"matriz_cnc": unified_rows},
        }
        return out

    # ----------------------------- detección header -----------------------------

    def _find_header_and_columns(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
        """
        Detecta fila de header y mapea columnas clave (ID/Desc primaria y secundaria).
        Tolera merges y variaciones.
        """
        nrows = min(len(df), 30)
        ncols = min(df.shape[1], 20)

        header_row: Optional[int] = None
        best_score = -1
        best_map: Dict[str, Optional[int]] = {}

        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            row_norm = [_norm(s) for s in row_vals]

            # dónde están los textos "Causa Primaria"/"Causa Secundaria"
            c_prim_desc_cols = [c for c, s in enumerate(row_norm) if HEADER_HINTS["causa_primaria_desc"].search(s)]
            c_sec_desc_cols  = [c for c, s in enumerate(row_norm) if HEADER_HINTS["causa_secundaria_desc"].search(s)]
            id_cols          = [c for c, s in enumerate(row_norm) if HEADER_HINTS["id_code"].search(s)]

            if not c_prim_desc_cols or not c_sec_desc_cols:
                continue

            c_prim_desc = c_prim_desc_cols[0]
            c_sec_desc  = c_sec_desc_cols[0]

            colmap: Dict[str, Optional[int]] = {}
            score = 0

            # ---- Caso Formato Plano
            id_prim_cand = [c for c in id_cols if c < c_prim_desc]
            id_sec_cand  = [c for c in id_cols if c > c_prim_desc and c < c_sec_desc]
            if id_prim_cand and id_sec_cand:
                logger.debug("MatrizCNC: Formato 'Plano' detectado en fila %s", r)
                colmap["causa_primaria_id"]   = id_prim_cand[-1]
                colmap["causa_primaria_desc"] = c_prim_desc
                colmap["causa_secundaria_id"] = id_sec_cand[-1]
                colmap["causa_secundaria_desc"]= c_sec_desc
                score = 4

            # ---- Caso Formato Agrupado (IDs y Descs en columnas contiguas)
            if score < 4 and c_prim_desc < c_sec_desc:
                logger.debug("MatrizCNC: Formato 'Agrupado' tentativo en fila %s", r)
                # Por defecto: [Prim-Desc] está SOBRE la columna de descripción. ID suele estar a la derecha o izquierda inmediata.
                # Intento 1: ID justo a la derecha de cada descriptor
                prim_id_guess = c_prim_desc + 0  # el header puede estar sobre ID o DESC según plantilla
                sec_id_guess  = c_sec_desc + 0

                # Mejorar con heurística: buscar en vecindad una celda header vecina vacía y que la primera fila de datos luzca como ID
                # Mirar siguiente fila para validar patrón de ID
                next_r = r + 1 if r + 1 < len(df) else r
                # Buscar a la derecha inmediata
                if prim_id_guess + 1 < ncols and _looks_like_code_id(df.iat[next_r, prim_id_guess + 1]):
                    prim_id_guess = prim_id_guess + 1
                # Buscar a la derecha inmediata para secundaria
                if sec_id_guess + 1 < ncols and _looks_like_code_id(df.iat[next_r, sec_id_guess + 1]):
                    sec_id_guess = sec_id_guess + 1

                # Desc suponen ser la siguiente columna tras ID
                prim_desc_guess = prim_id_guess + 1 if prim_id_guess + 1 < ncols else prim_id_guess
                sec_desc_guess  = sec_id_guess + 1  if sec_id_guess + 1  < ncols else sec_id_guess

                colmap["causa_primaria_id"]   = prim_id_guess
                colmap["causa_primaria_desc"] = prim_desc_guess
                colmap["causa_secundaria_id"] = sec_id_guess
                colmap["causa_secundaria_desc"]= sec_desc_guess
                score = 4

            if score > best_score:
                best_score = score
                header_row = r
                # Capturar gestión atribuible si existe en la fila evaluada
                gest_cols = [c for c, s in enumerate(row_norm) if HEADER_HINTS["gestion_atribuible"].search(s)]
                if gest_cols:
                    colmap["gestion_atribuible"] = gest_cols[0]
                best_map = colmap

        if header_row is None:
            return None, {}

        logger.debug("MatrizCNC header at row %s | colmap=%s", header_row, best_map)
        return header_row, best_map

    # ----------------------------- parseo de filas -----------------------------

    def _parse_rows(
        self,
        df: pd.DataFrame,
        header_row: int,
        colmap: Dict[str, Optional[int]],
    ) -> List[Dict[str, Any]]:
        """
        Parsea la tabla de CNC con fill-down de Primaria.
        Normaliza IDs (1,1 -> 1.1) y filtra filas fantasma.
        """
        start_data = header_row + 1
        nrows = len(df)
        consecutive_blank = 0

        current_id_primaria: Optional[str] = None
        current_causa_primaria_desc: Optional[str] = None
        current_gestion_atribuible: Optional[str] = None

        parsed_rows: List[Dict[str, Any]] = []

        for r in range(start_data, nrows):
            id_prim_raw  = _safe_str(df, r, colmap.get("causa_primaria_id"))
            prim_desc    = _safe_str(df, r, colmap.get("causa_primaria_desc"))
            id_sec_raw   = _safe_str(df, r, colmap.get("causa_secundaria_id"))
            sec_desc     = _safe_str(df, r, colmap.get("causa_secundaria_desc"))
            gestion_attr_raw = _safe_str(df, r, colmap.get("gestion_atribuible"))

            # Fin al detectar 3 filas vacías seguidas
            if _empty_text(id_prim_raw) and _empty_text(prim_desc) and _empty_text(id_sec_raw) and _empty_text(sec_desc):
                consecutive_blank += 1
                if consecutive_blank >= 3:
                    logger.debug("MatrizCNC: stop en fila %s por 3 vacías", r)
                    break
                continue
            consecutive_blank = 0

            # Fill-down de primaria
            if not _empty_text(id_prim_raw):
                current_id_primaria = _norm_code_id(id_prim_raw) or id_prim_raw
            if not _empty_text(prim_desc):
                current_causa_primaria_desc = prim_desc
            if not _empty_text(gestion_attr_raw):
                current_gestion_atribuible = _normalize_gestion_attr(gestion_attr_raw)

            id_sec_norm = _norm_code_id(id_sec_raw) if id_sec_raw else None

            row_data = {
                "causa_primaria_id": current_id_primaria,
                "causa_primaria_desc": current_causa_primaria_desc,
                "causa_secundaria_id": id_sec_norm,
                "causa_secundaria_desc": sec_desc,
                "gestion_atribuible": current_gestion_atribuible,
            }

            if _is_phantom_row(row_data):
                continue

            parsed_rows.append(row_data)

        return parsed_rows


# ================================ Loader =================================
def save(self, transformed: dict, meta: dict) -> None:
    """Backward-compatible shim: delegate to pretdb.etl.loaders.MatrizCNCLoader.persist

    Keep the old `save(transformed, meta)` signature for callers that still
    use it; meta may contain 'pod' or 'pod_id'.
    """
    try:
        from pretdb.etl.loaders.matriz_cnc_loader import MatrizCNCLoader as _Loader
    except Exception as e:
        logger.warning("MatrizCNCLoader shim: no se pudo importar loader: %s", e)
        return

    pod = meta.get("pod")
    pod_id = meta.get("pod_id")
    if pod is None and pod_id is not None:
        try:
            from pretdb import models as mdl
            PodModel = getattr(mdl, "Pod", None)
            if PodModel is not None:
                pod = PodModel.objects.get(id=pod_id)
        except Exception:
            pod = None

    loader = _Loader()
    dry = meta.get("dry_run", False) or transformed.get("dry_run", False)
    try:
        loader.persist(pod, transformed, dry_run=dry)
    except Exception as e:
        logger.exception("MatrizCNCLoader shim: error delegating persist: %s", e)
