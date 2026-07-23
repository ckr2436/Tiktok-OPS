from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
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
Rate = Numeric(12, 8)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CommerceProductMapping(Base):
    """Durable link between a Shop product and its advertising item group."""

    __tablename__ = "commerce_product_mappings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "shop_row_id",
            "shop_product_id",
            "advertiser_id",
            "item_group_id",
            name="uq_commerce_product_mapping",
        ),
        Index(
            "idx_commerce_mapping_product",
            "workspace_id",
            "shop_row_id",
            "shop_product_id",
        ),
        Index(
            "idx_commerce_mapping_advertiser",
            "workspace_id",
            "advertiser_id",
            "store_id",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    business_auth_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("oauth_accounts_ttb.id", ondelete="SET NULL"),
        default=None,
    )
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'exact_product_id'"),
    )
    confidence: Mapped[Decimal] = mapped_column(
        Rate,
        nullable=False,
        server_default=text("1.0"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        default=_utcnow,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class CommerceProductCostVersion(Base):
    """Append-only product or SKU cost assumptions used for profitability."""

    __tablename__ = "commerce_product_cost_versions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "shop_row_id",
            "product_id",
            "sku_id",
            "effective_from",
            name="uq_commerce_product_cost_version",
        ),
        Index(
            "idx_commerce_cost_effective",
            "workspace_id",
            "shop_row_id",
            "product_id",
            "sku_id",
            "effective_from",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_row_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_shops.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Empty string means product-level defaults. A concrete SKU overrides it.
    sku_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        server_default=text("''"),
    )
    effective_from: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'USD'"),
    )
    unit_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    packaging_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    fulfillment_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    seller_shipping_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    other_variable_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    platform_fee_rate: Mapped[Decimal] = mapped_column(
        Rate, nullable=False, server_default=text("0")
    )
    payment_fee_rate: Mapped[Decimal] = mapped_column(
        Rate, nullable=False, server_default=text("0")
    )
    affiliate_commission_rate: Mapped[Decimal] = mapped_column(
        Rate, nullable=False, server_default=text("0")
    )
    expected_refund_rate: Mapped[Decimal] = mapped_column(
        Rate, nullable=False, server_default=text("0")
    )
    target_margin_rate: Mapped[Decimal] = mapped_column(
        Rate, nullable=False, server_default=text("0")
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", ondelete="SET NULL"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        default=_utcnow,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )


__all__ = [
    "CommerceProductMapping",
    "CommerceProductCostVersion",
]
