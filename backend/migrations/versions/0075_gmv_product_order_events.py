"""add product order event timing table

Revision ID: 0075_gmv_product_order_events
Revises: 0074_gmv_realtime_guard_tables
Create Date: 2026-07-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0075_gmv_product_order_events"
down_revision = "0074_gmv_realtime_guard_tables"
branch_labels = None
depends_on = None


ID_TYPE = sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade():
    op.create_table(
        "gmv_product_order_events",
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("order_time_hour", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("advertiser_timezone", sa.String(length=64), nullable=True),
        sa.Column("order_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'GMVMAX_PRODUCT_HOURLY'"),
        ),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column(
            "first_seen_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "last_seen_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
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
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            "order_time_hour",
            name="uk_product_order_event_hour",
        ),
    )
    op.create_index(
        "idx_product_order_events_product_time",
        "gmv_product_order_events",
        ["workspace_id", "auth_id", "item_group_id", "order_time_hour"],
    )
    op.create_index(
        "idx_product_order_events_campaign_time",
        "gmv_product_order_events",
        ["campaign_id", "order_time_hour"],
    )


def downgrade():
    op.drop_index("idx_product_order_events_campaign_time", table_name="gmv_product_order_events")
    op.drop_index("idx_product_order_events_product_time", table_name="gmv_product_order_events")
    op.drop_table("gmv_product_order_events")
