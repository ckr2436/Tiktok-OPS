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
from sqlalchemy.dialects.mysql import MEDIUMTEXT as MySQL_MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base

UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)
LongText = Text().with_variant(MySQL_MEDIUMTEXT(), "mysql")


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
        UniqueConstraint(
            "conversation_id",
            "role",
            "run_id",
            name="uq_hermes_message_turn_role",
        ),
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
    content_text: Mapped[str | None] = mapped_column(LongText, default=None)
    content_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, default=None)
    run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )


class HermesContentProducerAttachment(Base):
    """A user-owned intake file staged before explicit project confirmation."""

    __tablename__ = "hermes_content_producer_attachments"
    __table_args__ = (
        UniqueConstraint("attachment_key", name="uq_content_producer_attachment_key"),
        Index(
            "idx_content_producer_attachment_scope",
            "workspace_id",
            "user_id",
            "conversation_id",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    attachment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_agent_conversations.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    preview_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    mime_type: Mapped[str | None] = mapped_column(String(128), default=None)
    size_bytes: Mapped[int] = mapped_column(UBigInt, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ready'")
    )
    analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    project_asset_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
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
    # Preserve the complete upstream answer.  Long Director/analyst outputs
    # can exceed MySQL TEXT's 64 KiB boundary and must remain identical to the
    # assistant message and provider response envelope.
    result_text: Mapped[str | None] = mapped_column(LongText, default=None)
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


class HermesContentFactoryProject(Base):
    __tablename__ = "hermes_content_factory_projects"
    __table_args__ = (
        UniqueConstraint("project_key", name="uq_hermes_content_project_key"),
        Index("idx_hermes_content_project_ws_user", "workspace_id", "user_id", "updated_at"),
        Index("idx_hermes_content_project_status", "status", "current_stage"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    project_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("hermes_content_products.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'US'"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'draft'"))
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'INTAKE'"))
    product_brief: Mapped[str | None] = mapped_column(LongText, default=None)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    state_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentSeriesSlate(Base):
    """Immutable project-level Director slate and its review evidence."""

    __tablename__ = "hermes_content_series_slates"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "series_id",
            "series_version",
            name="uq_hermes_content_series_slate_version",
        ),
        UniqueConstraint(
            "project_id",
            "slate_sha256",
            name="uq_hermes_content_series_slate_sha",
        ),
        Index(
            "idx_hermes_content_series_slate_project",
            "project_id",
            "series_version",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(
        UBigInt,
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_projects.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    series_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    brief_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    slate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    brief_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    slate_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    reviews_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentDirectorBrief(Base):
    """Immutable, project-scoped input contract for one video variant."""

    __tablename__ = "hermes_content_director_briefs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "brief_key",
            "execution_key",
            name="uq_hermes_content_director_brief_execution",
        ),
        UniqueConstraint(
            "project_id",
            "variant_index",
            "brief_version",
            "execution_key",
            name="uq_hermes_content_director_version_execution",
        ),
        Index(
            "idx_hermes_content_director_brief_project",
            "project_id",
            "variant_index",
            "brief_version",
        ),
        Index(
            "idx_hermes_content_director_brief_status",
            "status",
            "updated_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_projects.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    brief_key: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        server_default=text("'legacy'"),
    )
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'running'"),
    )
    brief_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    brief_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentDirectorArtifact(Base):
    """One runtime-signed director revision in an immutable ancestry chain."""

    __tablename__ = "hermes_content_director_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "brief_id",
            "artifact_key",
            "revision",
            name="uq_hermes_content_director_artifact_revision",
        ),
        UniqueConstraint(
            "brief_id",
            "artifact_sha256",
            name="uq_hermes_content_director_brief_artifact_sha",
        ),
        Index(
            "idx_hermes_content_director_artifact_project",
            "project_id",
            "variant_index",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    brief_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_director_briefs.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    project_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_artifact_sha256: Mapped[str | None] = mapped_column(
        String(64),
        default=None,
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentDirectorAttempt(Base):
    """Auditable accepted or contract-rejected author response."""

    __tablename__ = "hermes_content_director_attempts"
    __table_args__ = (
        UniqueConstraint(
            "brief_id",
            "artifact_key",
            "revision",
            "operation",
            "contract_repair_attempt",
            name="uq_hermes_content_director_attempt_identity",
        ),
        Index(
            "idx_hermes_content_director_attempt_project",
            "project_id",
            "variant_index",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    brief_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_director_briefs.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    project_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_repair_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    response_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_error: Mapped[str | None] = mapped_column(Text, default=None)
    model: Mapped[str | None] = mapped_column(String(255), default=None)
    request_id: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentDirectorReview(Base):
    """Independent critic decision for one exact artifact revision."""

    __tablename__ = "hermes_content_director_reviews"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "review_round",
            name="uq_hermes_content_director_review_round",
        ),
        Index(
            "idx_hermes_content_director_review_project",
            "project_id",
            "variant_index",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_director_artifacts.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    project_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    review_round: Mapped[int] = mapped_column(Integer, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    preflight_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    verdict_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    critic_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    critic_response_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentProductionPlanAudit(Base):
    """Immutable, variant-scoped evidence for one production-plan loop.

    The stage row remains the workflow checkpoint.  This table is the
    append-only authority that binds the accepted provider-independent plan
    to the exact Director artifact, author attempts, and independent Critic
    verdicts that produced it.
    """

    __tablename__ = "hermes_content_production_plan_audits"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "plan_sha256",
            name="uq_hermes_content_production_plan_sha",
        ),
        UniqueConstraint(
            "stage_id",
            name="uq_hermes_content_production_plan_stage",
        ),
        Index(
            "idx_hermes_content_production_plan_project",
            "project_id",
            "variant_index",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(
        UBigInt,
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_projects.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    stage_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_stages.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    director_artifact_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    critic_attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    reviews_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    contract_errors_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentProduct(Base):
    __tablename__ = "hermes_content_products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "product_key", name="uq_hermes_content_product_ws_key"),
        Index("idx_hermes_content_product_ws", "workspace_id", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    product_key: Mapped[str] = mapped_column(String(191), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'US'"))
    product_brief: Mapped[str | None] = mapped_column(Text, default=None)
    facts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentProductAsset(Base):
    __tablename__ = "hermes_content_product_assets"
    __table_args__ = (
        Index("idx_hermes_content_product_asset_product", "product_id", "kind", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_products.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'source'"))
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), default=None)
    size_bytes: Mapped[int | None] = mapped_column(UBigInt, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesBrowserBridge(Base):
    """A user/device-owned browser bridge used by Hermes content-factory runs."""

    __tablename__ = "hermes_browser_bridges"
    __table_args__ = (
        UniqueConstraint("bridge_id", name="uq_hermes_browser_bridge_id"),
        UniqueConstraint("workspace_id", "user_id", "device_id", name="uq_hermes_browser_bridge_device"),
        Index("idx_hermes_browser_bridge_ws_user", "workspace_id", "user_id", "last_seen_at"),
        Index("idx_hermes_browser_bridge_lease", "status", "active_project_id", "lease_expires_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    bridge_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(255), default=None)
    cdp_url: Mapped[str] = mapped_column(String(512), nullable=False)
    server_port: Mapped[int | None] = mapped_column(Integer, default=None)
    inbox_root: Mapped[str | None] = mapped_column(String(1024), default=None)
    outbox_root: Mapped[str | None] = mapped_column(String(1024), default=None)
    browser: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pending'"))
    active_project_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
    active_stage_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_seen_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    load_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentFactoryStage(Base):
    __tablename__ = "hermes_content_factory_stages"
    __table_args__ = (
        UniqueConstraint("project_id", "stage", "attempt", name="uq_hermes_content_stage_attempt"),
        Index("idx_hermes_content_stage_project", "project_id", "id"),
        Index("idx_hermes_content_stage_status", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'queued'"))
    instruction: Mapped[str | None] = mapped_column(Text, default=None)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # VIDEO_PROMPTS can legitimately exceed MySQL TEXT's 64 KiB limit once a
    # multi-segment compiler result includes per-segment timelines, reference
    # bindings, continuity constraints, and audit evidence.  Keep the complete
    # durable response instead of truncating the execution record.
    response_text: Mapped[str | None] = mapped_column(LongText, default=None)
    chat_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentFactoryAsset(Base):
    __tablename__ = "hermes_content_factory_assets"
    __table_args__ = (
        Index("idx_hermes_content_asset_project", "project_id", "kind", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    stage: Mapped[str | None] = mapped_column(String(32), default=None)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), default=None)
    size_bytes: Mapped[int | None] = mapped_column(UBigInt, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentExecution(Base):
    """Durable production run identity, independent from project JSON state."""

    __tablename__ = "hermes_content_executions"
    __table_args__ = (
        UniqueConstraint("project_id", "execution_key", name="uq_hermes_content_execution_key"),
        Index("idx_hermes_content_execution_scope", "workspace_id", "project_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    execution_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    started_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentVariantRun(Base):
    """One auditable internal attempt for a user-facing video ordinal."""

    __tablename__ = "hermes_content_variant_runs"
    __table_args__ = (
        UniqueConstraint("execution_id", "variant_index", "attempt", name="uq_hermes_content_variant_attempt"),
        Index("idx_hermes_content_variant_scope", "workspace_id", "project_id", "variant_index"),
        Index("idx_hermes_content_variant_state", "state", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_executions.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    deliverable_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    media_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    error_class: Mapped[str | None] = mapped_column(String(96), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    started_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentSegmentRun(Base):
    """Provider and local-file ledger for one immutable segment attempt."""

    __tablename__ = "hermes_content_segment_runs"
    __table_args__ = (
        UniqueConstraint("variant_run_id", "segment_index", "attempt", name="uq_hermes_content_segment_attempt"),
        Index("idx_hermes_content_segment_scope", "workspace_id", "project_id", "state"),
        Index("idx_hermes_content_segment_provider_task", "provider_task_row_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    variant_run_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_variant_runs.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_task_row_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("kie_api_tasks.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    local_file_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    error_class: Mapped[str | None] = mapped_column(String(96), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    started_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentDeliverable(Base):
    """Exactly one local deliverable row per ordinal and output kind."""

    __tablename__ = "hermes_content_deliverables"
    __table_args__ = (
        UniqueConstraint("execution_id", "deliverable_ordinal", "kind", name="uq_hermes_content_deliverable_ordinal_kind"),
        Index("idx_hermes_content_deliverable_scope", "workspace_id", "project_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_executions.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    variant_run_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_variant_runs.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_projects.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    deliverable_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("hermes_content_factory_assets.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)"), nullable=False)


class HermesContentEvaluation(Base):
    """Independent semantic or deterministic evidence for one media artifact.

    Provider transport state deliberately does not live here.  A successfully
    downloaded provider task remains transport-successful even when a
    multimodal reviewer recommends a bounded creative repair.
    """

    __tablename__ = "hermes_content_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_key",
            name="uq_hermes_content_evaluation_key",
        ),
        Index(
            "idx_hermes_content_evaluation_scope",
            "workspace_id",
            "project_id",
            "evaluation_kind",
            "status",
        ),
        Index(
            "idx_hermes_content_evaluation_task",
            "provider_task_row_id",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    evaluation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    user_id: Mapped[int | None] = mapped_column(UBigInt, nullable=True)
    project_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_projects.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    execution_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_executions.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    variant_run_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_variant_runs.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    segment_run_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_segment_runs.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    provider_task_row_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("kie_api_tasks.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    evaluation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class HermesContentRuntimeEvent(Base):
    """Transactional outbox event for execution-ledger state transitions."""

    __tablename__ = "hermes_content_runtime_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_hermes_content_runtime_event"),
        Index("idx_hermes_content_runtime_event_pending", "status", "available_at"),
        Index(
            "idx_hermes_content_runtime_event_scope",
            "workspace_id",
            "project_id",
            "created_at",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    project_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_factory_projects.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    execution_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_executions.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    variant_run_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_variant_runs.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    segment_run_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "hermes_content_segment_runs.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    provider_task_row_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("kie_api_tasks.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
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
    "HermesBrowserBridge",
    "HermesContentProduct",
    "HermesContentProductAsset",
    "HermesContentFactoryProject",
    "HermesContentFactoryStage",
    "HermesContentFactoryAsset",
    "HermesContentSeriesSlate",
    "HermesContentDirectorBrief",
    "HermesContentDirectorArtifact",
    "HermesContentDirectorAttempt",
    "HermesContentDirectorReview",
    "HermesContentProductionPlanAudit",
    "HermesContentExecution",
    "HermesContentVariantRun",
    "HermesContentSegmentRun",
    "HermesContentDeliverable",
    "HermesContentEvaluation",
    "HermesContentRuntimeEvent",
]
