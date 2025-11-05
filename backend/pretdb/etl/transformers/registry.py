# pretdb/etl/transformers/registry.py
from .portada_transformer import PortadaTransformer
from pretdb.etl.transformers.asistencia_transformer import AsistenciaTransformer 
from .sso_transformer import SSOTransformer
from .plan_anterior_transformer import PlanAnteriorTransformer
# from .asistencia_transformer import AsistenciaTransformer  # cuando lo tengas

REGISTRY = {
    "portada": PortadaTransformer(),
    "asistencia": AsistenciaTransformer(),
    "sso": SSOTransformer(),
    "plan_anterior": PlanAnteriorTransformer(),
    # "asistencia": AsistenciaTransformer(),
}

