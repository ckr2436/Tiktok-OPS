"""TikTok Shop API-backed order analytics for GMV Max and Commerce."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopOrder,
    TikTokShopOrderFinanceSummary,
    TikTokShopOrderLine,
    TikTokShopSyncRun,
)


class CommerceOrderError(ValueError):
    pass


_CANCELLED_ORDER_STATUSES = {"CANCELLED", "CANCELED"}


def validate_timezone(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        raise CommerceOrderError(
            "Advertiser timezone is unavailable. Sync TikTok Business account "
            "metadata before running time-based reporting."
        )
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise CommerceOrderError(f"Unsupported IANA timezone: {candidate}") from exc
    return candidate


def _shop_for_store(
    db: Session,
    *,
    workspace_id: int,
    store_id: str,
) -> OAuthTikTokShopShop:
    shop = db.scalar(
        select(OAuthTikTokShopShop)
        .where(
            OAuthTikTokShopShop.workspace_id == int(workspace_id),
            OAuthTikTokShopShop.shop_id == str(store_id),
            OAuthTikTokShopShop.is_active.is_(True),
        )
        .order_by(OAuthTikTokShopShop.last_seen_at.desc())
        .limit(1)
    )
    if not shop:
        raise CommerceOrderError(
            "No active TikTok Shop authorization matches this GMV Max store."
        )
    return shop


def _utc_bounds(
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    if start_date > end_date:
        raise CommerceOrderError("start_date must not exceed end_date")
    if (end_date - start_date).days > 366:
        raise CommerceOrderError("Order analytics supports up to 367 days")
    zone = ZoneInfo(validate_timezone(timezone_name))
    start = datetime.combine(start_date, datetime_time.min, zone)
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1),
        datetime_time.min,
        zone,
    )
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end_exclusive.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _eligible_order_filters(
    *,
    workspace_id: int,
    shop_row_id: int,
    start_utc: datetime,
    end_utc_exclusive: datetime,
) -> tuple[Any, ...]:
    return (
        TikTokShopOrder.workspace_id == int(workspace_id),
        TikTokShopOrder.shop_row_id == int(shop_row_id),
        TikTokShopOrder.paid_at.is_not(None),
        TikTokShopOrder.paid_at >= start_utc,
        TikTokShopOrder.paid_at < end_utc_exclusive,
        TikTokShopOrder.is_sample_order.is_(False),
        func.upper(func.coalesce(TikTokShopOrder.status, "")).not_in(
            _CANCELLED_ORDER_STATUSES
        ),
    )


def _money(value: Decimal | int | float | None) -> float:
    return round(float(value or 0), 2)


def _hourly_profile(
    db: Session,
    *,
    filters: tuple[Any, ...],
    timezone_name: str,
    total_orders: int,
) -> list[dict[str, Any]]:
    zone = ZoneInfo(timezone_name)
    dialect = str(db.get_bind().dialect.name)
    hour_expression = (
        func.strftime("%Y-%m-%d %H:00:00", TikTokShopOrder.paid_at)
        if dialect == "sqlite"
        else func.date_format(TikTokShopOrder.paid_at, "%Y-%m-%d %H:00:00")
    )
    rows = db.execute(
        select(
            hour_expression.label("paid_hour_utc"),
            func.count(TikTokShopOrder.id).label("orders"),
            func.coalesce(func.sum(TikTokShopOrder.total_amount), 0).label(
                "net_revenue"
            ),
        )
        .where(*filters)
        .group_by(hour_expression)
        .order_by(hour_expression)
    ).mappings().all()
    hourly: dict[int, dict[str, Decimal | int]] = defaultdict(
        lambda: {"orders": 0, "net_revenue": Decimal("0")}
    )
    for row in rows:
        raw_hour = row.get("paid_hour_utc")
        if not raw_hour:
            continue
        paid_hour = (
            raw_hour
            if isinstance(raw_hour, datetime)
            else datetime.fromisoformat(str(raw_hour))
        )
        local_hour = paid_hour.replace(tzinfo=timezone.utc).astimezone(zone).hour
        hourly[local_hour]["orders"] = int(hourly[local_hour]["orders"]) + int(
            row.get("orders") or 0
        )
        hourly[local_hour]["net_revenue"] = Decimal(
            hourly[local_hour]["net_revenue"]
        ) + Decimal(row.get("net_revenue") or 0)

    prior = max(1.0, total_orders / 48.0)
    confidence = total_orders / (total_orders + 48.0)
    profile: list[dict[str, Any]] = []
    for hour in range(24):
        bucket = hourly[hour]
        orders = int(bucket["orders"])
        raw_share = (orders + prior) / (total_orders + prior * 24)
        relative_rate = raw_share * 24
        multiplier = max(0.75, min(1.5, 1 + confidence * (relative_rate - 1)))
        profile.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00-{hour:02d}:59",
                "orders": orders,
                "net_revenue": _money(Decimal(bucket["net_revenue"])),
                "confidence": round(confidence, 4),
                "delivery_multiplier": round(multiplier, 4),
            }
        )
    return profile


def _latest_order_sync(
    db: Session,
    *,
    workspace_id: int,
    shop_row_id: int,
) -> TikTokShopSyncRun | None:
    return db.scalar(
        select(TikTokShopSyncRun)
        .where(
            TikTokShopSyncRun.workspace_id == int(workspace_id),
            TikTokShopSyncRun.shop_row_id == int(shop_row_id),
            TikTokShopSyncRun.domain == "orders",
            TikTokShopSyncRun.status == "success",
        )
        .order_by(TikTokShopSyncRun.completed_at.desc())
        .limit(1)
    )


def order_summary(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    store_id: str,
    start_date: date,
    end_date: date,
    advertiser_timezone: str,
) -> dict[str, Any]:
    """Aggregate API orders into the advertiser reporting timezone.

    ``auth_id`` and ``advertiser_id`` remain accepted so existing guard and
    report callers can migrate without changing their scope contract. Shop
    ownership is resolved independently by workspace + provider shop ID.
    """

    del auth_id, advertiser_id
    timezone_name = validate_timezone(advertiser_timezone)
    shop = _shop_for_store(
        db,
        workspace_id=int(workspace_id),
        store_id=str(store_id),
    )
    start_utc, end_utc_exclusive = _utc_bounds(
        start_date=start_date,
        end_date=end_date,
        timezone_name=timezone_name,
    )
    valid_filters = _eligible_order_filters(
        workspace_id=int(workspace_id),
        shop_row_id=int(shop.id),
        start_utc=start_utc,
        end_utc_exclusive=end_utc_exclusive,
    )

    valid_summary = db.execute(
        select(
            func.count(TikTokShopOrder.id),
            func.coalesce(func.sum(TikTokShopOrder.total_amount), 0),
            func.min(TikTokShopOrder.currency),
        ).where(*valid_filters)
    ).one()
    total_orders = int(valid_summary[0] or 0)
    net_revenue = Decimal(valid_summary[1] or 0)
    currency = str(valid_summary[2] or "USD")

    cancelled_filters = (
        TikTokShopOrder.workspace_id == int(workspace_id),
        TikTokShopOrder.shop_row_id == int(shop.id),
        TikTokShopOrder.paid_at.is_not(None),
        TikTokShopOrder.paid_at >= start_utc,
        TikTokShopOrder.paid_at < end_utc_exclusive,
        TikTokShopOrder.is_sample_order.is_(False),
        func.upper(func.coalesce(TikTokShopOrder.status, "")).in_(
            _CANCELLED_ORDER_STATUSES
        ),
    )
    cancelled_summary = db.execute(
        select(
            func.count(TikTokShopOrder.id),
            func.coalesce(func.sum(TikTokShopOrder.total_amount), 0),
        ).where(*cancelled_filters)
    ).one()
    cancelled_count = int(cancelled_summary[0] or 0)
    cancelled_amount = Decimal(cancelled_summary[1] or 0)

    finance_summary = db.execute(
        select(
            func.count(TikTokShopOrderFinanceSummary.id),
            func.coalesce(
                func.sum(TikTokShopOrderFinanceSummary.revenue_amount),
                0,
            ),
            func.coalesce(
                func.sum(TikTokShopOrderFinanceSummary.fee_tax_amount),
                0,
            ),
            func.coalesce(
                func.sum(TikTokShopOrderFinanceSummary.shipping_cost_amount),
                0,
            ),
            func.coalesce(
                func.sum(TikTokShopOrderFinanceSummary.settlement_amount),
                0,
            ),
        )
        .join(
            TikTokShopOrder,
            (
                (
                    TikTokShopOrder.shop_row_id
                    == TikTokShopOrderFinanceSummary.shop_row_id
                )
                & (
                    TikTokShopOrder.order_id
                    == TikTokShopOrderFinanceSummary.order_id
                )
            ),
        )
        .where(*valid_filters)
    ).one()
    finance_order_count = int(finance_summary[0] or 0)

    # Group by UTC hour in SQL. Converting bounded buckets in Python preserves
    # daylight-saving correctness without materializing individual orders.
    hour_profile = _hourly_profile(
        db,
        filters=valid_filters,
        timezone_name=timezone_name,
        total_orders=total_orders,
    )

    product_rows = db.execute(
        select(
            TikTokShopOrderLine.product_id,
            func.max(TikTokShopOrderLine.product_name).label("product_name"),
            func.count(func.distinct(TikTokShopOrder.order_id)).label("orders"),
            func.coalesce(func.sum(TikTokShopOrderLine.quantity), 0).label(
                "quantity"
            ),
            func.coalesce(
                func.sum(
                    TikTokShopOrderLine.sale_price * TikTokShopOrderLine.quantity
                ),
                0,
            ).label("item_revenue"),
        )
        .join(
            TikTokShopOrder,
            (
                (TikTokShopOrder.shop_row_id == TikTokShopOrderLine.shop_row_id)
                & (TikTokShopOrder.order_id == TikTokShopOrderLine.order_id)
            ),
        )
        .where(*valid_filters)
        .group_by(TikTokShopOrderLine.product_id)
        .order_by(func.sum(TikTokShopOrderLine.sale_price * TikTokShopOrderLine.quantity).desc())
    ).mappings().all()
    products = []
    for row in product_rows:
        quantity = int(row.get("quantity") or 0)
        item_revenue = Decimal(row.get("item_revenue") or 0)
        products.append(
            {
                "product_id": str(row.get("product_id") or "unknown"),
                "product_name": str(row.get("product_name") or ""),
                "orders": int(row.get("orders") or 0),
                "quantity": quantity,
                "item_revenue": _money(item_revenue),
                "average_item_price": _money(
                    item_revenue / max(1, quantity)
                ),
            }
        )

    latest_sync = _latest_order_sync(
        db,
        workspace_id=int(workspace_id),
        shop_row_id=int(shop.id),
    )
    return {
        "range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": timezone_name,
        },
        "source": "tiktok_shop_api",
        "shop_timezone": str(shop.timezone_name),
        "shop_timezone_source": str(shop.timezone_source),
        "shop_id": int(shop.id),
        "provider_shop_id": str(shop.shop_id),
        "currency": currency,
        "order_count": total_orders,
        "cancelled_order_count": cancelled_count,
        "gross_amount": _money(net_revenue + cancelled_amount),
        "cancellation_amount": _money(cancelled_amount),
        "refund_amount": _money(cancelled_amount),
        "net_revenue": _money(net_revenue),
        "average_order_value": _money(net_revenue / max(1, total_orders)),
        "hourly_profile": hour_profile,
        "peak_hours": sorted(
            hour_profile,
            key=lambda item: (
                item["delivery_multiplier"],
                item["orders"],
            ),
            reverse=True,
        )[:5],
        "products": products,
        "channels": [],
        "finance": {
            "covered_orders": finance_order_count,
            "coverage_ratio": round(
                finance_order_count / max(1, total_orders),
                4,
            ),
            "revenue": _money(finance_summary[1]),
            "fee_and_tax": _money(finance_summary[2]),
            "shipping_cost": _money(finance_summary[3]),
            "settlement": _money(finance_summary[4]),
        },
        "last_synced_at": (
            latest_sync.completed_at.isoformat()
            if latest_sync and latest_sync.completed_at
            else None
        ),
        "data_quality": {
            "orders": "api",
            "refunds": "cancelled_orders_only",
            "finance": (
                "settlement_api_complete"
                if total_orders > 0 and finance_order_count >= total_orders
                else "settlement_api_partial"
            ),
        },
        "model_note": (
            "Hourly delivery multipliers are sample-smoothed and bounded. "
            "Only aggregate Shop API facts are provided to Hermes."
        ),
    }


def current_order_timing_signal(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    store_id: str,
    advertiser_timezone: str,
    now: datetime,
) -> dict[str, Any]:
    """Return a bounded 30-day API-order demand signal without buyer data."""

    del auth_id, advertiser_id
    timezone_name = validate_timezone(advertiser_timezone)
    zone = ZoneInfo(timezone_name)
    aware_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    local_today = aware_now.astimezone(zone).date()
    shop = _shop_for_store(
        db,
        workspace_id=int(workspace_id),
        store_id=str(store_id),
    )
    start_utc, end_utc_exclusive = _utc_bounds(
        start_date=local_today - timedelta(days=29),
        end_date=local_today,
        timezone_name=timezone_name,
    )
    filters = _eligible_order_filters(
        workspace_id=int(workspace_id),
        shop_row_id=int(shop.id),
        start_utc=start_utc,
        end_utc_exclusive=end_utc_exclusive,
    )
    total_orders = int(
        db.scalar(select(func.count(TikTokShopOrder.id)).where(*filters)) or 0
    )
    hour_profile = _hourly_profile(
        db,
        filters=filters,
        timezone_name=timezone_name,
        total_orders=total_orders,
    )
    current_hour = aware_now.astimezone(zone).hour
    profile = {
        int(item["hour"]): item
        for item in hour_profile
    }
    current = profile.get(current_hour, {})
    latest_sync = _latest_order_sync(
        db,
        workspace_id=int(workspace_id),
        shop_row_id=int(shop.id),
    )
    return {
        "source": "tiktok_shop_api",
        "timezone": timezone_name,
        "hour": current_hour,
        "sample_orders_30d": total_orders,
        "hour_orders_30d": int(current.get("orders") or 0),
        "hour_net_revenue_30d": float(current.get("net_revenue") or 0),
        "confidence": float(current.get("confidence") or 0),
        "delivery_multiplier": float(current.get("delivery_multiplier") or 1),
        "last_synced_at": (
            latest_sync.completed_at.isoformat()
            if latest_sync and latest_sync.completed_at
            else None
        ),
    }


__all__ = [
    "CommerceOrderError",
    "current_order_timing_signal",
    "order_summary",
    "validate_timezone",
]
