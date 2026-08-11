from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopOrder,
    TikTokShopOrderLine,
    TikTokShopProduct,
)


_ACTIVE_FLASH_STATUSES = {"ONGOING", "NOT_START"}


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _candidate(
    *,
    price: Any,
    source: str,
    observed_at: datetime | None,
) -> dict[str, Any] | None:
    parsed = _positive_decimal(price)
    if parsed is None:
        return None
    return {
        "price": parsed,
        "source": source,
        "observed_at": observed_at,
    }


def _newer_candidate(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if left is None:
        return right
    if right is None:
        return left
    left_at = left.get("observed_at")
    right_at = right.get("observed_at")
    if left_at is None:
        return right
    if right_at is None:
        return left
    return right if right_at >= left_at else left


def load_authoritative_product_prices(
    db: Session,
    *,
    workspace_id: int,
    store_id: str,
    product_ids: Iterable[str],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve current product prices from TikTok Shop-owned data.

    The newest paid unit price and the newest provider-confirmed flash-sale
    price compete by their official observation time.  The current listing
    price is only a fallback because a routine product refresh must not replace
    a real transaction price merely by being fetched later.
    """

    clean_ids = sorted(
        {str(product_id).strip() for product_id in product_ids if str(product_id or "").strip()}
    )
    normalized_store_id = str(store_id or "").strip()
    if not clean_ids or not normalized_store_id:
        return {}

    shop_ids = list(
        db.scalars(
            select(OAuthTikTokShopShop.id)
            .where(
                OAuthTikTokShopShop.workspace_id == int(workspace_id),
                OAuthTikTokShopShop.shop_id == normalized_store_id,
                OAuthTikTokShopShop.is_active.is_(True),
            )
            .order_by(OAuthTikTokShopShop.last_seen_at.desc(), OAuthTikTokShopShop.id.desc())
        ).all()
    )
    if not shop_ids:
        return {}

    transaction_observed_at = func.coalesce(
        TikTokShopOrder.paid_at,
        TikTokShopOrder.provider_created_at,
        TikTokShopOrderLine.synced_at,
    )
    transaction_ranked = (
        select(
            TikTokShopOrderLine.product_id.label("product_id"),
            TikTokShopOrderLine.sale_price.label("price"),
            transaction_observed_at.label("observed_at"),
            func.row_number()
            .over(
                partition_by=TikTokShopOrderLine.product_id,
                order_by=(transaction_observed_at.desc(), TikTokShopOrderLine.id.desc()),
            )
            .label("row_num"),
        )
        .join(
            TikTokShopOrder,
            and_(
                TikTokShopOrder.shop_row_id == TikTokShopOrderLine.shop_row_id,
                TikTokShopOrder.order_id == TikTokShopOrderLine.order_id,
            ),
        )
        .where(
            TikTokShopOrderLine.workspace_id == int(workspace_id),
            TikTokShopOrderLine.shop_row_id.in_(shop_ids),
            TikTokShopOrderLine.product_id.in_(clean_ids),
            TikTokShopOrderLine.sale_price.is_not(None),
            TikTokShopOrderLine.sale_price > 0,
            TikTokShopOrder.paid_at.is_not(None),
            ~func.upper(func.coalesce(TikTokShopOrder.status, "")).in_(
                ("CANCELLED", "UNPAID")
            ),
        )
        .subquery()
    )
    transaction_rows = db.execute(
        select(
            transaction_ranked.c.product_id,
            transaction_ranked.c.price,
            transaction_ranked.c.observed_at,
        ).where(transaction_ranked.c.row_num == 1)
    ).mappings().all()
    transactions = {
        str(row["product_id"]): _candidate(
            price=row["price"],
            source="tiktok_shop_latest_transaction",
            observed_at=row["observed_at"],
        )
        for row in transaction_rows
    }

    now_value = now or datetime.utcnow()
    flash_observed_at = func.coalesce(
        TikTokShopFlashSalePolicy.last_checked_at,
        TikTokShopFlashSalePolicy.last_applied_at,
        TikTokShopFlashSalePolicy.updated_at,
    )
    flash_rows = db.execute(
        select(
            TikTokShopFlashSalePolicy.product_id,
            TikTokShopFlashSalePolicy.activity_price_amount.label("price"),
            TikTokShopFlashSalePolicy.current_activity_status.label("activity_status"),
            TikTokShopFlashSalePolicy.current_begin_at.label("begin_at"),
            TikTokShopFlashSalePolicy.current_end_at.label("end_at"),
            flash_observed_at.label("observed_at"),
        )
        .where(
            TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
            TikTokShopFlashSalePolicy.shop_row_id.in_(shop_ids),
            TikTokShopFlashSalePolicy.product_id.in_(clean_ids),
            TikTokShopFlashSalePolicy.enabled.is_(True),
            TikTokShopFlashSalePolicy.status == "active",
            TikTokShopFlashSalePolicy.applied_revision
            >= TikTokShopFlashSalePolicy.policy_revision,
            TikTokShopFlashSalePolicy.current_activity_status.in_(
                tuple(_ACTIVE_FLASH_STATUSES)
            ),
            TikTokShopFlashSalePolicy.current_end_at.is_not(None),
            TikTokShopFlashSalePolicy.current_end_at >= now_value,
        )
        .order_by(
            TikTokShopFlashSalePolicy.product_id.asc(),
            flash_observed_at.desc(),
            TikTokShopFlashSalePolicy.id.desc(),
        )
    ).mappings().all()
    flashes: dict[str, dict[str, Any]] = {}
    flash_details: dict[str, dict[str, Any]] = {}
    for row in flash_rows:
        product_id = str(row["product_id"])
        if product_id in flashes:
            continue
        candidate = _candidate(
            price=row["price"],
            source="tiktok_shop_flash_sale",
            observed_at=row["observed_at"],
        )
        if candidate is None:
            continue
        flashes[product_id] = candidate
        flash_details[product_id] = {
            "flash_sale_status": row["activity_status"],
            "flash_sale_begin_at": _isoformat(row["begin_at"]),
            "flash_sale_end_at": _isoformat(row["end_at"]),
        }

    listing_rows = db.execute(
        select(
            TikTokShopProduct.product_id,
            TikTokShopProduct.min_sale_price.label("price"),
            TikTokShopProduct.synced_at.label("observed_at"),
        )
        .where(
            TikTokShopProduct.workspace_id == int(workspace_id),
            TikTokShopProduct.shop_row_id.in_(shop_ids),
            TikTokShopProduct.product_id.in_(clean_ids),
            TikTokShopProduct.min_sale_price.is_not(None),
            TikTokShopProduct.min_sale_price > 0,
        )
        .order_by(
            TikTokShopProduct.product_id.asc(),
            TikTokShopProduct.synced_at.desc(),
            TikTokShopProduct.id.desc(),
        )
    ).mappings().all()
    listings: dict[str, dict[str, Any]] = {}
    for row in listing_rows:
        product_id = str(row["product_id"])
        if product_id not in listings:
            listings[product_id] = _candidate(
                price=row["price"],
                source="tiktok_shop_listing",
                observed_at=row["observed_at"],
            )

    resolved: dict[str, dict[str, Any]] = {}
    for product_id in clean_ids:
        transaction = transactions.get(product_id)
        flash = flashes.get(product_id)
        selected = _newer_candidate(transaction, flash) or listings.get(product_id)
        if selected is None:
            continue
        resolved[product_id] = {
            "effective_price": float(selected["price"]),
            "effective_price_source": selected["source"],
            "effective_price_updated_at": _isoformat(selected.get("observed_at")),
            "latest_transaction_price": (
                float(transaction["price"]) if transaction is not None else None
            ),
            "latest_transaction_at": (
                _isoformat(transaction.get("observed_at")) if transaction is not None else None
            ),
            "flash_sale_price": float(flash["price"]) if flash is not None else None,
            "flash_sale_observed_at": (
                _isoformat(flash.get("observed_at")) if flash is not None else None
            ),
            "listing_price": (
                float(listings[product_id]["price"])
                if listings.get(product_id) is not None
                else None
            ),
            **flash_details.get(product_id, {}),
        }
    return resolved


def load_authoritative_product_price(
    db: Session,
    *,
    workspace_id: int,
    store_id: str,
    product_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    return load_authoritative_product_prices(
        db,
        workspace_id=workspace_id,
        store_id=store_id,
        product_ids=[product_id],
        now=now,
    ).get(str(product_id))


__all__ = [
    "load_authoritative_product_price",
    "load_authoritative_product_prices",
]
