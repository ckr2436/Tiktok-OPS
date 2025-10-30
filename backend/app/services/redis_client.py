# app/services/redis_client.py
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

import redis.asyncio as aioredis
from redis.asyncio.client import Redis

from app.core.config import settings

_redis: Optional[Redis] = None
_lock = asyncio.Lock()


@lru_cache
def _is_rediss(url: str) -> bool:
    try:
        return urlparse(url).scheme.lower() == "rediss"
    except Exception:
        return False


async def get_redis() -> Redis:
    """获取全局异步 Redis 客户端（懒初始化，跨请求复用连接池）。"""

    global _redis
    if _redis is None:
        async with _lock:
            if _redis is None:
                url = settings.REDIS_URL
                # redis.from_url 会根据 rediss:// 自动启用 TLS；不要显式传入 ssl 参数以避免兼容性问题
                kwargs = dict(
                    decode_responses=False,
                    socket_connect_timeout=3.0,
                    socket_timeout=5.0,
                    health_check_interval=30,
                    retry_on_timeout=True,
                    max_connections=100,
                )
                if _is_rediss(url):
                    # rediss:// 走 TLS，无需额外参数；占位逻辑保留扩展点
                    pass
                _redis = aioredis.from_url(url, **kwargs)
    return _redis

async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None

