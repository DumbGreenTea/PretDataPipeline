# pretdb/etl/readers/excel_reader.py
"""
Lector de Excel para el pipeline POD (solo IO + descubrimiento de hojas).

Qué hace:
- Lee un XLSX desde ruta o bytes.
- Estandariza NOMBRES DE HOJAS (no columnas) a claves canónicas.
- Entrega un dict: { clave_normalizada: (nombre_original, DataFrame_crudo) }.

Qué NO hace:
- No determina header ni limpia columnas (eso lo hacen los transformers).
"""

from __future__ import annotations
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Any, Callable, List, Tuple
import logging
import unicodedata
import pandas as pd
import re
import time

logger = logging.getLogger(__name__)


# =========================
# Excepción
# =========================

class ExcelReadError(Exception):
    """Errores de lectura del Excel."""


# =========================
# Normalización de nombres (hojas/columnas)
# =========================

def normalize_name(s: str) -> str:
    """
    Normaliza un nombre genérico (útil para columnas si lo necesitas en transformers):
    - sin acentos, strip, minúsculas, espacios y '/' → '_',
    - quita símbolos raros, colapsa '__', quita '_' extremos.
    """
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower().replace(" ", "_").replace("/", "_")
    s = pd.Series([s]).str.replace(r"[^a-z0-9_]", "", regex=True).iloc[0]
    s = pd.Series([s]).str.replace(r"__+", "_", regex=True).str.strip("_").iloc[0]
    return s


# =========================
# Clave canónica de HOJAS (sin numeración ni tildes)
# =========================

_num_prefix = re.compile(r"^\s*\d+\s*[\.\-)_]*\s*")  # quita "2.", "10 -", etc.
_sep_collapse = re.compile(r"[\s\W]+")

def canonical_sheet_key(raw: str) -> str:
    """
    '2. Asistencia' -> 'asistencia'; '12. LayOut' -> 'layout'; 'Plan de Día' -> 'plan_de_dia'.
    """
    s = unicodedata.normalize("NFD", raw).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = _num_prefix.sub("", s)              # remueve prefijo numérico
    s = _sep_collapse.sub(" ", s).strip()   # colapsa separadores
    s = s.replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"__+", "_", s).strip("_")
    return s


# =========================
# Sinónimos/regex para mapear a claves fijas
# =========================

SHEET_PATTERNS: dict[str, list[str]] = {
    "portada": [
        r"\bportada\b",
        r"\bcover\b",
        r"\binicio\b",
    ],
    "asistencia": [
        r"\basist(?:encia)?\b",        # asistencia / asist
        r"\bhh\b",                     # a veces dejan "HH"
        r"control[\s\W]*asist",        # control asistencia
    ],
    "layout": [
        r"\blay[\s\W]*out\b",          # layout / lay-out / lay out
        r"\bdistribuci[oó]n\b",
    ],
    "sso": [
        r"\bsso\b",
        r"seguridad[\s\W]*salud[\s\W]*ocup",
    ],
    "ppc": [
        r"\bppc\b",
        r"plan[\s\W]*por[\s\W]*cumpl",  # plan percent complete
    ],
    "plan_dia": [
        r"\bplan[\s\W]*(?:del\s+)?d[ií]a\b",       # plan día / plan del día
        r"\bplan_dia\b",
    ],
}

def _strip_accents_lower(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()

def _normalize_for_match(raw: str) -> str:
    s = _strip_accents_lower(raw)
    s = _num_prefix.sub("", s)
    s = _sep_collapse.sub(" ", s).strip()
    return s

def _compile_patterns(patterns: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    compiled: dict[str, list[re.Pattern]] = {}
    for key, pats in patterns.items():
        compiled[key] = [re.compile(p, re.IGNORECASE) for p in pats]
    return compiled

_COMPILED_DEFAULT = _compile_patterns(SHEET_PATTERNS)


# =========================
# ExcelReader (IO + descubrimiento de hojas)
# =========================

class ExcelReader:
    """
    Uso típico:
        r = ExcelReader(file_path=".../POD.xlsx")
        wb = r.read_all_sheets_keyed(header=None)  # columnas crudas
        # wb => {'asistencia': ('2. Asistencia', df_raw), 'ppc': ('9.PPC', df_raw), ...}
    """

    def __init__(self, file_path: Optional[str] = None, blob: Optional[bytes] = None):
        if not file_path and blob is None:
            raise ValueError("Debes pasar file_path o blob")
        self._path = Path(file_path) if file_path else None
        self._blob = blob
        logger.debug("ExcelReader(init): path=%s blob=%s", self._path, self._blob is not None)
        if self._path and not self._path.exists():
            raise FileNotFoundError(f"El archivo {self._path} no existe")

    # -------- helpers internos --------

    def _buffer(self) -> BytesIO:
        if self._blob is not None:
            logger.debug("ExcelReader(_buffer): leyendo desde blob (%d bytes)", len(self._blob))
            return BytesIO(self._blob)
        size = self._path.stat().st_size  # type: ignore
        logger.debug("ExcelReader(_buffer): leyendo desde %s (%d bytes)", self._path, size)
        return BytesIO(self._path.read_bytes())  # type: ignore

    # -------- API pública mínima --------

    def sheet_names(self) -> List[str]:
        """Nombres crudos de las hojas (tal como vienen en el XLSX)."""
        t0 = time.perf_counter()
        try:
            bio = self._buffer()
            xls = pd.ExcelFile(bio, engine="openpyxl")
            names = xls.sheet_names
            logger.debug("ExcelReader(sheet_names): raw=%s took=%.3fs", names, time.perf_counter() - t0)
            return names
        except Exception as e:
            raise ExcelReadError(f"No se pudieron obtener las hojas: {e}") from e

    def read_sheet(
        self,
        sheet_name: str | int,
        *,
        header: int | None = None,
        dtype: Any = "string",
    ) -> pd.DataFrame:
        """
        Lee una hoja por nombre exacto o índice (0-based).
        header=None => NO asumir encabezado (deja columnas como 0..N-1).
        """
        t0 = time.perf_counter()
        try:
            bio = self._buffer()
            df = pd.read_excel(bio, sheet_name=sheet_name, header=header, dtype=dtype, engine="openpyxl")
            logger.debug("ExcelReader(read_sheet): sheet=%s shape=%s header=%s took=%.3fs",
                         sheet_name, df.shape, header, time.perf_counter() - t0)
            return df
        except Exception as e:
            raise ExcelReadError(f"Error leyendo hoja '{sheet_name}': {e}") from e

    # -------- NUEVO: leer TODAS las hojas con clave normalizada --------

    def _map_raw_to_key_canonical(self, raw_names: List[str]) -> Dict[str, str]:
        """
        Estrategia 'canonical': clave = canonical_sheet_key(nombre_raw).
        Maneja colisiones con sufijo _2, _3, ...
        """
        key_to_raw: Dict[str, str] = {}
        for raw in raw_names:
            key = canonical_sheet_key(raw)
            base = key
            i = 2
            while key in key_to_raw:
                key = f"{base}_{i}"
                i += 1
            key_to_raw[key] = raw
        return key_to_raw

    def _map_raw_to_key_patterns(
        self,
        raw_names: List[str],
        compiled: Optional[dict[str, List[re.Pattern]]] = None
    ) -> Dict[str, str]:
        """
        Estrategia 'patterns': usa SHEET_PATTERNS para mapear variaciones a claves fijas.
        Si una hoja no matchea ningún patrón, cae a canonical_sheet_key.
        Si dos hojas matchean la misma clave, la segunda gana sufijo _2, _3...
        """
        regs = compiled or _COMPILED_DEFAULT
        key_to_raw: Dict[str, str] = {}
        for raw in raw_names:
            norm = _normalize_for_match(raw)
            matched_key: Optional[str] = None
            for key, patterns in regs.items():
                if any(p.search(norm) for p in patterns):
                    matched_key = key
                    break
            if matched_key is None:
                matched_key = canonical_sheet_key(raw)
            base = matched_key
            i = 2
            while matched_key in key_to_raw:
                matched_key = f"{base}_{i}"
                i += 1
            key_to_raw[matched_key] = raw
        return key_to_raw

    def normalized_sheet_map(
        self,
        *,
        strategy: str = "patterns",   # 'patterns' | 'canonical'
        patterns: Optional[dict[str, list[str]]] = None
    ) -> Dict[str, str]:
        """
        Devuelve {clave_normalizada -> nombre_original}, sin leer DataFrames.
        strategy='patterns' usa diccionario de sinónimos/regex; 'canonical' solo normaliza.
        """
        names = self.sheet_names()
        if strategy == "canonical":
            mapping = self._map_raw_to_key_canonical(names)
        elif strategy == "patterns":
            compiled = _compile_patterns(patterns) if patterns else _COMPILED_DEFAULT
            mapping = self._map_raw_to_key_patterns(names, compiled)
        else:
            raise ValueError("strategy debe ser 'canonical' o 'patterns'")
        logger.debug("normalized_sheet_map(%s): %s", strategy, mapping)
        return mapping

    def read_all_sheets_keyed(
        self,
        *,
        header: int | None = None,     # deja None para no asumir encabezado
        dtype: Any = "string",
        strategy: str = "patterns",    # 'patterns' | 'canonical'
        patterns: Optional[dict[str, list[str]]] = None,
    ) -> Dict[str, Tuple[str, pd.DataFrame]]:
        """
        Lee todas las hojas y retorna:
            { clave_normalizada: (nombre_original, DataFrame_crudo) }

        - NO toca columnas (queda responsabilidad del transformer).
        - strategy='patterns' intenta mapear con sinónimos; si no calza, usa canonical.
        """
        t0 = time.perf_counter()
        try:
            bio = self._buffer()
            # Primero solo nombres
            xls = pd.ExcelFile(bio, engine="openpyxl")
            raw_names = xls.sheet_names

            # Mapeo a claves
            if strategy == "canonical":
                key_to_raw = self._map_raw_to_key_canonical(raw_names)
            elif strategy == "patterns":
                compiled = _compile_patterns(patterns) if patterns else _COMPILED_DEFAULT
                key_to_raw = self._map_raw_to_key_patterns(raw_names, compiled)
            else:
                raise ValueError("strategy debe ser 'canonical' o 'patterns'")

            # Volver a abrir buffer para leer data (evita puntero adelantado)
            bio2 = self._buffer()
            dfd = pd.read_excel(bio2, sheet_name=None, header=header, dtype=dtype, engine="openpyxl")

            out: Dict[str, Tuple[str, pd.DataFrame]] = {}
            for key, raw in key_to_raw.items():
                df = dfd[raw]
                out[key] = (raw, df)  # DataFrame CRUDO, sin limpiar columnas
                logger.debug("read_all_sheets_keyed: '%s' -> '%s' shape=%s", raw, key, df.shape)

            logger.debug("read_all_sheets_keyed: total=%d strategy=%s took=%.3fs",
                         len(out), strategy, time.perf_counter() - t0)
            return out

        except Exception as e:
            raise ExcelReadError(f"Error leyendo archivo: {e}") from e
