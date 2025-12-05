from __future__ import annotations

"""Data models for the restructured GMV Max schema.

The tables split static entity metadata from time-series metrics and introduce
explicit promotion types so both Product GMV Max and LIVE GMV Max can coexist.
"""

from datetime import datetime, date
from enum import Enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Enum as SqlEnum,
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
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


UBigInt = Integer().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class PromotionTypeEnum(str, Enum):
    """Supported GMV Max promotion types."""

    PRODUCT = "PRODUCT"
    LIVE = "LIVE"


class CreativeDeliveryStatusEnum(str, Enum):
    """Delivery status for creatives as reported by TikTok."""

    IN_QUEUE = "IN_QUEUE"
    LEARNING = "LEARNING"
    DELIVERING = "DELIVERING"
    NOT_DELIVERING = "NOT_DELIVERING"
    AUTHORIZATION_NEEDED = "AUTHORIZATION_NEEDED"
    EXCLUDED = "EXCLUDED"
    UNAVAILABLE = "UNAVAILABLE"
    REJECTED = "REJECTED"
    NOT_ACTIVE = "NOT_ACTIVE"


class GmvCampaign(Base):
    """Static metadata for a GMV Max campaign (product or LIVE)."""

    __tablename__ = "gmv_campaigns"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            name="uk_gmv_campaign_scope",
        ),
        Index("idx_gmv_campaign_advertiser", "advertiser_id"),
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
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    operation_status: Mapped[str | None] = mapped_column(String(128), default=None)
    secondary_status: Mapped[str | None] = mapped_column(String(128), default=None)
    tt_account_name: Mapped[str | None] = mapped_column(String(255), default=None)
    tt_account_profile_image_url: Mapped[str | None] = mapped_column(Text, default=None)
    identity_id: Mapped[str | None] = mapped_column(String(64), default=None)
    schedule_type: Mapped[str | None] = mapped_column(String(64), default=None)
    schedule_start_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    schedule_end_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    shopping_ads_type: Mapped[str | None] = mapped_column(String(64), default=None)
    optimization_goal: Mapped[str | None] = mapped_column(String(64), default=None)
    bid_type: Mapped[str | None] = mapped_column(String(64), default=None)
    roas_bid: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    target_roi_budget: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    max_delivery_budget: Mapped[int | None] = mapped_column(BigInteger, default=None)
    daily_budget_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))

    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvProduct(Base):
    """Static product metadata for GMV Max."""

    __tablename__ = "gmv_products"
    __table_args__ = (
        UniqueConstraint("item_group_id", name="uk_gmv_product_item_group"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(255), default=None)
    product_image_url: Mapped[str | None] = mapped_column(Text, default=None)
    product_status: Mapped[str | None] = mapped_column(String(64), default=None)
    is_running_custom_shop_ads: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCreative(Base):
    """Static creative metadata for GMV Max."""

    __tablename__ = "gmv_creatives"
    __table_args__ = (
        UniqueConstraint("creative_id", name="uk_gmv_creative_id"),
        Index("idx_gmv_creative_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    creative_name: Mapped[str | None] = mapped_column(String(255), default=None)
    tt_account_name: Mapped[str | None] = mapped_column(String(255), default=None)
    authorization_type: Mapped[str | None] = mapped_column(String(64), default=None)
    shop_content_type: Mapped[str | None] = mapped_column(String(64), default=None)
    creative_delivery_status: Mapped[CreativeDeliveryStatusEnum | None] = mapped_column(
        SqlEnum(CreativeDeliveryStatusEnum), default=None
    )
    video_duration_sec: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvLivestream(Base):
    """Static livestream metadata for LIVE GMV Max."""

    __tablename__ = "gmv_livestreams"
    __table_args__ = (UniqueConstraint("room_id", name="uk_gmv_livestream_room"),)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)
    live_name: Mapped[str | None] = mapped_column(String(255), default=None)
    live_status: Mapped[str | None] = mapped_column(String(64), default=None)
    live_launched_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    live_duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCampaignSyncSnapshot(Base):
    """Raw sync snapshots for GMV Max campaign pulls."""

    __tablename__ = "gmv_campaign_sync_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "snapshot_type",
            "synced_at",
            name="uk_gmv_campaign_sync_snapshot",
        ),
        Index(
            "idx_gmv_campaign_sync_snapshot_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
        ),
        Index("idx_gmv_campaign_sync_snapshot_time", "synced_at"),
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
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[PromotionTypeEnum | None] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=True
    )
    snapshot_type: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'CAMPAIGN'")
    )
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCampaignProduct(Base):
    """Bridge between campaigns and products with promotion type awareness."""

    __tablename__ = "gmv_campaign_products"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            "store_id",
            "item_group_id",
            name="uk_gmv_campaign_product",
        ),
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "store_id",
            "item_group_id",
            name="uk_gmv_store_product_unique",
        ),
        Index("idx_gmv_campaign_product_campaign", "campaign_id"),
        Index("idx_gmv_campaign_product_item", "item_group_id"),
        Index("idx_gmv_campaign_product_store", "store_id"),
        Index("idx_gmv_campaign_product_workspace", "workspace_id", "auth_id"),
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
    campaign_pk: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("gmv_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    operation_status: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCampaignCreative(Base):
    """Bridge between campaigns and creatives."""

    __tablename__ = "gmv_campaign_creatives"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creative_id", "promotion_type", name="uk_gmv_campaign_creative"
        ),
        Index("idx_gmv_campaign_creative_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvCampaignLivestream(Base):
    """Bridge between campaigns and livestream rooms."""

    __tablename__ = "gmv_campaign_livestreams"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "room_id", "promotion_type", name="uk_gmv_campaign_room"
        ),
        Index("idx_gmv_campaign_room_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
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


class GmvCreativeHeating(Base):
    """State for creative heating cycles managed inside GMV Max."""

    __tablename__ = "gmv_creative_heating"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            "creative_id",
            "promotion_type",
            name="uk_gmv_creative_heating_scope",
        ),
        Index(
            "idx_gmv_creative_heating_campaign",
            "workspace_id",
            "auth_id",
            "campaign_id",
        ),
        Index(
            "idx_gmv_creative_heating_creative",
            "workspace_id",
            "auth_id",
            "creative_id",
        ),
        Index(
            "idx_gmv_creative_heating_status",
            "workspace_id",
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
    auth_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
    )

    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )

    creative_name: Mapped[str | None] = mapped_column(String(255), default=None)
    product_id: Mapped[str | None] = mapped_column(String(64), default=None)
    item_id: Mapped[str | None] = mapped_column(String(64), default=None)

    mode: Mapped[str | None] = mapped_column(String(32), default=None)
    target_daily_budget: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    budget_delta: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    max_duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    evaluation_window_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    min_clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    min_ctr: Mapped[float | None] = mapped_column(Numeric(10, 4), default=None)
    min_gross_revenue: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    auto_stop_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    is_heating_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PENDING'"))
    last_action_type: Mapped[str | None] = mapped_column(String(64), default=None)
    last_action_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_status: Mapped[str | None] = mapped_column(String(64), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_action_request: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_action_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_evaluation_result: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvActionLog(Base):
    """Record of campaign actions executed within GMV Max."""

    __tablename__ = "gmv_action_logs"
    __table_args__ = (
        Index("idx_gmv_action_workspace", "workspace_id"),
        Index("idx_gmv_action_auth", "auth_id"),
        Index("idx_gmv_action_campaign", "campaign_id"),
        Index("idx_gmv_action_created", "created_at"),
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
        ForeignKey("gmv_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
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
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class GmvStrategyConfig(Base):
    """Per-campaign strategy tuning configuration for GMV Max."""

    __tablename__ = "gmv_strategy_configs"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            name="uq_gmv_strategy_workspace_auth_campaign",
        ),
        Index("idx_gmv_strategy_workspace", "workspace_id"),
        Index("idx_gmv_strategy_auth", "auth_id"),
        Index("idx_gmv_strategy_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_id: Mapped[int] = mapped_column(Integer, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))

    target_roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    min_roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    max_roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    min_impressions: Mapped[int | None] = mapped_column(Integer, default=None)
    min_clicks: Mapped[int | None] = mapped_column(Integer, default=None)

    max_budget_raise_pct_per_day: Mapped[float | None] = mapped_column(
        Numeric(5, 2), default=None
    )
    max_budget_cut_pct_per_day: Mapped[float | None] = mapped_column(
        Numeric(5, 2), default=None
    )
    max_roas_step_per_adjust: Mapped[float | None] = mapped_column(Numeric(10, 4), default=None)

    cooldown_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    min_runtime_minutes_before_first_change: Mapped[int | None] = mapped_column(
        Integer, default=None
    )

    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvMonitoringStrategy(Base):
    """Platform-level monitoring strategy controlling sync cadence."""

    __tablename__ = "gmvmax_monitoring_strategies"
    __table_args__ = (
        Index("idx_workspace_level", "workspace_id", "level", "enabled"),
        Index("idx_auth_store", "auth_id", "store_id"),
        Index("idx_enabled_interval", "enabled", "interval_minutes"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[PromotionTypeEnum | None] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=True
    )
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_campaigns_per_run: Mapped[int | None] = mapped_column(Integer, default=None)

    last_run_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    last_success_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    last_error_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class BaseMetricMixin:
    """Common metric columns reused across tables."""

    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_clicks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    product_click_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    net_cost_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    orders: Mapped[int | None] = mapped_column(BigInteger, default=None)
    gross_revenue_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cost_per_order: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    ad_click_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    ad_conversion_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    conversion_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_2s: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_6s: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_25: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_50: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_75: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    video_view_rate_100: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)


class AllShopsMetricMixin:
    """Omni-shop financial rollups for LIVE reporting."""

    all_shops_orders: Mapped[int | None] = mapped_column(BigInteger, default=None)
    all_shops_gross_revenue_cents: Mapped[int | None] = mapped_column(
        BigInteger, default=None
    )
    all_shops_roi: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    all_shops_cost_per_order: Mapped[float | None] = mapped_column(
        Numeric(18, 4), default=None
    )


class LiveMetricExtras(AllShopsMetricMixin):
    """LIVE-only engagement metrics and derived unit costs."""

    cost_per_live_view: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    cost_per_10_second_live_view: Mapped[float | None] = mapped_column(
        Numeric(18, 4), default=None
    )


class GmvOverviewMetricsDaily(Base, BaseMetricMixin):
    """Advertiser-level daily metrics."""

    __tablename__ = "gmv_overview_metrics_daily"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "stat_time_day", name="uk_overview_daily"),
        Index("idx_overview_daily_advertiser", "advertiser_id", "stat_time_day"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)


class GmvOverviewMetricsHourly(Base, BaseMetricMixin):
    """Advertiser-level hourly metrics."""

    __tablename__ = "gmv_overview_metrics_hourly"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "stat_time_hour", name="uk_overview_hourly"),
        Index("idx_overview_hourly_advertiser", "advertiser_id", "stat_time_hour"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)


class GmvCampaignMetricsDaily(Base, BaseMetricMixin, LiveMetricExtras):
    """Campaign-level daily metrics supporting product and LIVE."""

    __tablename__ = "gmv_campaign_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "stat_time_day",
            "promotion_type",
            name="uk_campaign_daily",
        ),
        Index("idx_campaign_daily_campaign", "campaign_id", "stat_time_day"),
        Index("idx_campaign_daily_store", "store_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)


class GmvCampaignMetricsHourly(Base, BaseMetricMixin, LiveMetricExtras):
    """Campaign-level hourly metrics supporting product and LIVE."""

    __tablename__ = "gmv_campaign_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "stat_time_hour",
            "promotion_type",
            name="uk_campaign_hourly",
        ),
        Index("idx_campaign_hourly_campaign", "campaign_id", "stat_time_hour"),
        Index("idx_campaign_hourly_store", "store_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    promotion_type: Mapped[PromotionTypeEnum] = mapped_column(
        SqlEnum(PromotionTypeEnum), nullable=False
    )
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)


class GmvProductMetricsDaily(Base, BaseMetricMixin):
    """Product-level daily metrics."""

    __tablename__ = "gmv_product_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "item_group_id", "stat_time_day", name="uk_product_daily"
        ),
        Index(
            "idx_product_daily_campaign_item",
            "campaign_id",
            "item_group_id",
            "stat_time_day",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    bid_type: Mapped[str | None] = mapped_column(String(64), default=None)


class GmvProductMetricsHourly(Base, BaseMetricMixin):
    """Product-level hourly metrics."""

    __tablename__ = "gmv_product_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "item_group_id", "stat_time_hour", name="uk_product_hourly"
        ),
        Index(
            "idx_product_hourly_campaign_item",
            "campaign_id",
            "item_group_id",
            "stat_time_hour",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    bid_type: Mapped[str | None] = mapped_column(String(64), default=None)


class GmvCreativeMetricsDaily(Base, BaseMetricMixin):
    """Creative-level daily metrics."""

    __tablename__ = "gmv_creative_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creative_id", "stat_time_day", name="uk_creative_daily"
        ),
        Index(
            "idx_creative_daily_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_day",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)


class GmvCreativeMetricsHourly(Base, BaseMetricMixin):
    """Creative-level hourly metrics."""

    __tablename__ = "gmv_creative_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creative_id", "stat_time_hour", name="uk_creative_hourly"
        ),
        Index(
            "idx_creative_hourly_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_hour",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)


class GmvCreativeMetrics10Min(Base, BaseMetricMixin):
    """Creative-level 10 minute snapshots."""

    __tablename__ = "gmv_creative_metrics_10min"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
            name="uk_creative_10min",
        ),
        Index(
            "idx_creative_10min_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)


class GmvDurationMetricsDaily(Base, BaseMetricMixin, AllShopsMetricMixin):
    """Duration-level daily metrics."""

    __tablename__ = "gmv_duration_metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_day",
            name="uk_duration_daily",
        ),
        Index(
            "idx_duration_daily_campaign",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_day",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    duration: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    bid_type: Mapped[str | None] = mapped_column(String(64), default=None)


class GmvDurationMetricsHourly(Base, BaseMetricMixin, AllShopsMetricMixin):
    """Duration-level hourly metrics."""

    __tablename__ = "gmv_duration_metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_hour",
            name="uk_duration_hourly",
        ),
        Index(
            "idx_duration_hourly_campaign",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_hour",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str | None] = mapped_column(String(64), default=None)
    duration: Mapped[str] = mapped_column(String(64), nullable=False)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    bid_type: Mapped[str | None] = mapped_column(String(64), default=None)


class GmvLivestreamMetricsDaily(Base, BaseMetricMixin, LiveMetricExtras):
    """Livestream-level daily metrics."""

    __tablename__ = "gmv_livestream_metrics_daily"
    __table_args__ = (
        UniqueConstraint("room_id", "stat_time_day", name="uk_livestream_daily"),
        Index("idx_livestream_daily_room", "room_id", "stat_time_day"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stat_time_day: Mapped[date] = mapped_column(Date, nullable=False)
    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)


class GmvLivestreamMetricsHourly(Base, BaseMetricMixin, LiveMetricExtras):
    """Livestream-level hourly metrics."""

    __tablename__ = "gmv_livestream_metrics_hourly"
    __table_args__ = (
        UniqueConstraint("room_id", "stat_time_hour", name="uk_livestream_hourly"),
        Index("idx_livestream_hourly_room", "room_id", "stat_time_hour"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stat_time_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    live_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_10s_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    live_follows: Mapped[int | None] = mapped_column(BigInteger, default=None)

