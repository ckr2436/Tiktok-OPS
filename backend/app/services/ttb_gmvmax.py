"""GMV Max service layer: syncs campaigns, metrics, and local state."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Collection, Mapping, Optional, Sequence, TypedDict

from sqlalchemy import select, func, delete, update
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import (
    GmvActionLog,
    GmvCampaign,
    GmvCampaignCreative,
    GmvCampaignLivestream,
    GmvCampaignMetricsDaily,
    GmvCampaignMetricsHourly,
    GmvCampaignProduct,
    GmvCreativeMetricsDaily,
    GmvCreativeMetricsHourly,
    GmvDurationMetricsDaily,
    GmvDurationMetricsHourly,
    GmvLivestreamMetricsDaily,
    GmvLivestreamMetricsHourly,
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
    GmvStrategyConfig,
    PromotionTypeEnum,
)
from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.repositories.tiktok_business import gmvmax as gmvmax_repo
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignCreateRequest,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignReportRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxCreativeReportRequest,
    GMVMaxDataset,
    GMVMaxExclusiveAuthorizationCreateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxIdentityInfo,
    GMVMaxMetricsLevel,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxReportTimeRange,
    GMVMaxStoreListRequest,
    build_gmv_max_report_request,
    TikTokBusinessGMVMaxClient,
)
from app.gmvmax.services.campaign_mapper import map_gmvmax_campaign_info_to_model
from app.services.gmvmax_spec import (
    GMVMAX_BASE_METRICS,
    GMVMAX_SUPPORTED_METRICS,
    GMVMAX_DEFAULT_METRICS,
    GMVMAX_CREATIVE_METRICS,
    GMVMaxReportLevel,
    GMV_REPORT_CONFIG,
)
from app.services.ttb_api import TTBApiError, TTBBusinessError
from app.services.ttb_binding_config import get_binding_config


logger = logging.getLogger("gmv.tenants.gmvmax")


def _ensure_campaign_not_deleted(campaign: GmvCampaign | None) -> None:
    if campaign is None:
        return
    if getattr(campaign, "is_deleted", False):
        raise TTBBusinessError(
            "This campaign has been deleted on TikTok and can no longer be updated.",
            code="CAMPAIGN_DELETED",
            payload={"campaign_id": getattr(campaign, "campaign_id", None)},
        )

__all__ = [
    "upsert_campaign_from_api",
    "sync_gmvmax_campaigns",
    "upsert_metrics_hourly_row",
    "sync_gmvmax_metrics_hourly",
    "upsert_metrics_daily_row",
    "sync_gmvmax_metrics_daily",
    "sync_gmvmax_reports_for_campaign",
    "log_campaign_action",
    "apply_campaign_action",
    "get_or_create_strategy_config",
    "aggregate_recent_metrics",
    "decide_campaign_action",
    "resolve_store_id_from_page_context",
    "ensure_gmvmax_store_authorized",
    "build_gmvmax_anchor_params",
    "create_gmvmax_campaign",
    "update_gmvmax_campaign",
    "fetch_gmvmax_report_by_level",
    "get_item_group_ids_for_campaign",
    "fetch_and_cache_campaign_detail",
]

GMVMAX_LEVEL_TABLES: dict[str, list[str]] = {
    "OVERVIEW": ["gmv_overview_metrics_daily", "gmv_overview_metrics_hourly"],
    "CAMPAIGN": ["gmv_campaign_metrics_daily", "gmv_campaign_metrics_hourly"],
    "PRODUCT": ["gmv_product_metrics_daily", "gmv_product_metrics_hourly"],
    "LIVESTREAM": ["gmv_livestream_metrics_daily", "gmv_livestream_metrics_hourly"],
    "DURATION": ["gmv_duration_metrics_daily", "gmv_duration_metrics_hourly"],
    "CREATIVE": [
        "gmv_creative_metrics_daily",
        "gmv_creative_metrics_hourly",
        "gmv_creative_metrics_10min",
    ],
}


def _log_sync_target(level: str, *, granularity: str | None = None) -> None:
    logger.info(
        "gmvmax metrics sync target mapping",
        extra={
            "level": level,
            "granularity": granularity,
            "tables": GMVMAX_LEVEL_TABLES.get(level),
        },
    )


_DECIMAL_FOUR = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")


def _sanitize_id_list(values: Sequence[str] | Sequence[int] | None) -> list[str] | None:
    """Drop falsy and sentinel values from an ID list."""

    if not values:
        return None

    cleaned: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() == "all":
            continue
        cleaned.append(text)
    return cleaned or None
_DEFAULT_REPORT_METRICS = list(GMVMAX_DEFAULT_METRICS)


class GMVMaxReportEntry(TypedDict):
    metrics: dict[str, Any]
    dimensions: dict[str, Any]


_CREATIVE_ATTRIBUTE_FIELDS = (
    "creative_name",
    "creative_status",
    "creative_delivery_status",
    "adgroup_id",
    "product_id",
    "item_id",
)

_REPORT_PAGE_SIZE = 200


async def fetch_gmvmax_report_by_level(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    campaign_ids: Sequence[str] | None = None,
    level: str,
    start_date: date | str,
    end_date: date | str,
    item_group_ids: Sequence[str] | None = None,
) -> list[GMVMaxReportEntry]:
    """Fetch GMV Max metrics from TikTok for the requested level."""

    clean_campaign_ids = _sanitize_id_list(campaign_ids)
    clean_item_group_ids = _sanitize_id_list(item_group_ids)
    level_value = GMVMaxMetricsLevel(level)

    if level_value is GMVMaxMetricsLevel.CREATIVE and not clean_item_group_ids:
        raise TTBBusinessError(
            "item_group_ids are required for creative level reports",
            code="GMVMAX_REPORT_ITEM_GROUP_REQUIRED",
            payload={
                "campaign_id": campaign_id,
                "campaign_ids": clean_campaign_ids,
                "item_group_ids": item_group_ids,
            },
        )

    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)
    response = await client.fetch_gmvmax_report(
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        campaign_id=str(campaign_id),
        campaign_ids=clean_campaign_ids,
        level=level_value,
        start_date=start_date_str,
        end_date=end_date_str,
        item_group_ids=clean_item_group_ids,
    )
    data = getattr(response, "data", None)
    raw_entries = getattr(data, "list", None) or []

    def _build_dimensions(payload: Mapping[str, Any]) -> dict[str, Any]:
        base = {
            "campaign_id": payload.get("campaign_id"),
            "store_id": str(store_id),
            "stat_time_day": payload.get("stat_time_day")
            or payload.get("date")
            or start_date_str,
        }
        if level_value is GMVMaxMetricsLevel.PRODUCT:
            base["product_id"] = payload.get("item_group_id") or payload.get("product_id")
        if level_value is GMVMaxMetricsLevel.CREATIVE:
            base["product_id"] = payload.get("item_group_id") or payload.get("product_id")
            base["shop_content_id"] = payload.get("item_id") or payload.get("creative_id")
        return base

    mapped_entries: list[GMVMaxReportEntry] = []
    for entry in raw_entries:
        payload = _merge_report_entry(entry)
        metrics_payload = {
            metric: payload.get(metric)
            for metric in GMVMAX_SUPPORTED_METRICS
            if metric in payload
        }
        dimensions_payload = _build_dimensions(payload)
        mapped_entries.append(
            GMVMaxReportEntry(metrics=metrics_payload, dimensions=dimensions_payload)
        )

    return mapped_entries


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_bound_store_id(db: Session, *, workspace_id: int, auth_id: int) -> str | None:
    """Return the configured store_id for the binding, if any."""

    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    return _normalize_identifier(binding.store_id) if binding else None


def _require_single_identifier(
    *,
    values: Sequence[str] | None,
    field: str,
) -> list[str]:
    normalized = [
        _normalize_identifier(value) or ""
        for value in (values or [])
        if _normalize_identifier(value)
    ]
    if len(normalized) != 1:
        raise TTBApiError(
            f"{field} must contain exactly one id when requesting attributes",
            code="GMVMAX_REPORT_ATTRIBUTE_SCOPE",
            payload={field: values},
        )
    return normalized


async def ensure_gmvmax_store_authorized(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    target_store_id: str,
) -> str:
    """Validate store availability and ensure exclusive authorization exists."""

    logger.info(
        "gmvmax.ensure_store_authorized.list",
        extra={"advertiser_id": advertiser_id, "store_id": target_store_id},
    )
    matched_store = None
    page = 1
    page_size = 200
    while matched_store is None:
        store_list_request = GMVMaxStoreListRequest(
            advertiser_id=str(advertiser_id), page=page, page_size=page_size
        )
        store_list_response = await client.gmv_max_store_list(store_list_request)
        store_list = (
            getattr(store_list_response.data, "store_list", []) if store_list_response else []
        )
        for store in store_list:
            if _normalize_identifier(getattr(store, "store_id", None)) == str(
                target_store_id
            ):
                matched_store = store
                break

        if matched_store:
            break

        page_info = getattr(store_list_response.data, "page_info", None)
        current_page = getattr(page_info, "page", None) or page
        total_page = getattr(page_info, "total_page", None)
        has_more = bool(getattr(page_info, "has_next", None)) or bool(
            getattr(page_info, "has_more", None)
        )
        if has_more or (total_page and current_page < total_page):
            page = current_page + 1
        else:
            break

    if matched_store is None:
        raise TTBApiError(
            "store is not available for the advertiser",
            code="GMVMAX_STORE_UNAVAILABLE",
            payload={"store_id": target_store_id, "advertiser_id": advertiser_id},
        )

    if not bool(getattr(matched_store, "is_gmv_max_available", False)):
        raise TTBApiError(
            "store does not support GMV Max",
            code="GMVMAX_STORE_NOT_AVAILABLE",
            payload={
                "store_id": target_store_id,
                "advertiser_id": advertiser_id,
                "authorization_status": getattr(
                    matched_store, "gmv_max_authorization_status", None
                ),
            },
        )

    store_authorized_bc_id = _normalize_identifier(
        getattr(matched_store, "store_authorized_bc_id", None)
    )
    if not store_authorized_bc_id:
        raise TTBApiError(
            "store_authorized_bc_id missing from GMV Max store list",
            code="GMVMAX_STORE_MISSING_BC",
            payload={"store_id": target_store_id, "advertiser_id": advertiser_id},
        )

    get_request = GMVMaxExclusiveAuthorizationGetRequest(
        advertiser_id=str(advertiser_id),
        store_id=str(target_store_id),
        store_authorized_bc_id=store_authorized_bc_id,
    )
    logger.info(
        "gmvmax.ensure_store_authorized.get",
        extra={"advertiser_id": advertiser_id, "store_id": target_store_id},
    )
    get_response = await client.gmv_max_exclusive_authorization_get(get_request)
    if get_response and getattr(get_response, "data", None):
        data = get_response.data
        if bool(getattr(data, "is_authorized", False)):
            return _normalize_identifier(getattr(data, "store_authorized_bc_id", None)) or store_authorized_bc_id

    create_request = GMVMaxExclusiveAuthorizationCreateRequest(
        advertiser_id=str(advertiser_id),
        store_id=str(target_store_id),
        store_authorized_bc_id=store_authorized_bc_id,
    )
    logger.info(
        "gmvmax.ensure_store_authorized.create",
        extra={"advertiser_id": advertiser_id, "store_id": target_store_id},
    )
    create_response = await client.gmv_max_exclusive_authorization_create(create_request)
    auth_data = getattr(create_response, "data", None)
    if auth_data:
        authorized_id = _normalize_identifier(getattr(auth_data, "store_authorized_bc_id", None))
        if authorized_id:
            return authorized_id
    return store_authorized_bc_id


async def build_gmvmax_anchor_params(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    shopping_ads_type: str,
    store_id: str,
    store_authorized_bc_id: str,
    product_specific_type: str | None = None,
    item_group_ids: Sequence[str] | None = None,
    product_video_specific_type: str | None = None,
    identity_ids: Sequence[str] | None = None,
) -> dict:
    """Construct anchor params for GMV Max campaign creation based on ad type."""

    normalized_type = (shopping_ads_type or "").upper()
    anchor: dict[str, Any] = {"shopping_ads_type": normalized_type}

    if normalized_type == "LIVE":
        identity_request = GMVMaxIdentityGetRequest(
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            store_authorized_bc_id=str(store_authorized_bc_id),
        )
        identity_response = await client.gmv_max_identity_get(identity_request)
        available = []
        if identity_response and getattr(identity_response, "data", None):
            for entry in identity_response.data.identity_list:
                identity = getattr(entry, "identity_info", None)
                if identity is None:
                    continue
                live_available = getattr(entry, "live_gmv_max_available", None)
                if live_available is None:
                    live_available = getattr(identity, "live_gmv_max_available", None)
                if live_available is False:
                    continue
                identity_id = _normalize_identifier(getattr(identity, "identity_id", None))
                if identity_id:
                    available.append(identity_id)

        chosen_id = None
        if identity_ids:
            if len(identity_ids) != 1:
                raise TTBApiError(
                    "live campaigns require exactly one identity",
                    code="GMVMAX_LIVE_IDENTITY_INVALID",
                    payload={"identity_ids": list(identity_ids)},
                )
            chosen_id = _normalize_identifier(identity_ids[0])

        if chosen_id and chosen_id not in available:
            raise TTBApiError(
                "selected identity is not eligible for live GMV Max",
                code="GMVMAX_LIVE_IDENTITY_UNAVAILABLE",
                payload={"identity_id": chosen_id, "available": available},
            )

        if not chosen_id:
            if not available:
                raise TTBApiError(
                    "no eligible identities found for live GMV Max",
                    code="GMVMAX_LIVE_IDENTITY_MISSING",
                    payload={"store_id": store_id},
                )
            chosen_id = available[0]

        anchor["identity_list"] = [GMVMaxIdentityInfo(identity_id=chosen_id)]
        return anchor

    resolved_product_type = (product_specific_type or "").upper()
    if not resolved_product_type:
        resolved_product_type = "ALL" if not item_group_ids else "CUSTOMIZED_PRODUCTS"
    anchor["product_specific_type"] = resolved_product_type

    if resolved_product_type != "ALL" and item_group_ids:
        anchor["item_group_ids"] = [str(item) for item in item_group_ids if item is not None]

    if product_video_specific_type:
        anchor["product_video_specific_type"] = str(product_video_specific_type)

    if identity_ids:
        anchor["identity_list"] = [
            GMVMaxIdentityInfo(identity_id=_normalize_identifier(identity_id))
            for identity_id in identity_ids
            if _normalize_identifier(identity_id)
        ]

    return anchor


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


def _resolve_store_id_for_metrics(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
) -> str | None:
    """Pick the best-effort store_id for metrics sync.

    Falls back to advertiser↔store links when the campaign record does not
    carry a store_id, ensuring metrics sync does not silently skip entire
    workspaces.
    """

    store_id = _normalize_identifier(campaign.store_id)
    if store_id:
        return store_id

    stmt = (
        select(TTBAdvertiserStoreLink.store_id)
        .where(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .where(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .where(TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
    )
    candidate_rows = [row[0] for row in db.execute(stmt).all() if row[0]]
    deduped = list(
        dict.fromkeys(_normalize_identifier(item) or "" for item in candidate_rows)
    )
    deduped = [item for item in deduped if item]
    if not deduped:
        logger.warning(
            "skip metrics sync because store_id missing and no links found",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign.campaign_id,
            },
        )
        return None

    if len(deduped) > 1:
        logger.warning(
            "multiple store links found for advertiser; defaulting to first",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign.campaign_id,
                "store_candidates": deduped,
            },
        )

    chosen = deduped[0]
    campaign.store_id = chosen
    db.add(campaign)
    return chosen


def _resolve_room_ids_for_campaign(db: Session, *, campaign_id: str) -> list[str]:
    stmt = select(GmvCampaignLivestream.room_id).where(
        GmvCampaignLivestream.campaign_id == str(campaign_id)
    )
    rows = [row[0] for row in db.execute(stmt).all() if row and row[0]]
    deduped = list(dict.fromkeys(_normalize_identifier(value) or "" for value in rows))
    return [value for value in deduped if value]


def _pick_dataset_for_level(
    *, campaign: GmvCampaign, level: GMVMaxReportLevel
) -> GMVMaxDataset:
    promotion_type = (_normalize_identifier(campaign.shopping_ads_type) or "PRODUCT").upper()
    if level == GMVMaxReportLevel.OVERVIEW:
        return GMVMaxDataset.OVERVIEW
    if level == GMVMaxReportLevel.CAMPAIGN:
        return (
            GMVMaxDataset.LIVE_CAMPAIGN
            if promotion_type == "LIVE"
            else GMVMaxDataset.PRODUCT_CAMPAIGN
        )
    if level == GMVMaxReportLevel.PRODUCT:
        return GMVMaxDataset.PRODUCT_PRODUCT
    if level == GMVMaxReportLevel.CREATIVE:
        return GMVMaxDataset.CREATIVE
    if level == GMVMaxReportLevel.ROOM:
        return GMVMaxDataset.LIVE_LIVESTREAM
    if level == GMVMaxReportLevel.SESSION:
        return GMVMaxDataset.LIVE_DURATION
    raise ValueError(f"unsupported report level: {level}")


def _apply_attribute_scope_constraints(
    *,
    request: GMVMaxReportGetRequest,
    level: GMVMaxReportLevel,
    include_attributes: bool,
) -> GMVMaxReportGetRequest:
    if not include_attributes or level == GMVMaxReportLevel.CAMPAIGN:
        return request

    id_fields: dict[GMVMaxReportLevel, tuple[str, str]] = {
        GMVMaxReportLevel.PRODUCT: ("item_group_ids", "item_group_id"),
        GMVMaxReportLevel.CREATIVE: ("item_group_ids", "item_id"),
        GMVMaxReportLevel.ROOM: ("room_ids", "room_id"),
        GMVMaxReportLevel.SESSION: ("room_ids", "duration"),
    }

    filter_field, dimension_field = id_fields[level]
    filter_values = getattr(request, filter_field, None)
    if not filter_values:
        return request
    normalized_ids = _require_single_identifier(values=filter_values, field=filter_field)
    setattr(request, filter_field, normalized_ids)

    if request.campaign_ids:
        campaign_ids = _require_single_identifier(
            values=request.campaign_ids, field="campaign_ids"
        )
        request.campaign_ids = campaign_ids

    dimensions = [dimension_field]
    if "stat_time_day" in GMV_REPORT_CONFIG.get(level, {}).get("dimensions", ()):  # type: ignore[arg-type]
        dimensions.append("stat_time_day")
    request.dimensions = dimensions
    return request


def _build_level_report_request(
    *,
    campaign: GmvCampaign,
    store_id: str,
    level: GMVMaxReportLevel,
    start_date: str,
    end_date: str,
    metrics: Sequence[str],
    include_attributes: bool,
    campaign_ids: Sequence[str] | None = None,
    item_group_ids: Sequence[str] | None = None,
    room_ids: Sequence[str] | None = None,
) -> GMVMaxReportGetRequest:
    dataset = _pick_dataset_for_level(campaign=campaign, level=level)
    request = build_gmv_max_report_request(
        dataset=dataset,
        advertiser_id=str(campaign.advertiser_id),
        store_ids=[str(store_id)],
        start_date=start_date,
        end_date=end_date,
        metrics=list(metrics),
        campaign_ids=list(campaign_ids) if campaign_ids else [str(campaign.campaign_id)],
        item_group_ids=list(item_group_ids) if item_group_ids else None,
        room_ids=list(room_ids) if room_ids else None,
        page_size=_REPORT_PAGE_SIZE,
    )

    if include_attributes:
        attribute_scopes: dict[GMVMaxReportLevel, Sequence[str] | None] = {
            GMVMaxReportLevel.PRODUCT: item_group_ids,
            GMVMaxReportLevel.CREATIVE: item_group_ids,
            GMVMaxReportLevel.ROOM: room_ids,
            GMVMaxReportLevel.SESSION: room_ids,
        }
        attribute_scope_values = attribute_scopes.get(level)
        include_attributes = bool(attribute_scope_values)

    if include_attributes:
        request = _apply_attribute_scope_constraints(
            request=request, level=level, include_attributes=include_attributes
        )

    if "stat_time_day" not in request.dimensions and level not in {
        GMVMaxReportLevel.CREATIVE,
    }:
        request.dimensions = list(request.dimensions) + ["stat_time_day"]

    return request


def _merge_report_entry(entry: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if entry is None:
        return payload
    if isinstance(entry, Mapping):
        payload.update(entry.get("metrics") or {})
        payload.update(entry.get("dimensions") or {})
    else:
        payload.update(getattr(entry, "metrics", {}) or {})
        payload.update(getattr(entry, "dimensions", {}) or {})
    return payload


async def create_gmvmax_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    client: TikTokBusinessGMVMaxClient,
    body: GMVMaxCampaignCreateBody,
) -> GmvCampaign:
    body_dump = body.model_dump(exclude_none=False)
    if body_dump.get("store_id") and not body_dump.get("store_authorized_bc_id"):
        authorized_bc_id = await ensure_gmvmax_store_authorized(
            client,
            advertiser_id=str(advertiser_id),
            target_store_id=str(body_dump["store_id"]),
        )
        body = body.copy(update={"store_authorized_bc_id": authorized_bc_id})

    request = GMVMaxCampaignCreateRequest(advertiser_id=str(advertiser_id), body=body)
    response = await client.gmv_max_campaign_create(request)
    raw_data = response.data
    if hasattr(raw_data, "model_dump"):
        campaign_data: dict[str, Any] = raw_data.model_dump(exclude_none=True)
    elif isinstance(raw_data, Mapping):
        campaign_data = dict(raw_data)
    else:
        campaign_data = {}

    campaign_payload = campaign_data.get("campaign") if isinstance(campaign_data, Mapping) else None
    if not isinstance(campaign_payload, dict):
        campaign_payload = dict(campaign_data)

    body_dump = body.model_dump(exclude_none=True)
    if campaign_payload.get("campaign_id") is None and body_dump.get("campaign_id"):
        campaign_payload["campaign_id"] = body_dump["campaign_id"]
    if "item_group_ids" not in campaign_payload and body_dump.get("item_group_ids"):
        campaign_payload["item_group_ids"] = [str(item) for item in body_dump.get("item_group_ids", [])]

    row = upsert_campaign_from_api(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=campaign_payload,
        store_id_hint=str(body_dump.get("store_id")) if body_dump.get("store_id") is not None else None,
        campaign_details=campaign_data,
    )
    db.flush()
    return row


async def update_gmvmax_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    client: TikTokBusinessGMVMaxClient,
    body: GMVMaxCampaignUpdateBody,
) -> GmvCampaign:
    request = GMVMaxCampaignUpdateRequest(advertiser_id=str(advertiser_id), body=body)
    response = await client.gmv_max_campaign_update(request)
    raw_data = response.data
    if hasattr(raw_data, "model_dump"):
        campaign_data: dict[str, Any] = raw_data.model_dump(exclude_none=True)
    elif isinstance(raw_data, Mapping):
        campaign_data = dict(raw_data)
    else:
        campaign_data = {}

    campaign_payload = campaign_data.get("campaign") if isinstance(campaign_data, Mapping) else None
    if not isinstance(campaign_payload, dict):
        campaign_payload = dict(campaign_data)

    body_dump = body.model_dump(exclude_none=True)
    if campaign_payload.get("campaign_id") is None and body_dump.get("campaign_id"):
        campaign_payload["campaign_id"] = body_dump["campaign_id"]
    if "item_group_ids" not in campaign_payload and body_dump.get("item_group_ids"):
        campaign_payload["item_group_ids"] = [str(item) for item in body_dump.get("item_group_ids", [])]

    target_campaign_id = _normalize_identifier(body_dump.get("campaign_id"))
    if target_campaign_id:
        existing = (
            db.execute(
                select(GmvCampaign)
                .where(GmvCampaign.workspace_id == workspace_id)
                .where(GmvCampaign.auth_id == auth_id)
                .where(GmvCampaign.campaign_id == target_campaign_id)
                .where(GmvCampaign.advertiser_id == str(advertiser_id))
            )
            .scalars()
            .first()
        )
        _ensure_campaign_not_deleted(existing)

    row = upsert_campaign_from_api(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=campaign_payload,
        store_id_hint=str(body_dump.get("store_id")) if body_dump.get("store_id") is not None else None,
        campaign_details=campaign_data,
    )
    db.flush()
    return row


def _assign_sqlite_pk(db: Session, row: GmvCampaign) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(row, "id", None):
        return
    next_value = db.execute(
        select(func.coalesce(func.max(GmvCampaign.id), 0))
    ).scalar_one()
    row.id = int(next_value or 0) + 1


def _migrate_campaign_products(db: Session, *, source_id: int, target_id: int) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    stmt = (
        update(GmvCampaignProduct)
        .where(GmvCampaignProduct.campaign_pk == int(source_id))
        .values(campaign_pk=int(target_id))
    )
    db.execute(stmt)


def _migrate_action_logs(db: Session, *, source_id: int, target_id: int) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    stmt = (
        update(GmvActionLog)
        .where(GmvActionLog.campaign_id == int(source_id))
        .values(campaign_id=int(target_id))
    )
    db.execute(stmt)


def _merge_duplicate_campaign_rows(
    db: Session,
    *,
    campaign_rows: Sequence[GmvCampaign],
) -> GmvCampaign:
    if not campaign_rows:
        raise ValueError("campaign_rows must not be empty")
    primary = campaign_rows[0]
    if not getattr(primary, "id", None):
        return primary
    duplicates = [row for row in campaign_rows[1:] if getattr(row, "id", None)]
    if not duplicates:
        return primary
    logger.warning(
        "detected duplicate gmvmax campaign rows; merging",  # noqa: G004
        extra={
            "workspace_id": primary.workspace_id,
            "advertiser_id": primary.advertiser_id,
            "campaign_id": primary.campaign_id,
            "duplicates": [row.id for row in duplicates],
            "kept": primary.id,
        },
    )
    for duplicate in duplicates:
        _migrate_campaign_products(
            db,
            source_id=int(duplicate.id),
            target_id=int(primary.id),
        )
        _migrate_action_logs(
            db,
            source_id=int(duplicate.id),
            target_id=int(primary.id),
        )
        db.delete(duplicate)
    return primary


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
    campaign: GmvCampaign,
    product_ids: Sequence[str],
    store_id_hint: str | None,
    operation_status: Any,
    promotion_type: PromotionTypeEnum,
) -> None:
    normalized_status = _normalize_status_value(operation_status)
    store_id = (
        _normalize_identifier(store_id_hint)
        or _normalize_identifier(getattr(campaign, "store_id", None))
        or ""
    )

    campaign_pk = getattr(campaign, "id", None)
    if campaign_pk is None:
        db.flush([campaign])
        campaign_pk = getattr(campaign, "id", None)
    if campaign_pk is None:
        raise ValueError("campaign.id must be available before syncing products")

    incoming_ids: set[str] = set()
    for product_id in product_ids:
        normalized = _normalize_identifier(product_id)
        if normalized:
            incoming_ids.add(normalized)

    if not incoming_ids:
        return

    existing_products = (
        db.execute(
            select(GmvCampaignProduct)
            .where(GmvCampaignProduct.workspace_id == campaign.workspace_id)
            .where(GmvCampaignProduct.auth_id == campaign.auth_id)
            .where(GmvCampaignProduct.store_id == store_id)
            .where(GmvCampaignProduct.item_group_id.in_(incoming_ids))
        )
        .scalars()
        .all()
    )

    existing_by_item_group_id = {item.item_group_id: item for item in existing_products}

    to_update = incoming_ids & set(existing_by_item_group_id)
    to_insert = incoming_ids - set(existing_by_item_group_id)

    for item_group_id in to_update:
        record = existing_by_item_group_id[item_group_id]
        record.campaign_pk = campaign_pk
        record.campaign_id = campaign.campaign_id
        record.store_id = store_id
        record.promotion_type = promotion_type
        record.operation_status = normalized_status

    if to_insert:
        db.add_all(
            [
                GmvCampaignProduct(
                    workspace_id=campaign.workspace_id,
                    auth_id=campaign.auth_id,
                    campaign_pk=campaign_pk,
                    campaign_id=campaign.campaign_id,
                    item_group_id=item_group_id,
                    promotion_type=promotion_type,
                    store_id=store_id,
                    operation_status=normalized_status,
                )
                for item_group_id in to_insert
            ]
        )


def _list_campaign_product_ids(
    db: Session, *, campaign: GmvCampaign
) -> list[str]:
    if not getattr(campaign, "id", None):
        return []

    rows = (
        db.execute(
            select(GmvCampaignProduct.item_group_id)
            .where(GmvCampaignProduct.campaign_pk == int(campaign.id))
            .where(GmvCampaignProduct.store_id == str(campaign.store_id or ""))
            .where(GmvCampaignProduct.operation_status == "ENABLE")
        )
        .scalars()
        .all()
    )
    return [item for item in (_normalize_identifier(row) for row in rows) if item]


def _extract_item_group_ids_from_campaign_payload(raw_payload: Any) -> list[str]:
    if not raw_payload:
        return []

    payload: Mapping[str, Any] | None
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except Exception:  # noqa: BLE001
            return []
    elif isinstance(raw_payload, Mapping):
        payload = raw_payload
    else:
        return []

    item_group_ids: Any = payload.get("item_group_ids")
    if not item_group_ids:
        campaign_info = payload.get("_campaign_info")
        if isinstance(campaign_info, Mapping):
            item_group_ids = campaign_info.get("item_group_ids")

    if not item_group_ids:
        return []

    if isinstance(item_group_ids, str):
        item_group_ids = [item_group_ids]

    normalized = [_normalize_identifier(value) for value in item_group_ids]
    deduped = list(dict.fromkeys(filter(None, normalized)))
    return deduped


def get_item_group_ids_for_campaign(
    db: Session, *, campaign: GmvCampaign
) -> list[str]:
    """Return item_group_ids for a campaign, backfilling product rows when needed."""

    item_group_ids = _list_campaign_product_ids(db, campaign=campaign)
    if item_group_ids:
        return item_group_ids

    raw_item_group_ids = _extract_item_group_ids_from_campaign_payload(campaign.raw_json)
    normalized_item_group_ids = [
        _normalize_identifier(value) or "" for value in (raw_item_group_ids or [])
    ]
    deduped_item_group_ids = list(dict.fromkeys(filter(None, normalized_item_group_ids)))

    if not deduped_item_group_ids:
        return []

    store_id = str(campaign.store_id or "")
    promotion_type = _normalize_promotion_type(campaign.shopping_ads_type)
    normalized_status = _normalize_status_value(campaign.operation_status)
    operation_status = normalized_status or "ENABLE"
    for item_group_id in deduped_item_group_ids:
        existing = (
            db.query(GmvCampaignProduct)
            .filter_by(
                workspace_id=campaign.workspace_id,
                auth_id=campaign.auth_id,
                store_id=store_id,
                item_group_id=item_group_id,
            )
            .one_or_none()
        )

        new_values = dict(
            campaign_pk=campaign.id,
            campaign_id=campaign.campaign_id,
            promotion_type=promotion_type,
            operation_status=operation_status,
        )

        if existing is None:
            db.add(
                GmvCampaignProduct(
                    workspace_id=campaign.workspace_id,
                    auth_id=campaign.auth_id,
                    store_id=store_id,
                    item_group_id=item_group_id,
                    **new_values,
                )
            )
            continue

        if (
            existing.campaign_pk == new_values["campaign_pk"]
            and existing.campaign_id == new_values["campaign_id"]
            and existing.promotion_type == new_values["promotion_type"]
            and existing.operation_status == new_values["operation_status"]
        ):
            continue

        if existing.campaign_pk != new_values["campaign_pk"]:
            existing.campaign_pk = new_values["campaign_pk"]
        if existing.campaign_id != new_values["campaign_id"]:
            existing.campaign_id = new_values["campaign_id"]
        if existing.promotion_type != new_values["promotion_type"]:
            existing.promotion_type = new_values["promotion_type"]
        if existing.operation_status != new_values["operation_status"]:
            existing.operation_status = new_values["operation_status"]

    # Flush pending inserts/updates. In rare cases multiple workers may try to
    # insert the same (workspace_id, auth_id, store_id, item_group_id) mapping
    # concurrently, which would raise a duplicate-key IntegrityError on
    # gmv_campaign_products.uk_gmv_store_product_unique. Treat that as benign
    # by re-reading existing mappings from the database.
    from sqlalchemy.exc import IntegrityError  # local import to avoid touching global imports

    try:
        # Use a nested transaction so that a duplicate-key failure here does
        # not force the outer transaction into a failed state.
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:  # pragma: no cover - defensive concurrency guard
        message = str(getattr(exc, "orig", exc))
        if "gmv_campaign_products.uk_gmv_store_product_unique" not in message:
            # Not the duplicate-key we expect; bubble it up.
            raise
        # Another transaction inserted the rows first; reuse the mappings
        # already present in the database.
        return _list_campaign_product_ids(db, campaign=campaign)

    return deduped_item_group_ids


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
    if not text:
        return None
    if text in {"ENABLE", "DISABLE"}:
        return text
    return None


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


def _normalize_promotion_type(value: Any, fallback: PromotionTypeEnum = PromotionTypeEnum.PRODUCT) -> PromotionTypeEnum:
    normalized = _normalize_identifier(value)
    if normalized:
        candidate = normalized.upper()
        if candidate == "PRODUCT_GMV_MAX":
            candidate = "PRODUCT"
        try:
            return PromotionTypeEnum(candidate)
        except ValueError:  # pragma: no cover - defensive fallback
            return fallback
    return fallback


def _normalize_metric_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    cost_cents_value = _extract_field(row, "cost_cents")
    net_cost_cents_value = _extract_field(row, "net_cost_cents")
    gross_revenue_cents_value = _extract_field(row, "gross_revenue_cents")
    all_shops_gross_revenue_cents_value = _extract_field(
        row, "all_shops_gross_revenue_cents"
    )

    return {
        "impressions": _to_int(
            _extract_field(row, "impressions", "show_cnt", "views", "product_impressions")
        ),
        "product_impressions": _to_int(_extract_field(row, "product_impressions")),
        "clicks": _to_int(_extract_field(row, "clicks", "click", "click_cnt")),
        "product_clicks": _to_int(
            _extract_field(row, "product_clicks", "product_click", "product_click_cnt")
        ),
        "product_click_rate": _to_decimal(
            _extract_field(row, "product_click_rate"), quantize=_DECIMAL_FOUR
        ),
        "cost_cents": _to_int(cost_cents_value)
        if cost_cents_value is not None
        else _to_cents(_extract_field(row, "cost", "spend", "total_spend", "total_cost")),
        "net_cost_cents": _to_int(net_cost_cents_value)
        if net_cost_cents_value is not None
        else _to_cents(_extract_field(row, "net_cost")),
        "orders": _to_int(_extract_field(row, "orders", "order_num", "conversions")),
        "cost_per_order": _to_decimal(
            _extract_field(row, "cost_per_order"), quantize=_DECIMAL_FOUR
        ),
        "gross_revenue_cents": _to_int(gross_revenue_cents_value)
        if gross_revenue_cents_value is not None
        else _to_cents(_extract_field(row, "gross_revenue", "gmv", "revenue")),
        "roi": _to_decimal(_extract_field(row, "roi", "roas"), quantize=_DECIMAL_FOUR),
        "ad_click_rate": _to_decimal(
            _extract_field(row, "ad_click_rate", "ctr"), quantize=_DECIMAL_FOUR
        ),
        "ad_conversion_rate": _to_decimal(
            _extract_field(row, "ad_conversion_rate", "conversion_rate", "cvr"),
            quantize=_DECIMAL_FOUR,
        ),
        "conversion_rate": _to_decimal(
            _extract_field(row, "conversion_rate", "ad_conversion_rate", "cvr"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_2s": _to_decimal(
            _extract_field(row, "video_view_rate_2s", "ad_video_view_rate_2s"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_6s": _to_decimal(
            _extract_field(row, "video_view_rate_6s", "ad_video_view_rate_6s"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_25": _to_decimal(
            _extract_field(row, "video_view_rate_25", "ad_video_view_rate_p25"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_50": _to_decimal(
            _extract_field(row, "video_view_rate_50", "ad_video_view_rate_p50"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_75": _to_decimal(
            _extract_field(row, "video_view_rate_75", "ad_video_view_rate_p75"),
            quantize=_DECIMAL_FOUR,
        ),
        "video_view_rate_100": _to_decimal(
            _extract_field(row, "video_view_rate_100", "ad_video_view_rate_p100"),
            quantize=_DECIMAL_FOUR,
        ),
        "cost_per_live_view": _to_decimal(
            _extract_field(row, "cost_per_live_view"), quantize=_DECIMAL_FOUR
        ),
        "cost_per_10s_live_view": _to_decimal(
            _extract_field(
                row,
                "cost_per_10s_live_view",
                "cost_per_10_second_live_view",
            ),
            quantize=_DECIMAL_FOUR,
        ),
        "all_shops_orders": _to_int(_extract_field(row, "all_shops_orders")),
        "all_shops_gross_revenue_cents": _to_int(all_shops_gross_revenue_cents_value)
        if all_shops_gross_revenue_cents_value is not None
        else _to_cents(_extract_field(row, "all_shops_gross_revenue")),
        "all_shops_roi": _to_decimal(
            _extract_field(row, "all_shops_roi"), quantize=_DECIMAL_FOUR
        ),
        "all_shops_cost_per_order": _to_decimal(
            _extract_field(row, "all_shops_cost_per_order"), quantize=_DECIMAL_FOUR
        ),
    }


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
    ttb_client: TikTokBusinessGMVMaxClient,
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
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    **filters: Any,
) -> dict:
    provided_filters = {k: v for k, v in filters.items() if v is not None}
    store_scope = provided_filters.get("store_ids")
    campaign_scope = provided_filters.get("campaign_ids")
    if store_scope is not None:
        store_scope = [str(item) for item in store_scope]
    if campaign_scope is not None:
        campaign_scope = [str(item) for item in campaign_scope]

    bound_store_id = _get_bound_store_id(db, workspace_id=workspace_id, auth_id=auth_id)
    if bound_store_id:
        store_scope = [bound_store_id]
        provided_filters["store_ids"] = [bound_store_id]
    else:
        logger.warning(
            "gmvmax.sync_campaigns: missing bound store; falling back to provided filters",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
            },
        )

    base_filters = {k: v for k, v in provided_filters.items() if v is not None}
    if store_scope:
        base_filters["store_ids"] = store_scope
    if campaign_scope:
        base_filters["campaign_ids"] = campaign_scope
    promotion_filters = base_filters.get("gmv_max_promotion_types") or []
    if not promotion_filters:
        base_filters["gmv_max_promotion_types"] = ["PRODUCT_GMV_MAX"]

    synced = 0
    details_cache: dict[str, Mapping[str, Any] | None] = {}

    async def _sync_round(primary_status: str | None) -> None:
        nonlocal synced
        page = 1
        while True:
            # NOTE:
            # TikTok 不接受 STATUS_NOT_DELETE 作为 primary_status 的枚举值。
            # 想要“未删除”（STATUS_NOT_DELETE）的行为，必须完全省略 primary_status 字段。
            filtering_kwargs: dict[str, Any] = {
                "gmv_max_promotion_types": base_filters.get(
                    "gmv_max_promotion_types", ["PRODUCT_GMV_MAX"]
                ),
                "store_ids": base_filters.get("store_ids"),
                "campaign_ids": base_filters.get("campaign_ids"),
            }
            if primary_status is not None:
                filtering_kwargs["primary_status"] = primary_status

            request = GMVMaxCampaignGetRequest(
                advertiser_id=str(advertiser_id),
                filtering=GMVMaxCampaignFiltering(**filtering_kwargs),
                page_size=50,
                page=page,
            )
            response = await ttb_client.gmv_max_campaign_get(request)
            data = response.data

            page_context: Mapping[str, Any] | None = None
            try:  # pragma: no cover - defensive fallback for unexpected payloads
                if hasattr(data, "model_dump"):
                    page_context = data.model_dump(exclude_none=True)
            except Exception:
                page_context = None
            if not isinstance(page_context, Mapping) and isinstance(data, Mapping):
                page_context = data

            for item in data.list or []:
                payload: Mapping[str, Any] | dict[str, Any]
                if isinstance(item, Mapping):
                    payload = item
                else:
                    try:
                        payload = item.model_dump()
                    except AttributeError:
                        payload = dict(item)

                if not isinstance(payload, Mapping):
                    continue

                campaign_identifier = _normalize_identifier(
                    _extract_field(payload, "campaign_id", "id")
                )
                if not campaign_identifier:
                    continue

                campaign_details: Mapping[str, Any] | None = None
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
                        advertiser_id=str(advertiser_id),
                        campaign_payload=payload,
                        page_context=page_context or {},
                    )
                if not resolved_store_id and campaign_details:
                    resolved_store_id = _extract_field_from_sources(
                        ("store_id", "shop_id"), campaign_details
                    )

                normalized_store_id = _normalize_identifier(resolved_store_id)
                if bound_store_id:
                    if normalized_store_id is None:
                        logger.info(
                            "skipping campaign without store_id due to binding",
                            extra={
                                "workspace_id": workspace_id,
                                "auth_id": auth_id,
                                "advertiser_id": advertiser_id,
                                "campaign_id": campaign_identifier,
                                "bound_store_id": bound_store_id,
                            },
                        )
                        continue
                    if normalized_store_id != bound_store_id:
                        logger.info(
                            "skipping campaign for unrelated store",
                            extra={
                                "workspace_id": workspace_id,
                                "auth_id": auth_id,
                                "advertiser_id": advertiser_id,
                                "campaign_id": campaign_identifier,
                                "store_id": normalized_store_id,
                                "bound_store_id": bound_store_id,
                            },
                        )
                        continue

                promotion_type = _normalize_promotion_type(
                    _extract_field_from_sources(
                        ("gmv_max_promotion_type", "promotion_type", "shopping_ads_type"),
                        campaign_details,
                        payload,
                    )
                )
                store_for_round = normalized_store_id or str(resolved_store_id or "")

                if (
                    bound_store_id
                    and store_for_round
                    and store_for_round != bound_store_id
                ):
                    logger.info(
                        "gmvmax.sync_campaigns: skip campaign %s for unrelated store",
                        campaign_identifier,
                        extra={
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "advertiser_id": advertiser_id,
                            "campaign_store_id": store_for_round,
                            "bound_store_id": bound_store_id,
                        },
                    )
                    continue

                upsert_campaign_from_api(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    payload=payload,
                    store_id_hint=store_for_round,
                    campaign_details=campaign_details,
                    promotion_type=promotion_type,
                )
                synced += 1

            page_info = data.page_info
            if not page_info or not page_info.total_page or page >= page_info.total_page:
                break
            page += 1

    # 第一轮：不传 primary_status（等价于 STATUS_NOT_DELETE，只返回未删除系列）
    # 第二轮：显式 STATUS_DELETE，用于同步已删除系列
    await _sync_round(None)
    await _sync_round("STATUS_DELETE")

    db.flush()

    return {"synced": synced, "removed": 0}


def upsert_campaign_from_api(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: dict,
    store_id_hint: str | None = None,
    campaign_details: Mapping[str, Any] | None = None,
    promotion_type: PromotionTypeEnum | None = None,
) -> GmvCampaign:
    if not isinstance(payload, dict):
        raise ValueError("payload must be dict")
    campaign_identifier = _extract_field(payload, "campaign_id", "id")
    if not campaign_identifier:
        raise ValueError("campaign_id missing in payload")
    campaign_id = str(campaign_identifier)

    normalized_advertiser = str(advertiser_id)
    existing = (
        db.execute(
            select(GmvCampaign)
            .where(GmvCampaign.workspace_id == workspace_id)
            .where(GmvCampaign.auth_id == auth_id)
            .where(GmvCampaign.campaign_id == campaign_id)
        )
        .scalars()
        .first()
    )

    promotion_type_raw = _extract_field_from_sources(
        ("gmv_max_promotion_type", "promotion_type", "shopping_ads_type"),
        payload,
        campaign_details,
    )
    promotion_type_value = promotion_type or _normalize_promotion_type(promotion_type_raw)

    normalized_status = _normalize_status_value(
        _extract_field_from_sources(("status", "campaign_status"), payload, campaign_details)
    )

    store_candidates = [
        _extract_field_from_sources(("store_id", "shop_id"), campaign_details),
        _extract_field_from_sources(("store_id", "shop_id"), payload),
        store_id_hint,
        _lookup_store_id_from_links(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign_payload=payload,
        ),
    ]
    store_identifier: str | None = None
    for candidate in store_candidates:
        normalized = _normalize_identifier(candidate)
        if normalized:
            store_identifier = normalized
            break
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

    currency_value = _extract_field_from_sources(
        ("currency", "budget_currency"), payload, campaign_details
    )

    merged_payload: dict[str, Any] = dict(payload)
    if isinstance(campaign_details, Mapping) and campaign_details:
        merged_payload["_campaign_info"] = dict(campaign_details)

    result = map_gmvmax_campaign_info_to_model(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=normalized_advertiser,
        info=merged_payload,
        campaign_id=campaign_id,
        status_value=normalized_status,
        store_id_hint=store_identifier,
        currency_fallback=str(currency_value) if currency_value is not None else None,
        promotion_type_override=promotion_type_value,
        synced_at=datetime.now(timezone.utc),
        existing=existing,
    )
    if existing is None:
        db.add(result)

    operation_status_value = _extract_field_from_sources(
        ("operation_status",), payload, campaign_details
    )
    product_ids = _extract_item_group_ids_from_payload(payload)
    if isinstance(campaign_details, Mapping):
        detail_products = _extract_item_group_ids_from_payload(campaign_details)
        if detail_products:
            product_ids = sorted({*product_ids, *detail_products})
    _sync_campaign_product_assignments(
        db,
        campaign=result,
        product_ids=product_ids,
        store_id_hint=store_identifier,
        operation_status=operation_status_value,
        promotion_type=promotion_type_value,
    )

    db.flush()
    return result


async def fetch_and_cache_campaign_detail(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    include_sessions: bool = True,
) -> dict[str, Any]:
    _ = include_sessions
    bound_store_id = _get_bound_store_id(db, workspace_id=workspace_id, auth_id=auth_id)
    info_request = GMVMaxCampaignInfoRequest(
        advertiser_id=str(advertiser_id), campaign_id=str(campaign_id)
    )
    info_resp = await ttb_client.gmv_max_campaign_info(info_request)

    normalized_store = _normalize_identifier(info_resp.data.store_id)
    existing_row = (
        db.execute(
            select(GmvCampaign)
            .where(GmvCampaign.workspace_id == workspace_id)
            .where(GmvCampaign.auth_id == auth_id)
            .where(GmvCampaign.campaign_id == str(campaign_id))
        )
        .scalars()
        .first()
    )
    resolved_store = normalized_store or _normalize_identifier(getattr(existing_row, "store_id", None))
    if bound_store_id and (resolved_store is None or resolved_store != bound_store_id):
        raise TTBBusinessError(
            "Campaign does not belong to the bound store",
            code="GMVMAX_CAMPAIGN_STORE_MISMATCH",
            payload={
                "campaign_id": str(campaign_id),
                "campaign_store_id": resolved_store,
                "bound_store_id": bound_store_id,
            },
        )

    local_row: GmvCampaign | None = upsert_campaign_from_api(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=info_resp.data.model_dump(exclude_none=True),
        store_id_hint=info_resp.data.store_id,
        campaign_details={
            "campaign_id": info_resp.data.campaign_id,
            "store_id": info_resp.data.store_id,
        },
    )
    db.flush()

    synced_at = datetime.now(timezone.utc)
    store_id = info_resp.data.store_id or (local_row.store_id if local_row else "")

    upsert_campaign_from_api(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=info_resp.data.model_dump(exclude_none=True),
        store_id_hint=str(store_id or ""),
        campaign_details=info_resp.data.model_dump(exclude_none=True),
    )

    db.commit()

    return {
        "campaign_id": str(campaign_id),
        "request_id": info_resp.request_id,
        "sessions_request_id": None,
        "synced_at": synced_at.isoformat(),
    }


def _upsert_product_metrics_hourly(
    db: Session,
    *,
    campaign_id: str,
    stat_time_hour: datetime,
    item_group_id: str,
    metrics: Mapping[str, Any],
    bid_type: Any | None = None,
) -> GmvProductMetricsHourly:
    stmt = (
        select(GmvProductMetricsHourly)
        .where(GmvProductMetricsHourly.campaign_id == campaign_id)
        .where(GmvProductMetricsHourly.item_group_id == item_group_id)
        .where(GmvProductMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvProductMetricsHourly(
            campaign_id=campaign_id,
            item_group_id=item_group_id,
            stat_time_hour=stat_time_hour,
        )
        db.add(instance)

    for field, value in metrics.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    if bid_type is not None:
        instance.bid_type = str(bid_type)

    db.flush()
    return instance


def _upsert_product_metrics_daily(
    db: Session,
    *,
    campaign_id: str,
    stat_time_day: date,
    item_group_id: str,
    metrics: Mapping[str, Any],
    bid_type: Any | None = None,
) -> GmvProductMetricsDaily:
    stmt = (
        select(GmvProductMetricsDaily)
        .where(GmvProductMetricsDaily.campaign_id == campaign_id)
        .where(GmvProductMetricsDaily.item_group_id == item_group_id)
        .where(GmvProductMetricsDaily.stat_time_day == stat_time_day)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvProductMetricsDaily(
            campaign_id=campaign_id,
            item_group_id=item_group_id,
            stat_time_day=stat_time_day,
        )
        db.add(instance)

    for field, value in metrics.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    if bid_type is not None:
        instance.bid_type = str(bid_type)

    db.flush()
    return instance


def _upsert_creative_metrics(
    db: Session,
    *,
    campaign_id: str,
    creative_id: str,
    metrics_row: Mapping[str, Any],
    stat_time_day: date | None = None,
    stat_time_hour: datetime | None = None,
    item_group_id: str | None = None,
) -> GmvCreativeMetricsDaily | GmvCreativeMetricsHourly:
    if stat_time_day is None and stat_time_hour is None:
        raise ValueError("stat_time required")

    metrics = _normalize_metric_payload(metrics_row)
    normalized_item = _normalize_identifier(item_group_id) if item_group_id else None

    if stat_time_day is not None:
        stmt = (
            select(GmvCreativeMetricsDaily)
            .where(GmvCreativeMetricsDaily.campaign_id == campaign_id)
            .where(GmvCreativeMetricsDaily.creative_id == creative_id)
            .where(GmvCreativeMetricsDaily.stat_time_day == stat_time_day)
        )
        instance = db.execute(stmt).scalars().first()
        if instance is None:
            instance = GmvCreativeMetricsDaily(
                campaign_id=campaign_id,
                creative_id=creative_id,
                stat_time_day=stat_time_day,
            )
            db.add(instance)
    else:
        stmt = (
            select(GmvCreativeMetricsHourly)
            .where(GmvCreativeMetricsHourly.campaign_id == campaign_id)
            .where(GmvCreativeMetricsHourly.creative_id == creative_id)
            .where(GmvCreativeMetricsHourly.stat_time_hour == stat_time_hour)
        )
        instance = db.execute(stmt).scalars().first()
        if instance is None and stat_time_hour is not None:
            instance = GmvCreativeMetricsHourly(
                campaign_id=campaign_id,
                creative_id=creative_id,
                stat_time_hour=stat_time_hour,
            )
            db.add(instance)

    if normalized_item:
        instance.item_group_id = normalized_item

    for field, value in metrics.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    db.flush()
    return instance


def upsert_metrics_hourly_row(
    db: Session,
    *,
    campaign: GmvCampaign,
    row: dict,
) -> GmvCampaignMetricsHourly:
    if not isinstance(row, dict):
        raise ValueError("row must be dict")
    stat_time_value = _extract_field(
        row,
        "stat_time_hour",
        "interval_start",
        "interval_start_time",
        "start_time",
        "stat_time",
    )
    stat_time_hour = _parse_datetime(stat_time_value)
    if stat_time_hour is None:
        raise ValueError("interval_start missing")

    promotion_type = _normalize_promotion_type(
        _extract_field(row, "promotion_type", "gmv_max_promotion_type", "gmv_max_promotion_types"),
        fallback=_normalize_promotion_type(campaign.shopping_ads_type),
    )

    store_id = _normalize_identifier(getattr(campaign, "store_id", None)) or ""

    stmt = (
        select(GmvCampaignMetricsHourly)
        .where(GmvCampaignMetricsHourly.workspace_id == campaign.workspace_id)
        .where(GmvCampaignMetricsHourly.auth_id == campaign.auth_id)
        .where(GmvCampaignMetricsHourly.advertiser_id == campaign.advertiser_id)
        .where(GmvCampaignMetricsHourly.store_id == store_id)
        .where(GmvCampaignMetricsHourly.campaign_id == str(campaign.campaign_id))
        .where(GmvCampaignMetricsHourly.promotion_type == promotion_type)
        .where(GmvCampaignMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvCampaignMetricsHourly(
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            store_id=store_id,
            campaign_id=str(campaign.campaign_id),
            promotion_type=promotion_type,
            stat_time_hour=stat_time_hour,
        )
        db.add(instance)

    instance.workspace_id = campaign.workspace_id
    instance.auth_id = campaign.auth_id
    instance.advertiser_id = campaign.advertiser_id
    if store_id:
        instance.store_id = store_id

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    instance.live_views = _to_int(_extract_field(row, "live_views", "live_watch_cnt"))
    instance.live_10s_views = _to_int(
        _extract_field(row, "live_10s_views", "live_view_10s", "live_views_10s")
    )
    instance.live_follows = _to_int(_extract_field(row, "live_follows", "live_followers"))

    item_group_id = _normalize_identifier(
        _extract_field(row, "item_group_id", "product_id", "itemId", "spu_id", "item_id")
    )
    if item_group_id:
        _upsert_product_metrics_hourly(
            db,
            campaign_id=str(campaign.campaign_id),
            stat_time_hour=stat_time_hour,
            item_group_id=item_group_id,
            metrics=metrics_payload,
            bid_type=_extract_field(row, "bid_type"),
        )

    db.flush()
    return instance


async def sync_gmvmax_metrics_hourly(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict:
    _log_sync_target("CAMPAIGN", granularity="HOURLY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    dimensions = ["campaign_id", "stat_time_hour"]
    campaign_ids = [campaign.campaign_id]
    request = _build_campaign_report_request(
        advertiser_id=str(advertiser_id),
        campaign_ids=campaign_ids,
        store_id=store_id,
        start_date=start_date_str,
        end_date=end_date_str,
        granularity="HOURLY",
        metrics=_DEFAULT_REPORT_METRICS,
        dimensions=dimensions,
        page=1,
        page_size=_REPORT_PAGE_SIZE,
    )
    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, dict)]
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

    db.flush()
    return {"synced_rows": synced_rows}


def upsert_metrics_daily_row(
    db: Session,
    *,
    campaign: GmvCampaign,
    row: dict,
) -> GmvCampaignMetricsDaily:
    if not isinstance(row, dict):
        raise ValueError("row must be dict")
    date_value = _extract_field(row, "stat_time_day", "date", "stat_time")
    stat_date = _parse_date(date_value)
    if stat_date is None:
        raise ValueError("date missing")

    promotion_type = _normalize_promotion_type(
        _extract_field(row, "promotion_type", "gmv_max_promotion_type", "gmv_max_promotion_types"),
        fallback=_normalize_promotion_type(campaign.shopping_ads_type),
    )

    store_id = _normalize_identifier(getattr(campaign, "store_id", None)) or ""

    stmt = (
        select(GmvCampaignMetricsDaily)
        .where(GmvCampaignMetricsDaily.workspace_id == campaign.workspace_id)
        .where(GmvCampaignMetricsDaily.auth_id == campaign.auth_id)
        .where(GmvCampaignMetricsDaily.advertiser_id == campaign.advertiser_id)
        .where(GmvCampaignMetricsDaily.store_id == store_id)
        .where(GmvCampaignMetricsDaily.campaign_id == str(campaign.campaign_id))
        .where(GmvCampaignMetricsDaily.promotion_type == promotion_type)
        .where(GmvCampaignMetricsDaily.stat_time_day == stat_date)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvCampaignMetricsDaily(
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            store_id=store_id,
            campaign_id=str(campaign.campaign_id),
            promotion_type=promotion_type,
            stat_time_day=stat_date,
        )
        db.add(instance)

    instance.workspace_id = campaign.workspace_id
    instance.auth_id = campaign.auth_id
    instance.advertiser_id = campaign.advertiser_id
    if store_id:
        instance.store_id = store_id

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    instance.live_views = _to_int(_extract_field(row, "live_views", "live_watch_cnt"))
    instance.live_10s_views = _to_int(
        _extract_field(row, "live_10s_views", "live_view_10s", "live_views_10s")
    )
    instance.live_follows = _to_int(_extract_field(row, "live_follows", "live_followers"))

    item_group_id = _normalize_identifier(
        _extract_field(row, "item_group_id", "product_id", "itemId", "spu_id", "item_id")
    )
    if item_group_id:
        _upsert_product_metrics_daily(
            db,
            campaign_id=str(campaign.campaign_id),
            stat_time_day=stat_date,
            item_group_id=item_group_id,
            metrics=metrics_payload,
            bid_type=_extract_field(row, "bid_type"),
        )

    db.flush()
    return instance


def _upsert_livestream_metrics_daily(
    db: Session,
    *,
    campaign_id: str,
    room_id: str,
    row: Mapping[str, Any],
) -> GmvLivestreamMetricsDaily:
    stat_time_day = _parse_date(_extract_field(row, "stat_time_day", "stat_time"))
    if stat_time_day is None:
        raise ValueError("date missing")

    stmt = (
        select(GmvLivestreamMetricsDaily)
        .where(GmvLivestreamMetricsDaily.room_id == str(room_id))
        .where(GmvLivestreamMetricsDaily.stat_time_day == stat_time_day)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvLivestreamMetricsDaily(room_id=str(room_id), stat_time_day=stat_time_day)
        db.add(instance)

    instance.campaign_id = str(campaign_id)
    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    for attr, value in {
        "live_views": _to_int(_extract_field(row, "live_views", "live_watch_cnt")),
        "live_10s_views": _to_int(
            _extract_field(row, "live_10s_views", "live_view_10s", "live_views_10s")
        ),
        "live_follows": _to_int(_extract_field(row, "live_follows", "live_followers")),
    }.items():
        if value is not None and hasattr(instance, attr):
            setattr(instance, attr, value)

    db.flush()
    return instance


def _upsert_livestream_metrics_hourly(
    db: Session,
    *,
    campaign_id: str,
    room_id: str,
    row: Mapping[str, Any],
) -> GmvLivestreamMetricsHourly:
    stat_time_value = _extract_field(row, "stat_time_hour", "interval_start", "stat_time")
    stat_time_hour = _parse_datetime(stat_time_value)
    if stat_time_hour is None:
        raise ValueError("hour missing")

    stmt = (
        select(GmvLivestreamMetricsHourly)
        .where(GmvLivestreamMetricsHourly.room_id == str(room_id))
        .where(GmvLivestreamMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvLivestreamMetricsHourly(
            room_id=str(room_id), stat_time_hour=stat_time_hour
        )
        db.add(instance)

    instance.campaign_id = str(campaign_id)
    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    for attr, value in {
        "live_views": _to_int(_extract_field(row, "live_views", "live_watch_cnt")),
        "live_10s_views": _to_int(
            _extract_field(row, "live_10s_views", "live_view_10s", "live_views_10s")
        ),
        "live_follows": _to_int(_extract_field(row, "live_follows", "live_followers")),
    }.items():
        if value is not None and hasattr(instance, attr):
            setattr(instance, attr, value)

    db.flush()
    return instance


def _upsert_duration_metrics_daily(
    db: Session,
    *,
    campaign_id: str,
    row: Mapping[str, Any],
) -> GmvDurationMetricsDaily:
    stat_time_day = _parse_date(_extract_field(row, "stat_time_day", "stat_time"))
    if stat_time_day is None:
        raise ValueError("date missing")

    duration_value = _normalize_identifier(_extract_field(row, "duration"))
    if not duration_value:
        raise ValueError("duration missing")

    item_group_id = _normalize_identifier(_extract_field(row, "item_group_id", "product_id"))

    stmt = (
        select(GmvDurationMetricsDaily)
        .where(GmvDurationMetricsDaily.campaign_id == str(campaign_id))
        .where(GmvDurationMetricsDaily.duration == str(duration_value))
        .where(GmvDurationMetricsDaily.stat_time_day == stat_time_day)
    )
    if item_group_id:
        stmt = stmt.where(GmvDurationMetricsDaily.item_group_id == item_group_id)

    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvDurationMetricsDaily(
            campaign_id=str(campaign_id),
            duration=str(duration_value),
            stat_time_day=stat_time_day,
            item_group_id=item_group_id,
        )
        db.add(instance)

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    if hasattr(instance, "bid_type"):
        instance.bid_type = _extract_field(row, "bid_type")

    db.flush()
    return instance


def _upsert_duration_metrics_hourly(
    db: Session,
    *,
    campaign_id: str,
    row: Mapping[str, Any],
) -> GmvDurationMetricsHourly:
    stat_time_value = _extract_field(row, "stat_time_hour", "interval_start", "stat_time")
    stat_time_hour = _parse_datetime(stat_time_value)
    if stat_time_hour is None:
        raise ValueError("hour missing")

    duration_value = _normalize_identifier(_extract_field(row, "duration"))
    if not duration_value:
        raise ValueError("duration missing")

    item_group_id = _normalize_identifier(_extract_field(row, "item_group_id", "product_id"))

    stmt = (
        select(GmvDurationMetricsHourly)
        .where(GmvDurationMetricsHourly.campaign_id == str(campaign_id))
        .where(GmvDurationMetricsHourly.duration == str(duration_value))
        .where(GmvDurationMetricsHourly.stat_time_hour == stat_time_hour)
    )
    if item_group_id:
        stmt = stmt.where(GmvDurationMetricsHourly.item_group_id == item_group_id)

    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvDurationMetricsHourly(
            campaign_id=str(campaign_id),
            duration=str(duration_value),
            stat_time_hour=stat_time_hour,
            item_group_id=item_group_id,
        )
        db.add(instance)

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    if hasattr(instance, "bid_type"):
        instance.bid_type = _extract_field(row, "bid_type")

    db.flush()
    return instance


async def sync_gmvmax_product_metrics_hourly(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict[str, Any]:
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        logger.warning(
            "gmvmax product hourly metrics sync skipped: missing store_id",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign.campaign_id,
                "granularity": "HOURLY",
            },
        )
        return {"synced_rows": 0}

    campaign_ids = [campaign.campaign_id]
    page = 1
    while True:
        request = _build_campaign_report_request(
            advertiser_id=str(advertiser_id),
            campaign_ids=campaign_ids,
            store_id=store_id,
            start_date=start_date_str,
            end_date=end_date_str,
            granularity="HOURLY",
            metrics=_DEFAULT_REPORT_METRICS,
            dimensions=["campaign_id", "item_group_id", "stat_time_hour"],
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        try:
            response = await ttb_client.gmv_max_report_get(request)
        except Exception:
            logger.exception(
                "gmvmax product hourly report fetch failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign.campaign_id,
                    "page": page,
                },
            )
            raise

        data = getattr(response, "data", None)
        rows_raw = getattr(data, "list", None) or []
        rows = [_merge_report_entry(item) for item in rows_raw]
        rows = [row for row in rows if isinstance(row, dict)]
        for row in rows:
            stat_time_value = _extract_field(row, "stat_time_hour", "stat_time", "interval_start")
            stat_time_hour = _parse_datetime(stat_time_value)
            item_group_id = _normalize_identifier(
                _extract_field(row, "item_group_id", "product_id", "itemId", "spu_id", "item_id")
            )
            if stat_time_hour is None or not item_group_id:
                logger.debug(
                    "skip product hourly row missing identifiers",
                    extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id, "auth_id": auth_id},
                )
                continue

            metrics_payload = _normalize_metric_payload(row)
            _upsert_product_metrics_hourly(
                db,
                campaign_id=str(campaign.campaign_id),
                stat_time_hour=stat_time_hour,
                item_group_id=item_group_id,
                metrics=metrics_payload,
                bid_type=_extract_field(row, "bid_type"),
            )
            synced_rows += 1

        page_info = getattr(data, "page_info", None)
        has_more = bool(getattr(page_info, "has_more", False) or getattr(page_info, "has_next", False))
        total_page = getattr(page_info, "total_page", None) if page_info else None
        if has_more:
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


async def sync_gmvmax_product_metrics_daily(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict[str, Any]:
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        logger.warning(
            "gmvmax product daily metrics sync skipped: missing store_id",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign.campaign_id,
                "granularity": "DAILY",
            },
        )
        return {"synced_rows": 0}

    campaign_ids = [campaign.campaign_id]
    page = 1
    while True:
        request = _build_campaign_report_request(
            advertiser_id=str(advertiser_id),
            campaign_ids=campaign_ids,
            store_id=store_id,
            start_date=start_date_str,
            end_date=end_date_str,
            granularity="DAILY",
            metrics=_DEFAULT_REPORT_METRICS,
            dimensions=["campaign_id", "item_group_id", "stat_time_day"],
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        try:
            response = await ttb_client.gmv_max_report_get(request)
        except Exception:
            logger.exception(
                "gmvmax product daily report fetch failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign.campaign_id,
                    "page": page,
                },
            )
            raise

        data = getattr(response, "data", None)
        rows_raw = getattr(data, "list", None) or []
        rows = [_merge_report_entry(item) for item in rows_raw]
        rows = [row for row in rows if isinstance(row, dict)]
        for row in rows:
            stat_date = _parse_date(_extract_field(row, "stat_time_day", "date", "stat_time"))
            item_group_id = _normalize_identifier(
                _extract_field(row, "item_group_id", "product_id", "itemId", "spu_id", "item_id")
            )
            if stat_date is None or not item_group_id:
                logger.debug(
                    "skip product daily row missing identifiers",
                    extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id, "auth_id": auth_id},
                )
                continue

            metrics_payload = _normalize_metric_payload(row)
            _upsert_product_metrics_daily(
                db,
                campaign_id=str(campaign.campaign_id),
                stat_time_day=stat_date,
                item_group_id=item_group_id,
                metrics=metrics_payload,
                bid_type=_extract_field(row, "bid_type"),
            )
            synced_rows += 1

        page_info = getattr(data, "page_info", None)
        has_more = bool(getattr(page_info, "has_more", False) or getattr(page_info, "has_next", False))
        total_page = getattr(page_info, "total_page", None) if page_info else None
        if has_more:
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


def _upsert_overview_daily(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    row: Mapping[str, Any],
) -> GmvOverviewMetricsDaily:
    stat_date = _parse_date(_extract_field(row, "stat_time_day", "date", "stat_time"))
    if stat_date is None:
        raise ValueError("date missing")

    stmt = (
        select(GmvOverviewMetricsDaily)
        .where(GmvOverviewMetricsDaily.workspace_id == int(workspace_id))
        .where(GmvOverviewMetricsDaily.auth_id == int(auth_id))
        .where(GmvOverviewMetricsDaily.advertiser_id == str(advertiser_id))
        .where(GmvOverviewMetricsDaily.store_id == str(store_id))
        .where(GmvOverviewMetricsDaily.stat_time_day == stat_date)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvOverviewMetricsDaily(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            stat_time_day=stat_date,
        )
        db.add(instance)

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    db.flush()
    return instance


def _upsert_overview_hourly(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    row: Mapping[str, Any],
) -> GmvOverviewMetricsHourly:
    stat_time_value = _extract_field(row, "stat_time_hour", "stat_time")
    stat_time_hour = _parse_datetime(stat_time_value)
    if stat_time_hour is None:
        raise ValueError("hour missing")

    stmt = (
        select(GmvOverviewMetricsHourly)
        .where(GmvOverviewMetricsHourly.workspace_id == int(workspace_id))
        .where(GmvOverviewMetricsHourly.auth_id == int(auth_id))
        .where(GmvOverviewMetricsHourly.advertiser_id == str(advertiser_id))
        .where(GmvOverviewMetricsHourly.store_id == str(store_id))
        .where(GmvOverviewMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvOverviewMetricsHourly(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            stat_time_hour=stat_time_hour,
        )
        db.add(instance)

    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    db.flush()
    return instance


def _build_campaign_report_request(
    *,
    advertiser_id: str,
    campaign_ids: Sequence[str],
    store_id: str | None,
    start_date: str,
    end_date: str,
    granularity: str,
    metrics: Sequence[str],
    dimensions: Sequence[str],
    page: int,
    page_size: int,
) -> GMVMaxReportGetRequest:
    filtering = GMVMaxReportFiltering(
        gmv_max_promotion_types=["PRODUCT_GMV_MAX"],
        store_ids=[store_id] if store_id else None,
        campaign_ids=[str(cid) for cid in campaign_ids],
    )
    return GMVMaxReportGetRequest(
        advertiser_id=str(advertiser_id),
        store_ids=[store_id] if store_id else [],
        start_date=start_date,
        end_date=end_date,
        metrics=list(metrics),
        dimensions=list(dimensions),
        gmv_max_promotion_types=["PRODUCT_GMV_MAX"],
        campaign_ids=list(campaign_ids),
        filtering=filtering,
        page=page,
        page_size=page_size,
    )


async def sync_gmvmax_metrics_daily(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict:
    _log_sync_target("PRODUCT", granularity="DAILY")
    _log_sync_target("PRODUCT", granularity="HOURLY")
    _log_sync_target("CAMPAIGN", granularity="DAILY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    synced_rows = 0
    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    dimensions = ["campaign_id", "stat_time_day"]
    campaign_ids = [campaign.campaign_id]
    request = _build_campaign_report_request(
        advertiser_id=str(advertiser_id),
        campaign_ids=campaign_ids,
        store_id=store_id,
        start_date=start_date_str,
        end_date=end_date_str,
        granularity="DAILY",
        metrics=_DEFAULT_REPORT_METRICS,
        dimensions=dimensions,
        page=1,
        page_size=_REPORT_PAGE_SIZE,
    )
    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, dict)]
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

    db.flush()
    return {"synced_rows": synced_rows}


async def sync_gmvmax_overview_metrics(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_ids: Sequence[str],
    start_date: date | str,
    end_date: date | str,
    granularity: str = "DAILY",
) -> dict:
    _log_sync_target("OVERVIEW", granularity=str(granularity or "").strip().upper())
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)
    if not store_ids:
        return {"synced_rows": 0}

    metrics = list(GMVMAX_BASE_METRICS)
    granularity_normalized = str(granularity or "").strip().upper()
    synced_rows = 0

    for store_id in store_ids:
        request = build_gmv_max_report_request(
            dataset=GMVMaxDataset.OVERVIEW,
            advertiser_id=str(advertiser_id),
            store_ids=[str(store_id)],
            start_date=start_date_str,
            end_date=end_date_str,
            metrics=metrics,
            page_size=_REPORT_PAGE_SIZE,
        )

        if granularity_normalized == "HOUR":
            request.dimensions = ["advertiser_id", "stat_time_hour"]
        else:
            request.dimensions = ["advertiser_id", "stat_time_day"]

        response = await ttb_client.gmv_max_report_get(request)
        data = getattr(response, "data", None)
        rows_raw = getattr(data, "list", None) or []
        rows = [_merge_report_entry(item) for item in rows_raw]
        rows = [row for row in rows if isinstance(row, dict)]

        for row in rows:
            try:
                if granularity_normalized == "HOUR":
                    _upsert_overview_hourly(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=advertiser_id,
                        store_id=str(store_id),
                        row=row,
                    )
                else:
                    _upsert_overview_daily(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=advertiser_id,
                        store_id=str(store_id),
                        row=row,
                    )
                synced_rows += 1
            except ValueError:
                logger.debug(
                    "skip overview metrics row without timestamp",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "store_id": store_id,
                        "granularity": granularity_normalized,
                    },
                )
                continue

    db.flush()
    return {"synced_rows": synced_rows}


async def sync_gmvmax_livestream_metrics_hourly(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
    campaign_ids: Sequence[str] | None = None,
    room_ids: Sequence[str] | None = None,
) -> dict:
    _log_sync_target("LIVESTREAM", granularity="HOURLY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    resolved_rooms = list(room_ids) if room_ids else _resolve_room_ids_for_campaign(
        db, campaign_id=str(campaign.campaign_id)
    )
    if not resolved_rooms:
        logger.warning(
            "gmvmax livestream metrics sync skipped: no room ids",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    request = build_gmv_max_report_request(
        dataset=GMVMaxDataset.LIVE_LIVESTREAM,
        advertiser_id=str(advertiser_id),
        store_ids=[store_id],
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.ROOM]["metrics"]),
        campaign_ids=list(campaign_ids) if campaign_ids else [str(campaign.campaign_id)],
        room_ids=resolved_rooms,
        page_size=_REPORT_PAGE_SIZE,
    )
    request.dimensions = ["room_id", "stat_time_hour"]

    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, Mapping)]

    synced_rows = 0
    for row in rows:
        room_id = _normalize_identifier(_extract_field(row, "room_id")) or (resolved_rooms[0] if resolved_rooms else None)
        if not room_id:
            continue
        try:
            _upsert_livestream_metrics_hourly(
                db, campaign_id=str(campaign.campaign_id), room_id=room_id, row=row
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip livestream hourly metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {"synced_rows": synced_rows}


async def sync_gmvmax_livestream_metrics_daily(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
    campaign_ids: Sequence[str] | None = None,
    room_ids: Sequence[str] | None = None,
) -> dict:
    _log_sync_target("LIVESTREAM", granularity="DAILY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    resolved_rooms = list(room_ids) if room_ids else _resolve_room_ids_for_campaign(
        db, campaign_id=str(campaign.campaign_id)
    )
    if not resolved_rooms:
        logger.warning(
            "gmvmax livestream daily sync skipped: no room ids",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    request = build_gmv_max_report_request(
        dataset=GMVMaxDataset.LIVE_LIVESTREAM,
        advertiser_id=str(advertiser_id),
        store_ids=[store_id],
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.ROOM]["metrics"]),
        campaign_ids=list(campaign_ids) if campaign_ids else [str(campaign.campaign_id)],
        room_ids=resolved_rooms,
        page_size=_REPORT_PAGE_SIZE,
    )
    request.dimensions = ["room_id", "stat_time_day"]

    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, Mapping)]

    synced_rows = 0
    for row in rows:
        room_id = _normalize_identifier(_extract_field(row, "room_id")) or (resolved_rooms[0] if resolved_rooms else None)
        if not room_id:
            continue
        try:
            _upsert_livestream_metrics_daily(
                db, campaign_id=str(campaign.campaign_id), room_id=room_id, row=row
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip livestream daily metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {"synced_rows": synced_rows}


async def sync_gmvmax_duration_metrics_hourly(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
    campaign_ids: Sequence[str] | None = None,
    room_ids: Sequence[str] | None = None,
) -> dict:
    _log_sync_target("DURATION", granularity="HOURLY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    resolved_rooms = list(room_ids) if room_ids else _resolve_room_ids_for_campaign(
        db, campaign_id=str(campaign.campaign_id)
    )
    if not resolved_rooms:
        logger.warning(
            "gmvmax duration hourly sync skipped: no room ids",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    request = build_gmv_max_report_request(
        dataset=GMVMaxDataset.LIVE_DURATION,
        advertiser_id=str(advertiser_id),
        store_ids=[store_id],
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.SESSION]["metrics"]),
        campaign_ids=list(campaign_ids) if campaign_ids else [str(campaign.campaign_id)],
        room_ids=resolved_rooms,
        page_size=_REPORT_PAGE_SIZE,
    )
    request.dimensions = ["duration", "stat_time_hour"]

    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, Mapping)]

    synced_rows = 0
    for row in rows:
        try:
            _upsert_duration_metrics_hourly(
                db, campaign_id=str(campaign.campaign_id), row=row
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip duration hourly metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {"synced_rows": synced_rows}


async def sync_gmvmax_duration_metrics_daily(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
    campaign_ids: Sequence[str] | None = None,
    room_ids: Sequence[str] | None = None,
) -> dict:
    _log_sync_target("DURATION", granularity="DAILY")
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"synced_rows": 0}

    resolved_rooms = list(room_ids) if room_ids else _resolve_room_ids_for_campaign(
        db, campaign_id=str(campaign.campaign_id)
    )
    if not resolved_rooms:
        logger.warning(
            "gmvmax duration daily sync skipped: no room ids",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    request = build_gmv_max_report_request(
        dataset=GMVMaxDataset.LIVE_DURATION,
        advertiser_id=str(advertiser_id),
        store_ids=[store_id],
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.SESSION]["metrics"]),
        campaign_ids=list(campaign_ids) if campaign_ids else [str(campaign.campaign_id)],
        room_ids=resolved_rooms,
        page_size=_REPORT_PAGE_SIZE,
    )
    request.dimensions = ["duration", "stat_time_day"]

    response = await ttb_client.gmv_max_report_get(request)
    data = getattr(response, "data", None)
    rows_raw = getattr(data, "list", None) or []
    rows = [_merge_report_entry(item) for item in rows_raw]
    rows = [row for row in rows if isinstance(row, Mapping)]

    synced_rows = 0
    for row in rows:
        try:
            _upsert_duration_metrics_daily(db, campaign_id=str(campaign.campaign_id), row=row)
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip duration daily metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {"synced_rows": synced_rows}


async def _sync_campaign_level_daily(
    db: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: GmvCampaign,
    store_id: str,
    start_date: str,
    end_date: str,
) -> int:
    synced = 0
    request = _build_campaign_report_request(
        advertiser_id=str(campaign.advertiser_id or ""),
        campaign_ids=[str(campaign.campaign_id)],
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        granularity="DAILY",
        metrics=_DEFAULT_REPORT_METRICS,
        dimensions=["campaign_id", "stat_time_day"],
        page=1,
        page_size=_REPORT_PAGE_SIZE,
    )
    response = await client.gmv_max_report_get(request)
    entries = getattr(getattr(response, "data", None), "list", []) or []
    for entry in entries:
        row = _merge_report_entry(entry)
        try:
            upsert_metrics_daily_row(db, campaign=campaign, row=row)
            synced += 1
        except ValueError:
            logger.debug(
                "skip daily metrics row without date",
                extra={
                    "campaign_id": campaign.campaign_id,
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                },
            )
    db.flush()
    return synced


async def _sync_creative_level_daily(
    db: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: GmvCampaign,
    store_id: str,
    start_date: str,
    end_date: str,
    include_attributes: bool,
    item_group_ids: Sequence[str] | None = None,
) -> int:
    dimensions = ["campaign_id", "item_group_id", "item_id"]
    metrics = list(GMVMAX_CREATIVE_METRICS)
    page = 1
    rows = 0
    while True:
        filtering = GMVMaxReportFiltering(
            store_ids=[store_id] if store_id else None,
            campaign_ids=[str(campaign.campaign_id)],
            item_group_ids=list(item_group_ids) if item_group_ids else None,
            gmv_max_promotion_types=["PRODUCT"],
        )
        request = GMVMaxCreativeReportRequest(
            advertiser_id=str(campaign.advertiser_id or ""),
            campaign_ids=[str(campaign.campaign_id)],
            metrics=metrics,
            dimensions=dimensions,
            time_range=GMVMaxReportTimeRange(start_time=start_date, end_time=end_date),
            time_granularity="DAILY",
            time_dimension="DAILY",
            filtering=filtering,
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        response = await client.gmv_max_creative_report(request)
        entries = getattr(getattr(response, "data", None), "list", []) or []
        if not entries:
            break
        for entry in entries:
            payload = _merge_report_entry(entry)
            creative_id = _normalize_identifier(
                payload.get("item_id") or payload.get("creative_id")
            )
            stat_time = payload.get("stat_time_day") or payload.get("date") or start_date
            stat_time_day = _parse_date(stat_time)
            if not creative_id or stat_time_day is None:
                continue
            try:
                _upsert_creative_metrics(
                    db,
                    campaign_id=str(campaign.campaign_id),
                    creative_id=str(creative_id),
                    stat_time_day=stat_time_day,
                    item_group_id=payload.get("item_group_id"),
                    metrics_row=payload,
                )
                rows += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to upsert creative metrics",
                    extra={
                        "campaign_id": campaign.campaign_id,
                        "creative_id": creative_id,
                        "stat_time": stat_time,
                        "include_attributes": include_attributes,
                    },
                )
        page_info = getattr(getattr(response, "data", None), "page_info", None)
        has_more = bool(
            getattr(page_info, "has_more", False) or getattr(page_info, "has_next", False)
        )
        total_page = getattr(page_info, "total_page", None) if page_info else None
        if has_more:
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
    return rows


async def sync_gmvmax_reports_for_campaign(
    db: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict[str, int]:
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    store_id = _resolve_store_id_for_metrics(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign=campaign,
    )
    if not store_id:
        return {"campaign_rows": 0, "creative_rows": 0}

    campaign_rows = await _sync_campaign_level_daily(
        db,
        client,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
        store_id=store_id,
        start_date=start_date_str,
        end_date=end_date_str,
    )

    creative_rows = 0
    if _normalize_identifier(campaign.shopping_ads_type) != "LIVE":
        item_group_ids = get_item_group_ids_for_campaign(db, campaign=campaign)
        if item_group_ids:
            creative_rows += await _sync_creative_level_daily(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                campaign=campaign,
                store_id=store_id,
                start_date=start_date_str,
                end_date=end_date_str,
                include_attributes=False,
                item_group_ids=item_group_ids,
            )
            for item_group_id in item_group_ids:
                creative_rows += await _sync_creative_level_daily(
                    db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    campaign=campaign,
                    store_id=store_id,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    include_attributes=True,
                    item_group_ids=[item_group_id],
                )

    return {"campaign_rows": campaign_rows, "creative_rows": creative_rows}


def log_campaign_action(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: GmvCampaign,
    action: str,
    reason: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    performed_by: str = "system",
    result: str = "SUCCESS",
    error_message: str | None = None,
    audit_hook: Callable[..., Any] | None = None,
) -> GmvActionLog:
    log_row = GmvActionLog(
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
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign: GmvCampaign,
    action: str,
    payload: dict | None = None,
    reason: str | None = None,
    performed_by: str = "system",
    audit_hook: Callable[..., Any] | None = None,
) -> GmvActionLog:
    _ensure_campaign_not_deleted(campaign)

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
    campaign: GmvCampaign,
) -> GmvStrategyConfig:
    stmt = (
        select(GmvStrategyConfig)
        .where(GmvStrategyConfig.workspace_id == workspace_id)
        .where(GmvStrategyConfig.auth_id == auth_id)
        .where(GmvStrategyConfig.campaign_id == campaign.campaign_id)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvStrategyConfig(
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
    campaign: GmvCampaign,
    hours_window: int = 6,
    days_window: int = 1,
) -> dict[str, Any]:
    now = datetime.utcnow()

    promotion_type = _normalize_promotion_type(campaign.shopping_ads_type)

    rows_day: list[GmvCampaignMetricsDaily] = []
    if days_window > 0:
        day_from = now.date() - timedelta(days=days_window)
        stmt_day = (
            select(GmvCampaignMetricsDaily)
            .where(GmvCampaignMetricsDaily.campaign_id == str(campaign.campaign_id))
            .where(GmvCampaignMetricsDaily.promotion_type == promotion_type)
            .where(GmvCampaignMetricsDaily.stat_time_day >= day_from)
            .where(GmvCampaignMetricsDaily.stat_time_day <= now.date())
        )
        rows_day = db.execute(stmt_day).scalars().all()

    rows_hour: list[GmvCampaignMetricsHourly] = []
    if hours_window > 0:
        ts_from = now - timedelta(hours=hours_window)
        stmt_hour = (
            select(GmvCampaignMetricsHourly)
            .where(GmvCampaignMetricsHourly.campaign_id == str(campaign.campaign_id))
            .where(GmvCampaignMetricsHourly.promotion_type == promotion_type)
            .where(GmvCampaignMetricsHourly.stat_time_hour >= ts_from)
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
    campaign: GmvCampaign,
    strategy: GmvStrategyConfig,
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
