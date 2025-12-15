"""GMV Max campaign metrics fact tables (report/get)."""

from __future__ import annotations

from datetime import datetime, date

from decimal import Decimal

from sqlalchemy import BigInteger, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class _CommonMetricMixin:
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

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


class GmvmaxProductCampaignMetricsDaily(_CommonMetricMixin, Base):
    """Daily aggregated metrics for PRODUCT campaigns (report/get)."""

    __tablename__ = "gmvmax_product_campaign_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            name="uk_prod_campaign_day",
        ),
        Index("idx_prod_campaign_day_store_date", "workspace_id", "store_id", "stat_time_day"),
        Index("idx_prod_campaign_day_campaign_date", "campaign_id", "stat_time_day"),
        Index(
            "idx_prod_campaign_day_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_day",
        ),
        Index("idx_prod_campaign_day_cutoff", "stat_time_day", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    stat_time_day: Mapped[date] = mapped_column(nullable=False)


class GmvmaxProductCampaignMetricsHourly(_CommonMetricMixin, Base):
    """Hourly metrics for PRODUCT campaigns (report/get)."""

    __tablename__ = "gmvmax_product_campaign_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_hour",
            name="uk_prod_campaign_hour",
        ),
        Index(
            "idx_prod_campaign_hour_store_time",
            "workspace_id",
            "store_id",
            "stat_time_hour",
        ),
        Index("idx_prod_campaign_hour_campaign_time", "campaign_id", "stat_time_hour"),
        Index(
            "idx_prod_campaign_hour_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_hour",
        ),
        Index("idx_prod_campaign_hour_cutoff", "stat_time_hour", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)


class GmvmaxLiveCampaignMetricsDaily(_CommonMetricMixin, Base):
    """Daily aggregated metrics for LIVE campaigns (report/get)."""

    __tablename__ = "gmvmax_live_campaign_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            name="uk_live_campaign_day",
        ),
        Index("idx_live_campaign_day_store_date", "workspace_id", "store_id", "stat_time_day"),
        Index("idx_live_campaign_day_campaign_date", "campaign_id", "stat_time_day"),
        Index(
            "idx_live_campaign_day_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_day",
        ),
        Index("idx_live_campaign_day_cutoff", "stat_time_day", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    stat_time_day: Mapped[date] = mapped_column(nullable=False)

    all_shops_orders: Mapped[int | None] = mapped_column(BigInteger)
    all_shops_gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger)
    all_shops_roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    all_shops_cost_per_order: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    live_views: Mapped[int | None] = mapped_column(BigInteger)
    cost_per_live_view: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger)
    cost_per_10s_live_view: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    live_follows: Mapped[int | None] = mapped_column(BigInteger)


class GmvmaxLiveCampaignMetricsHourly(_CommonMetricMixin, Base):
    """Hourly metrics for LIVE campaigns (report/get)."""

    __tablename__ = "gmvmax_live_campaign_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_hour",
            name="uk_live_campaign_hour",
        ),
        Index(
            "idx_live_campaign_hour_store_time",
            "workspace_id",
            "store_id",
            "stat_time_hour",
        ),
        Index("idx_live_campaign_hour_campaign_time", "campaign_id", "stat_time_hour"),
        Index(
            "idx_live_campaign_hour_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_hour",
        ),
        Index("idx_live_campaign_hour_cutoff", "stat_time_hour", "id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)

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
    "GmvmaxProductCampaignMetricsDaily",
    "GmvmaxProductCampaignMetricsHourly",
    "GmvmaxLiveCampaignMetricsDaily",
    "GmvmaxLiveCampaignMetricsHourly",
]
