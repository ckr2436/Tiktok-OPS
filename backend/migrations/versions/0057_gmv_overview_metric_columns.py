"""Backfill GMV overview metric columns to match ORM schema.

Revision ID: 0057_gmv_overview_metric_columns
Revises: 0056_strategy_scheduler_unification
Create Date: 2025-03-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0057_gmv_overview_metric_columns"
down_revision = "0056_strategy_scheduler_unification"
branch_labels = None
depends_on = None


METRIC_COLUMNS = [
    ("impressions", sa.Column("impressions", sa.BigInteger(), nullable=True)),
    (
        "product_impressions",
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
    ),
    ("clicks", sa.Column("clicks", sa.BigInteger(), nullable=True)),
    ("product_clicks", sa.Column("product_clicks", sa.BigInteger(), nullable=True)),
    (
        "product_click_rate",
        sa.Column("product_click_rate", sa.Numeric(10, 6), nullable=True),
    ),
    ("cost_cents", sa.Column("cost_cents", sa.BigInteger(), nullable=True)),
    ("net_cost_cents", sa.Column("net_cost_cents", sa.BigInteger(), nullable=True)),
    ("orders", sa.Column("orders", sa.BigInteger(), nullable=True)),
    (
        "gross_revenue_cents",
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
    ),
    (
        "cost_per_order",
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
    ),
    ("roi", sa.Column("roi", sa.Numeric(18, 4), nullable=True)),
    ("ad_click_rate", sa.Column("ad_click_rate", sa.Numeric(10, 6), nullable=True)),
    (
        "ad_conversion_rate",
        sa.Column("ad_conversion_rate", sa.Numeric(10, 6), nullable=True),
    ),
    ("conversion_rate", sa.Column("conversion_rate", sa.Numeric(10, 6), nullable=True)),
    (
        "video_view_rate_2s",
        sa.Column("video_view_rate_2s", sa.Numeric(10, 6), nullable=True),
    ),
    (
        "video_view_rate_6s",
        sa.Column("video_view_rate_6s", sa.Numeric(10, 6), nullable=True),
    ),
    (
        "video_view_rate_25",
        sa.Column("video_view_rate_25", sa.Numeric(10, 6), nullable=True),
    ),
    (
        "video_view_rate_50",
        sa.Column("video_view_rate_50", sa.Numeric(10, 6), nullable=True),
    ),
    (
        "video_view_rate_75",
        sa.Column("video_view_rate_75", sa.Numeric(10, 6), nullable=True),
    ),
    (
        "video_view_rate_100",
        sa.Column("video_view_rate_100", sa.Numeric(10, 6), nullable=True),
    ),
]

TABLES = ["gmv_overview_metrics_daily", "gmv_overview_metrics_hourly"]


def _add_missing_columns(table_name: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    for name, column in METRIC_COLUMNS:
        if name not in existing:
            op.add_column(table_name, column.copy())


def _drop_columns_if_exist(table_name: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    for name, _ in METRIC_COLUMNS:
        if name in existing:
            op.drop_column(table_name, name)


def upgrade():
    for table in TABLES:
        _add_missing_columns(table)


def downgrade():
    for table in TABLES:
        _drop_columns_if_exist(table)
