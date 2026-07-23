"""Add Hermes content product library.

Revision ID: 0071_hermes_content_product_library
Revises: 0070_hermes_content_factory
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0071_hermes_content_product_library"
down_revision = "0070_hermes_content_factory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint = mysql.BIGINT(unsigned=True)
    dt = mysql.DATETIME(fsp=6)
    common = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "hermes_content_products",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("product_key", sa.String(191), nullable=False),
        sa.Column("workspace_id", bigint, nullable=False),
        sa.Column("user_id", bigint, nullable=True),
        sa.Column("brand_name", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("market", sa.String(64), nullable=False, server_default="US"),
        sa.Column("product_brief", sa.Text(), nullable=True),
        sa.Column("facts_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("meta_json", sa.JSON(), nullable=True),
        sa.Column("created_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE", onupdate="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL", onupdate="RESTRICT"),
        sa.UniqueConstraint("workspace_id", "product_key", name="uq_hermes_content_product_ws_key"),
        **common,
    )
    op.create_index("idx_hermes_content_product_ws", "hermes_content_products", ["workspace_id", "updated_at"])
    op.create_table(
        "hermes_content_product_assets",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("product_id", bigint, nullable=False),
        sa.Column("workspace_id", bigint, nullable=False),
        sa.Column("user_id", bigint, nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="source"),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", bigint, nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=True),
        sa.Column("created_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["product_id"], ["hermes_content_products.id"], ondelete="CASCADE", onupdate="RESTRICT"),
        **common,
    )
    op.create_index("idx_hermes_content_product_asset_product", "hermes_content_product_assets", ["product_id", "kind", "id"])
    op.add_column("hermes_content_factory_projects", sa.Column("product_id", bigint, nullable=True))
    op.create_foreign_key(
        "fk_hermes_content_project_product",
        "hermes_content_factory_projects",
        "hermes_content_products",
        ["product_id"],
        ["id"],
        ondelete="SET NULL",
        onupdate="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_hermes_content_project_product", "hermes_content_factory_projects", type_="foreignkey")
    op.drop_column("hermes_content_factory_projects", "product_id")
    op.drop_table("hermes_content_product_assets")
    op.drop_table("hermes_content_products")
