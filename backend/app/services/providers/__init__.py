"""Provider plugin loading utilities."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Iterable


def load_builtin_providers(package: str = "app.providers") -> list[ModuleType]:
    """Import all provider submodules so they can register themselves."""

    modules: list[ModuleType] = []
    pkg = importlib.import_module(package)
    package_path = getattr(pkg, "__path__", None)
    if not package_path:
        return modules

    for module_info in pkgutil.iter_modules(package_path):
        if module_info.name.startswith("_"):
            continue
        modules.append(importlib.import_module(f"{package}.{module_info.name}"))

    return modules


__all__: Iterable[str] = ("load_builtin_providers",)
