"""introduce restructured GMV Max tables

Revision ID: 0042_gmv_restructure_schema
Revises: 0041_add_ttb_gmvmax_creative_metrics_10min
Create Date: 2024-06-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0042_gmv_restructure_schema"
down_revision = "0041_add_ttb_gmvmax_creative_metrics_10min"
branch_labels = None
depends_on = None


promotion_enum = sa.Enum("PRODUCT", "LIVE", name="promotiontypeenum")
creative_delivery_enum = sa.Enum(
    "IN_QUEUE",
    "LEARNING",
    "DELIVERING",
    "NOT_DELIVERING",
    "AUTHORIZATION_NEEDED",
    "EXCLUDED",
    "UNAVAILABLE",
    "REJECTED",
    "NOT_ACTIVE",
    name="creativedeliverystatusenum",
)


def _metric_columns():
    return [
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("conversion_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_2s", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_6s", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_25", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_50", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_75", sa.Numeric(10, 6), nullable=True),
        sa.Column("video_view_rate_100", sa.Numeric(10, 6), nullable=True),
    ]


def upgrade():
    op.create_table(
        "gmv_campaigns",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("schedule_type", sa.String(length=64), nullable=True),
        sa.Column("schedule_start_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("schedule_end_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("shopping_ads_type", sa.String(length=64), nullable=True),
        sa.Column("optimization_goal", sa.String(length=64), nullable=True),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        sa.Column("roas_bid", sa.Numeric(18, 4), nullable=True),
        sa.Column("target_roi_budget", sa.Numeric(18, 4), nullable=True),
        sa.Column("max_delivery_budget", sa.BigInteger(), nullable=True),
        sa.Column("daily_budget_cents", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("ext_created_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("ext_updated_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("workspace_id", "auth_id", "campaign_id", name="uk_gmv_campaign_scope"),
        sa.Index("idx_gmv_campaign_advertiser", "advertiser_id"),
    )

    op.create_table(
        "gmv_products",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("product_name", sa.String(length=255), nullable=True),
        sa.Column("product_image_url", sa.Text(), nullable=True),
        sa.Column("product_status", sa.String(length=64), nullable=True),
        sa.Column("is_running_custom_shop_ads", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("item_group_id", name="uk_gmv_product_item_group"),
    )

    op.create_table(
        "gmv_creatives",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("creative_name", sa.String(length=255), nullable=True),
        sa.Column("tt_account_name", sa.String(length=255), nullable=True),
        sa.Column("authorization_type", sa.String(length=64), nullable=True),
        sa.Column("shop_content_type", sa.String(length=64), nullable=True),
        sa.Column("creative_delivery_status", creative_delivery_enum, nullable=True),
        sa.Column("video_duration_sec", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("creative_id", name="uk_gmv_creative_id"),
        sa.Index("idx_gmv_creative_campaign", "campaign_id"),
    )

    op.create_table(
        "gmv_livestreams",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("live_name", sa.String(length=255), nullable=True),
        sa.Column("live_status", sa.String(length=64), nullable=True),
        sa.Column("live_launched_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("live_duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("room_id", name="uk_gmv_livestream_room"),
    )

    op.create_table(
        "gmv_campaign_products",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("operation_status", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "campaign_id", "item_group_id", "promotion_type", name="uk_gmv_campaign_product"
        ),
        sa.Index("idx_gmv_campaign_product_campaign", "campaign_id"),
        sa.Index("idx_gmv_campaign_product_item", "item_group_id"),
    )

    op.create_table(
        "gmv_campaign_creatives",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "campaign_id", "creative_id", "promotion_type", name="uk_gmv_campaign_creative"
        ),
        sa.Index("idx_gmv_campaign_creative_campaign", "campaign_id"),
    )

    op.create_table(
        "gmv_campaign_livestreams",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "campaign_id", "room_id", "promotion_type", name="uk_gmv_campaign_room"
        ),
        sa.Index("idx_gmv_campaign_room_campaign", "campaign_id"),
    )

    op.create_table(
        "gmv_overview_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("advertiser_id", "stat_time_day", name="uk_overview_daily"),
        sa.Index("idx_overview_daily_advertiser", "advertiser_id", "stat_time_day"),
    )

    op.create_table(
        "gmv_overview_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("advertiser_id", "stat_time_hour", name="uk_overview_hourly"),
        sa.Index("idx_overview_hourly_advertiser", "advertiser_id", "stat_time_hour"),
    )

    op.create_table(
        "gmv_campaign_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "stat_time_day", "promotion_type", name="uk_campaign_daily"),
        sa.Index("idx_campaign_daily_campaign", "campaign_id", "stat_time_day"),
    )

    op.create_table(
        "gmv_campaign_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "stat_time_hour", "promotion_type", name="uk_campaign_hourly"),
        sa.Index("idx_campaign_hourly_campaign", "campaign_id", "stat_time_hour"),
    )

    op.create_table(
        "gmv_product_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "item_group_id", "stat_time_day", name="uk_product_daily"),
        sa.Index(
            "idx_product_daily_campaign_item",
            "campaign_id",
            "item_group_id",
            "stat_time_day",
        ),
    )

    op.create_table(
        "gmv_product_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "item_group_id", "stat_time_hour", name="uk_product_hourly"),
        sa.Index(
            "idx_product_hourly_campaign_item",
            "campaign_id",
            "item_group_id",
            "stat_time_hour",
        ),
    )

    op.create_table(
        "gmv_creative_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "creative_id", "stat_time_day", name="uk_creative_daily"),
        sa.Index(
            "idx_creative_daily_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_day",
        ),
    )

    op.create_table(
        "gmv_creative_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("campaign_id", "creative_id", "stat_time_hour", name="uk_creative_hourly"),
        sa.Index(
            "idx_creative_hourly_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_hour",
        ),
    )

    op.create_table(
        "gmv_creative_metrics_10min",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("snapshot_at", mysql.DATETIME(fsp=6), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint(
            "campaign_id", "creative_id", "stat_time_day", "snapshot_at", name="uk_creative_10min"
        ),
        sa.Index(
            "idx_creative_10min_campaign",
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
        ),
    )

    op.create_table(
        "gmv_duration_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("duration", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint(
            "campaign_id", "item_group_id", "duration", "stat_time_day", name="uk_duration_daily"
        ),
        sa.Index(
            "idx_duration_daily_campaign",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_day",
        ),
    )

    op.create_table(
        "gmv_duration_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("duration", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint(
            "campaign_id", "item_group_id", "duration", "stat_time_hour", name="uk_duration_hourly"
        ),
        sa.Index(
            "idx_duration_hourly_campaign",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_hour",
        ),
    )

    op.create_table(
        "gmv_livestream_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("room_id", "stat_time_day", name="uk_livestream_daily"),
        sa.Index("idx_livestream_daily_room", "room_id", "stat_time_day"),
    )

    op.create_table(
        "gmv_livestream_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), primary_key=True),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("room_id", "stat_time_hour", name="uk_livestream_hourly"),
        sa.Index("idx_livestream_hourly_room", "room_id", "stat_time_hour"),
    )


def downgrade():
    op.drop_table("gmv_livestream_metrics_hourly")
    op.drop_table("gmv_livestream_metrics_daily")
    op.drop_table("gmv_duration_metrics_hourly")
    op.drop_table("gmv_duration_metrics_daily")
    op.drop_table("gmv_creative_metrics_10min")
    op.drop_table("gmv_creative_metrics_hourly")
    op.drop_table("gmv_creative_metrics_daily")
    op.drop_table("gmv_product_metrics_hourly")
    op.drop_table("gmv_product_metrics_daily")
    op.drop_table("gmv_campaign_metrics_hourly")
    op.drop_table("gmv_campaign_metrics_daily")
    op.drop_table("gmv_overview_metrics_hourly")
    op.drop_table("gmv_overview_metrics_daily")
    op.drop_table("gmv_campaign_livestreams")
    op.drop_table("gmv_campaign_creatives")
    op.drop_table("gmv_campaign_products")
    op.drop_table("gmv_livestreams")
    op.drop_table("gmv_creatives")
    op.drop_table("gmv_products")
    op.drop_table("gmv_campaigns")
    creative_delivery_enum.drop(op.get_bind(), checkfirst=True)
    promotion_enum.drop(op.get_bind(), checkfirst=True)

