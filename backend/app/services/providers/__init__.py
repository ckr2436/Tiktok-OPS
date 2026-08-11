# backend/app/services/providers/__init__.py
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.provider_registry import ProviderRegistry

if TYPE_CHECKING:  # pragma: no cover - import-time typing aid
    from .tiktok_business import TiktokBusinessProvider


def builtin_providers(registry: ProviderRegistry) -> None:
    """
    Register built-in provider handlers into the given registry.

    仅做“声明式注册”，不包含业务逻辑；业务逻辑在各自模块中实现。
    """
    # 注册 TiktokBusinessProvider
    from .tiktok_business import TiktokBusinessProvider
    ttb = TiktokBusinessProvider()
    registry.register("tiktok-business", ttb)
    registry.register("tiktok_business", ttb)

