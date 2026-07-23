"""Add immutable Hermes content-director audit tables.

Revision ID: 0106_hermes_content_director_audit
Revises: 0105_tiktok_shop_business_data
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import (
    HermesContentDirectorArtifact,
    HermesContentDirectorAttempt,
    HermesContentDirectorBrief,
    HermesContentDirectorReview,
)


revision = "0106_hermes_content_director_audit"
down_revision = "0105_tiktok_shop_business_data"
branch_labels = None
depends_on = None


MODELS = (
    HermesContentDirectorBrief,
    HermesContentDirectorArtifact,
    HermesContentDirectorAttempt,
    HermesContentDirectorReview,
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(_inspector().get_table_names())
    for model in MODELS:
        table = model.__table__
        if table.name not in existing:
            table.create(bind=bind, checkfirst=True)
            existing.add(table.name)
            continue
        actual = {
            str(item["name"])
            for item in _inspector().get_columns(table.name)
        }
        expected = {str(column.name) for column in table.columns}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"{table.name} exists with an incomplete schema; missing columns: "
                + ", ".join(missing)
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(_inspector().get_table_names())
    for model in reversed(MODELS):
        table = model.__table__
        if table.name in existing:
            table.drop(bind=bind, checkfirst=True)
            existing.discard(table.name)
