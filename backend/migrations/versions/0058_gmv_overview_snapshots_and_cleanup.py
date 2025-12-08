"""Refine overview metrics tables and add snapshots.

Revision ID: 0058_gmv_overview_snapshots_and_cleanup
Revises: 0057_gmv_overview_metric_columns
Create Date: 2025-03-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "0058_gmv_overview_snapshots_and_cleanup"
down_revision = "0057_gmv_overview_metric_columns"
branch_labels = None
depends_on = None


TABLES = ["gmv_overview_metrics_daily", "gmv_overview_metrics_hourly"]

COLUMNS_TO_DROP = {
    "impressions": sa.BigInteger(),
    "product_impressions": sa.BigInteger(),
    "clicks": sa.BigInteger(),
    "product_clicks": sa.BigInteger(),
    "product_click_rate": sa.Numeric(10, 6),
    "ad_click_rate": sa.Numeric(10, 6),
    "ad_conversion_rate": sa.Numeric(10, 6),
    "conversion_rate": sa.Numeric(10, 6),
    "video_view_rate_2s": sa.Numeric(10, 6),
    "video_view_rate_6s": sa.Numeric(10, 6),
    "video_view_rate_25": sa.Numeric(10, 6),
    "video_view_rate_50": sa.Numeric(10, 6),
    "video_view_rate_75": sa.Numeric(10, 6),
    "video_view_rate_100": sa.Numeric(10, 6),
}


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    inspector = inspect(op.get_bind())
    existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name in existing_columns:
        op.drop_column(table_name, column_name)


def _add_column_if_missing(table_name: str, column_name: str, column_type: sa.types.TypeEngine) -> None:
    inspector = inspect(op.get_bind())
    existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name not in existing_columns:
        column_copy = column_type.copy() if hasattr(column_type, "copy") else column_type
        op.add_column(table_name, sa.Column(column_name, column_copy))


def _drop_unique_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    inspector = inspect(op.get_bind())
    existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints(table_name)}
    if constraint_name in existing_constraints:
        op.drop_constraint(constraint_name, table_name, type_="unique")


def _create_unique_constraint_if_missing(
    table_name: str, constraint_name: str, columns: list[str]
) -> None:
    inspector = inspect(op.get_bind())
    existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints(table_name)}
    if constraint_name not in existing_constraints:
        op.create_unique_constraint(constraint_name, table_name, columns)


def upgrade() -> None:
    # Remove exposure-related columns from overview tables.
    for table in TABLES:
        for column_name in COLUMNS_TO_DROP:
            _drop_column_if_exists(table, column_name)

    # Refresh unique constraints with stable names.
    _drop_unique_constraint_if_exists(
        "gmv_overview_metrics_daily", "uk_overview_daily"
    )
    _drop_unique_constraint_if_exists(
        "gmv_overview_metrics_hourly", "uk_overview_hourly"
    )

    _create_unique_constraint_if_missing(
        "gmv_overview_metrics_daily",
        "uk_gmv_overview_metrics_daily_scope",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_day"],
    )
    _create_unique_constraint_if_missing(
        "gmv_overview_metrics_hourly",
        "uk_gmv_overview_metrics_hourly_scope",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_hour"],
    )

    # Create snapshots table for aggregated overview results.
    op.create_table(
        "gmv_overview_snapshots",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("snapshot_type", sa.String(length=32), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=False, fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "snapshot_type",
            "end_date",
            name="uk_gmv_overview_snapshot_scope",
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_gmv_overview_snapshots_ws_auth_adv",
        "gmv_overview_snapshots",
        ["workspace_id", "auth_id", "advertiser_id"],
    )


def downgrade() -> None:
    inspector = inspect(op.get_bind())

    # Drop snapshots table and index if they exist.
    if "gmv_overview_snapshots" in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("gmv_overview_snapshots")}
        if "ix_gmv_overview_snapshots_ws_auth_adv" in existing_indexes:
            op.drop_index("ix_gmv_overview_snapshots_ws_auth_adv", table_name="gmv_overview_snapshots")
        op.drop_table("gmv_overview_snapshots")

    # Restore previous unique constraint names.
    _drop_unique_constraint_if_exists(
        "gmv_overview_metrics_daily", "uk_gmv_overview_metrics_daily_scope"
    )
    _drop_unique_constraint_if_exists(
        "gmv_overview_metrics_hourly", "uk_gmv_overview_metrics_hourly_scope"
    )

    _create_unique_constraint_if_missing(
        "gmv_overview_metrics_daily",
        "uk_overview_daily",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_day"],
    )
    _create_unique_constraint_if_missing(
        "gmv_overview_metrics_hourly",
        "uk_overview_hourly",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_hour"],
    )

    # Re-add dropped columns for backward compatibility.
    for table in TABLES:
        for column_name, column_type in COLUMNS_TO_DROP.items():
            _add_column_if_missing(table, column_name, column_type)
