"""Redis-backed distributed lock utilities for sync tasks."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

# Lua scripts ensure only the owner token can refresh or release the lock.
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_REFRESH_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


def _run_async(coro):
    """Execute an async coroutine in a fresh event loop."""
    return asyncio.run(coro)


async def _acquire_lock(key: str, owner_token: str, ttl_seconds: int) -> bool:
    redis = await get_redis()
    value = owner_token.encode("utf-8")
    ttl_seconds = max(int(ttl_seconds), 1)
    return bool(await redis.set(key, value, nx=True, ex=ttl_seconds))


async def _refresh_lock(key: str, owner_token: str, ttl_ms: int) -> bool:
    redis = await get_redis()
    ttl_ms = max(int(ttl_ms), 1)
    result = await redis.eval(_REFRESH_SCRIPT, 1, key, owner_token.encode("utf-8"), ttl_ms)
    return bool(result)


async def _release_lock(key: str, owner_token: str) -> bool:
    redis = await get_redis()
    result = await redis.eval(_RELEASE_SCRIPT, 1, key, owner_token.encode("utf-8"))
    return bool(result)


@dataclass
class RedisDistributedLock:
    """Distributed lock with automatic heartbeats."""

    key: str
    owner_token: str
    ttl_seconds: int = settings.TTB_SYNC_LOCK_TTL_SECONDS
    heartbeat_interval: int = settings.TTB_SYNC_LOCK_HEARTBEAT_SECONDS

    def __post_init__(self) -> None:
        self._acquired = False
        self._lost = False
        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.ttl_seconds = max(int(self.ttl_seconds), 1)
        configured_heartbeat = max(int(self.heartbeat_interval), 0)
        if configured_heartbeat and configured_heartbeat >= self.ttl_seconds:
            adjusted = max(self.ttl_seconds // 2, 1)
            if adjusted >= self.ttl_seconds:
                adjusted = max(self.ttl_seconds - 1, 1)
            logger.warning(
                "redis lock heartbeat interval >= ttl; adjusting",
                extra={
                    "key": self.key,
                    "ttl_seconds": self.ttl_seconds,
                    "configured_heartbeat": configured_heartbeat,
                    "effective_heartbeat": adjusted,
                },
            )
            self.heartbeat_interval = adjusted
        else:
            self.heartbeat_interval = configured_heartbeat

    @property
    def acquired(self) -> bool:
        return self._acquired and not self._lost

    @property
    def lost(self) -> bool:
        return self._lost

    def acquire(self, *, timeout: float = 0.0, retry_interval: float = 0.1) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            success = _run_async(_acquire_lock(self.key, self.owner_token, self.ttl_seconds))
            if success:
                with self._lock:
                    self._acquired = True
                    self._lost = False
                self._start_heartbeat()
                logger.debug(
                    "redis lock acquired",
                    extra={
                        "key": self.key,
                        "ttl_seconds": self.ttl_seconds,
                        "heartbeat_interval": self.heartbeat_interval,
                    },
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
            while not self._stop_event.wait(interval):
                try:
                    refreshed = _run_async(
                        _refresh_lock(self.key, self.owner_token, self.ttl_seconds * 1000)
                    )
                    if not refreshed:
                        logger.warning(
                            "redis lock heartbeat lost ownership",
                            extra={"key": self.key},
                        )
                        with self._lock:
                            self._lost = True
                        return
                except Exception:  # noqa: BLE001
                    logger.exception("redis lock heartbeat failed", extra={"key": self.key})

        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()

    def release(self) -> bool:
        with self._lock:
            if not self._acquired:
                return False
            self._stop_event.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        try:
            released = _run_async(_release_lock(self.key, self.owner_token))
            if not released:
                logger.debug(
                    "redis lock release skipped (not owner)",
                    extra={"key": self.key},
                )
            return released
        finally:
            with self._lock:
                self._acquired = False
                self._heartbeat_thread = None
                self._stop_event = threading.Event()

    def force_stop(self) -> None:
        """Stop heartbeat loop without releasing the lock (testing helper)."""
        self._stop_event.set()
        thread = self._heartbeat_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._heartbeat_thread = None
            self._stop_event = threading.Event()


__all__ = [
    "RedisDistributedLock",
    "_release_lock",
    "_refresh_lock",
]
