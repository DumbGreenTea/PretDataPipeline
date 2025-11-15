# pretdb/etl/loaders/plan_anterior_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
import re
from datetime import date, datetime, time
from decimal import Decimal

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

def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    try:
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return None

def _to_decimal(val: Optional[float]) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except:
        return None

def _to_decimal_pct(val: Optional[float]) -> Optional[Decimal]:
    """Convierte porcentaje (0-100 o 0-1) a Decimal"""
    if val is None:
        return None
    try:
        v = float(val)
        if 0 <= v <= 1:  # Si viene como fracción (0.85)
            v *= 100.0
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except:
        return None

def _parse_time(s: Optional[str]) -> Optional[time]:
    """Parsea string de tiempo HH:MM a objeto time"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return time(hour=hh, minute=mm)
    return None

def _truncate(s: Optional[str], maxlen: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s[:maxlen] if len(s) > maxlen else s


@register_loader("plan_anterior")
class PlanAnteriorLoader:
    """
    Carga datos de Plan Anterior desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.PlanAnterior = _get_model(pretdb, "PlanAnterior")
        self.PlanAnteriorHoras = _get_model(pretdb, "PlanAnteriorHoras")
        self.PlanAnteriorCnc = _get_model(pretdb, "PlanAnteriorCnc")
        self.PlanAnteriorFront = _get_model(pretdb, "PlanAnteriorFront")

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
        Persiste los datos de Plan Anterior
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.PlanAnterior:
            error_msg = "Modelos Pod o PlanAnterior no encontrados"
            logger.error(error_msg)
            return 0, {}, {"models": error_msg}

        if not pod or not isinstance(pod, self.Pod):
            error_msg = "Pod no proporcionado o inválido"
            logger.error(error_msg)
            return 0, {}, {"pod": error_msg}

        # Extraer datos del transformer
        flat_rows = out.get("flat_rows", [])
        horarios_inicio = out.get("horarios_inicio", [])
        by_tag = out.get("by_tag", {})
        summary = out.get("summary", {})
        
        if not flat_rows and not horarios_inicio:
            warnings["sin_datos"] = "No hay datos de Plan Anterior para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        actividades_procesadas = 0
        cnc_procesados = 0
        horarios_procesados = 0
        realizadas_count = 0
        no_realizadas_count = 0

        try:
            # 1. Procesar actividades del plan anterior
            for actividad_data in flat_rows:
                # Determinar si es realizada o no
                realizada = actividad_data.get("realizada")
                if realizada is True:
                    realizadas_count += 1
                elif realizada is False:
                    no_realizadas_count += 1
                
                if dry_run:
                    descripcion = actividad_data.get('descripcion_item', 'Sin descripción')
                    logger.debug(f"🔶 DRY RUN - Actividad: {descripcion} - Realizada: {realizada}")
                    actividades_procesadas += 1
                    continue
                
                # Crear registro de actividad principal
                try:
                    actividad_obj, created = self.PlanAnterior.objects.update_or_create(
                        pod=pod,
                        id_p6=_truncate(actividad_data.get("id_p6"), 50),
                        area_trabajo=_truncate(actividad_data.get("area_trabajo"), 30),
                        descripcion_item=_truncate(actividad_data.get("descripcion_item"), 50),
                        fecha=_to_date(actividad_data.get("fecha")) or pod.fecha,
                        defaults={
                            'numero_pod': actividad_data.get("nro_pod"),
                            'tipo_plan': _truncate(actividad_data.get("tipo_tc"), 30),
                            'responsable': _truncate(actividad_data.get("responsable"), 100),
                            'unidad': _truncate(actividad_data.get("unidad"), 10),
                            'cantidad_programada': _to_decimal(actividad_data.get("cant_programada")),
                            'cantidad_real': _to_decimal(actividad_data.get("cant_real")),
                            'cumplimiento': _to_decimal_pct(actividad_data.get("cumplimiento")),
                            'realizada': realizada,
                        }
                    )
                    if created:
                        actividades_procesadas += 1
                        total_rows += 1

                    # 2. Procesar horas (datos semanales)
                    self._procesar_horas(actividad_obj, actividad_data, warnings)
                    
                    # 3. Procesar CNC si existe información
                    if self._tiene_datos_cnc(actividad_data):
                        cnc_creado = self._procesar_cnc(actividad_obj, actividad_data, warnings)
                        if cnc_creado:
                            cnc_procesados += 1
                            total_rows += 1
                            
                except Exception as e:
                    descripcion = actividad_data.get('descripcion_item', 'Sin descripción')
                    warnings.setdefault("actividades", []).append(f"'{descripcion}': {e}")

            # 4. Procesar horarios de inicio
            for horario_data in horarios_inicio:
                frente = horario_data.get("frente")
                hora_inicio = horario_data.get("hora_inicio")
                
                if not frente and not hora_inicio:
                    continue
                    
                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Horario: {frente} - {hora_inicio}")
                    horarios_procesados += 1
                    continue
                
                # Crear actividad holder para horarios
                holder_actividad = self._crear_actividad_holder_horarios(pod)
                if holder_actividad:
                    try:
                        horario_obj, created = self.PlanAnteriorFront.objects.update_or_create(
                            plan_anterior=holder_actividad,
                            frente=_truncate(frente, 50),
                            defaults={
                                'hora_inicio_efectivo': _parse_time(hora_inicio),
                                'comentario': None,
                            }
                        )
                        if created:
                            horarios_procesados += 1
                            total_rows += 1
                    except Exception as e:
                        warnings.setdefault("horarios", []).append(f"'{frente}': {e}")

            # Resumen final
            stats = {
                "actividades_procesadas": actividades_procesadas,
                "cnc_procesados": cnc_procesados,
                "horarios_procesados": horarios_procesados,
                "realizadas": realizadas_count,
                "no_realizadas": no_realizadas_count,
                "total_actividades": len(flat_rows),
                "total_horarios": len(horarios_inicio),
            }
            
            if not dry_run:
                logger.info(f"✅ PlanAnterior procesado: {actividades_procesadas} actividades, {cnc_procesados} CNC, {horarios_procesados} horarios")
                logger.info(f"   - Realizadas: {realizadas_count}, No realizadas: {no_realizadas_count}")
            else:
                logger.info(f"🔶 DRY RUN - PlanAnterior: {actividades_procesadas} actividades, {cnc_procesados} CNC, {horarios_procesados} horarios")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de PlanAnterior: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _procesar_horas(self, actividad_obj: Any, actividad_data: Dict[str, Any], warnings: Dict[str, Any]) -> None:
        """Procesa los datos de horas semanales"""
        try:
            horas_obj, created = self.PlanAnteriorHoras.objects.update_or_create(
                plan_anterior=actividad_obj,
                defaults={
                    'cantidad_programada': _to_decimal(actividad_data.get("sem_cant_prog") or actividad_data.get("cant_programada")),
                    'cantidad_real': _to_decimal(actividad_data.get("sem_cant_real") or actividad_data.get("cant_real")),
                    'horas_hombre_programadas': None,  # Estos campos podrían venir de otras fuentes
                    'horas_hombre_reales': None,
                    'porcentaje_avance': _to_decimal_pct(actividad_data.get("sem_avance") or actividad_data.get("cumplimiento")),
                }
            )
        except Exception as e:
            warnings.setdefault("horas", []).append(f"Actividad {actividad_obj.id}: {e}")

    def _tiene_datos_cnc(self, actividad_data: Dict[str, Any]) -> bool:
        """Verifica si hay datos de CNC para procesar"""
        campos_cnc = ["cnc_causa", "cnc_subcausa", "cnc_tipo", "cnc_descripcion", "cnc_responsable"]
        return any(actividad_data.get(campo) not in (None, "", "<NA>", "-") for campo in campos_cnc)

    def _procesar_cnc(self, actividad_obj: Any, actividad_data: Dict[str, Any], warnings: Dict[str, Any]) -> bool:
        """Procesa los datos de CNC"""
        try:
            cnc_obj, created = self.PlanAnteriorCnc.objects.update_or_create(
                plan_anterior=actividad_obj,
                causa=_truncate(actividad_data.get("cnc_causa"), 100),
                subcausa=_truncate(actividad_data.get("cnc_subcausa"), 100),
                tipo=_truncate(actividad_data.get("cnc_tipo"), 30),
                descripcion_cnc=_truncate(actividad_data.get("cnc_descripcion"), 200),
                defaults={
                    'responsable': _truncate(actividad_data.get("cnc_responsable"), 100),
                }
            )
            return created
        except Exception as e:
            warnings.setdefault("cnc", []).append(f"Actividad {actividad_obj.id}: {e}")
            return False

    def _crear_actividad_holder_horarios(self, pod: Any) -> Optional[Any]:
        """Crea una actividad holder para los horarios de inicio"""
        try:
            holder, created = self.PlanAnterior.objects.get_or_create(
                pod=pod,
                id_p6=None,
                area_trabajo=None,
                descripcion_item="Horario de inicio del día",
                fecha=pod.fecha,
                defaults={
                    'numero_pod': None,
                    'tipo_plan': None,
                    'responsable': None,
                    'unidad': None,
                    'cantidad_programada': None,
                    'cantidad_real': None,
                    'cumplimiento': None,
                    'realizada': None,
                }
            )
            return holder
        except Exception as e:
            logger.error(f"Error creando actividad holder para horarios: {e}")
            return None
