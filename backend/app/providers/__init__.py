"""Built-in provider implementations."""

from __future__ import annotations

from app.services.providers import load_builtin_providers as _load_builtin_providers


def load_builtin_providers(package: str = "app.providers"):
    """Load provider modules and persist registry metadata."""

    modules = _load_builtin_providers(package)
    from app.services.providers.catalog import sync_registry

    sync_registry()
    return modules

__all__ = ["load_builtin_providers"]
