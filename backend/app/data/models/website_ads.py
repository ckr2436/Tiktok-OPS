from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base


UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql").with_variant(Integer, "sqlite")


class WebsiteAdsMagentoConnection(Base):
    __tablename__ = "website_ads_magento_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "base_url", name="uk_web_ads_magento_workspace_url"),
        Index("idx_web_ads_magento_workspace", "workspace_id", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    access_token_cipher: Mapped[bytes] = mapped_column(LargeBinary(4096), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    last_sync_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsLandingPage(Base):
    __tablename__ = "website_ads_landing_pages"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uk_web_ads_landing_external"),
        Index("idx_web_ads_landing_workspace", "workspace_id", "is_active"),
        Index("idx_web_ads_landing_product", "workspace_id", "product_id"),
        Index("idx_web_ads_landing_content_product", "workspace_id", "content_product_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    connection_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("website_ads_magento_connections.id", ondelete="CASCADE"), nullable=True)
    content_product_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("hermes_content_products.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    website_id: Mapped[int | None] = mapped_column(Integer, default=None)
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    landing_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(128), default=None)
    content_name: Mapped[str | None] = mapped_column(String(512), default=None)
    content_category: Mapped[str | None] = mapped_column(String(255), default=None)
    brand: Mapped[str | None] = mapped_column(String(128), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'USD'"))
    image_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    seller_profile: Mapped[str | None] = mapped_column(Text, default=None)
    promotion_text: Mapped[str | None] = mapped_column(Text, default=None)
    product_details: Mapped[str | None] = mapped_column(Text, default=None)
    hermes_analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'NOT_ANALYZED'"))
    analysis_error: Mapped[str | None] = mapped_column(Text, default=None)
    analyzed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    external_updated_at: Mapped[str | None] = mapped_column(String(64), default=None)
    last_synced_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsCreativeAsset(Base):
    __tablename__ = "website_ads_creative_assets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "advertiser_id", "video_id", name="uk_web_ads_asset_video"),
        Index("idx_web_ads_asset_scope", "workspace_id", "auth_id", "advertiser_id"),
        Index("idx_web_ads_asset_product", "landing_page_id", "is_active"),
        Index("idx_web_ads_asset_auto_launch", "auto_launch_status", "auto_launch_next_retry_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    landing_page_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("website_ads_landing_pages.id", ondelete="SET NULL"), default=None
    )
    video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(512), default=None)
    preview_url: Mapped[str | None] = mapped_column(String(4096), default=None)
    cover_url: Mapped[str | None] = mapped_column(String(4096), default=None)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), default=None)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'TIKTOK_LIBRARY'"))
    user_notes: Mapped[str | None] = mapped_column(Text, default=None)
    tags_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    hermes_analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    analysis_inputs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    analysis_version: Mapped[str | None] = mapped_column(String(64), default=None)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'NOT_ANALYZED'"))
    analysis_error: Mapped[str | None] = mapped_column(Text, default=None)
    analysis_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    analysis_next_retry_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    transcript_text: Mapped[str | None] = mapped_column(Text, default=None)
    transcript_language: Mapped[str | None] = mapped_column(String(32), default=None)
    contact_sheet_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    analyzed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    auto_launch_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    auto_launch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    auto_launch_next_retry_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    auto_launch_decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    auto_launch_error: Mapped[str | None] = mapped_column(Text, default=None)
    auto_launched_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_synced_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsUploadFingerprint(Base):
    __tablename__ = "website_ads_upload_fingerprints"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "content_sha256",
            name="uk_web_ads_upload_fingerprint",
        ),
        Index("idx_web_ads_upload_scope", "workspace_id", "auth_id", "advertiser_id"),
        Index("idx_web_ads_upload_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'FILE_SHA256'"))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    file_name: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'UPLOADING'"))
    video_id: Mapped[str | None] = mapped_column(String(128), default=None)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsMediaPlan(Base):
    __tablename__ = "website_ads_media_plans"
    __table_args__ = (
        Index("idx_web_ads_plan_scope", "workspace_id", "auth_id", "advertiser_id"),
        Index("idx_web_ads_plan_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    landing_page_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_landing_pages.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_local_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("website_ads_campaigns.id", ondelete="SET NULL"), default=None
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'DRAFT'"))
    daily_budget: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    activate_after_create: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    strategy_source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'HERMES'"))
    confidence: Mapped[str | None] = mapped_column(String(16), default=None)
    strategy_summary: Mapped[str | None] = mapped_column(Text, default=None)
    product_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    selected_asset_ids_json: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    execution_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    hermes_response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    generated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    executed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsMediaPlanGroup(Base):
    __tablename__ = "website_ads_media_plan_groups"
    __table_args__ = (
        UniqueConstraint("media_plan_id", "sort_order", name="uk_web_ads_plan_group_order"),
        Index("idx_web_ads_plan_group_plan", "media_plan_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    media_plan_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_media_plans.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    hypothesis: Mapped[str | None] = mapped_column(Text, default=None)
    targeting_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    daily_budget: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    bid_strategy: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'LOWEST_COST'"))
    conversion_bid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))


class WebsiteAdsMediaPlanCreative(Base):
    __tablename__ = "website_ads_media_plan_creatives"
    __table_args__ = (
        UniqueConstraint("media_plan_group_id", "creative_asset_id", name="uk_web_ads_plan_group_asset"),
        Index("idx_web_ads_plan_creative_group", "media_plan_group_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    media_plan_group_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_media_plan_groups.id", ondelete="CASCADE"), nullable=False
    )
    creative_asset_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_creative_assets.id", ondelete="RESTRICT"), nullable=False
    )
    ad_name: Mapped[str] = mapped_column(String(512), nullable=False)
    ad_text: Mapped[str] = mapped_column(String(100), nullable=False)
    call_to_action: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'SHOP_NOW'"))
    rationale: Mapped[str | None] = mapped_column(Text, default=None)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))


class WebsiteAdsCampaign(Base):
    __tablename__ = "website_ads_campaigns"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "campaign_id", name="uk_web_ads_campaign_remote"),
        UniqueConstraint("workspace_id", "auth_id", "request_key", name="uk_web_ads_campaign_request"),
        Index("idx_web_ads_campaign_scope", "workspace_id", "auth_id", "advertiser_id"),
        Index("idx_web_ads_campaign_status", "local_status", "operation_status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    landing_page_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("website_ads_landing_pages.id", ondelete="RESTRICT"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    objective_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'WEB_CONVERSIONS'"))
    local_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'CREATING'"))
    operation_status: Mapped[str | None] = mapped_column(String(32), default=None)
    secondary_status: Mapped[str | None] = mapped_column(String(128), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsAdGroup(Base):
    __tablename__ = "website_ads_adgroups"
    __table_args__ = (
        UniqueConstraint("campaign_local_id", "adgroup_id", name="uk_web_ads_adgroup_remote"),
        Index("idx_web_ads_adgroup_campaign", "campaign_local_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_local_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("website_ads_campaigns.id", ondelete="CASCADE"), nullable=False)
    adgroup_id: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    pixel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    targeting_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    budget_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    budget: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    bid_type: Mapped[str] = mapped_column(String(64), nullable=False)
    conversion_bid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    schedule_start_time: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_status: Mapped[str | None] = mapped_column(String(32), default=None)
    secondary_status: Mapped[str | None] = mapped_column(String(128), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsAd(Base):
    __tablename__ = "website_ads_ads"
    __table_args__ = (
        UniqueConstraint("adgroup_local_id", "ad_id", name="uk_web_ads_ad_remote"),
        Index("idx_web_ads_ad_campaign", "campaign_local_id"),
        Index("idx_web_ads_ad_status", "guard_enabled", "operation_status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    campaign_local_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("website_ads_campaigns.id", ondelete="CASCADE"), nullable=False)
    adgroup_local_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("website_ads_adgroups.id", ondelete="CASCADE"), nullable=False)
    ad_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ad_id_v2: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    landing_page_url: Mapped[str] = mapped_column(String(4096), nullable=False)
    operation_status: Mapped[str | None] = mapped_column(String(32), default=None)
    secondary_status: Mapped[str | None] = mapped_column(String(128), default=None)
    guard_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    target_roas: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    max_unprofitable_spend: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    guard_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsMetricHourly(Base):
    __tablename__ = "website_ads_metrics_hourly"
    __table_args__ = (
        UniqueConstraint("ad_local_id", "stat_hour", name="uk_web_ads_metric_hour"),
        Index("idx_web_ads_metric_scope", "workspace_id", "advertiser_id", "stat_hour"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_local_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("website_ads_ads.id", ondelete="CASCADE"), nullable=False)
    stat_hour: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    spend: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default=text("0"))
    impressions: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_play_actions: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_watched_2s: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_watched_6s: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_views_p25: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_views_p50: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_views_p75: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    video_views_p100: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    average_video_play: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    conversions: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default=text("0"))
    conversion_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default=text("0"))
    cpc: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    cpm: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    cpa: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    roas: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))


class WebsiteAdsConversionGuardState(Base):
    """Campaign-level Website Ads control state backed by product GMV order pulses."""

    __tablename__ = "website_ads_conversion_guard_states"
    __table_args__ = (
        UniqueConstraint("campaign_local_id", name="uk_web_ads_conversion_guard_campaign"),
        Index("idx_web_ads_conversion_guard_due", "status", "resume_at"),
        Index("idx_web_ads_conversion_guard_product", "workspace_id", "auth_id", "advertiser_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_local_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    control_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'OBSERVING'"))
    observation_started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    source_window_start_hour: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    baseline_website_spend: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default=text("0"))
    baseline_website_clicks: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    baseline_order_count: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    last_observed_order_count: Mapped[int] = mapped_column(UBigInt, nullable=False, server_default=text("0"))
    last_order_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_order_detected_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    pause_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    paused_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    resume_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_source_hour: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    policy_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    state_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsDailyReport(Base):
    """Persisted advertiser-day learning report for one Website Ads campaign."""

    __tablename__ = "website_ads_daily_reports"
    __table_args__ = (
        UniqueConstraint("campaign_local_id", "report_date", name="uk_web_ads_daily_report_campaign_date"),
        Index("idx_web_ads_daily_report_scope", "workspace_id", "advertiser_id", "report_date"),
        Index("idx_web_ads_daily_report_product", "landing_page_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE"), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_local_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    landing_page_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("website_ads_landing_pages.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    advertiser_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'GENERATED'"))
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    audience_performance_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    gmv_signal_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    action_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    hermes_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    report_text: Mapped[str | None] = mapped_column(Text, default=None)
    source_freshness_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    generated_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"), server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class WebsiteAdsActionLog(Base):
    __tablename__ = "website_ads_action_logs"
    __table_args__ = (
        Index("idx_web_ads_action_ad", "ad_local_id", "created_at"),
        Index("idx_web_ads_action_scope", "workspace_id", "action", "created_at"),
        Index("idx_web_ads_action_auth_scope", "workspace_id", "auth_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    auth_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", ondelete="SET NULL"),
        default=None,
    )
    ad_local_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("website_ads_ads.id", ondelete="SET NULL"), default=None)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1024), default=None)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    request_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"))
