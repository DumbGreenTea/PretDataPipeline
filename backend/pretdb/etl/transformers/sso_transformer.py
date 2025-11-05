# pretdb/etl/transformers/sso_transformer.py
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
from openpyxl import load_workbook

# ========================= COLORES ============================================

HEX_ALIASES = {
    "#00B050": "verde", "#92D050": "verde",
    "#FFFF00": "amarillo", "#FFC000": "amarillo",
    "#FF0000": "rojo",   "#C00000": "rojo",
}
HEX_ALIASES = {k.upper(): v for k, v in HEX_ALIASES.items()}
COLOR_CODE = {"verde": 1, "amarillo": 2, "rojo": 3}
NEGROS_NUMERO = {"#000000", "#000", "BLACK"}

# ========================= UTILS ==============================================

def _norm_txt(x: str) -> str:
    return x.replace("\n", " ").replace("\r", " ").strip()

def _to_int_or_none(v) -> Optional[int]:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # acepta "17", "17.0"
            if s.replace(".", "", 1).isdigit():
                return int(float(s))
            return None
        return int(v)
    except Exception:
        return None

def _hex_from_fill(fill) -> Optional[str]:
    if not fill:
        return None
    for attr in ("start_color", "fgColor"):
        obj = getattr(fill, attr, None)
        if obj is None:
            continue
        rgb = getattr(obj, "rgb", None)
        if not rgb:
            continue
        s = str(rgb).upper()
        if len(s) == 8:       # ARGB
            return f"#{s[2:]}"
        if len(s) == 6:       # RGB
            return f"#{s}"
    return None

def _sheet_colors_df(xlsx_path: str, sheet_name: str, shape: Tuple[int, int]) -> pd.DataFrame:
    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]
    rows, cols = shape
    out = [[None for _ in range(cols)] for _ in range(rows)]
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            out[r-1][c-1] = _hex_from_fill(ws.cell(row=r, column=c).fill)
    return pd.DataFrame(out)

def _map_hex_to_label(hexv: Optional[str]) -> Optional[str]:
    if not hexv:
        return None
    hv = hexv.upper()
    if hv in NEGROS_NUMERO:
        return "negro"
    return HEX_ALIASES.get(hv)

def _severity_pick(labels: List[str]) -> Optional[str]:
    best, score = None, -1
    for lb in labels:
        sc = COLOR_CODE.get(lb, -1)
        if sc > score:
            best, score = lb, sc
    return best

# ========================= CRUZ DE SEGURIDAD ==================================

@dataclass
class CrossDay:
    dia_mes: int
    fecha: Optional[pd.Timestamp]
    color_label: Optional[str]
    color_hex: Optional[str]
    estado_codigo: Optional[int]
    row_top: int
    col_right: int

def _infer_month_ref_from_df(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    for r in range(df.shape[0]):
        for c in range(df.shape[1]):
            dt = pd.to_datetime(df.iat[r, c], dayfirst=True, errors="coerce")
            if pd.notna(dt):
                return dt
    return None

def _extract_cross_days(df_vals: pd.DataFrame, df_colors: pd.DataFrame,
                        month_ref: Optional[pd.Timestamp]) -> List[CrossDay]:
    rows, cols = df_vals.shape
    candidates: List[CrossDay] = []

    for r in range(rows):
        for c in range(cols):
            dia = _to_int_or_none(df_vals.iat[r, c])
            if dia is None or dia < 1 or dia > 31:
                continue

            neigh = []
            for (rr, cc) in [(r, c-1), (r+1, c-1), (r+1, c)]:
                if 0 <= rr < rows and 0 <= cc < cols:
                    neigh.append((rr, cc))

            labels, label_hex = [], []
            for (rr, cc) in neigh:
                hx = df_colors.iat[rr, cc]
                lb = _map_hex_to_label(hx)
                if lb and lb != "negro":
                    labels.append(lb)
                    label_hex.append((lb, hx))

            final_label = _severity_pick(labels)
            final_hex = None
            if final_label:
                for (lb, hx) in label_hex:
                    if lb == final_label:
                        final_hex = hx
                        break

            fecha = None
            if isinstance(month_ref, pd.Timestamp):
                try:
                    fecha = pd.Timestamp(month_ref.year, month_ref.month, dia)
                except Exception:
                    fecha = None

            candidates.append(
                CrossDay(
                    dia_mes=dia, fecha=fecha,
                    color_label=final_label, color_hex=final_hex,
                    estado_codigo=COLOR_CODE.get(final_label),
                    row_top=r, col_right=c
                )
            )

    # Dedupe por día con prioridad rojo>amarillo>verde
    unique: Dict[int, CrossDay] = {}
    for cd in candidates:
        prev = unique.get(cd.dia_mes)
        if prev is None:
            unique[cd.dia_mes] = cd
        else:
            prev_sc = prev.estado_codigo or -1
            cur_sc  = cd.estado_codigo or -1
            if cur_sc > prev_sc:
                unique[cd.dia_mes] = cd

    return list(sorted(unique.values(), key=lambda x: x.dia_mes))

# ========================= EVENTOS / REFLEXIÓN ================================

def _extract_events(df: pd.DataFrame) -> List[Dict]:
    df_norm = df.apply(lambda col: col.map(lambda x: _norm_txt(x) if isinstance(x, str) else x))
    header_row = None
    for r in range(df_norm.shape[0]):
        row_vals = [str(v).lower() for v in df_norm.iloc[r].tolist()]
        joined = " | ".join(row_vals)
        if (("descripción" in joined) or ("descripcion" in joined)) and ("fecha" in joined):
            header_row = r
            break
    if header_row is None:
        return []

    header = [str(v).lower() if isinstance(v, str) else "" for v in df_norm.iloc[header_row].tolist()]
    idx = {}
    for i, name in enumerate(header):
        if "descripción" in name or "descripcion" in name:
            idx["descripcion"] = i
        elif "clasific" in name:
            idx["clasificacion"] = i
        elif "fecha compromiso" in name or ("compromiso" in name and "fecha" in name):
            idx["fecha_compromiso"] = i
        elif name.strip() == "fecha" or name.startswith("fecha"):
            idx["fecha"] = i
        elif "estado" in name:
            idx["estado"] = i

    out = []
    r = header_row + 1
    while r < df.shape[0]:
        row = df.iloc[r]
        if row.isna().all():
            break

        def pick(key):
            j = idx.get(key);  return None if j is None else row.iloc[j]

        fecha = pd.to_datetime(pick("fecha"), dayfirst=True, errors="coerce") if pick("fecha") is not None else None
        fecha_comp = pd.to_datetime(pick("fecha_compromiso"), dayfirst=True, errors="coerce") if pick("fecha_compromiso") is not None else None

        ev = {
            "fecha": fecha,
            "descripcion": pick("descripcion"),
            "clasificacion": pick("clasificacion"),
            "fecha_compromiso": fecha_comp,
            "estado": pick("estado"),
        }
        if any(v not in (None, "") for v in ev.values()):
            out.append(ev)
        r += 1
    return out

def _extract_reflexion(df: pd.DataFrame) -> List[Dict]:
    df_str = df.apply(lambda col: col.map(lambda x: x if isinstance(x, str) else None))
    rows, cols = df_str.shape
    head = None
    for r in range(rows):
        for c in range(cols):
            v = df_str.iat[r, c]
            if v and "reflex" in v.lower():
                head = r; break
        if head is not None:
            break
    if head is None:
        return []

    fecha = None
    for r in range(head, min(head + 5, rows)):
        for c in range(cols):
            dt = pd.to_datetime(df_str.iat[r, c], dayfirst=True, errors="coerce")
            if pd.notna(dt):
                fecha = dt; break
        if fecha is not None:
            break

    textos = []
    for r in range(head + 1, min(head + 12, rows)):
        row_vals = [x for x in df_str.iloc[r].tolist() if isinstance(x, str)]
        line = " ".join(s.strip() for s in row_vals if s and s.strip())
        if line:
            textos.append(line)
    body = "\n".join(textos).strip() or None
    if not body:
        return []
    return [{"fecha": fecha, "descripcion": body}]

# ========================= TRANSFORMER ========================================

class SSOTransformer:
    """
    Orquestador:
        tr = SSOTransformer()
        payload = tr.transform(df_sso, ctx={"excel_path": path, "raw_sheet_name": "3.SSO", "month_ref": None})
    También puedes usar: tr.run(df, excel_path=..., raw_sheet_name=..., month_ref=None)
    """
    sheet = "sso"

    def __init__(self):
        pass  # sin argumentos para que el registry pueda instanciar

    # Alias cómodo si tu orquestador usa 'run'
    def run(self, df_sso: pd.DataFrame, *, excel_path: Optional[str] = None,
            raw_sheet_name: Optional[str] = None, month_ref: Optional[pd.Timestamp] = None) -> Dict:
        return self.transform(df_sso, excel_path=excel_path, raw_sheet_name=raw_sheet_name, month_ref=month_ref)

    def transform(self, df: pd.DataFrame, ctx: Optional[dict] = None, **kwargs) -> Dict:
        # ---------- Contexto ----------
        ctx = ctx or {}
        excel_path: Optional[str] = kwargs.get("excel_path", ctx.get("excel_path"))
        raw_sheet_name: Optional[str] = kwargs.get("raw_sheet_name", ctx.get("raw_sheet_name"))
        month_ref: Optional[pd.Timestamp] = kwargs.get("month_ref", ctx.get("month_ref"))

        # Normaliza strings (sin applymap deprecado)
        df_norm = df.apply(lambda col: col.map(lambda x: _norm_txt(x) if isinstance(x, str) else x))

        # Mes de referencia
        month_ref = month_ref or _infer_month_ref_from_df(df_norm)

        # Colores hoja (si no tenemos excel_path/sheet, devolvemos cruz vacía)
        cross_days: List[CrossDay] = []
        if excel_path and raw_sheet_name:
            df_colors = _sheet_colors_df(excel_path, raw_sheet_name, df.shape)
            cross_days = _extract_cross_days(df, df_colors, month_ref)

        # Eventos y Reflexión
        events = _extract_events(df_norm)
        reflexion = _extract_reflexion(df_norm)

        # Resumen colores (post-dedupe)
        by_color = {"verde": 0, "amarillo": 0, "rojo": 0}
        for cd in cross_days:
            if cd.color_label in by_color:
                by_color[cd.color_label] += 1

        summary = {
            "rows_events": len(events),
            "rows_reflexiones": len(reflexion),
            "color_augmented": bool(excel_path and raw_sheet_name),
            "cross_days_count": len(cross_days),
            "by_color": by_color,
            "sample_cross_days": [vars(x) for x in cross_days[:10]],
        }

        return {
            "sheet": self.sheet,
            "summary": summary,
            "events": events,
            "reflexion": reflexion,
            "cross_days": [vars(x) for x in cross_days],
            "meta": {"file_name": excel_path, "raw_sheet_name": raw_sheet_name},
        }

    # ------------------ Persistencia opcional ---------------------------------
    def save(self, sso_obj, payload: Optional[Dict] = None, dry_run: bool = False) -> Dict:
        if payload is None:
            raise ValueError("Debes pasar el payload retornado por transform/run.")
        if dry_run:
            return {"cross": None, "eventos": {"created": 0, "updated": 0}, "reflexion": {"created": 0, "updated": 0}}

        from django.db import transaction
        from pretdb.models import SSO_cruz_seguridad, SSO_estado_dia, SSO_Evento, SSO_Reflexion

        cross = payload.get("cross_days", [])
        fechas = [row.get("fecha") for row in cross if row.get("fecha") is not None]
        fecha_inicio = min(fechas) if fechas else None
        fecha_fin = max(fechas) if fechas else None

        res = {"cross": None, "eventos": {"created": 0, "updated": 0}, "reflexion": {"created": 0, "updated": 0}}
        with transaction.atomic():
            # Cruz
            cruz, _ = SSO_cruz_seguridad.objects.get_or_create(
                sso=sso_obj,
                defaults={"fecha_inicio": fecha_inicio, "fecha_fin": fecha_fin},
            )
            changed = False
            if fecha_inicio and cruz.fecha_inicio != fecha_inicio:
                cruz.fecha_inicio = fecha_inicio; changed = True
            if fecha_fin and cruz.fecha_fin != fecha_fin:
                cruz.fecha_fin = fecha_fin; changed = True
            if changed:
                cruz.save(update_fields=["fecha_inicio", "fecha_fin"])

            created = updated = 0
            for row in cross:
                obj, was_created = SSO_estado_dia.objects.update_or_create(
                    sso_cruz_seguridad=cruz,
                    dia_mes=int(row["dia_mes"]),
                    defaults={
                        "fecha": row.get("fecha"),
                        "estado_dia": row.get("estado_codigo"),
                        "color_label": row.get("color_label"),
                        "color_hex": row.get("color_hex"),
                    },
                )
                created += 1 if was_created else 0
                updated += 0 if was_created else 1
            res["cross"] = {"created": created, "updated": updated, "cruz_id": cruz.pk,
                            "fecha_inicio": fecha_inicio, "fecha_fin": fecha_fin}

            # Eventos
            ev_c = ev_u = 0
            for ev in payload.get("events", []):
                obj, created_flag = SSO_Evento.objects.update_or_create(
                    sso=sso_obj,
                    fecha=ev.get("fecha"),
                    descripcion_dia=ev.get("descripcion"),
                    defaults={
                        "clasificacion": ev.get("clasificacion"),
                        "fecha_compromiso": ev.get("fecha_compromiso"),
                        "estado": ev.get("estado"),
                    },
                )
                ev_c += 1 if created_flag else 0
                ev_u += 0 if created_flag else 1
            res["eventos"] = {"created": ev_c, "updated": ev_u}

            # Reflexión
            rx_c = rx_u = 0
            for rx in payload.get("reflexion", []):
                obj, created_flag = SSO_Reflexion.objects.update_or_create(
                    sso=sso_obj,
                    fecha=rx.get("fecha"),
                    hallazgo=rx.get("descripcion"),
                    defaults={},
                )
                rx_c += 1 if created_flag else 0
                rx_u += 0 if created_flag else 1
            res["reflexion"] = {"created": rx_c, "updated": rx_u}

        return res