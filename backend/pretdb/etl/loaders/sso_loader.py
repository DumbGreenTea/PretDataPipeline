# pretdb/etl/loaders/sso_loader.py
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


class SsoLoader:
    """
    Carga datos de SSO (Cruz de Seguridad) desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Sso = _get_model(pretdb, "Sso")
        self.SsoCruzSeguridad = _get_model(pretdb, "SsoCruzSeguridad")
        self.SsoEstadoDia = _get_model(pretdb, "SsoEstadoDia")
        self.SsoReflexion = _get_model(pretdb, "SsoReflexion")

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
        Persiste los datos de SSO
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.Sso:
            error_msg = "Modelos Pod o Sso no encontrados"
            logger.error(error_msg)
            return 0, {}, {"models": error_msg}

        if not pod or not isinstance(pod, self.Pod):
            error_msg = "Pod no proporcionado o inválido"
            logger.error(error_msg)
            return 0, {}, {"pod": error_msg}

        # Extraer datos del transformer
        summary = out.get("summary", {})
        dias_detail = out.get("dias_detail", [])
        
        mes = summary.get("mes")
        anio = summary.get("anio")
        
        if not mes or not anio:
            warnings["datos_incompletos"] = "Faltan mes o año en los datos de SSO"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        dias_procesados = 0
        estados_creados = 0
        reflexion_creada = False

        try:
            # 1. Crear o obtener registro principal de SSO
            if dry_run:
                logger.info(f"🔶 DRY RUN - Crearía SSO para POD {pod.numero_pod}")
                sso_obj = None
            else:
                sso_obj, sso_created = self.Sso.objects.get_or_create(pod=pod)
                if sso_created:
                    logger.info(f"✅ SSO creado para POD {pod.numero_pod}")
                else:
                    logger.debug(f"✅ SSO encontrado para POD {pod.numero_pod}")

            # 2. Procesar estados de días (cruz de seguridad)
            for dia_data in dias_detail:
                dia_num = dia_data.get("dia")
                estado_color = dia_data.get("estado")
                color_hex = dia_data.get("color")
                
                if not dia_num:
                    continue
                    
                # Crear fecha para este día
                try:
                    fecha_dia = date(anio, mes, dia_num)
                except ValueError as e:
                    warnings.setdefault("fechas_invalidas", []).append(f"Día {dia_num}: {e}")
                    continue
                
                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Día {dia_num}: estado={estado_color}, color={color_hex}")
                    dias_procesados += 1
                    continue
                
                # Crear registro de estado del día
                if sso_obj:
                    try:
                        estado_obj, created = self.SsoEstadoDia.objects.update_or_create(
                            sso=sso_obj,
                            fecha=fecha_dia,
                            defaults={
                                'estado': estado_color,
                                'color_hex': color_hex,
                            }
                        )
                        if created:
                            estados_creados += 1
                        dias_procesados += 1
                        total_rows += 1
                    except Exception as e:
                        warnings.setdefault("estados_dia", []).append(f"Día {dia_num}: {e}")
            
            # 3. Procesar datos de reflexión (hallazgos, tarjeta verde, policlínico)
            hallazgos = summary.get("hallazgos")
            tarjeta_verde = summary.get("tarjeta_verde")
            policlinico = summary.get("policlinico")
            
            # Usar la fecha del POD para la reflexión
            fecha_reflexion = pod.fecha
            
            if hallazgos is not None or tarjeta_verde is not None or policlinico is not None:
                if dry_run:
                    logger.info(f"🔶 DRY RUN - Reflexión: hallazgos={hallazgos}, tarjeta_verde={tarjeta_verde}, policlínico={policlinico}")
                    reflexion_creada = True
                elif sso_obj:
                    try:
                        reflexion_obj, created = self.SsoReflexion.objects.update_or_create(
                            sso=sso_obj,
                            fecha=fecha_reflexion,
                            defaults={
                                'hallazgos': int(hallazgos) if hallazgos is not None else None,
                                'tarjeta_verde': int(tarjeta_verde) if tarjeta_verde is not None else None,
                                'traslado_policlinico': int(policlinico) if policlinico is not None else None,
                            }
                        )
                        if created:
                            reflexion_creada = True
                            total_rows += 1
                            logger.info(f"✅ Reflexión SSO creada para POD {pod.numero_pod}")
                    except Exception as e:
                        warnings["reflexion"] = f"No se pudo crear reflexión: {e}"

            # 4. Crear registro de cruz de seguridad (resumen mensual)
            if not dry_run and sso_obj:
                try:
                    cruz_obj, created = self.SsoCruzSeguridad.objects.update_or_create(
                        sso=sso_obj,
                        mes=mes,
                        anio=anio,
                        defaults={
                            'total_dias': len(dias_detail),
                            'dias_con_estado': summary.get("n_dias_con_estado", 0),
                        }
                    )
                    if created:
                        total_rows += 1
                        logger.info(f"✅ Cruz de seguridad creada para {mes}/{anio}")
                except Exception as e:
                    warnings["cruz_seguridad"] = f"No se pudo crear cruz de seguridad: {e}"

            # Resumen final
            stats = {
                "dias_procesados": dias_procesados,
                "estados_creados": estados_creados,
                "reflexion_creada": reflexion_creada,
                "mes_procesado": mes,
                "anio_procesado": anio,
                "hallazgos": hallazgos,
                "tarjeta_verde": tarjeta_verde,
                "policlinico": policlinico,
            }
            
            if not dry_run:
                logger.info(f"✅ SSO procesado: {dias_procesados} días, {estados_creados} estados, reflexión: {reflexion_creada}")
            else:
                logger.info(f"🔶 DRY RUN - SSO: {dias_procesados} días, reflexión: {reflexion_creada}")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de SSO: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _get_fecha_desde_pod(self, pod: Any, dia_num: int, mes: int, anio: int) -> Optional[date]:
        """Obtiene la fecha considerando el contexto del POD"""
        try:
            # Intentar usar el mes y año del transformer, con el día específico
            return date(anio, mes, dia_num)
        except ValueError as e:
            logger.warning(f"Fecha inválida {dia_num}/{mes}/{anio}: {e}")
            return None

# Registro en el sistema de loaders
def register_loader():
    from etl.registry import register_loader
    register_loader("sso")(SsoLoader)