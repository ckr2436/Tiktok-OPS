"""GMV Max service layer: syncs campaigns, metrics, and local state."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Collection, Mapping, Optional, Sequence, TypedDict

from sqlalchemy import select, func, delete, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxCampaignProduct,
    TTBGmvMaxCampaignSyncSnapshot,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import upsert_creative_metrics
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignCreateRequest,
    GMVMaxCampaignReportRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxCreativeReportRequest,
    GMVMaxExclusiveAuthorizationCreateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxIdentityInfo,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxReportTimeRange,
    GMVMaxStoreListRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.gmvmax_spec import (
    GMVMAX_DEFAULT_METRICS,
    GMVMAX_CREATIVE_METRICS,
    GMVMaxReportLevel,
    GMV_REPORT_CONFIG,
)
from app.services.ttb_api import TTBApiError


logger = logging.getLogger("gmv.tenants.gmvmax")

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
]


_DECIMAL_FOUR = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")
_DEFAULT_REPORT_METRICS = list(GMVMAX_DEFAULT_METRICS)
_CREATIVE_ATTRIBUTE_FIELDS = (
    "creative_name",
    "creative_status",
    "creative_delivery_status",
    "adgroup_id",
    "product_id",
    "item_id",
)

_REPORT_PAGE_SIZE = 200


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    campaign: TTBGmvMaxCampaign,
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


def _pick_dataset_for_level(
    *, campaign: TTBGmvMaxCampaign, level: GMVMaxReportLevel
) -> GMVMaxDataset:
    promotion_type = (_normalize_identifier(campaign.shopping_ads_type) or "PRODUCT").upper()
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
    campaign: TTBGmvMaxCampaign,
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
        if level == GMVMaxReportLevel.CREATIVE:
            request.metrics = list(
                dict.fromkeys(list(request.metrics) + list(_CREATIVE_ATTRIBUTE_FIELDS))
            )
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
) -> TTBGmvMaxCampaign:
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
) -> TTBGmvMaxCampaign:
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


def _mark_missing_snapshot_campaigns_as_deleted(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_scope: Collection[str] | None,
    synced_at: datetime,
) -> int:
    """Soft-delete campaigns missing from the latest sync snapshot."""

    snapshot_stmt = (
        select(TTBGmvMaxCampaignSyncSnapshot.campaign_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.auth_id == auth_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.advertiser_id == advertiser_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.synced_at == synced_at)
    )
    if store_scope:
        snapshot_stmt = snapshot_stmt.where(
            TTBGmvMaxCampaignSyncSnapshot.store_id.in_(store_scope)
        )
    snapshot_ids = {
        str(value)
        for value in db.execute(snapshot_stmt.distinct()).scalars()
        if value is not None
    }

    campaign_stmt = (
        select(TTBGmvMaxCampaign.campaign_id)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser_id)
    )
    if store_scope:
        campaign_stmt = campaign_stmt.where(
            TTBGmvMaxCampaign.store_id.in_(store_scope)
        )
    campaign_ids = {
        str(value)
        for value in db.execute(campaign_stmt).scalars()
        if value is not None
    }
    missing_snapshot_ids = campaign_ids - snapshot_ids
    if not missing_snapshot_ids:
        return 0

    delete_stmt = (
        update(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser_id)
    )
    if store_scope:
        delete_stmt = delete_stmt.where(
            TTBGmvMaxCampaign.store_id.in_(store_scope)
        )
    delete_stmt = delete_stmt.where(
        TTBGmvMaxCampaign.campaign_id.in_(missing_snapshot_ids)
    ).values(
        status="DELETE",
        operation_status="DELETE",
        secondary_status="CAMPAIGN_STATUS_DELETE",
        is_deleted=True,
        deleted_at=datetime.now(timezone.utc),
    )
    result = db.execute(delete_stmt)

    product_cleanup = (
        update(TTBGmvMaxCampaignProduct)
        .where(TTBGmvMaxCampaignProduct.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaignProduct.campaign_id.in_(missing_snapshot_ids))
        .values(operation_status="DELETE")
    )
    if store_scope:
        product_cleanup = product_cleanup.where(
            TTBGmvMaxCampaignProduct.store_id.in_(store_scope)
        )
    db.execute(product_cleanup)

    return result.rowcount or 0


def _bulk_upsert_snapshots(
    db: Session, rows: Sequence[Mapping[str, Any]]
) -> None:
    if not rows:
        return
    bind = db.get_bind()
    rows_to_insert = list(rows)
    if bind and bind.dialect.name == "sqlite":
        next_id = db.execute(
            select(func.coalesce(func.max(TTBGmvMaxCampaignSyncSnapshot.id), 0))
        ).scalar_one()
        rows_with_ids: list[Mapping[str, Any]] = []
        for row in rows_to_insert:
            if row.get("id"):
                rows_with_ids.append(row)
                continue
            next_id = int(next_id or 0) + 1
            rows_with_ids.append({**row, "id": next_id})
        stmt = sqlite_insert(TTBGmvMaxCampaignSyncSnapshot).values(rows_with_ids)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                TTBGmvMaxCampaignSyncSnapshot.workspace_id,
                TTBGmvMaxCampaignSyncSnapshot.advertiser_id,
                TTBGmvMaxCampaignSyncSnapshot.store_id,
                TTBGmvMaxCampaignSyncSnapshot.campaign_id,
            ],
            set_={
                "auth_id": stmt.excluded.auth_id,
                "synced_at": stmt.excluded.synced_at,
                "raw_json": stmt.excluded.raw_json,
            },
        )
    else:
        stmt = mysql_insert(TTBGmvMaxCampaignSyncSnapshot).values(rows_to_insert)
        stmt = stmt.on_duplicate_key_update(
            auth_id=stmt.inserted.auth_id,
            synced_at=stmt.inserted.synced_at,
            raw_json=stmt.inserted.raw_json,
        )
    db.execute(stmt)


def _prune_outdated_snapshots(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    synced_at: datetime,
    store_scope: Collection[str] | None,
    campaign_ids: Collection[str] | None,
) -> None:
    delete_stmt = (
        delete(TTBGmvMaxCampaignSyncSnapshot)
        .where(TTBGmvMaxCampaignSyncSnapshot.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.auth_id == auth_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.advertiser_id == advertiser_id)
        .where(TTBGmvMaxCampaignSyncSnapshot.synced_at < synced_at)
    )
    if store_scope:
        delete_stmt = delete_stmt.where(
            TTBGmvMaxCampaignSyncSnapshot.store_id.in_(store_scope)
        )
    if campaign_ids:
        delete_stmt = delete_stmt.where(
            TTBGmvMaxCampaignSyncSnapshot.campaign_id.in_(campaign_ids)
        )
    db.execute(delete_stmt)


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


def _migrate_metric_rows(
    db: Session,
    *,
    model: type[TTBGmvMaxMetricsDaily] | type[TTBGmvMaxMetricsHourly],
    key_attr: str,
    source_id: int,
    target_id: int,
) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    key_column = getattr(model, key_attr)
    existing_keys = set(
        db.execute(
            select(key_column).where(model.campaign_id == int(target_id))
        ).scalars()
    )
    source_rows = (
        db.execute(select(model).where(model.campaign_id == int(source_id)))
        .scalars()
        .all()
    )
    for row in source_rows:
        key_value = getattr(row, key_attr)
        if key_value in existing_keys:
            db.delete(row)
            continue
        row.campaign_id = int(target_id)
        existing_keys.add(key_value)


def _migrate_campaign_products(db: Session, *, source_id: int, target_id: int) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    stmt = (
        update(TTBGmvMaxCampaignProduct)
        .where(TTBGmvMaxCampaignProduct.campaign_pk == int(source_id))
        .values(campaign_pk=int(target_id))
    )
    db.execute(stmt)


def _migrate_action_logs(db: Session, *, source_id: int, target_id: int) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    stmt = (
        update(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == int(source_id))
        .values(campaign_id=int(target_id))
    )
    db.execute(stmt)


def _merge_duplicate_campaign_rows(
    db: Session,
    *,
    campaign_rows: Sequence[TTBGmvMaxCampaign],
) -> TTBGmvMaxCampaign:
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
        _migrate_metric_rows(
            db,
            model=TTBGmvMaxMetricsDaily,
            key_attr="date",
            source_id=int(duplicate.id),
            target_id=int(primary.id),
        )
        _migrate_metric_rows(
            db,
            model=TTBGmvMaxMetricsHourly,
            key_attr="interval_start",
            source_id=int(duplicate.id),
            target_id=int(primary.id),
        )
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


def _list_campaign_product_ids(
    db: Session, *, campaign: TTBGmvMaxCampaign
) -> list[str]:
    if not getattr(campaign, "id", None):
        return []

    rows = (
        db.execute(
            select(TTBGmvMaxCampaignProduct.item_group_id)
            .where(TTBGmvMaxCampaignProduct.campaign_pk == int(campaign.id))
            .where(TTBGmvMaxCampaignProduct.store_id == str(campaign.store_id))
        )
        .scalars()
        .all()
    )
    return [item for item in (_normalize_identifier(row) for row in rows) if item]


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


def _normalize_creative_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(row)
    for key in ("cost", "net_cost", "gross_revenue", "cost_per_order"):
        if key in metrics:
            metrics[key] = _to_decimal(metrics[key], quantize=_DECIMAL_FOUR)
    for key in (
        "orders",
        "impressions",
        "clicks",
        "product_impressions",
        "product_clicks",
    ):
        if key in metrics:
            metrics[key] = _to_int(metrics[key])
    for key in (
        "roi",
        "ad_click_rate",
        "ad_conversion_rate",
        "product_click_rate",
        "ad_video_view_rate_2s",
        "ad_video_view_rate_6s",
        "ad_video_view_rate_p25",
        "ad_video_view_rate_p50",
        "ad_video_view_rate_p75",
        "ad_video_view_rate_p100",
    ):
        if key in metrics:
            metrics[key] = _to_decimal(metrics[key], quantize=_DECIMAL_FOUR)
    return metrics


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
    filtered_run = bool(provided_filters)

    normalized_advertiser = str(advertiser_id)
    filter_keys = set(provided_filters)
    normalized_store_scope: list[str] = []
    for item in provided_filters.get("store_ids", []):
        normalized = _normalize_identifier(item)
        if normalized:
            normalized_store_scope.append(normalized)

    sync_started_at = datetime.now(timezone.utc)
    synced = 0
    details_cache: dict[str, Mapping[str, Any] | None] = {}
    campaign_ids_seen: set[str] = set()
    snapshot_rows: list[dict[str, Any]] = []
    async for payload, page_context in ttb_client.iter_gmvmax_campaigns(
        advertiser_id, **provided_filters
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
        if campaign_identifier:
            campaign_ids_seen.add(campaign_identifier)
            snapshot_rows.append(
                {
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": normalized_advertiser,
                    "store_id": str(resolved_store_id or ""),
                    "campaign_id": campaign_identifier,
                    "synced_at": sync_started_at,
                    "raw_json": payload,
                }
            )
        synced += 1
    db.flush()

    if normalized_store_scope:
        scoped_campaign_ids = {
            str(value)
            for value in db.execute(
                select(TTBGmvMaxCampaign.campaign_id)
                .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
                .where(TTBGmvMaxCampaign.auth_id == auth_id)
                .where(TTBGmvMaxCampaign.advertiser_id == normalized_advertiser)
                .where(TTBGmvMaxCampaign.store_id.in_(normalized_store_scope))
            ).scalars()
            if value is not None
        }
        campaign_ids_seen.update(scoped_campaign_ids)

    _bulk_upsert_snapshots(db, snapshot_rows)

    removed = 0
    removal_filter_keys = {"store_ids", "gmv_max_promotion_types"}
    allow_scoped_removal = bool(normalized_store_scope) and filter_keys.issubset(
        removal_filter_keys
    )
    if "gmv_max_promotion_types" in filter_keys:
        allow_scoped_removal = False
    if not filtered_run or allow_scoped_removal:
        _prune_outdated_snapshots(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser,
            synced_at=sync_started_at,
            store_scope=normalized_store_scope or None,
            campaign_ids=campaign_ids_seen,
        )
        removed = _mark_missing_snapshot_campaigns_as_deleted(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser,
            store_scope=normalized_store_scope or None,
            synced_at=sync_started_at,
        )

    return {"synced": synced, "removed": removed}


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

    normalized_advertiser = str(advertiser_id)
    by_advertiser_stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.advertiser_id == normalized_advertiser)
        .where(TTBGmvMaxCampaign.campaign_id == campaign_id)
        .order_by(TTBGmvMaxCampaign.id.asc())
    )
    rows = db.execute(by_advertiser_stmt).scalars().all()
    result: TTBGmvMaxCampaign | None = None
    if rows:
        result = _merge_duplicate_campaign_rows(db, campaign_rows=rows)

    if result is None:
        legacy_stmt = (
            select(TTBGmvMaxCampaign)
            .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
            .where(TTBGmvMaxCampaign.auth_id == auth_id)
            .where(TTBGmvMaxCampaign.campaign_id == campaign_id)
        )
        result = db.execute(legacy_stmt).scalars().first()

    if result is None:
        result = TTBGmvMaxCampaign(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser,
            campaign_id=campaign_id,
        )
        db.add(result)
        _assign_sqlite_pk(db, result)

    result.auth_id = auth_id
    result.advertiser_id = normalized_advertiser
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
    result.is_deleted = False
    result.deleted_at = None
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
    ttb_client: TikTokBusinessGMVMaxClient,
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
    campaign: TTBGmvMaxCampaign,
    start_date: date | str,
    end_date: date | str,
) -> dict:
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


async def _sync_campaign_level_daily(
    db: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    campaign: TTBGmvMaxCampaign,
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
    campaign: TTBGmvMaxCampaign,
    store_id: str,
    start_date: str,
    end_date: str,
    include_attributes: bool,
    item_group_ids: Sequence[str] | None = None,
) -> int:
    dimensions = ["campaign_id", "item_group_id", "item_id"]
    metrics = list(GMVMAX_CREATIVE_METRICS)
    if include_attributes:
        metrics = list(dict.fromkeys(metrics + list(_CREATIVE_ATTRIBUTE_FIELDS)))
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
            if not creative_id or not stat_time:
                continue
            metrics_payload = _normalize_creative_metrics(payload)
            if "item_group_id" in payload:
                metrics_payload.setdefault("product_id", payload.get("item_group_id"))
            if "item_id" in payload:
                metrics_payload.setdefault("item_id", payload.get("item_id"))
            if "creative_delivery_status" in payload:
                metrics_payload.setdefault(
                    "creative_delivery_status", payload.get("creative_delivery_status")
                )
            try:
                await upsert_creative_metrics(
                    db,
                    workspace_id=workspace_id,
                    provider="tiktok-business",
                    auth_id=auth_id,
                    campaign_id=str(campaign.campaign_id),
                    creative_id=str(creative_id),
                    stat_time_day=_parse_date(stat_time),
                    metrics=metrics_payload,
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
    campaign: TTBGmvMaxCampaign,
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
        item_group_ids = _list_campaign_product_ids(db, campaign=campaign)
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
    ttb_client: TikTokBusinessGMVMaxClient,
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
