"""add independent TikTok Shop OAuth and authorized shop storage

Revision ID: 0082_tiktok_shop_oauth
Revises: 0081_encrypt_ai_provider_keys
Create Date: 2026-07-11 08:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0082_tiktok_shop_oauth"
down_revision = "0081_encrypt_ai_provider_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    provider_columns = {column["name"] for column in inspector.get_columns("oauth_provider_apps")}
    if "service_id" not in provider_columns:
        op.add_column(
            "oauth_provider_apps",
            sa.Column("service_id", sa.String(length=128), nullable=True),
        )

    tables = set(inspector.get_table_names())
    if "oauth_tiktok_shop_authz_sessions" not in tables:
        op.create_table(
            "oauth_tiktok_shop_authz_sessions",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("state", sa.String(length=64), nullable=False),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("provider_app_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("return_to", sa.String(length=512), nullable=True),
            sa.Column("alias", sa.String(length=128), nullable=True),
            sa.Column("created_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("ip_address", sa.LargeBinary(length=16), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column(
                "status",
                mysql.ENUM("pending", "consumed", "expired", "failed"),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.String(length=512), nullable=True),
            sa.Column(
                "created_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("consumed_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.ForeignKeyConstraint(["provider_app_id"], ["oauth_provider_apps.id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.UniqueConstraint("state", name="uk_tiktok_shop_state"),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_tiktok_shop_session_workspace_status",
            "oauth_tiktok_shop_authz_sessions",
            ["workspace_id", "status"],
        )
        op.create_index(
            "idx_tiktok_shop_session_expires",
            "oauth_tiktok_shop_authz_sessions",
            ["expires_at"],
        )
        op.create_index(
            "idx_tiktok_shop_session_app",
            "oauth_tiktok_shop_authz_sessions",
            ["provider_app_id"],
        )

    if "oauth_tiktok_shop_accounts" not in tables:
        op.create_table(
            "oauth_tiktok_shop_accounts",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("provider_app_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("alias", sa.String(length=128), nullable=True),
            sa.Column("open_id", sa.String(length=192), nullable=False),
            sa.Column("seller_name", sa.String(length=255), nullable=True),
            sa.Column("user_type", sa.Integer(), nullable=True),
            sa.Column("access_token_cipher", sa.LargeBinary(length=4096), nullable=False),
            sa.Column("refresh_token_cipher", sa.LargeBinary(length=4096), nullable=False),
            sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("token_fingerprint", mysql.BINARY(length=32), nullable=False),
            sa.Column("granted_scopes_json", sa.JSON(), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column(
                "status",
                mysql.ENUM("active", "revoked", "invalid", "expired"),
                nullable=False,
                server_default="active",
            ),
            sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("refresh_expires_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("last_synced_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.Column("last_error_message", sa.String(length=512), nullable=True),
            sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
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
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.ForeignKeyConstraint(["provider_app_id"], ["oauth_provider_apps.id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.UniqueConstraint(
                "workspace_id",
                "provider_app_id",
                "open_id",
                name="uk_tiktok_shop_account_open_id",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_tiktok_shop_account_workspace_status",
            "oauth_tiktok_shop_accounts",
            ["workspace_id", "status"],
        )
        op.create_index(
            "idx_tiktok_shop_account_app",
            "oauth_tiktok_shop_accounts",
            ["provider_app_id"],
        )
        op.create_index(
            "idx_tiktok_shop_account_expires",
            "oauth_tiktok_shop_accounts",
            ["expires_at"],
        )

    if "oauth_tiktok_shop_shops" not in tables:
        op.create_table(
            "oauth_tiktok_shop_shops",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("account_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("shop_id", sa.String(length=128), nullable=False),
            sa.Column("shop_cipher", sa.String(length=512), nullable=False),
            sa.Column("shop_name", sa.String(length=255), nullable=True),
            sa.Column("region", sa.String(length=32), nullable=True),
            sa.Column("seller_type", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column(
                "first_seen_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "last_seen_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["oauth_tiktok_shop_accounts.id"],
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("account_id", "shop_id", name="uk_tiktok_shop_account_shop"),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_tiktok_shop_shop_workspace",
            "oauth_tiktok_shop_shops",
            ["workspace_id"],
        )
        op.create_index(
            "idx_tiktok_shop_shop_status",
            "oauth_tiktok_shop_shops",
            ["status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "oauth_tiktok_shop_shops" in tables:
        op.drop_table("oauth_tiktok_shop_shops")
    if "oauth_tiktok_shop_accounts" in tables:
        op.drop_table("oauth_tiktok_shop_accounts")
    if "oauth_tiktok_shop_authz_sessions" in tables:
        op.drop_table("oauth_tiktok_shop_authz_sessions")
    provider_columns = {column["name"] for column in inspector.get_columns("oauth_provider_apps")}
    if "service_id" in provider_columns:
        op.drop_column("oauth_provider_apps", "service_id")
