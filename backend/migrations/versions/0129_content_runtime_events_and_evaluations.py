"""Add Content Factory runtime outbox and independent evaluations.

Revision ID: 0129_content_runtime_events
Revises: 0128_hermes_run_result_mediumtext
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import (
    HermesContentEvaluation,
    HermesContentRuntimeEvent,
)


revision = "0129_content_runtime_events"
down_revision = "0128_hermes_run_result_mediumtext"
branch_labels = None
depends_on = None


TABLES = (
    HermesContentEvaluation.__table__,
    HermesContentRuntimeEvent.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in TABLES:
        if table.name not in existing:
            table.create(bind=bind, checkfirst=True)
            existing.add(table.name)
            continue
        actual = {
            str(item["name"])
            for item in sa.inspect(bind).get_columns(table.name)
        }
        expected = {str(column.name) for column in table.columns}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"{table.name} exists with missing columns: {missing}"
            )
def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in reversed(TABLES):
        if table.name in existing:
            table.drop(bind=bind, checkfirst=True)
