"""Expand schedule_run status enum for Redis locking."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_expand_schedule_run_status_enum"
down_revision = "0008_ttb_sync_schedule_stats"
branch_labels = None
depends_on = None


_NEW_VALUES = ("enqueued", "running", "success", "failed", "partial")
_OLD_VALUES = ("scheduled", "enqueued", "consumed", "success", "failed", "skipped")


def _update_out_of_range_to_failed() -> None:
    placeholders = ", ".join(f":status_{idx}" for idx, _ in enumerate(_NEW_VALUES))
    params = {f"status_{idx}": value for idx, value in enumerate(_NEW_VALUES)}
    stmt = sa.text(
        f"UPDATE schedule_runs SET status='failed' WHERE status NOT IN ({placeholders})"
    )
    op.execute(stmt, params)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    _update_out_of_range_to_failed()

    if dialect == "mysql":
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

    op.execute(sa.text("UPDATE schedule_runs SET status='enqueued' WHERE status='running'"))
    op.execute(sa.text("UPDATE schedule_runs SET status='failed' WHERE status='partial'"))

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
