from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base

from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT

UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(BigInteger, "sqlite")
)


class TTBGmvMaxCreativeMetrics10Min(Base):
    __tablename__ = "ttb_gmvmax_creative_metrics_10min"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
            name="uk_ttb_gmvmax_creative_metrics_10min_scope",
        ),
        Index(
            "idx_ttb_gmvmax_creative_metrics_10min_campaign",
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "stat_time_day",
        ),
        Index(
            "idx_ttb_gmvmax_creative_metrics_10min_creative",
            "workspace_id",
            "provider",
            "auth_id",
            "creative_id",
            "stat_time_day",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'tiktok-business'"))
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), default=None)
    product_id: Mapped[str | None] = mapped_column(String(64), default=None)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)

    product_impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)

    ad_video_view_rate_p25: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p50: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p75: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p100: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_2s: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_6s: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)

    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    orders: Mapped[int | None] = mapped_column(BigInteger, default=None)

    cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cost_per_order_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)

    creative_delivery_status: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

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
