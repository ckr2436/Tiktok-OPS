from __future__ import annotations

"""Persistent control-plane primitives for tenant GMV Max operations.

This module intentionally contains no table-creation fallback. Production
schema changes are owned by Alembic so GET requests can remain read-only.
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.config import settings
from app.data.db import Base


UBigInt = (
    BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer, "sqlite")
)
UTCDateTime = DateTime().with_variant(MySQL_DATETIME(fsp=6), "mysql")

TASK_OWNERSHIP_TTL_DAYS = 7
MANUAL_UPLOAD_URL_TTL_SECONDS = 30 * 60
MANUAL_UPLOAD_URL_MAX_TTL_SECONDS = 60 * 60
MANUAL_UPLOAD_ROOT = Path(
    os.getenv(
        "GMVMAX_MANUAL_UPLOAD_ROOT",
        "/opt/gmv/GMV-OPS/backend/var/gmvmax/manual_uploads",
    )
)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def canonical_provider(provider: str) -> str:
    return str(provider or "").strip().lower().replace("_", "-")


def ensure_private_manual_upload_directory(*parts: str | int) -> Path:
    """Create every upload directory from the private root with mode 0750."""

    base_dir = MANUAL_UPLOAD_ROOT.resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    base_dir.chmod(0o750)

    target_dir = base_dir
    for part in parts:
        component = str(part).strip()
        if not component or component in {".", ".."} or "/" in component or "\\" in component:
            raise ValueError("invalid manual upload path component")
        target_dir = target_dir / component
        target_dir.mkdir(exist_ok=True)
        target_dir.chmod(0o750)
    return target_dir


class GMVMaxTaskOwnership(Base):
    __tablename__ = "gmvmax_task_ownership"
    __table_args__ = (
        Index("idx_gmvmax_task_owner_scope", "workspace_id", "auth_id", "created_at"),
        Index("idx_gmvmax_task_owner_expiry", "expires_at"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class GMVMaxSyncSchedule(Base):
    __tablename__ = "gmvmax_sync_schedules"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "provider",
            name="uq_gmvmax_sync_schedule_scope",
        ),
        Index("idx_gmvmax_sync_schedule_due", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )


class GMVMaxCampaignManualOverride(Base):
    __tablename__ = "gmvmax_campaign_manual_overrides"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            name="uq_gmvmax_manual_override_campaign",
        ),
        Index(
            "idx_gmvmax_manual_override_active",
            "workspace_id",
            "auth_id",
            "active",
            "override_type",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    override_type: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(191), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    override_started_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    external_enable_first_observed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    external_enable_last_observed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    external_enable_observation_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )


class GMVMaxCampaignPauseIntent(Base):
    """Durable, coalesced user-pause request awaiting the mutation lease.

    A pause is safety-critical: a short-lived account sync must never turn a
    deliberate operator action into a 409 that the UI can accidentally drop.
    ``active_key`` is populated only while work remains and makes repeated
    clicks idempotent without preventing a later pause after an enable.
    """

    __tablename__ = "gmvmax_campaign_pause_intents"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_gmvmax_pause_intent_active"),
        Index(
            "idx_gmvmax_pause_intent_due",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index(
            "idx_gmvmax_pause_intent_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    actor: Mapped[str | None] = mapped_column(String(191), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )


class GMVMaxGuardActionLease(Base):
    """Durable companion to the Redis guard lock with a monotonic fence."""

    __tablename__ = "gmvmax_guard_action_leases"

    lease_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(
        UBigInt,
        nullable=False,
        default=0,
    )
    acquired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )


def acquire_guard_action_lease(
    db: Session,
    *,
    lease_name: str,
    owner_token: str,
    ttl_seconds: int = 30 * 60,
) -> int | None:
    """Acquire a database-backed guard lease and return its fencing token."""

    now = utcnow_naive()
    row = db.execute(
        select(GMVMaxGuardActionLease)
        .where(GMVMaxGuardActionLease.lease_name == str(lease_name))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        row = GMVMaxGuardActionLease(
            lease_name=str(lease_name),
            owner_token=str(owner_token),
            fencing_token=1,
            acquired_at=now,
            expires_at=now + timedelta(seconds=max(60, int(ttl_seconds))),
        )
        db.add(row)
        db.flush()
        return 1
    if (
        row.owner_token
        and row.owner_token != str(owner_token)
        and row.expires_at is not None
        and row.expires_at > now
    ):
        return None
    row.owner_token = str(owner_token)
    row.fencing_token = int(row.fencing_token or 0) + 1
    row.acquired_at = now
    row.expires_at = now + timedelta(seconds=max(60, int(ttl_seconds)))
    db.flush()
    return int(row.fencing_token)


def release_guard_action_lease(
    db: Session,
    *,
    lease_name: str,
    owner_token: str,
    fencing_token: int | None = None,
) -> bool:
    row = db.execute(
        select(GMVMaxGuardActionLease)
        .where(GMVMaxGuardActionLease.lease_name == str(lease_name))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        row is None
        or row.owner_token != str(owner_token)
        or (
            fencing_token is not None
            and int(row.fencing_token or 0) != int(fencing_token)
        )
    ):
        return False
    row.owner_token = None
    row.expires_at = utcnow_naive()
    db.flush()
    return True


def assert_guard_action_lease_generation(
    db: Session,
    *,
    lease_name: str,
    owner_token: str,
    fencing_token: int,
    ttl_seconds: int = 30 * 60,
    redis_lock: Any | None = None,
) -> int:
    """Synchronously prove and renew one exact Guard lease generation."""

    if not owner_token or int(fencing_token or 0) <= 0:
        raise RuntimeError("GMV Max guard mutation fence is not owned")
    if redis_lock is not None:
        verifier = getattr(redis_lock, "verify_ownership", None)
        if callable(verifier):
            redis_owned = bool(verifier())
        else:
            redis_owned = bool(
                getattr(redis_lock, "acquired", False)
                and not getattr(redis_lock, "lost", False)
            )
        if not redis_owned:
            raise RuntimeError("GMV Max guard Redis ownership was lost")

    now = utcnow_naive()
    row = db.execute(
        select(GMVMaxGuardActionLease)
        .where(GMVMaxGuardActionLease.lease_name == str(lease_name))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        row is None
        or row.owner_token != str(owner_token)
        or int(row.fencing_token or 0) != int(fencing_token)
        or row.expires_at is None
        or row.expires_at <= now
    ):
        raise RuntimeError("GMV Max guard mutation fence was lost")
    row.expires_at = now + timedelta(seconds=max(60, int(ttl_seconds)))
    db.flush()
    return int(fencing_token)


def assert_guard_action_lease_current(
    db: Session,
    *,
    lease_name: str = "gmvmax:guard-actions:cycle",
    ttl_seconds: int = 30 * 60,
) -> int:
    """Fence every external Guard mutation and renew the durable lease."""

    owner_token = str(db.info.get("gmvmax_guard_owner_token") or "")
    fencing_token = int(db.info.get("gmvmax_guard_fencing_token") or 0)
    redis_lock = db.info.get("gmvmax_guard_redis_lock")
    if redis_lock is None:
        raise RuntimeError("GMV Max guard mutation fence is not owned")
    return assert_guard_action_lease_generation(
        db,
        lease_name=lease_name,
        owner_token=owner_token,
        fencing_token=fencing_token,
        ttl_seconds=ttl_seconds,
        redis_lock=redis_lock,
    )


def new_owned_task_id() -> str:
    return str(uuid4())


def record_task_ownership(
    db: Session,
    *,
    task_id: str,
    workspace_id: int,
    auth_id: int,
    provider: str,
    task_name: str,
    created_by_user_id: int | None = None,
    expires_at: datetime | None = None,
) -> GMVMaxTaskOwnership:
    row = GMVMaxTaskOwnership(
        task_id=str(task_id),
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        provider=canonical_provider(provider),
        task_name=str(task_name),
        created_by_user_id=created_by_user_id,
        expires_at=expires_at
        or (utcnow_naive() + timedelta(days=TASK_OWNERSHIP_TTL_DAYS)),
    )
    db.add(row)
    db.flush()
    return row


def remove_task_ownership(db: Session, task_id: str) -> None:
    db.execute(
        delete(GMVMaxTaskOwnership).where(
            GMVMaxTaskOwnership.task_id == str(task_id)
        )
    )


def task_is_owned(
    db: Session,
    *,
    task_id: str,
    workspace_id: int,
    auth_id: int | None = None,
    provider: str | None = None,
) -> bool:
    stmt = select(GMVMaxTaskOwnership.task_id).where(
        GMVMaxTaskOwnership.task_id == str(task_id),
        GMVMaxTaskOwnership.workspace_id == int(workspace_id),
        GMVMaxTaskOwnership.expires_at > utcnow_naive(),
    )
    if auth_id is not None:
        stmt = stmt.where(GMVMaxTaskOwnership.auth_id == int(auth_id))
    if provider is not None:
        stmt = stmt.where(
            GMVMaxTaskOwnership.provider == canonical_provider(provider)
        )
    return db.execute(stmt.limit(1)).scalar_one_or_none() is not None


def get_sync_schedule(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    provider: str,
) -> GMVMaxSyncSchedule | None:
    return db.execute(
        select(GMVMaxSyncSchedule).where(
            GMVMaxSyncSchedule.workspace_id == int(workspace_id),
            GMVMaxSyncSchedule.auth_id == int(auth_id),
            GMVMaxSyncSchedule.provider == canonical_provider(provider),
        )
    ).scalar_one_or_none()


def upsert_sync_schedule(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    provider: str,
    advertiser_id: str,
    store_id: str | None,
    interval_minutes: int,
    actor_user_id: int | None,
) -> GMVMaxSyncSchedule:
    row = get_sync_schedule(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        provider=provider,
    )
    now = utcnow_naive()
    if row is None:
        row = GMVMaxSyncSchedule(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            provider=canonical_provider(provider),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id) if store_id else None,
            interval_minutes=int(interval_minutes),
            enabled=True,
            next_run_at=now + timedelta(minutes=int(interval_minutes)),
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
        )
        db.add(row)
    else:
        row.advertiser_id = str(advertiser_id)
        row.store_id = str(store_id) if store_id else None
        row.interval_minutes = int(interval_minutes)
        row.enabled = True
        row.next_run_at = now + timedelta(minutes=int(interval_minutes))
        row.last_error = None
        row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def set_manual_pause_override(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
    actor: str | None,
    reason: str | None = None,
    pending: bool = False,
) -> GMVMaxCampaignManualOverride:
    now = utcnow_naive()
    row = db.execute(
        select(GMVMaxCampaignManualOverride).where(
            GMVMaxCampaignManualOverride.workspace_id == int(workspace_id),
            GMVMaxCampaignManualOverride.auth_id == int(auth_id),
            GMVMaxCampaignManualOverride.advertiser_id == str(advertiser_id),
            GMVMaxCampaignManualOverride.store_id
            == (str(store_id) if store_id else None),
            GMVMaxCampaignManualOverride.campaign_id == str(campaign_id),
        )
    ).scalar_one_or_none()
    if row is None:
        row = GMVMaxCampaignManualOverride(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id) if store_id else None,
            campaign_id=str(campaign_id),
            override_type="PENDING_PAUSE" if pending else "PAUSE",
        )
        db.add(row)
    row.advertiser_id = str(advertiser_id)
    row.store_id = str(store_id) if store_id else None
    row.override_type = "PENDING_PAUSE" if pending else "PAUSE"
    row.active = True
    row.reason = reason
    row.actor = actor
    row.expires_at = None
    row.override_started_at = now
    row.external_enable_first_observed_at = None
    row.external_enable_last_observed_at = None
    row.external_enable_observation_count = 0
    row.resolved_at = None
    row.resolution_type = None
    db.flush()
    return row


def clear_manual_override(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
    resolution_type: str = "CONTROL_PLANE_CLEAR",
) -> None:
    row = db.execute(
        select(GMVMaxCampaignManualOverride).where(
            GMVMaxCampaignManualOverride.workspace_id == int(workspace_id),
            GMVMaxCampaignManualOverride.auth_id == int(auth_id),
            GMVMaxCampaignManualOverride.advertiser_id == str(advertiser_id),
            GMVMaxCampaignManualOverride.store_id
            == (str(store_id) if store_id else None),
            GMVMaxCampaignManualOverride.campaign_id == str(campaign_id),
        )
    ).scalar_one_or_none()
    if row is not None:
        row.active = False
        row.resolved_at = utcnow_naive()
        row.resolution_type = str(resolution_type or "CONTROL_PLANE_CLEAR")[:64]
        db.flush()


def is_manual_pause_override_active(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
    now: datetime | None = None,
) -> bool:
    current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    stmt = select(GMVMaxCampaignManualOverride.id).where(
        GMVMaxCampaignManualOverride.workspace_id == int(workspace_id),
        GMVMaxCampaignManualOverride.auth_id == int(auth_id),
        GMVMaxCampaignManualOverride.advertiser_id == str(advertiser_id),
        GMVMaxCampaignManualOverride.store_id
        == (str(store_id) if store_id else None),
        GMVMaxCampaignManualOverride.campaign_id == str(campaign_id),
        GMVMaxCampaignManualOverride.active.is_(True),
        GMVMaxCampaignManualOverride.override_type.in_(("PAUSE", "PENDING_PAUSE")),
    )
    stmt = stmt.where(
        (GMVMaxCampaignManualOverride.expires_at.is_(None))
        | (GMVMaxCampaignManualOverride.expires_at > current)
    )
    return db.execute(stmt.limit(1)).scalar_one_or_none() is not None


def _pause_intent_active_key(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
) -> str:
    value = ":".join(
        (
            "gmvmax-pause-v1",
            str(int(workspace_id)),
            str(int(auth_id)),
            str(advertiser_id),
            str(store_id or ""),
            str(campaign_id),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_or_get_campaign_pause_intent(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
    actor: str | None,
    reason: str | None = None,
) -> tuple[GMVMaxCampaignPauseIntent, bool]:
    """Persist one active pause intent for an exact tenant campaign scope."""

    active_key = _pause_intent_active_key(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
    )
    existing = db.execute(
        select(GMVMaxCampaignPauseIntent)
        .where(GMVMaxCampaignPauseIntent.active_key == active_key)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    intent = GMVMaxCampaignPauseIntent(
        id=str(uuid4()),
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=str(advertiser_id),
        store_id=str(store_id) if store_id else None,
        campaign_id=str(campaign_id),
        active_key=active_key,
        status="PENDING",
        actor=str(actor) if actor else None,
        reason=str(reason)[:500] if reason else None,
        next_attempt_at=utcnow_naive(),
    )
    try:
        with db.begin_nested():
            db.add(intent)
            db.flush()
        return intent, True
    except IntegrityError:
        # A concurrent double-click won the unique active-key race.  The
        # outer transaction remains usable because the failed insert used a
        # savepoint.
        existing = db.execute(
            select(GMVMaxCampaignPauseIntent)
            .where(GMVMaxCampaignPauseIntent.active_key == active_key)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False


def cancel_pending_campaign_pause_intent(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
) -> None:
    """A later deliberate enable/delete supersedes an unexecuted pause."""

    active_key = _pause_intent_active_key(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
    )
    row = db.execute(
        select(GMVMaxCampaignPauseIntent)
        .where(GMVMaxCampaignPauseIntent.active_key == active_key)
        .with_for_update()
    ).scalar_one_or_none()
    if row is not None:
        row.status = "CANCELLED"
        row.active_key = None
        row.lease_owner = None
        row.lease_expires_at = None
        row.completed_at = utcnow_naive()
        db.flush()


def claim_campaign_pause_intent(
    db: Session,
    *,
    intent_id: str,
    owner_token: str,
    lease_seconds: int = 90,
) -> GMVMaxCampaignPauseIntent | None:
    """Atomically claim a due or abandoned intent for exactly one worker."""

    now = utcnow_naive()
    row = db.execute(
        select(GMVMaxCampaignPauseIntent)
        .where(GMVMaxCampaignPauseIntent.id == str(intent_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.active_key is None:
        return None
    if row.status == "RUNNING" and row.lease_expires_at and row.lease_expires_at > now:
        return None
    if row.next_attempt_at and row.next_attempt_at > now:
        return None
    row.status = "RUNNING"
    row.lease_owner = str(owner_token)
    row.lease_expires_at = now + timedelta(seconds=max(30, int(lease_seconds)))
    row.attempt_count = int(row.attempt_count or 0) + 1
    db.flush()
    return row


def defer_campaign_pause_intent(
    db: Session,
    *,
    intent_id: str,
    owner_token: str,
    countdown_seconds: int,
    error: str | None = None,
) -> bool:
    row = db.execute(
        select(GMVMaxCampaignPauseIntent)
        .where(GMVMaxCampaignPauseIntent.id == str(intent_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.active_key is None or row.lease_owner != str(owner_token):
        return False
    row.status = "PENDING"
    row.lease_owner = None
    row.lease_expires_at = None
    row.next_attempt_at = utcnow_naive() + timedelta(
        seconds=max(1, int(countdown_seconds))
    )
    row.last_error = str(error)[:4000] if error else None
    db.flush()
    return True


def complete_campaign_pause_intent(
    db: Session,
    *,
    intent_id: str,
    owner_token: str,
    status: str,
    error: str | None = None,
) -> bool:
    """Terminally finish an owned intent and release its active-key slot."""

    row = db.execute(
        select(GMVMaxCampaignPauseIntent)
        .where(GMVMaxCampaignPauseIntent.id == str(intent_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.active_key is None or row.lease_owner != str(owner_token):
        return False
    row.status = str(status).upper()
    row.active_key = None
    row.lease_owner = None
    row.lease_expires_at = None
    row.next_attempt_at = None
    row.last_error = str(error)[:4000] if error else None
    row.completed_at = utcnow_naive()
    db.flush()
    return True


def due_campaign_pause_intent_ids(
    db: Session,
    *,
    limit: int = 100,
) -> list[str]:
    """Return a bounded, stable recovery batch without claiming it yet."""

    now = utcnow_naive()
    rows = db.execute(
        select(GMVMaxCampaignPauseIntent.id)
        .where(
            GMVMaxCampaignPauseIntent.active_key.is_not(None),
            (
                (GMVMaxCampaignPauseIntent.status == "PENDING")
                & (
                    (GMVMaxCampaignPauseIntent.next_attempt_at.is_(None))
                    | (GMVMaxCampaignPauseIntent.next_attempt_at <= now)
                )
            )
            | (
                (GMVMaxCampaignPauseIntent.status == "RUNNING")
                & (
                    (GMVMaxCampaignPauseIntent.lease_expires_at.is_(None))
                    | (GMVMaxCampaignPauseIntent.lease_expires_at <= now)
                )
            ),
        )
        .order_by(GMVMaxCampaignPauseIntent.created_at.asc())
        .limit(max(1, min(int(limit), 500)))
    ).scalars().all()
    return [str(value) for value in rows]


def recover_orphaned_pending_pause_override_intent_ids(
    db: Session,
    *,
    limit: int = 100,
) -> list[str]:
    """Recreate an intent if an interrupted worker left only a pending hold.

    ``PENDING_PAUSE`` is deliberately fail-safe: Guard must stop mutating the
    campaign before the remote pause finishes.  It must never become a
    permanent dead-end when a worker dies or incorrectly reports completion.
    """

    rows = db.execute(
        select(GMVMaxCampaignManualOverride)
        .where(
            GMVMaxCampaignManualOverride.active.is_(True),
            GMVMaxCampaignManualOverride.override_type == "PENDING_PAUSE",
        )
        .order_by(GMVMaxCampaignManualOverride.updated_at.asc())
        .limit(max(1, min(int(limit), 500)))
    ).scalars().all()
    recovered: list[str] = []
    for row in rows:
        intent, created = create_or_get_campaign_pause_intent(
            db,
            workspace_id=int(row.workspace_id),
            auth_id=int(row.auth_id),
            advertiser_id=str(row.advertiser_id),
            store_id=str(row.store_id) if row.store_id else None,
            campaign_id=str(row.campaign_id),
            actor=row.actor,
            reason=row.reason,
        )
        if created:
            recovered.append(str(intent.id))
    return recovered


def _upload_signature_message(
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_id: str,
    expires: int,
) -> bytes:
    return (
        f"gmvmax-upload:v1:{int(workspace_id)}:{canonical_provider(provider)}:"
        f"{int(auth_id)}:{str(upload_id)}:{int(expires)}"
    ).encode("utf-8")


def sign_manual_upload(
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_id: str,
    expires: int,
) -> str:
    digest = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        _upload_signature_message(
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            upload_id=upload_id,
            expires=expires,
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_manual_upload_signature(
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_id: str,
    expires: int,
    signature: str,
    now_epoch: int | None = None,
) -> bool:
    now = int(now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp())
    expiry = int(expires)
    if expiry <= now or expiry > now + MANUAL_UPLOAD_URL_MAX_TTL_SECONDS:
        return False
    expected = sign_manual_upload(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        upload_id=upload_id,
        expires=expiry,
    )
    return hmac.compare_digest(expected, str(signature or ""))


def build_manual_upload_url(
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_id: str,
    ttl_seconds: int = MANUAL_UPLOAD_URL_TTL_SECONDS,
) -> str:
    ttl = max(60, min(int(ttl_seconds), MANUAL_UPLOAD_URL_MAX_TTL_SECONDS))
    expires = int(datetime.now(timezone.utc).timestamp()) + ttl
    signature = sign_manual_upload(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        upload_id=upload_id,
        expires=expires,
    )
    path = (
        f"{settings.API_PREFIX}/tenants/{int(workspace_id)}/providers/"
        f"{quote(canonical_provider(provider), safe='')}/accounts/{int(auth_id)}"
        f"/gmvmax/creative-assets/uploads/{quote(str(upload_id), safe='')}/file"
        f"?expires={expires}&signature={quote(signature, safe='')}"
    )
    issuer = str(settings.ISSUER or "").rstrip("/")
    return f"{issuer}{path}" if issuer else path
