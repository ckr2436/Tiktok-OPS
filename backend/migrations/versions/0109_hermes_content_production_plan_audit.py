"""Add immutable Hermes production-plan audit storage.

Revision ID: 0109_hermes_content_production_plan_audit
Revises: 0108_tiktok_shop_flash_sale_automation
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import HermesContentProductionPlanAudit


revision = "0109_hermes_content_production_plan_audit"
down_revision = "0108_tiktok_shop_flash_sale_automation"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    table = HermesContentProductionPlanAudit.__table__
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
            f"{table.name} exists with missing columns: {missing}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    table = HermesContentProductionPlanAudit.__table__
    if table.name in set(_inspector().get_table_names()):
        table.drop(bind=bind, checkfirst=True)
