# pretdb/etl/transformers/plan_dia_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

from pretdb.etl.transformers.registry import register_transformer

logger = logging.getLogger(__name__)

# =============================== utilidades (transformer) ===============================

def _norm(s: Any) -> str:
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
    return s == "" or s.upper() in ("<NA>", "NA", "NAN", "NONE")

def _is_phantom_row(row_data: dict) -> bool:
    text_cols = [
        'ruta_critica', 'area_trabajo', 'descripcion_item',
        'riesgos_criticos', 'restricciones', 'restricciones_resp'
    ]
    num_cols = ['cant_programada', 'cant_proyectada_dia']
    no_text = all(_empty_text(row_data.get(k)) for k in text_cols)
    no_nums = all(row_data.get(k) is None for k in num_cols)
    return no_text and no_nums

def _row_key(row: dict) -> str:
    base = "|".join([
        (row.get("fecha").isoformat() if isinstance(row.get("fecha"), date) else str(row.get("fecha") or "")),
        str(row.get("id_p6") or ""),
        _norm(row.get("area_trabajo") or ""),
        _norm(row.get("descripcion_item") or ""),
        str(row.get("nro_pod") or ""),
        str(row.get("tipo_tc") or ""),
    ])
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================ patrones de header ============================

HEADER_HINTS = {
    "nro_pod": re.compile(r"\b(n[°º]\s*)?pod\b", re.I),
    "tipo_tc": re.compile(r"triemanal|colchon", re.I),
    "fecha": re.compile(r"\bfecha\b", re.I),
    "responsable": re.compile(r"\bresponsable\b", re.I),
    "id_p6": re.compile(r"\b(id|codigo)\s*p6\b", re.I),
    "ruta_critica": re.compile(r"ruta\s*critica", re.I),
    "area_trabajo": re.compile(r"\bar(ea)?\s*de\s*trabajo\b|\barea\b", re.I),
    "descripcion_item": re.compile(r"\bdescri(pci[oó]n)\b.*(item|i[íi]tem)?|\bdesc\.", re.I),
    "unidad": re.compile(r"\bunidad(es)?\b|\buni\.", re.I),
    "cant_programada": re.compile(r"\bcantidad\b.*programada|\bcant(idad)?\s*prog(?!\.)", re.I),
    "cant_proyectada_dia": re.compile(r"cantidad\s*proyectada", re.I),
    "riesgos_criticos": re.compile(r"riesgos\s*criticos", re.I),
    "restricciones": re.compile(r"\brestricciones\b", re.I),
}

SECTION_MARKERS_PATTERNS: Dict[re.Pattern, int] = {
    re.compile(r"actividades\s*realizables", re.I): 1,
    re.compile(r"actividad(es)?\s*colch[oó]n", re.I): 2,
    re.compile(r"actividades\s*no\s*realizables", re.I): 0,
}

HORARIO_INICIO_TITLE = re.compile(r"horario\s*de\s*inicio\s*(de\s*trabajo)?", re.I)

# ============================= clase transformador =============================

@register_transformer("plan_dia")
class PlanDiaTransformer:
    sheet_key = "plan_dia"

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PlanDiaTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Plan Dia'.")

        horario_row_start = self._find_horario_inicio_row(df, header_row + 1, len(df))
        stop_at_row = horario_row_start if horario_row_start is not None else len(df)

        section_markers = self._find_section_markers(df, header_row + 1, stop_at_row)
        if not section_markers or section_markers[0][0] > header_row + 5:
            logger.warning(
                "No se encontraron títulos de sección; asumiendo 'realizable' desde fila %s.",
                header_row + 1
            )
            section_markers.insert(0, (header_row + 1, 1))

        parsed_rows_list = self._parse_rows(df, header_row, colmap, section_markers, stop_at_row)
        horarios_list = self._parse_horario_inicio(df, horario_row_start)

        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []
        plan_realizables_list = []
        plan_no_realizables_list = []

        for row in parsed_rows_list:
            row_id = _row_key(row)
            meta_row = {
                "nro_pod": row.get("nro_pod"),
                "realizada": row.get("realizable"),
                "tipo_tc": row.get("tipo_tc"),
                "fecha": row.get("fecha"),
                "responsable": row.get("responsable"),
                "id_p6": row.get("id_p6"),
                "area_trabajo": row.get("area_trabajo"),
                "descripcion_item": row.get("descripcion_item"),
                "unidad": row.get("unidad"),
            }
            plan_dia_tag = {
                "ruta_critica": row.get("ruta_critica"),
                "cant_programada": row.get("cant_programada"),
                "cant_proyectada_dia": row.get("cant_proyectada_dia"),
                "riesgos_criticos": row.get("riesgos_criticos"),
                "restricciones": row.get("restricciones"),
                "restricciones_resp": row.get("restricciones_resp"),
            }
            unified_rows.append({
                "row_id": row_id,
                "meta": meta_row,
                "plan_dia": plan_dia_tag,
                "cnc": None,
                "cantidad_semanal": None,
                "inicio_trabajos": None,
            })
            flat_rows.append({"row_id": row_id, **meta_row, **plan_dia_tag})

            if meta_row.get("realizada") in (1, 2):
                plan_realizables_list.append({"meta": meta_row, **plan_dia_tag})
            elif meta_row.get("realizada") == 0:
                plan_no_realizables_list.append({"meta": meta_row, **plan_dia_tag})

        summary = {
            "header_row": header_row,
            "section_markers": section_markers,
            "horario_inicio_row": horario_row_start,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "counts": {
                "plan_realizables": len(plan_realizables_list),
                "plan_no_realizables": len(plan_no_realizables_list),
                "horarios_inicio_frentes": len(horarios_list),
                "unified_rows": len(unified_rows),
            },
            "samples": {
                "unified": unified_rows[:2],
                "flat": flat_rows[:2],
                "horarios_inicio": horarios_list[:5],
            }
        }

        return {
            "sheet": self.sheet_key,
            "rows": len(unified_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "horarios_inicio": horarios_list,
            "unified": unified_rows,
            "by_tag": {"plan_dia": plan_realizables_list + plan_no_realizables_list},
            "blocks": {
                "plan_dia": {
                    "realizables": plan_realizables_list,
                    "no_realizables": plan_no_realizables_list,
                },
                "cnc": [],
                "semanal_acumulado": [],
                "inicio_trabajos_timeline": [],
            },
        }

    # ------- header -------
    def _find_header_and_columns(self, df: pd.DataFrame) -> Tuple[Optional[int], Dict[str, Optional[int]]]:
        nrows = min(len(df), 60)
        ncols = min(df.shape[1], 80)
        header_row: Optional[int] = None
        best_score = -1
        best_map: Dict[str, Optional[int]] = {}
        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            colmap: Dict[str, Optional[int]] = {k: None for k in HEADER_HINTS.keys()}
            for c, raw in enumerate(row_vals):
                s_norm = _norm(raw)
                for k, pat in HEADER_HINTS.items():
                    if colmap.get(k) is None and (pat.search(raw) or pat.search(s_norm)):
                        colmap[k] = c
            # 'responsable' de restricciones (a la derecha)
            if colmap.get("restricciones") is not None:
                rest_c = colmap["restricciones"]
                cand = None
                resp_pat = re.compile(r"\bresponsable\b|tipo", re.I)
                for c in range(rest_c + 1, ncols):
                    raw = row_vals[c]
                    if resp_pat.search(raw) or resp_pat.search(_norm(raw)):
                        cand = c
                        break
                colmap["restricciones_resp"] = cand
            else:
                colmap["restricciones_resp"] = None
            score = sum(1 for v in colmap.values() if v is not None)
            if (score > best_score and
                colmap.get("descripcion_item") is not None and
                colmap.get("cant_programada") is not None):
                best_score = score
                header_row = r
                best_map = colmap
        if header_row is None:
            return None, {}
        logger.debug("PlanDia header at row %s | colmap=%s", header_row, best_map)
        return header_row, best_map

    # ------- marcadores de sección -------
    def _find_section_markers(self, df: pd.DataFrame, start_row: int, stop_row: int) -> List[Tuple[int, int]]:
        markers = []
        for r in range(start_row, min(len(df), stop_row)):
            line = " ".join([_norm(str(x)) for x in df.iloc[r, :min(df.shape[1], 5)]])
            for pat, value in SECTION_MARKERS_PATTERNS.items():
                if pat.search(line):
                    markers.append((r, value))
                    break
        markers.sort(key=lambda x: x[0])
        logger.debug("Marcadores de sección encontrados: %s", markers)
        return markers

    # ------- 'Horario de inicio' (solo parseo) -------
    def _find_horario_inicio_row(self, df: pd.DataFrame, start_row: int, stop_row: int) -> Optional[int]:
        for r in range(start_row, min(stop_row, len(df))):
            for c in range(min(df.shape[1], 10)):
                val = str(df.iat[r, c])
                if re.search(r"horario\s*de\s*inicio\s*(de\s*trabajo)?", val, re.I):
                    logger.debug("Encontrado bloque 'Horario de Inicio' en fila %s", r)
                    return r
        logger.debug("No se encontró bloque 'Horario de Inicio'")
        return None

    def _parse_horario_inicio(self, df: pd.DataFrame, start_row: Optional[int]) -> List[Dict[str, Any]]:
        if start_row is None or start_row >= len(df) - 1:
            return []
        frente_row_idx = None
        for r in range(start_row, min(start_row + 4, len(df))):
            row_str = " ".join([_norm(c) for c in df.iloc[r, :min(df.shape[1], 10)]])
            if "frente" in row_str or "horario de inicio efectivo" in row_str:
                frente_row_idx = r
                break
        if frente_row_idx is None:
            for r in range(start_row + 1, min(start_row + 4, len(df))):
                non_empty = [c for c in df.iloc[r] if _norm(c) not in ("", "nan")]
                if len(non_empty) > 1:
                    frente_row_idx = r
                    break
        if frente_row_idx is None or frente_row_idx + 1 >= len(df):
            return []
        hora_row_idx = frente_row_idx + 1
        frentes = []
        for c in range(df.shape[1]):
            frente_val = _safe_str(df, frente_row_idx, c)
            hora_val = _safe_str(df, hora_row_idx, c)
            hora_parsed = None
            if hora_val:
                m = re.match(r"^(\d{1,2}):(\d{2})", hora_val.strip())
                if m:
                    hh, mm = int(m.group(1)), int(m.group(2))
                    if 0 <= hh <= 23 and 0 <= mm <= 59:
                        hora_parsed = f"{hh:02d}:{mm:02d}"
            if _norm(frente_val) in ("frente", "horario de inicio efectivo"):
                continue
            if frente_val or hora_parsed:
                frentes.append({"frente": frente_val, "hora_inicio": hora_parsed})
        logger.debug("Parseados %s frentes de 'Horario de Inicio'", len(frentes))
        return frentes

    # ------- parseo de filas -------
    def _parse_rows(
        self,
        df: pd.DataFrame,
        header_row: int,
        colmap: Dict[str, Optional[int]],
        section_markers: List[Tuple[int, int]],
        stop_at_row: int,
    ) -> List[Dict[str, Any]]:
        start_data = header_row + 1
        nrows = min(len(df), stop_at_row)
        consecutive_blank = 0

        def get_section_flag(r: int) -> Optional[int]:
            current_flag = None
            for marker_row, flag_value in section_markers:
                if r >= marker_row:
                    current_flag = flag_value
                else:
                    break
            return current_flag

        def row_is_blank(idx: int) -> bool:
            row = df.iloc[idx, :].tolist()
            for v in row:
                if v is None: 
                    continue
                if isinstance(v, float) and pd.isna(v): 
                    continue
                if str(v).strip() != "": 
                    return False
            return True

        parsed_rows: List[Dict[str, Any]] = []
        for r in range(start_data, nrows):
            if row_is_blank(r):
                consecutive_blank += 1
                if consecutive_blank >= 3:
                    break
                continue
            consecutive_blank = 0

            flag = get_section_flag(r)
            if flag is None:
                continue

            row_data = {
                "excel_row_index": r,
                "realizable": flag,  # 1/2/0
                "nro_pod": _safe_str(df, r, colmap.get("nro_pod")),
                "tipo_tc": _safe_str(df, r, colmap.get("tipo_tc")),
                "fecha": _parse_date_cell(_safe_str(df, r, colmap.get("fecha"))),
                "responsable": _safe_str(df, r, colmap.get("responsable")),
                "id_p6": _safe_str(df, r, colmap.get("id_p6")),
                "ruta_critica": _safe_str(df, r, colmap.get("ruta_critica")),
                "area_trabajo": _safe_str(df, r, colmap.get("area_trabajo")),
                "descripcion_item": _safe_str(df, r, colmap.get("descripcion_item")),
                "unidad": _safe_str(df, r, colmap.get("unidad")),
                "cant_programada": _safe_float(df, r, colmap.get("cant_programada")),
                "cant_proyectada_dia": _safe_float(df, r, colmap.get("cant_proyectada_dia")),
                "riesgos_criticos": _safe_str(df, r, colmap.get("riesgos_criticos")),
                "restricciones": _safe_str(df, r, colmap.get("restricciones")),
                "restricciones_resp": _safe_str(df, r, colmap.get("restricciones_resp")),
            }
            if _is_phantom_row(row_data):
                continue
            parsed_rows.append(row_data)
        return parsed_rows
