# backend/app/services/redis_client.py
from __future__ import annotations

import asyncio
from urllib.parse import urlparse, urlunparse

import redis                # redis-py v5.x 同步客户端
import redis.asyncio as aioredis

from app.core.config import settings

# ---- 工具 ----
def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "on"}

def _normalize_redis_url(raw_url: str | None, force_tls: bool) -> str:
    """
    redis-py v5 用 scheme 决定 TLS：
      - TLS 用 rediss://
      - 非 TLS 用 redis://
    不能再传 ssl=*** 参数。
    """
    url = (raw_url or "redis://127.0.0.1:6379/0").strip()
    p = urlparse(url)
    scheme = p.scheme or "redis"
    if force_tls:
        scheme = "rediss"
    elif scheme not in {"redis", "rediss"}:
        scheme = "redis"
    p = p._replace(scheme=scheme)
    return urlunparse(p)

# ---- 同步客户端（Celery prefork/线程内使用）----
_sync_client: redis.Redis | None = None
# Backwards compatibility shim for tests that patch `_redis`
_redis: redis.Redis | None = None

def get_redis_sync() -> redis.Redis:
    global _sync_client, _redis
    if _redis is not None and _sync_client is not _redis:
        _sync_client = _redis
    if _sync_client is None:
        if _redis is not None:
            _sync_client = _redis
        else:
            raw_url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
            force_tls = _truthy(getattr(settings, "REDIS_SSL", False))
            url = _normalize_redis_url(raw_url, force_tls)
            _sync_client = redis.Redis.from_url(
                url,
                decode_responses=False,     # 用字节串，Lua 脚本直接比对
                health_check_interval=30,
                socket_keepalive=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
    _redis = _sync_client
    return _sync_client

# ---- 异步客户端（HTTP 处理、异步任务等可用）----
_async_client: aioredis.Redis | None = None
_async_lock = asyncio.Lock()

async def get_redis() -> aioredis.Redis:
    global _async_client
    if _async_client is None:
        async with _async_lock:
            if _async_client is None:
                raw_url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
                force_tls = _truthy(getattr(settings, "REDIS_SSL", False))
                url = _normalize_redis_url(raw_url, force_tls)
                _async_client = aioredis.from_url(
                    url,
                    decode_responses=False,
                    socket_connect_timeout=3.0,
                    socket_timeout=5.0,
                    health_check_interval=30,
                    retry_on_timeout=True,
                    max_connections=100,
                )
    return _async_client

async def close_redis() -> None:
    global _async_client
    if _async_client is not None:
        await _async_client.close()
        _async_client = None

