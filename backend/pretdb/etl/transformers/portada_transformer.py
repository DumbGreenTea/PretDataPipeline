from __future__ import annotations
from typing import Dict, Any, List, Optional
import logging, re, unicodedata
from datetime import datetime, date
import pandas as pd

from pretdb.etl.transformers.registry import register_transformer

logger = logging.getLogger(__name__)

# ------------------------- helpers -------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\s\W]+", " ", s.strip().lower())
    return s

DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"]

def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return pd.to_datetime(s, dayfirst=False, errors="raise").date()
        return pd.to_datetime(s, dayfirst=True, errors="raise").date()
    except Exception:
        return None

def _parse_pod_num(s: str) -> Optional[int]:
    m = re.search(r"\d{1,6}", str(s))
    return int(m.group()) if m else None

def _parse_turno(s: Any) -> Optional[str]:
    if s is None:
        return None
    txt = str(s)
    m = re.search(r"(\d+\s*[xX]\s*\d+)", txt)
    return m.group(1).lower().replace(" ", "") if m else txt.strip() or None

def _parse_str(s: Any) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip()
    return s or None

# ------------------------- patrones -------------------------

KEY_PATTERNS: Dict[str, List[re.Pattern]] = {
    "fecha": [re.compile(p, re.I) for p in [r"\bfecha\b", r"\bfecha\s*pod\b"]],
    "pod_numero": [re.compile(p, re.I) for p in [
        r"\bnumero\s*pod\b", r"\bn[ºo]\s*pod\b", r"\bpod\s*n[ºo]\b", r"\bpod\s*#\b", r"\bpod\s*(n|no|numero)\b",
    ]],
    "inicio_periodo_pod": [re.compile(p, re.I) for p in [r"inicio\s*periodo\s*(del|de)?\s*pod", r"inicio\s*periodo"]],
    "termino_periodo_pod": [re.compile(p, re.I) for p in [r"t[ée]rmino\s*periodo\s*(del|de)?\s*pod", r"termino\s*periodo"]],
    "jefe_turno_codelco": [re.compile(p, re.I) for p in [
        r"jefe\s*de\s*turno\s*codelco", r"\bcodelco\b.*\bjefe\s*de\s*turno\b", r"\bjefe\s*turno\b.*\bcodelco\b"
    ]],
    "jefe_turno_contratista": [re.compile(p, re.I) for p in [
        r"jefe\s*de\s*turno\s*(fe\s*grande|contratista|empresa)",
        r"(fe\s*grande|contratista|empresa).*\bjefe\s*de\s*turno\b"
    ]],
    "turno": [re.compile(r"^turno$", re.I)],
}

def _match_key(label: str) -> Optional[str]:
    lab = _norm(label)
    if re.search(r"jefe\s*de\s*turno\s*codelco", lab, re.I):
        return "jefe_turno_codelco"
    if re.search(r"jefe\s*de\s*turno\s*(fe\s*grande|contratista|empresa)", lab, re.I):
        return "jefe_turno_contratista"
    if re.fullmatch(r"turno", lab):
        return "turno"
    for key, pats in KEY_PATTERNS.items():
        if any(p.search(lab) for p in pats):
            return key
    return None

# ------------------------- transformer -------------------------

@register_transformer("portada")
class PortadaTransformer:
    sheet_key = "portada"

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PortadaTransformer.run_df: shape=%s", df_raw.shape)
        kv = self._extract_key_values(df_raw)
        kv = self._fill_fallbacks(df_raw, kv)
        record = self._to_record(kv)

        flat_rows = [record] if record else []
        out = {
            "sheet": self.sheet_key,
            "rows": len(flat_rows),
            "dry_run": dry_run,
            "flat_rows": flat_rows,
            "by_tag": {"portada": flat_rows},
            "blocks": {"portada": flat_rows},
        }
        return out

    def _extract_key_values(self, df: pd.DataFrame) -> Dict[str, str]:
        kv: Dict[str, str] = {}
        nrows = min(50, len(df))
        ncols = min(12, df.shape[1])
        clean = df.iloc[:nrows, :ncols].astype("string").fillna("").replace("<NA>", "", regex=False)

        # [etiqueta | valor] contiguos
        for r in range(nrows):
            for c in range(ncols - 1):
                label = str(clean.iat[r, c]).strip()
                value = str(clean.iat[r, c + 1]).strip()
                key = _match_key(label)
                if key and value:
                    kv.setdefault(key, value)

        # "Etiqueta: Valor"
        for r in range(nrows):
            for c in range(ncols):
                cell = str(clean.iat[r, c]).strip()
                if ":" in cell:
                    a, b = [p.strip() for p in cell.split(":", 1)]
                    key = _match_key(a)
                    if key and b:
                        kv.setdefault(key, b)

        # bloques A:B y C:D
        for r in range(nrows):
            if ncols >= 2:
                a, b = str(clean.iat[r, 0]).strip(), str(clean.iat[r, 1]).strip()
                key = _match_key(a)
                if key and b:
                    kv.setdefault(key, b)
            if ncols >= 4:
                c_, d = str(clean.iat[r, 2]).strip(), str(clean.iat[r, 3]).strip()
                key = _match_key(c_)
                if key and d:
                    kv.setdefault(key, d)

        # búsqueda derecha/abajo (ventanas cortas)
        for r in range(nrows):
            for c in range(ncols):
                label = str(clean.iat[r, c]).strip()
                key = _match_key(label)
                if not key or key in kv:
                    continue
                val = None
                for c2 in range(c + 1, min(ncols, c + 6)):
                    cand = str(clean.iat[r, c2]).strip()
                    if cand:
                        val = cand; break
                if not val:
                    for r2 in range(r + 1, min(nrows, r + 3)):
                        cand = str(clean.iat[r2, c]).strip()
                        if cand:
                            val = cand; break
                if val:
                    kv.setdefault(key, val)
        return kv

    def _fill_fallbacks(self, df: pd.DataFrame, kv: Dict[str, str]) -> Dict[str, str]:
        nrows = min(50, len(df))
        ncols = min(12, df.shape[1])
        clean = df.iloc[:nrows, :ncols].astype("string").fillna("").replace("<NA>", "", regex=False)

        if "turno" not in kv:
            for r in range(nrows):
                for c in range(ncols):
                    txt = str(clean.iat[r, c]).strip()
                    m = re.search(r"(\d+\s*[xX]\s*\d+)", txt)
                    if m:
                        kv["turno"] = m.group(1); break
                if "turno" in kv: break
        return kv

    def _to_record(self, kv: Dict[str, str]) -> Dict[str, Any]:
        rec: Dict[str, Any] = {}
        # SOLO los 7 atributos requeridos
        if "pod_numero" in kv:          rec["pod_numero"] = _parse_pod_num(kv["pod_numero"])
        if "fecha" in kv:               rec["fecha"] = _parse_date(kv["fecha"])
        if "inicio_periodo_pod" in kv:  rec["inicio_periodo_pod"] = _parse_date(kv["inicio_periodo_pod"])
        if "termino_periodo_pod" in kv: rec["termino_periodo_pod"] = _parse_date(kv["termino_periodo_pod"])
        if "jefe_turno_codelco" in kv:  rec["jefe_turno_codelco"] = _parse_str(kv["jefe_turno_codelco"])
        if "jefe_turno_contratista" in kv: rec["jefe_turno_contratista"] = _parse_str(kv["jefe_turno_contratista"])
        if "turno" in kv:               rec["turno"] = _parse_turno(kv["turno"])
        
        return {k: v for k, v in rec.items() if v is not None}

# ---------- shims opcionales ----------
def persist_portada(out: dict, *, dry_run: bool = False, run_id: Optional[int] = None) -> dict:
    try:
        from pretdb.etl.loaders import create_loader
        loader = create_loader("portada")
        if not loader:
            return {"mode": "dry" if dry_run else "write", "skipped": len(out.get("flat_rows") or []),
                    "warning": "No loader registrado para 'portada'"}
        return loader.persist(
            pod=None,
            out=out,
            dry_run=dry_run,
            run_id=run_id,
        )
    except Exception as e:
        logger.exception("Error delegando persistencia portada: %s", e)
        return {"error": str(e), "mode": "dry" if dry_run else "write"}

def run_and_persist_portada(df_raw: pd.DataFrame, meta: dict, *, dry_run: bool = False, run_id: Optional[int] = None):
    tr = PortadaTransformer()
    out = tr.run_df(df_raw, meta=meta, dry_run=dry_run)
    stats = persist_portada(out, dry_run=dry_run, run_id=run_id)
    return {"sheet": tr.sheet_key, "rows": out["rows"], "dry_run": dry_run, "persist_result": stats}
