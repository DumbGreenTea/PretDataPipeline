# pretdb/etl/loaders/compromisos_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime
from django.db import transaction
from django.apps import apps

logger = logging.getLogger(__name__)

def _get_model(app_label: str, model_name: str):
    try:
        return apps.get_model(app_label, model_name)
    except LookupError:
        return None

def _pick(d: Dict[str, Any], *keys: str, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", "<NA>"):
            return d[k]
    return default

def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        import pandas as pd
        dt = pd.to_datetime(str(v), errors="coerce", dayfirst=True)
        return None if pd.isna(dt) else dt.date()
    except Exception:
        return None

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s

def _normalizar_estado(estado_raw: Optional[str]) -> str:
    """Normaliza el estado del compromiso"""
    if not estado_raw:
        return "desconocido"
    
    estado = str(estado_raw).strip().lower()
    if estado.startswith("cerrad"):
        return "cerrado"
    elif estado.startswith("abiert"):
        return "abierto"
    elif estado.startswith("inf"):  # INF, informativo
        return "informativo"
    else:
        return "desconocido"


class CompromisosLoader:
    """
    Carga datos de Compromisos desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Compromiso = _get_model(pretdb, "Compromiso")
        self.CategoriaCompromiso = _get_model(pretdb, "CategoriaCompromiso")
        self.Empresa = _get_model(pretdb, "Empresa")

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
        Persiste los datos de Compromisos
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.Compromiso:
            error_msg = "Modelos Pod o Compromiso no encontrados"
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
            warnings["sin_datos"] = "No hay datos de Compromisos para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        compromisos_procesados = 0
        categorias_procesadas = 0
        empresas_procesadas = 0

        try:
            # Cache para objetos reutilizables
            categorias_cache = {}
            empresas_cache = {}

            for row_data in flat_rows:
                # Obtener datos normalizados
                categoria_nombre = _truncate(row_data.get("categoria"), 100)
                item_numero = row_data.get("item")
                descripcion = _truncate(row_data.get("descripcion"), 500)
                responsable = _truncate(row_data.get("responsable"), 100)
                estado_raw = row_data.get("status_raw")
                estado_normalizado = _normalizar_estado(estado_raw)

                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Compromiso: {item_numero} - {descripcion[:50]}...")
                    continue

                # 1. Procesar categoría
                categoria_obj = None
                if categoria_nombre:
                    try:
                        if categoria_nombre not in categorias_cache:
                            if self.CategoriaCompromiso:
                                categoria_obj, created = self.CategoriaCompromiso.objects.update_or_create(
                                    nombre=categoria_nombre,
                                    defaults={'descripcion': categoria_nombre}
                                )
                            else:
                                # Si no existe modelo separado, usar campo directo
                                categoria_obj = None
                            categorias_cache[categoria_nombre] = categoria_obj
                            if created:
                                categorias_procesadas += 1
                                total_rows += 1
                        else:
                            categoria_obj = categorias_cache[categoria_nombre]
                    except Exception as e:
                        warnings.setdefault("categorias", []).append(f"'{categoria_nombre}': {e}")

                # 2. Procesar empresa/responsable
                empresa_obj = None
                if responsable:
                    try:
                        if responsable not in empresas_cache:
                            if self.Empresa:
                                empresa_obj, created = self.Empresa.objects.update_or_create(
                                    nombre=responsable,
                                    defaults={'nombre': responsable}
                                )
                            else:
                                # Si no existe modelo separado, usar campo directo
                                empresa_obj = None
                            empresas_cache[responsable] = empresa_obj
                            if created:
                                empresas_procesadas += 1
                                total_rows += 1
                        else:
                            empresa_obj = empresas_cache[responsable]
                    except Exception as e:
                        warnings.setdefault("empresas", []).append(f"'{responsable}': {e}")

                # 3. Crear/actualizar compromiso
                try:
                    # Determinar clave única para el compromiso
                    if categoria_obj and self.CategoriaCompromiso:
                        # Con modelo de categoría
                        compromiso_obj, created = self.Compromiso.objects.update_or_create(
                            pod=pod,
                            categoria=categoria_obj,
                            numero_item=item_numero,
                            defaults={
                                'descripcion': descripcion,
                                'fecha_toma': _to_date(row_data.get("fecha_toma")),
                                'fecha_cierre_proyectada': _to_date(row_data.get("fecha_cierre_proyectada")),
                                'fecha_cierre_efectiva': _to_date(row_data.get("fecha_cierre_efectiva")),
                                'responsable': empresa_obj if empresa_obj else responsable,
                                'estado_raw': _truncate(estado_raw, 50),
                                'estado': estado_normalizado,
                                'observaciones': _truncate(row_data.get("observacion"), 500),
                            }
                        )
                    else:
                        # Sin modelo de categoría o con categoría como texto
                        compromiso_obj, created = self.Compromiso.objects.update_or_create(
                            pod=pod,
                            categoria_texto=categoria_nombre,
                            numero_item=item_numero,
                            defaults={
                                'descripcion': descripcion,
                                'fecha_toma': _to_date(row_data.get("fecha_toma")),
                                'fecha_cierre_proyectada': _to_date(row_data.get("fecha_cierre_proyectada")),
                                'fecha_cierre_efectiva': _to_date(row_data.get("fecha_cierre_efectiva")),
                                'responsable': empresa_obj if empresa_obj else responsable,
                                'estado_raw': _truncate(estado_raw, 50),
                                'estado': estado_normalizado,
                                'observaciones': _truncate(row_data.get("observacion"), 500),
                            }
                        )
                    
                    if created:
                        compromisos_procesados += 1
                        total_rows += 1
                        
                except Exception as e:
                    desc = descripcion or "Sin descripción"
                    warnings.setdefault("compromisos", []).append(f"Item {item_numero} - '{desc}': {e}")

            # Resumen final
            stats = {
                "compromisos_procesados": compromisos_procesados,
                "categorias_procesadas": categorias_procesadas,
                "empresas_procesadas": empresas_procesadas,
                "total_compromisos": len(flat_rows),
                "por_estado": summary.get("por_estado", {}),
                "por_categoria": summary.get("por_categoria", {}),
                "por_empresa": summary.get("por_empresa", {}),
            }
            
            if not dry_run:
                logger.info(f"✅ Compromisos procesados: {compromisos_procesados} compromisos, {categorias_procesadas} categorías, {empresas_procesadas} empresas")
                # Log resumen por estado
                for estado, cantidad in stats["por_estado"].items():
                    logger.info(f"   - {estado}: {cantidad}")
            else:
                logger.info(f"🔶 DRY RUN - Compromisos: {len(flat_rows)} compromisos a procesar")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de Compromisos: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _crear_resumen_compromisos(self, pod: Any, stats: Dict[str, Any]) -> None:
        """Crea un registro de resumen de compromisos"""
        try:
            from pretdb.models import ResumenCompromisos
            resumen, created = ResumenCompromisos.objects.update_or_create(
                pod=pod,
                defaults={
                    'total_compromisos': stats.get("total_compromisos", 0),
                    'compromisos_abiertos': stats.get("por_estado", {}).get("abierto", 0),
                    'compromisos_cerrados': stats.get("por_estado", {}).get("cerrado", 0),
                    'compromisos_informativos': stats.get("por_estado", {}).get("informativo", 0),
                    'total_categorias': stats.get("categorias_procesadas", 0),
                    'total_empresas': stats.get("empresas_procesadas", 0),
                }
            )
            if created:
                logger.info(f"✅ Resumen Compromisos creado para POD {pod.numero_pod}")
        except Exception as e:
            logger.warning(f"No se pudo crear resumen de Compromisos: {e}")

# Registro en el sistema de loaders
def register_loader():
    from etl.registry import register_loader
    register_loader("compromisos")(CompromisosLoader)