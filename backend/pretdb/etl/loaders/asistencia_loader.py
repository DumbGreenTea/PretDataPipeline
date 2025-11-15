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

def _get_or_create_trabajador(
    nombre: str,
    codigo: Optional[str],
    empresa: Optional[str],
    Trabajador_model,
    Contratista_model,
):
    """Busca o crea un Trabajador usando los campos reales del modelo."""
    if not nombre or not Trabajador_model:
        return None

    nombre_limpio = nombre.strip()
    codigo_limpio = codigo.strip() if codigo else None
    empresa_limpia = (empresa or "").strip() or None

    if not nombre_limpio:
        return None

    try:
        if codigo_limpio:
            query = {"codigo_trabajador": codigo_limpio}
            if empresa_limpia:
                query["empresa"] = empresa_limpia
            trabajador = Trabajador_model.objects.filter(**query).first()
            if trabajador:
                logger.debug(
                    "✅ Trabajador encontrado por código %s (%s)",
                    codigo_limpio,
                    nombre_limpio,
                )
                return trabajador

            defaults = {"nombre": nombre_limpio}
            if empresa_limpia is not None:
                defaults["empresa"] = empresa_limpia

            trabajador = Trabajador_model.objects.create(
                codigo_trabajador=codigo_limpio,
                **defaults,
            )
            logger.info("✅ Trabajador creado: %s (%s)", nombre_limpio, codigo_limpio)
            return trabajador

        # Sin código no podemos crear (campo requerido). Intentamos buscar por nombre.
        trabajador = Trabajador_model.objects.filter(nombre=nombre_limpio).first()
        if trabajador:
            logger.debug("✅ Trabajador encontrado por nombre: %s", nombre_limpio)
        else:
            logger.warning(
                "No se puede crear trabajador sin código: %s (empresa=%s)",
                nombre_limpio,
                empresa_limpia or "N/D",
            )
        return trabajador

    except Exception as e:
        logger.error("❌ Error creando/buscando trabajador %s: %s", nombre_limpio, e)
        return None


def _truncate(value: Optional[str], maxlen: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:maxlen] if len(text) > maxlen else text


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
            contrato_obj = self._get_or_create_contrato(pod, contrato_data)

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

    def _get_or_create_contrato(self, pod: Any, contrato_data: Dict[str, Any]) -> Optional[Any]:
        """Busca o crea un contrato basado en los datos del transformer"""
        if not self.Contrato or not pod:
            return None
            
        contrato_num = contrato_data.get("numero")
        contrato_nombre = contrato_data.get("nombre")
        codigo_limpio = _truncate(str(contrato_num).strip(), 20) if contrato_num else None
        nombre_limpio = _truncate((contrato_nombre or "").strip(), 120) or None
        
        if not codigo_limpio and not nombre_limpio:
            return None
            
        try:
            filters = {"pod": pod}
            if codigo_limpio:
                filters["nombre_codigo"] = codigo_limpio
            if nombre_limpio:
                filters["nombre_contrato"] = nombre_limpio

            defaults = {}
            if codigo_limpio:
                defaults["nombre_codigo"] = codigo_limpio
            if nombre_limpio or codigo_limpio:
                defaults["nombre_contrato"] = nombre_limpio or (f"Contrato {codigo_limpio}" if codigo_limpio else None)

            contrato, created = self.Contrato.objects.get_or_create(
                **filters,
                defaults=defaults,
            )

            if created:
                logger.info(
                    "✅ Contrato creado: %s / %s",
                    contrato.nombre_codigo or "sin código",
                    contrato.nombre_contrato or "sin nombre",
                )
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
            return None

        # Actualizar metadata básica si cambió
        updated = False
        if cargo and getattr(trabajador, "cargo", None) != cargo:
            trabajador.cargo = _truncate(cargo, 20)
            updated = True
        if empresa and getattr(trabajador, "empresa", None) != empresa:
            trabajador.empresa = _truncate(empresa, 50)
            updated = True
        if updated:
            trabajador.save(update_fields=["cargo", "empresa"])

        return trabajador

    def _process_asistencia_dia(
        self, 
        pod: Any, 
        trabajador: Any, 
        fecha: date, 
        horas: float, 
        dry_run: bool = False
    ) -> int:
        """Procesa un registro de asistencia para un día específico"""
        
        if dry_run:
            logger.debug(f"🔶 DRY RUN - Asistencia: {trabajador.nombre} - {fecha} - {horas} hrs")
            return 1
            
        try:
            if not self.Asistencia or not self.AsistenciaDetalle:
                return 0

            asistencia, _ = self.Asistencia.objects.get_or_create(
                pod=pod,
                trabajador=trabajador,
            )

            defaults = {
                "dia_nombre": fecha.strftime("%A"),
                "presente": self._estado_from_horas(horas),
            }
            detalle, _ = self.AsistenciaDetalle.objects.update_or_create(
                asistencia=asistencia,
                fecha=fecha,
                defaults=defaults,
            )
                
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

    def _estado_from_horas(self, horas: Any) -> Optional[int]:
        if horas is None:
            return None
        try:
            cantidad = float(horas)
        except (TypeError, ValueError):
            return None
        if cantidad <= 0:
            return 0
        return 1
