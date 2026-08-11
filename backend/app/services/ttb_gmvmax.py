"""GMV Max service layer: syncs campaigns, metrics, and local state."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import time_ns
from typing import Any, Callable, Collection, Mapping, Optional, Sequence, TypedDict

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignCreative,
    GmvCampaignLivestream,
    GmvDurationMetricsDaily,
    GmvDurationMetricsHourly,
    GmvLivestreamMetricsDaily,
    GmvLivestreamMetricsHourly,
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvOverviewSnapshot,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
    PromotionTypeEnum,
)
from app.data.models.ttb_entities import TTBAdvertiser, TTBAdvertiserStoreLink
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
    GMVMaxDataset,
    GMVMaxExclusiveAuthorizationCreateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxIdentityInfo,
    GMVMaxMetricsLevel,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxStoreListRequest,
    build_gmv_max_report_request,
    TikTokBusinessGMVMaxClient,
)
from app.gmvmax.services.campaign_mapper import map_gmvmax_campaign_info_to_model
from app.gmvmax.services.campaign_catalog_freshness import (
    catalog_observation_now,
    catalog_response_is_stale,
    normalize_catalog_observed_at,
)
from app.gmvmax.services.fact_freshness import (
    settlement_metadata,
    utc_now_naive,
)
from app.gmvmax.services.fact_reconciliation import StagedFactKeySet
from app.gmvmax.services.manual_pause_reconciliation import (
    reconcile_manual_pause_from_official_catalog,
)
from app.gmvmax.services.report_pagination import (
    OFFICIAL_REPORT_PAGE_SIZE,
    ReportPaginationState,
    chunk_report_filter_ids,
    iter_numbered_pages,
    report_page_has_more,
)
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
    "resolve_store_id_from_page_context",
    "ensure_gmvmax_store_authorized",
    "build_gmvmax_anchor_params",
    "create_gmvmax_campaign",
    "update_gmvmax_campaign",
    "fetch_gmvmax_report_by_level",
    "fetch_overview_summary_rows",
    "fetch_and_cache_campaign_detail",
    "normalize_overview_metrics",
    "upsert_overview_snapshot",
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
_PRODUCT_REPORT_METRICS = list(
    dict.fromkeys(GMV_REPORT_CONFIG[GMVMaxReportLevel.PRODUCT]["metrics"])
)
_OVERVIEW_FINANCIAL_METRICS = (
    "cost",
    "net_cost",
    "orders",
    "gross_revenue",
    "cost_per_order",
    "roi",
)


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

_REPORT_PAGE_SIZE = OFFICIAL_REPORT_PAGE_SIZE


def _official_report_date_windows(
    start_date: date | str,
    end_date: date | str,
    *,
    max_days: int,
) -> list[tuple[date, date]]:
    """Split report/get dates at the API's inclusive date-window boundary."""

    if max_days < 1:
        raise ValueError("max_days must be positive")
    normalized_start = date.fromisoformat(_normalize_date(start_date))
    normalized_end = date.fromisoformat(_normalize_date(end_date))
    if normalized_start > normalized_end:
        raise ValueError("start_date must not be after end_date")

    windows: list[tuple[date, date]] = []
    current = normalized_start
    while current <= normalized_end:
        window_end = min(
            normalized_end,
            current + timedelta(days=max_days - 1),
        )
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _advertiser_timezone_for_fact(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    row = db.execute(
        select(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .where(TTBAdvertiser.workspace_id == int(workspace_id))
        .where(TTBAdvertiser.auth_id == int(auth_id))
        .where(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return str(row.display_timezone or row.timezone or "").strip() or None


def _apply_fact_freshness(
    instance: Any,
    *,
    stat_day: date,
    source_observed_at: datetime,
    ingested_at: datetime,
    advertiser_timezone: str | None,
) -> None:
    incoming_final, incoming_settled_at = settlement_metadata(
        stat_day,
        source_observed_at=source_observed_at,
        advertiser_timezone=advertiser_timezone,
    )
    instance.source_observed_at = source_observed_at
    instance.ingested_at = ingested_at
    instance.is_final = bool(
        getattr(instance, "is_final", False) or incoming_final
    )
    if incoming_settled_at is not None:
        instance.settled_at = incoming_settled_at


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
    """Fetch every GMV Max report page for the requested level."""

    clean_campaign_ids = _sanitize_id_list(campaign_ids) or _sanitize_id_list(
        [campaign_id]
    )
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

    report_level = GMVMaxReportLevel(level_value.value)
    report_config = GMV_REPORT_CONFIG[report_level]
    metrics = list(dict.fromkeys(report_config["metrics"]))
    dimensions = list(report_config["dimensions"])
    # The official product-level report rejects item_group_ids and
    # gmv_max_promotion_types with 40002. Item groups are dimensions at that
    # level, not supported filters. Creative-level reports still require them.
    #
    # PRODUCT responses also do not include campaign_id as a dimension, so
    # each request must be scoped to exactly one campaign. For the other
    # levels, campaign/item filters are chunked at the official 100-ID limit.
    if level_value is GMVMaxMetricsLevel.PRODUCT:
        campaign_id_chunks = [[value] for value in clean_campaign_ids]
    else:
        campaign_id_chunks = chunk_report_filter_ids(clean_campaign_ids)
    item_group_id_chunks: list[list[str] | None] = (
        chunk_report_filter_ids(clean_item_group_ids)
        if level_value is GMVMaxMetricsLevel.CREATIVE
        else [None]
    )

    def _build_dimensions(
        payload: Mapping[str, Any],
        *,
        fallback_stat_time_day: str,
        fallback_campaign_id: str | None,
        window_start: date,
        window_end: date,
    ) -> dict[str, Any]:
        raw_stat_time_day = payload.get("stat_time_day")
        if level_value is GMVMaxMetricsLevel.CREATIVE:
            parsed_stat_time_day = _parse_date(raw_stat_time_day)
            if (
                parsed_stat_time_day is None
                or parsed_stat_time_day < window_start
                or parsed_stat_time_day > window_end
            ):
                raise TTBBusinessError(
                    "GMV Max creative report returned a row without a valid "
                    "stat_time_day inside the requested window",
                    code="GMVMAX_REPORT_DATE_INCOMPLETE",
                    payload={
                        "campaign_ids": list(campaign_id_chunk),
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                        "stat_time_day": raw_stat_time_day,
                    },
                )
            stat_time_day: Any = parsed_stat_time_day.isoformat()
        else:
            stat_time_day = (
                raw_stat_time_day
                or payload.get("date")
                or fallback_stat_time_day
            )
        base = {
            "campaign_id": payload.get("campaign_id") or fallback_campaign_id,
            "store_id": str(store_id),
            "stat_time_day": stat_time_day,
        }
        if level_value is GMVMaxMetricsLevel.PRODUCT:
            base["product_id"] = payload.get("item_group_id") or payload.get("product_id")
        if level_value is GMVMaxMetricsLevel.CREATIVE:
            base["product_id"] = payload.get("item_group_id") or payload.get("product_id")
            base["shop_content_id"] = payload.get("item_id") or payload.get("creative_id")
        return base

    mapped_entries: list[GMVMaxReportEntry] = []
    max_pages = 200
    for window_start, window_end in _official_report_date_windows(
        start_date_str,
        end_date_str,
        max_days=30,
    ):
        for campaign_id_chunk in campaign_id_chunks:
            for item_group_id_chunk in item_group_id_chunks:
                filtering = GMVMaxReportFiltering(
                    campaign_ids=campaign_id_chunk,
                    item_group_ids=item_group_id_chunk,
                )
                request = GMVMaxReportGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_ids=[str(store_id)],
                    start_date=window_start.isoformat(),
                    end_date=window_end.isoformat(),
                    metrics=metrics,
                    dimensions=dimensions,
                    gmv_max_promotion_types=(
                        ["PRODUCT"]
                        if level_value is GMVMaxMetricsLevel.CAMPAIGN
                        else None
                    ),
                    campaign_ids=campaign_id_chunk,
                    item_group_ids=item_group_id_chunk,
                    filtering=filtering,
                    enable_total_metrics=False,
                    page=1,
                    page_size=_REPORT_PAGE_SIZE,
                )
                page = 1
                pagination_state = ReportPaginationState(require_dimensions=True)
                while True:
                    if page > max_pages:
                        raise RuntimeError(
                            f"GMV Max {level_value.value} report pagination "
                            f"exceeded {max_pages} pages for "
                            f"{window_start}..{window_end}"
                        )
                    request.page = page
                    response = await client.gmv_max_report_get(
                        request,
                        inject_promotion_types=(
                            level_value is GMVMaxMetricsLevel.CAMPAIGN
                        ),
                    )
                    data = getattr(response, "data", None)
                    raw_entries = getattr(data, "list", None) or []
                    for entry in raw_entries:
                        payload = _merge_report_entry(entry)
                        response_campaign_id = payload.get("campaign_id")
                        if (
                            response_campaign_id is not None
                            and str(response_campaign_id)
                            not in campaign_id_chunk
                        ):
                            raise RuntimeError(
                                "GMV Max report response escaped its "
                                "campaign filter chunk"
                            )
                        response_item_group_id = (
                            payload.get("item_group_id")
                            or payload.get("product_id")
                        )
                        if (
                            item_group_id_chunk is not None
                            and response_item_group_id is not None
                            and str(response_item_group_id)
                            not in item_group_id_chunk
                        ):
                            raise RuntimeError(
                                "GMV Max report response escaped its item "
                                "filter chunk"
                            )
                        metrics_payload = {
                            metric: payload.get(metric)
                            for metric in GMVMAX_SUPPORTED_METRICS
                            if metric in payload
                        }
                        dimensions_payload = _build_dimensions(
                            payload,
                            fallback_stat_time_day=window_start.isoformat(),
                            fallback_campaign_id=(
                                campaign_id_chunk[0]
                                if len(campaign_id_chunk) == 1
                                else None
                            ),
                            window_start=window_start,
                            window_end=window_end,
                        )
                        mapped_entries.append(
                            GMVMaxReportEntry(
                                metrics=metrics_payload,
                                dimensions=dimensions_payload,
                            )
                        )

                    has_more = report_page_has_more(
                        data,
                        current_page=page,
                        rows=raw_entries,
                        state=pagination_state,
                    )
                    if not has_more:
                        break
                    page += 1

    return mapped_entries


async def fetch_gmvmax_current_creative_statuses(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    campaign_ids: Sequence[str],
    item_group_ids: Sequence[str],
    report_date: date | str,
) -> list[GMVMaxReportEntry]:
    """Fetch the current creative inventory, including zero-delivery queue rows.

    TikTok omits ``IN_QUEUE`` rows when ``stat_time_day`` is requested because
    creatives without delivery cannot be assigned to a reporting day. Current
    status inventory therefore requires a second report without a time
    dimension. The caller assigns the snapshot to ``report_date`` before
    merging it with dated performance rows.
    """

    clean_campaign_ids = _sanitize_id_list(campaign_ids)
    clean_item_group_ids = _sanitize_id_list(item_group_ids)
    if not clean_campaign_ids or not clean_item_group_ids:
        raise TTBBusinessError(
            "campaign_ids and item_group_ids are required for creative status inventory",
            code="GMVMAX_REPORT_CREATIVE_STATUS_SCOPE_REQUIRED",
            payload={
                "campaign_ids": campaign_ids,
                "item_group_ids": item_group_ids,
            },
        )

    report_date_str = _normalize_date(report_date)
    max_pages = 200
    mapped_entries: list[GMVMaxReportEntry] = []
    for campaign_id_chunk in chunk_report_filter_ids(clean_campaign_ids):
        for item_group_id_chunk in chunk_report_filter_ids(clean_item_group_ids):
            page = 1
            pagination_state = ReportPaginationState(require_dimensions=True)
            while True:
                if page > max_pages:
                    raise RuntimeError(
                        "GMV Max creative status pagination exceeded "
                        f"{max_pages} pages"
                    )

                filtering = GMVMaxReportFiltering(
                    campaign_ids=campaign_id_chunk,
                    item_group_ids=item_group_id_chunk,
                )
                request = GMVMaxReportGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_ids=[str(store_id)],
                    start_date=report_date_str,
                    end_date=report_date_str,
                    metrics=list(GMVMAX_CREATIVE_METRICS),
                    dimensions=["campaign_id", "item_group_id", "item_id"],
                    campaign_ids=campaign_id_chunk,
                    item_group_ids=item_group_id_chunk,
                    filtering=filtering,
                    enable_total_metrics=False,
                    page=page,
                    page_size=_REPORT_PAGE_SIZE,
                )
                response = await client.gmv_max_report_get(
                    request,
                    inject_promotion_types=False,
                )
                data = getattr(response, "data", None)
                raw_entries = getattr(data, "list", None) or []
                for entry in raw_entries:
                    payload = _merge_report_entry(entry)
                    creative_id = payload.get("item_id") or payload.get("creative_id")
                    campaign_id = payload.get("campaign_id")
                    item_group_id = payload.get("item_group_id") or payload.get("product_id")
                    if not campaign_id or not item_group_id or not creative_id:
                        raise RuntimeError(
                            "GMV Max creative status response contains an "
                            "incomplete row"
                        )
                    if (
                        str(campaign_id) not in campaign_id_chunk
                        or str(item_group_id) not in item_group_id_chunk
                    ):
                        raise RuntimeError(
                            "GMV Max creative status response escaped its "
                            "campaign/item filter chunk"
                        )
                    metrics_payload = {
                        metric: payload.get(metric)
                        for metric in GMVMAX_SUPPORTED_METRICS
                        if metric in payload
                    }
                    mapped_entries.append(
                        GMVMaxReportEntry(
                            metrics=metrics_payload,
                            dimensions={
                                "campaign_id": str(campaign_id),
                                "store_id": str(store_id),
                                "item_group_id": str(item_group_id),
                                "product_id": str(item_group_id),
                                "item_id": str(creative_id),
                                "shop_content_id": str(creative_id),
                                "stat_time_day": report_date_str,
                            },
                        )
                    )

                if report_page_has_more(
                    data,
                    current_page=page,
                    rows=raw_entries,
                    state=pagination_state,
                ):
                    page += 1
                    continue
                break

    return mapped_entries


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_store_identifier(value: Any) -> str | None:
    """Treat TikTok's zero store placeholder as an absent identifier."""

    normalized = _normalize_identifier(value)
    if normalized is None or set(normalized) == {"0"}:
        return None
    return normalized


def _campaign_pagination_key(item: Any) -> str | None:
    if isinstance(item, Mapping):
        return _normalize_identifier(item.get("campaign_id") or item.get("id"))
    return _normalize_identifier(
        getattr(item, "campaign_id", None) or getattr(item, "id", None)
    )


def _get_bound_store_id(db: Session, *, workspace_id: int, auth_id: int) -> str | None:
    """Return the configured store_id for the binding, if any."""

    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    return _normalize_store_identifier(binding.store_id) if binding else None


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
    execution_guard: Callable[[], None] | None = None,
) -> str:
    """Validate store availability and ensure exclusive authorization exists."""

    logger.info(
        "gmvmax.ensure_store_authorized.list",
        extra={"advertiser_id": advertiser_id, "store_id": target_store_id},
    )
    if execution_guard is not None:
        execution_guard()
    store_list_response = await client.gmv_max_store_list(
        GMVMaxStoreListRequest(advertiser_id=str(advertiser_id))
    )
    if execution_guard is not None:
        execution_guard()
    store_list = (
        getattr(store_list_response.data, "store_list", [])
        if store_list_response
        else []
    )
    matched_store = next(
        (
            store
            for store in store_list
            if _normalize_store_identifier(getattr(store, "store_id", None))
            == str(target_store_id)
        ),
        None,
    )

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
    if execution_guard is not None:
        execution_guard()
    get_response = await client.gmv_max_exclusive_authorization_get(get_request)
    if execution_guard is not None:
        execution_guard()
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
    if execution_guard is not None:
        execution_guard()
    create_response = await client.gmv_max_exclusive_authorization_create(create_request)
    if execution_guard is not None:
        execution_guard()
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
                normalized = _normalize_store_identifier(candidate)
                if normalized:
                    store_ids.append(normalized)
        else:
            normalized = _normalize_store_identifier(raw_store_ids)
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
        store_key = _normalize_store_identifier(
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

    store_id = _normalize_store_identifier(campaign.store_id)
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


def _resolve_product_report_item_group_ids(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign: Any,
) -> list[str]:
    campaign_id = _normalize_identifier(getattr(campaign, "campaign_id", None))
    payloads = (
        getattr(campaign, "raw_json", None),
        getattr(campaign, "detail_raw_json", None),
        getattr(campaign, "list_raw_json", None),
    )
    resolved: list[str] = []
    for payload in payloads:
        if isinstance(payload, Mapping):
            resolved.extend(_extract_item_group_ids_from_campaign_payload(payload))

    if not resolved and campaign_id:
        rows = db.execute(
            select(GmvmaxProductCampaignItemGroup.item_group_id)
            .where(GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignItemGroup.advertiser_id == str(advertiser_id))
            .where(GmvmaxProductCampaignItemGroup.store_id == str(store_id))
            .where(GmvmaxProductCampaignItemGroup.campaign_id == str(campaign_id))
        ).scalars()
        resolved.extend(str(item) for item in rows if item)

    normalized = [_normalize_identifier(value) or "" for value in resolved]
    return list(dict.fromkeys(item for item in normalized if item))


def _resolve_room_ids_for_campaign(db: Session, *, campaign_id: str) -> list[str]:
    stmt = select(GmvCampaignLivestream.room_id).where(
        GmvCampaignLivestream.campaign_id == str(campaign_id)
    )
    rows = [row[0] for row in db.execute(stmt).all() if row and row[0]]
    deduped = list(dict.fromkeys(_normalize_identifier(value) or "" for value in rows))
    return [value for value in deduped if value]


def _record_campaign_livestream(
    db: Session,
    *,
    campaign_id: str,
    room_id: str,
) -> bool:
    """Persist a room discovered from the official LIVE_LIVESTREAM report."""

    normalized_campaign_id = _normalize_identifier(campaign_id)
    normalized_room_id = _normalize_identifier(room_id)
    if not normalized_campaign_id or not normalized_room_id:
        return False
    existing = db.scalar(
        select(GmvCampaignLivestream.id)
        .where(GmvCampaignLivestream.campaign_id == normalized_campaign_id)
        .where(GmvCampaignLivestream.room_id == normalized_room_id)
        .where(GmvCampaignLivestream.promotion_type == PromotionTypeEnum.LIVE)
    )
    if existing is not None:
        return False
    try:
        with db.begin_nested():
            db.add(
                GmvCampaignLivestream(
                    campaign_id=normalized_campaign_id,
                    room_id=normalized_room_id,
                    promotion_type=PromotionTypeEnum.LIVE,
                )
            )
            db.flush()
    except IntegrityError:
        # LIVE and DURATION strategies can discover the same room concurrently.
        # Keep the outer metrics transaction usable after the unique-key race.
        logger.debug(
            "gmvmax livestream room already discovered concurrently",
            extra={
                "campaign_id": normalized_campaign_id,
                "room_id": normalized_room_id,
            },
        )
        return False
    return True


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
    execution_guard: Callable[[Session], None] | None = None,
) -> GmvCampaign:
    body_dump = body.model_dump(exclude_none=False)
    if not body_dump.get("request_id"):
        body = body.copy(update={"request_id": str(time_ns())})
        body_dump = body.model_dump(exclude_none=False)
    if body_dump.get("store_id") and not body_dump.get("store_authorized_bc_id"):
        if execution_guard is not None:
            execution_guard(db)
        authorized_bc_id = await ensure_gmvmax_store_authorized(
            client,
            advertiser_id=str(advertiser_id),
            target_store_id=str(body_dump["store_id"]),
            execution_guard=(
                (lambda: execution_guard(db))
                if execution_guard is not None
                else None
            ),
        )
        if execution_guard is not None:
            execution_guard(db)
        body = body.copy(update={"store_authorized_bc_id": authorized_bc_id})

    request = GMVMaxCampaignCreateRequest(advertiser_id=str(advertiser_id), body=body)
    if execution_guard is not None:
        execution_guard(db)
    response = await client.gmv_max_campaign_create(request)
    if execution_guard is not None:
        execution_guard(db)
    mutation_observed_at = catalog_observation_now()
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

    row = _upsert_campaign_catalog_from_api(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=campaign_payload,
        store_id_hint=str(body_dump.get("store_id")) if body_dump.get("store_id") is not None else None,
        trusted_store_id_hint=True,
        campaign_details=campaign_data,
        source_observed_at=mutation_observed_at,
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
    execution_guard: Callable[[Session], None] | None = None,
) -> GmvCampaign:
    request = GMVMaxCampaignUpdateRequest(advertiser_id=str(advertiser_id), body=body)
    if execution_guard is not None:
        execution_guard(db)
    response = await client.gmv_max_campaign_update(request)
    if execution_guard is not None:
        execution_guard(db)
    mutation_observed_at = catalog_observation_now()
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

    row = _upsert_campaign_catalog_from_api(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=campaign_payload,
        store_id_hint=str(body_dump.get("store_id")) if body_dump.get("store_id") is not None else None,
        campaign_details=campaign_data,
        source_observed_at=mutation_observed_at,
    )
    db.flush()
    return row


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
        store_value = _normalize_store_identifier(row.store_id)
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


def _authoritative_item_group_snapshot(
    campaign_details: Mapping[str, Any] | None,
    *,
    details_complete: bool,
) -> tuple[bool, list[str]]:
    """Classify the official campaign/info product binding collection.

    TikTok documents ``item_group_ids`` as the complete SPU list for PRODUCT
    campaigns whose ``product_specific_type`` is ``CUSTOMIZED_PRODUCTS``.  It
    is omitted by contract for ``ALL``, which authoritatively means there are
    no explicit item-group bindings.  A missing field for any other type, a
    null value, or a malformed member is an incomplete response and must not
    drive absence deletion.
    """

    if not details_complete or not isinstance(campaign_details, Mapping):
        return False, []

    product_specific_type = str(
        campaign_details.get("product_specific_type") or ""
    ).strip().upper()
    if "item_group_ids" not in campaign_details:
        if product_specific_type == "ALL":
            return True, []
        return False, []

    raw_ids = campaign_details.get("item_group_ids")
    if not isinstance(raw_ids, (list, tuple)):
        return False, []

    normalized_ids: list[str] = []
    for raw_id in raw_ids:
        normalized = _normalize_identifier(raw_id)
        if not normalized:
            return False, []
        if normalized not in normalized_ids:
            normalized_ids.append(normalized)
    return True, sorted(normalized_ids)


def _upsert_campaign_catalog_from_api(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: Mapping[str, Any],
    store_id_hint: str | None = None,
    trusted_store_id_hint: bool = False,
    campaign_details: Mapping[str, Any] | None = None,
    campaign_details_complete: bool = False,
    promotion_type: PromotionTypeEnum | None = None,
    source_observed_at: datetime,
) -> Any:
    """Persist one catalog response if it is not older than local authority.

    ``source_observed_at`` must be captured before the official read starts.
    The row is locked before comparing the durable observation fence so a
    response that arrives late cannot overwrite a newer sync or a successful
    Guard/manual mutation.  A stale response returns the existing row without
    touching catalog fields or item-group relations.
    """

    observed_at = normalize_catalog_observed_at(source_observed_at)
    campaign_identifier = _normalize_identifier(
        _extract_field_from_sources(("campaign_id", "id"), payload, campaign_details)
    )
    if not campaign_identifier:
        raise ValueError("campaign_id missing in payload")

    promotion_type_value = promotion_type or _normalize_promotion_type(
        _extract_field_from_sources(
            ("gmv_max_promotion_type", "promotion_type", "shopping_ads_type"),
            campaign_details,
            payload,
        )
    )
    is_live = promotion_type_value == PromotionTypeEnum.LIVE
    catalog_model = GmvmaxLiveCampaignCatalog if is_live else GmvmaxProductCampaignCatalog
    ingested_at = catalog_observation_now()

    row = (
        db.query(catalog_model)
        .filter(catalog_model.workspace_id == int(workspace_id))
        .filter(catalog_model.auth_id == int(auth_id))
        .filter(catalog_model.advertiser_id == str(advertiser_id))
        .filter(catalog_model.campaign_id == str(campaign_identifier))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is not None and catalog_response_is_stale(row, observed_at):
        setattr(row, "_gmvmax_catalog_response_applied", False)
        logger.info(
            "gmvmax campaign catalog ignored stale official response",
            extra={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "campaign_id": str(campaign_identifier),
                "source_observed_at": observed_at.isoformat(),
            },
        )
        return row

    if row is None:
        row = catalog_model(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_identifier),
        )
        db.add(row)

    raw_store = _extract_field_from_sources(
        ("store_id", "shop_id"),
        campaign_details,
        payload,
    )
    # TikTok may return numeric ``0`` for store_id immediately after campaign
    # creation. The request scope is already validated by our tenant binding,
    # so use that trusted hint instead of persisting the placeholder.
    normalized_raw_store = _normalize_store_identifier(raw_store)
    normalized_store_hint = _normalize_store_identifier(store_id_hint)
    if trusted_store_id_hint and normalized_store_hint:
        if normalized_raw_store and normalized_raw_store != normalized_store_hint:
            logger.warning(
                "gmvmax create response store differed from validated request scope",
                extra={
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "advertiser_id": str(advertiser_id),
                    "campaign_id": str(campaign_identifier),
                    "response_store_id": normalized_raw_store,
                    "validated_store_id": normalized_store_hint,
                },
            )
        store_id = normalized_store_hint
    else:
        store_id = (
            normalized_raw_store
            or normalized_store_hint
            or _normalize_store_identifier(getattr(row, "store_id", None))
        )

    row.campaign_name = _extract_field_from_sources(
        ("campaign_name", "name"),
        campaign_details,
        payload,
    )
    row.operation_status = _extract_field_from_sources(
        ("operation_status", "status", "campaign_status"),
        campaign_details,
        payload,
    )
    row.secondary_status = _extract_field_from_sources(
        ("secondary_status", "primary_status"),
        campaign_details,
        payload,
    )
    row.objective_type = _extract_field_from_sources(("objective_type",), campaign_details, payload)
    row.create_time_utc = _parse_datetime(
        _extract_field_from_sources(("create_time", "created_time"), campaign_details, payload)
    )
    row.modify_time_utc = _parse_datetime(
        _extract_field_from_sources(("modify_time", "update_time", "updated_time"), campaign_details, payload)
    )
    row.list_raw_json = dict(payload)
    row.store_id = store_id
    row.shopping_ads_type = "LIVE" if is_live else "PRODUCT"
    if not is_live:
        row.product_specific_type = _extract_field_from_sources(
            ("product_specific_type",),
            campaign_details,
            payload,
        )
    row.optimization_goal = _extract_field_from_sources(
        ("optimization_goal",),
        campaign_details,
        payload,
    )
    row.deep_bid_type = _extract_field_from_sources(
        ("deep_bid_type", "bid_type"),
        campaign_details,
        payload,
    )
    row.roas_bid = _to_decimal(
        _extract_field_from_sources(("roas_bid",), campaign_details, payload),
        quantize=_DECIMAL_FOUR,
    )
    row.budget_cents = _to_cents(
        _extract_field_from_sources(("budget", "daily_budget"), campaign_details, payload)
    )
    row.schedule_type = _extract_field_from_sources(("schedule_type",), campaign_details, payload)
    row.schedule_start_time_utc = _parse_datetime(
        _extract_field_from_sources(("schedule_start_time",), campaign_details, payload)
    )
    row.schedule_end_time_utc = _parse_datetime(
        _extract_field_from_sources(("schedule_end_time",), campaign_details, payload)
    )
    # A campaign/get list response is intentionally cheaper and less complete
    # than campaign/info.  Realtime catalog refreshes therefore often omit
    # ``campaign_details`` for rows whose store ownership is already known.
    # Treat omission as "not observed", never as an authoritative deletion of
    # the last complete detail payload.  Clearing this field loses the durable
    # item-group binding used by the campaign detail page and silently disables
    # creative metrics in the frontend.
    if isinstance(campaign_details, Mapping):
        row.detail_raw_json = dict(campaign_details)
    row.list_synced_at = observed_at
    if campaign_details:
        row.detail_synced_at = observed_at
    row.updated_at = ingested_at
    setattr(row, "_gmvmax_catalog_response_applied", True)
    db.add(row)

    if not is_live and store_id:
        # A placeholder store can create a second item-group relation before a
        # later campaign sync corrects the catalog row. Keep one canonical
        # campaign/store scope and remove those stale cross-store relations.
        db.execute(
            delete(GmvmaxProductCampaignItemGroup)
            .where(
                GmvmaxProductCampaignItemGroup.workspace_id
                == int(workspace_id)
            )
            .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
            .where(
                GmvmaxProductCampaignItemGroup.advertiser_id
                == str(advertiser_id)
            )
            .where(
                GmvmaxProductCampaignItemGroup.campaign_id
                == str(campaign_identifier)
            )
            .where(GmvmaxProductCampaignItemGroup.store_id != str(store_id))
        )
        item_group_snapshot_complete, authoritative_item_group_ids = (
            _authoritative_item_group_snapshot(
                campaign_details,
                details_complete=campaign_details_complete,
            )
        )
        if item_group_snapshot_complete:
            item_group_ids = authoritative_item_group_ids
            absent_stmt = (
                delete(GmvmaxProductCampaignItemGroup)
                .where(
                    GmvmaxProductCampaignItemGroup.workspace_id
                    == int(workspace_id)
                )
                .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
                .where(
                    GmvmaxProductCampaignItemGroup.advertiser_id
                    == str(advertiser_id)
                )
                .where(
                    GmvmaxProductCampaignItemGroup.store_id == str(store_id)
                )
                .where(
                    GmvmaxProductCampaignItemGroup.campaign_id
                    == str(campaign_identifier)
                )
            )
            if item_group_ids:
                absent_stmt = absent_stmt.where(
                    GmvmaxProductCampaignItemGroup.item_group_id.notin_(
                        item_group_ids
                    )
                )
            db.execute(absent_stmt)
        else:
            # Partial/list/mutation payloads may add observed bindings, but
            # they never prove that an older binding disappeared.
            item_group_ids = _extract_item_group_ids_from_payload(payload)
        if not item_group_snapshot_complete and isinstance(campaign_details, Mapping):
            item_group_ids = sorted(
                {
                    *item_group_ids,
                    *_extract_item_group_ids_from_payload(campaign_details),
                }
            )
        for item_group_id in item_group_ids:
            relation = (
                db.query(GmvmaxProductCampaignItemGroup)
                .filter(GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id))
                .filter(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
                .filter(GmvmaxProductCampaignItemGroup.advertiser_id == str(advertiser_id))
                .filter(GmvmaxProductCampaignItemGroup.store_id == str(store_id))
                .filter(GmvmaxProductCampaignItemGroup.campaign_id == str(campaign_identifier))
                .filter(GmvmaxProductCampaignItemGroup.item_group_id == str(item_group_id))
                .first()
            )
            if relation is None:
                db.add(
                    GmvmaxProductCampaignItemGroup(
                        workspace_id=int(workspace_id),
                        auth_id=int(auth_id),
                        advertiser_id=str(advertiser_id),
                        store_id=str(store_id),
                        campaign_id=str(campaign_identifier),
                        item_group_id=str(item_group_id),
                    )
                )
    return row


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
        "live_10s_views": _to_int(
            _extract_field(
                row,
                "live_10s_views",
                "live_view_10s",
                "live_views_10s",
                "10_second_live_views",
            )
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


async def _fetch_campaign_details(
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    campaign_id: str,
) -> Mapping[str, Any] | None:
    try:
        response = await ttb_client.gmv_max_campaign_info(
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
            )
        )
        details = response.data
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

    if isinstance(details, Mapping):
        return details

    if hasattr(details, "model_dump"):
        dumped = details.model_dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped

    return None


async def sync_gmvmax_campaigns(
    db: Session,
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    include_campaign_details: bool = True,
    **filters: Any,
) -> dict:
    # One account snapshot shares a single pre-fetch boundary across every
    # list page and campaign/info lookup.  Using per-write timestamps would
    # let the oldest page win solely because it completed last.
    snapshot_started_at = catalog_observation_now()
    provided_filters = {k: v for k, v in filters.items() if v is not None}
    store_scope = provided_filters.get("store_ids")
    campaign_scope = provided_filters.get("campaign_ids")
    if store_scope is not None:
        store_scope = [str(item) for item in store_scope]
    if campaign_scope is not None:
        campaign_scope = [str(item) for item in campaign_scope]

    bound_store_id = _get_bound_store_id(db, workspace_id=workspace_id, auth_id=auth_id)
    known_campaign_stores: dict[str, str] = {}
    for catalog_model in (GmvmaxProductCampaignCatalog, GmvmaxLiveCampaignCatalog):
        known_rows = db.execute(
            select(catalog_model.campaign_id, catalog_model.store_id).where(
                catalog_model.workspace_id == int(workspace_id),
                catalog_model.auth_id == int(auth_id),
                catalog_model.advertiser_id == str(advertiser_id),
            )
        ).all()
        for known_campaign_id, known_store_id in known_rows:
            normalized_known_store = _normalize_store_identifier(known_store_id)
            if normalized_known_store and (
                not bound_store_id or normalized_known_store == bound_store_id
            ):
                known_campaign_stores[str(known_campaign_id)] = normalized_known_store
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

        async def _fetch_page(page: int):
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
                page_size=100,
                page=page,
            )
            return await ttb_client.gmv_max_campaign_get(request)

        async for fetched_page in iter_numbered_pages(
            _fetch_page,
            rows_from_data=lambda data: data.list,
            item_key=_campaign_pagination_key,
            requested_page_size=100,
            probe_on_missing_metadata=True,
        ):
            data = fetched_page.data

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
                needs_campaign_details = bool(include_campaign_details) or (
                    campaign_identifier not in known_campaign_stores
                )
                if needs_campaign_details and campaign_identifier not in details_cache:
                    details_cache[campaign_identifier] = await _fetch_campaign_details(
                        ttb_client,
                        advertiser_id=str(advertiser_id),
                        campaign_id=campaign_identifier,
                    )
                campaign_details = details_cache.get(campaign_identifier)

                resolved_store_id = _extract_field_from_sources(
                    ("store_id", "shop_id"), campaign_details, payload
                )
                raw_store_is_placeholder = (
                    resolved_store_id is not None
                    and _normalize_store_identifier(resolved_store_id) is None
                )
                if _normalize_store_identifier(resolved_store_id) is None:
                    resolved_store_id = _resolve_store_id(
                        advertiser_id=str(advertiser_id),
                        campaign_payload=payload,
                        page_context=page_context or {},
                    )
                if (
                    _normalize_store_identifier(resolved_store_id) is None
                    and campaign_details
                ):
                    resolved_store_id = _extract_field_from_sources(
                        ("store_id", "shop_id"), campaign_details
                    )
                if _normalize_store_identifier(resolved_store_id) is None:
                    # The existing catalog row was originally admitted through
                    # an exact workspace/auth/advertiser/store proof. Reusing
                    # that verified ownership lets the realtime lane update
                    # status without issuing campaign/info for every known row.
                    resolved_store_id = known_campaign_stores.get(campaign_identifier)

                normalized_store_id = _normalize_store_identifier(resolved_store_id)
                if (
                    normalized_store_id is None
                    and bound_store_id
                    and raw_store_is_placeholder
                ):
                    # This list request was already scoped to the validated
                    # bound store. Only TikTok's explicit ``0`` placeholder
                    # may use that trusted scope as a hint. A genuinely
                    # missing store remains unproven and is skipped below.
                    normalized_store_id = bound_store_id
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
                store_for_round = normalized_store_id

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

                catalog_row = _upsert_campaign_catalog_from_api(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    payload=payload,
                    store_id_hint=store_for_round,
                    campaign_details=campaign_details,
                    campaign_details_complete=campaign_details is not None,
                    promotion_type=promotion_type,
                    source_observed_at=snapshot_started_at,
                )
                if bool(
                    getattr(
                        catalog_row,
                        "_gmvmax_catalog_response_applied",
                        False,
                    )
                ):
                    reconcile_manual_pause_from_official_catalog(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        store_id=getattr(catalog_row, "store_id", None),
                        campaign_id=str(campaign_identifier),
                        operation_status=getattr(
                            catalog_row, "operation_status", None
                        ),
                        secondary_status=getattr(
                            catalog_row, "secondary_status", None
                        ),
                        remote_modified_at=getattr(
                            catalog_row, "modify_time_utc", None
                        ),
                        source_observed_at=snapshot_started_at,
                    )
                synced += 1

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
    campaign_details_complete: bool = False,
    promotion_type: PromotionTypeEnum | None = None,
    source_observed_at: datetime,
) -> Any:
    if not isinstance(payload, dict):
        raise ValueError("payload must be dict")
    result = _upsert_campaign_catalog_from_api(
        db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=str(advertiser_id),
        payload=payload,
        store_id_hint=store_id_hint,
        campaign_details=campaign_details,
        campaign_details_complete=campaign_details_complete,
        promotion_type=promotion_type,
        source_observed_at=source_observed_at,
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
    execution_guard: Callable[[Session], None] | None = None,
) -> dict[str, Any]:
    _ = include_sessions
    bound_store_id = _get_bound_store_id(db, workspace_id=workspace_id, auth_id=auth_id)
    info_request = GMVMaxCampaignInfoRequest(
        advertiser_id=str(advertiser_id), campaign_id=str(campaign_id)
    )
    source_observed_at = catalog_observation_now()
    info_resp = await ttb_client.gmv_max_campaign_info(info_request)

    normalized_store = _normalize_store_identifier(info_resp.data.store_id)
    existing_row = None
    for catalog_model in (GmvmaxProductCampaignCatalog, GmvmaxLiveCampaignCatalog):
        existing_row = (
            db.query(catalog_model)
            .filter(catalog_model.workspace_id == int(workspace_id))
            .filter(catalog_model.auth_id == int(auth_id))
            .filter(catalog_model.advertiser_id == str(advertiser_id))
            .filter(catalog_model.campaign_id == str(campaign_id))
            .first()
        )
        if existing_row is not None:
            break
    resolved_store = (
        normalized_store
        or _normalize_store_identifier(getattr(existing_row, "store_id", None))
        or bound_store_id
    )
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

    info_payload = info_resp.data.model_dump(exclude_none=True)
    upsert_campaign_from_api(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        payload=info_payload,
        store_id_hint=resolved_store,
        campaign_details=info_payload,
        campaign_details_complete=True,
        source_observed_at=source_observed_at,
    )

    synced_at = datetime.now(timezone.utc)

    if execution_guard is not None:
        execution_guard(db)
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
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    stat_time_hour: datetime,
    item_group_id: str,
    metrics: Mapping[str, Any],
    bid_type: Any | None = None,
    source_observed_at: datetime,
    ingested_at: datetime,
    advertiser_timezone: str | None,
) -> GmvProductMetricsHourly:
    stmt = (
        select(GmvProductMetricsHourly)
        .where(GmvProductMetricsHourly.workspace_id == workspace_id)
        .where(GmvProductMetricsHourly.auth_id == auth_id)
        .where(GmvProductMetricsHourly.advertiser_id == advertiser_id)
        .where(GmvProductMetricsHourly.store_id == store_id)
        .where(GmvProductMetricsHourly.campaign_id == campaign_id)
        .where(GmvProductMetricsHourly.item_group_id == item_group_id)
        .where(GmvProductMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvProductMetricsHourly(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            campaign_id=campaign_id,
            item_group_id=item_group_id,
            stat_time_hour=stat_time_hour,
        )
        db.add(instance)

    for field, value in metrics.items():
        if value is not None and hasattr(instance, field):
            setattr(instance, field, value)
    # PRODUCT-level report/get does not support net_cost. Never carry a stale
    # value from an older/invalid request forward as an official product fact.
    instance.net_cost_cents = None

    if bid_type is not None:
        instance.bid_type = str(bid_type)
    _apply_fact_freshness(
        instance,
        stat_day=stat_time_hour.date(),
        source_observed_at=source_observed_at,
        ingested_at=ingested_at,
        advertiser_timezone=advertiser_timezone,
    )

    db.flush()
    return instance


def _upsert_product_metrics_daily(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    stat_time_day: date,
    item_group_id: str,
    metrics: Mapping[str, Any],
    bid_type: Any | None = None,
    source_observed_at: datetime,
    ingested_at: datetime,
    advertiser_timezone: str | None,
) -> GmvProductMetricsDaily:
    stmt = (
        select(GmvProductMetricsDaily)
        .where(GmvProductMetricsDaily.workspace_id == workspace_id)
        .where(GmvProductMetricsDaily.auth_id == auth_id)
        .where(GmvProductMetricsDaily.advertiser_id == advertiser_id)
        .where(GmvProductMetricsDaily.store_id == store_id)
        .where(GmvProductMetricsDaily.campaign_id == campaign_id)
        .where(GmvProductMetricsDaily.item_group_id == item_group_id)
        .where(GmvProductMetricsDaily.stat_time_day == stat_time_day)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvProductMetricsDaily(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            campaign_id=campaign_id,
            item_group_id=item_group_id,
            stat_time_day=stat_time_day,
        )
        db.add(instance)

    for field, value in metrics.items():
        if value is not None and hasattr(instance, field):
            setattr(instance, field, value)
    # PRODUCT-level report/get does not support net_cost. Consumers may derive
    # their display fallback from cost, while provenance remains honest here.
    instance.net_cost_cents = None

    if bid_type is not None:
        instance.bid_type = str(bid_type)
    _apply_fact_freshness(
        instance,
        stat_day=stat_time_day,
        source_observed_at=source_observed_at,
        ingested_at=ingested_at,
        advertiser_timezone=advertiser_timezone,
    )

    db.flush()
    return instance


def _upsert_livestream_metrics_daily(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    room_id: str,
    row: Mapping[str, Any],
) -> GmvLivestreamMetricsDaily:
    stat_time_day = _parse_date(_extract_field(row, "stat_time_day", "stat_time"))
    if stat_time_day is None:
        raise ValueError("date missing")

    stmt = (
        select(GmvLivestreamMetricsDaily)
        .where(GmvLivestreamMetricsDaily.workspace_id == workspace_id)
        .where(GmvLivestreamMetricsDaily.auth_id == auth_id)
        .where(GmvLivestreamMetricsDaily.advertiser_id == advertiser_id)
        .where(GmvLivestreamMetricsDaily.store_id == store_id)
        .where(GmvLivestreamMetricsDaily.room_id == str(room_id))
        .where(GmvLivestreamMetricsDaily.stat_time_day == stat_time_day)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvLivestreamMetricsDaily(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            room_id=str(room_id),
            stat_time_day=stat_time_day,
        )
        db.add(instance)

    instance.store_id = store_id
    instance.campaign_id = str(campaign_id)
    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    for attr, value in {
        "live_views": _to_int(_extract_field(row, "live_views", "live_watch_cnt")),
        "live_10s_views": _to_int(
            _extract_field(
                row,
                "live_10s_views",
                "live_view_10s",
                "live_views_10s",
                "10_second_live_views",
            )
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
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
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
        .where(GmvLivestreamMetricsHourly.workspace_id == workspace_id)
        .where(GmvLivestreamMetricsHourly.auth_id == auth_id)
        .where(GmvLivestreamMetricsHourly.advertiser_id == advertiser_id)
        .where(GmvLivestreamMetricsHourly.store_id == store_id)
        .where(GmvLivestreamMetricsHourly.room_id == str(room_id))
        .where(GmvLivestreamMetricsHourly.stat_time_hour == stat_time_hour)
    )
    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvLivestreamMetricsHourly(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            room_id=str(room_id),
            stat_time_hour=stat_time_hour,
        )
        db.add(instance)

    instance.store_id = store_id
    instance.campaign_id = str(campaign_id)
    metrics_payload = _normalize_metric_payload(row)
    for field, value in metrics_payload.items():
        if hasattr(instance, field):
            setattr(instance, field, value)

    for attr, value in {
        "live_views": _to_int(_extract_field(row, "live_views", "live_watch_cnt")),
        "live_10s_views": _to_int(
            _extract_field(
                row,
                "live_10s_views",
                "live_view_10s",
                "live_views_10s",
                "10_second_live_views",
            )
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
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
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
        .where(GmvDurationMetricsDaily.workspace_id == workspace_id)
        .where(GmvDurationMetricsDaily.auth_id == auth_id)
        .where(GmvDurationMetricsDaily.advertiser_id == advertiser_id)
        .where(GmvDurationMetricsDaily.store_id == store_id)
        .where(GmvDurationMetricsDaily.campaign_id == str(campaign_id))
        .where(GmvDurationMetricsDaily.duration == str(duration_value))
        .where(GmvDurationMetricsDaily.stat_time_day == stat_time_day)
    )
    if item_group_id:
        stmt = stmt.where(GmvDurationMetricsDaily.item_group_id == item_group_id)

    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvDurationMetricsDaily(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            campaign_id=str(campaign_id),
            duration=str(duration_value),
            stat_time_day=stat_time_day,
            item_group_id=item_group_id,
        )
        db.add(instance)

    instance.store_id = store_id
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
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
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
        .where(GmvDurationMetricsHourly.workspace_id == workspace_id)
        .where(GmvDurationMetricsHourly.auth_id == auth_id)
        .where(GmvDurationMetricsHourly.advertiser_id == advertiser_id)
        .where(GmvDurationMetricsHourly.store_id == store_id)
        .where(GmvDurationMetricsHourly.campaign_id == str(campaign_id))
        .where(GmvDurationMetricsHourly.duration == str(duration_value))
        .where(GmvDurationMetricsHourly.stat_time_hour == stat_time_hour)
    )
    if item_group_id:
        stmt = stmt.where(GmvDurationMetricsHourly.item_group_id == item_group_id)

    instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = GmvDurationMetricsHourly(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            campaign_id=str(campaign_id),
            duration=str(duration_value),
            stat_time_hour=stat_time_hour,
            item_group_id=item_group_id,
        )
        db.add(instance)

    instance.store_id = store_id
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
    advertiser_timezone: str | None = None,
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

    resolved_timezone = advertiser_timezone or _advertiser_timezone_for_fact(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    campaign_ids = [campaign.campaign_id]
    for window_start, window_end in _official_report_date_windows(
        start_date_str,
        end_date_str,
        max_days=1,
    ):
        reconciliation_stage = StagedFactKeySet(
            model=GmvProductMetricsHourly,
            time_column="stat_time_hour",
            range_start=datetime.combine(window_start, datetime.min.time()),
            range_end_exclusive=datetime.combine(
                window_end + timedelta(days=1),
                datetime.min.time(),
            ),
            key_columns=("item_group_id", "stat_time_hour"),
            scope_equals={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "store_id": str(store_id),
                "campaign_id": str(campaign.campaign_id),
            },
        )
        page = 1
        pagination_state = ReportPaginationState(require_dimensions=True)
        while True:
            if page > 200:
                raise RuntimeError(
                    "GMV Max product hourly report pagination exceeded "
                    f"200 pages for {window_start}..{window_end}"
                )
            request = _build_campaign_report_request(
                advertiser_id=str(advertiser_id),
                campaign_ids=campaign_ids,
                store_id=store_id,
                start_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
                granularity="HOURLY",
                metrics=_PRODUCT_REPORT_METRICS,
                dimensions=[
                    "item_group_id",
                    "stat_time_hour",
                ],
                page=page,
                page_size=_REPORT_PAGE_SIZE,
            )
            try:
                response = await ttb_client.gmv_max_report_get(
                    request,
                    inject_promotion_types=False,
                )
            except Exception:
                logger.exception(
                    "gmvmax product hourly report fetch failed",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "campaign_id": campaign.campaign_id,
                        "date_start": window_start.isoformat(),
                        "date_end": window_end.isoformat(),
                        "page": page,
                    },
                )
                raise

            data = getattr(response, "data", None)
            rows_raw = getattr(data, "list", None) or []
            rows = [_merge_report_entry(item) for item in rows_raw]
            rows = [row for row in rows if isinstance(row, dict)]
            for row in rows:
                stat_time_value = _extract_field(
                    row,
                    "stat_time_hour",
                    "stat_time",
                    "interval_start",
                )
                stat_time_hour = _parse_datetime(stat_time_value)
                item_group_id = _normalize_identifier(
                    _extract_field(
                        row,
                        "item_group_id",
                        "product_id",
                        "itemId",
                        "spu_id",
                        "item_id",
                    )
                )
                if stat_time_hour is None or not item_group_id:
                    reconciliation_stage.invalidate()
                    logger.debug(
                        "skip product hourly row missing identifiers; absence "
                        "reconciliation disabled for window",
                        extra={
                            "campaign_id": campaign.campaign_id,
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                        },
                    )
                    continue
                if not reconciliation_stage.contains_time(stat_time_hour):
                    reconciliation_stage.invalidate()
                    logger.warning(
                        "skip product hourly row outside requested window; "
                        "absence reconciliation disabled for window",
                        extra={
                            "campaign_id": campaign.campaign_id,
                            "stat_time_hour": stat_time_hour,
                            "window_start": window_start,
                            "window_end": window_end,
                        },
                    )
                    continue
                returned_campaign_id = _normalize_identifier(
                    _extract_field(row, "campaign_id")
                )
                if (
                    returned_campaign_id
                    and returned_campaign_id != str(campaign.campaign_id)
                ):
                    reconciliation_stage.invalidate()
                    logger.warning(
                        "skip product hourly row outside requested campaign; "
                        "absence reconciliation disabled for window",
                        extra={
                            "requested_campaign_id": campaign.campaign_id,
                            "returned_campaign_id": returned_campaign_id,
                        },
                    )
                    continue

                metrics_payload = _normalize_metric_payload(row)
                _upsert_product_metrics_hourly(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=advertiser_id,
                    store_id=store_id,
                    campaign_id=str(campaign.campaign_id),
                    stat_time_hour=stat_time_hour,
                    item_group_id=item_group_id,
                    metrics=metrics_payload,
                    bid_type=_extract_field(row, "bid_type"),
                    source_observed_at=source_observed_at,
                    ingested_at=ingested_at,
                    advertiser_timezone=resolved_timezone,
                )
                reconciliation_stage.add(item_group_id, stat_time_hour)
                synced_rows += 1

            has_more = report_page_has_more(
                data,
                current_page=page,
                rows=rows_raw,
                state=pagination_state,
            )
            if not has_more:
                break
            page += 1
        reconciliation_stage.mark_pagination_complete()
        reconciliation_stage.reconcile(db)

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
    advertiser_timezone: str | None = None,
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

    resolved_timezone = advertiser_timezone or _advertiser_timezone_for_fact(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    campaign_ids = [campaign.campaign_id]
    for window_start, window_end in _official_report_date_windows(
        start_date_str,
        end_date_str,
        max_days=30,
    ):
        reconciliation_stage = StagedFactKeySet(
            model=GmvProductMetricsDaily,
            time_column="stat_time_day",
            range_start=window_start,
            range_end_exclusive=window_end + timedelta(days=1),
            key_columns=("item_group_id", "stat_time_day"),
            scope_equals={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "store_id": str(store_id),
                "campaign_id": str(campaign.campaign_id),
            },
        )
        page = 1
        pagination_state = ReportPaginationState(require_dimensions=True)
        while True:
            if page > 200:
                raise RuntimeError(
                    "GMV Max product daily report pagination exceeded "
                    f"200 pages for {window_start}..{window_end}"
                )
            request = _build_campaign_report_request(
                advertiser_id=str(advertiser_id),
                campaign_ids=campaign_ids,
                store_id=store_id,
                start_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
                granularity="DAILY",
                metrics=_PRODUCT_REPORT_METRICS,
                dimensions=[
                    "item_group_id",
                    "stat_time_day",
                ],
                page=page,
                page_size=_REPORT_PAGE_SIZE,
            )
            try:
                response = await ttb_client.gmv_max_report_get(
                    request,
                    inject_promotion_types=False,
                )
            except Exception:
                logger.exception(
                    "gmvmax product daily report fetch failed",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "campaign_id": campaign.campaign_id,
                        "date_start": window_start.isoformat(),
                        "date_end": window_end.isoformat(),
                        "page": page,
                    },
                )
                raise

            data = getattr(response, "data", None)
            rows_raw = getattr(data, "list", None) or []
            rows = [_merge_report_entry(item) for item in rows_raw]
            rows = [row for row in rows if isinstance(row, dict)]
            for row in rows:
                stat_date = _parse_date(
                    _extract_field(
                        row,
                        "stat_time_day",
                        "date",
                        "stat_time",
                    )
                )
                item_group_id = _normalize_identifier(
                    _extract_field(
                        row,
                        "item_group_id",
                        "product_id",
                        "itemId",
                        "spu_id",
                        "item_id",
                    )
                )
                if stat_date is None or not item_group_id:
                    reconciliation_stage.invalidate()
                    logger.debug(
                        "skip product daily row missing identifiers; absence "
                        "reconciliation disabled for window",
                        extra={
                            "campaign_id": campaign.campaign_id,
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                        },
                    )
                    continue
                if not reconciliation_stage.contains_time(stat_date):
                    reconciliation_stage.invalidate()
                    logger.warning(
                        "skip product daily row outside requested window; "
                        "absence reconciliation disabled for window",
                        extra={
                            "campaign_id": campaign.campaign_id,
                            "stat_time_day": stat_date,
                            "window_start": window_start,
                            "window_end": window_end,
                        },
                    )
                    continue
                returned_campaign_id = _normalize_identifier(
                    _extract_field(row, "campaign_id")
                )
                if (
                    returned_campaign_id
                    and returned_campaign_id != str(campaign.campaign_id)
                ):
                    reconciliation_stage.invalidate()
                    logger.warning(
                        "skip product daily row outside requested campaign; "
                        "absence reconciliation disabled for window",
                        extra={
                            "requested_campaign_id": campaign.campaign_id,
                            "returned_campaign_id": returned_campaign_id,
                        },
                    )
                    continue

                metrics_payload = _normalize_metric_payload(row)
                _upsert_product_metrics_daily(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=advertiser_id,
                    store_id=store_id,
                    campaign_id=str(campaign.campaign_id),
                    stat_time_day=stat_date,
                    item_group_id=item_group_id,
                    metrics=metrics_payload,
                    bid_type=_extract_field(row, "bid_type"),
                    source_observed_at=source_observed_at,
                    ingested_at=ingested_at,
                    advertiser_timezone=resolved_timezone,
                )
                reconciliation_stage.add(item_group_id, stat_date)
                synced_rows += 1

            has_more = report_page_has_more(
                data,
                current_page=page,
                rows=rows_raw,
                state=pagination_state,
            )
            if not has_more:
                break
            page += 1
        reconciliation_stage.mark_pagination_complete()
        reconciliation_stage.reconcile(db)

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
    source_observed_at: datetime,
    ingested_at: datetime,
    advertiser_timezone: str | None,
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
        if value is not None and hasattr(instance, field):
            setattr(instance, field, value)
    _apply_fact_freshness(
        instance,
        stat_day=stat_date,
        source_observed_at=source_observed_at,
        ingested_at=ingested_at,
        advertiser_timezone=advertiser_timezone,
    )

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
    source_observed_at: datetime,
    ingested_at: datetime,
    advertiser_timezone: str | None,
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
        if value is not None and hasattr(instance, field):
            setattr(instance, field, value)
    _apply_fact_freshness(
        instance,
        stat_day=stat_time_hour.date(),
        source_observed_at=source_observed_at,
        ingested_at=ingested_at,
        advertiser_timezone=advertiser_timezone,
    )

    db.flush()
    return instance


async def fetch_overview_summary_rows(
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    start_date: date | str,
    end_date: date | str,
    dimensions: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    requested_dimensions = list(dimensions or ["advertiser_id"])
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)

    rows: list[dict[str, Any]] = []
    for window_start, window_end in _official_report_date_windows(
        start_date_str,
        end_date_str,
        max_days=30,
    ):
        page = 1
        pagination_state = ReportPaginationState(require_dimensions=True)
        while True:
            if page > 200:
                raise RuntimeError(
                    "GMV Max overview summary pagination exceeded 200 pages "
                    f"for {window_start}..{window_end}"
                )
            request = build_gmv_max_report_request(
                dataset=GMVMaxDataset.OVERVIEW,
                advertiser_id=str(advertiser_id),
                store_ids=[str(store_id)],
                start_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
                metrics=list(_OVERVIEW_FINANCIAL_METRICS),
                page=page,
                page_size=_REPORT_PAGE_SIZE,
            )
            request.dimensions = requested_dimensions

            response = await ttb_client.gmv_max_report_get(request)
            data = getattr(response, "data", None)
            rows_raw = getattr(data, "list", None) or []
            rows.extend(
                row
                for row in (
                    _merge_report_entry(item) for item in rows_raw
                )
                if isinstance(row, dict)
            )
            has_more = report_page_has_more(
                data,
                current_page=page,
                rows=rows_raw,
                state=pagination_state,
            )
            if not has_more:
                break
            page += 1
    return rows


def normalize_overview_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_metric_payload(row)


def _aggregate_overview_metrics(metric_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized_rows = [_normalize_metric_payload(row) for row in metric_rows if isinstance(row, Mapping)]
    cost_cents = _sum_int([_to_int(row.get("cost_cents")) for row in normalized_rows])
    net_cost_cents = _sum_int([_to_int(row.get("net_cost_cents")) for row in normalized_rows])
    orders = _sum_int([_to_int(row.get("orders")) for row in normalized_rows])
    gross_revenue_cents = _sum_int([_to_int(row.get("gross_revenue_cents")) for row in normalized_rows])

    cost_per_order: Decimal | None = None
    if cost_cents and orders:
        try:
            cost_per_order = (
                Decimal(cost_cents) / Decimal(orders) / _ONE_HUNDRED
            ).quantize(_DECIMAL_FOUR)
        except (InvalidOperation, ZeroDivisionError):  # pragma: no cover
            cost_per_order = None

    roi = _calc_roi(gross_revenue_cents, cost_cents)

    return {
        "cost_cents": cost_cents,
        "net_cost_cents": net_cost_cents,
        "orders": orders,
        "gross_revenue_cents": gross_revenue_cents,
        "cost_per_order": cost_per_order,
        "roi": roi,
    }


def upsert_overview_snapshot(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    snapshot_type: str,
    start_date: date | None,
    end_date: date | None,
    metrics_rows: Sequence[Mapping[str, Any]],
) -> GmvOverviewSnapshot:
    aggregated = _aggregate_overview_metrics(metrics_rows)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _fetch_existing() -> GmvOverviewSnapshot | None:
        stmt = (
            select(GmvOverviewSnapshot)
            .where(GmvOverviewSnapshot.workspace_id == int(workspace_id))
            .where(GmvOverviewSnapshot.auth_id == int(auth_id))
            .where(GmvOverviewSnapshot.advertiser_id == str(advertiser_id))
            .where(GmvOverviewSnapshot.store_id == str(store_id))
            .where(GmvOverviewSnapshot.snapshot_type == str(snapshot_type))
            .where(GmvOverviewSnapshot.start_date == start_date)
            .where(GmvOverviewSnapshot.end_date == end_date)
            .order_by(GmvOverviewSnapshot.snapshot_at.desc())
        )
        return db.execute(stmt).scalars().first()

    def _apply_snapshot_values(target: GmvOverviewSnapshot) -> None:
        target.start_date = start_date
        target.end_date = end_date
        target.snapshot_at = now
        target.cost_cents = aggregated.get("cost_cents")
        target.net_cost_cents = aggregated.get("net_cost_cents")
        target.orders = aggregated.get("orders")
        target.gross_revenue_cents = aggregated.get("gross_revenue_cents")
        target.cost_per_order = aggregated.get("cost_per_order")
        target.roi = aggregated.get("roi")

    snapshot = _fetch_existing()
    if snapshot is None:
        snapshot = GmvOverviewSnapshot(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            snapshot_type=str(snapshot_type),
            start_date=start_date,
            end_date=end_date,
        )
        db.add(snapshot)

    _apply_snapshot_values(snapshot)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if "uk_gmv_overview_snapshot" not in str(exc.orig):
            raise
        snapshot = _fetch_existing()
        if snapshot is None:
            raise
        _apply_snapshot_values(snapshot)
        db.add(snapshot)
        db.flush()

    if snapshot_type == "MANUAL":
        retention_days = 90
        cutoff_date = date.today() - timedelta(days=retention_days)
        (
            db.query(GmvOverviewSnapshot)
            .filter(GmvOverviewSnapshot.snapshot_type == "MANUAL")
            .filter(GmvOverviewSnapshot.end_date < cutoff_date)
            .delete(synchronize_session=False)
        )
        db.flush()

    return snapshot


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
        campaign_ids=[str(cid) for cid in campaign_ids],
    )
    return GMVMaxReportGetRequest(
        advertiser_id=str(advertiser_id),
        store_ids=[store_id] if store_id else [],
        start_date=start_date,
        end_date=end_date,
        metrics=list(metrics),
        dimensions=list(dimensions),
        campaign_ids=list(campaign_ids),
        filtering=filtering,
        page=page,
        page_size=page_size,
    )


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
    hour_window: tuple[datetime, datetime] | None = None,
    advertiser_timezone: str | None = None,
) -> dict:
    _log_sync_target("OVERVIEW", granularity=str(granularity or "").strip().upper())
    start_date_str = _normalize_date(start_date)
    end_date_str = _normalize_date(end_date)
    if not store_ids:
        return {"synced_rows": 0}

    metrics = list(GMVMAX_BASE_METRICS)
    granularity_normalized = str(granularity or "").strip().upper()
    resolved_timezone = advertiser_timezone or _advertiser_timezone_for_fact(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    max_days = 1 if granularity_normalized == "HOUR" else 30
    synced_rows = 0

    for store_id in store_ids:
        for window_start, window_end in _official_report_date_windows(
            start_date_str,
            end_date_str,
            max_days=max_days,
        ):
            if granularity_normalized == "HOUR":
                natural_range_start = datetime.combine(
                    window_start,
                    datetime.min.time(),
                )
                natural_range_end = datetime.combine(
                    window_end + timedelta(days=1),
                    datetime.min.time(),
                )
                reconciliation_range_start = (
                    max(natural_range_start, hour_window[0])
                    if hour_window
                    else natural_range_start
                )
                reconciliation_range_end = (
                    min(
                        natural_range_end,
                        hour_window[1] + timedelta(microseconds=1),
                    )
                    if hour_window
                    else natural_range_end
                )
                reconciliation_model = GmvOverviewMetricsHourly
                reconciliation_time_column = "stat_time_hour"
            else:
                reconciliation_range_start = window_start
                reconciliation_range_end = window_end + timedelta(days=1)
                reconciliation_model = GmvOverviewMetricsDaily
                reconciliation_time_column = "stat_time_day"

            reconciliation_stage = StagedFactKeySet(
                model=reconciliation_model,
                time_column=reconciliation_time_column,
                range_start=reconciliation_range_start,
                range_end_exclusive=reconciliation_range_end,
                key_columns=(reconciliation_time_column,),
                scope_equals={
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(store_id),
                },
            )
            page = 1
            pagination_state = ReportPaginationState(require_dimensions=True)
            while True:
                if page > 200:
                    raise RuntimeError(
                        "GMV Max overview report pagination exceeded 200 "
                        f"pages for {window_start}..{window_end}"
                    )
                request = build_gmv_max_report_request(
                    dataset=GMVMaxDataset.OVERVIEW,
                    advertiser_id=str(advertiser_id),
                    store_ids=[str(store_id)],
                    start_date=window_start.isoformat(),
                    end_date=window_end.isoformat(),
                    metrics=metrics,
                    page=page,
                    page_size=_REPORT_PAGE_SIZE,
                )

                if granularity_normalized == "HOUR":
                    request.dimensions = [
                        "advertiser_id",
                        "stat_time_hour",
                    ]
                else:
                    request.dimensions = [
                        "advertiser_id",
                        "stat_time_day",
                    ]

                response = await ttb_client.gmv_max_report_get(request)
                data = getattr(response, "data", None)
                rows_raw = getattr(data, "list", None) or []
                rows = [_merge_report_entry(item) for item in rows_raw]
                rows = [row for row in rows if isinstance(row, dict)]

                for row in rows:
                    if granularity_normalized == "HOUR":
                        stat_time_value = _extract_field(
                            row,
                            "stat_time_hour",
                            "stat_time",
                        )
                        stat_time_hour = _parse_datetime(stat_time_value)
                        if stat_time_hour is None:
                            reconciliation_stage.invalidate()
                            continue
                        if hour_window and not (
                            hour_window[0] <= stat_time_hour <= hour_window[1]
                        ):
                            continue
                        if not (
                            reconciliation_range_start
                            <= stat_time_hour
                            < reconciliation_range_end
                        ):
                            reconciliation_stage.invalidate()
                            continue
                        reconciliation_key = stat_time_hour
                    else:
                        stat_time_day = _parse_date(
                            _extract_field(
                                row,
                                "stat_time_day",
                                "date",
                                "stat_time",
                            )
                        )
                        if stat_time_day is None:
                            reconciliation_stage.invalidate()
                            continue
                        if not (
                            reconciliation_range_start
                            <= stat_time_day
                            < reconciliation_range_end
                        ):
                            reconciliation_stage.invalidate()
                            continue
                        reconciliation_key = stat_time_day
                    try:
                        if granularity_normalized == "HOUR":
                            _upsert_overview_hourly(
                                db,
                                workspace_id=workspace_id,
                                auth_id=auth_id,
                                advertiser_id=advertiser_id,
                                store_id=str(store_id),
                                row=row,
                                source_observed_at=source_observed_at,
                                ingested_at=ingested_at,
                                advertiser_timezone=resolved_timezone,
                            )
                        else:
                            _upsert_overview_daily(
                                db,
                                workspace_id=workspace_id,
                                auth_id=auth_id,
                                advertiser_id=advertiser_id,
                                store_id=str(store_id),
                                row=row,
                                source_observed_at=source_observed_at,
                                ingested_at=ingested_at,
                                advertiser_timezone=resolved_timezone,
                            )
                        reconciliation_stage.add(reconciliation_key)
                        synced_rows += 1
                    except ValueError:
                        reconciliation_stage.invalidate()
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

                has_more = report_page_has_more(
                    data,
                    current_page=page,
                    rows=rows_raw,
                    state=pagination_state,
                )
                if not has_more:
                    break
                page += 1
            reconciliation_stage.mark_pagination_complete()
            reconciliation_stage.reconcile(db)

    db.flush()
    return {"synced_rows": synced_rows}


async def _fetch_chunked_live_report_rows(
    ttb_client: TikTokBusinessGMVMaxClient,
    *,
    dataset: GMVMaxDataset,
    advertiser_id: str,
    store_id: str,
    start_date: date | str,
    end_date: date | str,
    campaign_ids: Sequence[str],
    room_ids: Sequence[str] | None,
    metrics: Sequence[str],
    dimensions: Sequence[str],
    max_window_days: int,
) -> list[tuple[Mapping[str, Any], str | None, str | None]]:
    """Fetch all live report partitions within official date/ID/page limits."""

    clean_campaign_ids = _sanitize_id_list(campaign_ids) or []
    clean_room_ids = _sanitize_id_list(room_ids) or []
    if not clean_campaign_ids:
        return []
    if not clean_room_ids and dataset is not GMVMaxDataset.LIVE_LIVESTREAM:
        return []

    room_chunks: list[list[str] | None] = (
        list(chunk_report_filter_ids(clean_room_ids))
        if clean_room_ids
        else [None]
    )

    results: list[tuple[Mapping[str, Any], str | None, str | None]] = []
    for window_start, window_end in _official_report_date_windows(
        start_date,
        end_date,
        max_days=max_window_days,
    ):
        for campaign_chunk in chunk_report_filter_ids(clean_campaign_ids):
            for room_chunk in room_chunks:
                pagination_state = ReportPaginationState(require_dimensions=True)

                async def _fetch_page(
                    page: int,
                    *,
                    campaign_filter: list[str] = campaign_chunk,
                    room_filter: list[str] | None = room_chunk,
                    range_start: date = window_start,
                    range_end: date = window_end,
                ) -> Any:
                    request = build_gmv_max_report_request(
                        dataset=dataset,
                        advertiser_id=str(advertiser_id),
                        store_ids=[str(store_id)],
                        start_date=range_start.isoformat(),
                        end_date=range_end.isoformat(),
                        metrics=list(metrics),
                        campaign_ids=list(campaign_filter),
                        room_ids=list(room_filter) if room_filter else None,
                        page=page,
                        page_size=_REPORT_PAGE_SIZE,
                    )
                    request.dimensions = list(dimensions)
                    return await ttb_client.gmv_max_report_get(request)

                async for fetched_page in iter_numbered_pages(
                    _fetch_page,
                    rows_from_data=lambda data: getattr(data, "list", None) or [],
                    requested_page_size=_REPORT_PAGE_SIZE,
                    probe_on_missing_metadata=True,
                ):
                    pagination_state.validate(
                        page=fetched_page.page,
                        rows=fetched_page.rows,
                    )
                    fallback_campaign_id = (
                        campaign_chunk[0] if len(campaign_chunk) == 1 else None
                    )
                    fallback_room_id = (
                        room_chunk[0]
                        if room_chunk is not None and len(room_chunk) == 1
                        else None
                    )
                    for item in fetched_page.rows:
                        row = _merge_report_entry(item)
                        if isinstance(row, Mapping):
                            response_campaign_id = _normalize_identifier(
                                _extract_field(row, "campaign_id")
                            )
                            if (
                                response_campaign_id is not None
                                and response_campaign_id not in campaign_chunk
                            ):
                                raise RuntimeError(
                                    "GMV Max live report response escaped its "
                                    "campaign filter chunk"
                                )
                            response_room_id = _normalize_identifier(
                                _extract_field(row, "room_id")
                            )
                            if (
                                room_chunk is not None
                                and response_room_id is not None
                                and response_room_id not in room_chunk
                            ):
                                raise RuntimeError(
                                    "GMV Max live report response escaped its "
                                    "room filter chunk"
                                )
                            results.append(
                                (row, fallback_campaign_id, fallback_room_id)
                            )
    return results


def _merge_duration_report_partitions(
    rows: Sequence[tuple[Mapping[str, Any], str | None, str | None]],
    *,
    time_dimension: str,
) -> list[tuple[Mapping[str, Any], str | None, str | None]]:
    """Merge additive metrics split only because a room filter exceeded 100 IDs."""

    additive_fields = (
        "cost",
        "net_cost",
        "orders",
        "gross_revenue",
        "live_views",
        "live_follows",
        "10_second_live_views",
        "live_10s_views",
    )
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    fallback_campaigns: dict[tuple[str, str, str, str], str | None] = {}
    for row, fallback_campaign_id, _fallback_room_id in rows:
        campaign_id = (
            _normalize_identifier(_extract_field(row, "campaign_id"))
            or fallback_campaign_id
            or ""
        )
        key = (
            str(campaign_id),
            str(_normalize_identifier(_extract_field(row, "item_group_id")) or ""),
            str(_normalize_identifier(_extract_field(row, "duration")) or ""),
            str(_extract_field(row, time_dimension) or ""),
        )
        if key not in grouped:
            grouped[key] = dict(row)
            fallback_campaigns[key] = fallback_campaign_id
            continue
        target = grouped[key]
        for field_name in additive_fields:
            incoming = _to_decimal(_extract_field(row, field_name))
            if incoming is None:
                continue
            current = _to_decimal(_extract_field(target, field_name)) or Decimal("0")
            target[field_name] = current + incoming

    for key, row in grouped.items():
        cost = _to_decimal(_extract_field(row, "cost"))
        orders = _to_decimal(_extract_field(row, "orders"))
        revenue = _to_decimal(_extract_field(row, "gross_revenue"))
        live_views = _to_decimal(_extract_field(row, "live_views"))
        live_10s_views = _to_decimal(
            _extract_field(row, "10_second_live_views", "live_10s_views")
        )
        if cost is not None and orders:
            row["cost_per_order"] = cost / orders
        if cost and revenue is not None:
            row["roi"] = revenue / cost
        if cost is not None and live_views:
            row["cost_per_live_view"] = cost / live_views
        if cost is not None and live_10s_views:
            row["cost_per_10_second_live_view"] = cost / live_10s_views

    return [
        (row, fallback_campaigns[key], None)
        for key, row in grouped.items()
    ]


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
    resolved_campaigns = (
        _sanitize_id_list(campaign_ids) or [str(campaign.campaign_id)]
    )
    rows = await _fetch_chunked_live_report_rows(
        ttb_client,
        dataset=GMVMaxDataset.LIVE_LIVESTREAM,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.ROOM]["metrics"]),
        campaign_ids=resolved_campaigns,
        room_ids=resolved_rooms,
        dimensions=["campaign_id", "room_id", "stat_time_hour"],
        max_window_days=1,
    )

    synced_rows = 0
    discovered_rooms = 0
    for row, fallback_campaign_id, fallback_room_id in rows:
        room_id = (
            _normalize_identifier(_extract_field(row, "room_id"))
            or fallback_room_id
        )
        if not room_id:
            continue
        row_campaign_id = (
            _normalize_identifier(_extract_field(row, "campaign_id"))
            or fallback_campaign_id
            or str(campaign.campaign_id)
        )
        discovered_rooms += int(
            _record_campaign_livestream(
                db,
                campaign_id=str(row_campaign_id),
                room_id=room_id,
            )
        )
        try:
            _upsert_livestream_metrics_hourly(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=str(store_id),
                campaign_id=str(row_campaign_id),
                room_id=room_id,
                row=row,
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip livestream hourly metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {
        "synced_rows": synced_rows,
        "discovered_rooms": discovered_rooms,
    }


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
    resolved_campaigns = (
        _sanitize_id_list(campaign_ids) or [str(campaign.campaign_id)]
    )
    rows = await _fetch_chunked_live_report_rows(
        ttb_client,
        dataset=GMVMaxDataset.LIVE_LIVESTREAM,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.ROOM]["metrics"]),
        campaign_ids=resolved_campaigns,
        room_ids=resolved_rooms,
        dimensions=["campaign_id", "room_id", "stat_time_day"],
        max_window_days=30,
    )

    synced_rows = 0
    discovered_rooms = 0
    for row, fallback_campaign_id, fallback_room_id in rows:
        room_id = (
            _normalize_identifier(_extract_field(row, "room_id"))
            or fallback_room_id
        )
        if not room_id:
            continue
        row_campaign_id = (
            _normalize_identifier(_extract_field(row, "campaign_id"))
            or fallback_campaign_id
            or str(campaign.campaign_id)
        )
        discovered_rooms += int(
            _record_campaign_livestream(
                db,
                campaign_id=str(row_campaign_id),
                room_id=room_id,
            )
        )
        try:
            _upsert_livestream_metrics_daily(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=str(store_id),
                campaign_id=str(row_campaign_id),
                room_id=room_id,
                row=row,
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip livestream daily metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {
        "synced_rows": synced_rows,
        "discovered_rooms": discovered_rooms,
    }


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
        await sync_gmvmax_livestream_metrics_hourly(
            db,
            ttb_client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            start_date=start_date_str,
            end_date=end_date_str,
            campaign_ids=campaign_ids,
            room_ids=None,
        )
        resolved_rooms = _resolve_room_ids_for_campaign(
            db,
            campaign_id=str(campaign.campaign_id),
        )
    if not resolved_rooms:
        logger.warning(
            "gmvmax duration hourly sync skipped: room discovery returned no rooms",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    resolved_campaigns = (
        _sanitize_id_list(campaign_ids) or [str(campaign.campaign_id)]
    )
    rows = await _fetch_chunked_live_report_rows(
        ttb_client,
        dataset=GMVMaxDataset.LIVE_DURATION,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.SESSION]["metrics"]),
        campaign_ids=resolved_campaigns,
        room_ids=resolved_rooms,
        dimensions=["campaign_id", "duration", "stat_time_hour"],
        max_window_days=1,
    )
    rows = _merge_duration_report_partitions(
        rows,
        time_dimension="stat_time_hour",
    )

    synced_rows = 0
    for row, fallback_campaign_id, _fallback_room_id in rows:
        row_campaign_id = (
            _normalize_identifier(_extract_field(row, "campaign_id"))
            or fallback_campaign_id
            or str(campaign.campaign_id)
        )
        try:
            _upsert_duration_metrics_hourly(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=store_id,
                campaign_id=str(row_campaign_id),
                row=row,
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
        await sync_gmvmax_livestream_metrics_daily(
            db,
            ttb_client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            start_date=start_date_str,
            end_date=end_date_str,
            campaign_ids=campaign_ids,
            room_ids=None,
        )
        resolved_rooms = _resolve_room_ids_for_campaign(
            db,
            campaign_id=str(campaign.campaign_id),
        )
    if not resolved_rooms:
        logger.warning(
            "gmvmax duration daily sync skipped: room discovery returned no rooms",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign.campaign_id},
        )
        return {"synced_rows": 0}

    resolved_campaigns = (
        _sanitize_id_list(campaign_ids) or [str(campaign.campaign_id)]
    )
    rows = await _fetch_chunked_live_report_rows(
        ttb_client,
        dataset=GMVMaxDataset.LIVE_DURATION,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        start_date=start_date_str,
        end_date=end_date_str,
        metrics=list(GMV_REPORT_CONFIG[GMVMaxReportLevel.SESSION]["metrics"]),
        campaign_ids=resolved_campaigns,
        room_ids=resolved_rooms,
        dimensions=["campaign_id", "duration", "stat_time_day"],
        max_window_days=30,
    )
    rows = _merge_duration_report_partitions(
        rows,
        time_dimension="stat_time_day",
    )

    synced_rows = 0
    for row, fallback_campaign_id, _fallback_room_id in rows:
        row_campaign_id = (
            _normalize_identifier(_extract_field(row, "campaign_id"))
            or fallback_campaign_id
            or str(campaign.campaign_id)
        )
        try:
            _upsert_duration_metrics_daily(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=store_id,
                campaign_id=str(row_campaign_id),
                row=row,
            )
            synced_rows += 1
        except ValueError:
            logger.debug(
                "skip duration daily metrics row without timestamp",
                extra={"campaign_id": campaign.campaign_id, "workspace_id": workspace_id},
            )
            continue

    db.flush()
    return {"synced_rows": synced_rows}


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
