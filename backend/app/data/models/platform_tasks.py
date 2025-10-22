"""SQLAlchemy models for platform task management and tenant sync telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.dialects.sqlite import INTEGER as SQLite_INTEGER

from app.data.db import Base


# Shared BigInt definition with MySQL unsigned variant support
UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(SQLite_INTEGER(), "sqlite")
)


class PlatformTaskCatalog(Base):
    """Static catalog describing platform orchestrated tasks."""

    __tablename__ = "platform_task_catalog"
    __table_args__ = (
        UniqueConstraint("task_key", name="uq_platform_task_catalog_task"),
        Index("idx_platform_task_catalog_visibility", "visibility"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="platform"
    )
    supports_whitelist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_blacklist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_tags: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    defaults_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    input_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    config: Mapped["PlatformTaskConfig | None"] = relationship(
        back_populates="catalog", uselist=False, lazy="selectin"
    )


class PlatformTaskConfig(Base):
    """Persisted configuration for a catalog task."""

    __tablename__ = "platform_task_config"
    __table_args__ = (
        Index("idx_platform_task_config_enabled", "is_enabled"),
    )

    task_key: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("platform_task_catalog.task_key", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    schedule_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="interval")
    schedule_interval_sec: Mapped[int | None] = mapped_column(Integer, default=None)
    schedule_cron: Mapped[str | None] = mapped_column(String(64), default=None)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    schedule_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    schedule_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    rate_limit_per_workspace_min_interval_sec: Mapped[int | None] = mapped_column(Integer, default=None)
    rate_limit_global_concurrency: Mapped[int | None] = mapped_column(Integer, default=None)
    rate_limit_per_workspace_concurrency: Mapped[int | None] = mapped_column(Integer, default=None)

    targeting_whitelist_workspace_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    targeting_blacklist_workspace_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    targeting_include_tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    targeting_exclude_tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    target_snapshot_workspace_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    target_snapshot_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(255), default=None)
    updated_by_user_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    catalog: Mapped[PlatformTaskCatalog] = relationship(
        back_populates="config", lazy="joined"
    )


class WorkspaceTag(Base):
    """Tag assignments for workspaces."""

    __tablename__ = "workspace_tags"
    __table_args__ = (
        UniqueConstraint("workspace_id", "tag", name="uq_workspace_tag"),
        Index("idx_workspace_tag_tag", "tag"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class PlatformTaskRun(Base):
    """Aggregate execution record for a platform task run."""

    __tablename__ = "platform_task_run"
    __table_args__ = (
        Index("idx_platform_task_run_task", "task_key"),
        Index("idx_platform_task_run_started_at", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_key: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("platform_task_catalog.task_key", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(512), default=None)
    stats_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_sec: Mapped[int | None] = mapped_column(Integer, default=None)
    version: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    workspaces: Mapped[list["PlatformTaskRunWorkspace"]] = relationship(
        back_populates="run", lazy="selectin"
    )


class PlatformTaskRunWorkspace(Base):
    """Per-workspace breakdown for a platform task run."""

    __tablename__ = "platform_task_run_workspace"
    __table_args__ = (
        Index("idx_platform_task_run_workspace_run", "run_id"),
        Index("idx_platform_task_run_workspace_ws", "workspace_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("platform_task_run.run_id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    count: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    run: Mapped[PlatformTaskRun] = relationship(back_populates="workspaces", lazy="joined")


class TenantSyncJob(Base):
    """Binding-level sync job record for tenant triggered workflows."""

    __tablename__ = "tenant_sync_jobs"
    __table_args__ = (
        Index("idx_tenant_sync_jobs_ws_auth", "workspace_id", "auth_id"),
        Index("idx_tenant_sync_jobs_triggered", "triggered_at"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    summary: Mapped[str | None] = mapped_column(String(512), default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)

    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_allowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )


class RateLimitToken(Base):
    """Rate limit windows for platform/tenant scoped throttling."""

    __tablename__ = "rate_limit_tokens"
    __table_args__ = (
        UniqueConstraint("scope", "token_key", name="uq_rate_limit_scope_key"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    token_key: Mapped[str] = mapped_column(String(255), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_allowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )


class IdempotencyKey(Base):
    """Recorded idempotency keys with associated response payload."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    last_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    last_job_id: Mapped[str | None] = mapped_column(String(64), default=None)

