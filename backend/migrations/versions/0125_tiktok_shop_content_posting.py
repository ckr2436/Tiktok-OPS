"""add TikTok Shop creator content posting workflow

Revision ID: 0125_tiktok_shop_content_posting
Revises: 0124_doubao_ten_reference_images
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0125_tiktok_shop_content_posting"
down_revision = "0124_doubao_ten_reference_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_tiktok_shop_authz_sessions",
        sa.Column(
            "authorization_type",
            sa.String(length=16),
            nullable=False,
            server_default="seller",
        ),
    )
    op.create_index(
        "idx_tiktok_shop_session_type",
        "oauth_tiktok_shop_authz_sessions",
        ["workspace_id", "authorization_type", "status"],
    )

    op.create_table(
        "tiktok_shop_content_posts",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("account_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("created_by_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", mysql.BINARY(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("local_file_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("file_size", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sha256_digest", mysql.BINARY(length=32), nullable=False),
        sa.Column("official_file_id", sa.String(length=192), nullable=True),
        sa.Column("upload_md5", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=128), nullable=False),
        sa.Column("product_link_title", sa.String(length=64), nullable=False),
        sa.Column("video_title", sa.Text(), nullable=True),
        sa.Column("cover_uri", sa.Text(), nullable=True),
        sa.Column("cover_timestamp_ms", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("music_id", sa.String(length=128), nullable=True),
        sa.Column("precheck_task_id", sa.String(length=192), nullable=True),
        sa.Column("precheck_status", sa.String(length=32), nullable=True),
        sa.Column("precheck_issues_json", sa.JSON(), nullable=True),
        sa.Column("video_id", sa.String(length=192), nullable=True),
        sa.Column("post_status", sa.String(length=32), nullable=True),
        sa.Column("post_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("workflow_status", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("publish_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("poll_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_poll_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("provider_request_ids_json", sa.JSON(), nullable=True),
        sa.Column("api_versions_json", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_error_request_id", sa.String(length=128), nullable=True),
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
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["oauth_tiktok_shop_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_ttshop_content_post_idempotency",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        "idx_ttshop_content_post_workspace_status",
        "tiktok_shop_content_posts",
        ["workspace_id", "workflow_status"],
    )
    op.create_index(
        "idx_ttshop_content_post_account_created",
        "tiktok_shop_content_posts",
        ["account_id", "created_at"],
    )
    op.create_index(
        "idx_ttshop_content_post_video",
        "tiktok_shop_content_posts",
        ["workspace_id", "video_id"],
    )


def downgrade() -> None:
    op.drop_table("tiktok_shop_content_posts")
    op.drop_index("idx_tiktok_shop_session_type", table_name="oauth_tiktok_shop_authz_sessions")
    op.drop_column("oauth_tiktok_shop_authz_sessions", "authorization_type")
