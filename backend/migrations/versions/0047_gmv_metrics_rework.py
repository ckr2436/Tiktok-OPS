"""Rebuild GMV Max metric fact tables with new schemas

Revision ID: 0047_gmv_metrics_rework
Revises: 0046_drop_legacy_ttb_gmvmax_tables
Create Date: 2025-03-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

revision = "0047_gmv_metrics_rework"
down_revision = "0046_drop_legacy_ttb_gmvmax_tables"
branch_labels = None
depends_on = None

ID_TYPE = sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql")


METRIC_TABLES = [
    "gmv_overview_metrics_daily",
    "gmv_overview_metrics_hourly",
    "gmv_campaign_metrics_daily",
    "gmv_campaign_metrics_hourly",
    "gmv_product_metrics_daily",
    "gmv_product_metrics_hourly",
    "gmv_creative_metrics_daily",
    "gmv_creative_metrics_hourly",
    "gmv_duration_metrics_daily",
    "gmv_duration_metrics_hourly",
    "gmv_livestream_metrics_daily",
    "gmv_livestream_metrics_hourly",
]


MYSQL_TABLE_KWARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
}


# Helpers

def drop_tables_if_exist(table_names: list[str]):
    conn = op.get_bind()
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    for table in table_names:
        if table in existing:
            op.drop_table(table)


def _core_scope_columns():
    return [
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
    ]


def _money_and_order_columns(include_net_cost: bool = False):
    columns = [
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
    ]
    if include_net_cost:
        columns.insert(1, sa.Column("net_cost_cents", sa.BigInteger(), nullable=True))
    return columns


def _live_shop_columns():
    return [
        sa.Column("all_shops_orders", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_cost_per_order", sa.Numeric(18, 4), nullable=True),
    ]


def _live_interaction_columns():
    return [
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_10s_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
    ]


def upgrade():
    drop_tables_if_exist(METRIC_TABLES)

    # Overview metrics
    op.create_table(
        "gmv_overview_metrics_daily",
        *_core_scope_columns(),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_money_and_order_columns(include_net_cost=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_day",
            name="uq_overview_day",
        ),
        sa.Index("idx_overview_day_advertiser", "advertiser_id", "stat_time_day"),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_overview_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_money_and_order_columns(include_net_cost=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "stat_time_hour",
            name="uq_overview_hour",
        ),
        sa.Index("idx_overview_hour_advertiser", "advertiser_id", "stat_time_hour"),
        **MYSQL_TABLE_KWARGS,
    )

    # Campaign metrics
    op.create_table(
        "gmv_campaign_metrics_daily",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", sa.String(length=16), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        *_money_and_order_columns(include_net_cost=True),
        *_live_shop_columns(),
        *_live_interaction_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "promotion_type",
            "stat_time_day",
            name="uq_campaign_day",
        ),
        sa.Index("idx_campaign_day", "campaign_id", "stat_time_day"),
        sa.Index("idx_campaign_type_day", "promotion_type", "campaign_id", "stat_time_day"),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_campaign_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", sa.String(length=16), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        *_money_and_order_columns(include_net_cost=True),
        *_live_shop_columns(),
        *_live_interaction_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "promotion_type",
            "stat_time_hour",
            name="uq_campaign_hour",
        ),
        sa.Index("idx_campaign_hour", "campaign_id", "stat_time_hour"),
        sa.Index("idx_campaign_type_hour", "promotion_type", "campaign_id", "stat_time_hour"),
        **MYSQL_TABLE_KWARGS,
    )

    # Product metrics
    op.create_table(
        "gmv_product_metrics_daily",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "stat_time_day",
            name="uq_product_day",
        ),
        sa.Index("idx_product_day", "campaign_id", "item_group_id", "stat_time_day"),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_product_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "stat_time_hour",
            name="uq_product_hour",
        ),
        sa.Index("idx_product_hour", "campaign_id", "item_group_id", "stat_time_hour"),
        **MYSQL_TABLE_KWARGS,
    )

    # Creative metrics
    op.create_table(
        "gmv_creative_metrics_daily",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("creative_delivery_status", sa.String(length=32), nullable=True),
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_click_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_2s", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_6s", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p25", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p50", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p75", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p100", sa.Numeric(10, 6), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "item_id",
            "stat_time_day",
            name="uq_creative_day",
        ),
        sa.Index("idx_creative_day", "campaign_id", "item_group_id", "item_id", "stat_time_day"),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_creative_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("creative_delivery_status", sa.String(length=32), nullable=True),
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_click_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_2s", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_6s", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p25", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p50", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p75", sa.Numeric(10, 6), nullable=True),
        sa.Column("ad_video_view_rate_p100", sa.Numeric(10, 6), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "item_id",
            "stat_time_hour",
            name="uq_creative_hour",
        ),
        sa.Index("idx_creative_hour", "campaign_id", "item_group_id", "item_id", "stat_time_hour"),
        **MYSQL_TABLE_KWARGS,
    )

    # Duration metrics
    op.create_table(
        "gmv_duration_metrics_daily",
        *_core_scope_columns(),
        sa.Column("promotion_type", sa.String(length=16), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("room_id", sa.String(length=64), nullable=True),
        sa.Column("duration", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        *_live_shop_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "promotion_type",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_day",
            name="uq_duration_product_day",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "promotion_type",
            "campaign_id",
            "room_id",
            "duration",
            "stat_time_day",
            name="uq_duration_live_day",
        ),
        sa.Index(
            "idx_duration_product_day",
            "promotion_type",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_day",
        ),
        sa.Index(
            "idx_duration_live_day",
            "promotion_type",
            "campaign_id",
            "room_id",
            "duration",
            "stat_time_day",
        ),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_duration_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("promotion_type", sa.String(length=16), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("room_id", sa.String(length=64), nullable=True),
        sa.Column("duration", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("bid_type", sa.String(length=64), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        *_live_shop_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "promotion_type",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_hour",
            name="uq_duration_product_hour",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "promotion_type",
            "campaign_id",
            "room_id",
            "duration",
            "stat_time_hour",
            name="uq_duration_live_hour",
        ),
        sa.Index(
            "idx_duration_product_hour",
            "promotion_type",
            "campaign_id",
            "item_group_id",
            "duration",
            "stat_time_hour",
        ),
        sa.Index(
            "idx_duration_live_hour",
            "promotion_type",
            "campaign_id",
            "room_id",
            "duration",
            "stat_time_hour",
        ),
        **MYSQL_TABLE_KWARGS,
    )

    # Livestream metrics
    op.create_table(
        "gmv_livestream_metrics_daily",
        *_core_scope_columns(),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("live_name", sa.String(length=255), nullable=True),
        sa.Column("live_status", sa.String(length=32), nullable=True),
        sa.Column("live_launched_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("live_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("live_duration_raw", sa.String(length=64), nullable=True),
        *_money_and_order_columns(include_net_cost=True),
        *_live_interaction_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "room_id",
            "stat_time_day",
            name="uq_livestream_day",
        ),
        sa.Index("idx_livestream_day", "room_id", "stat_time_day"),
        sa.Index("idx_livestream_campaign_day", "campaign_id", "stat_time_day"),
        **MYSQL_TABLE_KWARGS,
    )

    op.create_table(
        "gmv_livestream_metrics_hourly",
        *_core_scope_columns(),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("live_name", sa.String(length=255), nullable=True),
        sa.Column("live_status", sa.String(length=32), nullable=True),
        sa.Column("live_launched_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("live_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("live_duration_raw", sa.String(length=64), nullable=True),
        *_money_and_order_columns(include_net_cost=True),
        *_live_interaction_columns(),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "room_id",
            "stat_time_hour",
            name="uq_livestream_hour",
        ),
        sa.Index("idx_livestream_hour", "room_id", "stat_time_hour"),
        sa.Index("idx_livestream_campaign_hour", "campaign_id", "stat_time_hour"),
        **MYSQL_TABLE_KWARGS,
    )


def downgrade():
    drop_tables_if_exist(METRIC_TABLES)

    for table in METRIC_TABLES:
        op.create_table(
            table,
            sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
            **MYSQL_TABLE_KWARGS,
        )
