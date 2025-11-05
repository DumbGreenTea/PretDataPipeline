# pretdb/etl/transformers/ppc_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

logger = logging.getLogger(__name__)

# =============================== utilidades ===============================
# (Copiadas de los otros transformers)

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
    if "/" in s: # Manejar '6/4'
        s = s.split("/")[0]
    if s == "" or s in ("-", "—") or s.lower() in ("na", "nan", "<na>"):
        return None
    # Manejar porcentajes que ya vienen como '0.705...'
    if s.startswith("0."):
        try:
            return float(s)
        except Exception:
            pass # Dejar que el regex lo intente
            
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

def _row_key(fecha: date) -> str:
    """ID estable por fecha."""
    base = fecha.isoformat()
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================= clase transformador =============================

class PPCTransformer:
    """
    Extrae la tabla de PPC (Percent Plan Complete).
    Esta tabla está "pivotada": cada columna es un día.
    El transformer la "des-pivota" para crear una fila por día,
    que es un formato mucho más útil para SQL.
    """
    sheet_key = "ppc"

    # ---------------------- API para el orquestador ----------------------

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PPCTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        try:
            rows_map = self._find_rows(df)
        except ValueError as e:
            logger.error("Error al buscar filas de PPC: %s", e)
            raise e

        parsed_rows_list = self._parse_columns(df, rows_map)

        # Preparar flat_rows y unified_rows
        flat_rows = []
        unified_rows = []
        
        for row in parsed_rows_list:
            fecha = row["fecha"]
            # No podemos crear row_id si la fecha es nula
            if not fecha:
                continue
                
            row_id = _row_key(fecha)

            flat = {
                "row_id": row_id,
                **row # (fecha, dia_semana, programadas, etc.)
            }
            flat_rows.append(flat)

            unified = {
                "row_id": row_id,
                "meta": {
                    "fecha": fecha,
                    "dia_semana": row.get("dia_semana"),
                },
                "datos_ppc": {
                    "programadas": row.get("programadas"),
                    "realizadas": row.get("realizadas"),
                    "ppc_porcentaje": row.get("ppc_porcentaje"),
                }
            }
            unified_rows.append(unified)

        summary = {
            "rows_map": rows_map,
            "counts": {
                "dias_encontrados": len(unified_rows),
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
            "horarios_inicio": [], # Vacío, por consistencia
            "unified": unified_rows,
            "by_tag": {"ppc": flat_rows},
            "blocks": {"ppc": unified_rows},
        }
        return out

    # ----------------------------- Detección y Parseo -----------------------------

    def _find_rows(self, df: pd.DataFrame) -> Dict[str, int]:
        """Encuentra los números de fila para Fechas, Días, Programadas, Realizadas y PPC."""
        rows_map: Dict[str, int] = {}
        
        # 1. Encontrar la fila de Fechas (buscar una fila con >3 fechas)
        for r in range(min(len(df), 20)):
            row_vals = [df.iat[r, c] for c in range(min(df.shape[1], 10))]
            date_count = sum(1 for v in row_vals if _parse_date_cell(v) is not None)
            if date_count > 3: # Asumir que esta es la fila de fechas
                rows_map["fecha"] = r
                rows_map["dia_semana"] = r - 1 # Asumir que el nombre del día está justo arriba
                break
        
        if "fecha" not in rows_map:
            raise ValueError("No se pudo encontrar la fila de fechas (ej. 2025-10-25).")
        
        # 2. Encontrar las filas de datos (buscar en la columna B, índice 1)
        for r in range(min(len(df), 20)):
            header_val = _norm(df.iat[r, 1]) # Escanear Columna B (índice 1)
            
            if "programadas" in header_val:
                rows_map["programadas"] = r
            elif "realizadas" in header_val:
                rows_map["realizadas"] = r
            elif "ppc" in header_val:
                rows_map["ppc"] = r
        
        if not all(k in rows_map for k in ["programadas", "realizadas", "ppc"]):
            raise ValueError("No se encontraron las filas 'PROGRAMADAS', 'REALIZADAS' o 'PPC %' en Col B.")
        
        logger.debug("Mapa de filas PPC encontrado: %s", rows_map)
        return rows_map

    def _parse_columns(self, df: pd.DataFrame, rows_map: Dict[str, int]) -> List[Dict[str, Any]]:
        """Itera HORIZONTALMENTE por las columnas y crea una FILA por cada día."""
        parsed_rows = []
        
        date_row_idx = rows_map["fecha"]
        day_row_idx = rows_map["dia_semana"]
        prog_row_idx = rows_map["programadas"]
        real_row_idx = rows_map["realizadas"]
        ppc_row_idx = rows_map["ppc"]
        
        # Iterar por las columnas, empezando en la C (índice 2)
        for c in range(2, min(df.shape[1], 15)): # Limitar búsqueda
            fecha = _parse_date_cell(df.iat[date_row_idx, c])
            
            # Si no hay fecha en la columna, parar.
            # Esto ignora "Adherencia" y cualquier columna de total.
            if fecha is None:
                break 
            
            dia_semana = _safe_str(df, day_row_idx, c)
            programadas = _safe_float(df, prog_row_idx, c)
            realizadas = _safe_float(df, real_row_idx, c)
            ppc_porcentaje = _safe_float(df, ppc_row_idx, c)
            
            # Recalcular PPC si falta pero los otros dos están
            if ppc_porcentaje is None and programadas is not None and programadas > 0 and realizadas is not None:
                ppc_porcentaje = realizadas / programadas

            # Crear la fila
            row_data = {
                "fecha": fecha,
                "dia_semana": dia_semana,
                "programadas": programadas,
                "realizadas": realizadas,
                "ppc_porcentaje": ppc_porcentaje,
            }
            
            parsed_rows.append(row_data)
        
        return parsed_rows