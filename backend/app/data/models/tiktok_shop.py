from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base


UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")
Money = Numeric(20, 6)
Rate = Numeric(20, 8)


class TikTokShopSyncRun(Base):
    __tablename__ = "tiktok_shop_sync_runs"
    __table_args__ = (
        Index("idx_ttshop_sync_scope", "workspace_id", "account_id", "shop_row_id"),
        Index("idx_ttshop_sync_status", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_row_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"),
        default=None,
    )
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'scheduled'"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'running'"))
    range_start: Mapped[date | None] = mapped_column(Date, default=None)
    range_end_exclusive: Mapped[date | None] = mapped_column(Date, default=None)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_upserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_code: Mapped[str | None] = mapped_column(String(64), default=None)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)


class TikTokShopCategory(Base):
    __tablename__ = "tiktok_shop_categories"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "category_id", name="uq_ttshop_category"),
        Index("idx_ttshop_category_parent", "shop_row_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(128), default=None)
    local_name: Mapped[str | None] = mapped_column(String(512), default=None)
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    permission_statuses_json: Mapped[list | None] = mapped_column(JSON, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopProduct(Base):
    __tablename__ = "tiktok_shop_products"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "product_id", name="uq_ttshop_product"),
        Index("idx_ttshop_product_status", "workspace_id", "shop_row_id", "status"),
        Index("idx_ttshop_product_updated", "shop_row_id", "provider_updated_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    audit_status: Mapped[str | None] = mapped_column(String(64), default=None)
    listing_quality_tier: Mapped[str | None] = mapped_column(String(64), default=None)
    brand_id: Mapped[str | None] = mapped_column(String(128), default=None)
    brand_name: Mapped[str | None] = mapped_column(String(255), default=None)
    leaf_category_id: Mapped[str | None] = mapped_column(String(128), default=None)
    leaf_category_name: Mapped[str | None] = mapped_column(String(512), default=None)
    main_image_url: Mapped[str | None] = mapped_column(Text, default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    min_sale_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    max_sale_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    has_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    is_not_for_sale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    provider_updated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    source_api_version: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'202309'"))
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopSku(Base):
    __tablename__ = "tiktok_shop_skus"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "sku_id", name="uq_ttshop_sku"),
        Index("idx_ttshop_sku_product", "shop_row_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seller_sku: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    sale_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    tax_exclusive_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    inventory_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopGlobalProduct(Base):
    __tablename__ = "tiktok_shop_global_products"
    __table_args__ = (
        UniqueConstraint("account_id", "global_product_id", name="uq_ttshop_global_product"),
        Index("idx_ttshop_global_product_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    global_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopOrder(Base):
    __tablename__ = "tiktok_shop_orders"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "order_id", name="uq_ttshop_order"),
        Index("idx_ttshop_order_status", "workspace_id", "shop_row_id", "status"),
        Index("idx_ttshop_order_created", "shop_row_id", "provider_created_at"),
        Index("idx_ttshop_order_updated", "shop_row_id", "provider_updated_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    fulfillment_type: Mapped[str | None] = mapped_column(String(64), default=None)
    delivery_type: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    total_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    sub_total: Mapped[Decimal | None] = mapped_column(Money, default=None)
    original_total_product_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    seller_discount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    platform_discount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    shipping_fee: Mapped[Decimal | None] = mapped_column(Money, default=None)
    tax: Mapped[Decimal | None] = mapped_column(Money, default=None)
    is_sample_order: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    is_on_hold_order: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    is_subscription_order: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    provider_updated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopOrderLine(Base):
    __tablename__ = "tiktok_shop_order_lines"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "line_item_id", name="uq_ttshop_order_line"),
        Index("idx_ttshop_order_line_order", "shop_row_id", "order_id"),
        Index("idx_ttshop_order_line_product", "shop_row_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(128), default=None)
    product_name: Mapped[str | None] = mapped_column(String(1024), default=None)
    sku_id: Mapped[str | None] = mapped_column(String(128), default=None)
    sku_name: Mapped[str | None] = mapped_column(String(1024), default=None)
    seller_sku: Mapped[str | None] = mapped_column(String(255), default=None)
    display_status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    original_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    sale_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    seller_discount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    platform_discount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopFinanceStatement(Base):
    __tablename__ = "tiktok_shop_finance_statements"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "statement_id", name="uq_ttshop_statement"),
        Index("idx_ttshop_statement_time", "shop_row_id", "statement_time"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    statement_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(128), default=None)
    payment_status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    revenue_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    fee_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    adjustment_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    shipping_cost_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    settlement_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    statement_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    payment_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopFinanceTransaction(Base):
    __tablename__ = "tiktok_shop_finance_transactions"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "transaction_id", name="uq_ttshop_transaction"),
        Index("idx_ttshop_transaction_statement", "shop_row_id", "statement_id"),
        Index("idx_ttshop_transaction_order", "shop_row_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    statement_id: Mapped[str | None] = mapped_column(String(128), default=None)
    order_id: Mapped[str | None] = mapped_column(String(128), default=None)
    transaction_type: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    revenue_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    fee_tax_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    adjustment_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    shipping_cost_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    settlement_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    reserve_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    order_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopOrderFinanceSummary(Base):
    __tablename__ = "tiktok_shop_order_finance_summaries"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "order_id", name="uq_ttshop_order_finance"),
        Index("idx_ttshop_order_finance_created", "shop_row_id", "order_created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    revenue_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    fee_tax_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    shipping_cost_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    settlement_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    sku_transaction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    order_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopWithdrawal(Base):
    __tablename__ = "tiktok_shop_withdrawals"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "withdrawal_id", name="uq_ttshop_withdrawal"),
        Index("idx_ttshop_withdrawal_time", "shop_row_id", "provider_created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    withdrawal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    withdrawal_type: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopPayment(Base):
    __tablename__ = "tiktok_shop_payments"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "payment_id", name="uq_ttshop_payment"),
        Index("idx_ttshop_payment_time", "shop_row_id", "provider_created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    settlement_currency: Mapped[str | None] = mapped_column(String(16), default=None)
    settlement_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    before_exchange_currency: Mapped[str | None] = mapped_column(String(16), default=None)
    payment_amount_before_exchange: Mapped[Decimal | None] = mapped_column(Money, default=None)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopUnsettledTransaction(Base):
    __tablename__ = "tiktok_shop_unsettled_transactions"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "transaction_id", name="uq_ttshop_unsettled_tx"),
        Index("idx_ttshop_unsettled_order", "shop_row_id", "order_id"),
        Index("idx_ttshop_unsettled_time", "shop_row_id", "order_created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(128), default=None)
    transaction_type: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    unsettled_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    estimated_revenue_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    estimated_fee_tax_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    estimated_shipping_cost_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    estimated_settlement_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    order_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopPromotionActivity(Base):
    __tablename__ = "tiktok_shop_promotion_activities"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "activity_id", name="uq_ttshop_activity"),
        Index("idx_ttshop_activity_status", "shop_row_id", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    activity_type: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_type: Mapped[str | None] = mapped_column(String(64), default=None)
    product_level: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    begin_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    end_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    provider_updated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopFlashSalePolicy(Base):
    __tablename__ = "tiktok_shop_flash_sale_policies"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "product_id", name="uq_ttshop_flash_sale_policy"),
        Index("idx_ttshop_flash_sale_enabled", "shop_row_id", "enabled"),
        Index("idx_ttshop_flash_sale_due", "enabled", "next_renewal_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    activity_price_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'USD'"))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'active'")
    )
    policy_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    applied_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    current_activity_id: Mapped[str | None] = mapped_column(String(128), default=None)
    current_activity_status: Mapped[str | None] = mapped_column(String(32), default=None)
    current_begin_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    current_end_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    next_renewal_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_applied_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), default=None)
    last_error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class TikTokShopFlashSaleRun(Base):
    __tablename__ = "tiktok_shop_flash_sale_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ttshop_flash_sale_run_key"),
        Index("idx_ttshop_flash_sale_run_shop", "shop_row_id", "started_at"),
        Index("idx_ttshop_flash_sale_run_status", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'scheduled'")
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    previous_activity_ids_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    new_activity_id: Mapped[str | None] = mapped_column(String(128), default=None)
    provider_request_ids_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    details_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_code: Mapped[str | None] = mapped_column(String(128), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    started_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6), default=None
    )


class TikTokShopCoupon(Base):
    __tablename__ = "tiktok_shop_coupons"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "coupon_id", name="uq_ttshop_coupon"),
        Index("idx_ttshop_coupon_status", "shop_row_id", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    coupon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str | None] = mapped_column(String(64), default=None)
    product_scope: Mapped[str | None] = mapped_column(String(64), default=None)
    creation_source: Mapped[str | None] = mapped_column(String(64), default=None)
    discount_type: Mapped[str | None] = mapped_column(String(64), default=None)
    discount_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    threshold_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    claim_start_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    claim_end_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    total_claim_limit: Mapped[int | None] = mapped_column(Integer, default=None)
    single_buyer_claim_limit: Mapped[int | None] = mapped_column(Integer, default=None)
    provider_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    provider_updated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopShopHourlyMetric(Base):
    __tablename__ = "tiktok_shop_shop_hourly_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", "hour_index", name="uq_ttshop_hour_metric"),
        Index("idx_ttshop_hour_metric_date", "workspace_id", "shop_row_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    hour_index: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    customers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latest_available_timestamp: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopVideoOverviewDailyMetric(Base):
    __tablename__ = "tiktok_shop_video_overview_daily_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", name="uq_ttshop_video_overview"),
        Index("idx_ttshop_video_overview_date", "workspace_id", "shop_row_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    avg_customers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    product_impressions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    product_clicks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopVideoDailyMetric(Base):
    __tablename__ = "tiktok_shop_video_daily_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", "video_id", name="uq_ttshop_video_metric"),
        Index("idx_ttshop_video_metric_date", "workspace_id", "shop_row_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024), default=None)
    creator_open_id: Mapped[str | None] = mapped_column(String(192), default=None)
    creator_username: Mapped[str | None] = mapped_column(String(255), default=None)
    author_type: Mapped[str | None] = mapped_column(String(64), default=None)
    video_post_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    gpm: Mapped[Decimal | None] = mapped_column(Money, default=None)
    views: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    avg_customers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    products_json: Mapped[list | None] = mapped_column(JSON, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopVideoContentAnalysis(Base):
    """Immutable-input, retryable Hermes analysis for one video and metric window."""

    __tablename__ = "tiktok_shop_video_content_analyses"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_ttshop_video_analysis_cache"),
        Index(
            "idx_ttshop_video_analysis_scope",
            "workspace_id",
            "shop_row_id",
            "video_id",
            "created_at",
        ),
        Index("idx_ttshop_video_analysis_status", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"),
        nullable=False,
    )
    video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'QUEUED'"))
    model_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric_end_date_exclusive: Mapped[date] = mapped_column(Date, nullable=False)
    source_asset_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
    source_media_fingerprint: Mapped[str | None] = mapped_column(String(128), default=None)
    transcript_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'PENDING'")
    )
    transcript_source: Mapped[str | None] = mapped_column(String(64), default=None)
    transcript_language: Mapped[str | None] = mapped_column(String(32), default=None)
    transcript_text: Mapped[str | None] = mapped_column(Text, default=None)
    transcript_segments_json: Mapped[list | None] = mapped_column(JSON, default=None)
    transcript_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    transcript_error_message: Mapped[str | None] = mapped_column(Text, default=None)
    transcript_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    transcript_started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    transcript_completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    input_summary_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    analysis_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    usage_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    requested_by_user_id: Mapped[int | None] = mapped_column(UBigInt, default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class TikTokShopProductDailyMetric(Base):
    __tablename__ = "tiktok_shop_product_daily_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", "product_id", name="uq_ttshop_product_metric"),
        Index("idx_ttshop_product_metric_date", "workspace_id", "shop_row_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    product_impressions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    product_clicks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    add_cart_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    add_cart_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    click_order_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    refund_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    metrics_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopProductChannelDailyMetric(Base):
    __tablename__ = "tiktok_shop_product_channel_daily_metrics"
    __table_args__ = (
        UniqueConstraint(
            "shop_row_id",
            "report_date",
            "product_id",
            "channel",
            name="uq_ttshop_product_channel_metric",
        ),
        Index(
            "idx_ttshop_product_channel_date",
            "workspace_id",
            "shop_row_id",
            "report_date",
            "channel",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    estimated_customers: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    product_impressions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    product_clicks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unique_product_impressions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    unique_clicks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    unique_click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    add_cart_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    add_cart_users: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    add_cart_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    unique_add_cart_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    click_order_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    unique_click_order_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    new_content_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopSkuDailyMetric(Base):
    __tablename__ = "tiktok_shop_sku_daily_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", "sku_id", name="uq_ttshop_sku_metric"),
        Index("idx_ttshop_sku_metric_product", "shop_row_id", "report_date", "product_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    sku_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(128), default=None)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    units_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class TikTokShopLiveDailyMetric(Base):
    __tablename__ = "tiktok_shop_live_daily_metrics"
    __table_args__ = (
        UniqueConstraint("shop_row_id", "report_date", name="uq_ttshop_live_metric"),
        Index("idx_ttshop_live_metric_date", "workspace_id", "shop_row_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"), nullable=False
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16), default=None)
    gmv: Mapped[Decimal | None] = mapped_column(Money, default=None)
    customers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    click_through_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    click_to_order_rate: Mapped[Decimal | None] = mapped_column(Rate, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    synced_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
