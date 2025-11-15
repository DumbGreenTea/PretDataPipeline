from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
from django.apps import apps

from pretdb.etl.loaders import create_loader
from pretdb.etl.readers.excel_reader import ExcelReadError, ExcelReader
from pretdb.etl.transformers import create_transformer

logger = logging.getLogger(__name__)


class ETLOrchestrator:
    """
    Orquestador simple: toma un DataFrame (hoja), corre transformer -> loader,
    y entrega estadísticas homogéneas para cada etapa.
    """

    ALLOWED_SHEETS = {
        "portada",
        "asistencia",
        "sso",
        "plan_anterior",
        "plan_dia",
        "monografia_pead",
        "monografias_cruces",
        "dotacion_y_maquinaria",
        "ppc",
        "matriz_cnc",
        "compromisos",
        "layout",
    }

    def __init__(self, *, strategy: str = "patterns") -> None:
        self.strategy = strategy
        try:
            self._PodModel = apps.get_model("pretdb", "Pod")
        except LookupError:
            self._PodModel = None

    def process_sheet(
        self,
        sheet_key: str,
        df: pd.DataFrame,
        *,
        raw_sheet_name: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
        pod: Optional[Any] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "sheet": sheet_key,
            "raw_sheet_name": raw_sheet_name,
            "status": "pending",
            "success": False,
            "summary": "",
            "transformer_result": None,
            "loader_result": None,
            "error": None,
        }

        sheet_meta: Dict[str, Any] = {**(meta or {})}
        sheet_meta.setdefault("sheet_key", sheet_key)
        sheet_meta.setdefault("sheet_name", raw_sheet_name or sheet_key)
        sheet_meta.setdefault("raw_sheet_name", raw_sheet_name or sheet_meta["sheet_name"])

        try:
            transformer = create_transformer(sheet_key)
            if not transformer:
                msg = f"No hay transformer registrado para '{sheet_key}'"
                logger.warning(msg)
                result.update({"status": "error", "error": msg})
                return result

            logger.info("Transformando hoja '%s' (%s)", sheet_key, raw_sheet_name or sheet_key)
            transformer_result = transformer.run_df(df, meta=sheet_meta, dry_run=dry_run)
            result["transformer_result"] = transformer_result

            loader = create_loader(sheet_key)
            if not loader:
                msg = f"No hay loader registrado para '{sheet_key}'"
                logger.warning(msg)
                result.update({"status": "error", "error": msg})
                return result

            logger.info("Cargando datos para '%s'", sheet_key)
            loader_result_raw = loader.persist(
                pod=pod,
                out=transformer_result,
                dry_run=dry_run,
                run_id=sheet_meta.get("run_id"),
            )
            loader_result = self._normalize_loader_result(loader_result_raw, sheet_key)
            result["loader_result"] = loader_result
            result["status"] = "ok"
            result["success"] = True
            result["summary"] = self._format_loader_summary(transformer_result, loader_result)

        except Exception as exc:
            logger.exception("Error procesando hoja '%s'", sheet_key)
            result.update({"status": "error", "error": str(exc)})

        return result

    def process_excel_file(
        self,
        file_path: str,
        *,
        sheets_to_process: Optional[Sequence[str]] = None,
        dry_run: bool = False,
        strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "file": file_path,
            "strategy": strategy or self.strategy,
            "dry_run": dry_run,
            "sheets_detected": 0,
            "sheets_processed": 0,
            "results": {},
        }

        try:
            reader = ExcelReader(file_path=file_path)
            workbook = reader.read_all_sheets_keyed(
                header=None,
                dtype="string",
                strategy=summary["strategy"],
            )
        except (ExcelReadError, FileNotFoundError) as exc:
            logger.error("No se pudo leer %s: %s", file_path, exc)
            summary["error"] = str(exc)
            summary["results"]["__file__"] = {
                "sheet": "__file__",
                "raw_sheet_name": None,
                "status": "error",
                "success": False,
                "summary": "",
                "transformer_result": None,
                "loader_result": None,
                "error": str(exc),
            }
            return summary

        if self.ALLOWED_SHEETS:
            original_keys = set(workbook.keys())
            workbook = {k: v for k, v in workbook.items() if k in self.ALLOWED_SHEETS}
            ignored = sorted(original_keys - set(workbook.keys()))
            if ignored:
                logger.info("Ignorando hojas no permitidas: %s", ", ".join(ignored))

        summary["sheets_detected"] = len(workbook)
        pod_context: Optional[Any] = None
        target_keys: Iterable[str]
        if sheets_to_process:
            target_keys = list(sheets_to_process)
        else:
            target_keys = list(workbook.keys())

        for key in target_keys:
            if key not in workbook:
                msg = f"La hoja '{key}' no está presente en el archivo"
                logger.warning(msg)
                summary["results"][key] = {
                    "sheet": key,
                    "raw_sheet_name": None,
                    "status": "error",
                    "success": False,
                    "summary": "",
                    "transformer_result": None,
                    "loader_result": None,
                    "error": msg,
                }
                continue

            raw_name, df = workbook[key]
            sheet_result = self.process_sheet(
                sheet_key=key,
                df=df,
                raw_sheet_name=raw_name,
                meta={
                    "file_path": file_path,
                    "sheet_name": raw_name,
                    "raw_sheet_name": raw_name,
                },
                dry_run=dry_run,
                pod=pod_context,
            )
            summary["results"][key] = sheet_result

            if sheet_result.get("success"):
                loader_info = sheet_result.get("loader_result") or {}
                new_pod = self._extract_pod_from_loader(loader_info)
                if new_pod:
                    pod_context = new_pod

        summary["sheets_processed"] = sum(
            1 for item in summary["results"].values() if item.get("success")
        )
        return summary

    @staticmethod
    def _format_loader_summary(
        transformer_result: Optional[Dict[str, Any]],
        loader_result: Optional[Dict[str, Any]],
    ) -> str:
        rows = (transformer_result or {}).get("rows")
        created = (loader_result or {}).get("created")
        updated = (loader_result or {}).get("updated")
        skipped = (loader_result or {}).get("skipped")
        warnings = loader_result.get("warnings") if loader_result else None

        parts: List[str] = []
        if rows is not None:
            parts.append(f"rows={rows}")
        if created:
            parts.append("created=1")
        if updated:
            parts.append("updated=1")
        if skipped:
            parts.append(f"skipped={skipped}")
        if warnings:
            parts.append(f"warnings={len(warnings)}")
        return ", ".join(parts) if parts else "sin cambios"

    def _extract_pod_from_loader(self, loader_result: Dict[str, Any]) -> Optional[Any]:
        if not loader_result:
            return None
        direct = loader_result.get("pod") or loader_result.get("pod_instance")
        if direct:
            return direct
        pod_id = loader_result.get("pod_id")
        if pod_id and self._PodModel:
            try:
                return self._PodModel.objects.get(id=pod_id)
            except self._PodModel.DoesNotExist:
                logger.warning("No se encontró Pod con id=%s para contexto ETL.", pod_id)
        numero = loader_result.get("numero_pod")
        if numero and self._PodModel:
            try:
                return self._PodModel.objects.get(numero_pod=numero)
            except self._PodModel.DoesNotExist:
                logger.warning("No se encontró Pod con numero_pod=%s para contexto ETL.", numero)
        return None

    def _normalize_loader_result(self, loader_result: Any, sheet_key: str) -> Dict[str, Any]:
        """
        Adapta salidas antiguas (tuplas) a dict homogéneo para el orquestador.
        """
        if isinstance(loader_result, dict):
            return loader_result
        if isinstance(loader_result, tuple):
            rows = loader_result[0] if len(loader_result) > 0 else 0
            warnings = loader_result[1] if len(loader_result) > 1 else None
            errors = loader_result[2] if len(loader_result) > 2 else None
            normalized: Dict[str, Any] = {
                "rows": rows,
                "warnings": warnings or [],
                "errors": errors,
                "created": bool(rows),
                "updated": False,
                "skipped": 0 if rows else 1,
                "sheet": sheet_key,
            }
            return normalized
        if loader_result is None:
            return {}
        return {"result": loader_result, "sheet": sheet_key}


def run_file(
    file_path: str,
    *,
    strategy: str = "patterns",
    only: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Helper usado por management commands para ejecutar el pipeline completo.
    """
    orchestrator = ETLOrchestrator(strategy=strategy)
    return orchestrator.process_excel_file(
        file_path=file_path,
        sheets_to_process=list(only) if only else None,
        dry_run=dry_run,
        strategy=strategy,
    )
