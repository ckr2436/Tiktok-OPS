"""Provider protocol and registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Protocol, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    """Runtime protocol for provider implementations."""

    def key(self) -> str:  # pragma: no cover - protocol definition
        """Return the unique provider key."""

    def display_name(self) -> str:  # pragma: no cover - protocol definition
        """Return the human readable name for the provider."""

    def capabilities(self) -> Mapping[str, object]:  # pragma: no cover - optional hook
        """Return provider capabilities metadata."""
        return {}


@dataclass(frozen=True, slots=True)
class _ProviderEntry:
    provider: Provider


class ProviderRegistry:
    """In-memory registry for provider plugins."""

    def __init__(self) -> None:
        self._providers: Dict[str, _ProviderEntry] = {}

    def register(self, provider: Provider) -> Provider:
        key = provider.key().strip().lower()
        if not key:
            raise ValueError("Provider key must be non-empty")

        if key in self._providers:
            raise ValueError(f"Provider '{key}' already registered")

        self._providers[key] = _ProviderEntry(provider=provider)
        return provider

    def get(self, key: str) -> Provider:
        normalized = key.strip().lower()
        try:
            entry = self._providers[normalized]
        except KeyError as exc:  # pragma: no cover - exercised in tests
            raise KeyError(f"Unknown provider '{key}'") from exc
        return entry.provider

    def list(self) -> Iterable[Provider]:
        return (entry.provider for entry in self._providers.values())


registry = ProviderRegistry()


__all__ = ["Provider", "ProviderRegistry", "registry"]
