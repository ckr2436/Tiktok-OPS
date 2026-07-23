"""add user/device scoped Hermes browser bridges

Revision ID: 0072_hermes_browser_bridges
Revises: 0071_hermes_content_product_library
Create Date: 2026-07-03 21:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0072_hermes_browser_bridges"
down_revision = "0071_hermes_content_product_library"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    op.create_table(
        "hermes_browser_bridges",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("bridge_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("device_name", sa.String(length=255), nullable=True),
        sa.Column("cdp_url", sa.String(length=512), nullable=False),
        sa.Column("server_port", sa.Integer(), nullable=True),
        sa.Column("inbox_root", sa.String(length=1024), nullable=True),
        sa.Column("outbox_root", sa.String(length=1024), nullable=True),
        sa.Column("browser", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("active_project_id", UBigInt, nullable=True),
        sa.Column("active_stage_id", UBigInt, nullable=True),
        sa.Column("lease_expires_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column("last_seen_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column("load_json", sa.JSON(), nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=True),
        sa.Column("created_at", mysql_dialect.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column("updated_at", mysql_dialect.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
    )
    op.create_unique_constraint("uq_hermes_browser_bridge_id", "hermes_browser_bridges", ["bridge_id"])
    op.create_unique_constraint("uq_hermes_browser_bridge_device", "hermes_browser_bridges", ["workspace_id", "user_id", "device_id"])
    op.create_index("idx_hermes_browser_bridge_ws_user", "hermes_browser_bridges", ["workspace_id", "user_id", "last_seen_at"])
    op.create_index("idx_hermes_browser_bridge_lease", "hermes_browser_bridges", ["status", "active_project_id", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("idx_hermes_browser_bridge_lease", table_name="hermes_browser_bridges")
    op.drop_index("idx_hermes_browser_bridge_ws_user", table_name="hermes_browser_bridges")
    op.drop_constraint("uq_hermes_browser_bridge_device", "hermes_browser_bridges", type_="unique")
    op.drop_constraint("uq_hermes_browser_bridge_id", "hermes_browser_bridges", type_="unique")
    op.drop_table("hermes_browser_bridges")
