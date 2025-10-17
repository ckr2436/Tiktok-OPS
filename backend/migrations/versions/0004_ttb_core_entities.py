"""create ttb core entities and cursors (safe downgrade)

Revision ID: 0004_ttb_core_entities
Revises: b3f2e1d9c003
Create Date: 2025-10-17 00:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0004_ttb_core_entities"
down_revision = "b3f2e1d9c003"
branch_labels = None
depends_on = None


def _dt6():
    # 通用 DATETIME(6)
    return mysql.DATETIME(fsp=6)


def upgrade() -> None:
    # ----- ttb_sync_cursors -----
    op.create_table(
        "ttb_sync_cursors",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=sa.text("'tiktok-business'")),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("cursor_token", sa.String(length=256), nullable=True),
        sa.Column("since_time", _dt6(), nullable=True),
        sa.Column("until_time", _dt6(), nullable=True),
        sa.Column("last_rev", sa.String(length=64), nullable=True),
        sa.Column("extra_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint(
        "uk_ttb_cursor_scope", "ttb_sync_cursors", ["workspace_id", "provider", "auth_id", "resource_type"]
    )
    op.create_index("idx_ttb_cursor_scope", "ttb_sync_cursors", ["workspace_id", "auth_id", "resource_type"])

    # ----- ttb_business_centers -----
    op.create_table(
        "ttb_business_centers",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("bc_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_bc_scope", "ttb_business_centers", ["workspace_id", "auth_id", "bc_id"])
    op.create_index("idx_ttb_bc_scope", "ttb_business_centers", ["workspace_id", "auth_id", "bc_id"])
    op.create_index("idx_ttb_bc_updated", "ttb_business_centers", ["ext_updated_time"])

    # ----- ttb_advertisers -----
    op.create_table(
        "ttb_advertisers",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("bc_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("industry", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_adv_scope", "ttb_advertisers", ["workspace_id", "auth_id", "advertiser_id"])
    op.create_index("idx_ttb_adv_scope", "ttb_advertisers", ["workspace_id", "auth_id", "advertiser_id"])
    op.create_index("idx_ttb_adv_bc", "ttb_advertisers", ["bc_id"])
    op.create_index("idx_ttb_adv_updated", "ttb_advertisers", ["ext_updated_time"])
    op.create_index("idx_ttb_adv_status", "ttb_advertisers", ["status"])

    # ----- ttb_shops -----
    op.create_table(
        "ttb_shops",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        sa.Column("bc_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("region_code", sa.String(length=8), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_shop_scope", "ttb_shops", ["workspace_id", "auth_id", "shop_id"])
    op.create_index("idx_ttb_shop_scope", "ttb_shops", ["workspace_id", "auth_id", "shop_id"])
    op.create_index("idx_ttb_shop_adv", "ttb_shops", ["advertiser_id"])
    op.create_index("idx_ttb_shop_updated", "ttb_shops", ["ext_updated_time"])
    op.create_index("idx_ttb_shop_status", "ttb_shops", ["status"])

    # ----- ttb_products -----
    op.create_table(
        "ttb_products",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("price", sa.Numeric(18, 4), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_product_scope", "ttb_products", ["workspace_id", "auth_id", "product_id"])
    op.create_index("idx_ttb_product_scope", "ttb_products", ["workspace_id", "auth_id", "product_id"])
    op.create_index("idx_ttb_product_shop", "ttb_products", ["shop_id"])
    op.create_index("idx_ttb_product_updated", "ttb_products", ["ext_updated_time"])
    op.create_index("idx_ttb_product_status", "ttb_products", ["status"])


def downgrade() -> None:
    # 为避免 MySQL 在 DROP INDEX/CONSTRAINT 时出现 “needed in a foreign key constraint”，
    # 这里直接按“子表→父表”顺序 DROP TABLE，且在一个临时的无外键检查窗口内执行。
    op.execute("SET FOREIGN_KEY_CHECKS=0")

    # 子表优先
    op.execute("DROP TABLE IF EXISTS ttb_products")
    op.execute("DROP TABLE IF EXISTS ttb_shops")
    op.execute("DROP TABLE IF EXISTS ttb_advertisers")
    op.execute("DROP TABLE IF EXISTS ttb_business_centers")
    op.execute("DROP TABLE IF EXISTS ttb_sync_cursors")

    op.execute("SET FOREIGN_KEY_CHECKS=1")

