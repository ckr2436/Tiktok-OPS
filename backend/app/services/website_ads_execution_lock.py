"""Shared distributed execution lease for Website Ads data and actions.

The monitor and daily-report services both replace hourly metric snapshots.
The monitor also performs remote TikTok mutations.  Their mutex therefore
belongs at the service boundary, so Celery and direct/manual callers cannot
bypass it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import logging
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.redis_locks import RedisDistributedLock


logger = logging.getLogger("gmv.services.website_ads.execution_lock")

_SESSION_LEASE_KEY = "website_ads_execution_lease"
_LOCK_FACTORY: Callable[..., Any] = RedisDistributedLock


class WebsiteAdsExecutionLockLost(RuntimeError):
    """Raised before further mutation when the distributed lease is lost."""


@dataclass(frozen=True)
class WebsiteAdsExecutionLease:
    lock: Any
    operation: str
    owner_token: str
    lease_name: str
    fencing_token: int
    ttl_seconds: int

    @property
    def active(self) -> bool:
        return bool(
            getattr(self.lock, "acquired", False)
            and not getattr(self.lock, "lost", False)
        )

    def assert_active(self) -> None:
        verify_ownership = getattr(self.lock, "verify_ownership", None)
        if callable(verify_ownership):
            try:
                ownership_valid = bool(verify_ownership())
            except Exception:  # noqa: BLE001 - verification must fail closed
                logger.exception(
                    "Website Ads execution lock ownership verification failed",
                    extra={"operation": self.operation},
                )
                ownership_valid = False
        else:
            # Deterministic test doubles and older compatible lock providers
            # may only expose the cached acquired/lost properties.
            ownership_valid = self.active
        if not ownership_valid:
            raise WebsiteAdsExecutionLockLost(
                f"Website Ads execution lock was lost during {self.operation}"
            )


def _lock_key() -> str:
    # This is deliberately global across workspaces. A scheduled all-workspace
    # cycle must conflict with a manual single-workspace run as well as the
    # daily report writer.
    environment = str(getattr(settings, "LOCK_ENV", "local") or "local")
    return f"website_ads:{environment}:metrics-actions:cycle"


def _durable_lease_name() -> str:
    """Return a bounded persistent lease key for the shared Redis key."""

    digest = sha256(_lock_key().encode("utf-8", "strict")).hexdigest()[:32]
    return f"website_ads:metrics-actions:{digest}"


def _lease_ttl_seconds() -> int:
    return max(
        60,
        int(
            getattr(
                settings,
                "WEBSITE_ADS_EXECUTION_LOCK_TTL_SECONDS",
                1500,
            )
        ),
    )


def _release_redis_lock(
    lock: Any,
    *,
    operation: str,
    workspace_id: int | None,
) -> None:
    try:
        lock.release()
    except Exception:  # noqa: BLE001 - best-effort owner-checked release
        logger.exception(
            "Website Ads Redis execution lock release failed",
            extra={"operation": operation, "workspace_id": workspace_id},
        )


@contextmanager
def website_ads_execution_lease(
    db: Session,
    *,
    operation: str,
    workspace_id: int | None,
    lock_factory: Callable[..., Any] | None = None,
) -> Iterator[WebsiteAdsExecutionLease | None]:
    """Acquire the shared Website Ads lease, yielding ``None`` on failure.

    Redis outages and contention are both fail-closed.  Tests can inject a
    deterministic factory without connecting to Redis.
    """

    existing = db.info.get(_SESSION_LEASE_KEY)
    if isinstance(existing, WebsiteAdsExecutionLease):
        assert_website_ads_execution_lock(db, required=True)
        yield existing
        return

    # Keep this below the durable table's VARCHAR(128) owner bound regardless
    # of operation/workspace labels.
    owner_token = f"website_ads:{uuid4().hex}"
    factory = lock_factory or _LOCK_FACTORY
    ttl_seconds = _lease_ttl_seconds()
    try:
        lock = factory(
            key=_lock_key(),
            owner_token=owner_token,
            ttl_seconds=ttl_seconds,
            heartbeat_interval=max(
                1,
                int(
                    getattr(
                        settings,
                        "WEBSITE_ADS_EXECUTION_LOCK_HEARTBEAT_SECONDS",
                        20,
                    )
                ),
            ),
        )
        acquired = bool(
            lock.acquire(
                timeout=max(
                    0.0,
                    float(
                        getattr(
                            settings,
                            "WEBSITE_ADS_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS",
                            0.2,
                        )
                    ),
                ),
                retry_interval=0.05,
            )
        )
    except Exception:  # noqa: BLE001 - lock infrastructure must fail closed
        logger.exception(
            "Website Ads execution lock acquisition failed",
            extra={"operation": operation, "workspace_id": workspace_id},
        )
        yield None
        return

    if not acquired:
        logger.info(
            "Website Ads execution skipped because the shared lease is unavailable",
            extra={"operation": operation, "workspace_id": workspace_id},
        )
        yield None
        return

    lease_name = _durable_lease_name()
    fencing_token: int | None = None
    try:
        # Lazy import is required here: importing the tenant package at module
        # load time traverses router -> Celery -> Website Ads tasks and would
        # re-enter this partially initialized module.
        from app.features.tenants.ttb.gmv_max.control import (
            acquire_guard_action_lease,
        )

        # A caller may have opened a read transaction before reaching this
        # boundary. Discard it before acquiring the durable generation so the
        # protected work starts from a fresh snapshot. Production entrypoints
        # must not carry pending business writes into this outer boundary.
        db.rollback()
        fencing_token = acquire_guard_action_lease(
            db,
            lease_name=lease_name,
            owner_token=owner_token,
            ttl_seconds=ttl_seconds,
        )
        if fencing_token is None:
            db.rollback()
            logger.info(
                "Website Ads execution skipped because the durable fence is unavailable",
                extra={"operation": operation, "workspace_id": workspace_id},
            )
            _release_redis_lock(
                lock,
                operation=operation,
                workspace_id=workspace_id,
            )
            yield None
            return
        lease = WebsiteAdsExecutionLease(
            lock=lock,
            operation=str(operation),
            owner_token=owner_token,
            lease_name=lease_name,
            fencing_token=int(fencing_token),
            ttl_seconds=ttl_seconds,
        )
        # Do not publish a durable owner if Redis ownership was already lost
        # while the database row was being acquired.
        lease.assert_active()
        db.commit()
    except Exception:  # noqa: BLE001 - either fence must fail closed
        db.rollback()
        logger.exception(
            "Website Ads durable execution fence acquisition failed",
            extra={"operation": operation, "workspace_id": workspace_id},
        )
        _release_redis_lock(
            lock,
            operation=operation,
            workspace_id=workspace_id,
        )
        yield None
        return

    db.info[_SESSION_LEASE_KEY] = lease
    try:
        yield lease
    finally:
        if db.info.get(_SESSION_LEASE_KEY) is lease:
            db.info.pop(_SESSION_LEASE_KEY, None)
        try:
            from app.features.tenants.ttb.gmv_max.control import (
                release_guard_action_lease,
            )

            # Roll back incomplete protected work before opening the
            # owner-and-generation checked release transaction. A stale
            # finally block can never clear a replacement generation.
            db.rollback()
            release_guard_action_lease(
                db,
                lease_name=lease.lease_name,
                owner_token=lease.owner_token,
                fencing_token=lease.fencing_token,
            )
            db.commit()
        except Exception:  # noqa: BLE001 - fail safe; expiry remains fallback
            db.rollback()
            logger.exception(
                "Website Ads durable execution fence release failed",
                extra={"operation": operation, "workspace_id": workspace_id},
            )
        _release_redis_lock(
            lock,
            operation=operation,
            workspace_id=workspace_id,
        )


def assert_website_ads_execution_lock(
    db: Session,
    *,
    required: bool = False,
) -> None:
    """Fail if an attached service lease has been lost.

    Lower-level helpers remain directly testable and reusable when no lease is
    attached. Top-level service entrypoints call this with ``required=True``.
    """

    lease = db.info.get(_SESSION_LEASE_KEY)
    if lease is None:
        if required:
            raise WebsiteAdsExecutionLockLost(
                "Website Ads service mutation requires the shared execution lock"
            )
        return
    if not isinstance(lease, WebsiteAdsExecutionLease):
        raise WebsiteAdsExecutionLockLost(
            "Website Ads session contains an invalid execution lease"
        )
    try:
        from app.features.tenants.ttb.gmv_max.control import (
            assert_guard_action_lease_generation,
        )

        # SELECT ... FOR UPDATE keeps this generation locked in the same
        # transaction as the caller's subsequent protected mutation/commit.
        # A replacement worker cannot advance the durable fencing token until
        # that transaction commits or rolls back.
        assert_guard_action_lease_generation(
            db,
            lease_name=lease.lease_name,
            owner_token=lease.owner_token,
            fencing_token=lease.fencing_token,
            ttl_seconds=lease.ttl_seconds,
            redis_lock=lease.lock,
        )
    except Exception as exc:  # noqa: BLE001 - all proof failures fail closed
        raise WebsiteAdsExecutionLockLost(
            f"Website Ads execution lock was lost during {lease.operation}"
        ) from exc


__all__ = [
    "WebsiteAdsExecutionLease",
    "WebsiteAdsExecutionLockLost",
    "assert_website_ads_execution_lock",
    "website_ads_execution_lease",
]
