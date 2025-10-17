"""db scheduling (catalog/schedules/runs)

Revision ID: 0002_scheduling
Revises: 0001_full_schema
Create Date: 2025-10-16 20:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

# revision identifiers, used by Alembic.
revision = "0002_scheduling"
down_revision = "0001_full_schema"
branch_labels = None
depends_on = None

UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")

schedule_type_enum = sa.Enum("interval", "crontab", "oneoff", name="schedule_type")
run_status_enum = sa.Enum(
    "scheduled", "enqueued", "consumed", "success", "failed", "skipped",
    name="schedule_run_status",
)

def _ts_created():
    return mysql_dialect.TIMESTAMP(fsp=6), sa.text("CURRENT_TIMESTAMP(6)")

def _ts_updated():
    return (
        mysql_dialect.TIMESTAMP(fsp=6),
        sa.text("CURRENT_TIMESTAMP(6)"),
        sa.text("CURRENT_TIMESTAMP(6)"),
    )

def upgrade():
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"

    # 非 MySQL 才需要显式创建命名枚举
    if not is_mysql:
        schedule_type_enum.create(bind, checkfirst=True)
        run_status_enum.create(bind, checkfirst=True)

    col_c_t, col_c_def = _ts_created()
    col_u_t, col_u_def, col_u_onupd = _ts_updated()

    # task_catalog
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

    # schedules
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
        sa.Column("oneoff_run_at", mysql_dialect.DATETIME(fsp=6), nullable=True),

        sa.Column("misfire_grace_s", sa.Integer, nullable=True, server_default=sa.text("300")),
        sa.Column("jitter_s", sa.Integer, nullable=True, server_default=sa.text("0")),

        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("next_fire_at", mysql_dialect.DATETIME(fsp=6), nullable=True),

        sa.Column("created_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_user_id", UBigInt, sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),

        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),

        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_sched_ws_en_next", "schedules", ["workspace_id", "enabled", "next_fire_at"])
    op.create_index("idx_sched_ws_name", "schedules", ["workspace_id", "task_name"])

    # schedule_runs
    op.create_table(
        "schedule_runs",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("schedule_id", UBigInt, sa.ForeignKey("schedules.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UBigInt, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),

        sa.Column("scheduled_for", mysql_dialect.DATETIME(fsp=6), nullable=False),
        sa.Column("enqueued_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
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


def downgrade():
    op.drop_index("idx_runs_status", table_name="schedule_runs")
    op.drop_index("idx_runs_ws_time", table_name="schedule_runs")
    op.drop_index("idx_runs_sched_time", table_name="schedule_runs")
    op.drop_table("schedule_runs")

    op.drop_index("idx_sched_ws_name", table_name="schedules")
    op.drop_index("idx_sched_ws_en_next", table_name="schedules")
    op.drop_table("schedules")

    op.drop_index("idx_catalog_enabled", table_name="task_catalog")
    op.drop_table("task_catalog")

    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        run_status_enum.drop(bind, checkfirst=True)
        schedule_type_enum.drop(bind, checkfirst=True)

