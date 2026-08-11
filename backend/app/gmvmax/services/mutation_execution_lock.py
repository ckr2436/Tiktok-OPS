"""Fenced execution boundary for every official GMV Max mutation."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from uuid import uuid4

from sqlalchemy.orm import Session

from app.gmvmax.services.sync_execution_lock import (
    GmvMaxAccountSyncFence,
    GmvMaxAccountSyncFenceLost,
    acquire_account_sync_fence,
    build_account_sync_lock,
    release_account_sync_fence,
)

logger = logging.getLogger("gmv.gmvmax.mutation_execution_lock")

_GLOBAL_LEASE_NAME = "gmvmax:guard-actions:cycle"
_GLOBAL_LEASE_TTL_SECONDS = 5 * 60
_ACTIVE_LEASE_INFO_KEY = "gmvmax_active_mutation_lease"


class GmvMaxMutationBusy(RuntimeError):
    """Raised when another sync or mutation owns the required scope."""


class GmvMaxMutationFenceLost(RuntimeError):
    """Raised when an execution cannot prove both lock generations."""


@dataclass
class GmvMaxMutationLease:
    """One exact global-then-account mutation generation."""

    workspace_id: int
    auth_id: int
    owner_token: str
    global_owner_token: str | None
    global_fencing_token: int | None
    global_redis_lock: object | None
    owns_global_lease: bool
    bypasses_automated_global_lease: bool
    account_lock: object
    account_fence: GmvMaxAccountSyncFence
    db_bind: object
    released: bool = False

    def assert_current(self, db: Session) -> None:
        """Synchronously prove both generations in fixed lock order."""

        from app.features.tenants.ttb.gmv_max.control import (
            assert_guard_action_lease_generation,
        )

        try:
            if not self.bypasses_automated_global_lease:
                assert_guard_action_lease_generation(
                    db,
                    lease_name=_GLOBAL_LEASE_NAME,
                    owner_token=str(self.global_owner_token),
                    fencing_token=int(self.global_fencing_token or 0),
                    ttl_seconds=_GLOBAL_LEASE_TTL_SECONDS,
                    redis_lock=self.global_redis_lock,
                )
            self.account_fence.assert_current(db)
        except (GmvMaxAccountSyncFenceLost, RuntimeError) as exc:
            raise GmvMaxMutationFenceLost(str(exc)) from exc

    def commit(self, db: Session) -> None:
        """Commit local facts only while this exact execution still owns both."""

        self.assert_current(db)
        db.commit()

    def release(self) -> None:
        """Release only generations owned by this concrete execution."""

        from app.features.tenants.ttb.gmv_max.control import (
            release_guard_action_lease,
        )

        if self.released:
            return
        self.released = True
        release_db = Session(bind=self.db_bind, expire_on_commit=False)
        try:
            release_account_sync_fence(release_db, fence=self.account_fence)
            if self.owns_global_lease:
                release_guard_action_lease(
                    release_db,
                    lease_name=_GLOBAL_LEASE_NAME,
                    owner_token=str(self.global_owner_token),
                    fencing_token=int(self.global_fencing_token or 0),
                )
            release_db.commit()
        except Exception:  # noqa: BLE001
            release_db.rollback()
            logger.exception(
                "failed to release GMV Max mutation generations",
                extra={
                    "workspace_id": self.workspace_id,
                    "auth_id": self.auth_id,
                    "owner_token": self.owner_token,
                    "global_fencing_token": self.global_fencing_token,
                    "account_fencing_token": self.account_fence.fencing_token,
                },
            )
        finally:
            release_db.close()
            try:
                self.account_lock.release()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to release GMV Max mutation Redis lease",
                    extra={
                        "workspace_id": self.workspace_id,
                        "auth_id": self.auth_id,
                        "owner_token": self.owner_token,
                    },
                )


def _release_standalone_global(
    db: Session,
    *,
    owner_token: str,
    fencing_token: int,
) -> None:
    from app.features.tenants.ttb.gmv_max.control import release_guard_action_lease

    try:
        db.rollback()
        release_guard_action_lease(
            db,
            lease_name=_GLOBAL_LEASE_NAME,
            owner_token=owner_token,
            fencing_token=int(fencing_token),
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "failed to release standalone GMV Max global mutation lease",
            extra={
                "owner_token": owner_token,
                "fencing_token": fencing_token,
            },
        )


def acquire_gmvmax_mutation_lease(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    owner_prefix: str,
    timeout: float = 0.2,
    bypass_automated_global_lease: bool = False,
) -> GmvMaxMutationLease:
    """Acquire global durable ownership, then account Redis+durable ownership."""

    from app.features.tenants.ttb.gmv_max.control import (
        acquire_guard_action_lease,
        assert_guard_action_lease_current,
    )

    owner_token = f"{str(owner_prefix)}:{uuid4()}"
    existing_global_owner = str(
        db.info.get("gmvmax_guard_owner_token") or ""
    )
    existing_global_token = int(
        db.info.get("gmvmax_guard_fencing_token") or 0
    )
    existing_global_redis = db.info.get("gmvmax_guard_redis_lock")
    bypasses_automated_global_lease = bool(
        bypass_automated_global_lease
        and existing_global_owner
        and not existing_global_owner.startswith("manual")
    )
    owns_global_lease = not bool(existing_global_owner)

    if bypasses_automated_global_lease:
        # Global Guard ownership is an automation-wide coordination aid, not
        # a reason to delay a human safety pause for another account. The
        # account Redis + durable fence below remains mandatory.
        db.commit()
        global_owner_token = None
        global_fencing_token = None
        global_redis_lock = None
        owns_global_lease = False
    elif existing_global_owner:
        try:
            assert_guard_action_lease_current(
                db,
                lease_name=_GLOBAL_LEASE_NAME,
                ttl_seconds=_GLOBAL_LEASE_TTL_SECONDS,
            )
            # End any earlier REPEATABLE READ snapshot before account
            # acquisition and before callers reload mutable campaign state.
            db.commit()
        except Exception as exc:
            db.rollback()
            raise GmvMaxMutationFenceLost(
                "the automated Guard global generation was lost"
            ) from exc
        global_owner_token = existing_global_owner
        global_fencing_token = existing_global_token
        global_redis_lock = existing_global_redis
    else:
        global_owner_token = owner_token
        global_fencing_token = int(
            acquire_guard_action_lease(
                db,
                lease_name=_GLOBAL_LEASE_NAME,
                owner_token=global_owner_token,
                ttl_seconds=_GLOBAL_LEASE_TTL_SECONDS,
            )
            or 0
        )
        if global_fencing_token <= 0:
            db.rollback()
            raise GmvMaxMutationBusy(
                "another GMV Max mutation owns the global Guard lease"
            )
        db.commit()
        global_redis_lock = None

    account_lock = build_account_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_token=owner_token,
    )
    if not account_lock.acquire(
        timeout=max(0.0, float(timeout)),
        retry_interval=0.05,
    ):
        if owns_global_lease:
            _release_standalone_global(
                db,
                owner_token=global_owner_token,
                fencing_token=global_fencing_token,
            )
        raise GmvMaxMutationBusy(
            "a GMV Max account sync or mutation is already running"
        )

    account_fence = None
    try:
        account_fence = acquire_account_sync_fence(
            db,
            redis_lock=account_lock,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_token=owner_token,
        )
        if account_fence is None:
            db.rollback()
            raise GmvMaxMutationBusy(
                "the durable GMV Max account mutation lease is busy"
            )
        db.commit()
        db.expire_all()
        return GmvMaxMutationLease(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_token=owner_token,
            global_owner_token=global_owner_token,
            global_fencing_token=int(global_fencing_token),
            global_redis_lock=global_redis_lock,
            owns_global_lease=owns_global_lease,
            bypasses_automated_global_lease=bypasses_automated_global_lease,
            account_lock=account_lock,
            account_fence=account_fence,
            db_bind=db.get_bind(),
        )
    except Exception:
        db.rollback()
        account_lock.release()
        if owns_global_lease:
            _release_standalone_global(
                db,
                owner_token=global_owner_token,
                fencing_token=global_fencing_token,
            )
        raise


@contextmanager
def gmvmax_mutation_lease(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    owner_prefix: str,
    timeout: float = 0.2,
    bypass_automated_global_lease: bool = False,
) -> Iterator[GmvMaxMutationLease]:
    """Install one reusable mutation lease for nested helper calls."""

    active = db.info.get(_ACTIVE_LEASE_INFO_KEY)
    if isinstance(active, GmvMaxMutationLease):
        if (
            active.workspace_id != int(workspace_id)
            or active.auth_id != int(auth_id)
        ):
            raise GmvMaxMutationFenceLost(
                "nested GMV Max mutation crossed its account scope"
            )
        active.assert_current(db)
        yield active
        return

    lease = acquire_gmvmax_mutation_lease(
        db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_prefix=owner_prefix,
        timeout=timeout,
        bypass_automated_global_lease=bypass_automated_global_lease,
    )
    db.info[_ACTIVE_LEASE_INFO_KEY] = lease
    try:
        yield lease
    finally:
        if db.info.get(_ACTIVE_LEASE_INFO_KEY) is lease:
            db.info.pop(_ACTIVE_LEASE_INFO_KEY, None)
        lease.release()


def active_gmvmax_mutation_lease(
    db: Session,
) -> GmvMaxMutationLease | None:
    lease = db.info.get(_ACTIVE_LEASE_INFO_KEY)
    return lease if isinstance(lease, GmvMaxMutationLease) else None


def assert_gmvmax_mutation_current(db: Session) -> None:
    lease = active_gmvmax_mutation_lease(db)
    if lease is None:
        raise GmvMaxMutationFenceLost(
            "GMV Max official mutation has no active execution lease"
        )
    lease.assert_current(db)


def commit_gmvmax_mutation(db: Session) -> None:
    lease = active_gmvmax_mutation_lease(db)
    if lease is None:
        raise GmvMaxMutationFenceLost(
            "GMV Max mutation commit has no active execution lease"
        )
    lease.commit(db)


__all__ = [
    "GmvMaxMutationBusy",
    "GmvMaxMutationFenceLost",
    "GmvMaxMutationLease",
    "acquire_gmvmax_mutation_lease",
    "active_gmvmax_mutation_lease",
    "assert_gmvmax_mutation_current",
    "commit_gmvmax_mutation",
    "gmvmax_mutation_lease",
]
