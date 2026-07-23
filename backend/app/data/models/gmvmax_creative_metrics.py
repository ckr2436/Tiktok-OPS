"""GMV Max creative metrics fact tables (report/get)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Index,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class GmvmaxProductCreativeMetricsDaily(Base):
    """Daily PRODUCT creative metrics from TikTok GMV Max report/get."""

    __tablename__ = "gmvmax_product_creative_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "creative_id",
            "stat_time_day",
            name="uk_prod_creative_day",
        ),
        Index(
            "idx_prod_creative_day_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_day",
        ),
        Index(
            "idx_prod_creative_day_campaign_item",
            "campaign_id",
            "item_group_id",
            "stat_time_day",
        ),
        Index("idx_prod_creative_day_creative", "creative_id", "stat_time_day"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)

    creative_delivery_status: Mapped[str | None] = mapped_column(String(64))
    cost_cents: Mapped[int | None] = mapped_column(BigInteger)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger)
    orders: Mapped[int | None] = mapped_column(BigInteger)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger)
    roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    cost_per_order: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    impressions: Mapped[int | None] = mapped_column(BigInteger)
    clicks: Mapped[int | None] = mapped_column(BigInteger)
    product_impressions: Mapped[int | None] = mapped_column(BigInteger)
    product_clicks: Mapped[int | None] = mapped_column(BigInteger)
    product_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_2s: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_6s: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_p25: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_p50: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_p75: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ad_video_view_rate_p100: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    raw_metrics_json: Mapped[dict | None] = mapped_column(JSON)

    source_observed_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=True
    )
    ingested_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=True
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=True
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


__all__ = ["GmvmaxProductCreativeMetricsDaily"]
