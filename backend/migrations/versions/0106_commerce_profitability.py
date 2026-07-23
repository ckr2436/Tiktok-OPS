"""Add commerce product mapping, cost versions, and Shop timezone provenance.

Revision ID: 0106_commerce_profitability
Revises: 0106_hermes_content_director_audit
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0106_commerce_profitability"
down_revision = "0106_hermes_content_director_audit"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _add_timezone_provenance() -> None:
    bind = op.get_bind()
    columns = {
        str(item["name"])
        for item in _inspector().get_columns("oauth_tiktok_shop_shops")
    }
    if "timezone_source" not in columns:
        op.add_column(
            "oauth_tiktok_shop_shops",
            sa.Column(
                "timezone_source",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'platform_default'"),
            ),
        )
    if "timezone_verified_at" not in columns:
        op.add_column(
            "oauth_tiktok_shop_shops",
            sa.Column("timezone_verified_at", sa.DateTime(), nullable=True),
        )
    if "timezone_locked" not in columns:
        op.add_column(
            "oauth_tiktok_shop_shops",
            sa.Column(
                "timezone_locked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    op.alter_column(
        "oauth_tiktok_shop_shops",
        "timezone_name",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default=sa.text("'Etc/GMT+8'"),
    )
    timestamp_expression = (
        "utc_timestamp(6)"
        if bind.dialect.name == "mysql"
        else "CURRENT_TIMESTAMP"
    )
    op.execute(
        sa.text(
            f"""
        update oauth_tiktok_shop_shops
        set timezone_name='Etc/GMT+8',
            timezone_source='merchant_confirmed_fixed_utc_minus_8',
            timezone_verified_at=coalesce(timezone_verified_at, {timestamp_expression}),
            timezone_locked=1
        """
        )
    )


def _create_tables() -> None:
    existing = set(_inspector().get_table_names())
    ubigint = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")
    timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    if "commerce_product_mappings" not in existing:
        op.create_table(
            "commerce_product_mappings",
            sa.Column("id", ubigint, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", ubigint, nullable=False),
            sa.Column("shop_row_id", ubigint, nullable=False),
            sa.Column("shop_product_id", sa.String(128), nullable=False),
            sa.Column("business_auth_id", ubigint, nullable=True),
            sa.Column("advertiser_id", sa.String(64), nullable=False),
            sa.Column("store_id", sa.String(64), nullable=False),
            sa.Column("item_group_id", sa.String(128), nullable=False),
            sa.Column(
                "source",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'exact_product_id'"),
            ),
            sa.Column(
                "confidence",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("1.0"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column(
                "created_at",
                timestamp,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                timestamp,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                ondelete="CASCADE",
                name="fk_commerce_mapping_workspace",
            ),
            sa.ForeignKeyConstraint(
                ["shop_row_id"],
                ["oauth_tiktok_shop_shops.id"],
                ondelete="CASCADE",
                name="fk_commerce_mapping_shop",
            ),
            sa.ForeignKeyConstraint(
                ["business_auth_id"],
                ["oauth_accounts_ttb.id"],
                ondelete="SET NULL",
                name="fk_commerce_mapping_auth",
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "shop_row_id",
                "shop_product_id",
                "advertiser_id",
                "item_group_id",
                name="uq_commerce_product_mapping",
            ),
        )
        op.create_index(
            "idx_commerce_mapping_product",
            "commerce_product_mappings",
            ["workspace_id", "shop_row_id", "shop_product_id"],
        )
        op.create_index(
            "idx_commerce_mapping_advertiser",
            "commerce_product_mappings",
            ["workspace_id", "advertiser_id", "store_id"],
        )
    if "commerce_product_cost_versions" not in existing:
        op.create_table(
            "commerce_product_cost_versions",
            sa.Column("id", ubigint, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", ubigint, nullable=False),
            sa.Column("shop_row_id", ubigint, nullable=False),
            sa.Column("product_id", sa.String(128), nullable=False),
            sa.Column(
                "sku_id",
                sa.String(128),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column("effective_from", timestamp, nullable=False),
            sa.Column(
                "currency",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'USD'"),
            ),
            sa.Column(
                "unit_cost",
                sa.Numeric(20, 6),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "packaging_cost",
                sa.Numeric(20, 6),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "fulfillment_cost",
                sa.Numeric(20, 6),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "seller_shipping_cost",
                sa.Numeric(20, 6),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "other_variable_cost",
                sa.Numeric(20, 6),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "platform_fee_rate",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "payment_fee_rate",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "affiliate_commission_rate",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "expected_refund_rate",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "target_margin_rate",
                sa.Numeric(12, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", ubigint, nullable=True),
            sa.Column(
                "created_at",
                timestamp,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                ondelete="CASCADE",
                name="fk_commerce_cost_workspace",
            ),
            sa.ForeignKeyConstraint(
                ["shop_row_id"],
                ["oauth_tiktok_shop_shops.id"],
                ondelete="CASCADE",
                name="fk_commerce_cost_shop",
            ),
            sa.ForeignKeyConstraint(
                ["created_by_user_id"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_commerce_cost_user",
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "shop_row_id",
                "product_id",
                "sku_id",
                "effective_from",
                name="uq_commerce_product_cost_version",
            ),
        )
        op.create_index(
            "idx_commerce_cost_effective",
            "commerce_product_cost_versions",
            [
                "workspace_id",
                "shop_row_id",
                "product_id",
                "sku_id",
                "effective_from",
            ],
        )


def _normalize_shop_sync_timestamps() -> None:
    bind = op.get_bind()
    if "tiktok_shop_sync_runs" not in set(_inspector().get_table_names()):
        return
    if bind.dialect.name == "mysql":
        op.execute(
            sa.text(
                """
                update tiktok_shop_sync_runs
                set started_at = date_sub(started_at, interval 8 hour)
                where completed_at is not null
                  and timestampdiff(minute, completed_at, started_at)
                      between 420 and 540
                """
            )
        )
        op.alter_column(
            "tiktok_shop_sync_runs",
            "started_at",
            existing_type=mysql.DATETIME(fsp=6),
            existing_nullable=False,
            server_default=sa.text("(UTC_TIMESTAMP(6))"),
        )


def _seed_exact_product_mappings() -> None:
    bind = op.get_bind()
    insert_prefix = (
        "insert ignore"
        if bind.dialect.name == "mysql"
        else "insert or ignore"
        if bind.dialect.name == "sqlite"
        else "insert"
    )
    product_match = (
        "binary ig.item_group_id=binary sp.product_id"
        if bind.dialect.name == "mysql"
        else "ig.item_group_id=sp.product_id"
    )
    store_match = (
        "binary shop.shop_id=binary ig.store_id"
        if bind.dialect.name == "mysql"
        else "shop.shop_id=ig.store_id"
    )
    op.execute(
        sa.text(
            f"""
        {insert_prefix} into commerce_product_mappings (
            workspace_id, shop_row_id, shop_product_id, business_auth_id,
            advertiser_id, store_id, item_group_id, source, confidence,
            is_active
        )
        select
            sp.workspace_id,
            sp.shop_row_id,
            sp.product_id,
            min(ig.auth_id),
            ig.advertiser_id,
            ig.store_id,
            ig.item_group_id,
            'exact_product_id',
            1.0,
            1
        from tiktok_shop_products sp
        join gmvmax_product_campaign_item_groups ig
          on ig.workspace_id=sp.workspace_id
         and {product_match}
        join oauth_tiktok_shop_shops shop
          on shop.id=sp.shop_row_id
         and shop.workspace_id=sp.workspace_id
         and {store_match}
        group by
            sp.workspace_id, sp.shop_row_id, sp.product_id,
            ig.advertiser_id, ig.store_id, ig.item_group_id
        """
        )
    )


def upgrade() -> None:
    _add_timezone_provenance()
    _create_tables()
    _normalize_shop_sync_timestamps()
    _seed_exact_product_mappings()


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    if "commerce_product_cost_versions" in tables:
        op.drop_table("commerce_product_cost_versions")
    if "commerce_product_mappings" in tables:
        op.drop_table("commerce_product_mappings")
    columns = {
        str(item["name"])
        for item in _inspector().get_columns("oauth_tiktok_shop_shops")
    }
    for name in ("timezone_locked", "timezone_verified_at", "timezone_source"):
        if name in columns:
            op.drop_column("oauth_tiktok_shop_shops", name)
    op.alter_column(
        "oauth_tiktok_shop_shops",
        "timezone_name",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default=sa.text("'America/New_York'"),
    )
