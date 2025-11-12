from __future__ import annotations
from typing import Callable, Dict, Any
from pretdb.models import Pod

from .plan_dia_loader import persist_plan_dia
from .plan_anterior_loader import PlanAnteriorLoader
from .sso_loader import SsoLoader
from .ppc_loader import PPCLoader
from .matriz_cnc_loader import MatrizCNCLoader
from .asistencia_loader import AsistenciaLoader
from .maquinaria_loader import DotacionMaquinariaLoader
from .compromiso_loader import CompromisosLoader
from .portada_loader import PortadaLoader

# Mapa: sheet_key -> función loader(pod, out, dry_run) -> stats
LOADER_REGISTRY: Dict[str, Callable[[Pod, Dict[str, Any], bool], Dict[str, Any]]] = {
    "plan_dia": persist_plan_dia,
    "plan_anterior": PlanAnteriorLoader().persist,
    "sso": SsoLoader().persist,
    "ppc": PPCLoader().persist,
    "matriz_cnc": MatrizCNCLoader().persist,
    "asistencia": AsistenciaLoader().persist,
    "dotacion_y_maquinaria": DotacionMaquinariaLoader().persist,
    "compromisos": CompromisosLoader().persist,
    "portada": PortadaLoader().persist,
    # Cuando implementes los demás, los registras así:
    # "compromisos": persist_compromisos,
    # "matriz_cnc": persist_matriz_cnc,
    # "dotacion_y_maquinaria": persist_dotacion_maquinaria,
    # "asistencia": persist_asistencia,
    # "plan_anterior": persist_plan_anterior,
    # "ppc": persist_ppc,
    # "sso": persist_sso,
}