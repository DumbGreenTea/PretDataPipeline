# pretdb/etl/processors/orchestrator.py
from __future__ import annotations
from pathlib import Path
from typing import Iterable, Dict, Any, Optional
import hashlib
import logging

import pandas as pd  # solo para type hints y depuración

from pretdb.etl.readers.excel_reader import ExcelReader
from pretdb.etl.transformers.registry import REGISTRY  # {"portada": PortadaTransformer(), ...}

log = logging.getLogger(__name__)

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run_file(
    file_path: str | Path,
    *,
    strategy: str = "patterns",          # detección de hojas: "patterns" | "canonical"
    only: Optional[Iterable[str]] = None, # p.ej. ["portada","asistencia"]
    dry_run: bool = False,                # si tus transformers lo soportan
) -> Dict[str, Any]:
    """
    Lee el Excel, detecta hojas, y despacha a sus transformers.
    Devuelve un resumen con resultados por hoja.
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(p)

    reader = ExcelReader(file_path=str(p))

    # 1) Descubrimiento de hojas
    mapping = reader.normalized_sheet_map(strategy=strategy)     # {clave -> nombre_original}
    wb = reader.read_all_sheets_keyed(header=None, strategy=strategy)  # {clave: (raw_name, df_raw)}
    file_hash = sha256_file(p)

    selected_keys = set(mapping.keys())
    if only:
        selected_keys &= {k.strip() for k in only}

    results: Dict[str, Dict[str, Any]] = {}
    for key in sorted(selected_keys):
        if key not in REGISTRY:
            log.info("Skipping sheet '%s' (no transformer registered)", key)
            continue

        raw_name, df_raw = wb[key]
        meta = {
            "sheet_key": key,
            "raw_sheet_name": raw_name,
            "file_name": p.name,
            "file_hash": file_hash,
            "source_path": str(p),
        }

        try:
            tx = REGISTRY[key]
            # Si el transformer expone run_df, úsalo (recibe el DataFrame crudo)
            if hasattr(tx, "run_df"):
                try:
                    out = tx.run_df(df_raw, meta=meta, dry_run=dry_run)  # type: ignore
                except TypeError:
                    out = tx.run_df(df_raw, meta=meta)  # type: ignore
            else:
                # Fallback: algunos transformers podrían leer ellos mismos desde el reader
                out = tx.run(reader)  # type: ignore
            results[key] = {"status": "ok", "summary": out}
        except Exception as e:
            log.exception("Error processing sheet %s", key)
            results[key] = {"status": "error", "error": str(e)}

    return {
        "file": p.name,
        "hash": file_hash,
        "sheets_detected": sorted(mapping.keys()),
        "sheets_processed": sorted([k for k, v in results.items() if v["status"] == "ok"]),
        "results": results,
    }
