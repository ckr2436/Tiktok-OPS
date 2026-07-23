"""add privacy-safe TikTok Shop order imports

Revision ID: 0078_ttb_shop_order_imports
Revises: 0077_gmv_hermes_mysql_memory
Create Date: 2026-07-10 17:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0078_ttb_shop_order_imports"
down_revision = "0077_gmv_hermes_mysql_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "ttb_shop_order_import_batches" not in tables:
        op.create_table(
            "ttb_shop_order_import_batches",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("advertiser_id", sa.String(64), nullable=False),
            sa.Column("store_id", sa.String(64), nullable=False),
            sa.Column("source_filename", sa.String(255), nullable=False),
            sa.Column("file_sha256", sa.String(64), nullable=False),
            sa.Column("source_timezone", sa.String(64), nullable=False),
            sa.Column("advertiser_timezone", sa.String(64), nullable=False),
            sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
            sa.Column("status", sa.String(24), nullable=False, server_default="PROCESSING"),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("order_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("valid_order_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_from_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_to_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("paid_from_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("paid_to_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("gross_amount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("refund_amount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("net_amount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("summary_json", sa.JSON(), nullable=False),
            sa.Column("error_message", sa.String(1000), nullable=True),
            sa.Column("created_by", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)")),
            sa.UniqueConstraint(
                "workspace_id", "auth_id", "store_id", "file_sha256", "source_timezone",
                name="uq_ttb_shop_order_import_file",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_ttb_shop_order_import_scope",
            "ttb_shop_order_import_batches",
            ["workspace_id", "auth_id", "advertiser_id", "store_id", "created_at"],
        )

    if "ttb_shop_orders" not in tables:
        op.create_table(
            "ttb_shop_orders",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("advertiser_id", sa.String(64), nullable=False),
            sa.Column("store_id", sa.String(64), nullable=False),
            sa.Column("external_order_id", sa.String(64), nullable=False),
            sa.Column("order_status", sa.String(48), nullable=True),
            sa.Column("order_substatus", sa.String(64), nullable=True),
            sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
            sa.Column("order_amount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("refund_amount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("net_revenue_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("source_timezone", sa.String(64), nullable=False),
            sa.Column("created_source_local", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("paid_source_local", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_at_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("paid_at_utc", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("order_channel", sa.String(64), nullable=True),
            sa.Column("creator_handle", sa.String(191), nullable=True),
            sa.Column("last_import_batch_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)")),
            sa.UniqueConstraint(
                "workspace_id", "auth_id", "store_id", "external_order_id",
                name="uq_ttb_shop_order_scope",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_ttb_shop_order_paid",
            "ttb_shop_orders",
            ["workspace_id", "auth_id", "advertiser_id", "store_id", "paid_at_utc"],
        )

    if "ttb_shop_order_items" not in tables:
        op.create_table(
            "ttb_shop_order_items",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("order_pk", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("line_key", sa.String(64), nullable=False),
            sa.Column("product_id", sa.String(64), nullable=True),
            sa.Column("sku_id", sa.String(64), nullable=True),
            sa.Column("seller_sku", sa.String(191), nullable=True),
            sa.Column("product_name", sa.String(512), nullable=True),
            sa.Column("variation", sa.String(255), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("unit_original_price_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("subtotal_before_discount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("platform_discount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("seller_discount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("subtotal_after_discount_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("product_category", sa.String(512), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.ForeignKeyConstraint(["order_pk"], ["ttb_shop_orders.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("order_pk", "line_key", name="uq_ttb_shop_order_line"),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index("idx_ttb_shop_order_item_product", "ttb_shop_order_items", ["product_id", "order_pk"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in ("ttb_shop_order_items", "ttb_shop_orders", "ttb_shop_order_import_batches"):
        if table_name in tables:
            op.drop_table(table_name)
