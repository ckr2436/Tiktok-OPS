from __future__ import annotations

from typing import Mapping

from app.services.providers.base import Provider, ProviderRegistry


class _DummyProvider(Provider):
    def __init__(self, key: str, display_name: str) -> None:
        self._key = key
        self._display = display_name

    def key(self) -> str:
        return self._key

    def display_name(self) -> str:
        return self._display

    def capabilities(self) -> Mapping[str, object]:
        return {"example": True}


def test_register_and_get_provider() -> None:
    registry = ProviderRegistry()
    provider = _DummyProvider("example", "Example Provider")

    registry.register(provider)

    assert registry.get("example") is provider
    assert registry.get("Example") is provider
    assert list(registry.list())[0] is provider


def test_register_duplicate_provider_disallowed() -> None:
    registry = ProviderRegistry()
    registry.register(_DummyProvider("duplicate", "Dup"))

    try:
        registry.register(_DummyProvider("duplicate", "Dup 2"))
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected duplicate registration to fail")


def test_register_rejects_empty_key() -> None:
    registry = ProviderRegistry()

    try:
        registry.register(_DummyProvider(" ", "Invalid"))
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected empty key to fail")
