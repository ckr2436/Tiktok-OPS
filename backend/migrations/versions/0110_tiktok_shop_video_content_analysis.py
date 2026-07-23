"""Add isolated Hermes video content analysis state.

Revision ID: 0110_tiktok_shop_video_content_analysis
Revises: 0109_hermes_content_production_plan_audit
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.tiktok_shop import TikTokShopVideoContentAnalysis


revision = "0110_tiktok_shop_video_content_analysis"
down_revision = "0109_hermes_content_production_plan_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    table = TikTokShopVideoContentAnalysis.__table__
    inspector = sa.inspect(bind)
    if table.name not in set(inspector.get_table_names()):
        table.create(bind=bind, checkfirst=True)
        return
    actual = {str(item["name"]) for item in inspector.get_columns(table.name)}
    missing = sorted({str(column.name) for column in table.columns} - actual)
    if missing:
        raise RuntimeError(f"{table.name} exists with missing columns: {missing}")


def downgrade() -> None:
    bind = op.get_bind()
    table = TikTokShopVideoContentAnalysis.__table__
    if table.name in set(sa.inspect(bind).get_table_names()):
        table.drop(bind=bind, checkfirst=True)
