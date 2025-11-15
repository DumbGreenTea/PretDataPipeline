from .registry import (
    LOADER_REGISTRY,
    available_loaders,
    create_loader,
    get_loader_class,
    register_loader,
)

# Importar módulos concretos para que se registren al importarse
from . import asistencia_loader  # noqa: F401
from . import portada_loader  # noqa: F401

__all__ = [
    "LOADER_REGISTRY",
    "available_loaders",
    "create_loader",
    "get_loader_class",
    "register_loader",
]
