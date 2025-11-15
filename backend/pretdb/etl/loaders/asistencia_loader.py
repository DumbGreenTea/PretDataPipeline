# pretdb/etl/loaders/asistencia_loader.py
from __future__ import annotations
from typing import Dict, Any, List, Optional
import logging
from datetime import date, datetime
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

def _get_or_create_trabajador(nombre: str, codigo: str, empresa: str, Trabajador_model, Contratista_model):
    """Busca o crea un Trabajador por nombre y código"""
    if not nombre or not Trabajador_model:
        return None
    
    nombre_limpio = nombre.strip()
    codigo_limpio = codigo.strip() if codigo else None
    
    if not nombre_limpio:
        return None
    
    try:
        # Buscar por código primero (si existe)
        if codigo_limpio:
            try:
                trabajador = Trabajador_model.objects.get(codigo=codigo_limpio)
                logger.debug(f"✅ Trabajador encontrado por código: {codigo_limpio} - {nombre_limpio}")
                return trabajador
            except Trabajador_model.DoesNotExist:
                pass
        
        # Buscar por nombre
        if codigo_limpio:
            trabajador, created = Trabajador_model.objects.get_or_create(
                codigo=codigo_limpio,
                defaults={
                    'nombre': nombre_limpio,
                    'empresa': empresa
                }
            )
        else:
            # Si no hay código, buscar solo por nombre
            trabajador, created = Trabajador_model.objects.get_or_create(
                nombre=nombre_limpio,
                defaults={'empresa': empresa}
            )
        
        if created:
            logger.info(f"✅ Trabajador creado: {nombre_limpio} ({codigo_limpio})")
        else:
            logger.debug(f"✅ Trabajador encontrado: {nombre_limpio}")
        
        return trabajador
        
    except Exception as e:
        logger.error(f"❌ Error creando/buscando trabajador {nombre_limpio}: {e}")
        return None


@register_loader("asistencia")
@register_loader("asistencia")
class AsistenciaLoader:
    """
    Carga datos de asistencia desde el transformer al modelo Asistencia y AsistenciaDetalle
    """
    
    def __init__(self) -> None:
        pretdb = "pretdb"
        self.Pod = _get_model(pretdb, "Pod")
        self.Trabajador = _get_model(pretdb, "Trabajador")
        self.Asistencia = _get_model(pretdb, "Asistencia")
        self.AsistenciaDetalle = _get_model(pretdb, "AsistenciaDetalle")
        self.Contrato = _get_model(pretdb, "Contrato")
        self.Contratista = _get_model(pretdb, "Contratista")

    @transaction.atomic
    def persist(
        self,
        pod,
        out: dict,
        *,
        dry_run: bool = False,
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Persiste los datos de asistencia y retorna estadísticas homogéneas para el orquestador.
        """
        warnings: Dict[str, List[str]] = {}

        # Extraer datos del transformer
        persons_detail = out.get("persons_detail", [])
        dates_map = out.get("dates_map", {})
        contrato_data = out.get("contrato", {})

        stats = self._init_stats(pod, dates_map, dry_run)

        if not self.Pod or not self.Asistencia or not self.AsistenciaDetalle or not self.Trabajador:
            msg = "Modelos requeridos no encontrados"
            logger.error(msg)
            stats["error"] = msg
            return stats

        if not pod or not isinstance(pod, self.Pod):
            msg = "Pod no proporcionado o inválido"
            logger.error(msg)
            stats["error"] = msg
            return stats

        if not persons_detail:
            warnings["empty_data"] = ["No hay datos de personas para procesar"]
            stats["warnings"] = self._flatten_warnings(warnings)
            stats["skipped"] = 1
            return stats

        # Buscar o crear contrato si existe la información
        contrato_obj = None
        if contrato_data and self.Contrato:
            contrato_obj = self._get_or_create_contrato(contrato_data)

        # Estadísticas
        total_rows = 0
        trabajadores_procesados = 0
        registros_asistencia_creados = 0

        try:
            # Procesar cada persona
            for person_data in persons_detail:
                trabajador = self._process_trabajador(person_data, warnings)
                if not trabajador:
                    continue
                
                trabajadores_procesados += 1
                
                # Procesar asistencia por día
                for dia_key, horas in person_data.items():
                    if dia_key not in ["codigo", "nombre", "cargo", "empresa", "correo"]:
                        fecha = dates_map.get(dia_key)
                        if fecha and horas is not None:
                            rows_created = self._process_asistencia_dia(
                                pod=pod,
                                trabajador=trabajador,
                                fecha=fecha,
                                horas=horas,
                                contrato=contrato_obj,
                                dry_run=dry_run
                            )
                            if rows_created:
                               registros_asistencia_creados += rows_created
                               total_rows += rows_created

            # Resumen final
            stats["rows"] = total_rows
            stats["trabajadores_procesados"] = trabajadores_procesados
            stats["registros_asistencia_creados"] = registros_asistencia_creados
            stats["dias_procesados"] = len(dates_map)
            stats["contrato_asociado"] = bool(contrato_obj)
            stats["warnings"] = self._flatten_warnings(warnings)

            if not dry_run:
                logger.info(f"✅ Asistencia procesada: {trabajadores_procesados} trabajadores, "
                           f"{registros_asistencia_creados} registros de asistencia")
            else:
                logger.info(f"🔶 DRY RUN - Asistencia: {trabajadores_procesados} trabajadores, "
                           f"{registros_asistencia_creados} registros de asistencia (simulados)")

            return stats

        except Exception as e:
            logger.exception("Error en persistencia de asistencia: %s", e)
            stats["warnings"] = self._flatten_warnings(warnings)
            stats["error"] = str(e)
            return stats

    def _get_or_create_contrato(self, contrato_data: Dict[str, Any]) -> Optional[Any]:
        """Busca o crea un contrato basado en los datos del transformer"""
        if not self.Contrato:
            return None
            
        contrato_num = contrato_data.get("numero")
        contrato_nombre = contrato_data.get("nombre")
        
        if not contrato_num and not contrato_nombre:
            return None
            
        try:
            if contrato_num:
                contrato, created = self.Contrato.objects.get_or_create(
                    codigo=contrato_num,
                    defaults={'nombre': contrato_nombre or f"Contrato {contrato_num}"}
                )
            else:
                contrato, created = self.Contrato.objects.get_or_create(
                    nombre=contrato_nombre
                )
                
            if created:
                logger.info(f"✅ Contrato creado: {contrato.codigo or contrato.nombre}")
            return contrato
            
        except Exception as e:
            logger.error(f"❌ Error creando contrato: {e}")
            return None

    def _process_trabajador(self, person_data: Dict[str, Any], warnings: Dict[str, Any]) -> Optional[Any]:
        """Procesa y busca/crea un trabajador"""
        nombre = person_data.get("nombre")
        codigo = person_data.get("codigo")
        empresa = person_data.get("empresa", "")
        cargo = person_data.get("cargo", "")
        
        if not nombre:
            warnings.setdefault("sin_nombre", []).append(str(codigo or "Sin código"))
            return None
            
        trabajador = _get_or_create_trabajador(
            nombre=nombre,
            codigo=codigo,
            empresa=empresa,
            Trabajador_model=self.Trabajador,
            Contratista_model=self.Contratista
        )
        
        if not trabajador:
            warnings.setdefault("trabajador_no_creado", []).append(f"{nombre} ({codigo})")
            
        return trabajador

    def _process_asistencia_dia(
        self, 
        pod: Any, 
        trabajador: Any, 
        fecha: date, 
        horas: float, 
        contrato: Optional[Any] = None,
        dry_run: bool = False
    ) -> int:
        """Procesa un registro de asistencia para un día específico"""
        
        if dry_run:
            logger.debug(f"🔶 DRY RUN - Asistencia: {trabajador.nombre} - {fecha} - {horas} hrs")
            return 1
            
        try:
            # Buscar o crear registro de asistencia principal
            asistencia, created = self.Asistencia.objects.get_or_create(
                pod=pod,
                trabajador=trabajador,
                fecha=fecha,
                defaults={
                    'horas_trabajadas': horas,
                    'contrato': contrato
                }
            )
            
            if not created:
                # Actualizar horas si el registro ya existe
                asistencia.horas_trabajadas = horas
                if contrato:
                    asistencia.contrato = contrato
                asistencia.save()
            
            # Crear detalle de asistencia
            detalle, det_created = self.AsistenciaDetalle.objects.get_or_create(
                asistencia=asistencia,
                defaults={
                    'horas_registradas': horas,
                    'estado': 'PRESENTE' if horas > 0 else 'AUSENTE'
                }
            )
            
            if not det_created:
                detalle.horas_registradas = horas
                detalle.estado = 'PRESENTE' if horas > 0 else 'AUSENTE'
                detalle.save()
                
            return 1
            
        except Exception as e:
            logger.error(f"❌ Error procesando asistencia para {trabajador.nombre} - {fecha}: {e}")
            return 0

    def _init_stats(self, pod: Optional[Any], dates_map: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
        return {
            "mode": "dry" if dry_run else "write",
            "pod_id": getattr(pod, "id", None),
            "pod_repr": str(pod) if pod else None,
            "rows": 0,
            "trabajadores_procesados": 0,
            "registros_asistencia_creados": 0,
            "dias_procesados": len(dates_map or {}),
            "contrato_asociado": False,
            "warnings": [],
            "skipped": 0,
        }

    def _flatten_warnings(self, warnings: Dict[str, Any]) -> List[str]:
        flat: List[str] = []
        for key, value in warnings.items():
            if isinstance(value, list):
                for item in value:
                    flat.append(f"{key}: {item}")
            else:
                flat.append(f"{key}: {value}")
        return flat
