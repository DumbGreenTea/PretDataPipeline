from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

LoaderCls = Type[Any]

# Diccionario global: sheet_key -> clase del loader
LOADER_REGISTRY: Dict[str, LoaderCls] = {}


def register_loader(sheet_key: str):
    """
    Decorador para registrar loaders sin acoplarlos al orquestador.
    """

    def decorator(loader_cls: LoaderCls) -> LoaderCls:
        LOADER_REGISTRY[sheet_key] = loader_cls
        return loader_cls

    return decorator


def get_loader_class(sheet_key: str) -> Optional[LoaderCls]:
    return LOADER_REGISTRY.get(sheet_key)


def create_loader(sheet_key: str) -> Optional[Any]:
    cls = get_loader_class(sheet_key)
    return cls() if cls else None


def available_loaders() -> List[str]:
    return sorted(LOADER_REGISTRY.keys())
