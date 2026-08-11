"""Remove legacy GMV Max CSV order-import storage.

Revision ID: 0107_remove_legacy_shop_order_imports
Revises: 0106_commerce_profitability
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0107_remove_legacy_shop_order_imports"
down_revision = "0106_commerce_profitability"
branch_labels = None
depends_on = None


LEGACY_TABLES = (
    "ttb_shop_order_import_batches",
    "ttb_shop_orders",
    "ttb_shop_order_items",
)


def _missing_api_orders() -> int:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "ttb_shop_orders" not in tables:
        return 0
    required = {"oauth_tiktok_shop_shops", "tiktok_shop_orders"}
    missing_tables = required - tables
    if missing_tables:
        raise RuntimeError(
            "Cannot remove legacy order imports before Shop API storage exists: "
            + ", ".join(sorted(missing_tables))
        )
    binary = "binary " if bind.dialect.name == "mysql" else ""
    count = bind.execute(
        sa.text(
            f"""
            select count(*)
            from ttb_shop_orders legacy
            left join oauth_tiktok_shop_shops shop
              on shop.workspace_id=legacy.workspace_id
             and {binary}shop.shop_id={binary}legacy.store_id
            left join tiktok_shop_orders api_order
              on api_order.workspace_id=legacy.workspace_id
             and api_order.shop_row_id=shop.id
             and {binary}api_order.order_id={binary}legacy.external_order_id
            where api_order.id is null
            """
        )
    ).scalar_one()
    return int(count or 0)


def upgrade() -> None:
    missing_orders = _missing_api_orders()
    if missing_orders:
        raise RuntimeError(
            "Legacy Shop order removal blocked: "
            f"{missing_orders} order(s) are not present in TikTok Shop API storage."
        )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in (
        "ttb_shop_order_items",
        "ttb_shop_orders",
        "ttb_shop_order_import_batches",
    ):
        if table_name in tables:
            op.drop_table(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "This migration is intentionally irreversible. Restore the pre-deploy "
        "legacy-order SQL backup before running an older application release."
    )
