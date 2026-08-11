from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Iterable, Mapping, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
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
from app.services.tiktok_shop_api import TikTokShopAPIClient, TikTokShopRequestResult


T = TypeVar("T")
logger = logging.getLogger("gmv.tiktok_shop.sync")
SUPPORTED_DOMAINS = frozenset(
    {"catalog", "orders", "finance", "promotions", "analytics", "global_products"}
)
_ORDER_PII_KEYS = {
    "buyer_avatar",
    "buyer_email",
    "buyer_message",
    "buyer_nickname",
    "recipient_address",
    "recipient_name",
    "tracking_number",
    "user_id",
}
_ORDER_PII_MARKERS = ("address", "email", "phone", "recipient", "tracking")


@dataclass(slots=True)
class SyncStats:
    pages: int = 0
    seen: int = 0
    upserted: int = 0
    request_id: str | None = None

    def absorb(self, result: TikTokShopRequestResult) -> None:
        self.pages += 1
        self.request_id = result.request_id or self.request_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _shop_zone(shop: OAuthTikTokShopShop) -> ZoneInfo:
    try:
        return ZoneInfo(str(shop.timezone_name or "Etc/GMT+8"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("Etc/GMT+8")


def shop_today(shop: OAuthTikTokShopShop) -> date:
    return datetime.now(_shop_zone(shop)).date()


def _local_midnight_utc(value: date, shop: OAuthTikTokShopShop) -> datetime:
    return (
        datetime.combine(value, dt_time.min, tzinfo=_shop_zone(shop))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def local_date_epoch(value: date, shop: OAuthTikTokShopShop) -> int:
    local = datetime.combine(value, dt_time.min, tzinfo=_shop_zone(shop))
    return int(local.timestamp())


def _date_range(
    shop: OAuthTikTokShopShop,
    *,
    start_date: date | None,
    end_date_exclusive: date | None,
    default_days: int,
    max_days: int = 365,
) -> tuple[date, date]:
    end = end_date_exclusive or (shop_today(shop) + timedelta(days=1))
    start = start_date or (end - timedelta(days=default_days))
    if start >= end:
        raise APIError("INVALID_DATE_RANGE", "start_date must be before end_date_exclusive.", 400)
    if (end - start).days > max_days:
        raise APIError("DATE_RANGE_TOO_LARGE", f"Date range cannot exceed {max_days} days.", 400)
    return start, end


def _text(value: Any, length: int | None = None) -> str | None:
    result = str(value or "").strip()
    if not result:
        return None
    return result[:length] if length else result


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, Mapping):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _currency(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return _text(value.get("currency"), 16)
    return None


def _datetime(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or str(value).isdigit():
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def sanitize_order_payload(value: Any) -> Any:
    """Remove customer and delivery identifiers before persisting provider JSON."""

    if isinstance(value, list):
        return [sanitize_order_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized in _ORDER_PII_KEYS or any(marker in normalized for marker in _ORDER_PII_MARKERS):
            continue
        cleaned[str(key)] = sanitize_order_payload(item)
    return cleaned


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _next_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    return _text(data.get("next_page_token"), 1024)


def _next_analytics_token(
    data: Any,
    seen_tokens: set[str],
    *,
    dataset: str,
    request_id: str | None,
) -> str | None:
    token = _next_token(data)
    if not token:
        return None
    if token in seen_tokens:
        raise APIError(
            "TIKTOK_SHOP_PAGINATION_REPEATED",
            f"TikTok Shop {dataset} analytics repeated a page token.",
            502,
            data={"request_id": request_id, "retryable": True},
        )
    seen_tokens.add(token)
    return token


def _upsert(
    db: Session,
    model: type[T],
    filters: Mapping[str, Any],
    values: Mapping[str, Any],
) -> T:
    filter_values = dict(filters)
    row = next(
        (
            candidate
            for candidate in db.new
            if isinstance(candidate, model)
            and all(getattr(candidate, key, None) == value for key, value in filter_values.items())
        ),
        None,
    )
    if row is None:
        row = db.scalar(select(model).filter_by(**filter_values))
    if row is None:
        row = model(**filter_values)  # type: ignore[call-arg]
    for key, value in values.items():
        setattr(row, key, value)
    if hasattr(row, "synced_at"):
        setattr(row, "synced_at", _utcnow())
    db.add(row)
    return row


def _scope(shop: OAuthTikTokShopShop) -> dict[str, int]:
    return {
        "workspace_id": int(shop.workspace_id),
        "account_id": int(shop.account_id),
        "shop_row_id": int(shop.id),
    }


def _primary_image(product: Mapping[str, Any]) -> str | None:
    images = product.get("main_images")
    if not isinstance(images, list) or not images or not isinstance(images[0], dict):
        return None
    image = images[0]
    for key in ("urls", "thumb_urls"):
        values = image.get(key)
        if isinstance(values, list) and values:
            return _text(values[0])
    return _text(image.get("uri"))


def _leaf_category(product: Mapping[str, Any]) -> tuple[str | None, str | None]:
    chains = product.get("category_chains")
    if not isinstance(chains, list):
        return None, None
    choices = [item for item in chains if isinstance(item, dict)]
    if not choices:
        return None, None
    leaf = next((item for item in reversed(choices) if bool(item.get("is_leaf"))), choices[-1])
    return _text(leaf.get("id"), 128), _text(leaf.get("local_name"), 512)


def _upsert_product(db: Session, shop: OAuthTikTokShopShop, product: Mapping[str, Any]) -> None:
    product_id = _text(product.get("id"), 128)
    if not product_id:
        return
    skus = [item for item in (product.get("skus") or []) if isinstance(item, dict)]
    prices = [_decimal(item.get("price", {}).get("sale_price")) for item in skus]
    prices = [value for value in prices if value is not None]
    currency = next(
        (
            _text(item.get("price", {}).get("currency"), 16)
            for item in skus
            if isinstance(item.get("price"), dict) and item.get("price", {}).get("currency")
        ),
        None,
    )
    audit = product.get("audit") if isinstance(product.get("audit"), dict) else {}
    brand = product.get("brand") if isinstance(product.get("brand"), dict) else {}
    category_id, category_name = _leaf_category(product)
    _upsert(
        db,
        TikTokShopProduct,
        {**_scope(shop), "product_id": product_id},
        {
            "title": _text(product.get("title"), 1024),
            "status": _text(product.get("product_status") or product.get("status"), 64),
            "audit_status": _text(audit.get("status"), 64),
            "listing_quality_tier": _text(product.get("listing_quality_tier"), 64),
            "brand_id": _text(brand.get("id"), 128),
            "brand_name": _text(brand.get("name"), 255),
            "leaf_category_id": category_id,
            "leaf_category_name": category_name,
            "main_image_url": _primary_image(product),
            "currency": currency,
            "min_sale_price": min(prices) if prices else None,
            "max_sale_price": max(prices) if prices else None,
            "has_draft": bool(product.get("has_draft")),
            "is_not_for_sale": bool(product.get("is_not_for_sale")),
            "provider_created_at": _datetime(product.get("create_time")),
            "provider_updated_at": _datetime(product.get("update_time")),
            "source_api_version": "202502" if not product.get("category_chains") else "202309",
            "raw_json": deepcopy(dict(product)),
        },
    )
    for sku in skus:
        sku_id = _text(sku.get("id"), 128)
        if not sku_id:
            continue
        price = sku.get("price") if isinstance(sku.get("price"), dict) else {}
        status_info = sku.get("status_info") if isinstance(sku.get("status_info"), dict) else {}
        inventory = sku.get("inventory") if isinstance(sku.get("inventory"), list) else []
        _upsert(
            db,
            TikTokShopSku,
            {**_scope(shop), "sku_id": sku_id},
            {
                "product_id": product_id,
                "seller_sku": _text(sku.get("seller_sku"), 255),
                "status": _text(status_info.get("status"), 64),
                "currency": _text(price.get("currency"), 16),
                "sale_price": _decimal(price.get("sale_price")),
                "tax_exclusive_price": _decimal(price.get("tax_exclusive_price")),
                "inventory_quantity": sum(
                    _integer(item.get("quantity"))
                    for item in inventory
                    if isinstance(item, dict)
                ),
                "raw_json": deepcopy(dict(sku)),
            },
        )


async def sync_catalog(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
) -> None:
    latest_category_sync = db.scalar(
        select(func.max(TikTokShopCategory.synced_at)).where(
            TikTokShopCategory.shop_row_id == int(client.shop.id)
        )
    )
    if not latest_category_sync or latest_category_sync < _utcnow() - timedelta(hours=24):
        existing_categories = {
            row.category_id: row
            for row in db.execute(
                select(TikTokShopCategory).where(
                    TikTokShopCategory.shop_row_id == int(client.shop.id)
                )
            ).scalars()
        }
        categories = await client.categories()
        stats.absorb(categories)
        for item in _rows(categories.data, "categories"):
            category_id = _text(item.get("id"), 128)
            if not category_id:
                continue
            stats.seen += 1
            row = existing_categories.get(category_id)
            if row is None:
                row = TikTokShopCategory(**_scope(client.shop), category_id=category_id)
                existing_categories[category_id] = row
            row.parent_id = _text(item.get("parent_id"), 128)
            row.local_name = _text(item.get("local_name"), 512)
            row.is_leaf = bool(item.get("is_leaf"))
            row.permission_statuses_json = list(item.get("permission_statuses") or [])
            row.raw_json = deepcopy(item)
            row.synced_at = _utcnow()
            db.add(row)
            stats.upserted += 1
        db.flush()

    token: str | None = None
    for _ in range(1000):
        result = await client.search_products(page_token=token)
        stats.absorb(result)
        products = _rows(result.data, "products")
        for summary in products:
            product_id = _text(summary.get("id"), 128)
            if not product_id:
                continue
            stats.seen += 1
            detail = await client.get_product(product_id)
            stats.absorb(detail)
            product = detail.data if isinstance(detail.data, dict) else summary
            _upsert_product(db, client.shop, product)
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break


def _upsert_order(db: Session, shop: OAuthTikTokShopShop, order: Mapping[str, Any]) -> None:
    order_id = _text(order.get("id"), 128)
    if not order_id:
        return
    payment = order.get("payment") if isinstance(order.get("payment"), dict) else {}
    _upsert(
        db,
        TikTokShopOrder,
        {**_scope(shop), "order_id": order_id},
        {
            "status": _text(order.get("status"), 64),
            "fulfillment_type": _text(order.get("fulfillment_type"), 64),
            "delivery_type": _text(order.get("delivery_type"), 64),
            "currency": _text(payment.get("currency"), 16),
            "total_amount": _decimal(payment.get("total_amount")),
            "sub_total": _decimal(payment.get("sub_total")),
            "original_total_product_price": _decimal(payment.get("original_total_product_price")),
            "seller_discount": _decimal(payment.get("seller_discount")),
            "platform_discount": _decimal(payment.get("platform_discount")),
            "shipping_fee": _decimal(payment.get("shipping_fee")),
            "tax": _decimal(payment.get("tax")),
            "is_sample_order": bool(order.get("is_sample_order")),
            "is_on_hold_order": bool(order.get("is_on_hold_order")),
            "is_subscription_order": bool(order.get("is_subscription_order")),
            "provider_created_at": _datetime(order.get("create_time")),
            "provider_updated_at": _datetime(order.get("update_time")),
            "paid_at": _datetime(order.get("paid_time")),
            "raw_json": sanitize_order_payload(deepcopy(dict(order))),
        },
    )
    for item in order.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        line_id = _text(item.get("id"), 128)
        if not line_id:
            continue
        _upsert(
            db,
            TikTokShopOrderLine,
            {**_scope(shop), "line_item_id": line_id},
            {
                "order_id": order_id,
                "product_id": _text(item.get("product_id"), 128),
                "product_name": _text(item.get("product_name"), 1024),
                "sku_id": _text(item.get("sku_id"), 128),
                "sku_name": _text(item.get("sku_name"), 1024),
                "seller_sku": _text(item.get("seller_sku"), 255),
                "display_status": _text(item.get("display_status"), 64),
                "currency": _text(item.get("currency"), 16),
                "original_price": _decimal(item.get("original_price")),
                "sale_price": _decimal(item.get("sale_price")),
                "seller_discount": _decimal(item.get("seller_discount")),
                "platform_discount": _decimal(item.get("platform_discount")),
                "quantity": max(_integer(item.get("quantity"), 1), 1),
                "raw_json": sanitize_order_payload(deepcopy(item)),
            },
        )


async def sync_orders(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
    *,
    start_date: date | None,
    end_date_exclusive: date | None,
) -> tuple[date, date]:
    start, end = _date_range(
        client.shop,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        default_days=14,
    )
    token: str | None = None
    for _ in range(1000):
        result = await client.search_orders(
            create_time_ge=local_date_epoch(start, client.shop),
            create_time_lt=local_date_epoch(end, client.shop),
            page_token=token,
        )
        stats.absorb(result)
        orders = _rows(result.data, "orders")
        ids = [_text(item.get("id"), 128) for item in orders]
        ids = [item for item in ids if item]
        detailed: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(ids), 50):
            detail = await client.get_orders(ids[offset : offset + 50])
            stats.absorb(detail)
            for item in _rows(detail.data, "orders"):
                if item.get("id"):
                    detailed[str(item["id"])] = item
        for item in orders:
            stats.seen += 1
            order = detailed.get(str(item.get("id"))) or item
            _upsert_order(db, client.shop, order)
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break
    return start, end


async def sync_finance(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
    *,
    start_date: date | None,
    end_date_exclusive: date | None,
) -> tuple[date, date]:
    start, end = _date_range(
        client.shop,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        default_days=90,
    )
    ge = local_date_epoch(start, client.shop)
    lt = local_date_epoch(end, client.shop)
    token: str | None = None
    statement_ids: list[str] = []
    transaction_order_ids: set[str] = set()
    for _ in range(1000):
        result = await client.statements(
            statement_time_ge=ge,
            statement_time_lt=lt,
            page_token=token,
        )
        stats.absorb(result)
        for item in _rows(result.data, "statements"):
            statement_id = _text(item.get("id"), 128)
            if not statement_id:
                continue
            statement_ids.append(statement_id)
            stats.seen += 1
            _upsert(
                db,
                TikTokShopFinanceStatement,
                {**_scope(client.shop), "statement_id": statement_id},
                {
                    "payment_id": _text(item.get("payment_id"), 128),
                    "payment_status": _text(item.get("payment_status"), 64),
                    "currency": _text(item.get("currency"), 16),
                    "revenue_amount": _decimal(item.get("revenue_amount")),
                    "fee_amount": _decimal(item.get("fee_amount")),
                    "adjustment_amount": _decimal(item.get("adjustment_amount")),
                    "shipping_cost_amount": _decimal(item.get("shipping_cost_amount")),
                    "settlement_amount": _decimal(item.get("settlement_amount")),
                    "statement_time": _datetime(item.get("statement_time")),
                    "payment_time": _datetime(item.get("payment_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break

    for statement_id in statement_ids:
        tx_token: str | None = None
        for _ in range(1000):
            result = await client.statement_transactions(statement_id, page_token=tx_token)
            stats.absorb(result)
            for item in _rows(result.data, "transactions"):
                transaction_id = _text(item.get("id"), 128)
                if not transaction_id:
                    continue
                stats.seen += 1
                associated_order_id = _text(item.get("associated_order_id"), 128)
                if associated_order_id:
                    transaction_order_ids.add(associated_order_id)
                _upsert(
                    db,
                    TikTokShopFinanceTransaction,
                    {**_scope(client.shop), "transaction_id": transaction_id},
                    {
                        "statement_id": statement_id,
                        "order_id": associated_order_id,
                        "transaction_type": _text(item.get("type"), 64),
                        "status": _text(item.get("status") or item.get("reserve_status"), 64),
                        "currency": _text(
                            item.get("currency")
                            or (result.data or {}).get("currency")
                            if isinstance(result.data, dict)
                            else None,
                            16,
                        ),
                        "revenue_amount": _decimal(item.get("revenue_amount")),
                        "fee_tax_amount": _decimal(item.get("fee_tax_amount")),
                        "adjustment_amount": _decimal(item.get("adjustment_amount")),
                        "shipping_cost_amount": _decimal(item.get("shipping_cost_amount")),
                        "settlement_amount": _decimal(item.get("settlement_amount")),
                        "reserve_amount": _decimal(item.get("reserve_amount")),
                        "order_created_at": _datetime(item.get("order_create_time")),
                        "raw_json": deepcopy(item),
                    },
                )
                stats.upserted += 1
            tx_token = _next_token(result.data)
            if not tx_token:
                break

    for order_id in sorted(transaction_order_ids):
        result = await client.order_transactions(order_id)
        stats.absorb(result)
        item = result.data if isinstance(result.data, dict) else {}
        provider_order_id = _text(item.get("order_id") or order_id, 128)
        if not provider_order_id:
            continue
        sku_transactions = (
            item.get("sku_transactions")
            if isinstance(item.get("sku_transactions"), list)
            else []
        )
        stats.seen += 1
        _upsert(
            db,
            TikTokShopOrderFinanceSummary,
            {**_scope(client.shop), "order_id": provider_order_id},
            {
                "currency": _text(item.get("currency"), 16),
                "revenue_amount": _decimal(item.get("revenue_amount")),
                "fee_tax_amount": _decimal(item.get("fee_and_tax_amount")),
                "shipping_cost_amount": _decimal(item.get("shipping_cost_amount")),
                "settlement_amount": _decimal(item.get("settlement_amount")),
                "sku_transaction_count": len(sku_transactions),
                "order_created_at": _datetime(item.get("order_create_time")),
                "raw_json": deepcopy(item),
            },
        )
        stats.upserted += 1

    token = None
    for _ in range(1000):
        result = await client.withdrawals(create_time_ge=ge, create_time_lt=lt, page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "withdrawals"):
            withdrawal_id = _text(item.get("id"), 128)
            if not withdrawal_id:
                continue
            stats.seen += 1
            _upsert(
                db,
                TikTokShopWithdrawal,
                {**_scope(client.shop), "withdrawal_id": withdrawal_id},
                {
                    "withdrawal_type": _text(item.get("type"), 64),
                    "status": _text(item.get("status"), 64),
                    "currency": _text(item.get("currency"), 16),
                    "amount": _decimal(item.get("amount")),
                    "provider_created_at": _datetime(item.get("create_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break

    token = None
    for _ in range(1000):
        result = await client.payments(create_time_ge=ge, create_time_lt=lt, page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "payments"):
            payment_id = _text(item.get("id"), 128)
            if not payment_id:
                continue
            amount = item.get("amount")
            settlement = item.get("settlement_amount")
            before_exchange = item.get("payment_amount_before_exchange")
            stats.seen += 1
            _upsert(
                db,
                TikTokShopPayment,
                {**_scope(client.shop), "payment_id": payment_id},
                {
                    "status": _text(item.get("status"), 64),
                    "currency": _currency(amount),
                    "amount": _decimal(amount),
                    "settlement_currency": _currency(settlement),
                    "settlement_amount": _decimal(settlement),
                    "before_exchange_currency": _currency(before_exchange),
                    "payment_amount_before_exchange": _decimal(before_exchange),
                    "exchange_rate": _decimal(item.get("exchange_rate")),
                    "provider_created_at": _datetime(item.get("create_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break

    token = None
    for _ in range(1000):
        result = await client.unsettled_transactions(
            search_time_ge=ge,
            search_time_lt=lt,
            page_token=token,
        )
        stats.absorb(result)
        for item in _rows(result.data, "transactions"):
            transaction_id = _text(item.get("id"), 128)
            if not transaction_id:
                continue
            stats.seen += 1
            _upsert(
                db,
                TikTokShopUnsettledTransaction,
                {**_scope(client.shop), "transaction_id": transaction_id},
                {
                    "order_id": _text(item.get("order_id"), 128),
                    "transaction_type": _text(item.get("type"), 64),
                    "status": _text(item.get("status"), 64),
                    "unsettled_reason": _text(item.get("unsettled_reason"), 255),
                    "currency": _text(item.get("currency"), 16),
                    "estimated_revenue_amount": _decimal(item.get("est_revenue_amount")),
                    "estimated_fee_tax_amount": _decimal(item.get("est_fee_tax_amount")),
                    "estimated_shipping_cost_amount": _decimal(
                        item.get("est_shipping_cost_amount")
                    ),
                    "estimated_settlement_amount": _decimal(
                        item.get("est_settlement_amount")
                        or item.get("estimated_settlement")
                    ),
                    "order_created_at": _datetime(item.get("order_create_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break
    return start, end


async def sync_promotions(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
) -> None:
    token: str | None = None
    for _ in range(1000):
        result = await client.promotion_activities(page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "activities"):
            activity_id = _text(item.get("id"), 128)
            if not activity_id:
                continue
            stats.seen += 1
            _upsert(
                db,
                TikTokShopPromotionActivity,
                {**_scope(client.shop), "activity_id": activity_id},
                {
                    "title": _text(item.get("title"), 512),
                    "activity_type": _text(item.get("activity_type"), 64),
                    "duration_type": _text(item.get("duration_type"), 64),
                    "product_level": _text(item.get("product_level"), 64),
                    "status": _text(item.get("status"), 64),
                    "begin_at": _datetime(item.get("begin_time")),
                    "end_at": _datetime(item.get("end_time")),
                    "provider_created_at": _datetime(item.get("create_time")),
                    "provider_updated_at": _datetime(item.get("update_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break

    token = None
    for _ in range(1000):
        result = await client.coupons(page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "coupons"):
            coupon_id = _text(item.get("id"), 128)
            if not coupon_id:
                continue
            discount = item.get("discount") if isinstance(item.get("discount"), dict) else {}
            reduction = (
                discount.get("reduction_amount")
                if isinstance(discount.get("reduction_amount"), dict)
                else {}
            )
            threshold = item.get("threshold") if isinstance(item.get("threshold"), dict) else {}
            min_spend = (
                threshold.get("min_spend")
                if isinstance(threshold.get("min_spend"), dict)
                else {}
            )
            duration = (
                item.get("claim_duration")
                if isinstance(item.get("claim_duration"), dict)
                else {}
            )
            limits = item.get("usage_limits") if isinstance(item.get("usage_limits"), dict) else {}
            stats.seen += 1
            _upsert(
                db,
                TikTokShopCoupon,
                {**_scope(client.shop), "coupon_id": coupon_id},
                {
                    "title": _text(item.get("title"), 512),
                    "status": _text(item.get("status"), 64),
                    "product_scope": _text(item.get("product_scope"), 64),
                    "creation_source": _text(item.get("creation_source"), 64),
                    "discount_type": _text(discount.get("type"), 64),
                    "discount_amount": _decimal(reduction.get("amount")),
                    "threshold_amount": _decimal(min_spend.get("amount")),
                    "currency": _text(
                        reduction.get("currency") or min_spend.get("currency"),
                        16,
                    ),
                    "claim_start_at": _datetime(duration.get("start_time")),
                    "claim_end_at": _datetime(duration.get("end_time")),
                    "total_claim_limit": _integer(limits.get("total_claim_limit")),
                    "single_buyer_claim_limit": _integer(limits.get("single_buyer_claim_limit")),
                    "provider_created_at": _datetime(item.get("create_time")),
                    "provider_updated_at": _datetime(item.get("update_time")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break


def _performance_total(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("total_performance")
    return value if isinstance(value, dict) else {}


_PRODUCT_CHANNEL_FIELDS = {
    "total": "total_performance",
    "seller_product_card": "seller_product_card_performance",
    "seller_video": "seller_video_performance",
    "seller_live": "seller_live_performance",
    "affiliate_total": "affiliate_total_performance",
    "affiliate_video": "affiliate_video_performance",
    "affiliate_live": "affiliate_live_performance",
    "shop_tab": "shop_tab_performance",
}


def _channel_metric_values(performance: Mapping[str, Any]) -> dict[str, Any]:
    money = (
        performance.get("gmv")
        or performance.get("attributed_gmv")
        or performance.get("attributed_video_gmv")
        or performance.get("live_attributed_gmv")
        or performance.get("shop_tab_gmv")
    )
    return {
        "currency": _currency(money),
        "gmv": _decimal(money),
        "orders": _integer(performance.get("orders") or performance.get("attributed_orders")),
        "sku_orders": _integer(
            performance.get("sku_orders") or performance.get("attributed_sku_orders")
        ),
        "items_sold": _integer(
            performance.get("items_sold")
            or performance.get("attributed_sold_items")
            or performance.get("shop_tab_sold_items")
        ),
        "estimated_customers": _integer(performance.get("estimated_customers")),
        "product_impressions": _integer(
            performance.get("product_impressions")
            or performance.get("shop_tab_product_impressions")
        ),
        "product_clicks": _integer(
            performance.get("product_clicks") or performance.get("shop_tab_product_clicks")
        ),
        "unique_product_impressions": _integer(
            performance.get("unique_product_impressions")
        ),
        "unique_clicks": _integer(
            performance.get("unique_clicks")
            or performance.get("unique_shop_tab_product_clicks")
        ),
        "click_through_rate": _decimal(
            performance.get("ctr") or performance.get("shop_tab_ctr")
        ),
        "unique_click_through_rate": _decimal(performance.get("unique_ctr")),
        "add_cart_count": _integer(performance.get("add_cart_count")),
        "add_cart_users": _integer(
            performance.get("add_cart_users") or performance.get("atc_users")
        ),
        "add_cart_rate": _decimal(performance.get("add_cart_rate")),
        "unique_add_cart_rate": _decimal(performance.get("unique_atc_rate")),
        "click_order_rate": _decimal(
            performance.get("click_order_rate") or performance.get("shop_tab_ctor_sku")
        ),
        "unique_click_order_rate": _decimal(
            performance.get("unique_click_order_rate")
        ),
        "new_content_count": _integer(
            performance.get("new_video_count") or performance.get("new_live_count")
        ),
        "raw_json": deepcopy(dict(performance)),
    }


def _video_channel_totals(
    totals: dict[str, Any],
    performance: Mapping[str, Any],
) -> None:
    values = _channel_metric_values(performance)
    totals["currency"] = values.get("currency") or totals.get("currency") or "USD"
    totals["gmv"] += values.get("gmv") or Decimal("0")
    totals["sku_orders"] += int(values.get("sku_orders") or 0)
    totals["estimated_customers"] += int(values.get("estimated_customers") or 0)
    totals["product_impressions"] += int(values.get("product_impressions") or 0)
    totals["product_clicks"] += int(values.get("product_clicks") or 0)


def _shop_video_gmv(data: Any) -> tuple[Decimal | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    performance = data.get("performance")
    intervals = performance.get("intervals") if isinstance(performance, dict) else []
    amount = Decimal("0")
    currency: str | None = None
    found = False
    for interval in intervals or []:
        sales = interval.get("sales") if isinstance(interval, dict) else None
        gmv = sales.get("gmv") if isinstance(sales, dict) else None
        breakdowns = gmv.get("breakdowns") if isinstance(gmv, dict) else []
        for breakdown in breakdowns or []:
            if not isinstance(breakdown, dict) or str(breakdown.get("type") or "").upper() != "VIDEO":
                continue
            money = breakdown.get("gmv")
            value = _decimal(money)
            if value is not None:
                amount += value
                found = True
            currency = _currency(money) or currency
    return (amount, currency) if found else (None, currency)


def _realtime_overview_fallback_allowed(exc: APIError) -> bool:
    data = exc.data if isinstance(exc.data, dict) else {}
    try:
        provider_code = int(data.get("provider_code"))
    except (TypeError, ValueError):
        provider_code = 0
    return bool(data.get("retryable")) and provider_code in {36009002, 36009003, 36009004}


def _overview_raw(
    item: Mapping[str, Any],
    *,
    source: str,
    provisional: bool,
    latest_available_date: Any,
    request_id: str | None,
    fallback_error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = deepcopy(dict(item))
    payload["_gmv_ops_meta"] = {
        "source": source,
        "provisional": bool(provisional),
        "latest_available_date": _text(latest_available_date, 32),
        "provider_request_id": _text(request_id, 128),
        "fallback_error": deepcopy(dict(fallback_error or {})) or None,
        "ctr_definition": "product_clicks_divided_by_video_views",
    }
    return payload


async def _sync_analytics_date(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
    report_date: date,
) -> None:
    report = report_date.isoformat()
    end = (report_date + timedelta(days=1)).isoformat()
    mutable_day = report_date == shop_today(client.shop)
    overview_error: APIError | None = None
    video_overview: TikTokShopRequestResult | None = None

    try:
        video_overview = await client.video_overview(report, end, today=mutable_day)
        stats.absorb(video_overview)
    except APIError as exc:
        if not mutable_day or not _realtime_overview_fallback_allowed(exc):
            raise
        overview_error = exc
        logger.warning(
            "TikTok Shop realtime video overview unavailable; using official product/shop "
            "fields workspace_id=%s shop_row_id=%s report_date=%s provider_code=%s request_id=%s",
            client.workspace_id,
            client.shop.id,
            report,
            (exc.data or {}).get("provider_code") if isinstance(exc.data, dict) else None,
            (exc.data or {}).get("request_id") if isinstance(exc.data, dict) else None,
        )
    overview_performance = (
        video_overview.data.get("performance")
        if video_overview is not None and isinstance(video_overview.data, dict)
        else {}
    )
    overview_intervals = (
        overview_performance.get("intervals")
        if isinstance(overview_performance, dict)
        else []
    )
    for item in overview_intervals or []:
        if not isinstance(item, dict):
            continue
        day = date.fromisoformat(str(item.get("start_date") or report)[:10])
        money = item.get("gmv")
        stats.seen += 1
        _upsert(
            db,
            TikTokShopVideoOverviewDailyMetric,
            {**_scope(client.shop), "report_date": day},
            {
                "currency": _currency(money),
                "gmv": _decimal(money),
                "avg_customers": _integer(item.get("avg_customers")),
                "product_impressions": _integer(item.get("product_impressions")),
                "product_clicks": _integer(item.get("product_clicks")),
                "sku_orders": _integer(item.get("sku_orders")),
                "click_through_rate": _decimal(item.get("click_through_rate")),
                "raw_json": _overview_raw(
                    item,
                    source="shop_video_overview",
                    provisional=mutable_day,
                    latest_available_date=(video_overview.data or {}).get(
                        "latest_available_date"
                    ),
                    request_id=video_overview.request_id,
                ),
            },
        )
        stats.upserted += 1

    hourly = await client.shop_hourly_performance(report)
    stats.absorb(hourly)
    performance = hourly.data.get("performance") if isinstance(hourly.data, dict) else {}
    if not isinstance(performance, dict):
        performance = {}
    latest = _datetime(performance.get("latest_available_timestamp"))
    for item in performance.get("intervals") or []:
        if not isinstance(item, dict):
            continue
        stats.seen += 1
        money = item.get("gmv")
        _upsert(
            db,
            TikTokShopShopHourlyMetric,
            {
                **_scope(client.shop),
                "report_date": report_date,
                "hour_index": _integer(item.get("index")),
            },
            {
                "currency": _currency(money),
                "gmv": _decimal(money),
                "visitors": _integer(item.get("visitors")),
                "customers": _integer(item.get("customers")),
                "items_sold": _integer(item.get("items_sold")),
                "latest_available_timestamp": latest,
                "raw_json": deepcopy(item),
            },
        )
        stats.upserted += 1

    token: str | None = None
    seen_video_tokens: set[str] = set()
    for _ in range(1000):
        result = await client.video_performance(report, end, page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "videos"):
            video_id = _text(item.get("id"), 128)
            if not video_id:
                continue
            creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
            money = item.get("gmv")
            stats.seen += 1
            _upsert(
                db,
                TikTokShopVideoDailyMetric,
                {**_scope(client.shop), "report_date": report_date, "video_id": video_id},
                {
                    "title": _text(item.get("title"), 1024),
                    "creator_open_id": _text(creator.get("open_id"), 192),
                    "creator_username": _text(
                        creator.get("user_name") or creator.get("nick_name") or item.get("username"),
                        255,
                    ),
                    "author_type": _text(creator.get("author_type"), 64),
                    "video_post_time": _datetime(item.get("video_post_time")),
                    "duration_seconds": _integer(item.get("duration")),
                    "currency": _currency(money),
                    "gmv": _decimal(money),
                    "gpm": _decimal(item.get("gpm")),
                    "views": _integer(item.get("views")),
                    "sku_orders": _integer(item.get("sku_orders")),
                    "items_sold": _integer(item.get("items_sold")),
                    "avg_customers": _integer(item.get("avg_customers")),
                    "click_through_rate": _decimal(item.get("click_through_rate")),
                    "products_json": list(item.get("products") or []),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_analytics_token(
            result.data,
            seen_video_tokens,
            dataset="video",
            request_id=result.request_id,
        )
        if not token:
            break
    else:
        raise APIError(
            "TIKTOK_SHOP_PAGINATION_LIMIT",
            "TikTok Shop video analytics exceeded the safe page limit.",
            502,
            data={"retryable": True},
        )

    video_channel_totals: dict[str, Any] = {
        "currency": "USD",
        "gmv": Decimal("0"),
        "sku_orders": 0,
        "estimated_customers": 0,
        "product_impressions": 0,
        "product_clicks": 0,
    }
    product_latest_available_date: str | None = None
    product_request_id: str | None = None
    token = None
    seen_product_tokens: set[str] = set()
    for _ in range(1000):
        result = await client.product_performance(report, end, page_token=token)
        stats.absorb(result)
        product_request_id = result.request_id or product_request_id
        if isinstance(result.data, dict):
            product_latest_available_date = _text(
                result.data.get("latest_available_date"), 32
            ) or product_latest_available_date
        for item in _rows(result.data, "products"):
            product_id = _text(item.get("id"), 128)
            if not product_id:
                continue
            total = _performance_total(item)
            money = total.get("gmv")
            stats.seen += 1
            _upsert(
                db,
                TikTokShopProductDailyMetric,
                {**_scope(client.shop), "report_date": report_date, "product_id": product_id},
                {
                    "currency": _currency(money),
                    "gmv": _decimal(money),
                    "orders": _integer(total.get("orders")),
                    "sku_orders": _integer(total.get("sku_orders")),
                    "items_sold": _integer(total.get("items_sold")),
                    "product_impressions": _integer(total.get("product_impressions")),
                    "product_clicks": _integer(total.get("product_clicks")),
                    "click_through_rate": _decimal(total.get("ctr")),
                    "add_cart_count": _integer(total.get("add_cart_count")),
                    "add_cart_rate": _decimal(total.get("add_cart_rate")),
                    "click_order_rate": _decimal(total.get("click_order_rate")),
                    "refund_amount": _decimal(total.get("refunds")),
                    "metrics_json": deepcopy(item),
                },
            )
            stats.upserted += 1
            for channel, field_name in _PRODUCT_CHANNEL_FIELDS.items():
                channel_performance = item.get(field_name)
                if not isinstance(channel_performance, dict):
                    continue
                if channel in {"seller_video", "affiliate_video"}:
                    _video_channel_totals(video_channel_totals, channel_performance)
                _upsert(
                    db,
                    TikTokShopProductChannelDailyMetric,
                    {
                        **_scope(client.shop),
                        "report_date": report_date,
                        "product_id": product_id,
                        "channel": channel,
                    },
                    _channel_metric_values(channel_performance),
                )
                stats.upserted += 1
        token = _next_analytics_token(
            result.data,
            seen_product_tokens,
            dataset="product",
            request_id=result.request_id,
        )
        if not token:
            break
    else:
        raise APIError(
            "TIKTOK_SHOP_PAGINATION_LIMIT",
            "TikTok Shop product analytics exceeded the safe page limit.",
            502,
            data={"retryable": True},
        )

    if overview_error is not None:
        shop_video_gmv: Decimal | None = None
        shop_video_currency: str | None = None
        shop_request_id: str | None = None
        shop_error: dict[str, Any] | None = None
        try:
            shop_result = await client.shop_performance(report, end)
            stats.absorb(shop_result)
            shop_request_id = shop_result.request_id
            shop_video_gmv, shop_video_currency = _shop_video_gmv(shop_result.data)
        except APIError as exc:
            shop_error = {
                "provider_code": (exc.data or {}).get("provider_code")
                if isinstance(exc.data, dict)
                else None,
                "request_id": (exc.data or {}).get("request_id")
                if isinstance(exc.data, dict)
                else None,
            }
            logger.warning(
                "TikTok Shop realtime shop performance fallback unavailable "
                "workspace_id=%s shop_row_id=%s report_date=%s provider_code=%s request_id=%s",
                client.workspace_id,
                client.shop.id,
                report,
                shop_error.get("provider_code"),
                shop_error.get("request_id"),
            )
        money = {
            "amount": str(
                shop_video_gmv
                if shop_video_gmv is not None
                else video_channel_totals["gmv"]
            ),
            "currency": shop_video_currency or video_channel_totals["currency"] or "USD",
        }
        fallback_error = {
            "provider_code": (overview_error.data or {}).get("provider_code")
            if isinstance(overview_error.data, dict)
            else None,
            "request_id": (overview_error.data or {}).get("request_id")
            if isinstance(overview_error.data, dict)
            else None,
            "shop_fallback_error": shop_error,
        }
        fallback_item = {
            "start_date": report,
            "end_date": end,
            "gmv": money,
            "avg_customers": 0,
            "product_impressions": video_channel_totals["product_impressions"],
            "product_clicks": video_channel_totals["product_clicks"],
            "sku_orders": video_channel_totals["sku_orders"],
            # The official overview CTR denominator is video views. Product
            # performance does not expose that denominator, so do not invent it.
            "click_through_rate": None,
        }
        _upsert(
            db,
            TikTokShopVideoOverviewDailyMetric,
            {**_scope(client.shop), "report_date": report_date},
            {
                "currency": _currency(money),
                "gmv": _decimal(money),
                "avg_customers": 0,
                "product_impressions": video_channel_totals["product_impressions"],
                "product_clicks": video_channel_totals["product_clicks"],
                "sku_orders": video_channel_totals["sku_orders"],
                "click_through_rate": None,
                "raw_json": _overview_raw(
                    fallback_item,
                    source="shop_and_product_video_channels",
                    provisional=True,
                    latest_available_date=product_latest_available_date,
                    request_id=shop_request_id or product_request_id,
                    fallback_error=fallback_error,
                ),
            },
        )
        stats.seen += 1
        stats.upserted += 1

    token = None
    seen_sku_tokens: set[str] = set()
    for _ in range(1000):
        result = await client.sku_performance(report, end, page_token=token)
        stats.absorb(result)
        for item in _rows(result.data, "skus"):
            sku_id = _text(item.get("id"), 128)
            if not sku_id:
                continue
            money = item.get("gmv")
            stats.seen += 1
            _upsert(
                db,
                TikTokShopSkuDailyMetric,
                {**_scope(client.shop), "report_date": report_date, "sku_id": sku_id},
                {
                    "product_id": _text(item.get("product_id"), 128),
                    "currency": _currency(money),
                    "gmv": _decimal(money),
                    "sku_orders": _integer(item.get("sku_orders")),
                    "units_sold": _integer(item.get("units_sold")),
                    "raw_json": deepcopy(item),
                },
            )
            stats.upserted += 1
        token = _next_analytics_token(
            result.data,
            seen_sku_tokens,
            dataset="SKU",
            request_id=result.request_id,
        )
        if not token:
            break
    else:
        raise APIError(
            "TIKTOK_SHOP_PAGINATION_LIMIT",
            "TikTok Shop SKU analytics exceeded the safe page limit.",
            502,
            data={"retryable": True},
        )

    live = await client.live_overview(report, end, today=mutable_day)
    stats.absorb(live)
    performance = live.data.get("performance") if isinstance(live.data, dict) else {}
    intervals = performance.get("intervals") if isinstance(performance, dict) else []
    for item in intervals or []:
        if not isinstance(item, dict):
            continue
        day = date.fromisoformat(str(item.get("start_date") or report)[:10])
        money = item.get("gmv")
        stats.seen += 1
        _upsert(
            db,
            TikTokShopLiveDailyMetric,
            {**_scope(client.shop), "report_date": day},
            {
                "currency": _currency(money),
                "gmv": _decimal(money),
                "customers": _integer(item.get("customers")),
                "items_sold": _integer(item.get("items_sold")),
                "sku_orders": _integer(item.get("sku_orders")),
                "click_through_rate": _decimal(item.get("click_through_rate")),
                "click_to_order_rate": _decimal(item.get("click_to_order_rate")),
                "raw_json": deepcopy(item),
            },
        )
        stats.upserted += 1


async def sync_analytics(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
    *,
    start_date: date | None,
    end_date_exclusive: date | None,
) -> tuple[date, date]:
    if start_date is not None or end_date_exclusive is not None:
        start, end = _date_range(
            client.shop,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            default_days=2,
            max_days=31,
        )
        dates = [start + timedelta(days=offset) for offset in range((end - start).days)]
    else:
        today = shop_today(client.shop)
        yesterday = today - timedelta(days=1)
        finalized_after_rollover = int(
            db.scalar(
                select(func.count(TikTokShopSyncRun.id)).where(
                    TikTokShopSyncRun.workspace_id == int(client.workspace_id),
                    TikTokShopSyncRun.shop_row_id == int(client.shop.id),
                    TikTokShopSyncRun.domain == "analytics",
                    TikTokShopSyncRun.status == "success",
                    TikTokShopSyncRun.range_start <= yesterday,
                    TikTokShopSyncRun.range_end_exclusive > yesterday,
                    TikTokShopSyncRun.completed_at >= _local_midnight_utc(today, client.shop),
                )
            )
            or 0
        )
        dates = [today] if finalized_after_rollover else [yesterday, today]
        start, end = dates[0], dates[-1] + timedelta(days=1)

    for current in dates:
        # A provider failure must not commit a half-written natural day. A
        # completed previous day may remain, while the failed day rolls back to
        # its prior coherent snapshot.
        with db.begin_nested():
            await _sync_analytics_date(db, client, stats, current)
            db.flush()
    return start, end


async def sync_global_products(
    db: Session,
    client: TikTokShopAPIClient,
    stats: SyncStats,
) -> None:
    token: str | None = None
    for _ in range(1000):
        result = await client.search_global_products(page_token=token)
        stats.absorb(result)
        rows = _rows(result.data, "global_products") or _rows(result.data, "products")
        for summary in rows:
            product_id = _text(summary.get("id") or summary.get("global_product_id"), 128)
            if not product_id:
                continue
            detail = await client.get_global_product(product_id)
            stats.absorb(detail)
            item = detail.data if isinstance(detail.data, dict) else summary
            stats.seen += 1
            _upsert(
                db,
                TikTokShopGlobalProduct,
                {
                    "workspace_id": int(client.shop.workspace_id),
                    "account_id": int(client.shop.account_id),
                    "global_product_id": product_id,
                },
                {
                    "title": _text(item.get("title"), 1024),
                    "status": _text(item.get("status") or item.get("product_status"), 64),
                    "raw_json": deepcopy(dict(item)),
                },
            )
            stats.upserted += 1
        token = _next_token(result.data)
        if not token:
            break


async def sync_domain(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
    shop_row_id: int,
    domain: str,
    trigger: str = "scheduled",
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    http_client: Any | None = None,
) -> TikTokShopSyncRun:
    normalized = str(domain).strip().lower()
    if normalized not in SUPPORTED_DOMAINS:
        raise APIError(
            "INVALID_SYNC_DOMAIN",
            f"Unsupported TikTok Shop sync domain: {normalized}.",
            400,
        )
    shop = db.get(OAuthTikTokShopShop, int(shop_row_id))
    if (
        not shop
        or int(shop.workspace_id) != int(workspace_id)
        or int(shop.account_id) != int(account_id)
        or not bool(shop.is_active)
    ):
        raise APIError("TIKTOK_SHOP_NOT_FOUND", "Active TikTok Shop not found.", 404)

    run = TikTokShopSyncRun(
        workspace_id=int(workspace_id),
        account_id=int(account_id),
        shop_row_id=int(shop_row_id),
        domain=normalized,
        trigger=str(trigger or "scheduled")[:32],
        status="running",
        range_start=start_date,
        range_end_exclusive=end_date_exclusive,
        started_at=_utcnow(),
    )
    db.add(run)
    db.flush()
    stats = SyncStats()
    try:
        client = await TikTokShopAPIClient.create(
            db,
            workspace_id=int(workspace_id),
            account_id=int(account_id),
            shop_row_id=int(shop_row_id),
            http_client=http_client,
        )
        async with client:
            actual_range: tuple[date, date] | None = None
            if normalized == "catalog":
                await sync_catalog(db, client, stats)
                db.flush()
                from app.services.commerce_analytics import (
                    sync_exact_product_mappings,
                )

                sync_exact_product_mappings(
                    db,
                    workspace_id=int(workspace_id),
                    shop_row_id=int(shop_row_id),
                )
            elif normalized == "orders":
                actual_range = await sync_orders(
                    db,
                    client,
                    stats,
                    start_date=start_date,
                    end_date_exclusive=end_date_exclusive,
                )
            elif normalized == "finance":
                actual_range = await sync_finance(
                    db,
                    client,
                    stats,
                    start_date=start_date,
                    end_date_exclusive=end_date_exclusive,
                )
            elif normalized == "promotions":
                await sync_promotions(db, client, stats)
            elif normalized == "analytics":
                actual_range = await sync_analytics(
                    db,
                    client,
                    stats,
                    start_date=start_date,
                    end_date_exclusive=end_date_exclusive,
                )
            else:
                await sync_global_products(db, client, stats)
            if actual_range:
                run.range_start, run.range_end_exclusive = actual_range
        run.status = "success"
        run.pages_fetched = stats.pages
        run.rows_seen = stats.seen
        run.rows_upserted = stats.upserted
        run.provider_request_id = stats.request_id
        run.completed_at = _utcnow()
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    except APIError as exc:
        run.status = "failed"
        run.provider_code = str((exc.data or {}).get("provider_code") or exc.code)[:64]
        run.provider_request_id = _text((exc.data or {}).get("request_id"), 128)
        run.error_message = str(exc.message)[:2000]
        run.pages_fetched = stats.pages
        run.rows_seen = stats.seen
        run.rows_upserted = stats.upserted
        run.completed_at = _utcnow()
        db.add(run)
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise


def serialize_model(row: Any, *, include_raw: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        key = str(column.name)
        if key.endswith("_cipher") or key in {"raw_json", "metrics_json"} and not include_raw:
            continue
        value = getattr(row, key)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = str(value)
        result[key] = value
    return result


def sum_decimal(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value or Decimal("0") for value in values), Decimal("0"))
