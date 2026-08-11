"""Add TikTok Shop flash-sale automation policy and run tables.

Revision ID: 0108_tiktok_shop_flash_sale_automation
Revises: 0107_hermes_content_series_slate
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopFlashSaleRun,
)


revision = "0108_tiktok_shop_flash_sale_automation"
down_revision = "0107_hermes_content_series_slate"
branch_labels = None
depends_on = None

TABLES = (
    TikTokShopFlashSalePolicy.__table__,
    TikTokShopFlashSaleRun.__table__,
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(_inspector().get_table_names())
    for table in TABLES:
        if table.name not in existing:
            table.create(bind=bind, checkfirst=True)
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
    for table in reversed(TABLES):
        if table.name in existing:
            table.drop(bind=bind, checkfirst=True)
