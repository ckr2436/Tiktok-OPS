"""Manual snapshot cache tables for GMV Max campaign metrics."""

from __future__ import annotations

from datetime import date, datetime

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class _SnapshotRowMixin:
    cost_cents: Mapped[int | None] = mapped_column(BigInteger)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger)
    orders: Mapped[int | None] = mapped_column(BigInteger)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger)
    roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    cost_per_order: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default="CURRENT_TIMESTAMP(6)"
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default="CURRENT_TIMESTAMP(6)",
        server_onupdate="CURRENT_TIMESTAMP(6)",
    )


class GmvmaxProductCampaignSnapshotBatch(Base):
    """Snapshot batch metadata for PRODUCT campaign metric exports (manual cache)."""

    __tablename__ = "gmvmax_product_campaign_snapshot_batches"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "start_date",
            "end_date",
            "snapshot_type",
            name="uk_prod_snapshot_batch",
        ),
        Index("idx_prod_snapshot_batch_time", "snapshot_at"),
        Index("idx_prod_snapshot_batch_time_id", "snapshot_at", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date] = mapped_column(nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default="CURRENT_TIMESTAMP(6)"
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default="CURRENT_TIMESTAMP(6)",
        server_onupdate="CURRENT_TIMESTAMP(6)",
    )


class GmvmaxProductCampaignSnapshotRow(_SnapshotRowMixin, Base):
    """Snapshot row for PRODUCT campaign metric caches."""

    __tablename__ = "gmvmax_product_campaign_snapshot_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "campaign_id", name="uk_prod_snapshot_row"),
        Index("idx_prod_snapshot_row_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "gmvmax_product_campaign_snapshot_batches.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)


class GmvmaxLiveCampaignSnapshotBatch(Base):
    """Snapshot batch metadata for LIVE campaign metric exports (manual cache)."""

    __tablename__ = "gmvmax_live_campaign_snapshot_batches"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "start_date",
            "end_date",
            "snapshot_type",
            name="uk_live_snapshot_batch",
        ),
        Index("idx_live_snapshot_batch_time", "snapshot_at"),
        Index("idx_live_snapshot_batch_time_id", "snapshot_at", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date] = mapped_column(nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default="CURRENT_TIMESTAMP(6)"
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default="CURRENT_TIMESTAMP(6)",
        server_onupdate="CURRENT_TIMESTAMP(6)",
    )


class GmvmaxLiveCampaignSnapshotRow(_SnapshotRowMixin, Base):
    """Snapshot row for LIVE campaign metric caches."""

    __tablename__ = "gmvmax_live_campaign_snapshot_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "campaign_id", name="uk_live_snapshot_row"),
        Index("idx_live_snapshot_row_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "gmvmax_live_campaign_snapshot_batches.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

    all_shops_orders: Mapped[int | None] = mapped_column(BigInteger)
    all_shops_gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger)
    all_shops_roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    all_shops_cost_per_order: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    live_views: Mapped[int | None] = mapped_column(BigInteger)
    cost_per_live_view: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger)
    cost_per_10s_live_view: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    live_follows: Mapped[int | None] = mapped_column(BigInteger)


__all__ = [
    "GmvmaxProductCampaignSnapshotBatch",
    "GmvmaxProductCampaignSnapshotRow",
    "GmvmaxLiveCampaignSnapshotBatch",
    "GmvmaxLiveCampaignSnapshotRow",
]
