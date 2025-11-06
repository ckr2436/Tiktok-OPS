# backend/app/services/providers/__init__.py
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.provider_registry import ProviderRegistry

if TYPE_CHECKING:  # pragma: no cover - import-time typing aid
    from .tiktok_business import TiktokBusinessProvider
    from .kie_ai import KieAIProvider  # 新增的KieAIProvider引入


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

    # 注册 KieAIProvider
    from .kie_ai import KieAIProvider
    kie_ai_provider = KieAIProvider()
    registry.register("kie-ai", kie_ai_provider)
    registry.register("kie_ai", kie_ai_provider)  # 支持两种写法

