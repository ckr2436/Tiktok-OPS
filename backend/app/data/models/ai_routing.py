from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = (
    BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class AiProviderModel(Base):
    """A provider-advertised model snapshot, never an automatic permission."""

    __tablename__ = "ai_provider_models"
    __table_args__ = (
        UniqueConstraint(
            "provider_key",
            "provider_model_id",
            name="uk_ai_provider_model_identity",
        ),
        Index("idx_ai_provider_model_available", "provider_key", "is_available"),
        Index("idx_ai_provider_model_lifecycle", "lifecycle_status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(191), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    capabilities_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    endpoint_modes_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    discovery_source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UPSTREAM'")
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DISCOVERED'")
    )
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    discovered_by_key_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("kie_api_keys.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
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


class AiModelRoute(Base):
    """One auditable provider/key route for a logical model and workload."""

    __tablename__ = "ai_model_routes"
    __table_args__ = (
        UniqueConstraint(
            "key_id",
            "workload",
            "logical_model_id",
            "provider_model_id",
            "capability",
            name="uk_ai_model_route_identity",
        ),
        Index(
            "idx_ai_model_route_select",
            "logical_model_id",
            "capability",
            "workload",
            "is_enabled",
            "priority",
        ),
        Index("idx_ai_model_route_health", "health_status", "circuit_open_until"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    key_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("kie_api_keys.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    workload: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'default'")
    )
    logical_model_id: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(191), nullable=False)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    health_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_successes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    latency_ema_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    circuit_open_until: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_error_class: Mapped[str | None] = mapped_column(String(64), default=None)
    last_error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class AiRouteAttempt(Base):
    """Metadata-only provider attempt log. Prompts and secrets are forbidden."""

    __tablename__ = "ai_route_attempts"
    __table_args__ = (
        Index("idx_ai_route_attempt_request", "request_id", "id"),
        Index("idx_ai_route_attempt_route", "route_id", "created_at"),
        Index("idx_ai_route_attempt_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("ai_model_routes.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(96), nullable=False)
    switched_from_route_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("ai_model_routes.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(64), default=None)
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )


__all__ = ["AiProviderModel", "AiModelRoute", "AiRouteAttempt"]
