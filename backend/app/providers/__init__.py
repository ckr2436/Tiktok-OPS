# backend/app/providers/__init__.py
"""Built-in provider implementations bootstrap."""

from __future__ import annotations

# 统一从 provider_registry 拉起内置 Provider，避免循环导入
from app.services.provider_registry import (
    load_builtin_providers as _load_builtin_providers,
    provider_registry,
)


def load_builtin_providers(package: str = "app.providers"):
    """
    Load/register built-in providers into the in-memory registry.

    NOTE:
    - `package` 参数仅保留兼容，不参与实际加载，避免循环依赖。
    - 若存在可选的 catalog 同步逻辑，则在此处 best-effort 触发。
    """
    _load_builtin_providers()

    # 可选：如果没有这个模块，不影响启动
    try:
        from app.services.providers.catalog import sync_registry  # type: ignore
        sync_registry()
    except Exception:
        pass

    return provider_registry


__all__ = ["load_builtin_providers"]

