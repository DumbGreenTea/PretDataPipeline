from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

TransformerCls = Type[Any]

TRANSFORMER_REGISTRY: Dict[str, TransformerCls] = {}


def register_transformer(sheet_key: str):
    """
    Decorador para mantener un registro centralizado de transformers disponibles.
    """

    def decorator(transformer_cls: TransformerCls) -> TransformerCls:
        TRANSFORMER_REGISTRY[sheet_key] = transformer_cls
        return transformer_cls

    return decorator


def get_transformer_class(sheet_key: str) -> Optional[TransformerCls]:
    return TRANSFORMER_REGISTRY.get(sheet_key)


def create_transformer(sheet_key: str) -> Optional[Any]:
    cls = get_transformer_class(sheet_key)
    return cls() if cls else None


def available_transformers() -> List[str]:
    return sorted(TRANSFORMER_REGISTRY.keys())
