from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Collection, Mapping, Optional, Sequence, TypedDict

from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxCampaignProduct,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.services.gmvmax_spec import GMVMAX_DEFAULT_METRICS
from app.services.ttb_api import TTBApiClient


logger = logging.getLogger("gmv.tenants.gmvmax")

__all__ = [
    "upsert_campaign_from_api",
    "sync_gmvmax_campaigns",
    "upsert_metrics_hourly_row",
    "sync_gmvmax_metrics_hourly",
    "upsert_metrics_daily_row",
    "sync_gmvmax_metrics_daily",
    "log_campaign_action",
    "apply_campaign_action",
    "get_or_create_strategy_config",
    "aggregate_recent_metrics",
    "decide_campaign_action",
    "resolve_store_id_from_page_context",
]


_DECIMAL_FOUR = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")
_DEFAULT_REPORT_METRICS = list(GMVMAX_DEFAULT_METRICS)

_REPORT_PAGE_SIZE = 200


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_store_links(payload: Mapping[str, Any] | None) -> dict[str, list[str]]:
    if not isinstance(payload, Mapping):
        return {}
    advertiser_map = payload.get("advertiser_to_stores")
    if not isinstance(advertiser_map, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for raw_adv, raw_store_ids in advertiser_map.items():
        adv_key = _normalize_identifier(raw_adv)
        if not adv_key:
            continue
        store_ids: list[str] = []
        if isinstance(raw_store_ids, (list, tuple, set)):
            for candidate in raw_store_ids:
                normalized = _normalize_identifier(candidate)
                if normalized:
                    store_ids.append(normalized)
        else:
            normalized = _normalize_identifier(raw_store_ids)
            if normalized:
                store_ids.append(normalized)
        if store_ids:
            result[adv_key] = store_ids
    return result


def _build_store_lookup(stores: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(stores, list):
        return {}
    lookup: dict[str, Mapping[str, Any]] = {}
    for entry in stores:
        if not isinstance(entry, Mapping):
            continue
        store_key = _normalize_identifier(
            entry.get("store_id") or entry.get("shop_id") or entry.get("id")
        )
        if not store_key:
            continue
        lookup[store_key] = entry
    return lookup


def _extract_campaign_bc_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("bc_id", "store_authorized_bc_id", "authorized_bc_id"):
        normalized = _normalize_identifier(payload.get(key))
        if normalized:
            return normalized
    return None


def _resolve_store_id(
    *,
    advertiser_id: str,
    campaign_payload: Mapping[str, Any],
    page_context: Mapping[str, Any],
) -> str | None:
    links_payload = page_context.get("links") if isinstance(page_context, Mapping) else None
    stores_payload = page_context.get("stores") if isinstance(page_context, Mapping) else None
    store_links = _extract_store_links(links_payload)
    store_lookup = _build_store_lookup(stores_payload)
    candidates = list(dict.fromkeys(store_links.get(str(advertiser_id), [])))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    campaign_bc_id = _extract_campaign_bc_id(campaign_payload)
    if campaign_bc_id:
        matches = [
            store_id
            for store_id in candidates
            if _normalize_identifier(
                store_lookup.get(store_id, {}).get("store_authorized_bc_id")
            )
            == campaign_bc_id
            or _normalize_identifier(store_lookup.get(store_id, {}).get("bc_id"))
            == campaign_bc_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "multiple stores matched bc_id; defaulting to first",
                extra={
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_payload.get("campaign_id"),
                    "bc_id": campaign_bc_id,
                    "store_candidates": matches,
                },
            )
            return matches[0]
    logger.warning(
        "ambiguous store mapping; defaulting to first",
        extra={
            "advertiser_id": advertiser_id,
            "campaign_id": campaign_payload.get("campaign_id"),
            "store_candidates": candidates,
            "bc_id": campaign_bc_id,
        },
        )
    return candidates[0]


def resolve_store_id_from_page_context(
    *,
    advertiser_id: str,
    campaign_payload: Mapping[str, Any],
    page_context: Mapping[str, Any],
) -> str | None:
    """Public helper that surfaces :func:`_resolve_store_id` for reuse."""

    return _resolve_store_id(
        advertiser_id=str(advertiser_id),
        campaign_payload=campaign_payload,
        page_context=page_context,
    )


def _assign_sqlite_pk(db: Session, row: TTBGmvMaxCampaign) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(row, "id", None):
        return
    next_value = db.execute(
        select(func.coalesce(func.max(TTBGmvMaxCampaign.id), 0))
    ).scalar_one()
    row.id = int(next_value or 0) + 1


def _collect_product_ids_from_value(source: Any, target: set[str]) -> None:
    if source is None:
        return
    if isinstance(source, (list, tuple, set)):
        for item in source:
            _collect_product_ids_from_value(item, target)
        return
    if isinstance(source, Mapping):
        for key in (
            "item_group_id",
            "itemGroupId",
            "spu_id",
            "spuId",
            "product_id",
            "productId",
            "item_id",
            "itemId",
            "id",
        ):
            normalized = _normalize_identifier(source.get(key))
            if normalized:
                target.add(normalized)
        for nested_key in (
            "item_group_ids",
            "itemGroupIds",
            "item_groups",
            "itemGroupList",
            "item_group_list",
            "item_list",
            "itemList",
            "item_ids",
            "itemIds",
            "product_ids",
            "productIds",
            "product_list",
            "productList",
            "products",
            "items",
        ):
            nested_value = source.get(nested_key)
            if nested_value is not None and nested_value is not source:
                _collect_product_ids_from_value(nested_value, target)
        return
    normalized = _normalize_identifier(source)
    if normalized:
        target.add(normalized)


def _extract_item_group_ids_from_payload(payload: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    collected: set[str] = set()
    for key in (
        "item_group_ids",
        "itemGroupIds",
        "item_groups",
        "itemGroupList",
        "item_group_list",
        "item_list",
        "itemList",
        "item_ids",
        "itemIds",
        "item_id",
        "itemId",
        "product_ids",
        "productIds",
        "product_list",
        "productList",
        "products",
    ):
        value = payload.get(key)
        if value is not None:
            _collect_product_ids_from_value(value, collected)

    nested_campaign = payload.get("campaign")
    if isinstance(nested_campaign, Mapping):
        _collect_product_ids_from_value(nested_campaign, collected)

    sessions = payload.get("sessions") or payload.get("session_list")
    if isinstance(sessions, Mapping):
        _collect_product_ids_from_value(sessions, collected)
    elif isinstance(sessions, (list, tuple, set)):
        for session in sessions:
            if isinstance(session, Mapping):
                _collect_product_ids_from_value(session, collected)

    return sorted(collected)


def _sync_campaign_product_assignments(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    product_ids: Sequence[str],
) -> None:
    if not getattr(campaign, "id", None):
        return
    db.execute(
        delete(TTBGmvMaxCampaignProduct).where(
            TTBGmvMaxCampaignProduct.campaign_pk == campaign.id
        )
    )
    normalized_status = _normalize_status_value(campaign.operation_status)
    if normalized_status != "ENABLE":
        return
    store_id = _normalize_identifier(campaign.store_id)
    if not store_id or not product_ids:
        return

    existing_conflicts = (
        delete(TTBGmvMaxCampaignProduct)
        .where(TTBGmvMaxCampaignProduct.workspace_id == campaign.workspace_id)
        .where(TTBGmvMaxCampaignProduct.auth_id == campaign.auth_id)
        .where(TTBGmvMaxCampaignProduct.store_id == store_id)
        .where(TTBGmvMaxCampaignProduct.item_group_id.in_(list(product_ids)))
    )
    db.execute(existing_conflicts)

    for product_id in product_ids:
        normalized = _normalize_identifier(product_id)
        if not normalized:
            continue
        db.add(
            TTBGmvMaxCampaignProduct(
                workspace_id=campaign.workspace_id,
                auth_id=campaign.auth_id,
                campaign_pk=campaign.id,
                campaign_id=campaign.campaign_id,
                store_id=store_id,
                item_group_id=normalized,
                operation_status=normalized_status,
            )
        )


def _lookup_store_id_from_links(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_payload: Mapping[str, Any],
) -> str | None:
    """Resolve store_id via advertiser ↔ store links stored in our database."""

    stmt = (
        select(
            TTBAdvertiserStoreLink.store_id,
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
        )
        .where(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .where(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .where(TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
    )
    rows = db.execute(stmt).all()
    if not rows:
        return None

    normalized_bc = _normalize_identifier(_extract_campaign_bc_id(campaign_payload))
    matched_by_bc: list[str] = []
    candidates: list[str] = []
    for row in rows:
        store_value = _normalize_identifier(row.store_id)
        if not store_value:
            continue
        candidates.append(store_value)
        if not normalized_bc:
            continue
        linked_bc_values = (
            _normalize_identifier(row.store_authorized_bc_id),
            _normalize_identifier(row.bc_id_hint),
        )
        if normalized_bc in linked_bc_values:
            matched_by_bc.append(store_value)

    if matched_by_bc:
        unique_matches = list(dict.fromkeys(matched_by_bc))
        if len(unique_matches) > 1:
            logger.warning(
                "multiple store links matched bc_id; defaulting to first",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "bc_id": normalized_bc,
                    "store_candidates": unique_matches,
                },
            )
        return unique_matches[0]

    unique_candidates = list(dict.fromkeys(candidates))
    if not unique_candidates:
        return None
    if len(unique_candidates) == 1:
        return unique_candidates[0]

    logger.warning(
        "ambiguous store link mapping; skipping auto resolution",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": advertiser_id,
            "store_candidates": unique_candidates,
            "bc_id": normalized_bc,
        },
    )
    return None


def _normalize_status_value(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip().upper()
    except Exception:  # pragma: no cover - defensive
        return None
    return text or None


def _normalize_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value
    raise ValueError("invalid date value")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
            ):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _to_decimal(value: Any, *, quantize: Decimal | None = None) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        result = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            result = Decimal(s)
        except (InvalidOperation, ValueError):
            return None
    if quantize is not None:
        try:
            result = result.quantize(quantize)
        except (InvalidOperation, ValueError):
            result = result.quantize(quantize, rounding=ROUND_HALF_UP)
    return result


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    s = str(value).strip()
    if not s:
        return None
    try:
        dec = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return int(dec.to_integral_value(rounding=ROUND_HALF_UP))


def _to_cents(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        cents = value * _ONE_HUNDRED
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            cents = Decimal(s) * _ONE_HUNDRED
        except (InvalidOperation, ValueError):
            return None
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _cents_to_currency(cents: int) -> str:
    quantized = (Decimal(int(cents)) / _ONE_HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def _extract_field(container: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in container and container[key] is not None:
            return container[key]
    dims = container.get("dimensions")
    if isinstance(dims, dict):
        for key in keys:
            if key in dims and dims[key] is not None:
                return dims[key]
    metrics = container.get("metrics")
    if isinstance(metrics, dict):
        for key in keys:
            if key in metrics and metrics[key] is not None:
                return metrics[key]
    return None


def _extract_field_from_sources(keys: Sequence[str], *sources: Mapping[str, Any] | None) -> Any:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        value = _extract_field(source, *keys)
        if value is not None:
            return value
    return None


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, Decimal):
            serialized[key] = format(value, "f")
        elif isinstance(value, datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, date):
            serialized[key] = value.isoformat()
        else:
            serialized[key] = value
    return serialized


async def _fetch_campaign_details(
    ttb_client: TTBApiClient,
    *,
    advertiser_id: str,
    campaign_id: str,
) -> Mapping[str, Any] | None:
    try:
        details = await ttb_client.get_gmvmax_campaign_info(advertiser_id, campaign_id)
    except Exception:  # pragma: no cover - defensive logging
        logger.warning(
            "failed to fetch campaign info when resolving store_id",
            exc_info=True,
            extra={
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
            },
        )
        return None

    if not isinstance(details, Mapping):
        return None

    return details


async def sync_gmvmax_campaigns(
    db: Session,
    ttb_client: TTBApiClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    **filters: Any,
) -> dict:
    synced = 0
    details_cache: dict[str, Mapping[str, Any] | None] = {}
    async for payload, page_context in ttb_client.iter_gmvmax_campaigns(
        advertiser_id, **filters
    ):
        if not isinstance(payload, dict):
            continue
        campaign_identifier = _normalize_identifier(
            _extract_field(payload, "campaign_id", "id")
        )
        campaign_details: Mapping[str, Any] | None = None
        if campaign_identifier:
            if campaign_identifier not in details_cache:
                details_cache[campaign_identifier] = await _fetch_campaign_details(
                    ttb_client,
                    advertiser_id=str(advertiser_id),
                    campaign_id=campaign_identifier,
                )
            campaign_details = details_cache.get(campaign_identifier)

        resolved_store_id = _extract_field_from_sources(
            ("store_id", "shop_id"), campaign_details, payload
        )
        if not resolved_store_id:
            resolved_store_id = _resolve_store_id(
                advertiser_id=advertiser_id,
                campaign_payload=payload,
                page_context=page_context,
            )
        if not resolved_store_id and campaign_details:
            resolved_store_id = _extract_field_from_sources(
                ("store_id", "shop_id"), campaign_details
            )
        upsert_campaign_from_api(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            payload=payload,
            store_id_hint=resolved_store_id,
            campaign_details=campaign_details,
        )
        synced += 1
    db.flush()
    return {"synced": synced}


def upsert_campaign_from_api(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: dict,
    store_id_hint: str | None = None,
    campaign_details: Mapping[str, Any] | None = None,
) -> TTBGmvMaxCampaign:
    if not isinstance(payload, dict):
        raise ValueError("payload must be dict")
    campaign_identifier = _extract_field(payload, "campaign_id", "id")
    if not campaign_identifier:
        raise ValueError("campaign_id missing in payload")
    campaign_id = str(campaign_identifier)

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == campaign_id)
    )
    result = db.execute(stmt).scalars().first()
    if result is None:
        result = TTBGmvMaxCampaign(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=campaign_id,
        )
        db.add(result)
        _assign_sqlite_pk(db, result)

    result.advertiser_id = str(advertiser_id)
    name_value = _extract_field_from_sources(
        ("campaign_name", "name"), payload, campaign_details
    )
    result.name = name_value

    store_identifier: str | None = None
    store_identifier_source: str | None = None

    def _try_set_store(candidate: Any, source: str) -> bool:
        nonlocal store_identifier, store_identifier_source
        normalized = _normalize_identifier(candidate)
        if not normalized:
            return False
        # Once a store identifier has been chosen, we should not let lower-priority
        # sources (e.g. cascade hints) override it. This keeps the authoritative
        # value resolved from the campaign info locked in place.
        if store_identifier is not None:
            return False
        store_identifier = normalized
        store_identifier_source = source
        return True

    if not _try_set_store(
        _extract_field_from_sources(("store_id", "shop_id"), campaign_details),
        "campaign_details",
    ):
        if not _try_set_store(
            _extract_field_from_sources(("store_id", "shop_id"), payload),
            "payload",
        ):
            if not _try_set_store(store_id_hint, "hint"):
                _try_set_store(
                    _lookup_store_id_from_links(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=advertiser_id,
                        campaign_payload=payload,
                    ),
                    "store_link",
                )

    existing_store_id = _normalize_identifier(result.store_id)
    if store_identifier_source == "campaign_details" and store_identifier is not None:
        result.store_id = store_identifier
    else:
        if existing_store_id and store_identifier and store_identifier != existing_store_id:
            logger.warning(
                "ignoring non-authoritative store_id override",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": campaign_id,
                    "existing_store_id": existing_store_id,
                    "incoming_store_id": store_identifier,
                    "store_source": store_identifier_source,
                },
            )
        if existing_store_id:
            store_identifier = existing_store_id
        if store_identifier is None:
            store_identifier = ""
            logger.warning(
                "gmvmax campaign missing store_id; defaulting to empty string",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": campaign_id,
                },
            )
        result.store_id = str(store_identifier)

    operation_status_value = _extract_field_from_sources(
        ("operation_status",), payload, campaign_details
    )
    result.operation_status = _normalize_status_value(operation_status_value)

    status_value = _extract_field_from_sources(
        ("status", "campaign_status"), payload, campaign_details
    )
    if status_value is None:
        status_value = _extract_field_from_sources(
            ("primary_status",), payload, campaign_details
        )
    if status_value is None and result.operation_status is not None:
        status_value = result.operation_status
    result.status = _normalize_status_value(status_value)
    secondary_status_value = _extract_field_from_sources(
        ("secondary_status",), payload, campaign_details
    )
    result.secondary_status = _normalize_status_value(secondary_status_value)
    result.shopping_ads_type = _extract_field_from_sources(
        ("shopping_ads_type",), payload, campaign_details
    )
    result.optimization_goal = _extract_field_from_sources(
        ("optimization_goal",), payload, campaign_details
    )

    roas_value = _extract_field_from_sources(
        ("roas_bid", "roi_target"), payload, campaign_details
    )
    result.roas_bid = _to_decimal(roas_value, quantize=_DECIMAL_FOUR)

    budget_cents_value = _extract_field_from_sources(
        ("daily_budget_cents",), payload, campaign_details
    )
    if budget_cents_value is not None:
        result.daily_budget_cents = _to_int(budget_cents_value)
    else:
        budget_value = _extract_field_from_sources(
            ("daily_budget", "budget"), payload, campaign_details
        )
        result.daily_budget_cents = _to_cents(budget_value)

    currency_value = _extract_field_from_sources(
        ("currency", "budget_currency"), payload, campaign_details
    )
    result.currency = str(currency_value) if currency_value is not None else None

    created_time = _extract_field_from_sources(
        ("create_time", "created_time", "ext_created_time"), payload, campaign_details
    )
    updated_time = _extract_field_from_sources(
        ("update_time", "updated_time", "ext_updated_time"), payload, campaign_details
    )
    result.ext_created_time = _parse_datetime(created_time)
    result.ext_updated_time = _parse_datetime(updated_time)

    if isinstance(campaign_details, Mapping) and campaign_details:
        combined_payload = dict(payload)
        combined_payload["_campaign_info"] = campaign_details
        result.raw_json = combined_payload
    else:
        result.raw_json = payload

    product_ids = _extract_item_group_ids_from_payload(payload)
    if isinstance(campaign_details, Mapping):
        detail_products = _extract_item_group_ids_from_payload(campaign_details)
        if detail_products:
            product_ids = sorted({*product_ids, *detail_products})
    _sync_campaign_product_assignments(db, campaign=result, product_ids=product_ids)

    db.flush()
    return result


def upsert_metrics_hourly_row(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    row: dict,
) -> TTBGmvMaxMetricsHourly:
    if not isinstance(row, dict):
        raise ValueError("row must be dict")
    interval_start_value = _extract_field(
        row,
        "interval_start",
        "interval_start_time",
        "start_time",
        "stat_time_hour",
        "stat_time",
    )
    interval_start = _parse_datetime(interval_start_value)
    if interval_start is None:
        raise ValueError("interval_start missing")

    stmt = (
        select(TTBGmvMaxMetricsHourly)
        .where(TTBGmvMaxMetricsHourly.campaign_id == campaign.id)
        .where(TTBGmvMaxMetricsHourly.interval_start == interval_start)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = TTBGmvMaxMetricsHourly(
            campaign_id=campaign.id,
            interval_start=interval_start,
        )
        db.add(instance)

    interval_end_value = _extract_field(
        row,
        "interval_end",
        "interval_end_time",
        "end_time",
        "stat_time_hour_end",
    )
    instance.interval_end = _parse_datetime(interval_end_value)

    instance.impressions = _to_int(_extract_field(row, "impressions", "show_cnt", "views"))
    instance.clicks = _to_int(_extract_field(row, "clicks", "click", "click_cnt"))
    cost_cents_value = _extract_field(row, "cost_cents")
    if cost_cents_value is not None:
        instance.cost_cents = _to_int(cost_cents_value)
    else:
        instance.cost_cents = _to_cents(
            _extract_field(row, "cost", "spend", "total_spend", "total_cost")
        )
    net_cost_cents_value = _extract_field(row, "net_cost_cents")
    if net_cost_cents_value is not None:
        instance.net_cost_cents = _to_int(net_cost_cents_value)
    else:
        instance.net_cost_cents = _to_cents(_extract_field(row, "net_cost"))
    instance.orders = _to_int(_extract_field(row, "orders", "order_num", "conversions"))
    gross_revenue_cents_value = _extract_field(row, "gross_revenue_cents")
    if gross_revenue_cents_value is not None:
        instance.gross_revenue_cents = _to_int(gross_revenue_cents_value)
    else:
        instance.gross_revenue_cents = _to_cents(
            _extract_field(row, "gross_revenue", "gmv", "revenue")
        )
    instance.roi = _to_decimal(_extract_field(row, "roi", "roas"), quantize=_DECIMAL_FOUR)
    instance.product_impressions = _to_int(
        _extract_field(row, "product_impressions", "product_show", "product_show_cnt")
    )
    instance.product_clicks = _to_int(
        _extract_field(row, "product_clicks", "product_click", "product_click_cnt")
    )
    instance.product_click_rate = _to_decimal(
        _extract_field(row, "product_click_rate", "product_ctr"), quantize=_DECIMAL_FOUR
    )
    instance.ad_click_rate = _to_decimal(
        _extract_field(row, "ad_click_rate", "ctr"), quantize=_DECIMAL_FOUR
    )
    instance.ad_conversion_rate = _to_decimal(
        _extract_field(row, "ad_conversion_rate", "cvr"), quantize=_DECIMAL_FOUR
    )
    instance.video_views_2s = _to_int(
        _extract_field(row, "video_views_2s", "video_play_2s", "video_views_2_sec")
    )
    instance.video_views_6s = _to_int(
        _extract_field(row, "video_views_6s", "video_play_6s", "video_views_6_sec")
    )
    instance.video_views_p25 = _to_int(
        _extract_field(row, "video_views_p25", "video_play_actions_25", "video_views_25")
    )
    instance.video_views_p50 = _to_int(
        _extract_field(row, "video_views_p50", "video_play_actions_50", "video_views_50")
    )
    instance.video_views_p75 = _to_int(
        _extract_field(row, "video_views_p75", "video_play_actions_75", "video_views_75")
    )
    instance.video_views_p100 = _to_int(
        _extract_field(row, "video_views_p100", "video_play_actions_100", "video_views_100")
    )
    instance.live_views = _to_int(_extract_field(row, "live_views", "live_watch_cnt"))
    instance.live_follows = _to_int(_extract_field(row, "live_follows", "live_followers"))
    instance.store_id = str(campaign.store_id or "")

    db.flush()
    return instance


async def sync_gmvmax_metrics_hourly(
    db: Session,
    ttb_client: TTBApiClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: TTBGmvMaxCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict:
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    page = 1
    store_id = campaign.store_id
    if not store_id:
        logger.warning(
            "skip hourly metrics sync because store_id missing",
            extra={
                "campaign_id": campaign.campaign_id,
                "workspace_id": workspace_id,
                "auth_id": auth_id,
            },
        )
        return {"synced_rows": 0}

    dimensions = ["campaign_id", "stat_time_hour"]
    campaign_ids = [campaign.campaign_id]
    while True:
        data = await ttb_client.report_gmvmax(
            advertiser_id,
            store_ids=[store_id],
            start_date=start_date_str,
            end_date=end_date_str,
            metrics=_DEFAULT_REPORT_METRICS,
            dimensions=dimensions,
            campaign_ids=campaign_ids,
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        if not isinstance(data, dict):
            break
        rows_raw = data.get("list") or data.get("items") or []
        rows = [item for item in rows_raw if isinstance(item, dict)]
        if not rows:
            break
        for row in rows:
            try:
                upsert_metrics_hourly_row(db, campaign=campaign, row=row)
                synced_rows += 1
            except ValueError:
                logger.debug(
                    "skip hourly metrics row without interval_start",
                    extra={
                        "campaign_id": campaign.campaign_id,
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                    },
                )
                continue
        page_info = data.get("page_info")
        if not isinstance(page_info, dict):
            break
        has_more = page_info.get("has_more") or page_info.get("has_next")
        total_page = page_info.get("total_page")
        if has_more in (True, 1):
            page += 1
            continue
        try:
            total_page_int = int(total_page) if total_page is not None else None
        except (TypeError, ValueError):
            total_page_int = None
        if total_page_int is not None and page < total_page_int:
            page += 1
            continue
        break

    db.flush()
    return {"synced_rows": synced_rows}


def upsert_metrics_daily_row(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    row: dict,
) -> TTBGmvMaxMetricsDaily:
    if not isinstance(row, dict):
        raise ValueError("row must be dict")
    date_value = _extract_field(row, "date", "stat_time_day", "stat_time")
    stat_date = _parse_date(date_value)
    if stat_date is None:
        raise ValueError("date missing")

    stmt = (
        select(TTBGmvMaxMetricsDaily)
        .where(TTBGmvMaxMetricsDaily.campaign_id == campaign.id)
        .where(TTBGmvMaxMetricsDaily.date == stat_date)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = TTBGmvMaxMetricsDaily(
            campaign_id=campaign.id,
            date=stat_date,
        )
        db.add(instance)

    instance.impressions = _to_int(_extract_field(row, "impressions", "show_cnt", "views"))
    instance.clicks = _to_int(_extract_field(row, "clicks", "click", "click_cnt"))
    cost_cents_value = _extract_field(row, "cost_cents")
    if cost_cents_value is not None:
        instance.cost_cents = _to_int(cost_cents_value)
    else:
        instance.cost_cents = _to_cents(
            _extract_field(row, "cost", "spend", "total_spend", "total_cost")
        )
    net_cost_cents_value = _extract_field(row, "net_cost_cents")
    if net_cost_cents_value is not None:
        instance.net_cost_cents = _to_int(net_cost_cents_value)
    else:
        instance.net_cost_cents = _to_cents(_extract_field(row, "net_cost"))
    instance.orders = _to_int(_extract_field(row, "orders", "order_num", "conversions"))
    gross_revenue_cents_value = _extract_field(row, "gross_revenue_cents")
    if gross_revenue_cents_value is not None:
        instance.gross_revenue_cents = _to_int(gross_revenue_cents_value)
    else:
        instance.gross_revenue_cents = _to_cents(
            _extract_field(row, "gross_revenue", "gmv", "revenue")
        )
    instance.roi = _to_decimal(_extract_field(row, "roi", "roas"), quantize=_DECIMAL_FOUR)
    instance.product_impressions = _to_int(
        _extract_field(row, "product_impressions", "product_show", "product_show_cnt")
    )
    instance.product_clicks = _to_int(
        _extract_field(row, "product_clicks", "product_click", "product_click_cnt")
    )
    instance.product_click_rate = _to_decimal(
        _extract_field(row, "product_click_rate", "product_ctr"), quantize=_DECIMAL_FOUR
    )
    instance.ad_click_rate = _to_decimal(
        _extract_field(row, "ad_click_rate", "ctr"), quantize=_DECIMAL_FOUR
    )
    instance.ad_conversion_rate = _to_decimal(
        _extract_field(row, "ad_conversion_rate", "cvr"), quantize=_DECIMAL_FOUR
    )
    instance.live_views = _to_int(_extract_field(row, "live_views", "live_watch_cnt"))
    instance.live_follows = _to_int(_extract_field(row, "live_follows", "live_followers"))
    instance.store_id = str(campaign.store_id or "")

    db.flush()
    return instance


async def sync_gmvmax_metrics_daily(
    db: Session,
    ttb_client: TTBApiClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: TTBGmvMaxCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict:
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    page = 1
    store_id = campaign.store_id
    if not store_id:
        logger.warning(
            "skip daily metrics sync because store_id missing",
            extra={
                "campaign_id": campaign.campaign_id,
                "workspace_id": workspace_id,
                "auth_id": auth_id,
            },
        )
        return {"synced_rows": 0}

    dimensions = ["campaign_id", "stat_time_day"]
    campaign_ids = [campaign.campaign_id]
    while True:
        data = await ttb_client.report_gmvmax(
            advertiser_id,
            store_ids=[store_id],
            start_date=start_date_str,
            end_date=end_date_str,
            metrics=_DEFAULT_REPORT_METRICS,
            dimensions=dimensions,
            campaign_ids=campaign_ids,
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        if not isinstance(data, dict):
            break
        rows_raw = data.get("list") or data.get("items") or []
        rows = [item for item in rows_raw if isinstance(item, dict)]
        if not rows:
            break
        for row in rows:
            try:
                upsert_metrics_daily_row(db, campaign=campaign, row=row)
                synced_rows += 1
            except ValueError:
                logger.debug(
                    "skip daily metrics row without date",
                    extra={
                        "campaign_id": campaign.campaign_id,
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                    },
                )
                continue
        page_info = data.get("page_info")
        if not isinstance(page_info, dict):
            break
        has_more = page_info.get("has_more") or page_info.get("has_next")
        total_page = page_info.get("total_page")
        if has_more in (True, 1):
            page += 1
            continue
        try:
            total_page_int = int(total_page) if total_page is not None else None
        except (TypeError, ValueError):
            total_page_int = None
        if total_page_int is not None and page < total_page_int:
            page += 1
            continue
        break

    db.flush()
    return {"synced_rows": synced_rows}


def log_campaign_action(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: TTBGmvMaxCampaign,
    action: str,
    reason: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    performed_by: str = "system",
    result: str = "SUCCESS",
    error_message: str | None = None,
    audit_hook: Callable[..., Any] | None = None,
) -> TTBGmvMaxActionLog:
    log_row = TTBGmvMaxActionLog(
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign.id,
        action=action,
        reason=reason,
        before_json=_serialize_state(before or {}),
        after_json=_serialize_state(after or {}),
        performed_by=performed_by,
        result=result,
        error_message=error_message,
    )
    db.add(log_row)
    db.flush()

    if audit_hook is not None:
        try:
            audit_hook(
                db=db,
                workspace_id=workspace_id,
                actor=performed_by,
                domain="gmv_max",
                event=f"campaign.{action.lower()}",
                target={
                    "campaign_id": campaign.campaign_id,
                    "advertiser_id": campaign.advertiser_id,
                },
                before=before,
                after=after,
                result=result,
                error=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "audit hook failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": campaign.campaign_id,
                    "action": action,
                },
            )
    return log_row


_ALLOWED_ACTIONS = {"START", "PAUSE", "SET_BUDGET", "SET_ROAS"}
_ACTION_NORMALIZATION = {
    "START": "START",
    "ENABLE": "START",
    "RESUME": "START",
    "RUN": "START",
    "PAUSE": "PAUSE",
    "STOP": "PAUSE",
    "DISABLE": "PAUSE",
    "SUSPEND": "PAUSE",
    "SET_BUDGET": "SET_BUDGET",
    "UPDATE_BUDGET": "SET_BUDGET",
    "SET_ROAS": "SET_ROAS",
    "UPDATE_ROAS": "SET_ROAS",
    "ADJUST_ROI": "SET_ROAS",
}


async def apply_campaign_action(
    db: Session,
    ttb_client: TTBApiClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: TTBGmvMaxCampaign,
    action: str,
    payload: dict | None = None,
    reason: str | None = None,
    performed_by: str = "system",
    audit_hook: Callable[..., Any] | None = None,
) -> TTBGmvMaxActionLog:
    requested_action = str(action or "").strip().upper()
    normalized_action = _ACTION_NORMALIZATION.get(requested_action, requested_action)
    if normalized_action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported action: {action}")

    payload = dict(payload or {})
    before_state = {
        "status": campaign.status,
        "daily_budget_cents": campaign.daily_budget_cents,
        "roas_bid": campaign.roas_bid,
    }

    api_body: dict[str, Any] = {"campaign_id": campaign.campaign_id}
    after_state = dict(before_state)

    if normalized_action == "START":
        api_body["is_enabled"] = True
        after_state["status"] = "ACTIVE"
    elif normalized_action == "PAUSE":
        api_body["is_enabled"] = False
        after_state["status"] = "PAUSED"
    elif normalized_action == "SET_BUDGET":
        budget_cents_value = payload.pop("daily_budget_cents", None)
        cents = _to_int(budget_cents_value) if budget_cents_value is not None else None
        if cents is None:
            raise ValueError("daily_budget_cents required for SET_BUDGET")
        api_body["budget"] = _cents_to_currency(cents)
        after_state["daily_budget_cents"] = cents
    elif normalized_action == "SET_ROAS":
        roas_value = payload.pop("roas_bid", None)
        roas_decimal = _to_decimal(roas_value, quantize=_DECIMAL_FOUR)
        if roas_decimal is None:
            raise ValueError("roas_bid required for SET_ROAS")
        api_body["roas_bid"] = format(roas_decimal, "f")
        after_state["roas_bid"] = roas_decimal

    for key in list(payload.keys()):
        api_body[key] = payload[key]

    try:
        await ttb_client.update_gmvmax_campaign(advertiser_id, api_body)
    except Exception as exc:  # noqa: BLE001
        log_campaign_action(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign=campaign,
            action=normalized_action,
            reason=reason,
            before=before_state,
            after=before_state,
            performed_by=performed_by,
            result="FAILED",
            error_message=str(exc),
            audit_hook=audit_hook,
        )
        raise

    if normalized_action == "SET_BUDGET":
        campaign.daily_budget_cents = after_state["daily_budget_cents"]
    elif normalized_action == "SET_ROAS":
        campaign.roas_bid = after_state["roas_bid"]
    else:
        campaign.status = after_state["status"]

    db.add(campaign)
    db.flush()

    return log_campaign_action(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
        action=normalized_action,
        reason=reason,
        before=before_state,
        after=after_state,
        performed_by=performed_by,
        result="SUCCESS",
        audit_hook=audit_hook,
    )


class StrategyDecision(TypedDict):
    action: str
    payload: dict[str, Any]
    reason: str


def get_or_create_strategy_config(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: TTBGmvMaxCampaign,
) -> TTBGmvMaxStrategyConfig:
    stmt = (
        select(TTBGmvMaxStrategyConfig)
        .where(TTBGmvMaxStrategyConfig.workspace_id == workspace_id)
        .where(TTBGmvMaxStrategyConfig.auth_id == auth_id)
        .where(TTBGmvMaxStrategyConfig.campaign_id == campaign.campaign_id)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = TTBGmvMaxStrategyConfig(
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign.campaign_id,
            enabled=False,
        )
        db.add(instance)
        db.flush()
    return instance


def _sum_int(values: list[Optional[int]]) -> int:
    return int(sum(v or 0 for v in values))


def _calc_roi(gross_cents: Optional[int], cost_cents: Optional[int]) -> Optional[Decimal]:
    if not gross_cents or not cost_cents:
        return None
    if cost_cents <= 0:
        return None
    try:
        return (Decimal(gross_cents) / Decimal(cost_cents)).quantize(_DECIMAL_FOUR)
    except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - guard rails
        return None


def aggregate_recent_metrics(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    hours_window: int = 6,
    days_window: int = 1,
) -> dict[str, Any]:
    now = datetime.utcnow()

    rows_day: list[TTBGmvMaxMetricsDaily] = []
    if days_window > 0:
        day_from = now.date() - timedelta(days=days_window)
        stmt_day = (
            select(TTBGmvMaxMetricsDaily)
            .where(TTBGmvMaxMetricsDaily.campaign_id == campaign.id)
            .where(TTBGmvMaxMetricsDaily.date >= day_from)
            .where(TTBGmvMaxMetricsDaily.date <= now.date())
        )
        rows_day = db.execute(stmt_day).scalars().all()

    rows_hour: list[TTBGmvMaxMetricsHourly] = []
    if hours_window > 0:
        ts_from = now - timedelta(hours=hours_window)
        stmt_hour = (
            select(TTBGmvMaxMetricsHourly)
            .where(TTBGmvMaxMetricsHourly.campaign_id == campaign.id)
            .where(TTBGmvMaxMetricsHourly.interval_start >= ts_from)
        )
        rows_hour = db.execute(stmt_hour).scalars().all()

    def _rows(column: str, source: list[Any]) -> list[Optional[int]]:
        return [getattr(item, column, None) for item in source]

    base_rows: list[Any] = rows_hour or rows_day
    impressions = _sum_int(_rows("impressions", base_rows))
    clicks = _sum_int(_rows("clicks", base_rows))
    cost_cents = _sum_int(_rows("cost_cents", base_rows))
    gross_revenue_cents = _sum_int(_rows("gross_revenue_cents", base_rows))

    return {
        "impressions": impressions,
        "clicks": clicks,
        "cost_cents": cost_cents,
        "gross_revenue_cents": gross_revenue_cents,
        "roi": _calc_roi(gross_revenue_cents, cost_cents),
    }


def decide_campaign_action(
    *,
    campaign: TTBGmvMaxCampaign,
    strategy: TTBGmvMaxStrategyConfig,
    metrics: dict[str, Any],
) -> Optional[StrategyDecision]:
    if not strategy.enabled:
        return None

    impressions = metrics.get("impressions") or 0
    clicks = metrics.get("clicks") or 0
    roi = metrics.get("roi")

    min_impr = strategy.min_impressions or 0
    min_clicks = strategy.min_clicks or 0
    if impressions < min_impr or clicks < min_clicks:
        return None

    current_budget = campaign.daily_budget_cents or 0
    current_roas = campaign.roas_bid

    min_roi = strategy.min_roi
    target_roi = strategy.target_roi

    max_raise_pct = strategy.max_budget_raise_pct_per_day or Decimal("0")
    max_cut_pct = strategy.max_budget_cut_pct_per_day or Decimal("0")
    max_roas_step = strategy.max_roas_step_per_adjust or Decimal("0")

    if roi is None:
        return None

    if min_roi is not None and roi < min_roi:
        if current_budget and current_budget <= 1000:
            return StrategyDecision(
                action="PAUSE",
                payload={},
                reason=f"auto: roi({roi}) < min_roi({min_roi})",
            )
        if current_budget and max_cut_pct > 0:
            new_budget = int(
                Decimal(current_budget)
                * (Decimal("1") - (max_cut_pct / Decimal("100")))
            )
            new_budget = max(new_budget, 100)
            if new_budget < current_budget:
                return StrategyDecision(
                    action="SET_BUDGET",
                    payload={"daily_budget_cents": new_budget},
                    reason=f"auto: roi({roi}) < min_roi({min_roi}), cut budget",
                )
        return None

    if target_roi is not None and roi > target_roi and current_budget > 0:
        if max_raise_pct > 0:
            new_budget = int(
                Decimal(current_budget)
                * (Decimal("1") + (max_raise_pct / Decimal("100")))
            )
            if new_budget > current_budget:
                return StrategyDecision(
                    action="SET_BUDGET",
                    payload={"daily_budget_cents": new_budget},
                    reason=f"auto: roi({roi}) > target_roi({target_roi}), raise budget",
                )
        if current_roas is not None and max_roas_step > 0:
            try:
                new_roas = (Decimal(current_roas) + max_roas_step).quantize(_DECIMAL_FOUR)
            except (InvalidOperation, ValueError):
                return None
            return StrategyDecision(
                action="SET_ROAS",
                payload={"roas_bid": format(new_roas, "f")},
                reason=f"auto: roi({roi}) > target_roi({target_roi}), adjust roas",
            )

    return None
