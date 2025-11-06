# pretdb/etl/transformers/matriz_cnc_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

logger = logging.getLogger(__name__)

# =============================== utilidades ===============================
# (Sin cambios)

def _norm(s: Any) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios."""
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\s\W]+", " ", s.strip().lower())
    return s

def _parse_date_cell(x: Any) -> Optional[date]:
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
    """Safe getter para string, filtrando <NA> y nan."""
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
    """True si el string es None, vacío, o un placeholder de NA."""
    if x is None:
        return True
    s = str(x).strip()
    return s == "" or s.upper() in ("<NA>", "NA", "NAN")

def _is_phantom_row(row_data: dict) -> bool:
    """Detecta filas 'fantasma' (solo si la causa secundaria está vacía)."""
    # Usar los nombres de BBDD
    no_sec_id = _empty_text(row_data.get("causa_secundaria_id"))
    no_sec_desc = _empty_text(row_data.get("causa_secundaria_desc"))
    
    if no_sec_id and no_sec_desc:
        return True
        
    # Caso POD 288 (Formato Plano): la fila de "Causa Primaria" tiene el ID/Desc en ambas secciones
    if no_sec_desc and row_data.get("causa_primaria_id") == row_data.get("causa_secundaria_id"):
         return True
    
    return False

def _row_key(row: dict) -> str:
    """ID estable por fila (basado en id_secundaria)."""
    # Usar los nombres de BBDD
    base = str(row.get("causa_secundaria_id") or _norm(row.get("causa_secundaria_desc") or ""))
    base += "|" + str(row.get("causa_primaria_id") or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================ patrones de header ============================

HEADER_HINTS: Dict[str, re.Pattern] = {
    # Nombres de las columnas de descripción
    "causa_primaria_desc": re.compile(r"causa\s*primaria", re.I),
    "causa_secundaria_desc": re.compile(r"causa\s*secundaria", re.I),
    
    # Nombres de las columnas de ID (N°, 1, 1.1, etc.)
    # Exigir que sea SOLO "nro", "n", "id", etc. para evitar matches con "1,1"
    "id_code": re.compile(r"^(nro|n|id|n[.°º]?)$", re.I), 
}

# ============================= clase transformador =============================

class MatrizCNCTransformer:
    """
    Extrae la Matriz de Causas de No Cumplimiento (CNC).
    Esta hoja es una tabla dimensional (lookup table).
    
    Maneja dos formatos:
    1. Formato "Plano" (POD 288): | N° | CAUSA PRIMARIA | N° | CAUSA SECUNDARIA |
    2. Formato "Agrupado" (POD 131): | Causa Primaria (ID) | (Desc) | Causa Secundaria (ID) | (Desc) |
    """
    sheet_key = "matriz_cnc"

    # ---------------------- API para el orquestador ----------------------
    
    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("MatrizCNCTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Matriz CNC'.")

        parsed_rows_list = self._parse_rows(df, header_row, colmap)

        # ------------------------ UNIFICACIÓN Y FORMATEO ------------------------
        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []

        for row in parsed_rows_list:
            row_id = _row_key(row)
            
            # --- CAMBIO: Usar los nombres de la BBDD ---
            flat = {
                "row_id": row_id,
                "causa_primaria_id": row.get("causa_primaria_id"),
                "causa_primaria_desc": row.get("causa_primaria_desc"),
                "causa_secundaria_id": row.get("causa_secundaria_id"),
                "causa_secundaria_desc": row.get("causa_secundaria_desc"),
            }
            flat_rows.append(flat)

            unified = {
                "row_id": row_id,
                "meta": {
                    "causa_primaria_id": row.get("causa_primaria_id"),
                    "causa_primaria_desc": row.get("causa_primaria_desc"),
                },
                "causa_secundaria": {
                    "causa_secundaria_id": row.get("causa_secundaria_id"),
                    "causa_secundaria_desc": row.get("causa_secundaria_desc"),
                }
            }
            unified_rows.append(unified)
            
        # --- Fin del bucle ---

        summary = {
            "header_row": header_row,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "counts": {
                "causas_secundarias_encontradas": len(unified_rows),
            },
            "samples": {
                "unified": unified_rows[:2],
                "flat": flat_rows[:2],
            }
        }

        # Salida estándar
        out = {
            "sheet": self.sheet_key,
            "rows": len(unified_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "horarios_inicio": [], # Vacío
            "unified": unified_rows,
            "by_tag": {"matriz_cnc": flat_rows},
            "blocks": {"matriz_cnc": unified_rows},
        }
        return out

    # ----------------------------- detección header -----------------------------

    # --- CAMBIO: Lógica de detección de header re-escrita ---
    def _find_header_and_columns(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
        """
        Busca la fila de encabezado y mapea las 4 columnas clave,
        manejando los dos formatos (plano y agrupado/merged).
        """
        nrows = min(len(df), 20)
        ncols = min(df.shape[1], 15)

        header_row: Optional[int] = None
        best_score = -1
        best_map: Dict[str, Optional[int]] = {}

        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            row_norm = [_norm(s) for s in row_vals]
            
            colmap: Dict[str, Optional[int]] = {}
            score = 0

            # 1. Encontrar todas las columnas candidatas
            c_prim_desc_cols = [c for c, s in enumerate(row_norm) if HEADER_HINTS["causa_primaria_desc"].search(s)]
            c_sec_desc_cols = [c for c, s in enumerate(row_norm) if HEADER_HINTS["causa_secundaria_desc"].search(s)]
            id_cols = [c for c, s in enumerate(row_norm) if HEADER_HINTS["id_code"].search(s)]

            if not c_prim_desc_cols or not c_sec_desc_cols:
                continue # No es un header válido si faltan las descripciones

            c_prim_desc = c_prim_desc_cols[0]
            c_sec_desc = c_sec_desc_cols[0]

            # 2. Intentar lógica de Formato 1 (Plano, tipo POD 288)
            # Busca: | N° (id_prim) | Causa Primaria (c_prim_desc) | N° (id_sec) | Causa Secundaria (c_sec_desc) |
            id_prim_cand = [c for c in id_cols if c < c_prim_desc]
            id_sec_cand = [c for c in id_cols if c > c_prim_desc and c < c_sec_desc]
            
            if id_prim_cand and id_sec_cand:
                logger.debug("Detectado formato Matriz CNC 'Plano' (tipo POD 288) en fila %s", r)
                colmap["causa_primaria_id"] = id_prim_cand[-1]   # Tomar el N° más cercano (ej. col 1)
                colmap["causa_primaria_desc"] = c_prim_desc      # ej. col 2
                colmap["causa_secundaria_id"] = id_sec_cand[-1]  # Tomar el N° más cercano (ej. col 3)
                colmap["causa_secundaria_desc"] = c_sec_desc      # ej. col 4
                score = 4
            
            # 3. Intentar lógica de Formato 2 (Agrupado, tipo POD 131)
            # Busca: | Causa Primaria (id_prim) | (causa_prim_desc) | ... | Causa Secundaria (id_sec) | (causa_sec_desc) |
            elif c_prim_desc < c_sec_desc:
                logger.debug("Detectado formato Matriz CNC 'Agrupado' (tipo POD 131) en fila %s", r)
                colmap["causa_primaria_id"] = c_prim_desc         # ej. col 3 (Contiene "1")
                colmap["causa_primaria_desc"] = c_prim_desc + 1   # ej. col 4 (Contiene "Equipos")
                colmap["causa_secundaria_id"] = c_sec_desc + 1    # <-- FIX: (ej. col 5, Contiene "1,1")
                colmap["causa_secundaria_desc"] = c_sec_desc + 2  # <-- FIX: (ej. col 6, Contiene "Fuera de...")
                score = 4
            
            # 4. Asignar el mejor
            if score > best_score:
                best_score = score
                header_row = r
                best_map = colmap

        if header_row is None:
            return None, {}

        logger.debug("MatrizCNC header at row %s | colmap=%s", header_row, best_map)
        return header_row, best_map
    # --- FIN CAMBIO ---

    # ----------------------------- parseo de filas -----------------------------
    
    def _parse_rows(
        self,
        df: pd.DataFrame,
        header_row: int,
        colmap: Dict[str, Optional[int]],
    ) -> List[Dict[str, Any]]:
        """
        Parsea la tabla de CNC, usando lógica "fill-down" para
        manejar el formato agrupado (POD 131).
        """

        start_data = header_row + 1
        nrows = len(df)
        consecutive_blank = 0
        
        # --- Lógica "Fill-Down" ---
        current_id_primaria: Optional[str] = None
        current_causa_primaria_desc: Optional[str] = None
        
        parsed_rows: List[Dict[str, Any]] = []

        for r in range(start_data, nrows):
            
            # --- CAMBIO: Leer con los nuevos nombres de llaves ---
            id_prim = _safe_str(df, r, colmap.get("causa_primaria_id"))
            causa_prim_desc = _safe_str(df, r, colmap.get("causa_primaria_desc"))
            id_sec = _safe_str(df, r, colmap.get("causa_secundaria_id"))
            causa_sec_desc = _safe_str(df, r, colmap.get("causa_secundaria_desc"))
            
            # --- Lógica de parada ---
            if _empty_text(id_prim) and _empty_text(causa_prim_desc) and _empty_text(id_sec) and _empty_text(causa_sec_desc):
                consecutive_blank += 1
                if consecutive_blank >= 3:
                    logger.debug("Deteniendo parseo en fila %s por 3 líneas vacías", r)
                    break
                continue
            consecutive_blank = 0
            
            # --- Lógica "Fill-Down" ---
            # Si esta fila SÍ tiene datos primarios, los actualizamos
            if not _empty_text(id_prim):
                current_id_primaria = id_prim
            if not _empty_text(causa_prim_desc):
                current_causa_primaria_desc = causa_prim_desc
            
            row_data = {
                "causa_primaria_id": current_id_primaria,
                "causa_primaria_desc": current_causa_primaria_desc,
                "causa_secundaria_id": id_sec,
                "causa_secundaria_desc": causa_sec_desc,
            }
            # --- FIN CAMBIO ---

            if _is_phantom_row(row_data):
                continue
            
            parsed_rows.append(row_data)
        
        return parsed_rows