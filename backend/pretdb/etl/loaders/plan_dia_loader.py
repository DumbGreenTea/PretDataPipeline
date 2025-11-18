# pretdb/etl/loaders/plan_dia_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import logging
from datetime import date, datetime

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

def _to_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val)))
    except (TypeError, ValueError):
        return None

def _truncate(text: Optional[str], maxlen: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    return s[:maxlen] if len(s) > maxlen else s


@register_loader("plan_dia")
class PlanDiaLoader:
    """
    Carga datos de Plan del Día desde el transformer
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.PlanDia = _get_model(pretdb, "PlanDia")
        self.HorarioInicio = _get_model(pretdb, "HorarioInicio")
        self.PlanDiaRestricciones = _get_model(pretdb, "PlanDiaRestricciones")
        self.TipoGrupo = _get_model(pretdb, "TipoGrupo")
        self.GrupoActividadesDia = _get_model(pretdb, "GrupoActividadesDia")
        self.PlanDiaGrupo = _get_model(pretdb, "PlanDiaGrupo")

        self._grupo_nombre_por_tipo = {
            "realizable": "Plan Dia - Realizables",
            "no_realizable": "Plan Dia - No Realizables",
            "colchon": "Plan Dia - Colchon",
        }

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
                # Determinar tipo de actividad basado en el flag del transformer
                realizable_flag = actividad_data.get("realizable")
                tipo_actividad = actividad_data.get("tipo_actividad") or self._determinar_tipo_actividad(realizable_flag)
                
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
                        id_p6=_truncate(actividad_data.get("id_p6"), 50),
                        descripcion_item=_truncate(actividad_data.get("descripcion_item"), 150),
                        defaults={
                            'fecha': _to_date(actividad_data.get("fecha")) or pod.fecha,
                            'numero_pod': _to_int(actividad_data.get("nro_pod")),
                            'tipo_plan': _truncate(actividad_data.get("tipo_tc"), 30),
                            'responsable': _truncate(actividad_data.get("responsable"), 100),
                            'ruta_critica': _truncate(actividad_data.get("ruta_critica"), 100),
                            'area_trabajo': _truncate(actividad_data.get("area_trabajo"), 100),
                            'unidad': _truncate(actividad_data.get("unidad"), 10),
                            'cantidad_programada': _to_float(actividad_data.get("cant_programada")),
                            'cantidad_proyectada_dia': _to_float(actividad_data.get("cant_proyectada_dia")),
                            'riesgos_criticos': _truncate(actividad_data.get("riesgos_criticos"), 200),
                            'tipo_actividad': _truncate(tipo_actividad, 30) if tipo_actividad else None,
                        }
                    )
                    if created:
                        actividades_procesadas += 1
                        total_rows += 1

                    self._attach_grupo_plan_dia(
                        actividad_obj,
                        tipo_actividad,
                        warnings,
                    )
                    self._persist_restriccion(
                        actividad_obj,
                        actividad_data.get("restricciones"),
                        actividad_data.get("restricciones_resp"),
                        warnings,
                    )
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
        if realizable is None:
            return "indeterminado"
        # Maneja texto como "realizable", "colchon", etc.
        if isinstance(realizable, str):
            text = realizable.strip().lower()
            if text in ("realizable", "r", "si", "sí", "s"):
                return "realizable"
            if text in ("colchon", "colchón", "c"):
                return "colchon"
            if text in ("no_realizable", "no realizable", "n", "no"):
                return "no_realizable"
            try:
                realizable = float(text)
            except ValueError:
                return "indeterminado"
        try:
            flag = int(round(float(realizable)))
        except (TypeError, ValueError):
            return "indeterminado"
        if flag == 1:
            return "realizable"
        if flag == 2:
            return "colchon"
        if flag == 0:
            return "no_realizable"
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

    def _persist_restriccion(
        self,
        plan_dia_obj: Any,
        restriccion_texto: Optional[str],
        responsable: Optional[str],
        warnings: Dict[str, Any],
    ) -> None:
        """Guarda las restricciones asociadas a la actividad si hay modelo disponible."""
        if not self.PlanDiaRestricciones or not plan_dia_obj:
            return
        if not restriccion_texto:
            return
        try:
            self.PlanDiaRestricciones.objects.update_or_create(
                plan_dia=plan_dia_obj,
                descripcion=_truncate(restriccion_texto, 200),
                defaults={"tipo": _truncate(responsable, 50)},
            )
        except Exception as exc:
            warnings.setdefault("restricciones", []).append(str(exc))

    def _attach_grupo_plan_dia(
        self,
        plan_dia_obj: Any,
        tipo_actividad: str,
        warnings: Dict[str, Any],
    ) -> None:
        """Asocia la actividad al grupo correspondiente según su tipo."""
        if not plan_dia_obj or tipo_actividad not in self._grupo_nombre_por_tipo:
            return
        if not (self.TipoGrupo and self.GrupoActividadesDia and self.PlanDiaGrupo):
            return
        nombre_tipo = self._grupo_nombre_por_tipo[tipo_actividad]
        try:
            tipo_obj, _ = self.TipoGrupo.objects.get_or_create(nombre=nombre_tipo)
            grupo_obj, _ = self.GrupoActividadesDia.objects.get_or_create(tipo_grupo=tipo_obj)
            self.PlanDiaGrupo.objects.get_or_create(plan_dia=plan_dia_obj, grupo=grupo_obj)
        except Exception as exc:
            warnings.setdefault("grupos", []).append(str(exc))
