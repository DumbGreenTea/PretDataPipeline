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

def _pick(d: Dict[str, Any], *keys: str, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", "<NA>"):
            return d[k]
    return default

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


@register_loader("matriz_cnc")
class MatrizCNCLoader:
    """
    Carga datos de Matriz CNC desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.CausaPrimaria = _get_model(pretdb, "CausaPrimaria")
        self.CausaSecundaria = _get_model(pretdb, "CausaSecundaria")
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
        warnings = {}
        errors = None
        
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
            warnings["sin_datos"] = "No hay datos de Matriz CNC para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        causas_primarias_procesadas = 0
        causas_secundarias_procesadas = 0
        relaciones_procesadas = 0

        try:
            # Diccionarios para cache de objetos creados
            primarias_cache = {}
            secundarias_cache = {}

            for row_data in flat_rows:
                # Obtener y normalizar IDs
                causa_primaria_id = _norm_code_id(row_data.get("causa_primaria_id"))
                causa_primaria_desc = _truncate(row_data.get("causa_primaria_desc"), 200)
                causa_secundaria_id = _norm_code_id(row_data.get("causa_secundaria_id"))
                causa_secundaria_desc = _truncate(row_data.get("causa_secundaria_desc"), 200)

                if dry_run:
                    logger.debug(f"🔶 DRY RUN - CNC: Primaria {causa_primaria_id} -> Secundaria {causa_secundaria_id}")
                    continue

                # 1. Procesar causa primaria
                causa_primaria_obj = None
                if causa_primaria_id or causa_primaria_desc:
                    try:
                        # Usar cache para evitar duplicados
                        cache_key = f"{causa_primaria_id}_{causa_primaria_desc}"
                        if cache_key not in primarias_cache:
                            if self.CausaPrimaria:
                                causa_primaria_obj, created = self.CausaPrimaria.objects.update_or_create(
                                    codigo=causa_primaria_id,
                                    defaults={
                                        'descripcion': causa_primaria_desc,
                                    }
                                )
                            else:
                                # Si no existe el modelo CausaPrimaria, crear directamente en MatrizCNC
                                causa_primaria_obj = None
                            primarias_cache[cache_key] = causa_primaria_obj
                            if created:
                                causas_primarias_procesadas += 1
                                total_rows += 1
                        else:
                            causa_primaria_obj = primarias_cache[cache_key]
                    except Exception as e:
                        warnings.setdefault("causas_primarias", []).append(
                            f"'{causa_primaria_id}': {e}"
                        )

                # 2. Procesar causa secundaria
                causa_secundaria_obj = None
                if causa_secundaria_id or causa_secundaria_desc:
                    try:
                        cache_key = f"{causa_secundaria_id}_{causa_secundaria_desc}"
                        if cache_key not in secundarias_cache:
                            if self.CausaSecundaria:
                                causa_secundaria_obj, created = self.CausaSecundaria.objects.update_or_create(
                                    codigo=causa_secundaria_id,
                                    defaults={
                                        'descripcion': causa_secundaria_desc,
                                    }
                                )
                            else:
                                # Si no existe el modelo CausaSecundaria, crear directamente en MatrizCNC
                                causa_secundaria_obj = None
                            secundarias_cache[cache_key] = causa_secundaria_obj
                            if created:
                                causas_secundarias_procesadas += 1
                                total_rows += 1
                        else:
                            causa_secundaria_obj = secundarias_cache[cache_key]
                    except Exception as e:
                        warnings.setdefault("causas_secundarias", []).append(
                            f"'{causa_secundaria_id}': {e}"
                        )

                # 3. Crear relación en Matriz CNC
                try:
                    # Determinar clave única para la relación
                    if causa_primaria_obj and causa_secundaria_obj:
                        # Si tenemos ambos objetos, usar FKs
                        matriz_obj, created = self.MatrizCNC.objects.update_or_create(
                            pod=pod,
                            causa_primaria=causa_primaria_obj,
                            causa_secundaria=causa_secundaria_obj,
                            defaults={}  # No hay campos adicionales por ahora
                        )
                    else:
                        # Si no tenemos modelos separados, usar campos directos
                        matriz_obj, created = self.MatrizCNC.objects.update_or_create(
                            pod=pod,
                            causa_primaria_codigo=causa_primaria_id,
                            causa_primaria_desc=causa_primaria_desc,
                            causa_secundaria_codigo=causa_secundaria_id,
                            causa_secundaria_desc=causa_secundaria_desc,
                            defaults={}
                        )
                    
                    if created:
                        relaciones_procesadas += 1
                        total_rows += 1
                        
                except Exception as e:
                    prim_id = causa_primaria_id or "Sin ID"
                    sec_id = causa_secundaria_id or "Sin ID"
                    warnings.setdefault("relaciones", []).append(
                        f"'{prim_id} -> {sec_id}': {e}"
                    )

            # Resumen final
            stats = {
                "causas_primarias_procesadas": causas_primarias_procesadas,
                "causas_secundarias_procesadas": causas_secundarias_procesadas,
                "relaciones_procesadas": relaciones_procesadas,
                "total_filas": len(flat_rows),
                "causas_primarias_unicas": len(primarias_cache),
                "causas_secundarias_unicas": len(secundarias_cache),
            }
            
            if not dry_run:
                logger.info(f"✅ Matriz CNC procesada: {causas_primarias_procesadas} primarias, {causas_secundarias_procesadas} secundarias, {relaciones_procesadas} relaciones")
            else:
                logger.info(f"🔶 DRY RUN - Matriz CNC: {len(flat_rows)} filas a procesar")

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
