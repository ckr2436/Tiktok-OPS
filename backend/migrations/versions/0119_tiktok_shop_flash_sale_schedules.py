"""add configurable TikTok Shop flash-sale schedules

Revision ID: 0119_tiktok_shop_flash_sale_schedules
Revises: 0118_product_library_remove_commercial_references
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.tiktok_shop import TikTokShopFlashSaleSchedule


revision = "0119_tiktok_shop_flash_sale_schedules"
down_revision = "0118_product_library_remove_commercial_references"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    schedule_table = TikTokShopFlashSaleSchedule.__table__
    if schedule_table.name not in tables:
        schedule_table.create(bind=bind, checkfirst=True)

    run_columns = {
        str(item["name"])
        for item in _inspector().get_columns("tiktok_shop_flash_sale_runs")
    }
    if "new_activity_ids_json" not in run_columns:
        op.add_column(
            "tiktok_shop_flash_sale_runs",
            sa.Column("new_activity_ids_json", sa.JSON(), nullable=True),
        )

    missing_schedules = bind.execute(
        sa.text(
            "SELECT MIN(p.workspace_id) AS workspace_id, "
            "MIN(p.account_id) AS account_id, p.shop_row_id AS shop_row_id "
            "FROM tiktok_shop_flash_sale_policies p "
            "LEFT JOIN tiktok_shop_flash_sale_schedules s "
            "ON s.shop_row_id = p.shop_row_id "
            "WHERE s.id IS NULL GROUP BY p.shop_row_id"
        )
    ).mappings()
    for row in missing_schedules:
        bind.execute(
            schedule_table.insert().values(
                workspace_id=int(row["workspace_id"]),
                account_id=int(row["account_id"]),
                shop_row_id=int(row["shop_row_id"]),
                activity_duration_minutes=4320,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(_inspector().get_table_names())
    if "tiktok_shop_flash_sale_runs" in tables:
        run_columns = {
            str(item["name"])
            for item in _inspector().get_columns("tiktok_shop_flash_sale_runs")
        }
        if "new_activity_ids_json" in run_columns:
            op.drop_column("tiktok_shop_flash_sale_runs", "new_activity_ids_json")
    if TikTokShopFlashSaleSchedule.__tablename__ in tables:
        TikTokShopFlashSaleSchedule.__table__.drop(bind=bind, checkfirst=True)
