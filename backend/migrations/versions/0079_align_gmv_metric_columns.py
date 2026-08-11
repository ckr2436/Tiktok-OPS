"""align deployed GMV metric tables with runtime models

Revision ID: 0079_align_gmv_metric_columns
Revises: 0078_ttb_shop_order_imports
Create Date: 2026-07-10 17:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0079_align_gmv_metric_columns"
down_revision = "0078_ttb_shop_order_imports"
branch_labels = None
depends_on = None


COMMON_COLUMNS = {
    "net_cost_cents": sa.Column("net_cost_cents", mysql.BIGINT(), nullable=True),
    "impressions": sa.Column("impressions", mysql.BIGINT(), nullable=True),
    "product_impressions": sa.Column("product_impressions", mysql.BIGINT(), nullable=True),
    "clicks": sa.Column("clicks", mysql.BIGINT(), nullable=True),
    "product_clicks": sa.Column("product_clicks", mysql.BIGINT(), nullable=True),
    "product_click_rate": sa.Column("product_click_rate", sa.Numeric(10, 6), nullable=True),
    "ad_click_rate": sa.Column("ad_click_rate", sa.Numeric(10, 6), nullable=True),
    "ad_conversion_rate": sa.Column("ad_conversion_rate", sa.Numeric(10, 6), nullable=True),
    "conversion_rate": sa.Column("conversion_rate", sa.Numeric(10, 6), nullable=True),
}

VIDEO_COLUMNS = {
    "video_view_rate_2s": sa.Column("video_view_rate_2s", sa.Numeric(10, 6), nullable=True),
    "video_view_rate_6s": sa.Column("video_view_rate_6s", sa.Numeric(10, 6), nullable=True),
    "video_view_rate_25": sa.Column("video_view_rate_25", sa.Numeric(10, 6), nullable=True),
    "video_view_rate_50": sa.Column("video_view_rate_50", sa.Numeric(10, 6), nullable=True),
    "video_view_rate_75": sa.Column("video_view_rate_75", sa.Numeric(10, 6), nullable=True),
    "video_view_rate_100": sa.Column("video_view_rate_100", sa.Numeric(10, 6), nullable=True),
}

ALL_SHOPS_COLUMNS = {
    "all_shops_orders": sa.Column("all_shops_orders", mysql.BIGINT(), nullable=True),
    "all_shops_gross_revenue_cents": sa.Column("all_shops_gross_revenue_cents", mysql.BIGINT(), nullable=True),
    "all_shops_roi": sa.Column("all_shops_roi", sa.Numeric(18, 4), nullable=True),
    "all_shops_cost_per_order": sa.Column("all_shops_cost_per_order", sa.Numeric(18, 4), nullable=True),
}


def _add_missing(table_name: str, columns: dict[str, sa.Column]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table_name, column.copy())


def upgrade() -> None:
    for table_name in ("gmv_product_metrics_daily", "gmv_product_metrics_hourly"):
        _add_missing(table_name, COMMON_COLUMNS)

    for table_name in ("gmv_creative_metrics_daily", "gmv_creative_metrics_hourly"):
        _add_missing(table_name, {**COMMON_COLUMNS, **VIDEO_COLUMNS})

    for table_name in ("gmv_duration_metrics_daily", "gmv_duration_metrics_hourly"):
        _add_missing(table_name, COMMON_COLUMNS)

    for table_name in ("gmv_livestream_metrics_daily", "gmv_livestream_metrics_hourly"):
        _add_missing(table_name, {**COMMON_COLUMNS, **ALL_SHOPS_COLUMNS})

    _add_missing(
        "gmv_creative_metrics_10min",
        {
            "workspace_id": sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=True),
            "auth_id": sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=True),
            "advertiser_id": sa.Column("advertiser_id", sa.String(64), nullable=True),
            "store_id": sa.Column("store_id", sa.String(64), nullable=True),
            "product_impressions": sa.Column("product_impressions", mysql.BIGINT(), nullable=True),
            "product_click_rate": sa.Column("product_click_rate", sa.Numeric(10, 6), nullable=True),
            "ad_conversion_rate": sa.Column("ad_conversion_rate", sa.Numeric(10, 6), nullable=True),
            "cost_per_order": sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        },
    )


def downgrade() -> None:
    # This migration repairs columns already expected by released runtime code.
    # Dropping them would reintroduce production failures, so downgrade is a no-op.
    pass
