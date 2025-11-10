# pretdb/etl/transformers/compromiso_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional
import logging
import re
import unicodedata
from datetime import datetime, date

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ------------------------------- utilidades -------------------------------

def _norm(s: Any) -> str:
    """Quita acentos, pasa a minúsculas y colapsa espacios."""
    if s is None:
        return ""
    # NaN de pandas
    if isinstance(s, float) and np.isnan(s):
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_date_cell(x: Any) -> Optional[date]:
    """Convierte celdas a date (similar a AsistenciaTransformer)."""
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.date()
    if isinstance(x, date):
        return x

    s = (str(x) if x is not None else "").strip()
    if not s or s.lower() in ("na", "nan", "<na>"):
        return None

    # intentar ISO primero
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        dt = pd.to_datetime(s, errors="coerce", dayfirst=False)
    else:
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)

    return None if pd.isna(dt) else dt.date()


# ------------------------------ clase principal ------------------------------

class CompromisosTransformer:
    """
    Extrae compromisos desde la hoja 'Compromisos' en sus distintas variantes
    (una única tabla o múltiples bloques por disciplina).
    """
    sheet_key = "compromisos"

    # -------------------------- API p/orquestador --------------------------

    def run_df(
        self,
        df_raw: pd.DataFrame,
        meta: dict,
        dry_run: bool = False,
        **kwargs,
    ) -> dict:
        logger.debug("CompromisosTransformer.run_df: shape=%s", df_raw.shape)

        df = df_raw.copy()
        compromisos = self._extract_compromisos(df)

        summary = self._build_summary(compromisos)

        return {
            "sheet": self.sheet_key,
            "rows": len(compromisos),
            "summary": summary,
            "compromisos_detail": compromisos,
            "dry_run": dry_run,
        }

    # --------------------------- helpers internos ---------------------------

    def _find_header_rows(self, df: pd.DataFrame) -> List[int]:
        """
        Detecta filas de encabezado de tabla (las que contienen 'Ítem' / 'Item').
        Limita búsqueda a las primeras ~80 filas.
        """
        headers: List[int] = []
        max_rows = min(len(df), 80)

        for r in range(max_rows):
            row = df.iloc[r]
            found_item = False
            for val in row:
                if isinstance(val, str) and "item" in _norm(val):
                    found_item = True
                    break
            if found_item:
                headers.append(r)

        logger.debug("Compromisos: header_rows=%s", headers)
        return headers

    def _build_colmap(self, df: pd.DataFrame, header_row: int) -> Dict[str, Optional[int]]:
        """
        A partir de una fila de encabezado, mapea nombre lógico -> índice de columna.
        Soporta variantes donde la columna de descripción se llama 'Descripción' o
        trae el nombre de la disciplina ('CONSTRUCCIÓN, LOGISTICA Y BODEGA', etc.).
        """
        row = df.iloc[header_row]
        ncols = df.shape[1]

        item_col = None
        desc_col = None
        fecha_toma_col = None
        fecha_cierre_proj_col = None
        fecha_cierre_efec_col = None
        responsable_col = None
        status_col = None
        obs_col = None

        for c, val in enumerate(row):
            if not isinstance(val, str):
                continue
            nv = _norm(val)

            if "item" in nv and item_col is None:
                item_col = c
            if "descripcion" in nv and desc_col is None:
                desc_col = c
            if "fecha toma" in nv and fecha_toma_col is None:
                fecha_toma_col = c
            if "cierre" in nv and "proyect" in nv and fecha_cierre_proj_col is None:
                fecha_cierre_proj_col = c
            if "cierre" in nv and "efect" in nv and fecha_cierre_efec_col is None:
                fecha_cierre_efec_col = c
            if "responsable" in nv and responsable_col is None:
                responsable_col = c
            if "status" in nv and status_col is None:
                status_col = c
            if "observacion" in nv and obs_col is None:
                obs_col = c

        # Fallback: descripción justo a la derecha de Ítem
        if desc_col is None and item_col is not None and item_col + 1 < ncols:
            desc_col = item_col + 1

        colmap = {
            "item": item_col,
            "descripcion": desc_col,
            "fecha_toma": fecha_toma_col,
            "fecha_cierre_proyectada": fecha_cierre_proj_col,
            "fecha_cierre_efectiva": fecha_cierre_efec_col,
            "responsable": responsable_col,
            "status": status_col,
            "observacion": obs_col,
        }

        logger.debug("Compromisos: colmap(header_row=%s)=%s", header_row, colmap)
        return colmap

    def _find_categoria_for_header(self, df: pd.DataFrame, header_row: int) -> Optional[str]:
        """
        Intenta detectar la categoría / disciplina asociada a un bloque de compromisos:
        revisa 1–3 filas por encima en la primera columna.
        """
        for r in range(header_row - 1, max(-1, header_row - 4), -1):
            if r < 0:
                break
            v = df.iat[r, 0]
            if not isinstance(v, str):
                continue
            nv = _norm(v)
            if not nv:
                continue
            if nv.startswith("item"):
                continue
            if "compromisos pod" in nv:
                continue
            if "cc 006" in nv:
                continue
            # Parece un título de bloque ("HSE SEGURIDAD", "QA/QC", etc.)
            return v.strip()
        return None

    def _extract_compromisos(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Recorre todas las tablas detectadas en la hoja y arma una lista
        plana de compromisos.
        """
        compromisos: List[Dict[str, Any]] = []

        header_rows = self._find_header_rows(df)
        if not header_rows:
            logger.warning("Compromisos: no se encontraron filas de encabezado.")
            return compromisos

        nrows = len(df)

        for idx, h in enumerate(header_rows):
            colmap = self._build_colmap(df, h)
            categoria = self._find_categoria_for_header(df, h)

            item_col = colmap["item"]
            desc_col = colmap["descripcion"]
            ft_col = colmap["fecha_toma"]
            fcp_col = colmap["fecha_cierre_proyectada"]
            fce_col = colmap["fecha_cierre_efectiva"]
            resp_col = colmap["responsable"]
            stat_col = colmap["status"]
            obs_col = colmap["observacion"]

            if item_col is None or desc_col is None:
                logger.warning(
                    "Compromisos: header_row=%s sin columnas mínimas (item/descripcion).",
                    h,
                )
                continue

            # rango de filas de este bloque (hasta el próximo header o fin de hoja)
            if idx + 1 < len(header_rows):
                end_row = header_rows[idx + 1]
            else:
                end_row = nrows

            for r in range(h + 1, end_row):
                row = df.iloc[r]

                # Saltar si parece otra fila de encabezado (por seguridad)
                if any(
                    isinstance(v, str) and "item" in _norm(v)
                    for v in row
                ):
                    continue

                # valores básicos
                item_val = row[item_col] if item_col < len(row) else None
                desc_val = row[desc_col] if desc_col < len(row) else None

                # Filas totalmente vacías
                if (
                    (item_val is None or (isinstance(item_val, float) and np.isnan(item_val)))
                    and (desc_val is None or (isinstance(desc_val, float) and np.isnan(desc_val)))
                ):
                    continue

                # Filas de título sin descripción (ej: "CONSTRUCCIÓN ..." sin datos)
                if isinstance(item_val, str) and not item_val.strip().isdigit() and (
                    desc_val is None or (isinstance(desc_val, float) and np.isnan(desc_val))
                ):
                    continue

                # item debe ser algo numérico o un string de dígitos
                if isinstance(item_val, (int, float)) and not pd.isna(item_val):
                    item_num = int(item_val)
                elif isinstance(item_val, str) and item_val.strip().isdigit():
                    item_num = int(item_val.strip())
                else:
                    # No parece una fila de datos real
                    continue

                # descripción obligatoria
                if not isinstance(desc_val, str) or not desc_val.strip():
                    continue

                def get(col_idx: Optional[int]) -> Any:
                    if col_idx is None:
                        return None
                    if col_idx >= len(row):
                        return None
                    v = row[col_idx]
                    if pd.isna(v):
                        return None
                    return v

                fecha_toma = _parse_date_cell(get(ft_col))
                fecha_cierre_proj = _parse_date_cell(get(fcp_col))
                fecha_cierre_efec = _parse_date_cell(get(fce_col))
                responsable = get(resp_col)
                status = get(stat_col)
                observacion = get(obs_col)

                resp_s = None if responsable is None else str(responsable).strip()
                stat_s = None if status is None else str(status).strip()
                obs_s = None if observacion is None else str(observacion).strip()

                compromisos.append(
                    {
                        "categoria": categoria,
                        "item": item_num,
                        "descripcion": str(desc_val).strip(),
                        "fecha_toma": fecha_toma.isoformat() if fecha_toma else None,
                        "fecha_cierre_proyectada": fecha_cierre_proj.isoformat() if fecha_cierre_proj else None,
                        "fecha_cierre_efectiva": fecha_cierre_efec.isoformat() if fecha_cierre_efec else None,
                        "responsable": resp_s,
                        "status": stat_s,
                        "observacion": obs_s,
                    }
                )

        logger.debug("Compromisos: total_compromisos=%s", len(compromisos))
        return compromisos

    def _build_summary(self, compromisos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Construye métricas:
        - cantidad total
        - cantidad por estado
        - cantidad por categoría
        - resumen por empresa (para tabla compromiso_resumen)
        """
        from collections import Counter

        n = len(compromisos)
        por_estado: Counter[str] = Counter()
        por_categoria: Counter[str] = Counter()

        # resumen por empresa: empresa -> {numero_compromiso, abierto, cerrado}
        por_empresa: Dict[str, Dict[str, int]] = {}

        for c in compromisos:
            estado_raw = c.get("status") or ""
            categoria_raw = c.get("categoria") or ""
            empresa_raw = c.get("responsable") or ""

            estado_norm = _norm(estado_raw)
            categoria = categoria_raw or ""
            empresa = empresa_raw.strip()

            por_estado[estado_raw or ""] += 1
            por_categoria[categoria] += 1

            e = por_empresa.setdefault(
                empresa,
                {"numero_compromiso": 0, "abierto": 0, "cerrado": 0},
            )
            e["numero_compromiso"] += 1

            is_cerrado = estado_norm.startswith("cerrad")
            is_abierto = estado_norm.startswith("abiert")

            if is_cerrado:
                e["cerrado"] += 1
            elif is_abierto or estado_norm:  # INF/Informativo los contamos como abiertos
                e["abierto"] += 1
            # si estado_norm == "" lo dejamos en ninguno

        summary = {
            "n_compromisos": n,
            "por_estado": dict(por_estado),
            "por_categoria": dict(por_categoria),
            "por_empresa": por_empresa,
        }
        return summary
