"""Provider transport registry (vendored from Hermes, MIT). See NOTICE."""
from __future__ import annotations

from .types import NormalizedResponse, ToolCall  # noqa: F401

_REGISTRY: dict[str, type] = {}


def register_transport(api_mode: str, transport_cls: type) -> None:
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    if api_mode not in _REGISTRY:
        from . import anthropic_transport  # noqa: F401
        from . import codex_transport  # noqa: F401
    return _REGISTRY[api_mode]()
