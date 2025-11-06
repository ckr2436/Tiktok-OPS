# app/services/kie_api/__init__.py
from __future__ import annotations

from .sora2 import Sora2ImageToVideoService, KieApiError, sora

# 兼容旧代码用的别名（如果以后有地方用 Sora2Service 也不会挂）
Sora2Service = Sora2ImageToVideoService

__all__ = [
    "Sora2ImageToVideoService",
    "Sora2Service",
    "KieApiError",
    "sora",
]

