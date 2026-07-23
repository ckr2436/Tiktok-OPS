"""normalize GMV Max creative asset cache collation

Revision ID: 0076_creative_asset_collation
Revises: 0075_gmv_product_order_events
Create Date: 2026-07-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0076_creative_asset_collation"
down_revision = "0075_gmv_product_order_events"
branch_labels = None
depends_on = None


def _create_cache_table() -> None:
    op.create_table(
        "gmvmax_creative_asset_cache",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("video_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("preview_url", sa.Text(), nullable=True),
        sa.Column("video_cover_url", sa.Text(), nullable=True),
        sa.Column("duration", sa.Numeric(18, 4), nullable=True),
        sa.Column("identity_id", sa.String(length=128), nullable=True),
        sa.Column("identity_type", sa.String(length=64), nullable=True),
        sa.Column("identity_name", sa.String(length=255), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column(
            "fetched_at",
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
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "item_id",
            name="uq_gmvmax_creative_asset_scope",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "idx_gmvmax_creative_asset_product",
        "gmvmax_creative_asset_cache",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "item_group_id"],
    )
    op.create_index(
        "idx_gmvmax_creative_asset_video",
        "gmvmax_creative_asset_cache",
        ["video_id"],
    )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "gmvmax_creative_asset_cache" not in inspector.get_table_names():
        _create_cache_table()
        return
    op.execute(
        """
        alter table gmvmax_creative_asset_cache
        convert to character set utf8mb4 collate utf8mb4_0900_ai_ci
        """
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "gmvmax_creative_asset_cache" in inspector.get_table_names():
        op.execute(
            """
            alter table gmvmax_creative_asset_cache
            convert to character set utf8mb4 collate utf8mb4_unicode_ci
            """
        )
