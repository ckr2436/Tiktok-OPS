# backend/app/services/provider_registry.py
from __future__ import annotations

"""Provider registry utilities used for sync orchestration."""

from typing import Dict, Iterable, Protocol


class ProviderHandler(Protocol):
    """Protocol describing provider specific sync handlers."""

    provider_id: str

    def validate_options(self, *, scope: str, options: dict) -> dict:
        """Validate and normalize sync options for the given scope."""

    async def run_scope(self, *, db, envelope: dict, scope: str, logger) -> dict:
        """Execute the sync scope and return structured stats."""


class ProviderRegistry:
    """In-memory registry for provider handlers."""

    def __init__(self) -> None:
        self._handlers: Dict[str, ProviderHandler] = {}

    def register(self, provider_id: str, handler: ProviderHandler) -> None:
        key = (provider_id or "").strip().lower()
        if not key:
            raise ValueError("provider_id is required")
        # 允许幂等注册：后注册覆盖同 key（避免重复导入报错）
        self._handlers[key] = handler

    def get(self, provider_id: str) -> ProviderHandler:
        key = (provider_id or "").strip().lower()
        if key not in self._handlers:
            raise KeyError(provider_id)
        return self._handlers[key]

    def list(self) -> Iterable[tuple[str, ProviderHandler]]:
        return tuple(self._handlers.items())


provider_registry = ProviderRegistry()


def load_builtin_providers() -> None:
    """
    Ensure bundled providers are registered.

    通过 app.services.providers.builtin_providers(registry) 完成注册，
    避免在此直接引用具体 Provider 实现引发循环导入。
    """
    from app.services.providers import builtin_providers  # local import to avoid cycles

    builtin_providers(provider_registry)


__all__ = [
    "ProviderHandler",
    "ProviderRegistry",
    "provider_registry",
    "load_builtin_providers",
]

