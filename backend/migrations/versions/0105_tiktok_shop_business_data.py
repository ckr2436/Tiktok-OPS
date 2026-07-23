"""Add normalized TikTok Shop business data.

Revision ID: 0105_tiktok_shop_business_data
Revises: 0104_gmvmax_campaign_pause_intents
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.tiktok_shop import (
    TikTokShopCategory,
    TikTokShopCoupon,
    TikTokShopFinanceStatement,
    TikTokShopFinanceTransaction,
    TikTokShopGlobalProduct,
    TikTokShopLiveDailyMetric,
    TikTokShopOrder,
    TikTokShopOrderFinanceSummary,
    TikTokShopOrderLine,
    TikTokShopPayment,
    TikTokShopProduct,
    TikTokShopProductChannelDailyMetric,
    TikTokShopProductDailyMetric,
    TikTokShopPromotionActivity,
    TikTokShopShopHourlyMetric,
    TikTokShopSku,
    TikTokShopSkuDailyMetric,
    TikTokShopSyncRun,
    TikTokShopUnsettledTransaction,
    TikTokShopVideoDailyMetric,
    TikTokShopVideoOverviewDailyMetric,
    TikTokShopWithdrawal,
)


revision = "0105_tiktok_shop_business_data"
down_revision = "0104_gmvmax_campaign_pause_intents"
branch_labels = None
depends_on = None


MODELS = (
    TikTokShopSyncRun,
    TikTokShopCategory,
    TikTokShopProduct,
    TikTokShopSku,
    TikTokShopGlobalProduct,
    TikTokShopOrder,
    TikTokShopOrderLine,
    TikTokShopOrderFinanceSummary,
    TikTokShopFinanceStatement,
    TikTokShopFinanceTransaction,
    TikTokShopWithdrawal,
    TikTokShopPayment,
    TikTokShopUnsettledTransaction,
    TikTokShopPromotionActivity,
    TikTokShopCoupon,
    TikTokShopShopHourlyMetric,
    TikTokShopVideoOverviewDailyMetric,
    TikTokShopVideoDailyMetric,
    TikTokShopProductDailyMetric,
    TikTokShopProductChannelDailyMetric,
    TikTokShopSkuDailyMetric,
    TikTokShopLiveDailyMetric,
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _add_shop_columns() -> None:
    columns = {
        str(item["name"])
        for item in _inspector().get_columns("oauth_tiktok_shop_shops")
    }
    if "shop_code" not in columns:
        op.add_column(
            "oauth_tiktok_shop_shops",
            sa.Column("shop_code", sa.String(length=128), nullable=True),
        )
    if "timezone_name" not in columns:
        op.add_column(
            "oauth_tiktok_shop_shops",
            sa.Column(
                "timezone_name",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'America/New_York'"),
            ),
        )


def _create_or_validate_tables() -> None:
    bind = op.get_bind()
    existing = set(_inspector().get_table_names())
    for model in MODELS:
        table = model.__table__
        if table.name not in existing:
            table.create(bind=bind, checkfirst=True)
            existing.add(table.name)
            continue
        actual = {str(item["name"]) for item in _inspector().get_columns(table.name)}
        expected = {str(column.name) for column in table.columns}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"{table.name} exists with an incomplete schema; missing columns: "
                + ", ".join(missing)
            )


def upgrade() -> None:
    _add_shop_columns()
    _create_or_validate_tables()


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(_inspector().get_table_names())
    for model in reversed(MODELS):
        table = model.__table__
        if table.name in existing:
            table.drop(bind=bind, checkfirst=True)
            existing.discard(table.name)
    columns = {
        str(item["name"])
        for item in _inspector().get_columns("oauth_tiktok_shop_shops")
    }
    if "timezone_name" in columns:
        op.drop_column("oauth_tiktok_shop_shops", "timezone_name")
    if "shop_code" in columns:
        op.drop_column("oauth_tiktok_shop_shops", "shop_code")
