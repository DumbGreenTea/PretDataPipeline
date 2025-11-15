# pretdb/etl/loaders/plan_dia_loader.py
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

def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and not pd.isna(val):
        return float(val)
    try:
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return None


class PlanDiaLoader:
    """
    Carga datos de Plan del Día desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.PlanDia = _get_model(pretdb, "PlanDia")
        self.HorarioInicio = _get_model(pretdb, "HorarioInicio")

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
        Persiste los datos de Plan del Día
        
        Returns:
            Tuple[int, Dict, Optional[Dict]]: (rows_upserted, warnings, errors)
        """
        warnings = {}
        errors = None
        
        if not self.Pod or not self.PlanDia:
            error_msg = "Modelos Pod o PlanDia no encontrados"
            logger.error(error_msg)
            return 0, {}, {"models": error_msg}

        if not pod or not isinstance(pod, self.Pod):
            error_msg = "Pod no proporcionado o inválido"
            logger.error(error_msg)
            return 0, {}, {"pod": error_msg}

        # Extraer datos del transformer
        flat_rows = out.get("flat_rows", [])
        horarios_inicio = out.get("horarios_inicio", [])
        summary = out.get("summary", {})
        
        if not flat_rows and not horarios_inicio:
            warnings["sin_datos"] = "No hay datos de Plan del Día para procesar"
            return 0, warnings, None

        # Estadísticas
        total_rows = 0
        actividades_procesadas = 0
        horarios_procesados = 0
        realizables_count = 0
        no_realizables_count = 0
        colchon_count = 0

        try:
            # 1. Procesar actividades del plan del día
            for actividad_data in flat_rows:
                # Determinar tipo de actividad basado en realizable
                realizable = actividad_data.get("realizable")
                tipo_actividad = self._determinar_tipo_actividad(realizable)
                
                if tipo_actividad == "realizable":
                    realizables_count += 1
                elif tipo_actividad == "no_realizable":
                    no_realizables_count += 1
                elif tipo_actividad == "colchon":
                    colchon_count += 1
                
                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Actividad: {actividad_data.get('descripcion_item')} - {tipo_actividad}")
                    actividades_procesadas += 1
                    continue
                
                # Crear registro de actividad
                try:
                    actividad_obj, created = self.PlanDia.objects.update_or_create(
                        pod=pod,
                        id_p6=actividad_data.get("id_p6"),
                        descripcion_item=actividad_data.get("descripcion_item"),
                        defaults={
                            'fecha': _to_date(actividad_data.get("fecha")) or pod.fecha,
                            'nro_pod': actividad_data.get("nro_pod"),
                            'tipo_tc': actividad_data.get("tipo_tc"),
                            'responsable': actividad_data.get("responsable"),
                            'ruta_critica': actividad_data.get("ruta_critica"),
                            'area_trabajo': actividad_data.get("area_trabajo"),
                            'unidad': actividad_data.get("unidad"),
                            'cant_programada': _to_float(actividad_data.get("cant_programada")),
                            'cant_proyectada_dia': _to_float(actividad_data.get("cant_proyectada_dia")),
                            'riesgos_criticos': actividad_data.get("riesgos_criticos"),
                            'restricciones': actividad_data.get("restricciones"),
                            'restricciones_resp': actividad_data.get("restricciones_resp"),
                            'tipo_actividad': tipo_actividad,
                            'realizable': realizable,
                        }
                    )
                    if created:
                        actividades_procesadas += 1
                        total_rows += 1
                except Exception as e:
                    descripcion = actividad_data.get('descripcion_item', 'Sin descripción')
                    warnings.setdefault("actividades", []).append(f"'{descripcion}': {e}")

            # 2. Procesar horarios de inicio
            for horario_data in horarios_inicio:
                frente = horario_data.get("frente")
                hora_inicio = horario_data.get("hora_inicio")
                
                if not frente and not hora_inicio:
                    continue
                    
                if dry_run:
                    logger.debug(f"🔶 DRY RUN - Horario: {frente} - {hora_inicio}")
                    horarios_procesados += 1
                    continue
                
                if self.HorarioInicio:
                    try:
                        horario_obj, created = self.HorarioInicio.objects.update_or_create(
                            pod=pod,
                            frente=frente,
                            defaults={
                                'hora_inicio': hora_inicio,
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
                "horarios_procesados": horarios_procesados,
                "realizables": realizables_count,
                "no_realizables": no_realizables_count,
                "colchon": colchon_count,
                "total_actividades": len(flat_rows),
                "total_horarios": len(horarios_inicio),
            }
            
            if not dry_run:
                logger.info(f"✅ PlanDia procesado: {actividades_procesadas} actividades, {horarios_procesados} horarios")
                logger.info(f"   - Realizables: {realizables_count}, No realizables: {no_realizables_count}, Colchón: {colchon_count}")
            else:
                logger.info(f"🔶 DRY RUN - PlanDia: {actividades_procesadas} actividades, {horarios_procesados} horarios")

            return total_rows, {**warnings, **stats}, None

        except Exception as e:
            logger.exception("Error en persistencia de PlanDia: %s", e)
            return 0, warnings, {"persist": str(e)}

    def _determinar_tipo_actividad(self, realizable: Optional[int]) -> str:
        """
        Determina el tipo de actividad basado en el valor de 'realizable'
        """
        if realizable == 1:
            return "realizable"
        elif realizable == 2:
            return "colchon"
        elif realizable == 0:
            return "no_realizable"
        else:
            return "indeterminado"

    def _crear_resumen_plan_dia(self, pod: Any, stats: Dict[str, Any]) -> None:
        """Crea un registro de resumen del plan del día"""
        try:
            from pretdb.models import ResumenPlanDia
            resumen, created = ResumenPlanDia.objects.update_or_create(
                pod=pod,
                defaults={
                    'total_actividades': stats.get("total_actividades", 0),
                    'actividades_realizables': stats.get("realizables", 0),
                    'actividades_no_realizables': stats.get("no_realizables", 0),
                    'actividades_colchon': stats.get("colchon", 0),
                    'total_horarios': stats.get("total_horarios", 0),
                }
            )
            if created:
                logger.info(f"✅ Resumen PlanDia creado para POD {pod.numero_pod}")
        except Exception as e:
            logger.warning(f"No se pudo crear resumen de PlanDia: {e}")

# Registro en el sistema de loaders
def register_loader():
    from etl.registry import register_loader
    register_loader("plan_dia")(PlanDiaLoader)