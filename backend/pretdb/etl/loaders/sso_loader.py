# pretdb/etl/loaders/sso_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime
from calendar import monthrange, month_name

import pandas as pd
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

def _mes_nombre(mes: Optional[int]) -> Optional[str]:
    if mes is None:
        return None
    try:
        mes_int = int(mes)
    except (TypeError, ValueError):
        return None
    if 1 <= mes_int <= 12:
        return month_name[mes_int].title()
    return None


@register_loader("sso")
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
        summary = out.get("summary", {}) or {}
        dias_detail = out.get("dias_detail", [])
        mes = summary.get("mes")
        anio = summary.get("anio")
        mes_nombre = summary.get("mes_nombre")

        fallback_mes = False
        if (not mes or not anio) and getattr(pod, "fecha", None):
            fecha_pod = pod.fecha
            mes = mes or fecha_pod.month
            anio = anio or fecha_pod.year
            fallback_mes = True
        if not mes or not anio:
            warnings["datos_incompletos"] = "Faltan mes o año en los datos de SSO"
            return 0, warnings, None

        mes_nombre = mes_nombre or _mes_nombre(mes)
        if not fallback_mes and getattr(pod, "fecha", None) and pod.fecha.month != mes:
            warnings.setdefault("mes_incongruente", []).append(
                f"Hoja indica {mes_nombre or mes}, pero POD es {pod.fecha.strftime('%m/%Y')}."
            )
        if fallback_mes:
            warnings.setdefault("mes_inferido", []).append(
                f"Mes/año inferidos desde POD: {mes_nombre or mes}/{anio}"
            )
        summary["mes"] = mes
        summary["anio"] = anio
        summary["mes_nombre"] = mes_nombre

        try:
            fecha_inicio = date(anio, mes, 1)
            _, fecha_fin = _month_bounds(anio, mes)
        except ValueError as exc:
            warnings["fechas_invalidas"] = str(exc)
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

            cruz_obj = None
            if not dry_run and self.SsoCruzSeguridad and sso_obj:
                cruz_obj, _ = self.SsoCruzSeguridad.objects.update_or_create(
                    sso=sso_obj,
                    defaults={
                        "fecha_inicio": fecha_inicio,
                        "fecha_fin": fecha_fin,
                    },
                )

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
                    if not self.SsoEstadoDia or not cruz_obj:
                        continue
                    try:
                        estado_val = self._estado_to_choice(estado_color)
                        estado_obj, created = self.SsoEstadoDia.objects.update_or_create(
                            sso_cruz_seguridad=cruz_obj,
                            fecha=fecha_dia,
                            defaults={
                                "estado_dia": estado_val,
                            },
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

            # Resumen final
            stats = {
                "dias_procesados": dias_procesados,
                "estados_creados": estados_creados,
                "reflexion_creada": reflexion_creada,
                "mes_procesado": mes,
                "mes_nombre": mes_nombre,
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

    def _estado_to_choice(self, estado: Optional[str]) -> Optional[int]:
        if estado is None:
            return None
        if isinstance(estado, int):
            if estado in (0, 1, 2):
                return estado
        normalized = str(estado).strip().lower()
        if normalized.startswith("verd"):
            return 0
        if normalized.startswith("amar"):
            return 1
        if normalized.startswith("roj"):
            return 2
        return None
def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    """Devuelve el primer y último día del mes dado."""
    first = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    last = date(year, month, last_day)
    return first, last
