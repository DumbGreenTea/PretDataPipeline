from .registry import (
    LOADER_REGISTRY,
    available_loaders,
    create_loader,
    get_loader_class,
    register_loader,
)

# Importar módulos concretos para que se registren al importarse
from . import asistencia_loader  # noqa: F401
from . import compromisos_loader  # noqa: F401
from . import maquinaria_loader  # noqa: F401
from . import matriz_cnc_loader  # noqa: F401
from . import plan_anterior_loader  # noqa: F401
from . import plan_dia_loader  # noqa: F401
from . import ppc_loader  # noqa: F401
from . import portada_loader  # noqa: F401
from . import sso_loader  # noqa: F401

__all__ = [
    "LOADER_REGISTRY",
    "available_loaders",
    "create_loader",
    "get_loader_class",
    "register_loader",
]
