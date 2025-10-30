# app/data/models/scheduling.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    String,
    Boolean,
    Integer,
    Enum,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.dialects.mysql import ENUM as MySQL_ENUM  # noqa: F401  # 保留以兼容历史

from app.data.db import Base

# 通用 BigInt + MySQL 无符号 BIGINT 变体
UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer, "sqlite")
)

# ========================= 任务目录 =========================
class TaskCatalog(Base):
    __tablename__ = "task_catalog"
    __table_args__ = (
        UniqueConstraint("task_name", name="uq_task_catalog_name"),
        Index("idx_catalog_enabled", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    # 任务稳定标识（如：tenant.oauth.health_check）
    task_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # 实现版本（任务升级时递增，用于兼容/迁移）
    impl_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # 入参与校验（JSON Schema）
    input_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    # 默认队列/限制/重试/超时（建议值；可在 schedule 层覆写，当前版本先读取目录建议值）
    default_queue: Mapped[str | None] = mapped_column(String(64), default=None)
    rate_limit: Mapped[str | None] = mapped_column(String(32), default=None)  # 例 "10/m"
    timeout_s: Mapped[int | None] = mapped_column(Integer, default=None)
    max_retries: Mapped[int | None] = mapped_column(Integer, default=3)

    # 可见性：tenant / platform / internal（先留可空，默认 tenant）
    visibility: Mapped[str | None] = mapped_column(String(16), default="tenant")

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# ========================= 租户计划 =========================
class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        Index("idx_sched_ws_en_next", "workspace_id", "enabled", "next_fire_at"),
        Index("idx_sched_ws_name", "workspace_id", "task_name"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    task_name: Mapped[str] = mapped_column(
        String(128), ForeignKey("task_catalog.task_name", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False
    )

    schedule_type: Mapped[str] = mapped_column(
        Enum("interval", "crontab", "oneoff", name="schedule_type"),
        nullable=False,
    )

    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    timezone: Mapped[str | None] = mapped_column(String(64), default="UTC")

    # 三选一
    interval_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    crontab_expr: Mapped[str | None] = mapped_column(String(64), default=None)
    oneoff_run_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    # 调度行为
    misfire_grace_s: Mapped[int | None] = mapped_column(Integer, default=300)
    jitter_s: Mapped[int | None] = mapped_column(Integer, default=0)

    # 生效与下次触发
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    next_fire_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), default=None
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

    catalog: Mapped["TaskCatalog"] = relationship(
        "TaskCatalog", primaryjoin="Schedule.task_name==TaskCatalog.task_name", lazy="joined"
    )


# ========================= 触发/执行轨迹 =========================
class ScheduleRun(Base):
    __tablename__ = "schedule_runs"
    __table_args__ = (
        Index("idx_runs_sched_time", "schedule_id", "scheduled_for"),
        Index("idx_runs_ws_time", "workspace_id", "scheduled_for"),
        Index("idx_runs_status", "status"),
        Index("idx_runs_broker_msg_id", "broker_msg_id"),
        UniqueConstraint("schedule_id", "idempotency_key", name="uq_runs_sched_idem"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    schedule_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("schedules.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    scheduled_for: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    enqueued_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    # 来自 broker / celery 的任务 id
    broker_msg_id: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(
        Enum("enqueued", "running", "success", "failed", "partial", name="schedule_run_status"),
        nullable=False,
        server_default=text("'enqueued'"),
    )

    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)

    stats_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    # 幂等键（每个触发窗口唯一）
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

