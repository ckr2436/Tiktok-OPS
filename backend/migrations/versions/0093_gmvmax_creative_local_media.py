"""Persist local GMV Max creative video and cover cache state.

Revision ID: 0093_gmvmax_creative_local_media
Revises: 0092_add_creative_status_to_10min_metrics
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy import inspect


revision = "0093_gmvmax_creative_local_media"
down_revision = "0092_add_creative_status_to_10min_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The cache table predates Alembic ownership and was historically created
    # by the sync service. Claim it here so a fresh install can migrate safely.
    op.execute(
        """
        create table if not exists gmvmax_creative_asset_cache (
            id bigint unsigned not null auto_increment primary key,
            workspace_id bigint unsigned not null,
            auth_id bigint unsigned not null,
            advertiser_id varchar(64) not null,
            store_id varchar(64) not null,
            item_id varchar(64) not null,
            item_group_id varchar(64) null,
            video_id varchar(128) null,
            title text null,
            preview_url text null,
            video_cover_url text null,
            duration decimal(18,4) null,
            identity_id varchar(128) null,
            identity_type varchar(64) null,
            identity_name varchar(255) null,
            raw_json json null,
            fetched_at datetime(6) not null default current_timestamp(6),
            created_at datetime(6) not null default current_timestamp(6),
            updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
            unique key uq_gmvmax_creative_asset_scope (
                workspace_id, auth_id, advertiser_id, store_id, item_id
            ),
            key idx_gmvmax_creative_asset_product (
                workspace_id, auth_id, advertiser_id, store_id, item_group_id
            ),
            key idx_gmvmax_creative_asset_video (video_id)
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    expected_columns = [
        sa.Column("local_preview_path", sa.Text(), nullable=True),
        sa.Column("local_cover_path", sa.Text(), nullable=True),
        sa.Column("preview_content_type", sa.String(length=128), nullable=True),
        sa.Column("cover_content_type", sa.String(length=128), nullable=True),
        sa.Column(
            "media_cache_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("media_cache_error", sa.Text(), nullable=True),
        sa.Column("media_cache_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("media_cache_next_retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("media_cached_at", mysql.DATETIME(fsp=6), nullable=True),
    ]
    inspector = inspect(op.get_bind())
    existing_columns = {
        column["name"] for column in inspector.get_columns("gmvmax_creative_asset_cache")
    }
    for column in expected_columns:
        if column.name not in existing_columns:
            op.add_column("gmvmax_creative_asset_cache", column)

    existing_indexes = {
        index["name"] for index in inspect(op.get_bind()).get_indexes("gmvmax_creative_asset_cache")
    }
    if "idx_gmvmax_creative_asset_media_cache" not in existing_indexes:
        op.create_index(
            "idx_gmvmax_creative_asset_media_cache",
            "gmvmax_creative_asset_cache",
            ["media_cache_status", "media_cache_next_retry_at"],
            unique=False,
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    existing_indexes = {
        index["name"] for index in inspector.get_indexes("gmvmax_creative_asset_cache")
    }
    if "idx_gmvmax_creative_asset_media_cache" in existing_indexes:
        op.drop_index("idx_gmvmax_creative_asset_media_cache", table_name="gmvmax_creative_asset_cache")
    existing_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("gmvmax_creative_asset_cache")
    }
    for column_name in (
        "media_cached_at",
        "media_cache_next_retry_at",
        "media_cache_attempts",
        "media_cache_error",
        "media_cache_status",
        "cover_content_type",
        "preview_content_type",
        "local_cover_path",
        "local_preview_path",
    ):
        if column_name in existing_columns:
            op.drop_column("gmvmax_creative_asset_cache", column_name)
