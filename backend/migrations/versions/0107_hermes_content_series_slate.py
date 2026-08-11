"""Add immutable Hermes content series-slate audit table.

Revision ID: 0107_hermes_content_series_slate
Revises: 0107_remove_legacy_shop_order_imports
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import HermesContentSeriesSlate


revision = "0107_hermes_content_series_slate"
down_revision = "0107_remove_legacy_shop_order_imports"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    table = HermesContentSeriesSlate.__table__
    existing = set(_inspector().get_table_names())
    if table.name not in existing:
        table.create(bind=bind, checkfirst=True)
        return
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
    table = HermesContentSeriesSlate.__table__
    if table.name in set(_inspector().get_table_names()):
        table.drop(bind=bind, checkfirst=True)
