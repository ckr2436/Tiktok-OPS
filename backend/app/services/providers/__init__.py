from __future__ import annotations

from typing import Callable

from app.services.provider_registry import ProviderRegistry


def builtin_providers(registry: ProviderRegistry) -> None:
    """Register built-in providers with the shared registry."""

    # Import inside the function to avoid eager side effects during tests.
    from .tiktok_business import TiktokBusinessProvider

    provider = TiktokBusinessProvider()
    try:
        registry.register(provider.provider_id, provider)
    except ValueError:
        # Registry may already contain the provider when multiple callers
        # attempt to load builtins concurrently. Silently ignore duplicates.
        pass


__all__ = ["builtin_providers"]
