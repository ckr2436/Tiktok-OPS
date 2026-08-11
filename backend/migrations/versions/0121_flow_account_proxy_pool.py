"""add platform-managed Flow account proxy pool

Revision ID: 0121_flow_account_proxy_pool
Revises: 0120_gmvmax_external_enable
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.flow_account_proxy import FlowAccountProxy


revision = "0121_flow_account_proxy_pool"
down_revision = "0120_gmvmax_external_enable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    FlowAccountProxy.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if FlowAccountProxy.__tablename__ in set(inspector.get_table_names()):
        FlowAccountProxy.__table__.drop(bind=op.get_bind(), checkfirst=True)
