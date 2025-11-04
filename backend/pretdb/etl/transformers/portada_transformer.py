# pretdb/etl/transformers/portada_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional
import logging, re, unicodedata
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

# ------------------------- helpers -------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\s\W]+", " ", s.strip().lower())
    return s

DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"]

def _parse_date(s: str) -> Optional[datetime.date]:
    s = (s or "").strip()
    if not s:
        return None
    # intenta formatos comunes primero
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # y luego el parser de pandas (soporta "2025-02-22 00:00:00")
    try:
        # si viene yyyy-mm-dd..., forzamos dayfirst=False implícitamente
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
    # (8x6) o 8x6
    m = re.search(r"(\d+\s*[xX]\s*\d+)", txt)
    return m.group(1).lower().replace(" ", "") if m else txt.strip() or None

def _parse_str(s: Any) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip()
    return s or None

# ------------------------- patrones -------------------------

# Importante: jefaturas van ANTES que 'turno' para no confundir
KEY_PATTERNS: Dict[str, List[re.Pattern]] = {
    # POD y fechas
    "fecha": [re.compile(p, re.I) for p in [r"\bfecha\b", r"\bfecha\s*pod\b"]],
    # Ampliado para captar: "POD N°", "POD Nº", "POD No", "POD Numero", "Número POD", etc.
    "pod_numero": [re.compile(p, re.I) for p in [
        r"\bnumero\s*pod\b",
        r"\bn[ºo]\s*pod\b",          # N° POD / Nº POD / No POD
        r"\bpod\s*n[ºo]\b",          # POD N° / POD Nº / POD No
        r"\bpod\s*#\b",
        r"\bpod\s*(n|no|numero)\b",
    ]],
    "inicio_periodo_pod": [re.compile(p, re.I) for p in [
        r"inicio\s*periodo\s*(del|de)?\s*pod", r"inicio\s*periodo"
    ]],
    "termino_periodo_pod": [re.compile(p, re.I) for p in [
        r"t[ée]rmino\s*periodo\s*(del|de)?\s*pod", r"termino\s*periodo"
    ]],

    # Jefaturas (variantes)
    "jefe_turno_codelco": [re.compile(p, re.I) for p in [
        r"jefe\s*de\s*turno\s*codelco", r"\bcodelco\b.*\bjefe\s*de\s*turno\b", r"\bjefe\s*turno\b.*\bcodelco\b"
    ]],
    "jefe_turno_fe_grande": [re.compile(p, re.I) for p in [
        r"jefe\s*de\s*turno\s*(fe\s*grande|contratista|empresa)",
        r"(fe\s*grande|contratista|empresa).*\bjefe\s*de\s*turno\b"
    ]],

    # Turno “puro”
    "turno": [re.compile(r"^turno$", re.I)],  # requiere coincidencia exacta

    # Otros
    "contrato": [re.compile(p, re.I) for p in [r"\bcontrato\b", r"\bcodigo\s*contrato\b"]],
    "empresa":  [re.compile(p, re.I) for p in [r"\bempresa\b", r"\bcontratista\b", r"\brazon\s*social\b"]],
    "obra":     [re.compile(p, re.I) for p in [r"\bobra\b", r"\bactividad\b", r"\bfrente\b", r"\bproyecto\b"]],
    "supervisor": [re.compile(p, re.I) for p in [r"\bsupervisor\b", r"\bjefe\s*de\s*terreno\b", r"\bresponsable\b"]],
    "jornada":  [re.compile(p, re.I) for p in [r"\bjornada\b", r"\bhorario\b"]],
    "clima":    [re.compile(p, re.I) for p in [r"\bclima\b", r"\bmeteorolog", r"\bcondiciones\b"]],
}

def _match_key(label: str) -> Optional[str]:
    """Prioriza jefaturas y exige 'turno' exacto para la clave turno."""
    lab = _norm(label)
    if re.search(r"jefe\s*de\s*turno\s*codelco", lab, re.I):
        return "jefe_turno_codelco"
    if re.search(r"jefe\s*de\s*turno\s*(fe\s*grande|contratista|empresa)", lab, re.I):
        return "jefe_turno_fe_grande"
    if re.fullmatch(r"turno", lab):  # evita confundir con 'jefe de turno ...'
        return "turno"
    for key, pats in KEY_PATTERNS.items():
        if any(p.search(lab) for p in pats):
            return key
    return None

# ------------------------- transformer -------------------------

class PortadaTransformer:
    """
    Transformer para la hoja 'portada'.
    Recibe df_raw (tal como lo entrega read_all_sheets_keyed(header=None))
    y devuelve un dict limpio con campos canónicos.
    """
    sheet_key = "portada"

    def run_df(self, df_raw: pd.DataFrame, meta: dict, dry_run: bool = False, **kwargs) -> dict:
        logger.debug("PortadaTransformer.run_df: df_raw shape=%s dry_run=%s", df_raw.shape, dry_run)
        kv = self._extract_key_values(df_raw)
        # Fallbacks inteligentes si algo faltó
        kv = self._fill_fallbacks(df_raw, kv)

        record = self._to_record(kv)
        record.update({
            "file_name": meta.get("file_name"),
            "file_hash": meta.get("file_hash"),
            "raw_sheet_name": meta.get("raw_sheet_name"),
        })
        logger.debug("PortadaTransformer.record: %s", record)
        # if not dry_run: self.upsert(record)
        return {"sheet": self.sheet_key, "record": record, "dry_run": dry_run}

    # ------------------------------------------------------------

    def _extract_key_values(self, df: pd.DataFrame) -> Dict[str, str]:
        """
        Heurísticas robustas en Portada:
        - A) pares contiguos [etiqueta | valor]
        - B) "Etiqueta: Valor" en la misma celda
        - C) pares en bloques A:B y C:D
        - D) búsqueda derecha/abajo (salta blanks y merges)
        """
        kv: Dict[str, str] = {}
        nrows = min(50, len(df))
        ncols = min(12, df.shape[1])

        clean = (
            df.iloc[:nrows, :ncols]
              .astype("string")
              .fillna("")
              .replace("<NA>", "", regex=False)
        )

        # A) dos columnas contiguas
        for r in range(nrows):
            for c in range(ncols - 1):
                label = str(clean.iat[r, c]).strip()
                value = str(clean.iat[r, c + 1]).strip()
                key = _match_key(label)
                if key and value:
                    kv.setdefault(key, value)

        # B) misma celda con separador :
        for r in range(nrows):
            for c in range(ncols):
                cell = str(clean.iat[r, c]).strip()
                if ":" in cell:
                    parts = [p.strip() for p in cell.split(":", 1)]
                    if len(parts) == 2:
                        key = _match_key(parts[0])
                        if key and parts[1]:
                            kv.setdefault(key, parts[1])

        # C) pares en bloques A:B y C:D
        for r in range(nrows):
            if ncols >= 2:
                a, b = str(clean.iat[r, 0]).strip(), str(clean.iat[r, 1]).strip()
                key = _match_key(a)
                if key and b:
                    kv.setdefault(key, b)
            if ncols >= 4:
                c, d = str(clean.iat[r, 2]).strip(), str(clean.iat[r, 3]).strip()
                key = _match_key(c)
                if key and d:
                    kv.setdefault(key, d)

        # D) búsqueda derecha/abajo (salta blanks y merges)
        WINDOW_RIGHT = 5
        WINDOW_DOWN = 2
        for r in range(nrows):
            for c in range(ncols):
                label = str(clean.iat[r, c]).strip()
                key = _match_key(label)
                if not key or key in kv:
                    continue

                # derecha
                val = None
                for c2 in range(c + 1, min(ncols, c + 1 + WINDOW_RIGHT)):
                    cand = str(clean.iat[r, c2]).strip()
                    if cand:
                        val = cand
                        break

                # abajo
                if not val:
                    for r2 in range(r + 1, min(nrows, r + 1 + WINDOW_DOWN)):
                        cand = str(clean.iat[r2, c]).strip()
                        if cand:
                            val = cand
                            break

                if val:
                    logger.debug("Portada: etiqueta '%s' -> valor '%s' (r=%s,c=%s)", label, val, r, c)
                    kv.setdefault(key, val)

        logger.debug("PortadaTransformer._extract_key_values: kv=%s", kv)
        return kv

    def _fill_fallbacks(self, df: pd.DataFrame, kv: Dict[str, str]) -> Dict[str, str]:
        """
        Fallbacks si faltó algo clave:
        - turno: busca patrón 'NxM' en toda la hoja
        - inicio/termino periodo: intenta hallar filas con 'inicio periodo'/'termino periodo' aunque el label no haya matcheado
        """
        nrows = min(50, len(df))
        ncols = min(12, df.shape[1])
        clean = (
            df.iloc[:nrows, :ncols]
              .astype("string")
              .fillna("")
              .replace("<NA>", "", regex=False)
        )

        # Turno: si no lo encontramos por etiqueta, busca 'NxM' en toda la hoja
        if "turno" not in kv:
            for r in range(nrows):
                for c in range(ncols):
                    txt = str(clean.iat[r, c]).strip()
                    m = re.search(r"(\d+\s*[xX]\s*\d+)", txt)
                    if m:
                        kv["turno"] = m.group(1)
                        logger.debug("Portada Fallback turno: '%s' (r=%s,c=%s)", kv["turno"], r, c)
                        break
                if "turno" in kv:
                    break

        # Inicio/Término periodo POD: si faltan, busca filas con palabras clave
        def _seek_right_down(r: int, c: int) -> Optional[str]:
            # derecha hasta +5
            for c2 in range(c + 1, min(ncols, c + 6)):
                cand = str(clean.iat[r, c2]).strip()
                if cand:
                    return cand
            # abajo hasta +2
            for r2 in range(r + 1, min(nrows, r + 3)):
                cand = str(clean.iat[r2, c]).strip()
                if cand:
                    return cand
            return None

        if "inicio_periodo_pod" not in kv:
            for r in range(nrows):
                for c in range(ncols):
                    lab = _norm(str(clean.iat[r, c]))
                    if "inicio periodo" in lab and "pod" in lab:
                        val = _seek_right_down(r, c)
                        if val:
                            kv["inicio_periodo_pod"] = val
                            logger.debug("Portada Fallback inicio_periodo_pod: '%s' (r=%s,c=%s)", val, r, c)
                            break
                if "inicio_periodo_pod" in kv:
                    break

        if "termino_periodo_pod" not in kv:
            for r in range(nrows):
                for c in range(ncols):
                    lab = _norm(str(clean.iat[r, c]))
                    if ("termino periodo" in lab or "t rmino periodo" in lab) and "pod" in lab:
                        val = _seek_right_down(r, c)
                        if val:
                            kv["termino_periodo_pod"] = val
                            logger.debug("Portada Fallback termino_periodo_pod: '%s' (r=%s,c=%s)", val, r, c)
                            break
                if "termino_periodo_pod" in kv:
                    break

        return kv

    def _to_record(self, kv: Dict[str, str]) -> Dict[str, Any]:
        rec: Dict[str, Any] = {}
        if "pod_numero" in kv:          rec["pod_numero"] = _parse_pod_num(kv["pod_numero"])
        if "fecha" in kv:               rec["fecha_pod"] = _parse_date(kv["fecha"])
        if "inicio_periodo_pod" in kv:  rec["fecha_inicio_periodo_pod"] = _parse_date(kv["inicio_periodo_pod"])
        if "termino_periodo_pod" in kv: rec["fecha_termino_periodo_pod"] = _parse_date(kv["termino_periodo_pod"])

        if "jefe_turno_codelco" in kv:      rec["jefe_turno_codelco"] = _parse_str(kv["jefe_turno_codelco"])
        if "jefe_turno_fe_grande" in kv:    rec["jefe_turno_fe_grande"] = _parse_str(kv["jefe_turno_fe_grande"])
        if "turno" in kv:                   rec["turno"] = _parse_turno(kv["turno"])

        if "contrato" in kv:   rec["contrato"]   = _parse_str(kv["contrato"])
        if "empresa" in kv:    rec["empresa"]    = _parse_str(kv["empresa"])
        if "obra" in kv:       rec["obra"]       = _parse_str(kv["obra"])
        if "supervisor" in kv: rec["supervisor"] = _parse_str(kv["supervisor"])
        if "jornada" in kv:    rec["jornada"]    = _parse_str(kv["jornada"])
        if "clima" in kv:      rec["clima"]      = _parse_str(kv["clima"])

        return {k: v for k, v in rec.items() if v is not None}

    # ---- (Opcional) Persistencia en DB ----
    # def upsert(self, rec: Dict[str, Any]):
    #     from pretdb.models import Portada  # ajusta el nombre del modelo
    #     unique = dict(file_hash=rec.get("file_hash"))  # o (fecha_pod, contrato, pod_numero)
    #     obj, created = Portada.objects.update_or_create(defaults=rec, **unique)
    #     return obj.id
