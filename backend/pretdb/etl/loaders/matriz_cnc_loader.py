# pretdb/etl/loaders/matriz_cnc_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
import re

from django.db import transaction
from django.apps import apps

from pretdb.etl.loaders.registry import register_loader

logger = logging.getLogger(__name__)


def _get_model(app_label: str, model_name: str):
    try:
        return apps.get_model(app_label, model_name)
    except LookupError:
        return None


def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s


def _norm_code_id(txt: Optional[str]) -> Optional[str]:
    """Normaliza códigos '1,1' / '1-1' -> '1.1'"""
    if not txt:
        return None
    s = str(txt).strip()
    # Si es algo tipo RC03, mantener
    if len(s) <= 10 and any(c.isalpha() for c in s):
        return s.upper()
    # Reemplazar separadores por '.'
    s = s.replace(",", ".").replace("-", ".")
    # Compactar múltiples puntos
    s = re.sub(r"\.+", ".", s)
    return s


def _code_to_int(code: Optional[str]) -> Optional[int]:
    """Convierte un código (1.1, '02', etc.) a entero si solo contiene dígitos."""
    if not code:
        return None
    digits = re.findall(r"\d+", str(code))
    if not digits:
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


@register_loader("matriz_cnc")
class MatrizCNCLoader:
    """
    Carga datos de Matriz CNC desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.MatrizCNC = _get_model(pretdb, "MatrizCNC")

    @transaction.atomic
    def persist(
        self,
        pod,  # Objeto Pod ya creado desde portada
        out: dict,
        *,
        dry_run: bool = False,
        run_id: Optional[int] = None,
    ) -> Tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
        """
        Persiste los datos de Matriz CNC
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings: Dict[str, List[str]] = {}

        if not self.Pod or not self.MatrizCNC:
            error_msg = "Modelos Pod o MatrizCNC no encontrados"
            logger.error(error_msg)
            return 0, {}, {"models": error_msg}

        if not pod or not isinstance(pod, self.Pod):
            error_msg = "Pod no proporcionado o inválido"
            logger.error(error_msg)
            return 0, {}, {"pod": error_msg}

        # Extraer datos del transformer
        flat_rows = out.get("flat_rows", [])
        summary = out.get("summary", {})
        
        if not flat_rows:
            warnings["sin_datos"] = ["No hay datos de Matriz CNC para procesar"]
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        registros_creados = 0
        registros_actualizados = 0
        causas_primarias_unicas: set[str] = set()
        causas_secundarias_unicas: set[str] = set()

        try:
            if not dry_run:
                deleted = self.RegistroCNC.objects.filter(pod=pod).delete()
                logger.debug("Matriz CNC: %s registros previos eliminados para pod %s", deleted[0], pod.id)

            for row_data in flat_rows:
                causa_primaria_id = _norm_code_id(row_data.get("causa_primaria_id"))
                causa_primaria_desc = _truncate(row_data.get("causa_primaria_desc"), 200)
                causa_secundaria_id = _norm_code_id(row_data.get("causa_secundaria_id"))
                causa_secundaria_desc = _truncate(row_data.get("causa_secundaria_desc"), 200)

                if dry_run:
                    logger.debug(
                        "🔶 DRY RUN - CNC: Primaria %s (%s) -> Secundaria %s",
                        causa_primaria_id,
                        causa_primaria_desc,
                        causa_secundaria_id,
                    )
                    total_rows += 1
                    if causa_primaria_desc:
                        causas_primarias_unicas.add(causa_primaria_desc)
                    if causa_secundaria_desc:
                        causas_secundarias_unicas.add(causa_secundaria_desc)
                    continue

                try:
                    defaults = {
                        "gestion_atribuible": row_data.get("gestion_atribuible"),
                        "causa_primaria_num": _code_to_int(causa_primaria_id),
                        "causa_primaria": causa_primaria_desc,
                        "causa_secundaria_codigo": causa_secundaria_id,
                        "causa_secundaria": causa_secundaria_desc,
                    }

                    filtro = {
                        "pod": pod,
                        "causa_primaria": causa_primaria_desc,
                        "causa_secundaria": causa_secundaria_desc,
                        "causa_secundaria_codigo": causa_secundaria_id,
                    }

                    registro_obj, created = self.MatrizCNC.objects.update_or_create(
                        defaults=defaults,
                        **filtro,
                    )
                    total_rows += 1
                    if created:
                        registros_creados += 1
                    else:
                        registros_actualizados += 1
                    if causa_primaria_desc:
                        causas_primarias_unicas.add(causa_primaria_desc)
                    if causa_secundaria_desc:
                        causas_secundarias_unicas.add(causa_secundaria_desc)
                except Exception as exc:
                    prim_id = causa_primaria_id or causa_primaria_desc or "Sin ID"
                    sec_id = causa_secundaria_id or causa_secundaria_desc or "Sin ID"
                    warnings.setdefault("registros", []).append(
                        f"{prim_id} -> {sec_id}: {exc}"
                    )

            stats = {
                "total_filas": len(flat_rows),
                "registros_creados": registros_creados,
                "registros_actualizados": registros_actualizados,
                "causas_primarias_unicas": len(causas_primarias_unicas),
                "causas_secundarias_unicas": len(causas_secundarias_unicas),
                "relaciones_procesadas": registros_creados + registros_actualizados,
                "resumen_transformer": summary.get("counts"),
            }

            if not dry_run:
                logger.info(
                    "✅ Matriz CNC: %s registros creados, %s actualizados",
                    registros_creados,
                    registros_actualizados,
                )
            else:
                logger.info("🔶 DRY RUN - Matriz CNC: %s filas", len(flat_rows))

            if not dry_run:
                self._crear_resumen_matriz_cnc(pod, stats)

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de Matriz CNC: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _crear_resumen_matriz_cnc(self, pod: Any, stats: Dict[str, Any]) -> None:
        """Crea un registro de resumen de la matriz CNC"""
        try:
            from pretdb.models import ResumenMatrizCNC
            resumen, created = ResumenMatrizCNC.objects.update_or_create(
                pod=pod,
                defaults={
                    'total_causas_primarias': stats.get("causas_primarias_unicas", 0),
                    'total_causas_secundarias': stats.get("causas_secundarias_unicas", 0),
                    'total_relaciones': stats.get("relaciones_procesadas", 0),
                }
            )
            if created:
                logger.info(f"✅ Resumen Matriz CNC creado para POD {pod.numero_pod}")
        except Exception as e:
            logger.warning(f"No se pudo crear resumen de Matriz CNC: {e}")
