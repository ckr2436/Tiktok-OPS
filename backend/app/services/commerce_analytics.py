"""Cross-channel commerce aggregation and profitability calculations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.data.models.commerce import (
    CommerceProductCostVersion,
    CommerceProductMapping,
)
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.data.models.gmv_restructured import (
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
)
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopOrder,
    TikTokShopOrderLine,
    TikTokShopProduct,
    TikTokShopSku,
    TikTokShopSyncRun,
)
from app.data.models.ttb_entities import TTBAdvertiser, TTBAdvertiserStoreLink
from app.services.commerce_orders import (
    CommerceOrderError,
    order_summary,
    validate_timezone,
)


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class CommerceScope:
    workspace_id: int
    shop: OAuthTikTokShopShop
    advertiser_id: str
    advertiser_name: str
    reporting_timezone: str
    reporting_timezone_source: str
    currency: str


def _decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _money(value: Any) -> float:
    return round(float(_decimal(value)), 2)


def _rate(value: Any) -> float:
    return round(float(_decimal(value)), 6)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    bottom = _decimal(denominator)
    if bottom <= 0:
        return None
    return round(float(_decimal(numerator) / bottom), 4)


def _active_shops(db: Session, workspace_id: int) -> list[OAuthTikTokShopShop]:
    return list(
        db.scalars(
            select(OAuthTikTokShopShop)
            .where(
                OAuthTikTokShopShop.workspace_id == int(workspace_id),
                OAuthTikTokShopShop.is_active.is_(True),
            )
            .order_by(OAuthTikTokShopShop.shop_name, OAuthTikTokShopShop.id)
        )
    )


def _advertiser_rows(
    db: Session,
    *,
    workspace_id: int,
    provider_store_id: str,
) -> list[dict[str, Any]]:
    campaign_advertiser_ids = set(
        db.scalars(
            select(GmvmaxProductCampaignCatalog.advertiser_id)
            .where(
                GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id),
                GmvmaxProductCampaignCatalog.store_id == str(provider_store_id),
            )
            .distinct()
            .order_by(GmvmaxProductCampaignCatalog.advertiser_id)
        )
    )
    advertiser_ids = set(campaign_advertiser_ids)
    advertiser_ids.update(
        str(value)
        for value in db.scalars(
            select(TTBAdvertiserStoreLink.advertiser_id)
            .where(
                TTBAdvertiserStoreLink.workspace_id == int(workspace_id),
                TTBAdvertiserStoreLink.store_id == str(provider_store_id),
                TTBAdvertiserStoreLink.advertiser_id.is_not(None),
            )
            .distinct()
        )
        if value
    )
    advertiser_ids = sorted(str(value) for value in advertiser_ids if value)
    if not advertiser_ids:
        return []
    activity_rows = db.execute(
        select(
            GmvProductMetricsDaily.advertiser_id,
            func.max(GmvProductMetricsDaily.stat_time_day).label(
                "latest_metric_date"
            ),
            func.coalesce(func.sum(GmvProductMetricsDaily.cost_cents), 0).label(
                "lifetime_cost_cents"
            ),
        )
        .where(
            GmvProductMetricsDaily.workspace_id == int(workspace_id),
            GmvProductMetricsDaily.store_id == str(provider_store_id),
            GmvProductMetricsDaily.advertiser_id.in_(advertiser_ids),
        )
        .group_by(GmvProductMetricsDaily.advertiser_id)
    ).all()
    activity = {
        str(advertiser_id): {
            "latest_metric_date": latest_metric_date,
            "lifetime_cost_cents": int(lifetime_cost_cents or 0),
        }
        for advertiser_id, latest_metric_date, lifetime_cost_cents in activity_rows
    }
    rows = db.scalars(
        select(TTBAdvertiser)
        .where(
            TTBAdvertiser.workspace_id == int(workspace_id),
            TTBAdvertiser.advertiser_id.in_(advertiser_ids),
        )
        .order_by(TTBAdvertiser.last_seen_at.desc())
    )
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        advertiser_id = str(row.advertiser_id)
        if advertiser_id in deduped:
            continue
        raw_timezone = str(row.display_timezone or row.timezone or "").strip()
        timezone_error = None
        try:
            timezone_name = (
                validate_timezone(raw_timezone) if raw_timezone else None
            )
        except CommerceOrderError as exc:
            timezone_name = None
            timezone_error = str(exc)
        advertiser_activity = activity.get(advertiser_id, {})
        latest_metric_date = advertiser_activity.get("latest_metric_date")
        deduped[advertiser_id] = {
            "advertiser_id": advertiser_id,
            "name": str(row.display_name or row.name or advertiser_id),
            "timezone": timezone_name,
            "timezone_source": (
                "tiktok_business_api" if timezone_name else "missing"
            ),
            "provider_timezone": str(row.timezone or ""),
            "currency": str(row.currency or "USD"),
            "has_campaigns": advertiser_id in campaign_advertiser_ids,
            "has_ad_metrics": advertiser_id in activity,
            "latest_metric_date": (
                latest_metric_date.isoformat()
                if isinstance(latest_metric_date, date)
                else None
            ),
            "lifetime_cost_cents": int(
                advertiser_activity.get("lifetime_cost_cents") or 0
            ),
            "timezone_error": timezone_error,
        }
    for advertiser_id in advertiser_ids:
        key = str(advertiser_id)
        advertiser_activity = activity.get(key, {})
        latest_metric_date = advertiser_activity.get("latest_metric_date")
        deduped.setdefault(
            key,
            {
                "advertiser_id": key,
                "name": key,
                "timezone": None,
                "timezone_source": "missing",
                "provider_timezone": "",
                "currency": "USD",
                "has_campaigns": key in campaign_advertiser_ids,
                "has_ad_metrics": key in activity,
                "latest_metric_date": (
                    latest_metric_date.isoformat()
                    if isinstance(latest_metric_date, date)
                    else None
                ),
                "lifetime_cost_cents": int(
                    advertiser_activity.get("lifetime_cost_cents") or 0
                ),
                "timezone_error": "TikTok Business account metadata is missing.",
            },
        )
    return sorted(
        deduped.values(),
        key=lambda item: (
            0 if item.get("timezone") else 1,
            0 if item.get("has_ad_metrics") else 1,
            -(
                date.fromisoformat(str(item["latest_metric_date"])).toordinal()
                if item.get("latest_metric_date")
                else 0
            ),
            -int(item.get("lifetime_cost_cents") or 0),
            0 if item.get("has_campaigns") else 1,
            str(item.get("name") or item.get("advertiser_id") or "").lower(),
        ),
    )


def commerce_context(db: Session, *, workspace_id: int) -> dict[str, Any]:
    shops = _active_shops(db, int(workspace_id))
    payload_shops: list[dict[str, Any]] = []
    for shop in shops:
        payload_shops.append(
            {
                "id": int(shop.id),
                "provider_shop_id": str(shop.shop_id),
                "shop_code": str(shop.shop_code or ""),
                "name": str(shop.shop_name or shop.shop_id),
                "region": str(shop.region or ""),
                "timezone": str(shop.timezone_name),
                "timezone_source": str(shop.timezone_source),
                "timezone_locked": bool(shop.timezone_locked),
                "timezone_verified_at": (
                    shop.timezone_verified_at.isoformat()
                    if shop.timezone_verified_at
                    else None
                ),
                "advertisers": _advertiser_rows(
                    db,
                    workspace_id=int(workspace_id),
                    provider_store_id=str(shop.shop_id),
                ),
            }
        )
    default_shop = payload_shops[0] if payload_shops else None
    default_advertiser = (
        default_shop["advertisers"][0]
        if default_shop and default_shop["advertisers"]
        else None
    )
    return {
        "shops": payload_shops,
        "default_shop_id": default_shop["id"] if default_shop else None,
        "default_advertiser_id": (
            default_advertiser["advertiser_id"] if default_advertiser else None
        ),
        "default_reporting_timezone": (
            default_advertiser["timezone"]
            if default_advertiser
            else None
        ),
        "order_source": "tiktok_shop_api",
    }


def resolve_scope(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int | None,
    advertiser_id: str | None,
) -> CommerceScope:
    shops = _active_shops(db, int(workspace_id))
    shop = next(
        (item for item in shops if shop_id is not None and int(item.id) == int(shop_id)),
        shops[0] if shop_id is None and shops else None,
    )
    if not shop:
        raise CommerceOrderError("No active TikTok Shop authorization is available.")
    advertisers = _advertiser_rows(
        db,
        workspace_id=int(workspace_id),
        provider_store_id=str(shop.shop_id),
    )
    advertiser = next(
        (
            item
            for item in advertisers
            if advertiser_id is not None
            and item["advertiser_id"] == str(advertiser_id)
        ),
        advertisers[0] if advertiser_id is None and advertisers else None,
    )
    if not advertiser:
        raise CommerceOrderError(
            "No GMV Max advertiser is mapped to this TikTok Shop."
        )
    if not advertiser.get("timezone"):
        raise CommerceOrderError(
            "Advertiser timezone is unavailable. Sync TikTok Business account "
            "metadata before running cross-channel reporting."
        )
    timezone_name = validate_timezone(str(advertiser["timezone"]))
    return CommerceScope(
        workspace_id=int(workspace_id),
        shop=shop,
        advertiser_id=str(advertiser["advertiser_id"]),
        advertiser_name=str(advertiser["name"]),
        reporting_timezone=timezone_name,
        reporting_timezone_source=str(
            advertiser.get("timezone_source") or "tiktok_business_api"
        ),
        currency=str(advertiser["currency"] or "USD"),
    )


def date_range(
    *,
    scope: CommerceScope,
    start_date: date | None,
    end_date: date | None,
    default_days: int = 7,
    max_days: int = 367,
) -> tuple[date, date, datetime, datetime]:
    zone = ZoneInfo(scope.reporting_timezone)
    today = datetime.now(zone).date()
    effective_end = end_date or today
    effective_start = start_date or (
        effective_end - timedelta(days=max(1, int(default_days)) - 1)
    )
    if effective_start > effective_end:
        raise CommerceOrderError("start_date must not exceed end_date")
    if (effective_end - effective_start).days >= max_days:
        raise CommerceOrderError(
            f"Date range cannot exceed {max_days} calendar days."
        )
    start_utc = datetime.combine(
        effective_start,
        datetime_time.min,
        zone,
    ).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(
        effective_end + timedelta(days=1),
        datetime_time.min,
        zone,
    ).astimezone(timezone.utc).replace(tzinfo=None)
    return effective_start, effective_end, start_utc, end_utc


def sync_exact_product_mappings(
    db: Session,
    *,
    workspace_id: int,
    shop_row_id: int,
) -> int:
    shop = db.get(OAuthTikTokShopShop, int(shop_row_id))
    if not shop or int(shop.workspace_id) != int(workspace_id):
        return 0
    product_ids = set(
        str(value)
        for value in db.scalars(
            select(TikTokShopProduct.product_id).where(
                TikTokShopProduct.workspace_id == int(workspace_id),
                TikTokShopProduct.shop_row_id == int(shop_row_id),
            )
        )
    )
    candidates = db.execute(
        select(
            GmvmaxProductCampaignItemGroup.auth_id,
            GmvmaxProductCampaignItemGroup.advertiser_id,
            GmvmaxProductCampaignItemGroup.store_id,
            GmvmaxProductCampaignItemGroup.item_group_id,
        )
        .where(
            GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id),
            GmvmaxProductCampaignItemGroup.store_id == str(shop.shop_id),
            GmvmaxProductCampaignItemGroup.item_group_id.in_(product_ids),
        )
        .distinct()
    ).all()
    existing = set(
        db.execute(
            select(
                CommerceProductMapping.shop_product_id,
                CommerceProductMapping.advertiser_id,
                CommerceProductMapping.item_group_id,
            ).where(
                CommerceProductMapping.workspace_id == int(workspace_id),
                CommerceProductMapping.shop_row_id == int(shop_row_id),
            )
        ).all()
    )
    inserted = 0
    for auth_id, advertiser_id, store_id, item_group_id in candidates:
        key = (str(item_group_id), str(advertiser_id), str(item_group_id))
        if key in existing:
            continue
        db.add(
            CommerceProductMapping(
                workspace_id=int(workspace_id),
                shop_row_id=int(shop_row_id),
                shop_product_id=str(item_group_id),
                business_auth_id=int(auth_id),
                advertiser_id=str(advertiser_id),
                store_id=str(store_id),
                item_group_id=str(item_group_id),
                source="exact_product_id",
                confidence=Decimal("1"),
                is_active=True,
            )
        )
        existing.add(key)
        inserted += 1
    if inserted:
        db.flush()
    return inserted


def _ad_metrics(
    db: Session,
    *,
    scope: CommerceScope,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, dict[str, Decimal | int]], dict[date, dict[str, Decimal | int]]]:
    advertiser_today = datetime.now(
        ZoneInfo(scope.reporting_timezone)
    ).date()
    product_selects = []
    for metric_model, stat_expression, source_priority in (
        (
            GmvProductMetricsDaily,
            GmvProductMetricsDaily.stat_time_day,
            case(
                (GmvProductMetricsDaily.source_observed_at.is_(None), 0),
                else_=2,
            ),
        ),
        (
            GmvProductMetricsHourly,
            func.date(GmvProductMetricsHourly.stat_time_hour),
            case(
                (
                    func.date(GmvProductMetricsHourly.stat_time_hour)
                    >= advertiser_today,
                    3,
                ),
                else_=1,
            ),
        ),
    ):
        product_selects.append(
            select(
                metric_model.campaign_id.label("campaign_id"),
                metric_model.item_group_id.label("item_group_id"),
                stat_expression.label("stat_time_day"),
                source_priority.label("source_priority"),
                literal(metric_model.__tablename__).label("source_name"),
                func.max(
                    func.coalesce(
                        metric_model.ingested_at,
                        metric_model.source_observed_at,
                    )
                ).label("source_updated_at"),
                func.coalesce(func.sum(metric_model.cost_cents), 0).label(
                    "cost_cents"
                ),
                func.coalesce(
                    func.sum(metric_model.gross_revenue_cents), 0
                ).label("gmv_cents"),
                func.coalesce(func.sum(metric_model.orders), 0).label("orders"),
            )
            .where(
                metric_model.workspace_id == scope.workspace_id,
                metric_model.advertiser_id == scope.advertiser_id,
                metric_model.store_id == str(scope.shop.shop_id),
                stat_expression >= start_date,
                stat_expression <= end_date,
            )
            .group_by(
                metric_model.campaign_id,
                metric_model.item_group_id,
                stat_expression,
                source_priority,
            )
        )
    source = union_all(*product_selects).subquery()
    candidates = db.execute(
        select(source).order_by(
            source.c.stat_time_day,
            source.c.campaign_id,
            source.c.item_group_id,
            source.c.source_priority.desc(),
        )
    ).mappings().all()
    canonical: dict[tuple[str, str, date], dict[str, Any]] = {}
    for row in candidates:
        raw_day = row.get("stat_time_day")
        day = (
            raw_day
            if isinstance(raw_day, date)
            else date.fromisoformat(str(raw_day))
        )
        key = (
            str(row.get("campaign_id") or ""),
            str(row.get("item_group_id") or ""),
            day,
        )
        current = canonical.get(key)
        if current is None:
            canonical[key] = dict(row, stat_time_day=day)
            continue
        row_priority = int(row.get("source_priority") or 0)
        current_priority = int(current.get("source_priority") or 0)
        row_updated = row.get("source_updated_at") or datetime.min
        current_updated = current.get("source_updated_at") or datetime.min
        if row_priority > current_priority or (
            row_priority == current_priority and row_updated > current_updated
        ):
            canonical[key] = dict(row, stat_time_day=day)

    products: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"cost_cents": 0, "gmv_cents": 0, "orders": 0}
    )
    days: dict[date, dict[str, Decimal | int]] = defaultdict(
        lambda: {"cost_cents": 0, "gmv_cents": 0, "orders": 0}
    )
    for row in canonical.values():
        product_id = str(row.get("item_group_id") or "")
        day = row.get("stat_time_day")
        for key in ("cost_cents", "gmv_cents", "orders"):
            value = int(row.get(key) or 0)
            products[product_id][key] = int(products[product_id][key]) + value
            if isinstance(day, date):
                days[day][key] = int(days[day][key]) + value
    return products, days


def _cost_versions(
    db: Session,
    *,
    scope: CommerceScope,
    product_ids: Iterable[str],
    end_utc: datetime,
) -> dict[tuple[str, str], list[CommerceProductCostVersion]]:
    ids = sorted(set(str(item) for item in product_ids if item))
    if not ids:
        return {}
    rows = list(
        db.scalars(
            select(CommerceProductCostVersion)
            .where(
                CommerceProductCostVersion.workspace_id == scope.workspace_id,
                CommerceProductCostVersion.shop_row_id == int(scope.shop.id),
                CommerceProductCostVersion.product_id.in_(ids),
                CommerceProductCostVersion.effective_from < end_utc,
            )
            .order_by(
                CommerceProductCostVersion.product_id,
                CommerceProductCostVersion.sku_id,
                CommerceProductCostVersion.effective_from.desc(),
                CommerceProductCostVersion.id.desc(),
            )
        )
    )
    result: dict[
        tuple[str, str], list[CommerceProductCostVersion]
    ] = defaultdict(list)
    for row in rows:
        result[(str(row.product_id), str(row.sku_id or ""))].append(row)
    return result


def _effective_cost(
    versions: dict[tuple[str, str], list[CommerceProductCostVersion]],
    *,
    product_id: str,
    sku_id: str,
    at: datetime,
) -> CommerceProductCostVersion | None:
    for key in ((product_id, sku_id), (product_id, "")):
        for row in versions.get(key, []):
            if row.effective_from <= at:
                return row
    return None


def _order_product_metrics(
    db: Session,
    *,
    scope: CommerceScope,
    start_utc: datetime,
    end_utc: datetime,
    costs: dict[tuple[str, str], list[CommerceProductCostVersion]],
) -> dict[str, dict[str, Any]]:
    base_filters = (
        TikTokShopOrder.workspace_id == scope.workspace_id,
        TikTokShopOrder.shop_row_id == int(scope.shop.id),
        TikTokShopOrder.paid_at.is_not(None),
        TikTokShopOrder.paid_at >= start_utc,
        TikTokShopOrder.paid_at < end_utc,
        TikTokShopOrder.is_sample_order.is_(False),
        func.upper(func.coalesce(TikTokShopOrder.status, "")).not_in(
            {"CANCELLED", "CANCELED"}
        ),
    )
    line_revenue = (
        func.coalesce(TikTokShopOrderLine.sale_price, 0)
        * func.coalesce(TikTokShopOrderLine.quantity, 0)
    )
    rows = db.execute(
        select(
            TikTokShopOrder.order_id,
            TikTokShopOrder.paid_at,
            TikTokShopOrder.total_amount,
            TikTokShopOrderLine.product_id,
            TikTokShopOrderLine.sku_id,
            func.max(TikTokShopOrderLine.product_name).label("product_name"),
            func.coalesce(func.sum(TikTokShopOrderLine.quantity), 0).label(
                "quantity"
            ),
            func.coalesce(func.sum(line_revenue), 0).label(
                "merchandise_revenue"
            ),
        )
        .join(
            TikTokShopOrder,
            (
                (TikTokShopOrder.shop_row_id == TikTokShopOrderLine.shop_row_id)
                & (TikTokShopOrder.order_id == TikTokShopOrderLine.order_id)
            ),
        )
        .where(*base_filters)
        .group_by(
            TikTokShopOrder.order_id,
            TikTokShopOrder.paid_at,
            TikTokShopOrder.total_amount,
            TikTokShopOrderLine.product_id,
            TikTokShopOrderLine.sku_id,
        )
    ).mappings().all()
    products: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "product_name": "",
            "order_ids": set(),
            "quantity": 0,
            "revenue": ZERO,
            "merchandise_revenue": ZERO,
            "fixed_cost": ZERO,
            "rate_cost": ZERO,
            "cost_complete": True,
            "missing_cost_skus": set(),
        }
    )
    order_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        order_rows[str(row.get("order_id") or "")].append(dict(row))

    for order_id, entries in order_rows.items():
        order_total = _decimal(entries[0].get("total_amount"))
        merchandise_total = sum(
            (_decimal(item.get("merchandise_revenue")) for item in entries),
            ZERO,
        )
        quantity_total = sum(int(item.get("quantity") or 0) for item in entries)
        remaining = order_total
        for index, row in enumerate(entries):
            product_id = str(row.get("product_id") or "unknown")
            sku_id = str(row.get("sku_id") or "")
            quantity = int(row.get("quantity") or 0)
            item_revenue = _decimal(row.get("merchandise_revenue"))
            if index == len(entries) - 1:
                allocated_revenue = remaining
            elif merchandise_total > 0:
                allocated_revenue = order_total * item_revenue / merchandise_total
                remaining -= allocated_revenue
            elif quantity_total > 0:
                allocated_revenue = order_total * Decimal(quantity) / Decimal(
                    quantity_total
                )
                remaining -= allocated_revenue
            else:
                allocated_revenue = ZERO
            paid_at = row.get("paid_at")
            at = paid_at if isinstance(paid_at, datetime) else start_utc
            cost = _effective_cost(
                costs,
                product_id=product_id,
                sku_id=sku_id,
                at=at,
            )
            target = products[product_id]
            target["product_name"] = str(
                row.get("product_name") or target["product_name"] or ""
            )
            target["order_ids"].add(order_id)
            target["quantity"] += quantity
            target["revenue"] += allocated_revenue
            target["merchandise_revenue"] += item_revenue
            if cost is None:
                target["cost_complete"] = False
                target["missing_cost_skus"].add(
                    sku_id or "product-default"
                )
                continue
            per_unit = (
                _decimal(cost.unit_cost)
                + _decimal(cost.packaging_cost)
                + _decimal(cost.fulfillment_cost)
                + _decimal(cost.seller_shipping_cost)
                + _decimal(cost.other_variable_cost)
            )
            rate_total = (
                _decimal(cost.platform_fee_rate)
                + _decimal(cost.payment_fee_rate)
                + _decimal(cost.affiliate_commission_rate)
                + _decimal(cost.expected_refund_rate)
            )
            target["fixed_cost"] += per_unit * quantity
            target["rate_cost"] += allocated_revenue * rate_total
    for target in products.values():
        target["orders"] = len(target.pop("order_ids"))
    return products


def _shop_daily_trends(
    db: Session,
    *,
    scope: CommerceScope,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[date, dict[str, Decimal | int]]:
    dialect = str(db.get_bind().dialect.name)
    hour_expression = (
        func.strftime("%Y-%m-%d %H:00:00", TikTokShopOrder.paid_at)
        if dialect == "sqlite"
        else func.date_format(TikTokShopOrder.paid_at, "%Y-%m-%d %H:00:00")
    )
    rows = db.execute(
        select(
            hour_expression.label("paid_hour"),
            func.count(TikTokShopOrder.id).label("orders"),
            func.coalesce(func.sum(TikTokShopOrder.total_amount), 0).label(
                "revenue"
            ),
        )
        .where(
            TikTokShopOrder.workspace_id == scope.workspace_id,
            TikTokShopOrder.shop_row_id == int(scope.shop.id),
            TikTokShopOrder.paid_at.is_not(None),
            TikTokShopOrder.paid_at >= start_utc,
            TikTokShopOrder.paid_at < end_utc,
            TikTokShopOrder.is_sample_order.is_(False),
            func.upper(func.coalesce(TikTokShopOrder.status, "")).not_in(
                {"CANCELLED", "CANCELED"}
            ),
        )
        .group_by(hour_expression)
    ).mappings().all()
    zone = ZoneInfo(scope.reporting_timezone)
    result: dict[date, dict[str, Decimal | int]] = defaultdict(
        lambda: {"orders": 0, "revenue": ZERO}
    )
    for row in rows:
        raw_hour = row.get("paid_hour")
        if not raw_hour:
            continue
        hour = (
            raw_hour
            if isinstance(raw_hour, datetime)
            else datetime.fromisoformat(str(raw_hour))
        )
        local_day = hour.replace(tzinfo=timezone.utc).astimezone(zone).date()
        result[local_day]["orders"] = int(result[local_day]["orders"]) + int(
            row.get("orders") or 0
        )
        result[local_day]["revenue"] = _decimal(
            result[local_day]["revenue"]
        ) + _decimal(row.get("revenue"))
    return result


def _current_cost(
    versions: dict[tuple[str, str], list[CommerceProductCostVersion]],
    product_id: str,
    at: datetime,
) -> CommerceProductCostVersion | None:
    return _effective_cost(
        versions,
        product_id=str(product_id),
        sku_id="",
        at=at,
    )


def _serialize_cost(row: CommerceProductCostVersion | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": int(row.id),
        "sku_id": str(row.sku_id or ""),
        "effective_from": row.effective_from.isoformat(),
        "currency": str(row.currency),
        "unit_cost": _money(row.unit_cost),
        "packaging_cost": _money(row.packaging_cost),
        "fulfillment_cost": _money(row.fulfillment_cost),
        "seller_shipping_cost": _money(row.seller_shipping_cost),
        "other_variable_cost": _money(row.other_variable_cost),
        "platform_fee_rate": _rate(row.platform_fee_rate),
        "payment_fee_rate": _rate(row.payment_fee_rate),
        "affiliate_commission_rate": _rate(row.affiliate_commission_rate),
        "expected_refund_rate": _rate(row.expected_refund_rate),
        "target_margin_rate": _rate(row.target_margin_rate),
        "notes": row.notes,
        "created_at": row.created_at.isoformat(),
    }


def commerce_overview(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int | None = None,
    advertiser_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    scope = resolve_scope(
        db,
        workspace_id=int(workspace_id),
        shop_id=shop_id,
        advertiser_id=advertiser_id,
    )
    effective_start, effective_end, start_utc, end_utc = date_range(
        scope=scope,
        start_date=start_date,
        end_date=end_date,
    )
    products = list(
        db.scalars(
            select(TikTokShopProduct)
            .where(
                TikTokShopProduct.workspace_id == scope.workspace_id,
                TikTokShopProduct.shop_row_id == int(scope.shop.id),
            )
            .order_by(TikTokShopProduct.title, TikTokShopProduct.product_id)
        )
    )
    product_ids = [str(item.product_id) for item in products]
    costs = _cost_versions(
        db,
        scope=scope,
        product_ids=product_ids,
        end_utc=end_utc,
    )
    order_products = _order_product_metrics(
        db,
        scope=scope,
        start_utc=start_utc,
        end_utc=end_utc,
        costs=costs,
    )
    shop_days = _shop_daily_trends(
        db,
        scope=scope,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    ad_products, ad_days = _ad_metrics(
        db,
        scope=scope,
        start_date=effective_start,
        end_date=effective_end,
    )
    order_totals = order_summary(
        db,
        workspace_id=scope.workspace_id,
        store_id=str(scope.shop.shop_id),
        start_date=effective_start,
        end_date=effective_end,
        advertiser_timezone=scope.reporting_timezone,
    )
    mapping_rows = {
        str(row.shop_product_id): row
        for row in db.scalars(
            select(CommerceProductMapping).where(
                CommerceProductMapping.workspace_id == scope.workspace_id,
                CommerceProductMapping.shop_row_id == int(scope.shop.id),
                CommerceProductMapping.advertiser_id == scope.advertiser_id,
                CommerceProductMapping.is_active.is_(True),
            )
        )
    }
    inventory_by_product = {
        str(product_id): int(quantity or 0)
        for product_id, quantity in db.execute(
            select(
                TikTokShopSku.product_id,
                func.coalesce(func.sum(TikTokShopSku.inventory_quantity), 0),
            )
            .where(
                TikTokShopSku.workspace_id == scope.workspace_id,
                TikTokShopSku.shop_row_id == int(scope.shop.id),
            )
            .group_by(TikTokShopSku.product_id)
        ).all()
    }
    payload_products: list[dict[str, Any]] = []
    complete_profit = ZERO
    known_product_count = 0
    selling_product_count = 0
    merchandise_sales = ZERO
    allocated_product_sales = ZERO
    for product in products:
        product_id = str(product.product_id)
        order_data = order_products.get(product_id, {})
        ad_data = ad_products.get(
            product_id,
            {"cost_cents": 0, "gmv_cents": 0, "orders": 0},
        )
        revenue = _decimal(order_data.get("revenue"))
        item_revenue = _decimal(order_data.get("merchandise_revenue"))
        quantity = int(order_data.get("quantity") or 0)
        orders = int(order_data.get("orders") or 0)
        fixed_cost = _decimal(order_data.get("fixed_cost"))
        rate_cost = _decimal(order_data.get("rate_cost"))
        ad_spend = Decimal(int(ad_data.get("cost_cents") or 0)) / 100
        ad_gmv = Decimal(int(ad_data.get("gmv_cents") or 0)) / 100
        cost_complete = bool(order_data.get("cost_complete", True))
        current_cost = _current_cost(costs, product_id, end_utc - timedelta(microseconds=1))
        if quantity > 0:
            selling_product_count += 1
        contribution_before_ads = revenue - fixed_cost - rate_cost
        contribution_profit = contribution_before_ads - ad_spend
        if current_cost is not None:
            known_product_count += 1
        if cost_complete:
            complete_profit += contribution_profit
        merchandise_sales += item_revenue
        allocated_product_sales += revenue
        payload_products.append(
            {
                "product_id": product_id,
                "title": str(product.title or product_id),
                "image_url": product.main_image_url,
                "status": product.status,
                "currency": str(product.currency or scope.currency),
                "sale_price": _money(product.min_sale_price),
                "inventory_quantity": inventory_by_product.get(product_id, 0),
                "mapping_status": (
                    "mapped" if product_id in mapping_rows else "unmapped"
                ),
                "current_cost": _serialize_cost(current_cost),
                "cost_complete": cost_complete,
                "missing_cost_skus": sorted(
                    order_data.get("missing_cost_skus") or []
                ),
                "orders": orders,
                "quantity": quantity,
                "actual_sales": _money(revenue),
                "merchandise_sales": _money(item_revenue),
                "ad_spend": _money(ad_spend),
                "ad_attributed_gmv": _money(ad_gmv),
                "ad_orders": int(ad_data.get("orders") or 0),
                "roas": _ratio(ad_gmv, ad_spend),
                "fixed_and_unit_cost": _money(fixed_cost),
                "rate_cost": _money(rate_cost),
                "contribution_before_ads": (
                    _money(contribution_before_ads) if cost_complete else None
                ),
                "contribution_profit": (
                    _money(contribution_profit) if cost_complete else None
                ),
                "contribution_margin": (
                    _ratio(contribution_profit, revenue)
                    if cost_complete and revenue > 0
                    else None
                ),
                "break_even_roas": (
                    _ratio(revenue, contribution_before_ads)
                    if cost_complete and contribution_before_ads > 0
                    else None
                ),
            }
        )
    payload_products.sort(
        key=lambda item: (
            float(item["actual_sales"] or 0),
            float(item["ad_spend"] or 0),
        ),
        reverse=True,
    )

    ad_spend_total = sum(
        (Decimal(int(value.get("cost_cents") or 0)) / 100 for value in ad_products.values()),
        ZERO,
    )
    ad_gmv_total = sum(
        (Decimal(int(value.get("gmv_cents") or 0)) / 100 for value in ad_products.values()),
        ZERO,
    )
    actual_net_sales = _decimal(order_totals.get("net_revenue"))
    product_id_set = set(product_ids)
    unrepresented_order_products = sorted(
        set(order_products).difference(product_id_set)
    )
    unrepresented_ad_products = sorted(
        product_id
        for product_id, metrics in ad_products.items()
        if product_id not in product_id_set
        and (
            int(metrics.get("cost_cents") or 0) > 0
            or int(metrics.get("gmv_cents") or 0) > 0
            or int(metrics.get("orders") or 0) > 0
        )
    )
    product_profit_complete = (
        not unrepresented_order_products
        and not unrepresented_ad_products
        and all(
        item["cost_complete"]
        for item in payload_products
        if int(item["quantity"] or 0) > 0
        )
    )
    trends = []
    cursor = effective_start
    while cursor <= effective_end:
        shop_day = shop_days.get(cursor, {"orders": 0, "revenue": ZERO})
        ad_day = ad_days.get(
            cursor,
            {"cost_cents": 0, "gmv_cents": 0, "orders": 0},
        )
        trends.append(
            {
                "date": cursor.isoformat(),
                "actual_sales": _money(shop_day.get("revenue")),
                "orders": int(shop_day.get("orders") or 0),
                "ad_spend": _money(
                    Decimal(int(ad_day.get("cost_cents") or 0)) / 100
                ),
                "ad_attributed_gmv": _money(
                    Decimal(int(ad_day.get("gmv_cents") or 0)) / 100
                ),
                "ad_orders": int(ad_day.get("orders") or 0),
            }
        )
        cursor += timedelta(days=1)

    latest_product_sync = db.scalar(
        select(func.max(TikTokShopProduct.synced_at)).where(
            TikTokShopProduct.workspace_id == scope.workspace_id,
            TikTokShopProduct.shop_row_id == int(scope.shop.id),
        )
    )
    latest_order_sync = db.scalar(
        select(func.max(TikTokShopSyncRun.completed_at)).where(
            TikTokShopSyncRun.workspace_id == scope.workspace_id,
            TikTokShopSyncRun.shop_row_id == int(scope.shop.id),
            TikTokShopSyncRun.domain == "orders",
            TikTokShopSyncRun.status == "success",
        )
    )
    latest_ad_sync = db.scalar(
        select(
            func.max(
                func.coalesce(
                    GmvProductMetricsDaily.ingested_at,
                    GmvProductMetricsDaily.source_observed_at,
                )
            )
        ).where(
            GmvProductMetricsDaily.workspace_id == scope.workspace_id,
            GmvProductMetricsDaily.advertiser_id == scope.advertiser_id,
            GmvProductMetricsDaily.store_id == str(scope.shop.shop_id),
        )
    )
    return {
        "scope": {
            "shop_id": int(scope.shop.id),
            "provider_shop_id": str(scope.shop.shop_id),
            "shop_name": str(scope.shop.shop_name or scope.shop.shop_id),
            "shop_timezone": str(scope.shop.timezone_name),
            "shop_timezone_source": str(scope.shop.timezone_source),
            "advertiser_id": scope.advertiser_id,
            "advertiser_name": scope.advertiser_name,
            "reporting_timezone": scope.reporting_timezone,
            "reporting_timezone_source": scope.reporting_timezone_source,
            "currency": scope.currency,
            "start_date": effective_start.isoformat(),
            "end_date": effective_end.isoformat(),
        },
        "summary": {
            "order_paid_sales": _money(actual_net_sales),
            "actual_net_sales": _money(actual_net_sales),
            "merchandise_sales": _money(merchandise_sales),
            "allocated_product_sales": _money(allocated_product_sales),
            "order_reconciliation_delta": _money(
                actual_net_sales - allocated_product_sales
            ),
            "orders": int(order_totals.get("order_count") or 0),
            "cancelled_orders": int(
                order_totals.get("cancelled_order_count") or 0
            ),
            "refunds_and_cancellations": _money(
                order_totals.get("refund_amount")
            ),
            "average_order_value": _money(
                order_totals.get("average_order_value")
            ),
            "ad_spend": _money(ad_spend_total),
            "ad_attributed_gmv": _money(ad_gmv_total),
            "ad_orders": sum(
                int(value.get("orders") or 0) for value in ad_products.values()
            ),
            "blended_mer": _ratio(actual_net_sales, ad_spend_total),
            "attributed_roas": _ratio(ad_gmv_total, ad_spend_total),
            "organic_revenue_estimate": _money(
                max(ZERO, actual_net_sales - ad_gmv_total)
            ),
            "organic_revenue_share": (
                _ratio(max(ZERO, actual_net_sales - ad_gmv_total), actual_net_sales)
                if actual_net_sales > 0
                else None
            ),
            "contribution_profit": (
                _money(complete_profit) if product_profit_complete else None
            ),
            "contribution_margin": (
                _ratio(complete_profit, actual_net_sales)
                if product_profit_complete and actual_net_sales > 0
                else None
            ),
        },
        "trends": trends,
        "products": payload_products,
        "data_health": {
            "order_source": "tiktok_shop_api",
            "order_source_timezone": str(scope.shop.timezone_name),
            "reporting_timezone": scope.reporting_timezone,
            "reporting_timezone_source": scope.reporting_timezone_source,
            "product_cost_coverage": {
                "configured_products": known_product_count,
                "selling_products": selling_product_count,
                "profit_complete": product_profit_complete,
            },
            "product_mapping": {
                "mapped": sum(
                    1 for item in payload_products if item["mapping_status"] == "mapped"
                ),
                "total": len(payload_products),
            },
            "refund_quality": order_totals.get("data_quality", {}).get(
                "refunds"
            ),
            "finance": order_totals.get("finance", {}),
            "finance_quality": order_totals.get("data_quality", {}).get(
                "finance"
            ),
            "sales_basis": "shop_order_paid_total",
            "unrepresented_product_ids": sorted(
                set(unrepresented_order_products)
                | set(unrepresented_ad_products)
            ),
            "last_product_sync_at": (
                latest_product_sync.isoformat() if latest_product_sync else None
            ),
            "last_order_sync_at": (
                latest_order_sync.isoformat() if latest_order_sync else None
            ),
            "last_ad_sync_at": (
                latest_ad_sync.isoformat() if latest_ad_sync else None
            ),
        },
    }


def cost_history(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int,
    product_id: str,
    sku_id: str | None = None,
) -> list[dict[str, Any]]:
    statement = (
        select(CommerceProductCostVersion)
        .where(
            CommerceProductCostVersion.workspace_id == int(workspace_id),
            CommerceProductCostVersion.shop_row_id == int(shop_id),
            CommerceProductCostVersion.product_id == str(product_id),
        )
        .order_by(
            CommerceProductCostVersion.effective_from.desc(),
            CommerceProductCostVersion.id.desc(),
        )
        .limit(100)
    )
    if sku_id is not None:
        statement = statement.where(
            CommerceProductCostVersion.sku_id == str(sku_id)
        )
    return [
        _serialize_cost(row)
        for row in db.scalars(statement)
        if row is not None
    ]


__all__ = [
    "CommerceScope",
    "commerce_context",
    "commerce_overview",
    "cost_history",
    "date_range",
    "resolve_scope",
    "sync_exact_product_mappings",
]
