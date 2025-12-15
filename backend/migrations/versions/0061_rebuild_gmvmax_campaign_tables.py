"""Rebuild GMV Max campaign catalog, metrics, mapping, and snapshot tables.

Revision ID: 0061_rebuild_gmvmax_campaign_tables
Revises: 0060_refresh_gmv_overview_snapshot_unique
Create Date: 2025-06-08 00:00:00.000000
"""

from __future__ import annotations

import os
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.engine import Inspector

revision = "0061_rebuild_gmvmax_campaign_tables"
down_revision = "0060_refresh_gmv_overview_snapshot_unique"
branch_labels = None
depends_on = None


OLD_TABLES = [
    "gmv_campaign_livestreams",
    "gmv_campaign_creatives",
    "gmv_campaign_metrics_hourly",
    "gmv_campaign_metrics_daily",
    "gmv_campaign_products",
    "gmv_campaigns",
]


def _drop_old_tables(inspector: Inspector) -> None:
    drop_legacy = os.getenv("GMVMAX_DROP_LEGACY_TABLES", "").lower() in {"1", "true", "yes"}
    suffix = datetime.utcnow().strftime("%Y%m%d")
    for name in OLD_TABLES:
        if name in inspector.get_table_names():
            if drop_legacy:
                op.drop_table(name)
            else:
                legacy_name = f"{name}_legacy_{suffix}"
                if legacy_name in inspector.get_table_names():
                    continue
                op.rename_table(name, legacy_name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    _drop_old_tables(inspector)

    op.create_table(
        "gmvmax_product_campaign_catalog",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_name", sa.String(length=255), nullable=True),
        sa.Column("operation_status", sa.String(length=32), nullable=True),
        sa.Column("secondary_status", sa.String(length=128), nullable=True),
        sa.Column("objective_type", sa.String(length=64), nullable=True),
        sa.Column("create_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("modify_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("list_raw_json", sa.JSON(), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("shopping_ads_type", sa.String(length=16), nullable=False, server_default="PRODUCT"),
        sa.Column("product_specific_type", sa.String(length=64), nullable=True),
        sa.Column("optimization_goal", sa.String(length=64), nullable=True),
        sa.Column("deep_bid_type", sa.String(length=64), nullable=True),
        sa.Column("roas_bid", sa.Numeric(18, 4), nullable=True),
        sa.Column("budget_cents", sa.BigInteger(), nullable=True),
        sa.Column("schedule_type", sa.String(length=64), nullable=True),
        sa.Column("schedule_start_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("schedule_end_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("detail_raw_json", sa.JSON(), nullable=True),
        sa.Column("list_synced_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("detail_synced_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id", "auth_id", "advertiser_id", "campaign_id", name="uk_prod_campaign_catalog"
        ),
        sa.Index("idx_prod_campaign_catalog_ws_adv", "workspace_id", "advertiser_id"),
        sa.Index("idx_prod_campaign_catalog_store", "store_id"),
        sa.Index("idx_prod_campaign_catalog_modify", "modify_time_utc"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_catalog",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_name", sa.String(length=255), nullable=True),
        sa.Column("operation_status", sa.String(length=32), nullable=True),
        sa.Column("secondary_status", sa.String(length=128), nullable=True),
        sa.Column("objective_type", sa.String(length=64), nullable=True),
        sa.Column("create_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("modify_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("list_raw_json", sa.JSON(), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("shopping_ads_type", sa.String(length=16), nullable=False, server_default="LIVE"),
        sa.Column("optimization_goal", sa.String(length=64), nullable=True),
        sa.Column("deep_bid_type", sa.String(length=64), nullable=True),
        sa.Column("roas_bid", sa.Numeric(18, 4), nullable=True),
        sa.Column("budget_cents", sa.BigInteger(), nullable=True),
        sa.Column("schedule_type", sa.String(length=64), nullable=True),
        sa.Column("schedule_start_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("schedule_end_time_utc", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("detail_raw_json", sa.JSON(), nullable=True),
        sa.Column("list_synced_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("detail_synced_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id", "auth_id", "advertiser_id", "campaign_id", name="uk_live_campaign_catalog"
        ),
        sa.Index("idx_live_campaign_catalog_ws_adv", "workspace_id", "advertiser_id"),
        sa.Index("idx_live_campaign_catalog_store", "store_id"),
        sa.Index("idx_live_campaign_catalog_modify", "modify_time_utc"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_product_campaign_item_groups",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            name="uk_prod_campaign_item",
        ),
        sa.Index("idx_prod_item_group", "item_group_id"),
        sa.Index("idx_prod_campaign", "campaign_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_identities",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("identity_id", sa.String(length=64), nullable=False),
        sa.Column("identity_type", sa.String(length=64), nullable=False),
        sa.Column("identity_authorized_bc_id", sa.String(length=64), nullable=True),
        sa.Column("identity_authorized_shop_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "campaign_id",
            "identity_id",
            "identity_type",
            name="uk_live_campaign_identity",
        ),
        sa.Index("idx_live_identity", "identity_id"),
        sa.Index("idx_live_campaign", "campaign_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_product_campaign_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            name="uk_prod_campaign_day",
        ),
        sa.Index(
            "idx_prod_campaign_day_store_date",
            "workspace_id",
            "store_id",
            "stat_time_day",
        ),
        sa.Index("idx_prod_campaign_day_campaign_date", "campaign_id", "stat_time_day"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_product_campaign_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", sa.dialects.mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_hour",
            name="uk_prod_campaign_hour",
        ),
        sa.Index(
            "idx_prod_campaign_hour_store_time",
            "workspace_id",
            "store_id",
            "stat_time_hour",
        ),
        sa.Index("idx_prod_campaign_hour_campaign_time", "campaign_id", "stat_time_hour"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_metrics_daily",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_orders", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_10s_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_day",
            name="uk_live_campaign_day",
        ),
        sa.Index(
            "idx_live_campaign_day_store_date",
            "workspace_id",
            "store_id",
            "stat_time_day",
        ),
        sa.Index("idx_live_campaign_day_campaign_date", "campaign_id", "stat_time_day"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_metrics_hourly",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_hour", sa.dialects.mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_orders", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_10s_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "stat_time_hour",
            name="uk_live_campaign_hour",
        ),
        sa.Index(
            "idx_live_campaign_hour_store_time",
            "workspace_id",
            "store_id",
            "stat_time_hour",
        ),
        sa.Index("idx_live_campaign_hour_campaign_time", "campaign_id", "stat_time_hour"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_product_campaign_snapshot_batches",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("snapshot_type", sa.String(length=32), nullable=False),
        sa.Column("snapshot_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "start_date",
            "end_date",
            "snapshot_type",
            name="uk_prod_snapshot_batch",
        ),
        sa.Index("idx_prod_snapshot_batch_time", "snapshot_at"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_product_campaign_snapshot_rows",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("batch_id", "campaign_id", name="uk_prod_snapshot_row"),
        sa.Index("idx_prod_snapshot_row_campaign", "campaign_id"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["gmvmax_product_campaign_snapshot_batches.id"],
            ondelete="CASCADE",
            onupdate="RESTRICT",
            name="fk_prod_snapshot_batch",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_snapshot_batches",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("snapshot_type", sa.String(length=32), nullable=False),
        sa.Column("snapshot_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "start_date",
            "end_date",
            "snapshot_type",
            name="uk_live_snapshot_batch",
        ),
        sa.Index("idx_live_snapshot_batch_time", "snapshot_at"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "gmvmax_live_campaign_snapshot_rows",
        sa.Column("id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_orders", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("all_shops_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("all_shops_cost_per_order", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_10s_views", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_10s_live_view", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint("batch_id", "campaign_id", name="uk_live_snapshot_row"),
        sa.Index("idx_live_snapshot_row_campaign", "campaign_id"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["gmvmax_live_campaign_snapshot_batches.id"],
            ondelete="CASCADE",
            onupdate="RESTRICT",
            name="fk_live_snapshot_batch",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    # Drop new tables in reverse dependency order
    for table in [
        "gmvmax_live_campaign_snapshot_rows",
        "gmvmax_live_campaign_snapshot_batches",
        "gmvmax_product_campaign_snapshot_rows",
        "gmvmax_product_campaign_snapshot_batches",
        "gmvmax_live_campaign_metrics_hourly",
        "gmvmax_live_campaign_metrics_daily",
        "gmvmax_product_campaign_metrics_hourly",
        "gmvmax_product_campaign_metrics_daily",
        "gmvmax_live_campaign_identities",
        "gmvmax_product_campaign_item_groups",
        "gmvmax_live_campaign_catalog",
        "gmvmax_product_campaign_catalog",
    ]:
        op.drop_table(table)
