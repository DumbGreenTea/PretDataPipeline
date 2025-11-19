# pretdb/etl/transformers/ppc_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import pandas as pd

from pretdb.etl.transformers.registry import register_transformer
from pretdb.etl.utils import parse_contract_from_df

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
    """Convierte a float números o strings; '-' y vacíos -> None. Soporta '6/4' -> 6."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    s = str(val).strip()
    if "/" in s:  # ej. '6/4'
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

def _to_ratio(val: Any) -> Optional[float]:
    """
    Convierte entrada a razón 0–1. Acepta:
    - 0.72      -> 0.72
    - '72%'     -> 0.72
    - '72'      -> 0.72 (asumiendo que < 100 es porcentaje)
    - '0,72'    -> 0.72
    """
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s in ("-", "—") or s.lower() in ("na", "nan", "<na>"):
        return None

    # Si viene con '%', extraer número y dividir por 100
    if "%" in s:
        m = re.findall(r"[-+]?\d*\.?\d+", s.replace(",", "."))
        if not m:
            return None
        try:
            num = float(m[0])
            return num / 100.0
        except Exception:
            return None

    # Si viene como decimal ya (0.7, 0,72)
    if s.startswith("0.") or s.startswith(".") or s.startswith("0,") or s.startswith(","):
        try:
            return float(s.replace(",", "."))
        except Exception:
            pass

    # Si viene como entero/float sin símbolo %
    v = _to_float(s)
    if v is None:
        return None
    # Heurística: 0–1 -> ya es razón; 1.0–100.0 -> tratar como porcentaje
    if 0.0 <= v <= 1.0:
        return v
    if 1.0 < v <= 100.0:
        return v / 100.0
    # Valores >100 no son PPC válidos; descartar
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

def _safe_ratio(df: pd.DataFrame, r: int, c: Optional[int]) -> Optional[float]:
    if c is None or c >= df.shape[1] or r >= len(df) or r < 0 or c < 0:
        return None
    return _to_ratio(df.iat[r, c])

def _row_key(fecha: date) -> str:
    """ID estable por fecha."""
    base = fecha.isoformat()
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# --- utilidades Decimal para loader ---
def _qdec(val: Optional[float], places: int) -> Optional[Decimal]:
    """Convierte float/str a Decimal cuantizado."""
    if val is None:
        return None
    try:
        d = Decimal(str(val))
        q = Decimal("1").scaleb(-places)  # 10^-places
        return d.quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None

# ============================= clase transformador =============================

@register_transformer("ppc")
class PPCTransformer:
    """
    Extrae la tabla de PPC (Percent Plan Complete) pivotada por columnas (días)
    y la des-pivota a una fila por día.
    - ppc_porcentaje se entrega como razón 0–1 (no 0–100).
    """
    sheet_key = "ppc"

    # ---------------------- API para el orquestador ----------------------

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PPCTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()
        contract_num, contract_name = parse_contract_from_df(df)

        rows_map = self._find_rows(df)
        parsed_rows_list = self._parse_columns(df, rows_map)

        # Preparar flat_rows y unified_rows
        flat_rows: List[Dict[str, Any]] = []
        unified_rows: List[Dict[str, Any]] = []
        
        for row in parsed_rows_list:
            fecha = row["fecha"]
            if not fecha:
                continue
                
            row_id = _row_key(fecha)

            flat = {
                "row_id": row_id,
                **row  # (fecha, dia_semana, programadas, realizadas, ppc_porcentaje)
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
            "horarios_inicio": [],  # por consistencia
            "unified": unified_rows,
            "contract": {"numero": contract_num, "nombre": contract_name},
            "by_tag": {"ppc": flat_rows},
            "blocks": {"ppc": unified_rows},
        }
        return out

    # ----------------------------- Detección y Parseo -----------------------------

    def _find_rows(self, df: pd.DataFrame) -> Dict[str, int]:
        """
        Encuentra índices de fila para:
        - 'fecha' (fila con varias fechas)
        - 'dia_semana' (justo arriba de la de fecha)
        - 'programadas', 'realizadas', 'ppc' (en primera o segunda columna)
        """
        rows_map: Dict[str, int] = {}
        max_scan_rows = min(len(df), 40)
        max_scan_cols = min(df.shape[1], 20)

        # 1) Fila de fechas: una fila con >3 fechas
        for r in range(max_scan_rows):
            row_vals = [df.iat[r, c] for c in range(max_scan_cols)]
            date_count = sum(1 for v in row_vals if _parse_date_cell(v) is not None)
            if date_count >= 3:
                rows_map["fecha"] = r
                rows_map["dia_semana"] = r - 1 if r - 1 >= 0 else r
                break
        if "fecha" not in rows_map:
            raise ValueError("PPC: No se pudo encontrar la fila de fechas.")

        # 2) Filas de datos: buscar en col 0 y 1 para ser más tolerantes
        keys = {"programadas": None, "realizadas": None, "ppc": None}
        for r in range(max_scan_rows):
            for c in (0, 1):
                header_val = _norm(df.iat[r, c])
                if keys["programadas"] is None and "programadas" in header_val:
                    keys["programadas"] = r
                elif keys["realizadas"] is None and "realizadas" in header_val:
                    keys["realizadas"] = r
                elif keys["ppc"] is None and ("ppc" in header_val):
                    keys["ppc"] = r

        if not all(keys[k] is not None for k in keys):
            raise ValueError("PPC: No se hallaron filas de 'PROGRAMADAS'/'REALIZADAS'/'PPC %' en col A/B.")

        rows_map.update(keys)
        logger.debug("PPC rows_map: %s", rows_map)
        return rows_map

    def _parse_columns(self, df: pd.DataFrame, rows_map: Dict[str, int]) -> List[Dict[str, Any]]:
        """
        Itera horizontalmente por columnas (desde C) y crea una fila por día.
        Se detiene al encontrar la primera columna sin fecha tras haber encontrado al menos una.
        """
        parsed_rows: List[Dict[str, Any]] = []
        
        date_row_idx = rows_map["fecha"]
        day_row_idx = rows_map["dia_semana"]
        prog_row_idx = rows_map["programadas"]
        real_row_idx = rows_map["realizadas"]
        ppc_row_idx = rows_map["ppc"]

        found_any_date = False
        
        for c in range(2, df.shape[1]):  # desde columna C
            fecha = _parse_date_cell(df.iat[date_row_idx, c])
            if fecha is None:
                if found_any_date:
                    # al primer vacío después de haber visto fechas, paramos (evita "Adherencia"/"Totales")
                    break
                else:
                    continue

            found_any_date = True
            dia_semana = _safe_str(df, day_row_idx, c)
            programadas = _safe_float(df, prog_row_idx, c)
            realizadas = _safe_float(df, real_row_idx, c)
            ppc_porcentaje = _safe_ratio(df, ppc_row_idx, c)

            # Recalcular PPC si falta pero hay base
            if ppc_porcentaje is None and programadas is not None and programadas > 0 and realizadas is not None:
                ppc_porcentaje = realizadas / programadas

            row_data = {
                "fecha": fecha,
                "dia_semana": dia_semana,
                "programadas": programadas,
                "realizadas": realizadas,
                "ppc_porcentaje": ppc_porcentaje,  # razón 0-1
            }
            parsed_rows.append(row_data)
        
        return parsed_rows


# ================================ Loader =================================
class PPCLoader:
    """
    Loader opcional para persistir PPC por fecha.
    - Interpreta ppc_porcentaje como razón 0–1.
    - Convierte a Decimal (programadas/realizadas: 2 dec; ppc: 4 dec).
    - update_or_create por (pod, fecha).
    """
    PROGRAMADAS_PLACES = 2
    REALIZADAS_PLACES = 2
    PPC_PLACES = 4

    def save(self, transformed: dict, meta: dict) -> None:
        """Backward-compatible shim: delegate to pretdb.etl.loaders.PPCLoader.persist

        Keep the old `save(transformed, meta)` signature for callers that still
        use it; meta may contain 'pod' or 'pod_id'.
        """
        try:
            from pretdb.etl.loaders.ppc_loader import PPCLoader as _Loader
        except Exception as e:
            logger.warning("PPCLoader shim: no se pudo importar loader: %s", e)
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
        # note: old API didn't support dry_run explicitly; we assume write mode
        try:
            loader.persist(pod, transformed, dry_run=False)
        except Exception as e:
            logger.exception("PPCLoader shim: error delegating persist: %s", e)
