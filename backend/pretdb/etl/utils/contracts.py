from __future__ import annotations

from typing import Optional, Tuple
import re

import pandas as pd
from django.apps import apps

CONTRACT_NUMBER_PATTERN = re.compile(
    r"contrato\W*(?:n[º°o]\.?|#)?\s*([0-9]{5,})",
    re.I,
)
CONTRACT_NAME_PATTERN = re.compile(
    r"(?:nombre\s*(?:del)?\s*contrato|nombre)\s*[:\-]\s*(.+)$",
    re.I,
)


def _clean_text(value: Optional[str], maxlen: int) -> Optional[str]:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text[:maxlen] if len(text) > maxlen else text


def _clean_contract_number(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D+", "", str(value))
    return digits[:20] if digits else None


def parse_contract_from_df(
    df: pd.DataFrame,
    max_rows: int = 20,
    max_cols: int = 10,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Busca el número y nombre de contrato en las primeras filas de un DataFrame.
    Devuelve (numero, nombre) o (None, None) si no se detecta.
    """
    if df is None or df.empty:
        return None, None

    numero: Optional[str] = None
    nombre: Optional[str] = None

    sub = df.iloc[:max_rows, :max_cols].fillna("")
    for _, row in sub.iterrows():
        values = [str(x) for x in row.tolist()]
        line = "  ".join(values)
        line = re.sub(r"\s+", " ", line).strip()

        if not numero:
            match_num = CONTRACT_NUMBER_PATTERN.search(line)
            if match_num:
                numero = match_num.group(1)
        if not nombre:
            match_nom = CONTRACT_NAME_PATTERN.search(line)
            if match_nom:
                nombre = match_nom.group(1).strip()
        if numero and nombre:
            break

        for cell in values:
            if not numero:
                match_num = CONTRACT_NUMBER_PATTERN.search(cell)
                if match_num:
                    numero = match_num.group(1)
            if not nombre:
                match_nom = CONTRACT_NAME_PATTERN.search(cell)
                if match_nom:
                    nombre = match_nom.group(1).strip()
            if numero and nombre:
                break

    return numero, nombre


def ensure_pod_contract(
    pod: Optional[object],
    numero: Optional[str],
    nombre: Optional[str],
) -> Optional[object]:
    """
    Garantiza que el POD tenga asociado un contrato (sin sobreescribir si ya existe).
    Reutiliza o crea un Contrato global y lo asigna al POD si no tenía uno.
    """
    if not pod:
        return None
    if getattr(pod, "contrato_id", None):
        return getattr(pod, "contrato", None)

    numero_limpio = _clean_contract_number(numero)
    nombre_limpio = _clean_text(nombre, 120)
    if not numero_limpio and not nombre_limpio:
        return None

    Contrato = apps.get_model("pretdb", "Contrato")
    filters = {}
    if numero_limpio:
        filters["nombre_codigo"] = numero_limpio
    if nombre_limpio:
        filters["nombre_contrato"] = nombre_limpio

    defaults = {}
    if "nombre_codigo" not in filters and numero_limpio:
        defaults["nombre_codigo"] = numero_limpio
    if "nombre_contrato" not in filters:
        defaults["nombre_contrato"] = nombre_limpio or (f"Contrato {numero_limpio}" if numero_limpio else None)

    contrato, _ = Contrato.objects.get_or_create(defaults=defaults, **filters)

    if not getattr(pod, "contrato_id", None):
        pod.contrato = contrato
        pod.save(update_fields=["contrato"])
    return contrato
