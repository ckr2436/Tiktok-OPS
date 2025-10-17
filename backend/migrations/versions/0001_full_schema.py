"""full schema with mysql-safe timestamps

Revision ID: 0001_full_schema
Revises:
Create Date: 2025-10-16 18:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

# revision identifiers, used by Alembic.
revision = "0001_full_schema"
down_revision = None
branch_labels = None
depends_on = None

# ---- 通用 BigInt，MySQL 使用无符号 BIGINT ----
UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")

# ---- 枚举（与你模型一致的名字）----
user_role = sa.Enum("owner", "admin", "member", name="user_role")
oauth_session_status = sa.Enum("pending", "consumed", "expired", "failed", name="oauth_session_status")
oauth_account_status = sa.Enum("active", "revoked", "invalid", name="oauth_account_status")


def _ts_created():
    """TIMESTAMP(6) for created_at, default current"""
    return mysql_dialect.TIMESTAMP(fsp=6), sa.text("CURRENT_TIMESTAMP(6)")


def _ts_updated():
    """TIMESTAMP(6) for updated_at, default current + on update current"""
    return (
        mysql_dialect.TIMESTAMP(fsp=6),
        sa.text("CURRENT_TIMESTAMP(6)"),
        sa.text("CURRENT_TIMESTAMP(6)"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"

    # 非 MySQL（例如 PostgreSQL）需要先注册枚举类型
    if not is_mysql:
        user_role.create(bind, checkfirst=True)
        oauth_session_status.create(bind, checkfirst=True)
        oauth_account_status.create(bind, checkfirst=True)

    col_type_c, col_def_c = _ts_created()
    col_type_u, col_def_u, col_onupd = _ts_updated()

    # ================ workspaces =================
    op.create_table(
        "workspaces",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("company_code", sa.String(4), nullable=False),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.Column(
            "updated_at",
            col_type_u,
            nullable=False,
            server_default=col_def_u,
            server_onupdate=col_onupd,
        ),
        sa.UniqueConstraint("company_code", name="uq_workspaces_company_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ================ users =================
    op.create_table(
        "users",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("is_platform_admin", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("role", user_role if not is_mysql else sa.Enum("owner", "admin", "member", name="user_role"), nullable=False),
        sa.Column("usercode", sa.String(9), nullable=False, unique=True),
        sa.Column(
            "created_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.Column(
            "updated_at",
            col_type_u,
            nullable=False,
            server_default=col_def_u,
            server_onupdate=col_onupd,
        ),
        sa.Column("deleted_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "active_until",
            mysql_dialect.DATETIME(fsp=6),
            sa.Computed(
                "COALESCE(`deleted_at`, TIMESTAMP('9999-12-31 23:59:59.999999'))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.UniqueConstraint("workspace_id", "username", "active_until", name="uq_users_ws_username_active"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_users_workspace_id", "users", ["workspace_id"])
    op.create_index("ix_users_created_by_user_id", "users", ["created_by_user_id"])

    # ================ audit_logs =================
    op.create_table(
        "audit_logs",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("event_time", mysql_dialect.TIMESTAMP(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "actor_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", UBigInt, nullable=True),
        sa.Column(
            "target_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("details", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_audit_time", "audit_logs", ["event_time"])
    op.create_index("idx_audit_action", "audit_logs", ["action"])
    op.create_index("idx_audit_workspace", "audit_logs", ["workspace_id"])

    # ================ crypto_keyrings =================
    op.create_table(
        "crypto_keyrings",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, unique=True),
        sa.Column("key_alias", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("rotated_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ================ oauth_provider_apps =================
    op.create_table(
        "oauth_provider_apps",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("client_secret_cipher", sa.LargeBinary(4096), nullable=False),
        sa.Column("client_secret_key_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.Column(
            "updated_at",
            col_type_u,
            nullable=False,
            server_default=col_def_u,
            server_onupdate=col_onupd,
        ),
        sa.UniqueConstraint("provider", "client_id", name="uk_provider_clientid"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_provider_enabled", "oauth_provider_apps", ["provider", "is_enabled"])
    op.create_index("idx_name", "oauth_provider_apps", ["name"])
    op.create_index("fk_opa_keyring", "oauth_provider_apps", ["client_secret_key_version"])

    # ================ oauth_provider_app_redirects =================
    op.create_table(
        "oauth_provider_app_redirects",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "provider_app_id",
            UBigInt,
            sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.UniqueConstraint("provider_app_id", "redirect_uri", name="uk_app_redirect"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ================ oauth_authz_sessions =================
    op.create_table(
        "oauth_authz_sessions",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("state", sa.String(36), nullable=False),
        sa.Column(
            "workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_app_id",
            UBigInt,
            sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("return_to", sa.String(512), nullable=True),
        sa.Column(
            "created_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ip_address", sa.LargeBinary(16), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("status", oauth_session_status if not is_mysql else sa.Enum("pending", "consumed", "expired", "failed", name="oauth_session_status"), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.Column("expires_at", mysql_dialect.DATETIME(fsp=6), nullable=False),
        sa.Column("consumed_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column("alias", sa.String(128), nullable=True),
        sa.UniqueConstraint("state", name="uk_state"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_wid_status", "oauth_authz_sessions", ["workspace_id", "status"])
    op.create_index("idx_expires_at", "oauth_authz_sessions", ["expires_at"])
    op.create_index("idx_provider_app", "oauth_authz_sessions", ["provider_app_id"])

    # ================ oauth_accounts_ttb =================
    op.create_table(
        "oauth_accounts_ttb",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_app_id",
            UBigInt,
            sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(128), nullable=True),
        sa.Column("access_token_cipher", sa.LargeBinary(4096), nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        # ★★ 关键修正：用 BINARY(32) 以支持唯一键索引
        sa.Column("token_fingerprint", mysql_dialect.BINARY(32), nullable=False),
        sa.Column("scope_json", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        sa.Column("status", oauth_account_status if not is_mysql else sa.Enum("active", "revoked", "invalid", name="oauth_account_status"), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", col_type_c, nullable=False, server_default=col_def_c),
        sa.Column("revoked_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "updated_at",
            col_type_u,
            nullable=False,
            server_default=col_def_u,
            server_onupdate=col_onupd,
        ),
        sa.UniqueConstraint("workspace_id", "provider_app_id", "token_fingerprint", name="uk_wid_app_fp"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_wid_status", "oauth_accounts_ttb", ["workspace_id", "status"])
    op.create_index("idx_app", "oauth_accounts_ttb", ["provider_app_id"])
    op.create_index("idx_created_at", "oauth_accounts_ttb", ["created_at"])


def downgrade() -> None:
    # 逆序删除以避免外键约束问题
    op.drop_index("idx_created_at", table_name="oauth_accounts_ttb")
    op.drop_index("idx_app", table_name="oauth_accounts_ttb")
    op.drop_index("idx_wid_status", table_name="oauth_accounts_ttb")
    op.drop_table("oauth_accounts_ttb")

    op.drop_index("idx_provider_app", table_name="oauth_authz_sessions")
    op.drop_index("idx_expires_at", table_name="oauth_authz_sessions")
    op.drop_index("idx_wid_status", table_name="oauth_authz_sessions")
    op.drop_table("oauth_authz_sessions")

    op.drop_table("oauth_provider_app_redirects")

    op.drop_index("fk_opa_keyring", table_name="oauth_provider_apps")
    op.drop_index("idx_name", table_name="oauth_provider_apps")
    op.drop_index("idx_provider_enabled", table_name="oauth_provider_apps")
    op.drop_table("oauth_provider_apps")

    op.drop_table("crypto_keyrings")

    op.drop_index("idx_audit_workspace", table_name="audit_logs")
    op.drop_index("idx_audit_action", table_name="audit_logs")
    op.drop_index("idx_audit_time", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_users_created_by_user_id", table_name="users")
    op.drop_index("ix_users_workspace_id", table_name="users")
    op.drop_table("users")

    op.drop_table("workspaces")

    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        # 仅在非 MySQL 下回收命名枚举类型
        oauth_account_status.drop(bind, checkfirst=True)
        oauth_session_status.drop(bind, checkfirst=True)
        user_role.drop(bind, checkfirst=True)

