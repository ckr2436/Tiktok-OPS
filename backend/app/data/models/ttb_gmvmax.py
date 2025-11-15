from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(BigInteger, "sqlite")
)


class TTBGmvMaxCampaign(Base):
    __tablename__ = "ttb_gmvmax_campaigns"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            name="uk_ttb_gmvmax_campaign_scope",
        ),
        Index("idx_ttb_gmvmax_campaign_advertiser", "advertiser_id"),
        Index("idx_ttb_gmvmax_campaign_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )

    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    operation_status: Mapped[str | None] = mapped_column(String(32), default=None)
    shopping_ads_type: Mapped[str | None] = mapped_column(String(32), default=None)
    optimization_goal: Mapped[str | None] = mapped_column(String(64), default=None)

    roas_bid: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    daily_budget_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

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


class TTBGmvMaxMetricsHourly(Base):
    __tablename__ = "ttb_gmvmax_metrics_hourly"
    __table_args__ = (
        UniqueConstraint("campaign_id", "interval_start", name="uk_ttb_gmvmax_metrics_hourly"),
        Index("idx_ttb_gmvmax_metrics_hourly_campaign", "campaign_id"),
        Index("idx_ttb_gmvmax_metrics_hourly_interval", "interval_start"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    campaign_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("ttb_gmvmax_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )

    interval_start: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    interval_end: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    orders: Mapped[int | None] = mapped_column(Integer, default=None)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    product_impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_click_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    ad_click_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    ad_conversion_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)

    video_views_2s: Mapped[int | None] = mapped_column(BigInteger, default=None)
    video_views_6s: Mapped[int | None] = mapped_column(BigInteger, default=None)
    video_views_p25: Mapped[int | None] = mapped_column(BigInteger, default=None)
    video_views_p50: Mapped[int | None] = mapped_column(BigInteger, default=None)
    video_views_p75: Mapped[int | None] = mapped_column(BigInteger, default=None)
    video_views_p100: Mapped[int | None] = mapped_column(BigInteger, default=None)

    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )


class TTBGmvMaxMetricsDaily(Base):
    __tablename__ = "ttb_gmvmax_metrics_daily"
    __table_args__ = (
        UniqueConstraint("campaign_id", "date", name="uk_ttb_gmvmax_metrics_daily"),
        Index("idx_ttb_gmvmax_metrics_daily_date", "date"),
        Index("idx_ttb_gmvmax_metrics_daily_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    campaign_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("ttb_gmvmax_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)

    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    orders: Mapped[int | None] = mapped_column(Integer, default=None)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    product_impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_click_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    ad_click_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    ad_conversion_rate: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)

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


class TTBGmvMaxCreativeMetric(Base):
    __tablename__ = "ttb_gmvmax_creative_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "creative_id",
            "stat_time_day",
            name="uk_ttb_gmvmax_creative_metrics_scope",
        ),
        Index(
            "idx_ttb_gmvmax_creative_metrics_campaign",
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
        ),
        Index(
            "idx_ttb_gmvmax_creative_metrics_day",
            "workspace_id",
            "provider",
            "auth_id",
            "stat_time_day",
        ),
        Index(
            "idx_ttb_gmvmax_creative_metrics_creative",
            "workspace_id",
            "provider",
            "auth_id",
            "creative_id",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'tiktok-business'"),
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_name: Mapped[str | None] = mapped_column(String(255), default=None)
    adgroup_id: Mapped[str | None] = mapped_column(String(64), default=None)
    product_id: Mapped[str | None] = mapped_column(String(64), default=None)
    item_id: Mapped[str | None] = mapped_column(String(64), default=None)

    stat_time_day: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)

    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    net_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    orders: Mapped[int | None] = mapped_column(Integer, default=None)
    gross_revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_click_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_2s: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_6s: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p25: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p50: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p75: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ad_video_view_rate_p100: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)

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


class TTBGmvMaxCreativeHeating(Base):
    __tablename__ = "ttb_gmvmax_creative_heating"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "creative_id",
            name="uk_ttb_gmvmax_creative_heating_scope",
        ),
        Index(
            "idx_ttb_gmvmax_creative_heating_campaign",
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
        ),
        Index(
            "idx_ttb_gmvmax_creative_heating_creative",
            "workspace_id",
            "provider",
            "auth_id",
            "creative_id",
        ),
        Index(
            "idx_ttb_gmvmax_creative_heating_status",
            "workspace_id",
            "provider",
            "auth_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'tiktok-business'"),
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_name: Mapped[str | None] = mapped_column(String(255), default=None)
    product_id: Mapped[str | None] = mapped_column(String(64), default=None)
    item_id: Mapped[str | None] = mapped_column(String(64), default=None)

    mode: Mapped[str | None] = mapped_column(String(32), default=None)
    target_daily_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    budget_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    max_duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    evaluation_window_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    min_clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    min_ctr: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), default=None)
    min_gross_revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    auto_stop_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    is_heating_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PENDING'"))
    last_action_type: Mapped[str | None] = mapped_column(String(64), default=None)
    last_action_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_action_request: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_action_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_evaluation_result: Mapped[str | None] = mapped_column(String(64), default=None)

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


class TTBGmvMaxActionLog(Base):
    __tablename__ = "ttb_gmvmax_action_logs"
    __table_args__ = (
        Index("idx_ttb_gmvmax_action_workspace", "workspace_id"),
        Index("idx_ttb_gmvmax_action_auth", "auth_id"),
        Index("idx_ttb_gmvmax_action_campaign", "campaign_id"),
        Index("idx_ttb_gmvmax_action_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("ttb_gmvmax_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )

    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), default=None)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    performed_by: Mapped[str | None] = mapped_column(String(64), default=None)
    result: Mapped[str | None] = mapped_column(String(32), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )


class TTBGmvMaxStrategyConfig(Base):
    __tablename__ = "ttb_gmvmax_strategy_config"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            name="uq_gmvmax_strategy_workspace_auth_campaign",
        ),
        Index("idx_gmvmax_strategy_workspace", "workspace_id"),
        Index("idx_gmvmax_strategy_auth", "auth_id"),
        Index("idx_gmvmax_strategy_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_id: Mapped[int] = mapped_column(Integer, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))

    target_roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    min_roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    max_roi: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    min_impressions: Mapped[int | None] = mapped_column(Integer, default=None)
    min_clicks: Mapped[int | None] = mapped_column(Integer, default=None)

    max_budget_raise_pct_per_day: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=None)
    max_budget_cut_pct_per_day: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=None)
    max_roas_step_per_adjust: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), default=None)

    cooldown_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    min_runtime_minutes_before_first_change: Mapped[int | None] = mapped_column(Integer, default=None)

    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

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
