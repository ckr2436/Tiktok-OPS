# app/features/tenants/oauth_ttb/__init__.py
from __future__ import annotations

from importlib import import_module

__all__ = ["router"]


def __getattr__(name: str):
    if name == "router":
        module = import_module("app.features.tenants.oauth_ttb.router")
        return getattr(module, "router")
    raise AttributeError(name)
