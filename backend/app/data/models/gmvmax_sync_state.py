"""Persistent dispatcher state for bounded GMV Max synchronization sweeps."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class GmvCreative10MinSyncState(Base):
    """Last attempt/result for one product-campaign 10-minute metrics worker."""

    __tablename__ = "gmv_creative_10min_sync_state"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "campaign_id",
            name="uk_gmv_creative_10min_sync_scope",
        ),
        Index(
            "idx_gmv_creative_10min_sync_due",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "next_attempt_at",
            "last_attempt_at",
        ),
        Index(
            "idx_gmv_creative_10min_sync_attempt",
            "last_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), default=None)

    last_attempt_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    last_error_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    last_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'NEVER'"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_result_rows: Mapped[int | None] = mapped_column(Integer, default=None)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    attempt_token: Mapped[str | None] = mapped_column(String(36), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCreative10MinBatchManifest(Base):
    """Authoritative completeness watermark for one creative snapshot batch."""

    __tablename__ = "gmv_creative_10min_batch_manifests"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            "snapshot_at",
            name="uk_gmv_creative_10min_batch_scope",
        ),
        Index(
            "idx_gmv_creative_10min_batch_latest",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            "complete",
            "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
    )
    complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    row_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    source_observed_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvSyncSelectionCursor(Base):
    """Frozen-round cursor for one explicitly capped monitoring strategy."""

    __tablename__ = "gmv_sync_selection_cursors"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "cursor_key",
            name="uk_gmv_sync_selection_cursor",
        ),
        Index(
            "idx_gmv_sync_selection_strategy",
            "strategy_id",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("gmvmax_monitoring_strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    cursor_key: Mapped[str] = mapped_column(String(191), nullable=False)
    high_water_id: Mapped[int] = mapped_column(
        UBigInt,
        nullable=False,
        server_default=text("0"),
    )
    last_id: Mapped[int] = mapped_column(
        UBigInt,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )
