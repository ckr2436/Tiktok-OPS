# backend/app/services/redis_locks.py
"""Redis-backed distributed lock utilities for sync tasks (Celery prefork safe)."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import settings
from app.services.redis_client import get_redis_sync  # 统一从这里拿同步客户端

logger = logging.getLogger(__name__)

# 原子脚本（字节串）：只有 owner 才能续期/释放
_RELEASE_SCRIPT = b"""
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_REFRESH_SCRIPT = b"""
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

def _b(s: str | bytes) -> bytes:
    return s if isinstance(s, (bytes, bytearray)) else s.encode("utf-8", "strict")


def _adapt_client(client):
    """Wrap async redis stubs exposed by tests into a sync-compatible adapter."""

    if not asyncio.iscoroutinefunction(getattr(client, "set", None)):
        return client

    class _SyncAdapter:
        def __init__(self, inner):
            self._inner = inner

        def set(self, *args, **kwargs):
            return asyncio.run(self._inner.set(*args, **kwargs))

        def eval(self, *args, **kwargs):
            return asyncio.run(self._inner.eval(*args, **kwargs))

        def get(self, *args, **kwargs):
            return asyncio.run(self._inner.get(*args, **kwargs))

        def exists(self, *args, **kwargs):
            return asyncio.run(self._inner.exists(*args, **kwargs))

        def ttl(self, *args, **kwargs):
            return asyncio.run(self._inner.ttl(*args, **kwargs))

    return _SyncAdapter(client)

@dataclass
class RedisDistributedLock:
    """
    生产可用的同步分布式锁：
    - acquire(): SET NX EX
    - 心跳线程定期 Lua 校验 owner + PEXPIRE
    - release(): Lua 校验 owner + DEL
    """
    key: str
    owner_token: str
    ttl_seconds: int = field(
        default_factory=lambda: getattr(settings, "TTB_SYNC_LOCK_TTL_SECONDS", 30)
    )
    heartbeat_interval: int = field(
        default_factory=lambda: getattr(settings, "TTB_SYNC_LOCK_HEARTBEAT_SECONDS", 10)
    )

    # 运行态
    _acquired: bool = False
    _lost: bool = False
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _heartbeat_thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # 统一使用同步客户端工厂；禁止手写 ssl= 等不兼容参数
        self._redis = _adapt_client(get_redis_sync())

        self.ttl_seconds = max(int(self.ttl_seconds), 1)
        hb = max(int(self.heartbeat_interval), 0)
        if hb and hb >= self.ttl_seconds:
            hb = max(self.ttl_seconds // 2, 1)
            if hb >= self.ttl_seconds:
                hb = max(self.ttl_seconds - 1, 1)
            message = "redis lock heartbeat interval >= ttl; adjusted"
            logger.warning(
                message,
                extra={"key": self.key, "ttl_seconds": self.ttl_seconds, "effective_heartbeat": hb},
            )
            logging.getLogger().warning(
                message,
                extra={"key": self.key, "ttl_seconds": self.ttl_seconds, "effective_heartbeat": hb},
            )
        self.heartbeat_interval = hb

    @property
    def acquired(self) -> bool:
        return self._acquired and not self._lost

    @property
    def lost(self) -> bool:
        return self._lost

    def acquire(self, *, timeout: float = 0.0, retry_interval: float = 0.1) -> bool:
        """获取锁；成功则启动心跳线程。"""
        value = _b(self.owner_token)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                ok = self._redis.set(self.key, value, nx=True, ex=self.ttl_seconds)
            except Exception:  # noqa: BLE001
                logger.exception("redis lock acquire failed", extra={"key": self.key})
                ok = False

            if ok:
                with self._lock:
                    self._acquired = True
                    self._lost = False
                self._start_heartbeat()
                logger.debug(
                    "redis lock acquired",
                    extra={"key": self.key, "ttl_seconds": self.ttl_seconds, "heartbeat_interval": self.heartbeat_interval},
                )
                return True

            if timeout <= 0:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(retry_interval, max(remaining, 0)))

    def _start_heartbeat(self) -> None:
        interval = max(int(self.heartbeat_interval), 0)
        if interval <= 0:
            return
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        def _loop() -> None:
            key_b = _b(self.key)
            owner_b = _b(self.owner_token)
            ttl_ms = max(int(self.ttl_seconds * 1000), 1)
            while not self._stop_event.wait(interval):
                try:
                    res = self._redis.eval(_REFRESH_SCRIPT, 1, key_b, owner_b, ttl_ms)
                    if not res:
                        logger.warning("redis lock heartbeat lost ownership", extra={"key": self.key})
                        with self._lock:
                            self._lost = True
                        return
                except Exception:  # noqa: BLE001
                    logger.exception("redis lock heartbeat failed", extra={"key": self.key})

        self._stop_event = threading.Event()
        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()

    def release(self) -> bool:
        """释放锁（原子校验 owner）。"""
        with self._lock:
            if not self._acquired:
                return False
            self._stop_event.set()

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)

        try:
            res = self._redis.eval(_RELEASE_SCRIPT, 1, _b(self.key), _b(self.owner_token))
            released = bool(res)
            if not released:
                logger.debug("redis lock release skipped (not owner)", extra={"key": self.key})
            return released
        except Exception:  # noqa: BLE001
            logger.exception("redis lock release failed", extra={"key": self.key})
            return False
        finally:
            with self._lock:
                self._acquired = False
                self._heartbeat_thread = None

    def force_stop(self) -> None:
        """仅停止心跳，不释放锁（测试/故障注入）。"""
        self._stop_event.set()
        t = self._heartbeat_thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        with self._lock:
            self._heartbeat_thread = None

