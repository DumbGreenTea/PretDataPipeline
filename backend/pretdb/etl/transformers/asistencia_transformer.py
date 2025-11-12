# pretdb/etl/transformers/asistencia_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging, re, unicodedata
from datetime import datetime, date, timedelta
import pandas as pd

from django.db import transaction, connection
from pretdb.models import Pod, Trabajador, Asistencia, AsistenciaDetalle, Contrato

logger = logging.getLogger(__name__)

# ------------------------------- utilidades -------------------------------

def _norm(s: str) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios."""
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\s\W]+", " ", s.strip().lower())
    return s

def _parse_date_cell(x: Any) -> Optional[date]:
    """Convierte celdas a date sin warnings molestos."""
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x
    s = (str(x) if x is not None else "").strip()
    if not s or s.lower() in ("na", "nan", "<na>"):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):  # ISO
        dt = pd.to_datetime(s, errors="coerce", dayfirst=False)
        return None if pd.isna(dt) else dt.date()
    dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if pd.isna(dt) else dt.date()

def _to_hh(val: Any) -> Optional[float]:
    """
    Normaliza valor a horas:
    - números -> float
    - iconos/OK -> 1.0; X/ausente -> 0.0
    - vacío -> None
    """
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    s = str(val).strip()
    if s == "" or s.lower() in ("na", "nan", "<na>"):
        return None
    # intentar número embebido
    m = re.findall(r"[-+]?\d*\.?\d+", s)
    if m:
        try:
            return float(m[0])
        except Exception:
            pass
    ns = _norm(s)
    if any(tok in ns for tok in ("ok","si","presente","present","✔","✅","🟢","🟩")):
        return 1.0
    if any(tok in ns for tok in ("x","no","ausente","✖","❌","🔴","🟥")):
        return 0.0
    return None

def _initials_from_name(name: str) -> Optional[str]:
    """
    Genera código a partir del nombre (p.ej., 'Nicolas Amin' -> 'NA').
    Si solo hay una palabra, toma sus dos primeras letras.
    Limita a 2–3 chars mayúsculas.
    """
    if not name:
        return None
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) >= 2:
        code = (parts[0][0] + parts[1][0]).upper()
    else:
        code = parts[0][:2].upper()
    return code or None

def _digits_only(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.findall(r"\d+", s)
    return "".join(m) if m else None

def _split_nombre_contrato(nombre: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    'CC-006-CONSTRUCCION ...' -> ('CC-006','CONSTRUCCION ...')
    """
    if not nombre:
        return None, None
    txt = str(nombre).strip()
    parts = txt.split("-", 1)
    if len(parts) == 2:
        code = parts[0].strip() or None
        tail = parts[1].strip() or None
        return code, tail
    m = re.match(r"^([A-Za-z]{2,3}-?\d{2,})\s+(.*)$", txt)
    if m:
        return m.group(1), (m.group(2) or None)
    return None, txt

# --------------------------- patrones de encabezado ---------------------------

DAY_PATTERNS: Dict[str, re.Pattern] = {
    "lunes": re.compile(r"\blunes\b", re.I),
    "martes": re.compile(r"\bmartes\b", re.I),
    "miercoles": re.compile(r"\bmi[eé]rcoles\b", re.I),   # acepta tilde o no
    "jueves": re.compile(r"\bjueves\b", re.I),
    "viernes": re.compile(r"\bviernes\b", re.I),
    "sabado": re.compile(r"\bs[áa]bado\b", re.I),
}

PERSON_KEYS = {
    "codigo": re.compile(r"\b(c[oó]d(igo)?)\b", re.I),
    "nombre": re.compile(r"\bnombre\b", re.I),
    "cargo": re.compile(r"\bcargo\b", re.I),
    "empresa": re.compile(r"\bempresa\b", re.I),
    "correo": re.compile(r"\bcorre(o|e)\b|\bemail\b", re.I),
}

# Filas de resumen a excluir (pueden venir en nombre o en código)
SUMMARY_ROW_FLAGS = (
    "asistencia fegrande",
    "asistencia vp",
    "asietencia general",  # algunos POD lo traen con esa falta
    "asistencia general",
    "asiste",
    "reemplazo",
    "no asiste",
)

# Header contrato en hoja asistencia
CONTRATO_PAT = re.compile(r"Contrato:\s*([0-9]{6,})", re.I)
NOMBRE_PAT = re.compile(r"Nombre:\s*(.+)$", re.I)

# ------------------------------ clase principal ------------------------------

class AsistenciaTransformer:
    """Extrae detalle de asistencia por persona y por día desde '2. Asistencia' y persiste en modelos."""
    sheet_key = "asistencia"

    # -------------------------- API p/orquestador --------------------------

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("AsistenciaTransformer.run_df: shape=%s", df_raw.shape)

        df = df_raw.copy()

        # (Opcional) Leer contrato/nombre desde encabezado de la hoja Asistencia
        contrato_num, contrato_nombre = self._parse_contrato_header(df)

        header_row, colmap_person, colmap_days = self._find_header_and_columns(df)
        if header_row is None:
            raise ValueError("No se pudo detectar encabezado en 'Asistencia'.")

        # fila de fechas = header_row + 1 (si hay desfaces, se cubrirá con fallback en persist)
        date_row_idx = header_row + 1 if header_row + 1 < len(df) else None
        dates = self._extract_dates(df, date_row_idx, colmap_days) if date_row_idx is not None else {}

        start_idx = header_row + 2
        persons = self._extract_person_rows(df, start_idx, colmap_person, colmap_days)

        # Tabla ancha por persona
        persons_detail = []
        for p in persons:
            # Fallback de código si falta
            codigo = p.get("codigo")
            if not codigo or str(codigo).strip() == "" or str(codigo).upper() in ("<NA>", "NA", "NAN"):
                guess = _initials_from_name(p.get("nombre") or "")
                codigo = guess or codigo

            row_out = {
                "codigo": codigo,
                "nombre": p.get("nombre"),
                "cargo": p.get("cargo"),
                "empresa": p.get("empresa"),
                "correo": p.get("correo"),
            }
            for dkey in colmap_days.keys():
                hh = p["days"].get(dkey)
                # Para lectura simple, None -> 0.0 (pero en persistencia distinguimos None)
                row_out[dkey] = 0.0 if hh is None else hh
            persons_detail.append(row_out)

        # Métricas (solo informativas del preview)
        presentes_por_dia: Dict[str, int] = {}
        ausentes_por_dia: Dict[str, int] = {}
        for dkey in colmap_days.keys():
            vals = [r[dkey] for r in persons_detail]
            pres = sum(1 for v in vals if v is not None and v > 0)
            aus = sum(1 for v in vals if v is not None and v == 0)
            presentes_por_dia[dkey] = pres
            ausentes_por_dia[dkey] = aus

        n_personas = len(persons_detail)
        n_dias = len(colmap_days)
        celdas_esperadas = n_personas * n_dias
        celdas_capturadas = sum(1 for r in persons_detail for d in colmap_days.keys() if r.get(d) is not None)
        celdas_faltantes = max(0, celdas_esperadas - celdas_capturadas)

        summary = {
            "rows_detail": n_personas * n_dias,
            "n_personas": n_personas,
            "person_columns": list(colmap_person.keys()),
            "day_columns": list(colmap_days.keys()),
            "dates": {k: (dates[k].isoformat() if dates.get(k) else None) for k in colmap_days.keys()},
            "presentes_por_dia": presentes_por_dia,
            "ausentes_por_dia": ausentes_por_dia,
            "cobertura": {
                "celdas_esperadas": celdas_esperadas,
                "celdas_capturadas": celdas_capturadas,
                "celdas_faltantes": celdas_faltantes,
            },
        }

        return {
            "sheet": self.sheet_key,
            "rows": n_personas * n_dias,
            "summary": summary,
            "persons_detail": persons_detail,
            "dates_map": dates,  # <- date objects por día
            "contrato": {"numero": contrato_num, "nombre": contrato_nombre},  # opcional
            "dry_run": dry_run,
        }

    def persist(self, result: dict, run_id: int) -> Tuple[int, Dict, Optional[Dict]]:
            """Shim persist: resolve Pod from run_id and delegate to AsistenciaLoader.persist

            Returns the same tuple (rows_upserted, warnings, errors) as before.
            """
            try:
                from pretdb.etl.loaders.asistencia_loader import AsistenciaLoader as _Loader
            except Exception as e:
                logger.warning("AsistenciaLoader shim: no se pudo importar loader: %s", e)
                return 0, {"import_error": str(e)}, None

            pod = self._get_pod_obj(run_id)
            if pod is None:
                return 0, {"pod_id": "etl_run.pod_id es NULL; ejecuta 'portada' antes."}, None

            loader = _Loader()
            dry = result.get("dry_run", False)
            try:
                return loader.persist(pod, result, dry_run=dry)
            except Exception as e:
                logger.exception("AsistenciaLoader shim: error delegating persist: %s", e)
                return 0, {"persist_error": str(e)}, None

    # --------------------------- helpers internos ---------------------------

    def _get_pod_obj(self, run_id: int) -> Optional[Pod]:
        with connection.cursor() as cur:
            cur.execute("SELECT pod_id FROM etl_run WHERE id=%s", [run_id])
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            return Pod.objects.get(id=row[0])
        except Pod.DoesNotExist:
            return None

    def _parse_contrato_header(self, df: pd.DataFrame, max_rows: int = 20, max_cols: int = 10) -> Tuple[Optional[str], Optional[str]]:
        num = None
        nombre = None
        sub = df.iloc[:max_rows, :max_cols].fillna("")
        for _, row in sub.iterrows():
            line = "  ".join([str(x) for x in row.tolist()])
            line = re.sub(r"\s+", " ", line).strip()
            if not num:
                m1 = CONTRATO_PAT.search(line)
                if m1: num = m1.group(1)
            if not nombre:
                m2 = NOMBRE_PAT.search(line)
                if m2: nombre = m2.group(1).strip()
            if num and nombre:
                break
            # por si vienen en celdas separadas
            for cell in row.tolist():
                s = str(cell)
                if not num:
                    m1c = CONTRATO_PAT.search(s)
                    if m1c: num = m1c.group(1)
                if not nombre:
                    m2c = NOMBRE_PAT.search(s)
                    if m2c: nombre = m2c.group(1).strip()
        return num, nombre

    def _find_header_and_columns(
        self, df: pd.DataFrame
    ) -> Tuple[Optional[int], Dict[str, int], Dict[str, int]]:
        """Detecta fila de encabezado y mapea columnas de persona y de días."""
        nrows = min(len(df), 60)
        ncols = min(df.shape[1], 32)

        header_row: Optional[int] = None
        best_person: Dict[str, int] = {}
        best_days: Dict[str, int] = {}

        for r in range(nrows):
            row_vals = [str(df.iat[r, c]) if c < ncols else "" for c in range(ncols)]

            # columnas de persona
            colmap_person: Dict[str, int] = {}
            for k, pat in PERSON_KEYS.items():
                for c, raw in enumerate(row_vals):
                    if pat.search(str(raw)) or _norm(str(raw)) == k:
                        colmap_person.setdefault(k, c)
                        break

            # columnas de días
            colmap_days: Dict[str, int] = {}
            for c, raw in enumerate(row_vals):
                s_raw = str(raw)
                s_norm = _norm(s_raw)
                for dkey in ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado"):
                    if dkey in colmap_days:
                        continue
                    if DAY_PATTERNS[dkey].search(s_raw) or s_norm.startswith(dkey):
                        colmap_days[dkey] = c

            if {"nombre", "cargo", "empresa"}.issubset(colmap_person.keys()) and len(colmap_days) >= 2:
                header_row = r
                best_person = colmap_person
                best_days = colmap_days
                break

        if header_row is None:
            return None, {}, {}

        logger.debug("Asistencia header at row %s | person_cols=%s | day_cols=%s",
                     header_row, best_person, best_days)
        return header_row, best_person, best_days

    def _extract_dates(self, df: pd.DataFrame, date_row_idx: int, colmap_days: Dict[str, int]) -> Dict[str, Optional[date]]:
        """Extrae fechas (fila inmediatamente bajo los encabezados de día)."""
        dates: Dict[str, Optional[date]] = {}
        for dkey, c in colmap_days.items():
            cell = df.iat[date_row_idx, c] if (0 <= date_row_idx < len(df) and 0 <= c < df.shape[1]) else None
            dates[dkey] = _parse_date_cell(cell)
        return dates

    def _extract_person_rows(
        self,
        df: pd.DataFrame,
        start_idx: int,
        colmap_person: Dict[str, int],
        colmap_days: Dict[str, int],
    ) -> List[Dict[str, Any]]:
        """
        Recorre filas de datos y arma registros por persona.
        Excluye filas vacías y filas de resumen (Asistencia Fegrande/VP/General, Asiste/Reemplazo/No asiste)
        aun si aparecen en la columna CODIGO.
        """
        persons: List[Dict[str, Any]] = []
        nrows = len(df)
        consecutive_blank = 0

        def is_summary_by_text(nombre: str, codigo: str) -> bool:
            nn = _norm(nombre)
            cc = _norm(codigo)
            return any(nn.startswith(f) for f in SUMMARY_ROW_FLAGS) or any(cc.startswith(f) for f in SUMMARY_ROW_FLAGS)

        def is_blank_row(idx: int) -> bool:
            try:
                nombre = df.iat[idx, colmap_person["nombre"]]
                cargo = df.iat[idx, colmap_person["cargo"]]
                empresa = df.iat[idx, colmap_person["empresa"]]
            except Exception:
                return True

            nombre_s = "" if pd.isna(nombre) else str(nombre).strip()
            cargo_s = "" if pd.isna(cargo) else str(cargo).strip()
            empresa_s = "" if pd.isna(empresa) else str(empresa).strip()

            # si todos los días están vacíos
            days_empty = True
            for dkey, c in colmap_days.items():
                val = df.iat[idx, c] if (0 <= idx < nrows and 0 <= c < df.shape[1]) else None
                if not pd.isna(val) and str(val).strip() != "":
                    days_empty = False
                    break

            basic_empty = (nombre_s == "" and cargo_s == "" and empresa_s == "")
            return basic_empty and days_empty

        for r in range(start_idx, nrows):
            # texto para revisión de resumen
            codigo_val = df.iat[r, colmap_person.get("codigo", 10**9)] if colmap_person.get("codigo") is not None else ""
            nombre_val = df.iat[r, colmap_person["nombre"]] if "nombre" in colmap_person else ""

            if is_summary_by_text(str(nombre_val), str(codigo_val)):
                continue

            if is_blank_row(r):
                consecutive_blank += 1
                if consecutive_blank >= 2:
                    break
                continue
            consecutive_blank = 0

            def safe_get(colname: str) -> Optional[str]:
                c = colmap_person.get(colname)
                if c is None or c >= df.shape[1]:
                    return None
                v = df.iat[r, c]
                return None if pd.isna(v) or str(v).strip() == "" else str(v).strip()

            person = {
                "codigo": safe_get("codigo"),
                "nombre": safe_get("nombre") or "",
                "cargo": safe_get("cargo"),
                "empresa": safe_get("empresa"),
                "correo": safe_get("correo"),
                "days": {},
            }

            # Excluir si (nombre/cargo/empresa) están en blanco y no hay días
            all_days_none = True
            for dkey, c in colmap_days.items():
                raw = df.iat[r, c] if (0 <= c < df.shape[1]) else None
                hh = _to_hh(raw)
                person["days"][dkey] = hh
                if hh is not None:
                    all_days_none = False

            if not person["nombre"] and not person["cargo"] and not person["empresa"] and all_days_none:
                continue

            persons.append(person)

        logger.debug("Asistencia: personas=%s", len(persons))
        return persons

# ----------------------- Wrapper compatible con orquestador -----------------------

def load_asistencia(df: pd.DataFrame, run_id: int, sheet: str):
    """
    Lo llama el orquestador. Transforma y luego persiste (INSERT/UPSERT).
    Retorna (rows_in, rows_upserted, warnings, errors)
    """
    tr = AsistenciaTransformer()
    result = tr.run_df(df, meta={"sheet": sheet}, dry_run=False)
    rows_in = result.get("rows", 0)
    try:
        rows_upserted, warnings, errors = tr.persist(result, run_id=run_id)
        return rows_in, rows_upserted, warnings, errors
    except Exception as e:
        logger.exception("Error en persistencia de asistencia: %s", e)
        return rows_in, 0, {}, {"persist": str(e)}
