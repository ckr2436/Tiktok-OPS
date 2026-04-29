from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base

UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class UserFeaturePermission(Base):
    """Per-user feature switches used for tenant tools such as Hermes Agent."""

    __tablename__ = "user_feature_permissions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "feature_key",
            "active_until",
            name="uq_user_feature_permission_active",
        ),
        Index("idx_user_feature_permission_user", "workspace_id", "user_id"),
        Index("idx_user_feature_permission_feature", "workspace_id", "feature_key", "is_enabled"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    feature_key: Mapped[str] = mapped_column(String(128), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    active_until: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        Computed(
            "COALESCE(`deleted_at`, CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)))",
            persisted=True,
        ),
    )


class HermesAgentConversation(Base):
    """Tenant-scoped conversation state mirrored from Hermes response chains."""

    __tablename__ = "hermes_agent_conversations"
    __table_args__ = (
        UniqueConstraint("conversation_key", name="uq_hermes_conversation_key"),
        Index("idx_hermes_conversation_ws_user", "workspace_id", "user_id", "task_type"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    conversation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    last_response_id: Mapped[str | None] = mapped_column(String(128), default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesAgentMessage(Base):
    """Local chat transcript for auditability and UI history."""

    __tablename__ = "hermes_agent_messages"
    __table_args__ = (
        Index("idx_hermes_message_conversation", "conversation_id", "id"),
        Index("idx_hermes_message_ws_user", "workspace_id", "user_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("hermes_agent_conversations.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, default=None)
    content_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, default=None)
    run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )


class HermesAgentRun(Base):
    """Single Hermes Agent execution, synchronous or Celery-backed."""

    __tablename__ = "hermes_agent_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_hermes_run_id"),
        Index("idx_hermes_run_ws_created", "workspace_id", "created_at"),
        Index("idx_hermes_run_ws_user", "workspace_id", "user_id"),
        Index("idx_hermes_run_status", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("hermes_agent_conversations.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pending'"))
    input_text: Mapped[str | None] = mapped_column(Text, default=None)
    input_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, default=None)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    result_text: Mapped[str | None] = mapped_column(Text, default=None)
    result_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, default=None)
    hermes_response_id: Mapped[str | None] = mapped_column(String(128), default=None)
    hermes_conversation: Mapped[str | None] = mapped_column(String(191), default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


__all__ = [
    "UserFeaturePermission",
    "HermesAgentConversation",
    "HermesAgentMessage",
    "HermesAgentRun",
]
