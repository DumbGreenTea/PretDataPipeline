# pretdb/etl/transformers/registry.py
from .portada_transformer import PortadaTransformer
from pretdb.etl.transformers.asistencia_transformer import AsistenciaTransformer 
from .sso_transformer import SSOTransformer
from .plan_anterior_transformer import PlanAnteriorTransformer
from .plan_dia_transformer import PlanDiaTransformer
# from .asistencia_transformer import AsistenciaTransformer  # cuando lo tengas

REGISTRY = {
    "portada": PortadaTransformer(),
    "asistencia": AsistenciaTransformer(),
    "sso": SSOTransformer(),
    "plan_anterior": PlanAnteriorTransformer(),
    "plan_dia": PlanDiaTransformer(),
    # "asistencia": AsistenciaTransformer(),
}

