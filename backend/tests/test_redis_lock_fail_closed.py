from __future__ import annotations

import time

from app.services import redis_locks


def test_heartbeat_transport_error_marks_lock_lost(monkeypatch):
    class _Redis:
        def set(self, *_args, **_kwargs):
            return True

        def eval(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(redis_locks, "get_redis_sync", lambda: _Redis())
    lock = redis_locks.RedisDistributedLock(
        key="test:fail-closed",
        owner_token="owner",
        ttl_seconds=2,
        heartbeat_interval=1,
    )

    assert lock.acquire() is True
    deadline = time.monotonic() + 2.5
    while not lock.lost and time.monotonic() < deadline:
        time.sleep(0.05)

    assert lock.lost is True
    assert lock.acquired is False
    lock.release()
