"""Add durable Hermes content execution, segment, and deliverable ledgers.

Revision ID: 0113_hermes_content_execution
Revises: 0112_ai_provider_routing
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import (
    HermesContentDeliverable,
    HermesContentExecution,
    HermesContentSegmentRun,
    HermesContentVariantRun,
)


revision = "0113_hermes_content_execution"
down_revision = "0112_ai_provider_routing"
branch_labels = None
depends_on = None


TABLES = (
    HermesContentExecution.__table__,
    HermesContentVariantRun.__table__,
    HermesContentSegmentRun.__table__,
    HermesContentDeliverable.__table__,
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
