"""Shared execution lock for account-wide GMV Max fact synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.redis_locks import RedisDistributedLock


class GmvMaxAccountSyncFenceLost(RuntimeError):
    """Raised when an account sync can no longer prove exclusive ownership."""


@dataclass(frozen=True)
class GmvMaxAccountSyncFence:
    lease_name: str
    owner_token: str
    fencing_token: int
    redis_lock: RedisDistributedLock
    ttl_seconds: int

    def assert_current(self, db: Session) -> None:
        """Verify Redis ownership and the durable fence in this transaction."""

        from app.features.tenants.ttb.gmv_max.control import (
            GMVMaxGuardActionLease,
            utcnow_naive,
        )

        verifier = getattr(self.redis_lock, "verify_ownership", None)
        if callable(verifier):
            redis_owned = bool(verifier())
        else:
            redis_owned = bool(
                getattr(self.redis_lock, "acquired", False)
                and not getattr(self.redis_lock, "lost", False)
            )
        if not redis_owned:
            raise GmvMaxAccountSyncFenceLost(
                "GMV Max account sync Redis ownership was lost"
            )

        row = db.execute(
            select(GMVMaxGuardActionLease)
            .where(
                GMVMaxGuardActionLease.lease_name == self.lease_name
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            row is None
            or row.owner_token != self.owner_token
            or int(row.fencing_token or 0) != int(self.fencing_token)
        ):
            raise GmvMaxAccountSyncFenceLost(
                "GMV Max account sync durable fence was superseded"
            )
        # The current Redis owner may renew an expired database lease if no
        # newer fencing token has been issued. If a replacement worker won the
        # row first, the token/owner checks above reject this stale worker.
        row.expires_at = utcnow_naive() + timedelta(
            seconds=max(60, int(self.ttl_seconds))
        )
        db.flush()


def account_sync_lock_key(
    *,
    workspace_id: int,
    auth_id: int | None,
) -> str:
    """Return the lock shared by manual/account and strategy sync entrypoints."""

    auth_scope = str(int(auth_id)) if auth_id is not None else "all"
    return f"gmvmax:account-fact-sync:{int(workspace_id)}:{auth_scope}"


def creative_10min_sync_lock_key(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
) -> str:
    """Keep the established Redis mutex key for one creative fact partition."""

    return (
        f"gmvmax:creative-metrics:{int(workspace_id)}:{int(auth_id)}:"
        f"{str(advertiser_id)}:{str(campaign_id)}"
    )


def creative_10min_sync_fence_name(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
) -> str:
    """Return a bounded durable lease name for an exact creative partition."""

    scope = "\x00".join(
        (
            str(int(workspace_id)),
            str(int(auth_id)),
            str(advertiser_id),
            str(campaign_id),
        )
    )
    # GMVMaxGuardActionLease.lease_name is VARCHAR(128). Hash the potentially
    # long official identifiers while retaining an explicit, collision-domain
    # prefix distinct from account and Guard leases.
    return f"gmvmax:creative-metrics:fence:{sha256(scope.encode('utf-8')).hexdigest()}"


def build_account_sync_lock(
    *,
    workspace_id: int,
    auth_id: int | None,
    owner_token: str,
) -> RedisDistributedLock:
    ttl_seconds = max(
        300,
        int(getattr(settings, "GMVMAX_ACCOUNT_SYNC_LOCK_TTL_SECONDS", 300)),
    )
    heartbeat_seconds = max(
        5,
        int(
            getattr(
                settings,
                "GMVMAX_ACCOUNT_SYNC_LOCK_HEARTBEAT_SECONDS",
                30,
            )
        ),
    )
    return RedisDistributedLock(
        key=account_sync_lock_key(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id) if auth_id is not None else None,
        ),
        owner_token=str(owner_token),
        ttl_seconds=ttl_seconds,
        heartbeat_interval=heartbeat_seconds,
    )


def build_creative_10min_sync_lock(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    owner_token: str,
) -> RedisDistributedLock:
    ttl_seconds = max(
        180,
        int(
            getattr(
                settings,
                "GMVMAX_CREATIVE_METRICS_LOCK_TTL_SECONDS",
                180,
            )
        ),
    )
    heartbeat_seconds = max(
        5,
        int(
            getattr(
                settings,
                "GMVMAX_CREATIVE_METRICS_LOCK_HEARTBEAT_SECONDS",
                20,
            )
        ),
    )
    return RedisDistributedLock(
        key=creative_10min_sync_lock_key(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
        ),
        owner_token=str(owner_token),
        ttl_seconds=ttl_seconds,
        heartbeat_interval=heartbeat_seconds,
    )


def acquire_account_sync_fence(
    db: Session,
    *,
    redis_lock: RedisDistributedLock,
    workspace_id: int,
    auth_id: int,
    owner_token: str,
) -> GmvMaxAccountSyncFence | None:
    """Acquire the durable companion after the Redis lock is owned."""

    from app.features.tenants.ttb.gmv_max.control import acquire_guard_action_lease

    verifier = getattr(redis_lock, "verify_ownership", None)
    if callable(verifier):
        redis_owned = bool(verifier())
    else:
        redis_owned = bool(
            getattr(redis_lock, "acquired", False)
            and not getattr(redis_lock, "lost", False)
        )
    if not redis_owned:
        return None
    lease_name = account_sync_lock_key(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
    )
    ttl_seconds = max(60, int(getattr(redis_lock, "ttl_seconds", 300)))
    fencing_token = acquire_guard_action_lease(
        db,
        lease_name=lease_name,
        owner_token=str(owner_token),
        ttl_seconds=ttl_seconds,
    )
    if fencing_token is None:
        return None
    return GmvMaxAccountSyncFence(
        lease_name=lease_name,
        owner_token=str(owner_token),
        fencing_token=int(fencing_token),
        redis_lock=redis_lock,
        ttl_seconds=ttl_seconds,
    )


def acquire_creative_10min_sync_fence(
    db: Session,
    *,
    redis_lock: RedisDistributedLock,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    owner_token: str,
) -> GmvMaxAccountSyncFence | None:
    """Acquire a persistent fencing generation for one creative fact writer."""

    from app.features.tenants.ttb.gmv_max.control import acquire_guard_action_lease

    verifier = getattr(redis_lock, "verify_ownership", None)
    if callable(verifier):
        redis_owned = bool(verifier())
    else:
        redis_owned = bool(
            getattr(redis_lock, "acquired", False)
            and not getattr(redis_lock, "lost", False)
        )
    if not redis_owned:
        return None
    lease_name = creative_10min_sync_fence_name(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=str(advertiser_id),
        campaign_id=str(campaign_id),
    )
    ttl_seconds = max(60, int(getattr(redis_lock, "ttl_seconds", 180)))
    fencing_token = acquire_guard_action_lease(
        db,
        lease_name=lease_name,
        owner_token=str(owner_token),
        ttl_seconds=ttl_seconds,
    )
    if fencing_token is None:
        return None
    return GmvMaxAccountSyncFence(
        lease_name=lease_name,
        owner_token=str(owner_token),
        fencing_token=int(fencing_token),
        redis_lock=redis_lock,
        ttl_seconds=ttl_seconds,
    )


def release_account_sync_fence(
    db: Session,
    *,
    fence: GmvMaxAccountSyncFence,
) -> bool:
    """Release only the exact execution generation that acquired the fence.

    Celery redelivery can reuse a task id.  The per-execution owner token and
    fencing-token comparison prevent a stale ``finally`` block from releasing
    a newer delivery's durable lease.
    """

    from app.features.tenants.ttb.gmv_max.control import (
        GMVMaxGuardActionLease,
        utcnow_naive,
    )

    row = db.execute(
        select(GMVMaxGuardActionLease)
        .where(GMVMaxGuardActionLease.lease_name == fence.lease_name)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        row is None
        or row.owner_token != fence.owner_token
        or int(row.fencing_token or 0) != int(fence.fencing_token)
    ):
        return False
    row.owner_token = None
    row.expires_at = utcnow_naive()
    db.flush()
    return True


def release_sync_fence(
    db: Session,
    *,
    fence: GmvMaxAccountSyncFence,
) -> bool:
    """Release an exact GMV Max sync generation."""

    return release_account_sync_fence(db, fence=fence)


__all__ = [
    "GmvMaxAccountSyncFence",
    "GmvMaxAccountSyncFenceLost",
    "account_sync_lock_key",
    "acquire_account_sync_fence",
    "acquire_creative_10min_sync_fence",
    "build_account_sync_lock",
    "build_creative_10min_sync_lock",
    "creative_10min_sync_fence_name",
    "creative_10min_sync_lock_key",
    "release_account_sync_fence",
    "release_sync_fence",
]
