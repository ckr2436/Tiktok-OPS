# app/services/redis_client.py
from __future__ import annotations

import asyncio
from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio.client import Redis

from app.core.config import settings

_redis: Optional[Redis] = None
_lock = asyncio.Lock()

async def get_redis() -> Redis:
    """
    获取全局异步 Redis 客户端（懒初始化，跨请求复用连接池）。
    """
    global _redis
    if _redis is None:
        async with _lock:
            if _redis is None:
                _redis = aioredis.from_url(
                    settings.REDIS_URL,
                    decode_responses=False,
                    ssl=bool(settings.REDIS_SSL),
                    socket_connect_timeout=3.0,
                    socket_timeout=5.0,
                    health_check_interval=30,
                    retry_on_timeout=True,
                    max_connections=100,
                )
    return _redis

async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None

