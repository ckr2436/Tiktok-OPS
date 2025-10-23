"""full schema (merged 0001..0004) with mysql-safe timestamps

Revision ID: 0001_full_schema
Revises:
Create Date: 2025-10-23 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

# ---- Alembic identifiers ----
revision = "0001_full_schema"
down_revision = None
branch_labels = None
depends_on = None

# ---- Common types & enums ----
UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")

user_role = sa.Enum("owner", "admin", "member", name="user_role")
oauth_session_status = sa.Enum("pending", "consumed", "expired", "failed", name="oauth_session_status")
oauth_account_status = sa.Enum("active", "revoked", "invalid", name="oauth_account_status")

schedule_type_enum = sa.Enum("interval", "crontab", "oneoff", name="schedule_type")
run_status_enum = sa.Enum("scheduled", "enqueued", "consumed", "success", "failed", "skipped", name="schedule_run_status")

def _ts_created():
    # TIMESTAMP(6) default CURRENT_TIMESTAMP(6)
    return mysql_dialect.TIMESTAMP(fsp=6), sa.text("CURRENT_TIMESTAMP(6)")

def _ts_updated():
    # TIMESTAMP(6) default CURRENT + ON UPDATE CURRENT
    return (
        mysql_dialect.TIMESTAMP(fsp=6),
        sa.text("CURRENT_TIMESTAMP(6)"),
        sa.text("CURRENT_TIMESTAMP(6)"),
    )

def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"

    # Non-MySQL must register named enums explicitly
    if not is_mysql:
        user_role.create(bind, checkfirst=True)
        oauth_session_status.create(bind, checkfirst=True)
        oauth_account_status.create(bind, checkfirst=True)
        schedule_type_enum.create(bind, checkfirst=True)
        run_status_enum.create(bind, checkfirst=True)

    col_c_t, col_c_def = _ts_created()
    col_u_t, col_u_def, col_u_onupd = _ts_updated()

    # ===================== workspaces =====================
    op.create_table(
        "workspaces",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("company_code", sa.String(4), nullable=False),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        sa.UniqueConstraint("company_code", name="uq_workspaces_company_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ===================== users =====================
    op.create_table(
        "users",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("is_platform_admin", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("role", user_role if not is_mysql else sa.Enum("owner", "admin", "member", name="user_role"), nullable=False),
        sa.Column("usercode", sa.String(9), nullable=False, unique=True),
        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_login_at", _dt6(), nullable=True),
        sa.Column("deleted_at", _dt6(), nullable=True),
        sa.Column(
            "active_until",
            _dt6(),
            sa.Computed("COALESCE(`deleted_at`, CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)))", persisted=True),
            nullable=True,
        ),
        sa.UniqueConstraint("workspace_id", "username", "active_until", name="uq_users_ws_username_active"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_users_workspace_id", "users", ["workspace_id"])
    op.create_index("ix_users_created_by_user_id", "users", ["created_by_user_id"])

    # ===================== audit_logs =====================
    op.create_table(
        "audit_logs",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("event_time", mysql_dialect.TIMESTAMP(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("actor_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", UBigInt, nullable=True),
        sa.Column("target_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("details", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_audit_time", "audit_logs", ["event_time"])
    op.create_index("idx_audit_action", "audit_logs", ["action"])
    op.create_index("idx_audit_workspace", "audit_logs", ["workspace_id"])

    # ===================== crypto_keyrings =====================
    op.create_table(
        "crypto_keyrings",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, unique=True),
        sa.Column("key_alias", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("rotated_at", _dt6(), nullable=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ===================== oauth_provider_apps =====================
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
        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        sa.UniqueConstraint("provider", "client_id", name="uk_provider_clientid"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_provider_enabled", "oauth_provider_apps", ["provider", "is_enabled"])
    op.create_index("idx_name", "oauth_provider_apps", ["name"])
    op.create_index("fk_opa_keyring", "oauth_provider_apps", ["client_secret_key_version"])

    # ===================== oauth_provider_app_redirects =====================
    op.create_table(
        "oauth_provider_app_redirects",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("provider_app_id", UBigInt, sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.UniqueConstraint("provider_app_id", "redirect_uri", name="uk_app_redirect"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ===================== oauth_authz_sessions =====================
    op.create_table(
        "oauth_authz_sessions",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("state", sa.String(36), nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_app_id", UBigInt, sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("return_to", sa.String(512), nullable=True),
        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("ip_address", sa.LargeBinary(16), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("status", oauth_session_status if not is_mysql else sa.Enum("pending", "consumed", "expired", "failed", name="oauth_session_status"), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("expires_at", _dt6(), nullable=False),
        sa.Column("consumed_at", _dt6(), nullable=True),
        sa.Column("alias", sa.String(128), nullable=True),
        sa.UniqueConstraint("state", name="uk_state"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_wid_status", "oauth_authz_sessions", ["workspace_id", "status"])
    op.create_index("idx_expires_at", "oauth_authz_sessions", ["expires_at"])
    op.create_index("idx_provider_app", "oauth_authz_sessions", ["provider_app_id"])

    # ===================== oauth_accounts_ttb =====================
    op.create_table(
        "oauth_accounts_ttb",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_app_id", UBigInt, sa.ForeignKey("oauth_provider_apps.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(128), nullable=True),
        sa.Column("access_token_cipher", sa.LargeBinary(4096), nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("token_fingerprint", mysql_dialect.BINARY(32), nullable=False),  # for unique index
        sa.Column("scope_json", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        sa.Column("status", oauth_account_status if not is_mysql else sa.Enum("active", "revoked", "invalid", name="oauth_account_status"), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("revoked_at", _dt6(), nullable=True),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        sa.UniqueConstraint("workspace_id", "provider_app_id", "token_fingerprint", name="uk_wid_app_fp"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_wid_status", "oauth_accounts_ttb", ["workspace_id", "status"])
    op.create_index("idx_app", "oauth_accounts_ttb", ["provider_app_id"])
    op.create_index("idx_created_at", "oauth_accounts_ttb", ["created_at"])

    # ===================== Scheduling (task_catalog / schedules / schedule_runs) =====================
    op.create_table(
        "task_catalog",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("task_name", sa.String(128), nullable=False, unique=True),
        sa.Column("impl_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("input_schema_json", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        sa.Column("default_queue", sa.String(64), nullable=True),
        sa.Column("rate_limit", sa.String(32), nullable=True),
        sa.Column("timeout_s", sa.Integer, nullable=True),
        sa.Column("max_retries", sa.Integer, nullable=True),
        sa.Column("visibility", sa.String(16), nullable=True, server_default=sa.text("'tenant'")),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        sa.UniqueConstraint("task_name", name="uq_task_catalog_name"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_catalog_enabled", "task_catalog", ["is_enabled"])

    op.create_table(
        "schedules",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("task_name", sa.String(128), sa.ForeignKey("task_catalog.task_name", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False),
        sa.Column("schedule_type", schedule_type_enum if not is_mysql else sa.Enum("interval", "crontab", "oneoff", name="schedule_type"), nullable=False),
        sa.Column("params_json", sa.JSON().with_variant(mysql_dialect.JSON(), "mysql"), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True, server_default=sa.text("'UTC'")),
        sa.Column("interval_seconds", sa.Integer, nullable=True),
        sa.Column("crontab_expr", sa.String(64), nullable=True),
        sa.Column("oneoff_run_at", _dt6(), nullable=True),
        sa.Column("misfire_grace_s", sa.Integer, nullable=True, server_default=sa.text("300")),
        sa.Column("jitter_s", sa.Integer, nullable=True, server_default=sa.text("0")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("next_fire_at", _dt6(), nullable=True),
        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_sched_ws_en_next", "schedules", ["workspace_id", "enabled", "next_fire_at"])
    op.create_index("idx_sched_ws_name", "schedules", ["workspace_id", "task_name"])

    op.create_table(
        "schedule_runs",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("schedule_id", UBigInt, sa.ForeignKey("schedules.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", _dt6(), nullable=False),
        sa.Column("enqueued_at", _dt6(), nullable=True),
        sa.Column("broker_msg_id", sa.String(64), nullable=True),
        sa.Column("status", run_status_enum if not is_mysql else sa.Enum("scheduled", "enqueued", "consumed", "success", "failed", "skipped", name="schedule_run_status"), nullable=False, server_default=sa.text("'scheduled'")),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_runs_sched_time", "schedule_runs", ["schedule_id", "scheduled_for"])
    op.create_index("idx_runs_ws_time", "schedule_runs", ["workspace_id", "scheduled_for"])
    op.create_index("idx_runs_status", "schedule_runs", ["status"])

    # ===================== TTB core entities (sync_cursors / BC / advertisers / shops / products) =====================
    op.create_table(
        "ttb_sync_cursors",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default=sa.text("'tiktok-business'")),
        sa.Column("auth_id", UBigInt, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("cursor_token", sa.String(256), nullable=True),
        sa.Column("since_time", _dt6(), nullable=True),
        sa.Column("until_time", _dt6(), nullable=True),
        sa.Column("last_rev", sa.String(64), nullable=True),
        sa.Column("extra_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_cursor_scope", "ttb_sync_cursors", ["workspace_id", "provider", "auth_id", "resource_type"])
    op.create_index("idx_ttb_cursor_scope", "ttb_sync_cursors", ["workspace_id", "auth_id", "resource_type"])

    op.create_table(
        "ttb_business_centers",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_id", UBigInt, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("bc_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("country_code", sa.String(8), nullable=True),
        sa.Column("owner_user_id", sa.String(64), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_bc_scope", "ttb_business_centers", ["workspace_id", "auth_id", "bc_id"])
    op.create_index("idx_ttb_bc_scope", "ttb_business_centers", ["workspace_id", "auth_id", "bc_id"])
    op.create_index("idx_ttb_bc_updated", "ttb_business_centers", ["ext_updated_time"])

    op.create_table(
        "ttb_advertisers",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_id", UBigInt, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("advertiser_id", sa.String(64), nullable=False),
        sa.Column("bc_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("industry", sa.String(64), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("country_code", sa.String(8), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_adv_scope", "ttb_advertisers", ["workspace_id", "auth_id", "advertiser_id"])
    op.create_index("idx_ttb_adv_scope", "ttb_advertisers", ["workspace_id", "auth_id", "advertiser_id"])
    op.create_index("idx_ttb_adv_bc", "ttb_advertisers", ["bc_id"])
    op.create_index("idx_ttb_adv_updated", "ttb_advertisers", ["ext_updated_time"])
    op.create_index("idx_ttb_adv_status", "ttb_advertisers", ["status"])

    op.create_table(
        "ttb_shops",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_id", UBigInt, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("advertiser_id", sa.String(64), nullable=True),
        sa.Column("bc_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("region_code", sa.String(8), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_shop_scope", "ttb_shops", ["workspace_id", "auth_id", "shop_id"])
    op.create_index("idx_ttb_shop_scope", "ttb_shops", ["workspace_id", "auth_id", "shop_id"])
    op.create_index("idx_ttb_shop_adv", "ttb_shops", ["advertiser_id"])
    op.create_index("idx_ttb_shop_updated", "ttb_shops", ["ext_updated_time"])
    op.create_index("idx_ttb_shop_status", "ttb_shops", ["status"])

    op.create_table(
        "ttb_products",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_id", UBigInt, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("price", sa.Numeric(18, 4), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("sync_rev", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("last_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_product_scope", "ttb_products", ["workspace_id", "auth_id", "product_id"])
    op.create_index("idx_ttb_product_scope", "ttb_products", ["workspace_id", "auth_id", "product_id"])
    op.create_index("idx_ttb_product_shop", "ttb_products", ["shop_id"])
    op.create_index("idx_ttb_product_updated", "ttb_products", ["ext_updated_time"])
    op.create_index("idx_ttb_product_status", "ttb_products", ["status"])


def downgrade() -> None:
    # Drop child tables first; wrap with FK checks off to be safe on MySQL
    op.execute("SET FOREIGN_KEY_CHECKS=0")

    # TTB entities
    op.execute("DROP TABLE IF EXISTS ttb_products")
    op.execute("DROP TABLE IF EXISTS ttb_shops")
    op.execute("DROP TABLE IF EXISTS ttb_advertisers")
    op.execute("DROP TABLE IF EXISTS ttb_business_centers")
    op.execute("DROP TABLE IF EXISTS ttb_sync_cursors")

    # Scheduling
    op.drop_index("idx_runs_status", table_name="schedule_runs")
    op.drop_index("idx_runs_ws_time", table_name="schedule_runs")
    op.drop_index("idx_runs_sched_time", table_name="schedule_runs")
    op.drop_table("schedule_runs")

    op.drop_index("idx_sched_ws_name", table_name="schedules")
    op.drop_index("idx_sched_ws_en_next", table_name="schedules")
    op.drop_table("schedules")

    op.drop_index("idx_catalog_enabled", table_name="task_catalog")
    op.drop_table("task_catalog")

    # OAuth tables
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

    # Misc
    op.drop_table("crypto_keyrings")

    op.drop_index("idx_audit_workspace", table_name="audit_logs")
    op.drop_index("idx_audit_action", table_name="audit_logs")
    op.drop_index("idx_audit_time", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_users_created_by_user_id", table_name="users")
    op.drop_index("ix_users_workspace_id", table_name="users")
    op.drop_table("users")

    op.drop_table("workspaces")

    op.execute("SET FOREIGN_KEY_CHECKS=1")

    # Drop named enums (non-MySQL only)
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        run_status_enum.drop(bind, checkfirst=True)
        schedule_type_enum.drop(bind, checkfirst=True)
        oauth_account_status.drop(bind, checkfirst=True)
        oauth_session_status.drop(bind, checkfirst=True)
        user_role.drop(bind, checkfirst=True)
