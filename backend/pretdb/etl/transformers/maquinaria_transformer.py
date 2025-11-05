# pretdb/etl/transformers/maquinaria_transformer.py
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
    """Convierte celdas a date (acepta distintos formatos, día/mes primero)."""
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("na", "nan", "<na>", "none"):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):  # ISO primero
        dtt = pd.to_datetime(s, errors="coerce", dayfirst=False)
        return None if pd.isna(dtt) else dtt.date()
    dtt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if pd.isna(dtt) else dtt.date()

def _to_float(val: Any) -> Optional[float]:
    """Convierte a float números o strings con números; '-' y vacíos -> None."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    s = str(val).strip()
    # Manejar formatos especiales como '3/3R' o '6/4' -> tomar el primer número
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
    """Safe getter para float."""
    if c is None or c >= df.shape[1] or r >= len(df) or r < 0 or c < 0:
        return None
    return _to_float(df.iat[r, c])

def _empty_text(x: Optional[str]) -> bool:
    """True si el string es None, vacío, o un placeholder de NA."""
    if x is None:
        return True
    s = str(x).strip()
    return s == "" or s.upper() in ("<NA>", "NA", "NAN")

# --- CAMBIO: Lógica de filtrado mejorada ---
def _is_phantom_row(row_data: dict) -> bool:
    """Detecta filas 'fantasma' (totales, notas, o vacías)."""
    cargo = _norm(row_data.get("cargo"))
    equipo = _norm(row_data.get("equipo"))

    # 1. Filtrar por palabras clave de resumen/notas
    if "total" in cargo or "total" in equipo:
        return True
    if "nota" in equipo or "dotacion al" in equipo:
        return True
    if equipo in ("mod", "moi"):
        return True

    # 2. Filtrar si está completamente vacía
    no_text = _empty_text(row_data.get("cargo")) and _empty_text(row_data.get("equipo"))
    return no_text
# --- FIN CAMBIO ---

def _row_key(row: dict) -> str:
    """ID estable por fila/tarea, adaptado a Dotación y Maquinaria."""
    base = "|".join([
        _norm(row.get("cargo") or ""),
        _norm(row.get("equipo") or ""),
    ])
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================ patrones de header ============================
# (Sin cambios)

HEADER_HINTS: Dict[str, re.Pattern] = {
    # Tabla Izquierda (Personal)
    "cargo": re.compile(r"\bcargo\b", re.I),
    "turno_a": re.compile(r"turno\s*a", re.I),
    "turno_b": re.compile(r"turno\s*b", re.I),
    
    # Tabla Derecha (Maquinaria)
    "equipo": re.compile(r"\bequipo\b", re.I),
    "peak": re.compile(r"\bpeak\b", re.I),
    "proyectado": re.compile(r"proyectado", re.I),
    "total_en_obra": re.compile(r"total\s*de\s*equipo\s*en\s*obra", re.I),
    "equipos_en_falla": re.compile(r"equipos\s*en\s*falla", re.I),
    "equipo_en_mantencion": re.compile(r"equipo\s*en\s*mantenci[oó]n", re.I),
    "operadores_disponibles": re.compile(r"operadores\s*disponibles", re.I),
    "equipos_operativos": re.compile(r"equipos\s*operativos", re.I),
    "reserva": re.compile(r"\breserva\b", re.I),
    "observacion": re.compile(r"\bobservaci[oó]n\b", re.I),
    "descripcion_falla": re.compile(r"descripci[oó]n\s*de\s*falla", re.I),
    "compromiso_equipo": re.compile(r"compromiso\s*equipo", re.I),
    "fecha_compromiso_equipo": re.compile(r"fecha", re.I), # El 1ro
    "compromiso_operador": re.compile(r"compromiso\s*operador", re.I),
    "fecha_compromiso_operador": re.compile(r"fecha", re.I), # El 2do
}

# ============================= clase transformador =============================

class DotacionMaquinariaTransformer:
    """
    Extrae las dos tablas paralelas de 'Dotación y Maquinaria':
      1. Estatus Licencias (Personal)
      2. Estatus de Maquinaria (Equipos)
    
    Las unifica en una sola `flat_rows` por fila de Excel.
    """
    sheet_key = "dotacion_y_maquinaria" # <-- Asegúrate que esta llave coincida con tu registry.py

    # ---------------------- API para el orquestador ----------------------
    # (run_df no cambia)
    
    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("DotacionMaquinariaTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Dotación y Maquinaria'.")

        # 1. Parsear las filas (devuelve una lista simple)
        parsed_rows_list = self._parse_rows(df, header_row, colmap)

        # ------------------------ UNIFICACIÓN Y FORMATEO ------------------------
        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []

        for row in parsed_rows_list:
            row_id = _row_key(row)
            
            dotacion_tag = {
                "cargo": row.get("cargo"),
                "turno_a": row.get("turno_a"),
                "turno_b": row.get("turno_b"),
            }

            maquinaria_tag = {
                "equipo": row.get("equipo"),
                "peak": row.get("peak"),
                "proyectado": row.get("proyectado"),
                "total_en_obra": row.get("total_en_obra"),
                "equipos_en_falla": row.get("equipos_en_falla"),
                "equipo_en_mantencion": row.get("equipo_en_mantencion"),
                "operadores_disponibles": row.get("operadores_disponibles"),
                "equipos_operativos": row.get("equipos_operativos"),
                "reserva": row.get("reserva"),
                "observacion": row.get("observacion"),
                "descripcion_falla": row.get("descripcion_falla"),
                "compromiso_equipo": row.get("compromiso_equipo"),
                "fecha_compromiso_equipo": row.get("fecha_compromiso_equipo"),
                "compromiso_operador": row.get("compromiso_operador"),
                "fecha_compromiso_operador": row.get("fecha_compromiso_operador"),
            }
            
            unified = {
                "row_id": row_id,
                "dotacion_personal": dotacion_tag,
                "maquinaria_equipos": maquinaria_tag,
            }
            unified_rows.append(unified)

            flat = {
                "row_id": row_id,
                **dotacion_tag,
                **maquinaria_tag,
            }
            flat_rows.append(flat)
            
        # --- Fin del bucle ---

        summary = {
            "header_row": header_row,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "counts": {
                "total_filas": len(unified_rows),
            },
            "samples": {
                "unified": unified_rows[:2],
                "flat": flat_rows[:2],
            }
        }

        # Salida final reordenada
        out = {
            "sheet": self.sheet_key,
            "rows": len(unified_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "unified": unified_rows,
            "horarios_inicio": [],
            "by_tag": {"dotacion_maquinaria": flat_rows},
            "blocks": {"dotacion_maquinaria": flat_rows},
        }
        return out

    # ----------------------------- detección header -----------------------------

    # --- CAMBIO: Lógica de detección de fecha mejorada ---
    def _find_header_and_columns(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
        """
        Busca la fila de encabezado y mapea columnas clave.
        Adaptado para 'Dotación y Maquinaria'.
        """
        nrows = min(len(df), 20)
        ncols = min(df.shape[1], 30)

        header_row: Optional[int] = None
        best_score = -1
        best_map: Dict[str, Optional[int]] = {}

        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            colmap: Dict[str, Optional[int]] = {k: None for k in HEADER_HINTS.keys()}
            
            # 1. Mapeo general (excepto fechas)
            for c, raw in enumerate(row_vals):
                s_norm = _norm(raw)
                for k, pat in HEADER_HINTS.items():
                    if k.startswith("fecha_"): # Saltar fechas por ahora
                        continue
                    if colmap.get(k) is None and (pat.search(raw) or pat.search(s_norm)):
                        colmap[k] = c

            # 2. Asignar 'fecha_compromiso_equipo'
            # Buscar "Fecha" DESPUÉS de "compromiso_equipo"
            start_c_eq = colmap.get("compromiso_equipo")
            if start_c_eq is not None: # Solo si encontramos la columna compromiso
                for c in range(start_c_eq + 1, ncols):
                    raw = row_vals[c]
                    if HEADER_HINTS["fecha_compromiso_equipo"].search(raw) or HEADER_HINTS["fecha_compromiso_equipo"].search(_norm(raw)):
                        colmap["fecha_compromiso_equipo"] = c
                        break # Encontramos la primera

            # 3. Asignar 'fecha_compromiso_operador'
            # Buscar "Fecha" DESPUÉS de "compromiso_operador"
            start_c_op = colmap.get("compromiso_operador")
            if start_c_op is not None: # Solo si encontramos la columna compromiso
                # Empezar a buscar DESPUÉS de la primera fecha encontrada (si existe)
                start_c_op_search = start_c_op + 1
                if colmap.get("fecha_compromiso_equipo") is not None:
                     start_c_op_search = max(start_c_op_search, colmap["fecha_compromiso_equipo"] + 1)
                
                for c in range(start_c_op_search, ncols):
                    raw = row_vals[c]
                    if HEADER_HINTS["fecha_compromiso_operador"].search(raw) or HEADER_HINTS["fecha_compromiso_operador"].search(_norm(raw)):
                        colmap["fecha_compromiso_operador"] = c
                        break # Encontramos la segunda
            
            # --- FIN CAMBIO ---

            # Score para elegir mejor fila de header
            score = sum(1 for v in colmap.values() if v is not None)
            
            # Requerir 'Cargo' y 'Equipo' como mínimo
            if (score > best_score and 
                colmap.get("cargo") is not None and
                colmap.get("equipo") is not None):
                best_score = score
                header_row = r
                best_map = colmap

        if header_row is None:
            return None, {}

        logger.debug("DotacionMaquinaria header at row %s | colmap=%s", header_row, best_map)
        return header_row, best_map

    # ----------------------------- parseo de filas -----------------------------
    
    # --- CAMBIO: Lógica de parseo de fecha ---
    def _parse_rows(
        self,
        df: pd.DataFrame,
        header_row: int,
        colmap: Dict[str, Optional[int]],
    ) -> List[Dict[str, Any]]:
        """Parsea las tablas paralelas de Dotación y Maquinaria, fila por fila."""

        start_data = header_row + 1
        nrows = len(df)
        consecutive_blank = 0

        def row_is_blank(idx: int) -> bool:
            row = df.iloc[idx, :].tolist()
            for v in row:
                if v is None: continue
                if isinstance(v, float) and pd.isna(v): continue
                if str(v).strip() != "": return False
            return True

        parsed_rows: List[Dict[str, Any]] = []

        for r in range(start_data, nrows):
            if row_is_blank(r):
                consecutive_blank += 1
                if consecutive_blank >= 3: break
                continue
            consecutive_blank = 0
            
            row_data = {
                "excel_row_index": r,
                
                # Tabla Izquierda (Personal)
                "cargo": _safe_str(df, r, colmap.get("cargo")),
                "turno_a": _safe_float(df, r, colmap.get("turno_a")),
                "turno_b": _safe_float(df, r, colmap.get("turno_b")),
                
                # Tabla Derecha (Maquinaria)
                "equipo": _safe_str(df, r, colmap.get("equipo")),
                "peak": _safe_float(df, r, colmap.get("peak")),
                "proyectado": _safe_float(df, r, colmap.get("proyectado")),
                "total_en_obra": _safe_float(df, r, colmap.get("total_en_obra")),
                "equipos_en_falla": _safe_float(df, r, colmap.get("equipos_en_falla")),
                "equipo_en_mantencion": _safe_float(df, r, colmap.get("equipo_en_mantencion")),
                "operadores_disponibles": _safe_float(df, r, colmap.get("operadores_disponibles")),
                "equipos_operativos": _safe_float(df, r, colmap.get("equipos_operativos")),
                "reserva": _safe_float(df, r, colmap.get("reserva")),
                "observacion": _safe_str(df, r, colmap.get("observacion")),
                "descripcion_falla": _safe_str(df, r, colmap.get("descripcion_falla")),
                "compromiso_equipo": _safe_str(df, r, colmap.get("compromiso_equipo")),
                # --- CAMBIO: Usar _safe_str para pasar a _parse_date_cell ---
                "fecha_compromiso_equipo": _parse_date_cell(_safe_str(df, r, colmap.get("fecha_compromiso_equipo"))),
                "compromiso_operador": _safe_str(df, r, colmap.get("compromiso_operador")),
                "fecha_compromiso_operador": _parse_date_cell(_safe_str(df, r, colmap.get("fecha_compromiso_operador"))),
                # --- FIN CAMBIO ---
            }

            if _is_phantom_row(row_data):
                continue
            
            parsed_rows.append(row_data)
        
        return parsed_rows