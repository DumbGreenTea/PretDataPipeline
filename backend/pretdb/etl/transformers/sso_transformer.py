# pretdb/etl/transformers/sso_transformer.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import datetime, date

import pandas as pd
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

# ----------------------------- configuración -----------------------------

MESES: Dict[str, int] = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}

# ⚠️ AJUSTA estos colores según los que tenga tu archivo real.
# Los de ejemplo suelen ser:
#   - verde:   FF00B050
#   - amarillo:FFFFFF00
#   - rojo:    FFFF0000
COLOR_A_ESTADO: Dict[str, int] = {
    "FF00B050": 0,  # verde   -> día sin accidente
    "FFFFFF00": 1,  # amarillo -> accidente sin tiempo perdido
    "FFFF0000": 2,  # rojo    -> accidente con tiempo perdido
}

# ------------------------------ utilidades ------------------------------


def _get_cell_color_hex(cell) -> Optional[str]:
    """
    Devuelve el color de relleno de una celda como ARGB (ej: 'FF00B050')
    o None si no hay color útil.
    Maneja distintos tipos que usa openpyxl para colores.
    """
    fill = getattr(cell, "fill", None)
    if fill is None:
        return None

    color = getattr(fill, "fgColor", None)
    if color is None:
        return None

    # openpyxl puede devolver .rgb como str, o como objeto RGB
    raw = getattr(color, "rgb", None)
    if raw is None:
        return None

    # Si ya es string
    if isinstance(raw, str):
        return raw.upper()

    # Si es otro tipo (p.ej. RGB), lo convertimos a str
    try:
        return str(raw).upper()
    except Exception:
        logger.debug("SSOTransformer: color.rgb raro=%r en celda %s", raw, cell.coordinate)
        return None


def _obtener_mes_y_anio(ws, df: pd.DataFrame) -> Tuple[int, int]:
    """
    Detecta el mes desde la parte superior de la hoja y el año desde alguna
    celda con fecha del DataFrame (ej: reflexión del día).
    """
    mes_num: Optional[int] = None

    # Buscar nombre del mes en las primeras filas
    for row in ws.iter_rows(min_row=1, max_row=15, values_only=True):
        for value in row:
            if isinstance(value, str):
                texto = value.strip().upper()
                if texto in MESES:
                    mes_num = MESES[texto]
                    break
        if mes_num is not None:
            break

    if mes_num is None:
        raise ValueError("SSO: no se pudo detectar el mes en la hoja.")

    # Buscar año en cualquier celda que sea fecha
    anio: Optional[int] = None
    for val in df.to_numpy().ravel():
        if isinstance(val, (pd.Timestamp, datetime, date)):
            anio = pd.to_datetime(val).year
            break

    if anio is None:
        anio = date.today().year

    return mes_num, anio


def _extraer_dias_cruz(ws) -> List[Dict[str, Any]]:
    """
    Recorre toda la hoja buscando celdas con valores 1..31 (días)
    y devuelve lista con info de cada celda (día, fila, columna, color, estado).
    """
    dias: List[Dict[str, Any]] = []

    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if isinstance(v, (int, float)) and 1 <= int(v) <= 31:
                dia = int(v)
                color_hex = _get_cell_color_hex(cell)
                estado = COLOR_A_ESTADO.get(color_hex)
                dias.append(
                    {
                        "dia": dia,
                        "fila": cell.row,
                        "columna": cell.column,
                        "color": color_hex,
                        "estado": estado,
                    }
                )

    return dias


def _buscar_valor_por_etiqueta(df: pd.DataFrame, etiqueta: str) -> Optional[float]:
    """
    Busca una fila que contenga 'etiqueta' en alguna celda string
    y devuelve el primer número que aparezca en esa fila.
    Sirve para Hallazgos, Tarjeta Verde, Policlinico.
    """
    mask = df.applymap(
        lambda x: isinstance(x, str) and etiqueta.lower() in x.lower()
    )
    coords = list(zip(*mask.to_numpy().nonzero()))
    if not coords:
        return None

    fila_idx, _ = coords[0]
    fila = df.loc[fila_idx]

    nums = fila[fila.apply(lambda x: isinstance(x, (int, float)))]
    if nums.empty:
        return None
    return float(nums.iloc[0])


def debug_colores_dias(path_excel: str, sheet_name: Optional[str] = None) -> None:
    """
    Helper opcional para probar desde consola:
    imprime los colores distintos usados en celdas de días (1..31).
    """
    wb = load_workbook(path_excel, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

    vistos = set()
    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if isinstance(v, (int, float)) and 1 <= int(v) <= 31:
                color_hex = _get_cell_color_hex(cell)
                if color_hex not in vistos:
                    vistos.add(color_hex)
                    print(f"Celda {cell.coordinate}: día={int(v)}, color={color_hex}")

    if not vistos:
        print("SSO: no se encontraron colores en días 1..31.")


# ----------------------------- clase principal -----------------------------

class SSOTransformer:
    """
    Transformer para la hoja SSO (Cruz de Seguridad + Hallazgos/Tarjeta/Policlínico).

    API compatible con el orquestador:
        run_df(self, df_raw, meta, dry_run=False, **kwargs) -> dict
    """

    sheet_key = "sso"

    def run_df(
        self,
        df_raw: pd.DataFrame,
        meta: Dict[str, Any],
        dry_run: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        logger.debug("SSOTransformer.run_df: shape=%s", df_raw.shape)

        df = df_raw.copy()

        # Intentamos obtener la ruta al archivo de varias fuentes:
        # - kwargs["file_path"]  (por si algún día se lo pasas directo)
        # - meta["source_path"]  (lo que arma el orchestrator que me mostraste)
        # - meta["file_path"]    (por compatibilidad con versiones anteriores)
        file_path = (
            kwargs.get("file_path")
            or meta.get("source_path")
            or meta.get("file_path")
        )

        if not file_path:
            logger.error(
                "SSOTransformer: meta sin file_path/source_path. Meta recibido: %r",
                meta,
            )
            raise ValueError(
                "SSOTransformer necesita 'file_path' o 'source_path' en meta/kwargs "
                "para poder leer los colores de la cruz de seguridad."
            )

        # Nombre de hoja original
        sheet_name = (
            kwargs.get("sheet_name")
            or meta.get("raw_sheet_name")
            or meta.get("sheet_key")
        )

        wb = load_workbook(file_path, data_only=True)
        if sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            logger.warning(
                "SSOTransformer: sheet_name '%s' no encontrado en %s, usando wb.active",
                sheet_name,
                file_path,
            )
            ws = wb.active

        # Mes y año de la hoja
        mes, anio = _obtener_mes_y_anio(ws, df)

        # Días de la cruz de seguridad
        dias = _extraer_dias_cruz(ws)
        logger.debug("SSOTransformer: dias_detectados=%s", len(dias))

        if not dias:
            raise ValueError("SSOTransformer: no se encontraron días (1..31) en la cruz.")

        dias_con_estado = [d for d in dias if d["estado"] is not None]

        if dias_con_estado:
            # último por número de día
            ultimo = max(dias_con_estado, key=lambda d: d["dia"])
        else:
            logger.warning(
                "SSOTransformer: ningún día tiene color mapeado en COLOR_A_ESTADO; "
                "usando último día solo por número."
            )
            ultimo = max(dias, key=lambda d: d["dia"])

        fecha_ultimo = date(anio, mes, ultimo["dia"])

        # Recuadros inferiores (Hallazgos, Tarjeta Verde, Policlinico)
        hallazgos = _buscar_valor_por_etiqueta(df, "Hallazgos")
        tarjeta_verde = _buscar_valor_por_etiqueta(df, "Tarjeta Verde")
        policlinico = _buscar_valor_por_etiqueta(df, "Policlinico")

        summary = {
            "mes": mes,
            "anio": anio,
            "ultimo_dia": ultimo["dia"],
            "fecha_ultimo_dia": fecha_ultimo.isoformat(),
            "estado_ultimo_dia": ultimo.get("estado"),
            "color_hex_ultimo_dia": ultimo.get("color"),
            "n_dias_detectados": len(dias),
            "n_dias_con_estado": len(dias_con_estado),
            "hallazgos": hallazgos,
            "tarjeta_verde": tarjeta_verde,
            "policlinico": policlinico,
        }

        return {
            "sheet": self.sheet_key,
            "rows": len(dias),
            "summary": summary,
            "dias_detail": dias,
            "dry_run": dry_run,
        }
