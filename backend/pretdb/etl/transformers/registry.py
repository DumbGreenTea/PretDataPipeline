# pretdb/etl/transformers/registry.py
from .portada_transformer import PortadaTransformer
from pretdb.etl.transformers.asistencia_transformer import AsistenciaTransformer 
# from .asistencia_transformer import AsistenciaTransformer  # cuando lo tengas

REGISTRY = {
    "portada": PortadaTransformer(),
    "asistencia": AsistenciaTransformer(),
    # "asistencia": AsistenciaTransformer(),
}
