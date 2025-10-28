# backend/app/services/providers/__init__.py
from __future__ import annotations

from app.services.provider_registry import ProviderRegistry
from .tiktok_business import TiktokBusinessProvider


def builtin_providers(registry: ProviderRegistry) -> None:
    """
    Register built-in provider handlers into the given registry.

    仅做“声明式注册”，不包含业务逻辑；业务逻辑在各自模块中实现。
    """
    ttb = TiktokBusinessProvider()
    # 同时支持两种 provider id 写法
    registry.register("tiktok-business", ttb)
    registry.register("tiktok_business", ttb)


__all__ = ["builtin_providers"]

