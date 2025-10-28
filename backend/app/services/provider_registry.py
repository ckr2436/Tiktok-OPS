from __future__ import annotations

"""Provider registry utilities used for sync orchestration."""

from collections.abc import Callable, Iterable
from typing import Dict, Protocol


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
        key = provider_id.strip().lower()
        if not key:
            raise ValueError("provider_id is required")
        if key in self._handlers:
            raise ValueError(f"provider handler already registered for {provider_id}")
        self._handlers[key] = handler

    def get(self, provider_id: str) -> ProviderHandler:
        key = provider_id.strip().lower()
        if key not in self._handlers:
            raise KeyError(provider_id)
        return self._handlers[key]

    def list(self) -> Iterable[tuple[str, ProviderHandler]]:
        return tuple(self._handlers.items())


provider_registry = ProviderRegistry()


def load_builtin_providers() -> None:
    """Ensure bundled providers are registered."""

    # Local import to avoid circular dependencies during module import.
    from app.services.providers import builtin_providers

    builtin_providers(provider_registry)


__all__ = [
    "ProviderHandler",
    "ProviderRegistry",
    "provider_registry",
    "load_builtin_providers",
]
