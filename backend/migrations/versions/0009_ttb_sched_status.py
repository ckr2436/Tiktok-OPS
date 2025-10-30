"""Expand schedule_run status enum for Redis locking."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_ttb_sched_status"
down_revision = "0008_ttb_sync_schedule_stats"
branch_labels = None
depends_on = None

# 新旧取值集合
_NEW_VALUES = ("enqueued", "running", "success", "failed", "partial")
_OLD_VALUES = ("scheduled", "enqueued", "consumed", "success", "failed", "skipped")


def _normalize_status_values() -> None:
    """
    迁移前清洗历史取值，避免直接修改 ENUM 时触发数据截断：
    - scheduled -> enqueued
    - consumed  -> running
    - skipped   -> failed
    - 其它任何不在新集合内的值 -> failed（兜底）
    """
    conn = op.get_bind()

    # 显式映射三种历史值
    conn.execute(sa.text("UPDATE schedule_runs SET status='enqueued' WHERE status='scheduled'"))
    conn.execute(sa.text("UPDATE schedule_runs SET status='running'  WHERE status='consumed'"))
    conn.execute(sa.text("UPDATE schedule_runs SET status='failed'   WHERE status='skipped'"))

    # 兜底：把仍不在新集合内的值置为 failed
    stmt = sa.text(
        "UPDATE schedule_runs "
        "SET status='failed' "
        "WHERE status NOT IN (:v0, :v1, :v2, :v3, :v4)"
    )
    params = {
        "v0": "enqueued",
        "v1": "running",
        "v2": "success",
        "v3": "failed",
        "v4": "partial",
    }
    conn.execute(stmt, params)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    _normalize_status_values()

    if dialect == "mysql":
        # MySQL 下修改 ENUM 定义
        enum_values = ", ".join(f"'{value}'" for value in _NEW_VALUES)
        op.execute(
            f"ALTER TABLE schedule_runs MODIFY COLUMN status "
            f"ENUM({enum_values}) NOT NULL DEFAULT 'enqueued'"
        )
    else:
        old_enum = sa.Enum(*_OLD_VALUES, name="schedule_run_status", create_type=False)
        new_enum = sa.Enum(*_NEW_VALUES, name="schedule_run_status", create_type=False)
        with op.batch_alter_table("schedule_runs") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=old_enum,
                type_=new_enum,
                existing_nullable=False,
                nullable=False,
                existing_server_default=sa.text("'scheduled'"),
                server_default=sa.text("'enqueued'"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 把新集合里旧集合不存在的值做回退映射
    op.execute(sa.text("UPDATE schedule_runs SET status='enqueued' WHERE status='running'"))
    op.execute(sa.text("UPDATE schedule_runs SET status='failed'   WHERE status='partial'"))

    if dialect == "mysql":
        enum_values = ", ".join(f"'{value}'" for value in _OLD_VALUES)
        op.execute(
            f"ALTER TABLE schedule_runs MODIFY COLUMN status "
            f"ENUM({enum_values}) NOT NULL DEFAULT 'scheduled'"
        )
    else:
        new_enum = sa.Enum(*_OLD_VALUES, name="schedule_run_status", create_type=False)
        current_enum = sa.Enum(*_NEW_VALUES, name="schedule_run_status", create_type=False)
        with op.batch_alter_table("schedule_runs") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=current_enum,
                type_=new_enum,
                existing_nullable=False,
                nullable=False,
                existing_server_default=sa.text("'enqueued'"),
                server_default=sa.text("'scheduled'"),
            )

