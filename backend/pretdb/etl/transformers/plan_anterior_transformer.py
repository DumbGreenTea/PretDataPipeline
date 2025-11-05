# pretdb/etl/transformers/plan_anterior_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata, hashlib
from datetime import datetime, date
import pandas as pd

logger = logging.getLogger(__name__)

# =============================== utilidades ===============================
# (Sin cambios en esta sección)

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
    # --- FIX ---
    # Ahora filtra <NA>, nan, etc.
    if s == "" or s.upper() in ("<NA>", "NA", "NAN", "NONE"):
        return None
    # --- FIN FIX ---
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

def _all_none_dict(d: Dict[str, Optional[float]]) -> bool:
    return all(v is None for v in d.values()) if isinstance(d, dict) else True

def _is_phantom_row(
    desc: Optional[str],
    area: Optional[str],
    cant_prog: Optional[float],
    cant_real: Optional[float],
    cumplimiento: Optional[float],
    sem_prog: Optional[float],
    sem_real: Optional[float],
    sem_avance: Optional[float],
    fc_forecast: Optional[float],
    fc_real: Optional[float],
    fc_avance: Optional[float],
    timeline_dict: Dict[str, Optional[float]],
) -> bool:
    """Detecta filas 'fantasma': sin texto útil, sin números y sin timeline."""
    no_text = _empty_text(desc) and _empty_text(area)
    no_nums = all(v is None for v in [
        cant_prog, cant_real, cumplimiento,
        sem_prog, sem_real, sem_avance,
        fc_forecast, fc_real, fc_avance
    ])
    no_timeline = _all_none_dict(timeline_dict)
    return no_text and no_nums and no_timeline

def _row_key(
    fecha: Optional[date],
    id_p6: Optional[str],
    area: Optional[str],
    desc: Optional[str],
    nro_pod: Optional[str],
    tipo_tc: Optional[str],
) -> str:
    """ID estable por fila/tarea."""
    base = "|".join([
        (fecha.isoformat() if isinstance(fecha, date) else str(fecha or "")),
        str(id_p6 or ""),
        _norm(area or ""),
        _norm(desc or ""),
        str(nro_pod or ""),
        str(tipo_tc or ""),
    ])
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

# ============================ patrones de header ============================
# (Sin cambios en esta sección)

HEADER_HINTS = {
    "nro_pod": re.compile(r"\b(n[°º]\s*)?pod\b", re.I),
    "tipo_tc": re.compile(r"trisemanal|colchon", re.I),
    "fecha": re.compile(r"\bfecha\b", re.I),
    "responsable": re.compile(r"\bresponsable\b", re.I),
    "id_p6": re.compile(r"\b(id|codigo)\s*p6\b", re.I),
    "area_trabajo": re.compile(r"\bar(ea)?\s*de\s*trabajo\b|\barea\b", re.I),
    "descripcion_item": re.compile(r"\bdescri(pci[oó]n)\b.*(item|i[íi]tem)?|\bdesc\.", re.I),
    "unidad": re.compile(r"\bunidad(es)?\b|\buni\.", re.I),
    "cant_programada": re.compile(r"\bcantidad\b.*programada|\bcant(idad)?\s*prog(?!\.)", re.I),
    "cant_real": re.compile(r"\bcantidad\b.*real(?!\.)|\bcant(idad)?\s*real(?!\.)\b", re.I),
    "cumplimiento": re.compile(r"\bcumpl(imiento)?\b|%.*cumpl", re.I),

    "cnc_causa": re.compile(r"\bcausa\b", re.I),
    "cnc_subcausa": re.compile(r"\bsubcausa\b", re.I),
    "cnc_tipo": re.compile(r"\btipo\b", re.I),
    "cnc_descripcion": re.compile(r"\bdescri(pci[oó]n)\s*de\s*cnc\b|\bdesc\.\s*cnc\b", re.I),

    # Semanal: exigir "CANT." para no confundir con “Cantidad …”
    "sem_cant_prog": re.compile(r"\bcant\.\s*prog\b", re.I),
    "sem_cant_real": re.compile(r"\bcant\.\s*real\b", re.I),
    "%_avance": re.compile(r"%\s*avance|\bporc(entaje)?\s*avance\b", re.I),

    "fc_cant_forecast": re.compile(r"forecast|pron[oó]stico", re.I),
}

SEC_TITLES = {
    "realizadas": re.compile(r"actividades\s*realizadas", re.I),
    "no_realizadas": re.compile(r"actividades\s*no\s*realizadas", re.I),
}

HORARIO_INICIO_TITLE = re.compile(r"horario\s*de\s*inicio\s*(de\s*trabajo)?", re.I)

# ============================= clase transformador =============================

class PlanAnteriorTransformer:
    """
    Extrae desde '4.Plan anterior' y entrega:
      - blocks: mismas listas por sección (compatibilidad)
      - unified: UNA FILA POR TAREA con sub-bloques etiquetados
      - by_tag: listas ya separadas por tag (con row_id/meta)
      - flat_rows: filas aplanadas para SQL/csv
      - horarios_inicio: lista separada del bloque "Horario de Inicio"
    """
    sheet_key = "plan_anterior"

    # ---------------------- API para el orquestador ----------------------

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PlanAnteriorTransformer.run_df: shape=%s", df_raw.shape)
        df = df_raw.copy()

        header_row, colmap, timeline = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Plan anterior'.")

        sec_rows = self._find_section_rows(df, header_row + 1)
        
        horario_row_start = self._find_horario_inicio_row(df, header_row + 1, len(df))
        stop_at_row = horario_row_start if horario_row_start is not None else len(df)

        plan_dict, cnc_dict, semanal_dict, inicio_dict = self._parse_rows(
            df, header_row, colmap, timeline, sec_rows, stop_at_row
        )

        horarios_list = self._parse_horario_inicio(df, horario_row_start)

        if colmap.get("fc_%_avance") == colmap.get("%_avance"):
            for s in semanal_dict.values():
                if s.get("acumulado_forecast"):
                    s["acumulado_forecast"]["avance"] = None

        # ------------------------ UNIFICACIÓN POR FILA ------------------------
        unified_rows: List[Dict[str, Any]] = []
        flat_rows: List[Dict[str, Any]] = []
        by_tag = {
            "plan_anterior": [],
            "cnc": [],
            "cantidad_semanal": [],
            "inicio_trabajos": [],
        }

        all_row_indices = set(plan_dict.keys()) | set(cnc_dict.keys()) | set(semanal_dict.keys()) | set(inicio_dict.keys())

        for r in sorted(list(all_row_indices)):
            
            p = plan_dict.get(r, {})
            s = semanal_dict.get(r, {})
            ini = inicio_dict.get(r, {})
            c = cnc_dict.get(r, {})

            # --- CAMBIO: Reordenado 'meta_row' para coincidir con tu lógica ---
            meta_row = {
                "nro_pod": p.get("nro_pod"),
                "realizada": p.get("realizada") if "realizada" in p else s.get("realizada"),
                "tipo_tc": p.get("tipo_tc"),
                "fecha": p.get("fecha") or s.get("fecha"),
                "responsable": p.get("responsable"),
                "id_p6": p.get("id_p6") or s.get("id_p6"),
                "area_trabajo": p.get("area_trabajo") or ini.get("area_trabajo"),
                "descripcion_item": p.get("descripcion_item") or s.get("descripcion_item"),
                "unidad": p.get("unidad"),
            }
            row_id = _row_key(
                meta_row["fecha"], meta_row["id_p6"], meta_row["area_trabajo"],
                meta_row["descripcion_item"], meta_row["nro_pod"], meta_row["tipo_tc"]
            )

            plan_tag = {
                "cant_programada": p.get("cant_programada"),
                "cant_real": p.get("cant_real"),
                "cumplimiento": p.get("cumplimiento"),
            } if p else None
            
            cnc_tag = None
            if c:
                has_info = any(val not in (None, "", "<NA>", "-") for key, val in c.items()
                               if key in ("cnc_causa", "cnc_subcausa", "cnc_tipo", "cnc_descripcion", "cnc_responsable"))
                if has_info:
                    cnc_tag = {
                        "cnc_causa": c.get("cnc_causa"),
                        "cnc_subcausa": c.get("cnc_subcausa"),
                        "cnc_tipo": c.get("cnc_tipo"),
                        "cnc_descripcion": c.get("cnc_descripcion"),
                        "cnc_responsable": c.get("cnc_responsable"),
                    }

            cantidad_semanal_tag = {
                "semanal": s.get("semanal", {}),
                "acumulado_forecast": s.get("acumulado_forecast", {}),
            } if s else None

            inicio_tag = {
                "timeline": ini.get("timeline", {})
            } if ini else None

            unified = {
                "row_id": row_id,
                "meta": meta_row,
                "plan_anterior": plan_tag,
                "cnc": cnc_tag,
                "cantidad_semanal": cantidad_semanal_tag,
                "inicio_trabajos": inicio_tag,
            }
            unified_rows.append(unified)

            # by_tag (sin cambios)
            if plan_tag:
                by_tag["plan_anterior"].append({"row_id": row_id, "meta": meta_row, **plan_tag})
            if cnc_tag:
                by_tag["cnc"].append({"row_id": row_id, "meta": meta_row, **cnc_tag})
            if cantidad_semanal_tag and (cantidad_semanal_tag["semanal"] or cantidad_semanal_tag["acumulado_forecast"]):
                by_tag["cantidad_semanal"].append({
                    "row_id": row_id,
                    "meta": meta_row,
                    **{
                        "sem_cant_prog": s.get("semanal", {}).get("cant_prog"),
                        "sem_cant_real": s.get("semanal", {}).get("cant_real"),
                        "sem_avance": s.get("semanal", {}).get("avance"),
                        "fc_cant_forecast": s.get("acumulado_forecast", {}).get("cant_forecast"),
                        "fc_cant_real": s.get("acumulado_forecast", {}).get("cant_real"),
                        "fc_avance": s.get("acumulado_forecast", {}).get("avance"),
                    }
                })
            if inicio_tag and inicio_tag["timeline"]:
                by_tag["inicio_trabajos"].append({"row_id": row_id, "meta": meta_row, **inicio_tag})

            # --- CAMBIO: Reordenado 'flat' para coincidir EXACTAMENTE con tu snippet ---
            flat = {
                "row_id": row_id,
                "nro_pod": meta_row["nro_pod"],
                "realizada": meta_row["realizada"],
                "tipo_tc": meta_row["tipo_tc"],
                "fecha": meta_row["fecha"],
                "responsable": meta_row["responsable"],
                "id_p6": meta_row["id_p6"],
                "area_trabajo": meta_row["area_trabajo"],
                "descripcion_item": meta_row["descripcion_item"],
                "unidad": meta_row["unidad"],
                
                "cant_programada": p.get("cant_programada") if p else None,
                "cant_real": p.get("cant_real") if p else None,
                "cumplimiento": p.get("cumplimiento") if p else None,

                "cnc_causa": cnc_tag.get("cnc_causa") if cnc_tag else None,
                "cnc_subcausa": cnc_tag.get("cnc_subcausa") if cnc_tag else None,
                "cnc_tipo": cnc_tag.get("cnc_tipo") if cnc_tag else None,
                "cnc_descripcion": cnc_tag.get("cnc_descripcion") if cnc_tag else None,
                "cnc_responsable": cnc_tag.get("cnc_responsable") if cnc_tag else None,

                "sem_cant_prog": s.get("semanal", {}).get("cant_prog") if s else None,
                "sem_cant_real": s.get("semanal", {}).get("cant_real") if s else None,
                "sem_avance": s.get("semanal", {}).get("avance") if s else None,
                "fc_cant_forecast": s.get("acumulado_forecast", {}).get("cant_forecast") if s else None,
                "fc_cant_real": s.get("acumulado_forecast", {}).get("cant_real") if s else None,
                "fc_avance": s.get("acumulado_forecast", {}).get("avance") if s else None,
            }
            
            # timeline expandido (sin cambios)
            tl = ini.get("timeline", {}) if ini else {}
            for k, v in (tl or {}).items():
                flat[f"timeline_{k}"] = v
            flat_rows.append(flat)

        # Regenerar listas para 'blocks' (sin cambios)
        plan_realizadas_list = []
        plan_no_realizadas_list = []
        cnc_list_final = []
        semanal_list_final = []
        inicio_list_final = []

        for row in unified_rows:
            meta = row["meta"]
            if row["plan_anterior"]:
                plan_data = {"meta": meta, **row["plan_anterior"]}
                if meta.get("realizada") is True:
                    plan_realizadas_list.append(plan_data)
                elif meta.get("realizada") is False:
                    plan_no_realizadas_list.append(plan_data)
            
            if row["cnc"]:
                cnc_list_final.append({"meta": meta, **row["cnc"]})
            
            if row["cantidad_semanal"] and (row["cantidad_semanal"]["semanal"] or row["cantidad_semanal"]["acumulado_forecast"]):
                semanal_list_final.append({"meta": meta, **row["cantidad_semanal"]})
            
            if row["inicio_trabajos"] and row["inicio_trabajos"]["timeline"]:
                inicio_list_final.append({"meta": meta, **row["inicio_trabajos"]})
        
        summary = {
            "header_row": header_row,
            "section_rows": sec_rows,
            "horario_inicio_row": horario_row_start,
            "columns_found": {k: v for k, v in colmap.items() if v is not None},
            "timeline_cols": [d.isoformat() for d in timeline.keys()],
            "counts": {
                "plan_realizadas": len(plan_realizadas_list),
                "plan_no_realizadas": len(plan_no_realizadas_list),
                "cnc": len(cnc_list_final),
                "semanal_acumulado": len(semanal_list_final),
                "inicio_trabajos_timeline": len(inicio_list_final),
                "horarios_inicio_frentes": len(horarios_list),
                "unified_rows": len(unified_rows),
            },
            "samples": {
                "unified": unified_rows[:2],
                "flat": flat_rows[:2],
                "horarios_inicio": horarios_list[:5],
            }
        }

        # --- CAMBIO: Reordenado 'out' final ---
        out = {
            "sheet": self.sheet_key,
            "rows": len(unified_rows),
            "dry_run": dry_run,
            "summary": summary,
            "flat_rows": flat_rows,
            "horarios_inicio": horarios_list,
            "unified": unified_rows,
            "by_tag": by_tag,
            "blocks": {
                "plan_anterior": {
                    "realizadas": plan_realizadas_list,
                    "no_realizadas": plan_no_realizadas_list,
                },
                "cnc": cnc_list_final,
                "semanal_acumulado": semanal_list_final,
                "inicio_trabajos_timeline": inicio_list_final,
            },
        }
        return out

    # ----------------------------- detección header -----------------------------
    # (Sin cambios en esta sección)

    def _find_header_and_columns(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[int], Dict[str, Optional[int]], Dict[date, int]]:
        nrows = min(len(df), 60)
        ncols = min(df.shape[1], 80)
        header_row: Optional[int] = None
        best_score = -1
        best_map: Dict[str, Optional[int]] = {}
        timeline_cols: Dict[date, int] = {}
        sem_anchor_candidates: List[Tuple[int, int]] = []
        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]
            colmap: Dict[str, Optional[int]] = {k: None for k in HEADER_HINTS.keys()}
            for c, raw in enumerate(row_vals):
                if re.search(r"cantidad\s*semanal", _norm(raw), re.I):
                    sem_anchor_candidates.append((r, c))
            for c, raw in enumerate(row_vals):
                s_norm = _norm(raw)
                for k, pat in HEADER_HINTS.items():
                    if colmap.get(k) is None and (pat.search(raw) or pat.search(s_norm)):
                        colmap[k] = c
            if colmap.get("cnc_descripcion") is not None:
                cdesc = colmap["cnc_descripcion"]
                cand = None
                for c in range(cdesc + 1, ncols):
                    raw = row_vals[c]
                    if HEADER_HINTS["responsable"].search(raw) or HEADER_HINTS["responsable"].search(_norm(raw)):
                        cand = c
                        break
                colmap["cnc_responsable"] = cand
            else:
                colmap["cnc_responsable"] = None
            tcols: Dict[date, int] = {}
            for c, raw in enumerate(row_vals):
                d = _parse_date_cell(raw)
                if d is not None:
                    tcols[d] = c
            score = sum(1 for v in colmap.values() if v is not None) + len(tcols)
            if score > best_score:
                best_score = score
                header_row = r
                best_map = colmap
                timeline_cols = tcols
        if header_row is None:
            return None, {}, {}
        def _refine_sem_cols():
            nonlocal best_map
            same_prog = (best_map.get("sem_cant_prog") == best_map.get("cant_programada"))
            same_real = (best_map.get("sem_cant_real") == best_map.get("cant_real"))
            def scan_row_for_sem(row_idx: int, start_c: int = 0):
                sem_prog_c = None
                sem_real_c = None
                sem_avance_c = None
                row_vals = [str(df.iat[row_idx, c]) if c < df.shape[1] else "" for c in range(df.shape[1])]
                for c in range(max(0, start_c), len(row_vals)):
                    raw = row_vals[c]
                    nrm = _norm(raw)
                    if sem_prog_c is None and (HEADER_HINTS["sem_cant_prog"].search(raw) or HEADER_HINTS["sem_cant_prog"].search(nrm)):
                        sem_prog_c = c
                    if sem_real_c is None and (HEADER_HINTS["sem_cant_real"].search(raw) or HEADER_HINTS["sem_cant_real"].search(nrm)):
                        sem_real_c = c
                    if sem_avance_c is None and (HEADER_HINTS["%_avance"].search(raw) or HEADER_HINTS["%_avance"].search(nrm)):
                        sem_avance_c = c
                return sem_prog_c, sem_real_c, sem_avance_c
            updated = False
            anchor_row = None
            anchor_col = None
            for (r, c) in sem_anchor_candidates:
                if abs(r - header_row) <= 3:
                    anchor_row, anchor_col = r, c
                    break
            if anchor_row is not None:
                sp, sr, sa = scan_row_for_sem(header_row, start_c=anchor_col + 1)
                if sp is None and sr is None and sa is None:
                    sp, sr, sa = scan_row_for_sem(anchor_row, start_c=anchor_col + 1)
                if sp is not None:
                    best_map["sem_cant_prog"] = sp; updated = True
                if sr is not None:
                    best_map["sem_cant_real"] = sr; updated = True
                if sa is not None:
                    best_map["%_avance"] = sa; updated = True
            if (same_prog or same_real) and not updated:
                right_start = (df.shape[1] // 2)
                sp, sr, sa = scan_row_for_sem(header_row, start_c=right_start)
                if sp is not None:
                    best_map["sem_cant_prog"] = sp
                if sr is not None:
                    best_map["sem_cant_real"] = sr
                if sa is not None and best_map.get("fc_%_avance") is None:
                    best_map["%_avance"] = sa
        _refine_sem_cols()
        if best_map.get("sem_cant_real") is not None:
            base = best_map["sem_cant_real"]
            extra = None
            for c in range(base + 1, ncols):
                raw = str(df.iat[header_row, c])
                if HEADER_HINTS["sem_cant_real"].search(raw) or HEADER_HINTS["sem_cant_real"].search(_norm(raw)):
                    extra = c
                    break
            best_map["fc_cant_real"] = extra
        avance_c = best_map.get("%_avance")
        if avance_c is not None:
            rep = None
            for c in range(avance_c + 1, ncols):
                raw = str(df.iat[header_row, c])
                if HEADER_HINTS["%_avance"].search(raw) or HEADER_HINTS["%_avance"].search(_norm(raw)):
                    rep = c
                    break
            best_map["fc_%_avance"] = rep
        else:
            best_map["fc_%_avance"] = None
        logger.debug("PlanAnterior header at row %s | colmap=%s | timeline=%s",
                     header_row, best_map, list(timeline_cols.keys()))
        return header_row, best_map, timeline_cols

    # ---------------------------- títulos de sección ----------------------------
    # (Sin cambios en esta sección)

    def _find_section_rows(self, df: pd.DataFrame, start_row: int) -> Dict[str, int]:
        sec = {}
        nrows = len(df)
        for r in range(start_row, min(nrows, start_row + 50)):
            row = [str(x) for x in df.iloc[r, :min(df.shape[1], 5)].tolist()]
            line = " ".join(row)
            if "realizadas_title" not in sec and SEC_TITLES["realizadas"].search(line):
                sec["realizadas_title"] = r
            if "no_realizadas_title" not in sec and SEC_TITLES["no_realizadas"].search(line):
                sec["no_realizadas_title"] = r
        return sec

    # --- NUEVA FUNCIÓN ---
    def _find_horario_inicio_row(self, df: pd.DataFrame, start_row: int, stop_row: int) -> Optional[int]:
        """Busca el título del bloque 'HORARIO DE INICIO...'"""
        for r in range(start_row, min(stop_row, len(df))):
            for c in range(min(df.shape[1], 10)):
                val = str(df.iat[r, c])
                if HORARIO_INICIO_TITLE.search(val):
                    logger.debug("Encontrado bloque 'Horario de Inicio' en fila %s", r)
                    return r
        logger.debug("No se encontró bloque 'Horario de Inicio'")
        return None

    # ----------------------------- parseo de filas -----------------------------

    def _parse_rows(
        self,
        df: pd.DataFrame,
        header_row: int,
        colmap: Dict[str, Optional[int]],
        timeline_cols: Dict[date, int],
        sec_rows: Dict[str, int],
        stop_at_row: int, # <-- PARÁMETRO NUEVO
    ) -> Tuple[
        Dict[int, Dict[str, Any]],  # plan_dict
        Dict[int, Dict[str, Any]],  # cnc_dict
        Dict[int, Dict[str, Any]],  # semanal_dict
        Dict[int, Dict[str, Any]],  # inicio_dict
    ]:

        start_data = header_row + 1
        # --- CAMBIO: Usar stop_at_row ---
        nrows = min(len(df), stop_at_row) 
        consecutive_blank = 0

        r_real = sec_rows.get("realizadas_title")
        r_no_real = sec_rows.get("no_realizadas_title")

        def section_flag(r: int) -> Optional[bool]:
            if r_real is not None and r_no_real is not None:
                if r > r_real and r < r_no_real:
                    return True
                if r > r_no_real:
                    return False
                return None
            if r_real is not None and r > r_real:
                return True
            if r_no_real is not None and r > r_no_real:
                return False
            return None

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

        plan_dict: Dict[int, Dict[str, Any]] = {}
        cnc_dict: Dict[int, Dict[str, Any]] = {}
        semanal_dict: Dict[int, Dict[str, Any]] = {}
        inicio_dict: Dict[int, Dict[str, Any]] = {}

        for r in range(start_data, nrows):
            if row_is_blank(r):
                consecutive_blank += 1
                if consecutive_blank >= 3:
                    break
                continue
            consecutive_blank = 0

            flag = section_flag(r)

            # (El resto de esta función no tiene cambios)
            
            nro_pod = _safe_str(df, r, colmap.get("nro_pod"))
            tipo_tc = _safe_str(df, r, colmap.get("tipo_tc"))
            fecha = _parse_date_cell(_safe_str(df, r, colmap.get("fecha")))
            resp = _safe_str(df, r, colmap.get("responsable"))
            id_p6 = _safe_str(df, r, colmap.get("id_p6"))
            area = _safe_str(df, r, colmap.get("area_trabajo"))
            desc = _safe_str(df, r, colmap.get("descripcion_item"))
            unidad = _safe_str(df, r, colmap.get("unidad"))
            cant_prog = _safe_float(df, r, colmap.get("cant_programada"))
            cant_real = _safe_float(df, r, colmap.get("cant_real"))
            cumplimiento = _safe_float(df, r, colmap.get("cumplimiento"))

            cnc_causa = _safe_str(df, r, colmap.get("cnc_causa"))
            cnc_sub = _safe_str(df, r, colmap.get("cnc_subcausa"))
            cnc_tipo = _safe_str(df, r, colmap.get("cnc_tipo"))
            cnc_desc = _safe_str(df, r, colmap.get("cnc_descripcion"))
            cnc_resp = _safe_str(df, r, colmap.get("cnc_responsable"))

            sem_prog = _safe_float(df, r, colmap.get("sem_cant_prog"))
            sem_real = _safe_float(df, r, colmap.get("sem_cant_real"))
            sem_avance = _safe_float(df, r, colmap.get("%_avance"))
            if sem_avance is None and sem_prog is not None and sem_prog > 0 and sem_real is not None:
                sem_avance = sem_real / sem_prog
            fc_forecast = _safe_float(df, r, colmap.get("fc_cant_forecast"))
            fc_real = _safe_float(df, r, colmap.get("fc_cant_real"))
            fc_avance = _safe_float(df, r, colmap.get("fc_%_avance"))
            if fc_avance is None and fc_forecast is not None and fc_forecast > 0 and fc_real is not None:
                fc_avance = fc_real / fc_forecast

            timeline_vals: Dict[str, Optional[float]] = {}
            if timeline_cols:
                for d, c in timeline_cols.items():
                    timeline_vals[d.isoformat()] = _safe_float(df, r, c)

            if _is_phantom_row(
                desc, area,
                cant_prog, cant_real, cumplimiento,
                sem_prog, sem_real, sem_avance,
                fc_forecast, fc_real, fc_avance,
                timeline_vals,
            ):
                continue

            if flag is not None:
                plan_row = {
                    "realizada": bool(flag),
                    "fecha": fecha,
                    "responsable": resp,
                    "id_p6": id_p6,
                    "nro_pod": nro_pod,
                    "tipo_tc": tipo_tc,
                    "area_trabajo": area,
                    "descripcion_item": desc,
                    "unidad": unidad,
                    "cant_programada": cant_prog,
                    "cant_real": cant_real,
                    "cumplimiento": cumplimiento,
                }
                plan_dict[r] = plan_row

            cnc_row = {
                "realizada": (flag is True),
                "fecha": fecha,
                "id_p6": id_p6,
                "descripcion_item": desc,
                "cnc_causa": None if _empty_text(cnc_causa) else cnc_causa,
                "cnc_subcausa": None if _empty_text(cnc_sub) else cnc_sub,
                "cnc_tipo": None if _empty_text(cnc_tipo) else cnc_tipo,
                "cnc_descripcion": None if _empty_text(cnc_desc) else cnc_desc,
                "cnc_responsable": None if _empty_text(cnc_resp) else cnc_resp,
            }
            cnc_dict[r] = cnc_row

            semanal_row = {
                "realizada": (flag is True),
                "fecha": fecha,
                "id_p6": id_p6,
                "descripcion_item": desc,
                "semanal": {
                    "cant_prog": sem_prog,
                    "cant_real": sem_real,
                    "avance": sem_avance,
                },
                "acumulado_forecast": {
                    "cant_forecast": fc_forecast,
                    "cant_real": fc_real,
                    "avance": fc_avance,
                },
            }
            semanal_dict[r] = semanal_row

            inicio_row = {
                "realizada": (flag is True),
                "id_p6": id_p6,
                "descripcion_item": desc,
                "area_trabajo": area,
                "timeline": timeline_vals,
            }
            inicio_dict[r] = inicio_row

        return plan_dict, cnc_dict, semanal_dict, inicio_dict
    
    # --- NUEVA FUNCIÓN ---
    def _parse_horario_inicio(self, df: pd.DataFrame, start_row: Optional[int]) -> List[Dict[str, Any]]:
        """
        Parsea el bloque 'HORARIO DE INICIO DE TRABAJOS'
        que tiene Frentes en una fila y Horas en la de abajo.
        """
        if start_row is None or start_row >= len(df) - 1:
            return []

        frente_row_idx = None
        # Buscar la fila con "FRENTE"
        for r in range(start_row, min(start_row + 4, len(df))):
            row_str = " ".join([_norm(c) for c in df.iloc[r, :min(df.shape[1], 10)]])
            if "frente" in row_str or "horario de inicio efectivo" in row_str:
                frente_row_idx = r
                break
        
        # Fallback: tomar la primera fila no vacía debajo del título
        if frente_row_idx is None:
            for r in range(start_row + 1, min(start_row + 4, len(df))):
                non_empty = [c for c in df.iloc[r] if _norm(c) not in ("", "nan")]
                if len(non_empty) > 1:
                    frente_row_idx = r
                    break
        
        if frente_row_idx is None or frente_row_idx + 1 >= len(df):
            return [] # No se encontró fila de frentes o de horas

        hora_row_idx = frente_row_idx + 1
        
        frentes = []
        for c in range(df.shape[1]):
            frente_val = _safe_str(df, frente_row_idx, c)
            hora_val = _safe_str(df, hora_row_idx, c)
            
            # Parsear string de hora 'HH:MM'
            hora_parsed = None
            if hora_val:
                m = re.match(r"^(\d{1,2}):(\d{2})", hora_val.strip())
                if m:
                    hh, mm = int(m.group(1)), int(m.group(2))
                    if 0 <= hh <= 23 and 0 <= mm <= 59:
                        hora_parsed = f"{hh:02d}:{mm:02d}"
            
            # Evitar headers ("FRENTE", "HORARIO...")
            if _norm(frente_val) in ("frente", "horario de inicio efectivo"):
                continue

            # Solo agregar si al menos uno tiene valor
            if frente_val or hora_parsed:
                frentes.append({
                    "frente": frente_val,
                    "hora_inicio": hora_parsed
                })
        
        logger.debug("Parseados %s frentes de 'Horario de Inicio'", len(frentes))
        return frentes