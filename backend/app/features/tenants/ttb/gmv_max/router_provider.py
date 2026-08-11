from __future__ import annotations

import asyncio
import json
import logging
import sys
"""Tenant GMV Max provider-scoped router definitions (router layer)."""

from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil
from pathlib import Path as LocalPath
from types import SimpleNamespace
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Path, Query, Request, UploadFile, status
from pydantic import ValidationError
from celery.result import AsyncResult
from sqlalchemy import JSON as SAJSON
from sqlalchemy import bindparam, case, func, literal, or_, select, text, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import FileResponse

from app.core.config import settings as app_settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.celery_app import (
    GMVMAX_SYNC_INTERVAL_OPTIONS,
    TTB_SYNC_QUEUE,
    celery_app,
)
from app.data.db import get_db
from app.data.models.ttb_entities import (
    TTBAdvertiser,
    TTBAdvertiserStoreLink,
    TTBBCAdvertiserLink,
    TTBProduct,
    TTBProductAdvertiserEligibility,
)
from app.data.models.oauth_ttb import OAuthTikTokAccount
from app.data.models.gmv_restructured import (
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvOverviewSnapshot,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
    GmvStrategyConfig,
)
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxLiveCampaignMetricsHourly,
    GmvmaxLiveCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
    GmvmaxProductCampaignMetricsDaily,
)
from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.data.repositories.tiktok_business.gmvmax_heating import (
    list_heating_configs,
    update_heating_action_result,
    upsert_creative_heating,
)
from app.data.repositories.tiktok_business.gmvmax_metrics import GMVMaxMetricDTO
from app.services.gmvmax_lifecycle import _derive_campaign_lifecycle
from app.gmvmax.services.campaign_catalog_freshness import (
    catalog_observation_now,
    stamp_catalog_row_observation,
)
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxBidRecommendRequest,
    GMVMaxCampaign,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxCreativeStatusUpdateBody,
    GMVMaxCreativeStatusUpdateItem,
    GMVMaxCreativeStatusUpdateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxShopCustomAnchorCreateRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxReportData,
    GMVMaxReportEntry,
    GMVMaxResponse,
    PageInfo,
    GMVMaxSessionListData,
    GMVMaxSessionListRequest,
    GMVMaxSession,
    GMVMaxSessionSettings,
    GMVMaxSessionUpdateBody,
    GMVMaxSessionUpdateRequest,
    GMVMaxStoreAdUsageCheckRequest,
    GMVMaxStoreListRequest,
    GMVMaxVideoGetRequest,
    TikTokAdVideoSearchRequest,
    TikTokAdVideoUploadRequest,
    TikTokAccountPublishStatusRequest,
    TikTokAccountVideoPublishRequest,
    TikTokBusinessGMVMaxClient,
    gmv_max_session_entries,
)
from app.services.oauth_ttb import get_fresh_tiktok_account_token_plain
from app.services.ttb_api import TTBApiError, TTBBusinessError, TTBHttpError
from app.services.ttb_binding_config import (
    BindingConfigStorageNotReady,
    get_binding_config,
    upsert_binding_config,
)
from app.services.provider_registry import provider_registry
from app.services.gmvmax_heating_actions import apply_boost_creative_action
from app.gmvmax.services.mutation_execution_lock import (
    GmvMaxMutationBusy,
    GmvMaxMutationFenceLost,
    active_gmvmax_mutation_lease,
    assert_gmvmax_mutation_current,
    gmvmax_mutation_lease,
)
from app.services.gmvmax_creative_assets import (
    iter_gmvmax_video_entries,
    resolve_store_authorized_bc_id as resolve_creative_asset_store_authorized_bc_id,
    sync_creative_assets_for_scope,
)
from app.services.gmvmax_creative_media_cache import creative_media_urls, resolve_creative_media
from app.services.gmvmax_hermes_decision import (
    apply_approved_plan_defaults_to_create_payload,
)
from app.services.gmvmax_hermes_creative_ranker import rank_creative_candidates
from app.services.ttb_sync import _normalize_identifier
from app.tasks.ttb_sync_tasks import task_sync_products

from .control import (
    MANUAL_UPLOAD_ROOT,
    build_manual_upload_url,
    cancel_pending_campaign_pause_intent,
    clear_manual_override,
    create_or_get_campaign_pause_intent,
    ensure_private_manual_upload_directory,
    get_sync_schedule,
    new_owned_task_id,
    record_task_ownership,
    remove_task_ownership,
    set_manual_pause_override,
    task_is_owned,
    upsert_sync_schedule,
    verify_manual_upload_signature,
)
from ._helpers import (
    GMVMaxAccountBinding,
    get_gmvmax_client_for_account,
    normalize_provider,
    resolve_account_binding,
)
from app.services.gmvmax_spec import (
    GMVMAX_DEFAULT_DIMENSIONS,
    GMVMAX_DEFAULT_METRICS,
    GMVMAX_METRIC_ALIASES,
    GMVMAX_SUPPORTED_DIMENSIONS,
    GMVMAX_SUPPORTED_METRICS,
    GMV_REPORT_CONFIG,
    GMVMaxReportLevel,
)
from .service import (
    _ensure_provider,
    create_gmvmax_campaign as svc_create_campaign,
    get_gmvmax_create_intent,
    gmvmax_create_payload_sha256,
    gmvmax_precheck,
    mark_gmvmax_create_intent,
)
from app.services.ttb_gmvmax import (
    _extract_item_group_ids_from_payload,
    _sanitize_id_list,
    ensure_gmvmax_store_authorized,
    upsert_campaign_from_api,
    update_gmvmax_campaign,
)
from app.gmvmax.creative_status import canonicalize_creative_delivery_status

from .schemas import (
    ActionLogEntry,
    AsyncTaskResponse,
    AutoBindingCandidate,
    AutoBindingRequest,
    AutoBindingResponse,
    BalanceSyncRequest,
    BindingStatusResponse,
    CampaignActionRequest,
    CampaignActionResponse,
    CampaignDetailResponse,
    CampaignFilter,
    CampaignListOptions,
    CampaignListResponse,
    CreateCampaignRequest,
    CreativeHeatingActionRequest,
    CreativeHeatingListResponse,
    CreativeHeatingActionResponse,
    CreativeHeatingRecord,
    DEFAULT_PROMOTION_TYPES,
    GMVMaxCampaignInfoData,
    GmvMaxManualSyncRequest,
    IdentityListResponse,
    IdentitySummary,
    GMVMaxPrecheckRequest,
    GMVMaxPrecheckResponse,
    MetricsRequest,
    MetricsFreshness,
    MetricsResponse,
    ReportFiltering,
    ReportRequest,
    StrategyPreviewRequest,
    StrategyPreviewResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    StrategyUpdateResponse,
    SyncIntervalResponse,
    SyncIntervalUpdateRequest,
    SyncTaskResponse,
    SyncTaskStateResponse,
    UpdateCampaignRequest,
    normalize_datetime_to_date,
)

router = APIRouter(prefix="/gmvmax")
logger = logging.getLogger("gmv.ttb.gmvmax.router")

SUPPORTED_GMVMAX_METRIC_LEVELS: set[GMVMaxReportLevel] = {
    GMVMaxReportLevel.OVERVIEW,
    GMVMaxReportLevel.CAMPAIGN,
    GMVMaxReportLevel.PRODUCT,
    GMVMaxReportLevel.CREATIVE,
}


# === GMV Max sync cadence & task monitoring ===
# - fetch/update sync interval configuration
# - enqueue full sync jobs and check Celery task status
def _build_task_response(
    async_res: AsyncResult,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
) -> AsyncTaskResponse:
    task_id = async_res.id or ""
    status_url = (
        f"/tenants/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/tasks/{task_id}"
        if task_id
        else None
    )
    return AsyncTaskResponse(task_id=task_id, state=str(async_res.state), status_url=status_url)


def _sync_task_progress(state: str, raw_info: Any) -> dict[str, str] | None:
    """Expose a stable, secret-free phase for the polling UI."""

    normalized = str(state or "").upper()
    detail = str(raw_info or "").lower()
    if normalized == "PENDING":
        return {"phase": "QUEUED", "message": "同步任务排队中…"}
    if normalized == "STARTED":
        return {"phase": "SYNCING_REPORTS", "message": "正在同步当前系列报表…"}
    if normalized == "RETRY":
        if "account sync" in detail or "account sync lease" in detail:
            return {
                "phase": "WAITING_ACCOUNT_SYNC",
                "message": "同账户定时同步正在收尾，当前系列将在锁释放后自动开始…",
            }
        if "quota" in detail or "rate" in detail or "429" in detail:
            return {
                "phase": "WAITING_UPSTREAM_QUOTA",
                "message": "TikTok API 限流，系统正在自动重试…",
            }
        return {"phase": "RETRYING", "message": "同步暂时中断，系统正在自动重试…"}
    return None


def _enqueue_owned_task(
    context: GMVMaxRouteContext,
    *,
    task_name: str,
    kwargs: Mapping[str, Any],
    queue: str,
    created_by_user_id: int | None = None,
) -> AsyncResult:
    """Persist task ownership before publishing so polling cannot race the DB."""

    task_id = new_owned_task_id()
    record_task_ownership(
        context.db,
        task_id=task_id,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
        task_name=task_name,
        created_by_user_id=created_by_user_id,
    )
    context.db.commit()
    try:
        return celery_app.send_task(
            task_name,
            kwargs=dict(kwargs),
            queue=queue,
            task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001 - broker failures become a stable API error
        remove_task_ownership(context.db, task_id)
        context.db.commit()
        logger.exception(
            "gmvmax owned task publish failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "task_id": task_id,
                "task_name": task_name,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "GMVMAX_TASK_PUBLISH_FAILED",
                "message": "GMV Max task could not be queued; please retry.",
            },
        ) from exc


def _require_owned_task(
    context: GMVMaxRouteContext,
    *,
    task_id: str,
) -> None:
    if not task_is_owned(
        context.db,
        task_id=str(task_id),
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
    ):
        # Deliberately use 404 so task UUIDs cannot be probed across scopes.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "GMVMAX_TASK_NOT_FOUND", "message": "Task not found."},
        )


def _validate_bound_scope(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str | None = None,
    store_id: str | None = None,
) -> tuple[str, str | None]:
    """Reject caller-supplied scope overrides outside the account binding."""

    bound_advertiser = _normalize_identifier(context.advertiser_id)
    bound_store = _normalize_identifier(context.store_id)
    requested_advertiser = _normalize_identifier(advertiser_id)
    requested_store = _normalize_identifier(store_id)
    if (
        requested_advertiser
        and bound_advertiser
        and requested_advertiser != bound_advertiser
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GMVMAX_ADVERTISER_SCOPE_MISMATCH",
                "message": "advertiser_id is outside the bound GMV Max scope.",
            },
        )
    if requested_store and bound_store and requested_store != bound_store:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GMVMAX_STORE_SCOPE_MISMATCH",
                "message": "store_id is outside the bound GMV Max scope.",
            },
        )
    effective_advertiser = requested_advertiser or bound_advertiser
    effective_store = requested_store or bound_store
    if not effective_advertiser:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "GMVMAX_ADVERTISER_REQUIRED",
                "message": "advertiser_id is required.",
            },
        )
    return str(effective_advertiser), str(effective_store) if effective_store else None


@contextmanager
def _manual_guard_mutation_lease(
    context: GMVMaxRouteContext,
    *,
    priority_pause: bool = False,
):
    """Serialize an operator mutation with Guard and account fact writers."""

    try:
        with gmvmax_mutation_lease(
            context.db,
            workspace_id=int(context.workspace_id),
            auth_id=int(context.auth_id),
            owner_prefix="manual-pause" if priority_pause else "manual",
            timeout=0.2,
            # A manual pause for account A must not wait on an automated Guard
            # cycle for account B. The per-account fenced lease still protects
            # an in-flight TikTok mutation for the same account.
            bypass_automated_global_lease=priority_pause,
        ) as lease:
            yield lease
    except GmvMaxMutationBusy as exc:
        context.db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GMVMAX_MUTATION_INFLIGHT",
                "message": "A GMV Max sync or action is in progress; retry shortly.",
            },
        ) from exc
    except GmvMaxMutationFenceLost as exc:
        context.db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GMVMAX_MUTATION_FENCE_LOST",
                "message": "Mutation ownership was lost; no local success was committed.",
            },
        ) from exc


def _is_retryable_manual_mutation_conflict(detail: Any) -> bool:
    """Only lease contention is safe to turn into a durable pause request."""

    if not isinstance(detail, Mapping):
        return False
    return str(detail.get("code") or "") in {
        "GMVMAX_MUTATION_INFLIGHT",
        "GMVMAX_MUTATION_FENCE_LOST",
    }


def _queue_campaign_pause_after_mutation_conflict(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    campaign: Any,
    actor: str | None,
    reason: str | None,
    before: Mapping[str, Any],
    disable_strategy: bool = False,
) -> CampaignActionResponse:
    """Commit a user pause before returning when account work owns the lease.

    The broker notification is deliberately best-effort.  The durable intent
    scanner is the source of truth if the notification races a broker outage.
    """

    advertiser_id = str(getattr(campaign, "advertiser_id", None) or context.advertiser_id)
    store_id = getattr(campaign, "store_id", None) or context.store_id
    intent, created = create_or_get_campaign_pause_intent(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=advertiser_id,
        store_id=str(store_id) if store_id else None,
        campaign_id=str(campaign_id),
        actor=actor,
        reason=reason,
    )
    # A pending user pause must suppress automatic enable/rebuild behavior
    # before the remote mutation can be serialized behind the active sync.
    set_manual_pause_override(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=advertiser_id,
        store_id=str(store_id) if store_id else None,
        campaign_id=str(campaign_id),
        actor=actor,
        reason=reason,
        pending=True,
    )
    if disable_strategy:
        _disable_local_automation_strategy(context, str(campaign_id))
    _log_action_entry(
        context,
        campaign_id=str(campaign_id),
        campaign=campaign,
        action="PAUSE",
        actor=actor,
        before=before,
        after=before,
        result="QUEUED",
        reason=reason,
    )
    try:
        context.db.commit()
    except Exception:
        context.db.rollback()
        raise
    if created:
        try:
            celery_app.send_task(
                "gmvmax.execute_campaign_pause_intent",
                kwargs={"intent_id": str(intent.id)},
                queue="gmvmax_control",
            )
        except Exception:  # noqa: BLE001
            # The scheduled scanner will enqueue this exact durable intent.
            # Do not tell the operator the pause was lost merely because the
            # initial broker handoff was unavailable.
            logger.exception(
                "gmvmax pause intent initial enqueue failed",
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "campaign_id": str(campaign_id),
                    "pause_intent_id": str(intent.id),
                },
            )
    return CampaignActionResponse(
        type="pause",
        status="queued",
        response={
            "pause_intent_id": str(intent.id),
            "coalesced": not created,
            "strategy_disabled": bool(disable_strategy),
            "message": "Manual pause has taken priority and will run as soon as the current same-account request ends.",
        },
    )


_ACTION_LOG_TYPES = {
    "pause": "PAUSE",
    "enable": "START",
    "delete": "DELETE",
    "update_budget": "SET_BUDGET",
    "update_strategy": "UPDATE_STRATEGY",
}


_DEFAULT_SEED_CONVERSIONS = 3
_DEFAULT_SEED_ROAS = 2.0
_DEFAULT_SEED_SPEND = 20.0


def _empty_overview_metrics_response() -> dict[str, Any]:
    empty_list: list[dict[str, Any]] = []
    return {
        "report": {
            "list": empty_list,
            "page_info": {
                "page": 1,
                "page_size": 0,
                "total_number": 0,
                "total_page": 1,
                "cursor": None,
                "has_more": False,
                "has_next": False,
            },
            "summary": None,
        },
        "request_id": None,
    }


def _catalog_model_to_mapping(row: Any, promotion_type: str) -> Dict[str, Any]:
    return {
        "campaign_id": getattr(row, "campaign_id", None),
        "campaign_name": getattr(row, "campaign_name", None),
        "advertiser_id": getattr(row, "advertiser_id", None),
        "operation_status": getattr(row, "operation_status", None),
        "secondary_status": getattr(row, "secondary_status", None),
        "objective_type": getattr(row, "objective_type", None),
        "schedule_type": getattr(row, "schedule_type", None),
        "schedule_start_time_utc": getattr(row, "schedule_start_time_utc", None),
        "schedule_end_time_utc": getattr(row, "schedule_end_time_utc", None),
        "create_time_utc": getattr(row, "create_time_utc", None),
        "modify_time_utc": getattr(row, "modify_time_utc", None),
        "roas_bid": getattr(row, "roas_bid", None),
        "detail_raw_json": getattr(row, "detail_raw_json", None),
        "gmv_max_promotion_type": promotion_type,
        "updated_at": getattr(row, "updated_at", None),
    }


def _catalog_row_to_detail(row: Any, promotion_type: str) -> GMVMaxCampaignInfoData:
    campaign = _catalog_row_to_campaign(_catalog_model_to_mapping(row, promotion_type))
    payload = campaign.model_dump(exclude_none=True)
    raw_detail = getattr(row, "detail_raw_json", None)
    if isinstance(raw_detail, Mapping):
        payload.update({k: v for k, v in raw_detail.items() if v is not None})
    if getattr(row, "store_id", None):
        payload["store_id"] = str(row.store_id)
    shopping_ads_type = getattr(row, "shopping_ads_type", None)
    if shopping_ads_type:
        payload["shopping_ads_type"] = shopping_ads_type
    return GMVMaxCampaignInfoData.model_validate(payload)


def _campaign_item_group_ids(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
) -> list[str]:
    """Return the durable PRODUCT campaign bindings in stable order."""

    return [
        str(value)
        for value in db.scalars(
            select(GmvmaxProductCampaignItemGroup.item_group_id)
            .where(
                GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id),
                GmvmaxProductCampaignItemGroup.auth_id == int(auth_id),
                GmvmaxProductCampaignItemGroup.advertiser_id == str(advertiser_id),
                GmvmaxProductCampaignItemGroup.store_id == str(store_id),
                GmvmaxProductCampaignItemGroup.campaign_id == str(campaign_id),
            )
            .order_by(GmvmaxProductCampaignItemGroup.item_group_id.asc())
        ).all()
        if value is not None and str(value).strip()
    ]

def _count_products(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> tuple[int, int]:
    query = db.query(TTBProductAdvertiserEligibility).filter(
        TTBProductAdvertiserEligibility.workspace_id == int(workspace_id),
        TTBProductAdvertiserEligibility.auth_id == int(auth_id),
        TTBProductAdvertiserEligibility.advertiser_id == str(advertiser_id),
        TTBProductAdvertiserEligibility.store_id == str(store_id),
        TTBProductAdvertiserEligibility.is_eligible.is_(True),
    )
    total = int(query.count() or 0)
    missing = int(
        query.filter(
            TTBProductAdvertiserEligibility.gmv_max_ads_status.is_(None)
        ).count()
        or 0
    )
    return total, missing


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _configured_product_price_for_hermes(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    item_group_id: str | None,
) -> tuple[float | None, str | None]:
    if not item_group_id:
        return None, None
    # Product prices may live on an older campaign strategy. Keep the newest
    # precedence, but inspect the complete account scope instead of an
    # arbitrary recent prefix.
    rows = (
        db.query(GmvStrategyConfig.config_json)
        .filter(GmvStrategyConfig.workspace_id == int(workspace_id))
        .filter(GmvStrategyConfig.auth_id == int(auth_id))
        .order_by(GmvStrategyConfig.updated_at.desc())
        .all()
    )
    for (config_json,) in rows:
        config = config_json if isinstance(config_json, Mapping) else {}
        for section_name in ("smart_guard", "creative_guard"):
            section = config.get(section_name)
            if not isinstance(section, Mapping):
                continue
            prices = section.get("product_effective_prices")
            if not isinstance(prices, Mapping):
                continue
            for key, value in prices.items():
                if str(key) != str(item_group_id):
                    continue
                price = _float_or_none(value)
                if price and price > 0:
                    return price, f"strategy.{section_name}.product_effective_prices"
    return None, None


def _product_price_for_hermes(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    store_id: str,
    item_group_id: str | None,
) -> tuple[float | None, str | None]:
    configured_price, configured_source = _configured_product_price_for_hermes(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        item_group_id=item_group_id,
    )
    if configured_price is not None:
        return configured_price, configured_source
    if not item_group_id:
        return None, None
    try:
        row = db.execute(
            text(
                """
                select effective_price, min_price, price
                from ttb_products
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and store_id=:store_id
                  and product_id=:product_id
                limit 1
                """
            ),
            {
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "store_id": str(store_id),
                "product_id": str(item_group_id),
            },
        ).mappings().first()
    except SQLAlchemyError:
        logger.exception(
            "Failed to load product price for Hermes creative ranking",
            extra={"item_group_id": str(item_group_id), "store_id": str(store_id)},
        )
        db.rollback()
        return None, None
    if not row:
        return None, None
    for key in ("effective_price", "min_price", "price"):
        price = _float_or_none(row.get(key))
        if price and price > 0:
            return price, f"ttb_products.{key}"
    return None, None


def _load_historical_removed_creatives(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    item_group_ids: Sequence[str] | None,
    guard_config: Mapping[str, Any] | None = None,
) -> list[tuple[str, str | None]]:
    normalized_items = list(
        dict.fromkeys(
            str(item).strip()
            for item in item_group_ids or []
            if str(item or "").strip()
        )
    )
    if not normalized_items:
        return []
    config = dict(guard_config or {})
    if not bool(config.get("historical_blacklist_enabled", True)):
        return []

    def _cfg_int(key: str, default: int) -> int:
        try:
            return int(config.get(key, default))
        except Exception:  # noqa: BLE001
            return default

    def _cfg_decimal(key: str, default: str) -> Decimal:
        try:
            return Decimal(str(config.get(key, default)))
        except Exception:  # noqa: BLE001
            return Decimal(default)

    def _value_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:  # noqa: BLE001
            return default

    product_price_cache: dict[str | None, int | None] = {}

    def _product_price_cents(item_group_id: str | None) -> int | None:
        if item_group_id in product_price_cache:
            return product_price_cache[item_group_id]
        price_map = config.get("product_effective_prices") or {}
        if isinstance(price_map, Mapping):
            value = price_map.get(str(item_group_id)) if item_group_id else None
            if value is None:
                value = price_map.get("default")
            try:
                price = Decimal(str(value)) if value not in (None, "") else None
            except Exception:  # noqa: BLE001
                price = None
            if price and price > 0:
                product_price_cache[item_group_id] = int(
                    (price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
                return product_price_cache[item_group_id]
        if item_group_id:
            row = db.execute(
                text(
                    """
                    select effective_price, min_price, price
                    from ttb_products
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and store_id=:store_id
                      and product_id=:product_id
                    limit 1
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "store_id": str(store_id),
                    "product_id": str(item_group_id),
                },
            ).mappings().first()
            if row:
                for key in ("effective_price", "min_price", "price"):
                    value = row.get(key)
                    try:
                        price = Decimal(str(value)) if value not in (None, "") else None
                    except Exception:  # noqa: BLE001
                        price = None
                    if price and price > 0:
                        product_price_cache[item_group_id] = int(
                            (price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                        )
                        return product_price_cache[item_group_id]
        product_price_cache[item_group_id] = None
        return None

    def _threshold_cents(item_group_id: str | None, multiplier_key: str) -> int:
        min_spend = max(0, _cfg_int("historical_blacklist_min_spend_cents", 300))
        price_cents = _product_price_cents(item_group_id)
        if not price_cents:
            return min_spend
        multiplier = _cfg_decimal(multiplier_key, "1.0")
        price_threshold = int(
            (Decimal(price_cents) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        return max(min_spend, price_threshold)

    rows = db.execute(
        text(
            """
            select
                m.creative_id,
                m.item_group_id,
                m.campaign_id as metric_campaign_id,
                m.stat_time_day,
                coalesce(m.cost_cents, 0) as metric_cost_cents,
                coalesce(m.gross_revenue_cents, 0) as metric_gmv_cents,
                coalesce(m.orders, 0) as metric_orders,
                e.id as remove_event_id,
                e.campaign_id as remove_campaign_id,
                e.reason as remove_reason,
                coalesce(e.cost_cents, 0) as remove_cost_cents,
                e.created_at as remove_created_at,
                m.updated_at as metric_updated_at
            from gmvmax_product_creative_metrics_daily m
            join gmv_campaign_guard_events e
              on e.workspace_id=m.workspace_id
             and e.auth_id=m.auth_id
             and e.advertiser_id=m.advertiser_id
             and e.store_id=m.store_id
             and e.campaign_id=m.campaign_id
            where m.workspace_id=:workspace_id
              and m.auth_id=:auth_id
              and m.advertiser_id=:advertiser_id
              and m.store_id=:store_id
              and m.item_group_id in :item_group_ids
              and m.creative_id is not null
              and m.creative_id not in ('', '-1', '0')
              and e.event_type='CREATIVE_GUARD'
              and e.action='REMOVE'
              and e.result='SUCCESS'
              and e.reason <> 'creative_guard:inherit_historical_exclusions'
              and (
                    json_search(e.request_json, 'one', m.creative_id, null, '$') is not null
                 or json_search(e.response_json, 'one', m.creative_id, null, '$') is not null
              )
              and not exists (
                    select 1
                    from gmvmax_product_creative_metrics_daily good
                    where good.workspace_id=m.workspace_id
                      and good.auth_id=m.auth_id
                      and good.advertiser_id=m.advertiser_id
                      and good.store_id=m.store_id
                      and good.item_group_id=m.item_group_id
                      and good.creative_id=m.creative_id
                      and coalesce(good.orders, 0) >= :historical_reinclude_min_orders
                      and coalesce(good.roi, 0) >= :historical_reinclude_min_roi
              )
            order by m.updated_at desc
            """
        ).bindparams(bindparam("item_group_ids", expanding=True)),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "item_group_ids": normalized_items,
            "historical_reinclude_min_orders": max(1, _cfg_int("historical_reinclude_min_orders", 1)),
            "historical_reinclude_min_roi": str(_cfg_decimal("historical_reinclude_min_roi", "1.2")),
        },
    ).mappings().all()

    grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
    timezone_row = db.execute(
        text(
            """
            select display_timezone, timezone
            from ttb_advertisers
            where workspace_id=:workspace_id and auth_id=:auth_id
              and advertiser_id=:advertiser_id
            limit 1
            """
        ),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": str(advertiser_id),
        },
    ).mappings().first()
    timezone_name = (
        timezone_row.get("display_timezone") or timezone_row.get("timezone")
        if timezone_row
        else None
    )
    time_bucket_hours = max(1, min(24, _cfg_int("historical_blacklist_time_bucket_hours", 4)))

    def _event_bucket(value: Any) -> str | None:
        if not value:
            return None
        created_at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if timezone_name:
            try:
                created_at = created_at.astimezone(ZoneInfo(str(timezone_name)))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return f"{created_at.date().isoformat()}:{(created_at.hour // time_bucket_hours) * time_bucket_hours:02d}"

    for row in rows:
        creative_id = str(row.get("creative_id") or "").strip()
        if not creative_id:
            continue
        item_group_id = str(row.get("item_group_id") or "").strip() or None
        key = (creative_id, item_group_id)
        group = grouped.setdefault(
            key,
            {
                "metric_keys": set(),
                "event_ids": set(),
                "event_campaigns": set(),
                "event_time_buckets": set(),
                "cost_cents": 0,
                "gmv_cents": 0,
                "orders": 0,
                "max_event_cost_cents": 0,
                "last_metric_at": row.get("metric_updated_at"),
            },
        )
        metric_key = (
            str(row.get("metric_campaign_id") or ""),
            str(row.get("stat_time_day") or ""),
            creative_id,
            str(item_group_id or ""),
        )
        if metric_key not in group["metric_keys"]:
            group["metric_keys"].add(metric_key)
            group["cost_cents"] += _value_int(row.get("metric_cost_cents"), 0)
            group["gmv_cents"] += _value_int(row.get("metric_gmv_cents"), 0)
            group["orders"] += _value_int(row.get("metric_orders"), 0)
        event_id = row.get("remove_event_id")
        if event_id is not None and event_id not in group["event_ids"]:
            group["event_ids"].add(event_id)
            group["event_campaigns"].add(str(row.get("remove_campaign_id") or ""))
            bucket = _event_bucket(row.get("remove_created_at"))
            if bucket:
                group["event_time_buckets"].add(bucket)
            group["max_event_cost_cents"] = max(
                int(group["max_event_cost_cents"]),
                int(row.get("remove_cost_cents") or 0),
            )
        if row.get("metric_updated_at") and (
            not group["last_metric_at"] or row.get("metric_updated_at") > group["last_metric_at"]
        ):
            group["last_metric_at"] = row.get("metric_updated_at")

    min_remove_events = max(1, _cfg_int("historical_blacklist_min_remove_events", 3))
    min_distinct_campaigns = max(1, _cfg_int("historical_blacklist_min_distinct_campaigns", 2))
    min_distinct_time_buckets = max(1, _cfg_int("historical_blacklist_min_distinct_time_buckets", 3))
    poor_roi_min_orders = max(1, _cfg_int("historical_blacklist_poor_roi_min_orders", 2))
    poor_roi_floor = _cfg_decimal("historical_blacklist_poor_roi_floor", "0.8")

    qualified: list[tuple[tuple[str, str | None], dict[str, Any]]] = []
    for key, group in grouped.items():
        creative_id, item_group_id = key
        if bool(config.get("historical_blacklist_honor_add_events", True)):
            latest_action = db.execute(
                text(
                    """
                    select action
                    from gmv_campaign_guard_events
                    where workspace_id=:workspace_id and auth_id=:auth_id
                      and advertiser_id=:advertiser_id and store_id=:store_id
                      and event_type='CREATIVE_GUARD' and action in ('REMOVE','ADD')
                      and result='SUCCESS'
                      and (request_json like :needle or response_json like :needle)
                    order by created_at desc, id desc limit 1
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(store_id),
                    "needle": f'%"{creative_id}"%',
                },
            ).scalar_one_or_none()
            if latest_action and str(latest_action).upper() != "REMOVE":
                continue
        cost_cents = int(group["cost_cents"])
        gmv_cents = int(group["gmv_cents"])
        orders = int(group["orders"])
        if cost_cents <= 0:
            continue
        remove_events = len(group["event_ids"])
        distinct_campaigns = len({item for item in group["event_campaigns"] if item})
        distinct_time_buckets = len(group["event_time_buckets"])
        aggregate_roi = Decimal(gmv_cents) / Decimal(cost_cents) if cost_cents > 0 else Decimal("0")
        zero_order_spend = _threshold_cents(item_group_id, "historical_blacklist_zero_order_price_multiplier")
        poor_roi_spend = _threshold_cents(item_group_id, "historical_blacklist_poor_roi_price_multiplier")
        single_event_spend = _threshold_cents(item_group_id, "historical_blacklist_single_event_price_multiplier")
        enough_repeated_evidence = (
            remove_events >= min_remove_events
            and distinct_campaigns >= min_distinct_campaigns
            and distinct_time_buckets >= min_distinct_time_buckets
        )
        zero_order_bad = enough_repeated_evidence and orders <= 0 and cost_cents >= zero_order_spend
        poor_roi_bad = (
            enough_repeated_evidence
            and orders >= poor_roi_min_orders
            and cost_cents >= poor_roi_spend
            and aggregate_roi <= poor_roi_floor
        )
        high_spend_bad_converter = (
            enough_repeated_evidence
            and orders > 0
            and cost_cents >= single_event_spend
            and aggregate_roi <= poor_roi_floor
        )
        if zero_order_bad or poor_roi_bad or high_spend_bad_converter:
            qualified.append((key, group))

    qualified.sort(key=lambda item: item[1].get("last_metric_at") or datetime.min, reverse=True)
    return [key for key, _ in qualified]


async def _inherit_historical_creative_exclusions(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    store_id: str,
    item_group_ids: Sequence[str] | None,
    strategy_id: int | None = None,
    guard_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = context.db
    if db is None:
        return {"excluded": 0, "reason": "db_unavailable"}
    mutation = active_gmvmax_mutation_lease(db)
    if mutation is None:
        raise GmvMaxMutationFenceLost(
            "historical creative exclusion has no active mutation lease"
        )
    creatives = _load_historical_removed_creatives(
        db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=context.advertiser_id,
        store_id=store_id,
        item_group_ids=item_group_ids,
        guard_config=guard_config,
    )
    if not creatives:
        return {"excluded": 0, "reason": "no_historical_removed_creatives"}
    # TikTok counts posts, not (post, SPU) pairs. Keep one wire item per post
    # and carry every associated product in that item's spu_id_list.
    products_by_creative: dict[str, list[str]] = {}
    for creative_id, item_group_id in creatives:
        normalized_creative_id = str(creative_id).strip()
        if not normalized_creative_id:
            continue
        products = products_by_creative.setdefault(normalized_creative_id, [])
        normalized_item_group_id = str(item_group_id or "").strip()
        if normalized_item_group_id and normalized_item_group_id not in products:
            products.append(normalized_item_group_id)
    item_list = [
        GMVMaxCreativeStatusUpdateItem(
            item_id=creative_id,
            spu_id_list=products or None,
        )
        for creative_id, products in products_by_creative.items()
    ]
    if not item_list:
        return {"excluded": 0, "reason": "no_historical_removed_creatives"}

    request_payload: dict[str, Any] = {
        "advertiser_id": str(context.advertiser_id),
        "campaign_id": str(campaign_id),
        "action": "REMOVE",
        "requested": len(item_list),
        "batch_size": 400,
        "item_list": [
            item.model_dump(exclude_none=True)
            for item in item_list
        ],
    }
    excluded_count = 0
    batch_results: list[dict[str, Any]] = []
    batch_errors: list[dict[str, Any]] = []
    if len(item_list) > 10_000:
        batch_errors.append(
            {
                "batch": None,
                "requested": len(item_list),
                "error": (
                    "historical exclusion set exceeds TikTok's official "
                    "10,000-post per-campaign limit"
                ),
            }
        )
    else:
        for batch_index, offset in enumerate(range(0, len(item_list), 400), start=1):
            batch_items = item_list[offset : offset + 400]
            api_request = GMVMaxCreativeStatusUpdateRequest(
                advertiser_id=str(context.advertiser_id),
                body=GMVMaxCreativeStatusUpdateBody(
                    campaign_id=str(campaign_id),
                    item_list=batch_items,
                    action="REMOVE",
                ),
            )
            try:
                mutation.assert_current(db)
                response = await _call_tiktok(
                    context.client.gmv_max_creative_status_update,
                    api_request,
                )
                mutation.assert_current(db)
                response_payload = (
                    response.model_dump(exclude_none=True)
                    if hasattr(response, "model_dump")
                    else {}
                )
                excluded_count += len(batch_items)
                batch_results.append(
                    {
                        "batch": batch_index,
                        "requested": len(batch_items),
                        "response": response_payload,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - continue to audit every official batch
                logger.warning(
                    "Failed to inherit one historical creative exclusion batch",
                    extra={
                        "campaign_id": str(campaign_id),
                        "batch": batch_index,
                        "batch_size": len(batch_items),
                        "creative_count": len(item_list),
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                batch_errors.append(
                    {
                        "batch": batch_index,
                        "requested": len(batch_items),
                        "error": str(exc),
                    }
                )

    result_status = "FAILED" if batch_errors else "SUCCESS"
    response_payload = {
        "excluded": excluded_count,
        "requested": len(item_list),
        "successful_batches": batch_results,
        "failed_batches": batch_errors,
    }
    if batch_errors:
        response_payload["error"] = (
            batch_errors[0]["error"]
            if len(batch_errors) == 1
            else f"{len(batch_errors)} historical exclusion batch(es) failed"
        )
    event_statement = text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, event_type, action, reason, result,
                request_json, response_json, created_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                :strategy_id, 'CREATIVE_GUARD', 'REMOVE',
                'creative_guard:inherit_historical_exclusions', :result,
                :request_json, :response_json, :created_at
            )
            """
        ).bindparams(
            bindparam("request_json", type_=SAJSON),
            bindparam("response_json", type_=SAJSON),
        )
    db.execute(
        event_statement,
        {
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "advertiser_id": str(context.advertiser_id),
            "store_id": str(store_id),
            "campaign_id": str(campaign_id),
            "strategy_id": strategy_id,
            "result": result_status,
            "request_json": request_payload,
            "response_json": response_payload,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )
    if batch_errors:
        # The caller must not report campaign creation as fully successful when
        # the inherited safety exclusions are incomplete. Commit the explicit
        # FAILED audit before surfacing the failure.
        mutation.commit(db)
        raise RuntimeError(
            f"historical exclusion incomplete: {response_payload['error']}"
        )
    mutation.commit(db)
    return {
        "excluded": excluded_count,
        "creative_ids": list(products_by_creative),
        "batches": len(batch_results),
    }


async def _sync_products_now(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    store_id: str,
) -> None:
    options: Dict[str, Any] = {
        "mode": "full",
        "store_id": str(store_id),
        "product_eligibility": "gmv_max",
        "advertiser_id": str(advertiser_id),
    }
    envelope = {
        "envelope_version": 1,
        "provider": context.provider,
        "scope": "products",
        "workspace_id": int(context.workspace_id),
        "auth_id": int(context.auth_id),
        "options": options,
    }
    sync_logger = logger.getChild("products")
    try:
        task = task_sync_products.apply_async(
            kwargs={
                "workspace_id": int(context.workspace_id),
                "auth_id": int(context.auth_id),
                "scope": "products",
                "params": {"envelope": envelope},
            },
            queue=TTB_SYNC_QUEUE,
        )
        result = await asyncio.to_thread(task.get, timeout=300)
        if (
            not isinstance(result, Mapping)
            or str(result.get("status") or "").strip().lower() != "success"
            or bool(result.get("errors"))
        ):
            raise RuntimeError("GMV Max product sync completed without a clean result")
    except Exception as exc:  # noqa: BLE001
        sync_logger.exception("product sync failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="GMV Max product sync failed; please retry later.",
        ) from exc


async def _ensure_products_ready(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    store_id: str,
) -> None:
    if not advertiser_id or not store_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="GMV Max product sync requires advertiser_id and store_id.",
        )

    db = context.db
    if db is None:
        return

    attempts = 0
    total, missing = _count_products(
        db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    while attempts < 2 and (total == 0 or missing > 0):
        attempts += 1
        await _sync_products_now(
            context, advertiser_id=advertiser_id, store_id=store_id
        )
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            raise
        db.expire_all()
        total, missing = _count_products(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
        )

    if total == 0 or missing > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "GMV Max products are missing eligibility data. "
                "Please run the product sync again after verifying store and advertiser binding."
            ),
        )


def _resolve_actor_label(me: SessionUser | None) -> str:
    if me is None:
        return "system"
    for candidate in (me.email, me.display_name, me.username):
        if candidate:
            return str(candidate)
    return f"user:{me.id}"


def _load_campaign_action_source(context: GMVMaxRouteContext, campaign_id: str) -> Any | None:
    advertiser_id = _normalize_identifier(context.advertiser_id)
    store_id = _normalize_identifier(context.store_id)
    if not advertiser_id or not store_id:
        return None
    for catalog_model in (GmvmaxProductCampaignCatalog, GmvmaxLiveCampaignCatalog):
        stmt = (
            select(catalog_model)
            .where(catalog_model.workspace_id == int(context.workspace_id))
            .where(catalog_model.auth_id == int(context.auth_id))
            .where(catalog_model.advertiser_id == str(advertiser_id))
            .where(catalog_model.store_id == str(store_id))
            .where(catalog_model.campaign_id == str(campaign_id))
            .order_by(catalog_model.updated_at.desc())
            .limit(1)
        )
        try:
            with context.db.begin_nested():
                row = context.db.execute(stmt).scalars().first()
        except SQLAlchemyError:
            logger.info(
                "gmvmax campaign catalog unavailable",
                exc_info=True,
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "campaign_id": str(campaign_id),
                    "catalog": catalog_model.__tablename__,
                },
            )
            continue
        if row is not None:
            return row
    return None


def _campaign_is_deleted(row: Any | None) -> bool:
    if row is None:
        return False
    lifecycle_values = {
        str(value or "").strip().upper()
        for value in (
            getattr(row, "operation_status", None),
            getattr(row, "secondary_status", None),
            getattr(row, "status", None),
        )
        if value is not None
    }
    return bool(getattr(row, "is_deleted", False)) or bool(lifecycle_values.intersection(
        {"DELETE", "DELETED", "CAMPAIGN_STATUS_DELETE", "CAMPAIGN_STATUS_DELETED"}
    ))


def _ensure_campaign_not_deleted(row: Any | None) -> None:
    if _campaign_is_deleted(row):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CAMPAIGN_DELETED",
                "message": "This campaign has been deleted on TikTok and can no longer be updated.",
            },
        )


def _snapshot_campaign_state(
    campaign: Any | None,
) -> Dict[str, Any]:
    if campaign is None:
        return {}
    return {
        "status": getattr(campaign, "operation_status", None)
        or getattr(campaign, "status", None),
        "daily_budget_cents": getattr(campaign, "budget_cents", None)
        if getattr(campaign, "budget_cents", None) is not None
        else getattr(campaign, "daily_budget_cents", None),
        "roas_bid": getattr(campaign, "roas_bid", None),
        "secondary_status": getattr(campaign, "secondary_status", None),
        "is_deleted": bool(getattr(campaign, "is_deleted", False)),
    }


def _apply_local_campaign_operation_status(
    context: "GMVMaxRouteContext",
    *,
    campaign_id: str,
    campaign: Any,
    operation_status: str,
) -> None:
    """Persist the successful remote action immediately in every local campaign view."""

    requested_status = str(operation_status or "").strip().upper()
    state_by_operation = {
        "ENABLE": ("ENABLE", "CAMPAIGN_STATUS_ENABLE", "ACTIVE", False),
        "DISABLE": ("DISABLE", "CAMPAIGN_STATUS_DISABLE", "INACTIVE", False),
        # TikTok reports deleted campaigns as disabled with a delete secondary status.
        "DELETE": ("DISABLE", "CAMPAIGN_STATUS_DELETE", "DELETED", True),
    }
    if requested_status not in state_by_operation:
        raise ValueError(f"Unsupported campaign operation status: {operation_status}")

    local_operation, secondary_status, lifecycle_status, is_deleted = state_by_operation[
        requested_status
    ]
    # Called only after TikTok accepted the mutation.  This completion time is
    # the durable authority fence that any in-flight older read must lose to.
    now_utc = catalog_observation_now()
    for target in (campaign,):
        stamp_catalog_row_observation(target, now_utc)
        if hasattr(target, "operation_status"):
            target.operation_status = local_operation
        if hasattr(target, "secondary_status"):
            target.secondary_status = secondary_status
        if hasattr(target, "modify_time_utc"):
            target.modify_time_utc = now_utc
        if hasattr(target, "ext_updated_time"):
            target.ext_updated_time = now_utc
        if hasattr(target, "status"):
            target.status = lifecycle_status
        if hasattr(target, "lifecycle_status"):
            target.lifecycle_status = lifecycle_status
        if hasattr(target, "is_deleted"):
            target.is_deleted = is_deleted
        if hasattr(target, "deleted_at"):
            target.deleted_at = now_utc if is_deleted else None

        for raw_field in ("detail_raw_json", "list_raw_json", "raw_json"):
            if not hasattr(target, raw_field):
                continue
            raw_payload = getattr(target, raw_field, None)
            if not isinstance(raw_payload, Mapping):
                continue
            updated_payload = dict(raw_payload)
            updated_payload["operation_status"] = local_operation
            updated_payload["secondary_status"] = secondary_status
            setattr(target, raw_field, updated_payload)

    # Keep the guard snapshot aligned with the canonical campaign catalog.
    # This update is best effort so an unavailable auxiliary table can never
    # block an emergency pause or delete that already succeeded remotely.
    try:
        with context.db.begin_nested():
            context.db.execute(
                text(
                    """
                    update gmv_campaign_realtime_state
                    set operation_status=:operation_status,
                        secondary_status=:secondary_status,
                        updated_at=:updated_at
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and campaign_id=:campaign_id
                    """
                ),
                {
                    "operation_status": local_operation,
                    "secondary_status": secondary_status,
                    "updated_at": now_utc,
                    "workspace_id": int(context.workspace_id),
                    "auth_id": int(context.auth_id),
                    "advertiser_id": str(context.advertiser_id),
                    "campaign_id": str(campaign_id),
                },
            )
    except SQLAlchemyError:
        logger.warning(
            "failed to align GMV Max realtime state after campaign action",
            exc_info=True,
            extra={"campaign_id": str(campaign_id)},
        )

    context.db.flush()


def _apply_local_campaign_update_body(
    context: "GMVMaxRouteContext",
    *,
    campaign: Any,
    body: GMVMaxCampaignUpdateBody,
) -> None:
    """Persist an accepted update body without an eventually-consistent readback."""

    observed_at = catalog_observation_now()
    payload = body.model_dump(exclude_none=True)
    stamp_catalog_row_observation(campaign, observed_at)
    if body.campaign_name is not None and hasattr(campaign, "campaign_name"):
        campaign.campaign_name = str(body.campaign_name)
    if body.budget is not None and hasattr(campaign, "budget_cents"):
        campaign.budget_cents = int(
            (Decimal(str(body.budget)) * Decimal("100")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    if body.roas_bid is not None and hasattr(campaign, "roas_bid"):
        campaign.roas_bid = Decimal(str(body.roas_bid))
    if body.schedule_type is not None and hasattr(campaign, "schedule_type"):
        campaign.schedule_type = str(body.schedule_type)

    timezone_name = _resolve_advertiser_timezone_name(
        context.db,
        workspace_id=int(context.workspace_id),
        auth_id=int(context.auth_id),
        advertiser_id=str(context.advertiser_id or ""),
    )
    for source_field, target_field in (
        ("schedule_start_time", "schedule_start_time_utc"),
        ("schedule_end_time", "schedule_end_time_utc"),
    ):
        value = getattr(body, source_field, None)
        if value is None or not hasattr(campaign, target_field):
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            setattr(
                campaign,
                target_field,
                parsed.astimezone(timezone.utc).replace(tzinfo=None),
            )
        except (ValueError, ZoneInfoNotFoundError):
            logger.warning(
                "failed to normalize accepted GMV Max campaign schedule",
                extra={
                    "campaign_id": getattr(campaign, "campaign_id", None),
                    "field": source_field,
                    "value": value,
                },
            )

    if hasattr(campaign, "modify_time_utc"):
        campaign.modify_time_utc = observed_at
    for raw_field in ("detail_raw_json", "list_raw_json", "raw_json"):
        if not hasattr(campaign, raw_field):
            continue
        raw_payload = getattr(campaign, raw_field, None)
        updated_payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        updated_payload.update(payload)
        setattr(campaign, raw_field, updated_payload)
    context.db.add(campaign)
    context.db.flush()


def _resolve_campaign_promotion_type(campaign: Any) -> str:
    return str(
        getattr(campaign, "promotion_type", None)
        or getattr(campaign, "gmv_max_promotion_type", None)
        or getattr(campaign, "shopping_ads_type", None)
        or "PRODUCT"
    )


def _log_action_entry(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    campaign: Any | None,
    action: str,
    actor: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    result: str,
    reason: str | None = None,
    error_message: str | None = None,
    persist_catalog_event: bool = True,
) -> None:
    db = getattr(context, "db", None)
    if db is None or campaign is None or not persist_catalog_event:
        return
    try:
        with db.begin_nested():
            advertiser_id = getattr(campaign, "advertiser_id", None) or context.advertiser_id
            store_id = getattr(campaign, "store_id", None) or context.store_id
            if not advertiser_id or not store_id:
                raise ValueError("campaign action audit requires advertiser_id and store_id")
            request_payload = {
                "manual": {"performed_by": actor},
                "before": dict(before or {}),
            }
            response_payload = {"after": dict(after or {})}
            db.execute(
                text(
                    """
                    insert into gmv_campaign_guard_events (
                        workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                        strategy_id, event_type, action, reason, result,
                        cost_cents, gross_revenue_cents, orders, roi,
                        request_json, response_json, error_message, created_at
                    ) values (
                        :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                        null, 'MANUAL_ACTION', :action, :reason, :result,
                        null, null, null, null,
                        :request_json, :response_json, :error_message, :created_at
                    )
                    """
                ),
                {
                    "workspace_id": int(context.workspace_id),
                    "auth_id": int(context.auth_id),
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(store_id),
                    "campaign_id": str(campaign_id),
                    "action": str(action),
                    "reason": reason,
                    "result": str(result),
                    "request_json": _json_dumps(request_payload),
                    "response_json": _json_dumps(response_payload),
                    "error_message": error_message,
                    "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            )
            db.flush()
    except Exception:  # noqa: BLE001
        logger.exception(
            "gmvmax campaign action log failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
                "action": action,
                "result": result,
            },
        )


def _dto_to_report_entry(row: GMVMaxMetricDTO) -> GMVMaxReportEntry:
    def _decimal_to_str(value: Any, *, quantize: str | None = None) -> str | None:
        if value is None:
            return None
        try:
            dec_value = Decimal(value)
        except Exception:  # noqa: BLE001 - defensive conversion
            return None
        if quantize:
            dec_value = dec_value.quantize(Decimal(quantize))
        return format(dec_value.normalize(), "f")

    metrics = {
        "impressions": row.impressions,
        "clicks": row.clicks,
        "cost": _decimal_to_str(row.cost, quantize="0.01"),
        "net_cost": _decimal_to_str(row.net_cost, quantize="0.01"),
        "orders": row.orders,
        "cost_per_order": _decimal_to_str(row.cost_per_order, quantize="0.0001"),
        "gross_revenue": _decimal_to_str(row.gross_revenue, quantize="0.01"),
        "roi": _decimal_to_str(row.roi, quantize="0.0001"),
        "product_impressions": row.product_impressions,
        "product_clicks": row.product_clicks,
        "product_click_rate": _decimal_to_str(row.product_click_rate, quantize="0.0001"),
        "ad_click_rate": _decimal_to_str(row.ad_click_rate, quantize="0.0001"),
        "ad_conversion_rate": _decimal_to_str(row.ad_conversion_rate, quantize="0.0001"),
        "live_views": row.live_views,
        "live_follows": row.live_follows,
    }
    serialized_metrics = {k: v for k, v in metrics.items() if v is not None}
    dimensions: Dict[str, Any] = {
        "campaign_id": row.campaign_id,
        "stat_time_day": row.stat_time_day.isoformat(),
    }
    if row.store_id:
        dimensions["store_id"] = row.store_id
    return GMVMaxReportEntry(metrics=serialized_metrics, dimensions=dimensions)


def _build_metrics_response(
    items: list[GMVMaxMetricDTO], *, total: int, page: int, page_size: int
) -> MetricsResponse:
    entries = [_dto_to_report_entry(item) for item in items]
    has_more = page_size > 0 and page * page_size < total
    total_page = ceil(total / page_size) if page_size else None
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_number=total,
        total_page=total_page,
        has_more=has_more,
        has_next=has_more,
    )
    report = GMVMaxReportData(list=entries, page_info=page_info, summary=None)
    return MetricsResponse(report=report, request_id=None)


def _build_creative_metrics_response(
    *,
    rows: Sequence[Any],
    start: date,
    end: date,
    page: int,
    page_size: int,
    total: int,
    seed_min_conversions: int,
    seed_min_roas: float,
    seed_min_spend: float,
) -> MetricsResponse:
    entries: list[GMVMaxReportEntry] = []
    for row in rows:
        raw = getattr(row, "raw_metrics", None) or {}
        creative_id = str(getattr(row, "creative_id", "") or raw.get("item_id") or "")
        if not creative_id:
            continue
        metrics_data: dict[str, Any] = dict(raw)
        metrics = {
            "title": metrics_data.get("title"),
            "item_id": metrics_data.get("item_id") or getattr(row, "item_id", None) or creative_id,
            "tt_account_authorization_type": metrics_data.get("tt_account_authorization_type"),
            "shop_content_type": metrics_data.get("shop_content_type"),
            "creative_delivery_status": canonicalize_creative_delivery_status(
                metrics_data.get("creative_delivery_status")
                or getattr(row, "creative_status", None)
            ),
            "cost": metrics_data.get("cost") or getattr(row, "cost", None),
            "orders": metrics_data.get("orders") or getattr(row, "orders", None),
            "cost_per_order": metrics_data.get("cost_per_order"),
            "gross_revenue": metrics_data.get("gross_revenue")
            or getattr(row, "gross_revenue", None),
            "roi": metrics_data.get("roi") or getattr(row, "roi", None),
            "product_impressions": metrics_data.get("product_impressions"),
            "product_clicks": metrics_data.get("product_clicks"),
            "product_click_rate": metrics_data.get("product_click_rate"),
            "ad_click_rate": metrics_data.get("ad_click_rate") or getattr(row, "ad_click_rate", None),
            "ad_conversion_rate": metrics_data.get("ad_conversion_rate")
            or getattr(row, "ad_conversion_rate", None),
            "ad_video_view_rate_2s": metrics_data.get("ad_video_view_rate_2s")
            or getattr(row, "ad_video_view_rate_2s", None),
            "ad_video_view_rate_6s": metrics_data.get("ad_video_view_rate_6s")
            or getattr(row, "ad_video_view_rate_6s", None),
            "ad_video_view_rate_p25": metrics_data.get("ad_video_view_rate_p25")
            or getattr(row, "ad_video_view_rate_p25", None),
            "ad_video_view_rate_p50": metrics_data.get("ad_video_view_rate_p50")
            or getattr(row, "ad_video_view_rate_p50", None),
            "ad_video_view_rate_p75": metrics_data.get("ad_video_view_rate_p75")
            or getattr(row, "ad_video_view_rate_p75", None),
            "ad_video_view_rate_p100": metrics_data.get("ad_video_view_rate_p100")
            or getattr(row, "ad_video_view_rate_p100", None),
        }
        serialized_metrics = {k: v for k, v in metrics.items() if v is not None}
        dimensions = {
            "campaign_id": getattr(row, "campaign_id", None),
            "item_group_id": metrics_data.get("item_group_id")
            or getattr(row, "item_group_id", None),
            "item_id": metrics.get("item_id"),
            "stat_time_day": getattr(row, "stat_time_day", None) or end,
        }
        entries.append(GMVMaxReportEntry(metrics=serialized_metrics, dimensions=dimensions))

    has_more = page_size > 0 and page * page_size < total
    total_page = ceil(total / page_size) if page_size else None
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_number=total,
        total_page=total_page,
        has_more=has_more,
        has_next=has_more,
    )
    report = GMVMaxReportData(list=entries, page_info=page_info, summary=None)
    return MetricsResponse(report=report, request_id=None)


def _normalize_metrics_list(metrics: Optional[Sequence[str]]) -> List[str]:
    """Return canonical metric names accepted by TikTok."""

    source = metrics or GMVMAX_DEFAULT_METRICS
    normalized: List[str] = []
    seen: set[str] = set()
    invalid: List[str] = []
    for metric in source:
        if not metric:
            continue
        canonical = GMVMAX_METRIC_ALIASES.get(metric, metric)
        if canonical not in GMVMAX_SUPPORTED_METRICS:
            invalid.append(metric)
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unsupported_metric",
                "message": "Unsupported GMV Max metrics provided.",
                "details": {
                    "invalid": invalid,
                    "allowed": sorted(GMVMAX_SUPPORTED_METRICS),
                },
            },
        )
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "empty_metrics",
                "message": "At least one GMV Max metric is required.",
            },
        )
    return normalized


def _normalize_dimensions_list(dimensions: Optional[Sequence[str]]) -> List[str]:
    """Return canonical dimension names accepted by TikTok."""

    source = dimensions or GMVMAX_DEFAULT_DIMENSIONS
    normalized: List[str] = []
    seen: set[str] = set()
    invalid: List[str] = []
    for dimension in source:
        if not dimension:
            continue
        canonical = dimension
        if canonical not in GMVMAX_SUPPORTED_DIMENSIONS:
            invalid.append(dimension)
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unsupported_dimension",
                "message": "Unsupported GMV Max dimensions provided.",
                "details": {
                    "invalid": invalid,
                    "allowed": sorted(GMVMAX_SUPPORTED_DIMENSIONS),
                },
            },
        )
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "empty_dimensions",
                "message": "At least one GMV Max dimension is required.",
            },
        )
    return normalized


def _resolve_report_level(level: Any) -> GMVMaxReportLevel:
    if level is None:
        return GMVMaxReportLevel.CAMPAIGN
    try:
        return GMVMaxReportLevel(str(level))
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "unsupported_level", "message": "Unsupported GMV Max report level."},
        ) from exc


def _validate_date_range_for_level(
    *, start_date: date, end_date: date, level: GMVMaxReportLevel
) -> None:
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_date_range",
                "message": "end_date must not be earlier than start_date.",
            },
        )

    config = GMV_REPORT_CONFIG.get(level)
    if not config:
        return
    max_range = config.get("max_range")
    if max_range and (end_date - start_date) > max_range:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "range_too_large",
                "message": (
                    f"{level.value} reports support up to {max_range.days} days for the selected granularity"
                ),
            },
        )


@dataclass(slots=True)
class GMVMaxRouteContext:
    """Per-request context containing the TikTok client and binding metadata."""

    workspace_id: int
    provider: str
    auth_id: int
    advertiser_id: Optional[str]
    store_id: Optional[str]
    binding: GMVMaxAccountBinding
    client: TikTokBusinessGMVMaxClient
    db: Session


async def _handle_tiktok_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, TTBApiError):
        detail: Dict[str, Any] = {
            "code": "tiktok_error",
            "message": str(exc),
            "details": {},
        }
        if exc.code is not None:
            detail["details"]["code"] = exc.code
        if exc.payload is not None:
            detail["details"]["payload"] = exc.payload
        upstream_status = (
            int(exc.status)
            if exc.status is not None and int(exc.status) >= 400
            else None
        )
        raise HTTPException(
            status_code=upstream_status
            or (status.HTTP_400_BAD_REQUEST if isinstance(exc, TTBBusinessError) else status.HTTP_502_BAD_GATEWAY),
            detail=detail,
        ) from exc
    if isinstance(exc, TTBHttpError):
        detail = {
            "code": "tiktok_http_error",
            "message": str(exc),
            "details": {"status": exc.status},
        }
        if exc.payload is not None:
            detail["details"]["payload"] = exc.payload
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "tiktok_error", "message": str(exc)},
    ) from exc


def _extract_request_payload(args: Sequence[Any], kwargs: Dict[str, Any]) -> Any:
    for candidate in list(args) + list(kwargs.values()):
        if hasattr(candidate, "model_dump"):
            try:
                return candidate.model_dump(exclude_none=True)
            except Exception:  # pragma: no cover - defensive
                return str(candidate)
        if isinstance(candidate, dict):
            return candidate
    return None


async def _call_tiktok(
    func: Callable[..., Awaitable[GMVMaxResponse[Any]]],
    *args: Any,
    _log_context: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> GMVMaxResponse[Any]:
    try:
        return await func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        payload = _extract_request_payload(args, kwargs)
        endpoint_name = getattr(func, "__name__", repr(func))
        extra: Dict[str, Any] = {
            "gmvmax_endpoint": endpoint_name,
            "gmvmax_request_payload": payload,
        }
        if _log_context is not None:
            extra["gmvmax_context"] = _log_context
        logger.warning(
            "tiktok gmv max request failed",
            exc_info=True,
            extra=extra,
        )
        await _handle_tiktok_error(exc)


def _normalize_status(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _should_include_campaign(entry: GMVMaxCampaign) -> bool:
    lifecycle_status, is_deleted = _derive_campaign_lifecycle(
        _normalize_status(entry.operation_status), _normalize_status(entry.secondary_status)
    )
    return lifecycle_status != "DELETED" and not is_deleted


def _filter_campaign_entries(entries: Optional[Sequence[GMVMaxCampaign | Dict[str, Any]]]) -> List[GMVMaxCampaign]:
    if not entries:
        return []
    filtered: List[GMVMaxCampaign] = []
    for entry in entries:
        campaign: Optional[GMVMaxCampaign]
        if isinstance(entry, GMVMaxCampaign):
            campaign = entry
        elif isinstance(entry, dict):
            try:
                campaign = GMVMaxCampaign.model_validate(entry)
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        if _should_include_campaign(campaign):
            filtered.append(campaign)
    return filtered


async def _refresh_campaign_snapshot(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    campaign_id: str,
    store_hint: Optional[str] = None,
) -> None:
    """Fetch the latest campaign info and persist it locally."""

    db = getattr(context, "db", None)
    if db is None:
        return
    mutation_lease = active_gmvmax_mutation_lease(db)
    source_observed_at = catalog_observation_now()
    try:
        if mutation_lease is not None:
            mutation_lease.assert_current(db)
        response = await _call_tiktok(
            context.client.gmv_max_campaign_info,
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
            ),
        )
        if mutation_lease is not None:
            mutation_lease.assert_current(db)
    except HTTPException:
        logger.warning(
            "gmvmax campaign refresh failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
            },
            exc_info=True,
        )
        return

    try:
        info_payload = response.data.model_dump(exclude_none=False)
        upsert_campaign_from_api(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            advertiser_id=str(advertiser_id),
            payload=info_payload,
            store_id_hint=store_hint or context.store_id,
            campaign_details=info_payload,
            campaign_details_complete=True,
            source_observed_at=source_observed_at,
        )
        db.flush()
    except Exception:  # noqa: BLE001
        logger.warning(
            "failed to persist refreshed gmvmax campaign",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
            },
            exc_info=True,
        )


def _normalize_store_ids(
    candidate: Optional[Sequence[str]],
    fallback: Optional[str],
) -> List[str]:
    if candidate and len(candidate) > 0:
        return [str(item) for item in candidate if item]
    if fallback:
        return [str(fallback)]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "missing_store", "message": "store_id is required for this operation"},
    )


def _normalize_date_value(value: Any, *, field_name: str) -> Optional[date]:
    normalized = normalize_datetime_to_date(value)
    if normalized is None:
        return None
    if isinstance(normalized, date) and not isinstance(normalized, datetime):
        return normalized
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "invalid_date_format",
            "message": f"{field_name} must be a valid date (YYYY-MM-DD)",
        },
    )


def _resolve_advertiser_today(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None,
) -> date:
    if not advertiser_id:
        return date.today()

    row = (
        db.query(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .filter(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .first()
    )
    tz_name = None
    if row:
        tz_name = row.display_timezone or row.timezone
    if not tz_name:
        return date.today()
    try:
        return datetime.now(ZoneInfo(str(tz_name))).date()
    except ZoneInfoNotFoundError:
        logger.warning(
            "invalid advertiser timezone; falling back to server date",
            extra={"advertiser_id": advertiser_id, "timezone": tz_name},
        )
        return date.today()


def _resolve_advertiser_timezone_name(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None,
) -> str:
    if not advertiser_id:
        return "UTC"
    row = (
        db.query(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .filter(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .first()
    )
    timezone_name = (row.display_timezone or row.timezone) if row else None
    if not timezone_name:
        return "UTC"
    try:
        ZoneInfo(str(timezone_name))
        return str(timezone_name)
    except ZoneInfoNotFoundError:
        return "UTC"


def _source_age_seconds(db: Session, value: datetime | None) -> int | None:
    """Calculate age for legacy timestamps written in either UTC or DB local time."""

    if value is None:
        return None
    source = value.replace(tzinfo=None)
    cached = db.info.get("gmv_metrics_clock_pair")
    if cached and monotonic() - float(cached[0]) < 5:
        clocks = cached[1]
    else:
        if db.bind is not None and db.bind.dialect.name == "mysql":
            clocks = db.execute(
                text(
                    "select utc_timestamp(6) as utc_now, "
                    "current_timestamp(6) as local_now"
                )
            ).mappings().first() or {}
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            clocks = {"utc_now": now, "local_now": now}
        db.info["gmv_metrics_clock_pair"] = (monotonic(), clocks)
    candidates: list[int] = []
    for key in ("utc_now", "local_now"):
        clock = clocks.get(key)
        if not isinstance(clock, datetime):
            continue
        delta = int((clock.replace(tzinfo=None) - source).total_seconds())
        if delta >= -5:
            candidates.append(max(0, delta))
    return min(candidates) if candidates else None


def _metrics_freshness(
    *,
    source: str,
    age_seconds: int | None,
    row_count: int,
    max_age_seconds: int,
    advertiser_timezone: str,
    start_date: date,
    end_date: date,
    advertiser_today: date,
) -> MetricsFreshness:
    historical = end_date < advertiser_today
    if row_count <= 0:
        state = "missing"
        message = "No persisted TikTok report is available for this range."
    elif historical:
        state = "historical"
        message = "Historical completed-day data."
    elif age_seconds is None or age_seconds > max_age_seconds:
        state = "stale"
        message = "Data is older than the allowed decision latency."
    else:
        state = "fresh"
        message = "Data is within the allowed latency."
    last_synced_at = None
    if age_seconds is not None:
        last_synced_at = datetime.now(timezone.utc) - timedelta(seconds=max(0, age_seconds))
    return MetricsFreshness(
        state=state,
        source=source,
        last_synced_at=last_synced_at,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        row_count=max(0, int(row_count or 0)),
        advertiser_timezone=advertiser_timezone,
        start_date=start_date,
        end_date=end_date,
        is_realtime=False,
        message=message,
    )

def _parse_session_settings(
    settings: Optional[Dict[str, Any]]
) -> Optional[GMVMaxSessionSettings]:
    if settings is None:
        return None
    return GMVMaxSessionSettings.model_validate(settings)


def _build_campaign_update_body(
    campaign_id: str,
    action_type: str,
    payload: Dict[str, Any],
) -> GMVMaxCampaignUpdateBody:
    body_payload: Dict[str, Any] = {"campaign_id": str(campaign_id)}
    if action_type == "update_budget":
        budget = payload.get("budget")
        if budget is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="payload.budget is required for update_budget",
            )
        body_payload["budget"] = float(budget)
    elif action_type == "update_strategy":
        if "roas_bid" in payload:
            body_payload["roas_bid"] = float(payload["roas_bid"])
        if "promotion_days" in payload:
            body_payload["promotion_days"] = payload["promotion_days"]
    return GMVMaxCampaignUpdateBody(**body_payload)


def _build_session_update_body(
    campaign_id: str,
    payload: Dict[str, Any],
    default_store_id: Optional[str],
) -> GMVMaxSessionUpdateBody:
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="payload.session_id is required for update_strategy",
        )
    store_id = payload.get("store_id") or default_store_id
    if store_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required for session updates"},
        )
    session_settings = _parse_session_settings(payload.get("session"))
    return GMVMaxSessionUpdateBody(
        campaign_id=str(campaign_id),
        session_id=str(session_id),
        store_id=str(store_id),
        session=session_settings,
    )


def _get_local_strategy_config(
    context: GMVMaxRouteContext,
    campaign_id: str,
) -> GmvStrategyConfig | None:
    stmt = (
        select(GmvStrategyConfig)
        .where(GmvStrategyConfig.workspace_id == int(context.workspace_id))
        .where(GmvStrategyConfig.auth_id == int(context.auth_id))
        .where(GmvStrategyConfig.campaign_id == str(campaign_id))
    )
    return context.db.execute(stmt).scalars().first()


def _ensure_campaign_not_creation_quarantined(
    context: GMVMaxRouteContext,
    campaign_id: str,
) -> None:
    advertiser_id = _normalize_identifier(context.advertiser_id)
    store_id = _normalize_identifier(context.store_id)
    unfinished_intent = None
    if advertiser_id and store_id:
        unfinished_intent = (
            context.db.query(GmvmaxCampaignCreateIntent)
            .filter(
                GmvmaxCampaignCreateIntent.workspace_id == int(context.workspace_id),
                GmvmaxCampaignCreateIntent.auth_id == int(context.auth_id),
                GmvmaxCampaignCreateIntent.advertiser_id == str(advertiser_id),
                GmvmaxCampaignCreateIntent.store_id == str(store_id),
                or_(
                    GmvmaxCampaignCreateIntent.campaign_id == str(campaign_id),
                    GmvmaxCampaignCreateIntent.replacement_campaign_id
                    == str(campaign_id),
                ),
                GmvmaxCampaignCreateIntent.state != "SUCCEEDED",
            )
            .order_by(GmvmaxCampaignCreateIntent.id.desc())
            .first()
        )
    strategy = _get_local_strategy_config(context, str(campaign_id))
    config = (
        dict(strategy.config_json or {})
        if strategy is not None and isinstance(strategy.config_json, Mapping)
        else {}
    )
    quarantine = config.get("creation_quarantine")
    if (
        unfinished_intent is None
        and (
            not isinstance(quarantine, Mapping)
            or not quarantine.get("enabled")
        )
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "GMVMAX_CREATE_QUARANTINED",
            "message": (
                "This campaign is paused because creation finalization is "
                "incomplete. Resume the original create request; ordinary "
                "strategy or enable actions cannot bypass quarantine."
            ),
            "campaign_id": str(campaign_id),
            "creation_state": (
                str(unfinished_intent.state)
                if unfinished_intent is not None
                else "QUARANTINED"
            ),
        },
    )


def _get_or_create_local_strategy_config(
    context: GMVMaxRouteContext,
    campaign_id: str,
) -> GmvStrategyConfig:
    row = _get_local_strategy_config(context, campaign_id)
    if row is None:
        row = GmvStrategyConfig(
            workspace_id=int(context.workspace_id),
            auth_id=int(context.auth_id),
            campaign_id=str(campaign_id),
            enabled=False,
            cooldown_minutes=30,
            min_runtime_minutes_before_first_change=30,
            config_json={
                "smart_guard": {
                    "enabled": False,
                    "monitor_interval_minutes": 3,
                    "evaluation_window_minutes": 60,
                    "pause_cooldown_minutes": 30,
                    "min_spend_cents": 300,
                    "use_effective_product_price": True,
                    "daily_spend_cap_enabled": True,
                    "daily_spend_cap_cents": None,
                    "daily_budget_pacing": True,
                    "catastrophic_pause_cooldown_minutes": 120,
                    "disable_strategy_on_catastrophic_stop": False,
                    "hermes_enabled": False,
                }
            },
        )
        context.db.add(row)
        context.db.flush()
    return row


def _serialize_strategy_config(row: GmvStrategyConfig | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "enabled": bool(row.enabled),
        "target_roi": float(row.target_roi) if row.target_roi is not None else None,
        "min_roi": float(row.min_roi) if row.min_roi is not None else None,
        "max_roi": float(row.max_roi) if row.max_roi is not None else None,
        "min_impressions": row.min_impressions,
        "min_clicks": row.min_clicks,
        "max_budget_raise_pct_per_day": float(row.max_budget_raise_pct_per_day)
        if row.max_budget_raise_pct_per_day is not None
        else None,
        "max_budget_cut_pct_per_day": float(row.max_budget_cut_pct_per_day)
        if row.max_budget_cut_pct_per_day is not None
        else None,
        "max_roas_step_per_adjust": float(row.max_roas_step_per_adjust)
        if row.max_roas_step_per_adjust is not None
        else None,
        "cooldown_minutes": row.cooldown_minutes,
        "min_runtime_minutes_before_first_change": row.min_runtime_minutes_before_first_change,
        "config_json": row.config_json or {},
    }


def _creative_asset_payload_from_cache(row: Mapping[str, Any]) -> dict[str, Any]:
    media = creative_media_urls(row)
    return {
        "item_id": row.get("item_id"),
        "creative_id": row.get("item_id"),
        "shop_content_id": row.get("item_id"),
        "video_id": row.get("video_id"),
        "title": row.get("title"),
        "creative_name": row.get("title"),
        "preview_url": media["preview_url"],
        "video_cover_url": media["video_cover_url"],
        "thumbnail_url": media["video_cover_url"],
        "media_cache_status": row.get("media_cache_status"),
        "duration": float(row["duration"]) if row.get("duration") is not None else None,
        "identity_id": row.get("identity_id"),
        "identity_type": row.get("identity_type"),
        "identity_name": row.get("identity_name"),
    }


def _money_from_cents(value: Any) -> float:
    try:
        return round(float(value or 0) / 100.0, 4)
    except (TypeError, ValueError):
        return 0.0


def _asset_score(row: Mapping[str, Any]) -> float:
    cost = float(row.get("cost_cents") or 0)
    gross = float(row.get("gross_revenue_cents") or 0)
    orders = float(row.get("orders") or 0)
    clicks = float(row.get("clicks") or row.get("product_clicks") or 0)
    impressions = float(row.get("impressions") or row.get("product_impressions") or 0)
    roi = gross / cost if cost > 0 else 0.0
    ctr = clicks / impressions if impressions > 0 else 0.0
    # Conservative ranking: paid proof first, then order proof, then engagement.
    return round((roi * 55.0) + (orders * 18.0) + min(cost / 1000.0, 12.0) + min(ctr * 100.0, 10.0), 4)


def _creative_asset_candidate_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    cost_cents = int(row.get("cost_cents") or 0)
    gross_cents = int(row.get("gross_revenue_cents") or 0)
    orders = int(row.get("orders") or 0)
    clicks = int(row.get("clicks") or row.get("product_clicks") or 0)
    impressions = int(row.get("impressions") or row.get("product_impressions") or 0)
    roi = round(gross_cents / cost_cents, 4) if cost_cents > 0 else 0.0
    cache_active = str(row.get("cache_active", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
    }
    partition_active = str(
        row.get("partition_active", "true")
    ).strip().lower() not in {"0", "false", "no"}
    selectable = bool(
        cache_active
        and partition_active
        and row.get("item_id")
        and str(row.get("item_id")) not in {"-1", "0"}
        and row.get("video_id")
        and row.get("identity_id")
        and row.get("identity_type")
    )
    if not cache_active:
        not_selectable_reason = "TikTok 当前完整素材列表已不再返回该视频。"
    elif not partition_active:
        not_selectable_reason = "TikTok 当前已不再返回该视频与所选商品的关联。"
    elif not selectable:
        not_selectable_reason = "缺少 TikTok 视频 ID 或授权身份，暂不能用于手动投放。"
    else:
        not_selectable_reason = None
    media = creative_media_urls(row)
    return {
        "source": "TIKTOK_VIDEO_GET",
        "selectable": selectable,
        "not_selectable_reason": not_selectable_reason,
        "item_id": row.get("item_id"),
        "creative_id": row.get("item_id"),
        "video_id": row.get("video_id"),
        "item_group_id": row.get("item_group_id"),
        "title": row.get("title") or row.get("item_id"),
        "preview_url": media["preview_url"],
        "video_cover_url": media["video_cover_url"],
        "thumbnail_url": media["video_cover_url"],
        "media_cache_status": row.get("media_cache_status"),
        "duration": float(row["duration"]) if row.get("duration") is not None else None,
        "identity_info": {
            "identity_id": row.get("identity_id"),
            "identity_type": row.get("identity_type"),
            "identity_authorized_bc_id": row.get("identity_authorized_bc_id"),
            "identity_authorized_shop_id": row.get("identity_authorized_shop_id"),
            "store_id": row.get("store_id"),
        },
        "identity_name": row.get("identity_name"),
        "can_change_anchor": str(row.get("can_change_anchor") or "").lower() == "true",
        "metrics": {
            "spend": _money_from_cents(cost_cents),
            "gmv": _money_from_cents(gross_cents),
            "orders": orders,
            "roi": roi,
            "clicks": clicks,
            "impressions": impressions,
            "ctr": round(clicks / impressions, 4) if impressions > 0 else 0.0,
            "ad_video_view_rate_2s": float(row.get("ad_video_view_rate_2s") or 0),
            "ad_video_view_rate_6s": float(row.get("ad_video_view_rate_6s") or 0),
            "ad_video_view_rate_p100": float(row.get("ad_video_view_rate_p100") or 0),
        },
        "score": _asset_score(row),
        "fetched_at": row.get("fetched_at"),
        "updated_at": row.get("updated_at"),
    }


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, Mapping):
                return dict(parsed)
        except json.JSONDecodeError:
            return {}
    return {}


def _model_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _first_video_material(response: Any) -> dict[str, Any]:
    data = getattr(response, "data", None)
    for attr in ("list", "videos"):
        items = getattr(data, attr, None)
        if isinstance(items, list) and items:
            return _model_to_mapping(items[0])
    return {}


def _manual_upload_candidate_from_row(
    row: Mapping[str, Any],
    *,
    signed_file_url: str | None = None,
) -> dict[str, Any]:
    raw = _json_mapping(row.get("raw_json"))
    public_url = (
        signed_file_url
        or row.get("public_url")
        or raw.get("public_url")
        or raw.get("file_url")
    )
    tiktok_upload = _json_mapping(raw.get("tiktok_upload"))
    ad_video_search = _json_mapping(raw.get("ad_video_search"))
    upload_error = row.get("upload_error") or raw.get("upload_error")
    upload_status = str(row.get("upload_status") or "")
    identity_info = _json_mapping(row.get("identity_info_json")) or _json_mapping(raw.get("identity_info"))
    preview_url = (
        row.get("tiktok_preview_url")
        or ad_video_search.get("preview_url")
        or tiktok_upload.get("preview_url")
        or public_url
    )
    cover_url = (
        row.get("tiktok_video_cover_url")
        or ad_video_search.get("video_cover_url")
        or ad_video_search.get("cover_url")
        or tiktok_upload.get("video_cover_url")
        or tiktok_upload.get("cover_url")
    )
    return {
        "source": "LOCAL_UPLOAD",
        "selectable": bool(
            row.get("tiktok_item_id")
            and row.get("tiktok_video_id")
            and row.get("identity_id")
            and row.get("identity_type")
            and (not row.get("item_group_id") or row.get("anchor_status") == "LINKED")
        ),
        "not_selectable_reason": None
        if (
            row.get("tiktok_item_id")
            and row.get("tiktok_video_id")
            and row.get("identity_id")
            and row.get("identity_type")
            and (not row.get("item_group_id") or row.get("anchor_status") == "LINKED")
        )
        else (
            f"TikTok 发布失败：{upload_error}"
            if upload_error
            else "该视频由旧版广告素材库接口上传，无法作为 GMV Max 帖子使用；请重新选择 TikTok 账号发布。"
            if upload_status == "LEGACY_AD_LIBRARY"
            else "视频已发布，正在关联所选商品。"
            if row.get("item_group_id") and row.get("tiktok_item_id")
            else "视频正在由所选 TikTok 账号发布，完成后会自动进入 GMV Max 素材池。"
        ),
        "upload_id": row.get("upload_id"),
        "item_id": row.get("tiktok_item_id") or f"local:{row.get('upload_id')}",
        "creative_id": row.get("tiktok_item_id") or f"local:{row.get('upload_id')}",
        "video_id": row.get("tiktok_video_id"),
        "material_id": row.get("tiktok_material_id"),
        "item_group_id": row.get("item_group_id"),
        "title": row.get("title") or row.get("file_name") or "上传视频",
        "preview_url": preview_url,
        "file_url": public_url,
        "video_cover_url": cover_url,
        "thumbnail_url": cover_url,
        "duration": None,
        "identity_info": identity_info or None,
        "identity_name": raw.get("tiktok_account_alias") or raw.get("identity_name"),
        "tiktok_account_id": row.get("tiktok_account_id"),
        "tiktok_business_id": row.get("tiktok_business_id"),
        "publish_id": row.get("publish_id"),
        "anchor_status": row.get("anchor_status"),
        "upload_status": upload_status,
        "upload_error": upload_error,
        "file_name": row.get("file_name"),
        "file_size": int(row.get("file_size") or 0),
        "metrics": {
            "spend": 0,
            "gmv": 0,
            "orders": 0,
            "roi": 0,
            "clicks": 0,
            "impressions": 0,
            "ctr": 0,
        },
        "score": 0,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _tiktok_account_can_publish(account: OAuthTikTokAccount) -> bool:
    scope_json = account.scope_json if isinstance(account.scope_json, Mapping) else {}
    raw_scope = scope_json.get("value") or scope_json.get("items") or ""
    if isinstance(raw_scope, Sequence) and not isinstance(raw_scope, str):
        scopes = {str(value).strip() for value in raw_scope if str(value).strip()}
    else:
        scopes = {value.strip() for value in str(raw_scope).split(",") if value.strip()}
    return "video.publish" in scopes


def _gmvmax_video_items(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    values: list[Any] = []
    for field_name in ("item_list", "video_list"):
        field_value = getattr(data, field_name, None)
        if isinstance(field_value, list):
            values.extend(field_value)
    return [_model_to_mapping(value) for value in values]


async def _refresh_pending_manual_uploads(
    context: GMVMaxRouteContext,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> dict[str, int]:
    """Advance account-published videos into the GMV Max selectable pool."""

    rows = context.db.execute(
        text(
            """
            select *
            from gmvmax_manual_creative_uploads
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and upload_status in (
                'TIKTOK_PUBLISHING', 'PUBLISHED_WAITING_GMV', 'LINKING_PRODUCT'
              )
            order by updated_at asc, id asc
            limit 12
            """
        ),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": advertiser_id,
            "store_id": store_id,
        },
    ).mappings().all()
    summary = {"checked": 0, "ready": 0, "failed": 0, "pending": 0}
    if not rows:
        return summary

    store_authorized_bc_id = (
        context.binding.bc_id
        or resolve_creative_asset_store_authorized_bc_id(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
        )
    )
    for row in rows:
        summary["checked"] += 1
        refresh_state = _json_mapping(row.get("raw_json"))
        try:
            refresh_attempts = int(refresh_state.get("refresh_attempts") or 0) + 1
        except (TypeError, ValueError):
            refresh_attempts = 1
        refresh_state.update(
            {
                "refresh_attempts": refresh_attempts,
                "last_refresh_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        # Every selected row advances before any pending branch can continue.
        # This moves it behind untouched rows in the oldest-first LIMIT window
        # and guarantees a finite pending set rotates instead of poisoning the
        # first twelve positions forever.
        context.db.execute(
            text(
                """
                update gmvmax_manual_creative_uploads
                set raw_json=cast(:raw_json as json),
                    updated_at=current_timestamp(6)
                where id=:id
                """
            ),
            {
                "id": int(row["id"]),
                "raw_json": json.dumps(refresh_state, ensure_ascii=False),
            },
        )
        account_client: TikTokBusinessGMVMaxClient | None = None
        try:
            token, account, _ = await get_fresh_tiktok_account_token_plain(
                context.db, int(row.get("tiktok_account_id") or 0)
            )
            if int(account.workspace_id) != int(workspace_id) or account.status != "active":
                raise ValueError("所选 TikTok 账号授权已失效")
            account_client = TikTokBusinessGMVMaxClient(access_token=token)
            item_id = _normalize_identifier(row.get("tiktok_item_id"))
            publish_status = str(row.get("upload_status") or "")
            if not item_id:
                publish_id = _normalize_identifier(row.get("publish_id"))
                if not publish_id:
                    raise ValueError("缺少 TikTok publish_id")
                status_response = await account_client.business_publish_status(
                    TikTokAccountPublishStatusRequest(
                        business_id=str(row.get("tiktok_business_id") or account.open_id),
                        publish_id=publish_id,
                    )
                )
                status_data = status_response.data
                remote_status = str(getattr(status_data, "status", "") or "")
                if remote_status == "FAILED":
                    raise ValueError(getattr(status_data, "reason", None) or "TikTok 发布失败")
                post_ids = getattr(status_data, "post_ids", None) or []
                if remote_status != "PUBLISH_COMPLETE" or not post_ids:
                    summary["pending"] += 1
                    continue
                item_id = str(post_ids[0])
                publish_status = "PUBLISHED_WAITING_GMV"
                context.db.execute(
                    text(
                        """
                        update gmvmax_manual_creative_uploads
                        set tiktok_item_id=:item_id,
                            upload_status='PUBLISHED_WAITING_GMV',
                            updated_at=current_timestamp(6)
                        where id=:id
                        """
                    ),
                    {"item_id": item_id, "id": int(row["id"])},
                )

            if not store_authorized_bc_id:
                summary["pending"] += 1
                continue
            product_id = _normalize_identifier(row.get("item_group_id"))
            video = None
            async for entry in iter_gmvmax_video_entries(
                context.client,
                advertiser_id=advertiser_id,
                store_id=store_id,
                store_authorized_bc_id=str(store_authorized_bc_id),
                identities=None,
                item_group_ids=[product_id] if product_id else None,
                keyword=item_id,
                custom_posts_eligible=True if product_id else None,
            ):
                candidate = _model_to_mapping(entry)
                if str(candidate.get("item_id")) == item_id:
                    video = candidate
                    break
            if not video:
                summary["pending"] += 1
                continue
            identity_info = _json_mapping(video.get("identity_info"))
            video_info = _json_mapping(video.get("video_info"))
            identity_id = _normalize_identifier(identity_info.get("identity_id"))
            identity_type = _normalize_identifier(identity_info.get("identity_type"))
            if not identity_id or not identity_type:
                summary["pending"] += 1
                continue

            anchor_status = "NOT_REQUESTED"
            if product_id:
                if video.get("can_change_anchor") is False:
                    raise ValueError("该 TikTok 视频不允许添加或更换商品链接")
                anchor_response = await context.client.gmv_max_shop_custom_anchor_create(
                    GMVMaxShopCustomAnchorCreateRequest(
                        advertiser_id=advertiser_id,
                        store_id=store_id,
                        store_authorized_bc_id=str(store_authorized_bc_id),
                        custom_anchor_video_list=[
                            {
                                "item_id": item_id,
                                "identity_info": identity_info,
                                "spu_id_list": [product_id],
                            }
                        ],
                    )
                )
                failures = getattr(anchor_response.data, "failure_list", None) or []
                if failures:
                    failure = _model_to_mapping(failures[0])
                    reason_code = str(failure.get("reason") or "")
                    if reason_code != "NATIVE_ANCHOR_EXISTS":
                        reason = failure.get("error_message") or reason_code or "商品关联失败"
                        raise ValueError(str(reason))
                anchor_status = "LINKED"

            context.db.execute(
                text(
                    """
                    update gmvmax_manual_creative_uploads
                    set upload_status='READY',
                        tiktok_video_id=:video_id,
                        tiktok_preview_url=:preview_url,
                        tiktok_video_cover_url=:cover_url,
                        identity_id=:identity_id,
                        identity_type=:identity_type,
                        identity_info_json=cast(:identity_info as json),
                        anchor_status=:anchor_status,
                        upload_error=null,
                        updated_at=current_timestamp(6)
                    where id=:id
                    """
                ),
                {
                    "id": int(row["id"]),
                    "video_id": _normalize_identifier(video_info.get("video_id")) or item_id,
                    "preview_url": video_info.get("preview_url"),
                    "cover_url": video_info.get("video_cover_url"),
                    "identity_id": identity_id,
                    "identity_type": identity_type,
                    "identity_info": json.dumps(identity_info, ensure_ascii=False),
                    "anchor_status": anchor_status,
                },
            )
            summary["ready"] += 1
        except ValueError as exc:
            logger.warning(
                "Failed to advance TikTok account video upload",
                extra={"upload_id": row.get("upload_id"), "error": str(exc)},
            )
            context.db.execute(
                text(
                    """
                    update gmvmax_manual_creative_uploads
                    set upload_status='TIKTOK_PUBLISH_FAILED',
                        upload_error=:error,
                        updated_at=current_timestamp(6)
                    where id=:id
                    """
                ),
                {"id": int(row["id"]), "error": str(exc)[:2000]},
            )
            summary["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TikTok account video upload remains pending after transient error",
                extra={"upload_id": row.get("upload_id"), "error": str(exc)},
            )
            context.db.execute(
                text(
                    """
                    update gmvmax_manual_creative_uploads
                    set upload_error=:error,
                        updated_at=current_timestamp(6)
                    where id=:id
                    """
                ),
                {"id": int(row["id"]), "error": str(exc)[:2000]},
            )
            summary["pending"] += 1
        finally:
            if account_client is not None:
                try:
                    await account_client.aclose()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to close TikTok account upload client",
                        extra={"upload_id": row.get("upload_id")},
                        exc_info=True,
                    )
    context.db.commit()
    return summary


def _safe_json_column(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            return json.loads(text_value)
        except json.JSONDecodeError:
            return value
    return value


def _serialize_hermes_daily_report(row: Mapping[str, Any]) -> dict[str, Any]:
    report_date = row.get("report_date")
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    return {
        "id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "auth_id": row.get("auth_id"),
        "advertiser_id": row.get("advertiser_id"),
        "store_id": row.get("store_id"),
        "report_date": report_date.isoformat() if hasattr(report_date, "isoformat") else report_date,
        "advertiser_timezone": row.get("advertiser_timezone"),
        "report_type": row.get("report_type"),
        "status": row.get("status"),
        "input": _safe_json_column(row.get("input_json")),
        "response": _safe_json_column(row.get("response_json")),
        "report_markdown": row.get("report_markdown") or "",
        "recommendation": _safe_json_column(row.get("recommendation_json")) or {},
        "hermes_response_id": row.get("hermes_response_id"),
        "usage": {
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "total_tokens": row.get("total_tokens"),
        },
        "error_message": row.get("error_message"),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def _apply_local_strategy_update(
    row: GmvStrategyConfig,
    payload: StrategyUpdateRequest,
) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    for field_name in {
        "enabled",
        "target_roi",
        "min_roi",
        "max_roi",
        "min_impressions",
        "min_clicks",
        "max_budget_raise_pct_per_day",
        "max_budget_cut_pct_per_day",
        "max_roas_step_per_adjust",
        "cooldown_minutes",
        "min_runtime_minutes_before_first_change",
    }:
        if field_name in data:
            setattr(row, field_name, data[field_name])

    config = dict(row.config_json or {})
    incoming_config = dict(data.get("config_json") or {})
    for key, value in incoming_config.items():
        if key not in {"smart_guard", "creative_guard"} or not isinstance(value, Mapping):
            config[key] = value
            continue
        merged_section = dict(config.get(key) or {})
        incoming_section = dict(value)
        if isinstance(incoming_section.get("product_effective_prices"), Mapping):
            merged_prices = dict(merged_section.get("product_effective_prices") or {})
            merged_prices.update(dict(incoming_section.get("product_effective_prices") or {}))
            incoming_section["product_effective_prices"] = merged_prices
        merged_section.update(incoming_section)
        config[key] = merged_section
    smart_guard = dict(config.get("smart_guard") or {})
    if data.get("auto_pause_roi_threshold") is not None:
        smart_guard["min_roi"] = data["auto_pause_roi_threshold"]
    elif data.get("min_roi") is not None:
        smart_guard.setdefault("min_roi", data["min_roi"])
    if data.get("cooldown_minutes") is not None:
        smart_guard["pause_cooldown_minutes"] = data["cooldown_minutes"]
    if "auto_heating_enabled" in data:
        config["auto_heating_enabled"] = bool(data["auto_heating_enabled"])
    smart_guard.setdefault("enabled", bool(row.enabled))
    smart_guard["enabled"] = bool(row.enabled)
    smart_guard.setdefault("monitor_interval_minutes", 3)
    smart_guard.setdefault("evaluation_window_minutes", 60)
    smart_guard.setdefault("pause_cooldown_minutes", row.cooldown_minutes or 30)
    smart_guard.setdefault("min_spend_cents", 300)
    smart_guard.setdefault("use_effective_product_price", True)
    smart_guard.setdefault("daily_spend_cap_enabled", True)
    smart_guard.setdefault("daily_spend_cap_cents", None)
    smart_guard.setdefault("daily_budget_pacing", True)
    smart_guard.setdefault("hermes_enabled", bool(config.get("hermes_enabled", False)))
    config["smart_guard"] = smart_guard
    creative_guard = dict(config.get("creative_guard") or {})
    effective_prices: dict[str, Any] = {}
    for section in (smart_guard, creative_guard):
        prices = section.get("product_effective_prices")
        if isinstance(prices, Mapping):
            effective_prices.update({str(key): value for key, value in prices.items()})
    if effective_prices:
        smart_guard["product_effective_prices"] = dict(effective_prices)
        creative_guard["product_effective_prices"] = dict(effective_prices)
        config["creative_guard"] = creative_guard
    row.config_json = config
    return _serialize_strategy_config(row)


def _disable_local_automation_strategy(
    context: GMVMaxRouteContext,
    campaign_id: str,
) -> dict[str, Any]:
    """Stop local automation as part of one operator shutdown intent."""

    strategy = _get_or_create_local_strategy_config(context, str(campaign_id))
    payload = _apply_local_strategy_update(
        strategy,
        StrategyUpdateRequest(enabled=False),
    )
    context.db.add(strategy)
    context.db.flush()
    return payload


def _persist_strategy_effective_prices(
    context: GMVMaxRouteContext,
    payload: StrategyUpdateRequest,
) -> int:
    data = payload.model_dump(exclude_unset=True)
    incoming_config = data.get("config_json") or {}
    if not isinstance(incoming_config, Mapping):
        return 0
    prices: dict[str, Decimal] = {}
    for section_name in ("smart_guard", "creative_guard"):
        section = incoming_config.get(section_name)
        if not isinstance(section, Mapping):
            continue
        price_map = section.get("product_effective_prices")
        if not isinstance(price_map, Mapping):
            continue
        for item_group_id, raw_price in price_map.items():
            try:
                price = Decimal(str(raw_price))
            except Exception:  # noqa: BLE001
                continue
            product_id = str(item_group_id or "").strip()
            if product_id and price > 0:
                prices[product_id] = price
    updated = 0
    for product_id, price in prices.items():
        result = context.db.execute(
            text(
                """
                update ttb_products
                set effective_price=:effective_price,
                    effective_price_source='manual_strategy_update',
                    effective_price_updated_at=current_timestamp(6),
                    last_seen_at=current_timestamp(6)
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and product_id=:product_id
                  and (:store_id is null or store_id=:store_id)
                """
            ),
            {
                "effective_price": str(price),
                "workspace_id": int(context.workspace_id),
                "auth_id": int(context.auth_id),
                "product_id": product_id,
                "store_id": str(context.store_id) if context.store_id else None,
            },
        )
        updated += max(0, int(result.rowcount or 0))
    return updated


def _extract_item_group_ids(sessions: Sequence[GMVMaxSession]) -> List[str]:
    results: List[str] = []
    for session in sessions:
        if not session.product_list:
            continue
        for product in session.product_list:
            candidate = product.spu_id or product.item_id
            if candidate:
                results.append(str(candidate))
    return list(dict.fromkeys(results))


async def _resolve_campaign_product_ids(
    context: GMVMaxRouteContext,
    campaign: Any | None,
    advertiser_id: str,
) -> list[str]:
    """Load product bindings for a campaign, refreshing from TikTok if needed."""

    payload: Mapping[str, Any] | None = None
    for attribute in ("detail_raw_json", "list_raw_json", "raw_json"):
        candidate = getattr(campaign, attribute, None)
        if isinstance(candidate, Mapping):
            payload = candidate
            break

    product_ids = _extract_item_group_ids_from_payload(payload)
    if product_ids:
        return product_ids

    source_observed_at = catalog_observation_now()
    try:
        response = await _call_tiktok(
            context.client.gmv_max_campaign_info,
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id),
                campaign_id=str(getattr(campaign, "campaign_id", "")),
            ),
        )
        payload = response.data.model_dump(exclude_none=False)
        product_ids = _extract_item_group_ids_from_payload(payload)
    except HTTPException:
        logger.warning(
            "failed to fetch campaign info for product bindings",
            exc_info=True,
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": getattr(campaign, "campaign_id", None),
            },
        )
        return []

    db = getattr(context, "db", None)
    if db is not None and payload is not None:
        try:
            upsert_campaign_from_api(
                db,
                workspace_id=context.workspace_id,
                auth_id=context.auth_id,
                advertiser_id=str(advertiser_id),
                payload=payload,
                store_id_hint=context.store_id,
                campaign_details=payload,
                campaign_details_complete=True,
                source_observed_at=source_observed_at,
            )
            db.flush()
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to persist refreshed campaign payload",
                exc_info=True,
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "campaign_id": getattr(campaign, "campaign_id", None),
                },
            )

    return product_ids


async def _ensure_campaign_products_available(
    context: GMVMaxRouteContext,
    *,
    campaign: Any | None,
    advertiser_id: str,
) -> None:
    """Verify that campaign product bindings are not occupied by other campaigns."""

    if campaign is None:
        return

    product_ids = await _resolve_campaign_product_ids(
        context, campaign=campaign, advertiser_id=advertiser_id
    )
    if not product_ids:
        return

    store_id = getattr(campaign, "store_id", None) or context.store_id
    if not store_id:
        return

    db = getattr(context, "db", None)
    if db is None:
        return

    conflict_stmt = (
        select(
            GmvmaxProductCampaignItemGroup.item_group_id,
            GmvmaxProductCampaignItemGroup.campaign_id,
        )
        .join(
            GmvmaxProductCampaignCatalog,
            (GmvmaxProductCampaignCatalog.workspace_id == GmvmaxProductCampaignItemGroup.workspace_id)
            & (GmvmaxProductCampaignCatalog.auth_id == GmvmaxProductCampaignItemGroup.auth_id)
            & (GmvmaxProductCampaignCatalog.advertiser_id == GmvmaxProductCampaignItemGroup.advertiser_id)
            & (GmvmaxProductCampaignCatalog.store_id == GmvmaxProductCampaignItemGroup.store_id)
            & (GmvmaxProductCampaignCatalog.campaign_id == GmvmaxProductCampaignItemGroup.campaign_id),
        )
        .where(GmvmaxProductCampaignItemGroup.workspace_id == int(context.workspace_id))
        .where(GmvmaxProductCampaignItemGroup.auth_id == int(context.auth_id))
        .where(GmvmaxProductCampaignItemGroup.advertiser_id == str(advertiser_id))
        .where(GmvmaxProductCampaignItemGroup.store_id == str(store_id))
        .where(GmvmaxProductCampaignItemGroup.item_group_id.in_(product_ids))
        .where(GmvmaxProductCampaignCatalog.operation_status == "ENABLE")
        .where(GmvmaxProductCampaignItemGroup.campaign_id != str(campaign.campaign_id))
    )

    conflicts = db.execute(conflict_stmt).all()
    if conflicts:
        occupied_products = sorted(
            {
                str(getattr(row, "item_group_id", None))
                for row in conflicts
                if getattr(row, "item_group_id", None) is not None
            }
        )
        conflicting_campaigns = sorted(
            {
                str(getattr(row, "campaign_id", None))
                for row in conflicts
                if getattr(row, "campaign_id", None)
            }
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "gmvmax_products_occupied",
                "message": "Existing products are occupied by other GMV Max campaigns.",
                "occupied_products": occupied_products,
                "conflicting_campaigns": conflicting_campaigns,
            },
        )


def _build_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session,
    *,
    allow_missing_advertiser: bool,
) -> GMVMaxRouteContext:
    normalized_provider = normalize_provider(provider)
    binding = resolve_account_binding(
        db,
        workspace_id,
        normalized_provider,
        auth_id,
        allow_missing_advertiser=allow_missing_advertiser,
    )
    client = get_gmvmax_client_for_account(
        db,
        workspace_id,
        normalized_provider,
        auth_id,
    )
    return GMVMaxRouteContext(
        workspace_id=workspace_id,
        provider=normalized_provider,
        auth_id=auth_id,
        advertiser_id=binding.advertiser_id,
        store_id=binding.store_id,
        binding=binding,
        client=client,
        db=db,
    )


def get_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return _build_route_context(
        workspace_id,
        provider,
        auth_id,
        db,
        allow_missing_advertiser=False,
    )


def get_optional_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return _build_route_context(
        workspace_id,
        provider,
        auth_id,
        db,
        allow_missing_advertiser=True,
    )


def _extract_store_metadata(store: Any) -> Dict[str, Any]:
    if hasattr(store, "model_dump"):
        try:
            return store.model_dump(exclude_none=False)
        except Exception:  # noqa: BLE001 - defensive
            return {}
    if isinstance(store, dict):
        return dict(store)
    return {}


def _build_auto_binding_candidate(
    store: Any,
    *,
    advertiser_id: str,
    authorization_data: Any,
    usage_data: Any,
    request_ids: Dict[str, Optional[str]],
) -> AutoBindingCandidate | None:
    payload = _extract_store_metadata(store)
    store_id = _normalize_identifier(
        payload.get("store_id") or getattr(store, "store_id", None)
    )
    advertiser_id = _normalize_identifier(
        payload.get("advertiser_id")
        or getattr(store, "advertiser_id", None)
        or advertiser_id
    )
    bc_id = _normalize_identifier(
        payload.get("store_authorized_bc_id")
        or getattr(store, "store_authorized_bc_id", None)
    )
    if not store_id or not advertiser_id:
        return None
    auth_status = None
    if authorization_data is not None:
        auth_status = (
            getattr(authorization_data, "authorization_status", None)
            or getattr(authorization_data, "status", None)
        )
        if not auth_status and getattr(authorization_data, "is_authorized", None):
            auth_status = "EFFECTIVE"

    usage_allowed = getattr(usage_data, "promote_all_products_allowed", None)
    is_running = getattr(usage_data, "is_running_custom_shop_ads", None)

    return AutoBindingCandidate(
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        store_name=payload.get("store_name") or getattr(store, "store_name", None),
        store_authorized_bc_id=bc_id,
        authorization_status=auth_status,
        is_gmv_max_available=payload.get("is_gmv_max_available")
        or getattr(store, "is_gmv_max_available", None),
        promote_all_products_allowed=usage_allowed,
        is_running_custom_shop_ads=is_running,
        request_id=request_ids.get("authorization") or request_ids.get("usage"),
        source=payload or None,
    )


def _is_binding_candidate_ready(candidate: AutoBindingCandidate) -> bool:
    auth_status = (candidate.authorization_status or "").upper()
    auth_ok = auth_status == "EFFECTIVE"
    return bool(candidate.store_authorized_bc_id) and auth_ok


@router.get(
    "/sync-interval",
    response_model=SyncIntervalResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def get_gmvmax_sync_interval_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncIntervalResponse:
    """Return the persisted account sync interval (no upstream TikTok call)."""
    row = get_sync_schedule(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
    )
    default_interval = int(
        getattr(app_settings, "GMVMAX_SYNC_INTERVAL_MINUTES", GMVMAX_SYNC_INTERVAL_OPTIONS[0])
    )
    interval = int(row.interval_minutes) if row is not None else default_interval
    if interval not in GMVMAX_SYNC_INTERVAL_OPTIONS:
        interval = GMVMAX_SYNC_INTERVAL_OPTIONS[0]
    logger.info(
        "gmvmax.sync_interval fetched",
        extra={"workspace_id": context.workspace_id, "auth_id": context.auth_id, "interval": interval},
    )
    return SyncIntervalResponse(interval=interval, available=list(GMVMAX_SYNC_INTERVAL_OPTIONS))


@router.put(
    "/sync-interval",
    response_model=SyncIntervalResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def update_gmvmax_sync_interval_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: SyncIntervalUpdateRequest,
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncIntervalResponse:
    """Persist the account interval consumed by the minute-level Beat dispatcher."""
    interval = int(payload.interval)
    if interval not in GMVMAX_SYNC_INTERVAL_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_SYNC_INTERVAL_INVALID",
                "message": f"interval must be one of {list(GMVMAX_SYNC_INTERVAL_OPTIONS)}",
            },
        )
    upsert_sync_schedule(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
        advertiser_id=str(context.advertiser_id),
        store_id=context.store_id,
        interval_minutes=interval,
        actor_user_id=int(getattr(me, "id", 0) or 0) or None,
    )
    logger.info(
        "gmvmax.sync_interval updated",
        extra={"workspace_id": context.workspace_id, "auth_id": context.auth_id, "interval": interval},
    )
    return SyncIntervalResponse(
        interval=interval,
        available=list(GMVMAX_SYNC_INTERVAL_OPTIONS),
        message="同步间隔已持久化，将由 Beat 调度器在下一轮扫描生效。",
    )


@router.post(
    "/sync",
    response_model=SyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def sync_gmvmax_manual(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: GmvMaxManualSyncRequest,
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskResponse:
    """Dispatch account-scoped GMV Max manual sync via Celery."""

    if not payload.levels:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "missing_levels", "message": "levels is required"},
        )

    end = payload.end_date or _resolve_advertiser_today(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=context.advertiser_id,
    )
    start = payload.start_date or (end - timedelta(days=2))
    if start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_date_range",
                "message": "start_date must be earlier than or equal to end_date.",
            },
        )

    if any(level.value == "OVERVIEW" for level in payload.levels):
        days_inclusive = (end - start).days + 1
        if days_inclusive > 365:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "date_range_too_long",
                    "message": "OVERVIEW date range must not exceed 365 days.",
                },
            )

    task_kwargs = {
        "workspace_id": context.workspace_id,
        "auth_id": context.auth_id,
        "advertiser_id": context.advertiser_id,
        "store_id": context.store_id,
        "levels": [level.value for level in payload.levels],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "campaign_ids": payload.campaign_ids,
        "item_group_ids": payload.item_group_ids,
        # Metrics refresh must not synchronously scan the paginated video
        # library. Only backfill report creatives missing from the local cache.
        "refresh_creative_assets": False,
        "backfill_missing_creative_assets": True,
        "refresh_catalog_details": True,
    }

    async_res = _enqueue_owned_task(
        context,
        task_name="gmvmax.manual_sync_levels",
        kwargs=task_kwargs,
        queue="gmvmax",
        created_by_user_id=int(getattr(me, "id", 0) or 0) or None,
    )
    logger.info(
        "gmvmax.manual_sync_levels dispatched",
        extra={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "task_id": async_res.id,
            "levels": task_kwargs["levels"],
            "start_date": task_kwargs["start_date"],
            "end_date": task_kwargs["end_date"],
            "campaign_ids": payload.campaign_ids,
            "item_group_ids": payload.item_group_ids,
        },
    )

    return _build_task_response(
        async_res, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )


@router.get(
    "/sync/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def get_sync_task_state(
    workspace_id: int,
    provider: str,
    auth_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskStateResponse:
    """Return Celery task status for GMV Max sync jobs (campaign/report sync)."""

    _require_owned_task(context, task_id=task_id)
    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    try:
        state = str(res.state)
        raw_info = res.info
        info = raw_info if isinstance(raw_info, dict) else {}
        error = info.get("error") if state in {"FAILURE", "RETRY"} else None
        progress = _sync_task_progress(state, raw_info)
        if state == "RETRY" and error is None:
            error = {"type": "TaskRetry", "message": progress["message"] if progress else "同步任务正在重试。"}
        elif state == "FAILURE" and error is None:
            error = {"type": type(raw_info).__name__, "message": "GMV Max 同步失败，请稍后重试。"}
        result = None
        if state == "SUCCESS":
            result = info.get("result") if info else res.result
    except ValueError as exc:
        logger.warning(
            "gmvmax.async_task result deserialization failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "task_id": task_id,
                "error": str(exc),
            },
        )
        state = "FAILURE"
        result = None
        error = {"type": "DeserializationError", "message": "Celery result payload invalid"}
        progress = None

    logger.info(
        "gmvmax.sync polled",
        extra={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "task_id": task_id,
            "state": state,
        },
    )

    return SyncTaskStateResponse(
        task_id=task_id,
        state=state,
        result=result,
        error=error,
        progress=progress,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_member)],
)
def get_async_task_state(
    workspace_id: int,
    provider: str,
    auth_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskStateResponse:
    """Return Celery task status for GMV Max async API fetches (report/bid preview)."""

    _require_owned_task(context, task_id=task_id)
    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    try:
        state = str(res.state)
        raw_info = res.info
        info = raw_info if isinstance(raw_info, dict) else {}
        error = info.get("error") if state in {"FAILURE", "RETRY"} else None
        progress = _sync_task_progress(state, raw_info)
        if state == "RETRY" and error is None:
            error = {"type": "TaskRetry", "message": progress["message"] if progress else "同步任务正在重试。"}
        elif state == "FAILURE" and error is None:
            error = {"type": type(raw_info).__name__, "message": "GMV Max 异步任务失败，请稍后重试。"}
        result = None
        if state == "SUCCESS":
            result = info.get("result") if info else res.result
    except ValueError as exc:
        logger.warning(
            "gmvmax.async_task result deserialization failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "task_id": task_id,
                "error": str(exc),
            },
        )
        state = "FAILURE"
        result = None
        error = {"type": "DeserializationError", "message": "Celery result payload invalid"}
        progress = None

    logger.info(
        "gmvmax.async_task polled",
        extra={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "task_id": task_id,
            "state": state,
        },
    )

    return SyncTaskStateResponse(
        task_id=task_id,
        state=state,
        result=result,
        error=error,
        progress=progress,
    )


def _upsert_store_link(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: Optional[str],
) -> None:
    row = (
        db.query(TTBAdvertiserStoreLink)
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id))
        .filter(TTBAdvertiserStoreLink.store_id == str(store_id))
        .first()
    )
    if row is None:
        row = TTBAdvertiserStoreLink(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            relation_type="AUTHORIZER",
            store_authorized_bc_id=_normalize_identifier(store_authorized_bc_id),
            bc_id_hint=_normalize_identifier(store_authorized_bc_id),
            source="gmvmax.auto_bind",
        )
    else:
        if not row.relation_type:
            row.relation_type = "AUTHORIZER"
        if not row.store_authorized_bc_id:
            row.store_authorized_bc_id = _normalize_identifier(store_authorized_bc_id)
        if not row.bc_id_hint:
            row.bc_id_hint = _normalize_identifier(store_authorized_bc_id)
        if not row.source:
            row.source = "gmvmax.auto_bind"
    db.add(row)


def _dedupe_append(target: list[str], value: Optional[str], seen: set[str]) -> None:
    normalized = _normalize_identifier(value)
    if normalized and normalized not in seen:
        seen.add(normalized)
        target.append(normalized)


def _persist_auto_binding(
    context: GMVMaxRouteContext,
    *,
    candidate: AutoBindingCandidate,
    actor_user_id: int | None,
) -> bool:
    """Atomically move every account-level scheduler to the selected binding."""

    previous_advertiser = _normalize_identifier(context.advertiser_id)
    previous_store = _normalize_identifier(context.store_id)
    next_advertiser = _normalize_identifier(candidate.advertiser_id)
    next_store = _normalize_identifier(candidate.store_id)
    changed = previous_advertiser != next_advertiser or previous_store != next_store

    upsert_binding_config(
        context.db,
        workspace_id=int(context.workspace_id),
        auth_id=int(context.auth_id),
        bc_id=candidate.store_authorized_bc_id,
        advertiser_id=next_advertiser,
        store_id=next_store,
        auto_sync_products=True,
        actor_user_id=actor_user_id,
    )
    existing_schedule = get_sync_schedule(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
    )
    interval = int(
        existing_schedule.interval_minutes
        if existing_schedule is not None
        else getattr(
            app_settings,
            "GMVMAX_SYNC_INTERVAL_MINUTES",
            GMVMAX_SYNC_INTERVAL_OPTIONS[0],
        )
    )
    if interval not in GMVMAX_SYNC_INTERVAL_OPTIONS:
        interval = GMVMAX_SYNC_INTERVAL_OPTIONS[0]
    upsert_sync_schedule(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        provider=context.provider,
        advertiser_id=str(next_advertiser),
        store_id=next_store,
        interval_minutes=interval,
        actor_user_id=actor_user_id,
    )
    _upsert_store_link(
        context.db,
        workspace_id=context.workspace_id,
        auth_id=context.auth_id,
        advertiser_id=str(next_advertiser),
        store_id=str(next_store),
        store_authorized_bc_id=candidate.store_authorized_bc_id,
    )
    context.db.commit()
    return changed


async def _refresh_auto_binding_candidate(
    context: GMVMaxRouteContext,
    candidate: AutoBindingCandidate,
) -> AutoBindingCandidate:
    request_ids: Dict[str, Optional[str]] = {}
    auth_resp = await _call_tiktok(
        context.client.gmv_max_exclusive_authorization_get,
        GMVMaxExclusiveAuthorizationGetRequest(
            advertiser_id=str(candidate.advertiser_id),
            store_id=str(candidate.store_id),
            store_authorized_bc_id=str(candidate.store_authorized_bc_id),
        ),
    )
    request_ids["authorization"] = auth_resp.request_id
    usage_resp = await _call_tiktok(
        context.client.gmv_max_store_shop_ad_usage_check,
        GMVMaxStoreAdUsageCheckRequest(
            advertiser_id=str(candidate.advertiser_id),
            store_id=str(candidate.store_id),
        ),
    )
    request_ids["usage"] = usage_resp.request_id
    return _build_auto_binding_candidate(
        {
            "store_id": candidate.store_id,
            "store_name": candidate.store_name,
            "store_authorized_bc_id": candidate.store_authorized_bc_id,
            "advertiser_id": candidate.advertiser_id,
        },
        advertiser_id=candidate.advertiser_id,
        authorization_data=auth_resp.data if auth_resp else None,
        usage_data=usage_resp.data if usage_resp else None,
        request_ids=request_ids,
    ) or candidate


def _build_binding_status(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    store_id: Optional[str],
    advertiser_id: Optional[str],
    bc_id: Optional[str],
) -> BindingStatusResponse:
    normalized_store = _normalize_identifier(store_id)
    normalized_adv = _normalize_identifier(advertiser_id)
    normalized_bc = _normalize_identifier(bc_id)
    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))

    has_binding = bool(normalized_store and normalized_adv)
    status = BindingStatusResponse(
        has_binding=has_binding,
        binding_ready=False,
        advertiser_id=normalized_adv,
        store_id=normalized_store,
        bc_id=normalized_bc,
        last_checked_at=datetime.now(timezone.utc),
    )

    if not binding or not has_binding:
        status.error_code = "binding_missing"
        status.error_message = "尚未完成 GMV Max 店铺-广告主绑定。"
        return status

    bound_store = _normalize_identifier(binding.store_id)
    bound_advertiser = _normalize_identifier(binding.advertiser_id)
    bound_bc = _normalize_identifier(binding.bc_id)
    if (
        normalized_store != bound_store
        or normalized_adv != bound_advertiser
        or (normalized_bc and bound_bc and normalized_bc != bound_bc)
    ):
        status.error_code = "binding_scope_mismatch"
        status.error_message = "当前页面范围与已保存的 GMV Max 广告户绑定不一致，请重新绑定。"
        return status

    candidate = _select_binding_from_links(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        target_store=normalized_store or _normalize_identifier(binding.store_id),
        target_advertiser=normalized_adv or _normalize_identifier(binding.advertiser_id),
        target_bc=normalized_bc or _normalize_identifier(binding.bc_id),
    )

    if candidate and _is_binding_candidate_ready(candidate):
        status.binding_ready = True
        return status

    if candidate is None:
        status.error_code = "binding_not_found"
        status.error_message = "未找到匹配的 GMV Max 授权记录，请重试绑定。"
    else:
        status.error_code = "binding_not_ready"
        status.error_message = "GMV Max 授权未生效，请重试绑定。"

    return status


def _select_binding_from_links(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    target_store: Optional[str],
    target_advertiser: Optional[str],
    target_bc: Optional[str],
) -> AutoBindingCandidate | None:
    """Try to derive a single binding tuple from cached link tables.

    Prefers the most recently seen advertiser-store link that is consistent with a
    business center relationship, and only returns a candidate when the triple is
    unique after normalization and optional targeting filters.
    """

    normalized_bc = _normalize_identifier(target_bc)
    normalized_store = _normalize_identifier(target_store)
    normalized_adv = _normalize_identifier(target_advertiser)

    bc_link_rows = (
        db.query(TTBBCAdvertiserLink.bc_id, TTBBCAdvertiserLink.advertiser_id)
        .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
        .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
        .order_by(TTBBCAdvertiserLink.last_seen_at.desc())
        .all()
    )
    bc_pairs = {
        (_normalize_identifier(bc_id), _normalize_identifier(adv_id))
        for bc_id, adv_id in bc_link_rows
        if _normalize_identifier(bc_id) and _normalize_identifier(adv_id)
    }

    query = (
        db.query(TTBAdvertiserStoreLink)
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
    )
    if normalized_store:
        query = query.filter(TTBAdvertiserStoreLink.store_id == normalized_store)
    if normalized_adv:
        query = query.filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)

    rows = (
        query.order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
        .filter(TTBAdvertiserStoreLink.store_authorized_bc_id.isnot(None))
        .all()
    )

    candidates: list[AutoBindingCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        store_id = _normalize_identifier(row.store_id)
        adv_id = _normalize_identifier(row.advertiser_id)
        bc_id = _normalize_identifier(row.store_authorized_bc_id or row.bc_id_hint)
        if not store_id or not adv_id or not bc_id:
            continue
        if normalized_bc and bc_id != normalized_bc:
            continue
        if bc_pairs and (bc_id, adv_id) not in bc_pairs:
            continue
        key = (store_id, adv_id, bc_id)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            AutoBindingCandidate(
                advertiser_id=adv_id,
                store_id=store_id,
                store_authorized_bc_id=bc_id,
                authorization_status="EFFECTIVE",
                request_id=None,
                source=row.raw_json if hasattr(row, "raw_json") else None,
            )
        )

    if len(candidates) != 1:
        return None
    return candidates[0]


# === GMV Max binding discovery & eligibility ===
# - check current binding status and store/advertiser readiness
# - auto-discover bindings via TikTok store/authorization APIs
# - run precheck for identity/asset occupancy and balance sync
@router.get(
    "/binding_status",
    response_model=BindingStatusResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_binding_status(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: Optional[str] = Query(default=None),
    advertiser_id: Optional[str] = Query(default=None),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> BindingStatusResponse:
    """Report binding readiness after verifying TikTok exclusive authorization."""
    provider = _ensure_provider(provider)
    status = _build_binding_status(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=store_id or (context.binding.store_id if context.binding else None),
        advertiser_id=advertiser_id or (context.binding.advertiser_id if context.binding else None),
        bc_id=context.binding.bc_id if context.binding else None,
    )
    if status.binding_ready:
        candidate = _select_binding_from_links(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            target_store=status.store_id,
            target_advertiser=status.advertiser_id,
            target_bc=status.bc_id,
        )
        if candidate is None:
            status.binding_ready = False
            status.error_code = "binding_not_found"
            status.error_message = "未找到匹配的 GMV Max 授权记录，请重试绑定。"
        else:
            auth_resp = await _call_tiktok(
                context.client.gmv_max_exclusive_authorization_get,
                GMVMaxExclusiveAuthorizationGetRequest(
                    advertiser_id=str(candidate.advertiser_id),
                    store_id=str(candidate.store_id),
                    store_authorized_bc_id=str(candidate.store_authorized_bc_id),
                ),
            )
            official_status = str(
                getattr(auth_resp.data, "authorization_status", None)
                or getattr(auth_resp.data, "status", None)
                or ""
            ).upper()
            if official_status != "EFFECTIVE":
                status.binding_ready = False
                status.error_code = "exclusive_authorization_changed"
                status.error_message = "TikTok 已将 GMV Max 独家授权切换到其他广告户，系统正在重新识别。"
    logger.info(
        "gmvmax.binding_status fetched",
        extra={
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "store_id": status.store_id,
            "advertiser_id": status.advertiser_id,
            "binding_ready": status.binding_ready,
            "has_binding": status.has_binding,
            "error_code": status.error_code,
        },
    )
    return status


@router.post(
    "/rebind_auto",
    response_model=AutoBindingResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def rebind_gmvmax_binding(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: AutoBindingRequest = Body(default_factory=AutoBindingRequest),
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> AutoBindingResponse:
    """Force auto-binding using TikTok store/authorization checks and persist when allowed."""
    provider = _ensure_provider(provider)
    normalized_payload = AutoBindingRequest(
        advertiser_id=payload.advertiser_id,
        store_id=payload.store_id,
        persist=True,
    )
    previous_advertiser = _normalize_identifier(context.advertiser_id)
    previous_store = _normalize_identifier(context.store_id)
    response = await auto_bind_gmvmax_account(
        workspace_id,
        provider,
        auth_id,
        normalized_payload,
        me=me,
        context=context,
    )
    selected = response.selected
    response.binding_changed = bool(
        response.persisted
        and selected
        and (
            previous_advertiser != _normalize_identifier(selected.advertiser_id)
            or previous_store != _normalize_identifier(selected.store_id)
        )
    )
    if response.binding_changed and selected:
        end = _resolve_advertiser_today(
            context.db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            advertiser_id=selected.advertiser_id,
        )
        try:
            async_result = _enqueue_owned_task(
                context,
                task_name="gmvmax.manual_sync_levels",
                kwargs={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "advertiser_id": selected.advertiser_id,
                    "store_id": selected.store_id,
                    "levels": ["OVERVIEW", "CAMPAIGN", "PRODUCT", "CREATIVE"],
                    "start_date": (end - timedelta(days=2)).isoformat(),
                    "end_date": end.isoformat(),
                    "refresh_creative_assets": False,
                    "backfill_missing_creative_assets": True,
                    "refresh_catalog_details": True,
                },
                queue="gmvmax",
                created_by_user_id=int(getattr(me, "id", 0) or 0) or None,
            )
            response.bootstrap_task_id = str(async_result.id)
            response.bootstrap_enqueued = True
        except HTTPException:
            logger.exception(
                "gmvmax.rebind_auto bootstrap enqueue failed after binding commit",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": selected.advertiser_id,
                    "store_id": selected.store_id,
                },
            )
    logger.info(
        "gmvmax.rebind_auto completed",
        extra={
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "store_id": normalized_payload.store_id,
            "advertiser_id": normalized_payload.advertiser_id,
            "persisted": response.persisted,
        },
    )
    return response


@router.post(
    "/binding/auto",
    response_model=AutoBindingResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def auto_bind_gmvmax_account(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: AutoBindingRequest,
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> AutoBindingResponse:
    """Discover GMV Max store bindings via TikTok store/list + authorization/usage APIs and optionally persist them."""

    target_store = _normalize_identifier(payload.store_id)
    target_bc = None
    advertiser_candidates: List[str] = []
    seen_advertisers: set[str] = set()

    _dedupe_append(advertiser_candidates, payload.advertiser_id, seen_advertisers)
    _dedupe_append(advertiser_candidates, context.advertiser_id, seen_advertisers)

    candidates: List[AutoBindingCandidate] = []
    db_candidate = _select_binding_from_links(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        target_store=target_store,
        target_advertiser=payload.advertiser_id or context.advertiser_id,
        target_bc=target_bc,
    )
    if db_candidate:
        candidate = await _refresh_auto_binding_candidate(context, db_candidate)
        candidates.append(candidate)
        if _is_binding_candidate_ready(candidate):
            persisted = False
            changed = False
            if payload.persist:
                try:
                    changed = _persist_auto_binding(
                        context,
                        candidate=candidate,
                        actor_user_id=int(me.id),
                    )
                    persisted = True
                except BindingConfigStorageNotReady as exc:
                    context.db.rollback()
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=(
                            "GMV Max binding configuration storage is not initialized; "
                            "please run database migrations."
                        ),
                    ) from exc
            return AutoBindingResponse(
                selected=candidate,
                candidates=[candidate],
                persisted=persisted,
                binding_changed=changed,
            )
        # A stale cached binding is expected after TikTok moves exclusive
        # authorization. Continue scanning every advertiser linked to the store.

    if target_store:
        link_rows = (
            context.db.query(TTBAdvertiserStoreLink.advertiser_id)
            .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
            .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
            .filter(TTBAdvertiserStoreLink.store_id == target_store)
            .order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
            .all()
        )
        for (adv_id,) in link_rows:
            _dedupe_append(advertiser_candidates, adv_id, seen_advertisers)

    advertiser_rows = (
        context.db.query(TTBAdvertiser.advertiser_id)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .all()
    )
    for (adv_id,) in advertiser_rows:
        _dedupe_append(advertiser_candidates, adv_id, seen_advertisers)

    if not advertiser_candidates:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No advertisers available for GMV Max binding discovery",
        )

    for advertiser_id in advertiser_candidates:
        if any(candidate.advertiser_id == str(advertiser_id) for candidate in candidates):
            continue
        store_resp = await _call_tiktok(
            context.client.gmv_max_store_list,
            GMVMaxStoreListRequest(advertiser_id=str(advertiser_id)),
        )

        for store in store_resp.data.store_list:
            store_meta = _extract_store_metadata(store)
            store_id = _normalize_identifier(store_meta.get("store_id") or getattr(store, "store_id", None))
            bc_id = _normalize_identifier(
                store_meta.get("store_authorized_bc_id")
                or getattr(store, "store_authorized_bc_id", None)
            )
            if target_store and store_id != target_store:
                continue
            if not store_id:
                continue
            if not bc_id:
                # Without a business center ID TikTok APIs reject usage checks, and the
                # binding cannot be persisted anyway.
                continue

            request_ids: Dict[str, Optional[str]] = {"store_list": store_resp.request_id}

            auth_resp = await _call_tiktok(
                context.client.gmv_max_exclusive_authorization_get,
                GMVMaxExclusiveAuthorizationGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=str(bc_id),
                ),
            )
            request_ids["authorization"] = auth_resp.request_id
            usage_resp = await _call_tiktok(
                context.client.gmv_max_store_shop_ad_usage_check,
                GMVMaxStoreAdUsageCheckRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id or ""),
                ),
            )
            request_ids["usage"] = usage_resp.request_id

            candidate = _build_auto_binding_candidate(
                store,
                advertiser_id=str(advertiser_id),
                authorization_data=auth_resp.data if auth_resp else None,
                usage_data=usage_resp.data,
                request_ids=request_ids,
            )
            if candidate:
                candidates.append(candidate)

    ready_candidates = [c for c in candidates if _is_binding_candidate_ready(c)]
    selected = ready_candidates[0] if ready_candidates else None
    if selected is None:
        authorized_candidates = [
            c
            for c in candidates
            if (c.authorization_status or "").upper() not in {"UNAUTHORIZED", "REJECTED"}
        ]
        selected = authorized_candidates[0] if authorized_candidates else None

    logger.info(
        "gmvmax.auto_bind selection",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "target_store": target_store,
            "target_advertiser": payload.advertiser_id or context.advertiser_id,
            "target_bc": target_bc,
            "candidate_count": len(candidates),
            "ready_candidates": len(ready_candidates),
            "selected": selected.model_dump(exclude_none=True) if selected else None,
        },
    )

    if selected is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="未找到授权的广告户，请在店铺后台进行绑定。",
        )

    persisted = False
    changed = False
    if payload.persist and selected:
        if not _is_binding_candidate_ready(selected):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No GMV Max binding candidates passed exclusive authorization checks; cannot persist binding."
                ),
            )
        if not selected.store_authorized_bc_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="store_authorized_bc_id is required to persist GMV Max binding",
            )
        try:
            changed = _persist_auto_binding(
                context,
                candidate=selected,
                actor_user_id=int(me.id),
            )
            persisted = True
        except BindingConfigStorageNotReady as exc:
            context.db.rollback()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
            ) from exc
        except Exception:
            context.db.rollback()
            raise
    elif selected:
        try:
            _upsert_store_link(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=selected.advertiser_id,
                store_id=selected.store_id,
                store_authorized_bc_id=selected.store_authorized_bc_id,
            )
            context.db.commit()
        except Exception:
            context.db.rollback()
            raise

    return AutoBindingResponse(
        selected=selected,
        candidates=candidates,
        persisted=persisted,
        binding_changed=changed if payload.persist and selected else False,
    )


@router.post(
    "/precheck",
    response_model=GMVMaxPrecheckResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def gmvmax_precheck_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: GMVMaxPrecheckRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> GMVMaxPrecheckResponse:
    """Run comprehensive precheck (shop usage, assets, recommendation) synchronously."""

    advertiser_id, _ = _validate_bound_scope(
        context,
        advertiser_id=payload.advertiser_id,
        store_id=payload.store_id,
    )
    try:
        return await gmvmax_precheck(
            context.db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        await _handle_tiktok_error(exc)


@router.get(
    "/identity",
    response_model=IdentityListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_identities_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: str = Query(...),
    advertiser_id: Optional[str] = Query(None),
    store_authorized_bc_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> IdentityListResponse:
    """Return eligible identities using TikTok's official identity contract."""

    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "GMVMAX_STORE_REQUIRED", "message": "store_id is required."},
        )
    persisted_bc_id = resolve_creative_asset_store_authorized_bc_id(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=effective_advertiser_id,
        store_id=effective_store_id,
    ) or context.binding.bc_id
    requested_bc_id = _normalize_identifier(store_authorized_bc_id)
    if (
        requested_bc_id
        and persisted_bc_id
        and requested_bc_id != _normalize_identifier(persisted_bc_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GMVMAX_BC_SCOPE_MISMATCH",
                "message": "store_authorized_bc_id is outside the persisted binding.",
            },
        )
    effective_bc_id = requested_bc_id or _normalize_identifier(persisted_bc_id)
    if not effective_bc_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_STORE_AUTHORIZED_BC_REQUIRED",
                "message": "store_authorized_bc_id is required.",
            },
        )

    response = await _call_tiktok(
        context.client.gmv_max_identity_get,
        GMVMaxIdentityGetRequest(
            advertiser_id=effective_advertiser_id,
            store_id=effective_store_id,
            store_authorized_bc_id=str(effective_bc_id),
        ),
    )
    identities: list[IdentitySummary] = []
    seen: set[str] = set()
    for entry in response.data.identity_list:
        entry_data = (
            entry.model_dump(exclude_none=True)
            if hasattr(entry, "model_dump")
            else {}
        )
        info = getattr(entry, "identity_info", None)
        info_data = (
            info.model_dump(exclude_none=True)
            if hasattr(info, "model_dump")
            else {}
        )
        identity_id = _normalize_identifier(
            info_data.get("identity_id") or entry_data.get("identity_id")
        )
        if not identity_id or identity_id in seen:
            continue
        if entry_data.get("product_gmv_max_available") is False:
            continue
        seen.add(identity_id)
        user_name = (
            info_data.get("user_name")
            or entry_data.get("user_name")
            or entry_data.get("display_name")
        )
        identities.append(
            IdentitySummary(
                identity_id=identity_id,
                identity_type=info_data.get("identity_type")
                or entry_data.get("identity_type"),
                user_name=user_name,
                identity_name=user_name,
                profile_image=info_data.get("profile_image")
                or entry_data.get("profile_image"),
                product_gmv_max_available=entry_data.get(
                    "product_gmv_max_available"
                ),
                identity_authorized_bc_id=entry_data.get(
                    "identity_authorized_bc_id"
                )
                or str(effective_bc_id),
                identity_authorized_bc_name=entry_data.get(
                    "identity_authorized_bc_name"
                ),
                identity_authorized_shop_id=entry_data.get(
                    "identity_authorized_shop_id"
                ),
                store_id=effective_store_id,
            )
        )
    return IdentityListResponse(
        identities=identities,
        identity_list=identities,
        items=identities,
        request_id=response.request_id,
    )


def _serve_gmvmax_creative_media(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    asset_id: int,
    kind: str,
) -> FileResponse:
    try:
        row = db.execute(
            text(
                """
                select id, workspace_id, auth_id, advertiser_id, store_id, item_id,
                       local_preview_path, local_cover_path,
                       preview_content_type, cover_content_type
                from gmvmax_creative_asset_cache
                where id=:asset_id and workspace_id=:workspace_id and auth_id=:auth_id
                limit 1
                """
            ),
            {"asset_id": int(asset_id), "workspace_id": int(workspace_id), "auth_id": int(auth_id)},
        ).mappings().first()
        media = resolve_creative_media(row, kind) if row else None
        if media is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creative media is not cached yet")
        path, content_type = media
    finally:
        # FileResponse streams after the route returns.  Release the read-only
        # transaction before that happens so a page full of covers cannot pin
        # every SQLAlchemy connection for the lifetime of the file response.
        db.rollback()
    return FileResponse(
        path=str(path),
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600, must-revalidate",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/creative-assets/{asset_id}/video", dependencies=[Depends(require_tenant_member)])
def get_gmvmax_creative_video_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int = Path(..., ge=1),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> FileResponse:
    return _serve_gmvmax_creative_media(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        asset_id=asset_id,
        kind="video",
    )


@router.get("/creative-assets/{asset_id}/cover", dependencies=[Depends(require_tenant_member)])
def get_gmvmax_creative_cover_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int = Path(..., ge=1),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> FileResponse:
    return _serve_gmvmax_creative_media(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        asset_id=asset_id,
        kind="cover",
    )


@router.get(
    "/creative-assets",
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_creative_assets_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: str = Query(...),
    advertiser_id: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    item_group_id: Optional[str] = Query(None),
    item_group_ids: Optional[List[str]] = Query(None),
    refresh: bool = Query(False),
    lookback_days: int = Query(30, ge=1, le=180),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=1000),
    offset: Optional[int] = Query(None, ge=0),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> dict[str, Any]:
    """Return GMV Max video candidates for manual validation campaigns."""

    db = context.db
    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    if not effective_advertiser_id or not effective_store_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="advertiser_id and store_id are required",
        )
    requested_item_group_ids = (
        list(item_group_ids)
        if isinstance(item_group_ids, (list, tuple, set))
        else []
    )
    if isinstance(item_group_id, str) and item_group_id.strip():
        requested_item_group_ids.append(item_group_id)
    normalized_item_group_ids = _sanitize_id_list(requested_item_group_ids)
    normalized_campaign_id = str(campaign_id or "").strip() or None
    if normalized_campaign_id:
        campaign_exists = db.execute(
            select(GmvmaxProductCampaignCatalog.id)
            .where(GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignCatalog.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignCatalog.advertiser_id == str(effective_advertiser_id))
            .where(GmvmaxProductCampaignCatalog.store_id == str(effective_store_id))
            .where(GmvmaxProductCampaignCatalog.campaign_id == normalized_campaign_id)
            .limit(1)
        ).scalar_one_or_none()
        if campaign_exists is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "campaign_not_found_in_scope",
                    "message": "Campaign is not bound to the requested advertiser and store.",
                },
            )
        campaign_item_group_ids = _sanitize_id_list(
            db.execute(
                select(GmvmaxProductCampaignItemGroup.item_group_id)
                .where(GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id))
                .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
                .where(GmvmaxProductCampaignItemGroup.advertiser_id == str(effective_advertiser_id))
                .where(GmvmaxProductCampaignItemGroup.store_id == str(effective_store_id))
                .where(GmvmaxProductCampaignItemGroup.campaign_id == normalized_campaign_id)
            ).scalars().all()
        )
        if not campaign_item_group_ids:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "campaign_product_binding_missing",
                    "message": "Campaign product binding is not available yet.",
                },
            )
        outside_campaign = sorted(
            set(normalized_item_group_ids) - set(campaign_item_group_ids)
        )
        if outside_campaign:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "item_group_outside_campaign_scope",
                    "message": "One or more products are not bound to this campaign.",
                    "item_group_ids": outside_campaign,
                },
            )
        normalized_item_group_ids = normalized_item_group_ids or campaign_item_group_ids
    metric_start_date = _resolve_advertiser_today(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(effective_advertiser_id),
    ) - timedelta(days=int(lookback_days))
    has_item_group_filter = bool(normalized_item_group_ids)
    metric_item_filter = (
        "and item_group_id in :item_group_ids" if has_item_group_filter else ""
    )
    metric_campaign_filter = (
        "and campaign_id=:campaign_id" if normalized_campaign_id else ""
    )
    partition_active_expression = (
        """
        case when exists (
          select 1
          from gmvmax_creative_asset_products active_ap
          where active_ap.workspace_id=a.workspace_id
            and active_ap.auth_id=a.auth_id
            and active_ap.advertiser_id=a.advertiser_id
            and active_ap.store_id=a.store_id
            and active_ap.item_id=a.item_id
            and active_ap.item_group_id in :item_group_ids
        ) then 1 else 0 end
        """
        if has_item_group_filter
        else "1"
    )
    asset_item_filter = (
        """
        and (
          m.item_group_id in :item_group_ids
          or exists (
            select 1
            from gmvmax_creative_asset_products ap
            where ap.workspace_id=a.workspace_id
              and ap.auth_id=a.auth_id
              and ap.advertiser_id=a.advertiser_id
              and ap.store_id=a.store_id
              and ap.item_id=a.item_id
              and ap.item_group_id in :item_group_ids
          )
        )
        """
        if has_item_group_filter
        else ""
    )

    # GET is database-only. Explicit refreshes are handled by the POST endpoint.
    upload_sync_result: dict[str, Any] | None = None
    sync_result: dict[str, Any] | None = (
        {"skipped": True, "reason": "refresh_requires_post"}
        if refresh
        else None
    )

    creative_assets_stmt = text(
        f"""
            with metrics as (
                select creative_id,
                       max(item_group_id) as item_group_id,
                       sum(coalesce(nullif(net_cost_cents, 0), cost_cents, 0)) as cost_cents,
                       sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents,
                       sum(coalesce(orders, 0)) as orders,
                       sum(coalesce(clicks, 0)) as clicks,
                       sum(coalesce(impressions, product_impressions, 0)) as impressions,
                       sum(coalesce(product_clicks, 0)) as product_clicks,
                       sum(coalesce(product_impressions, 0)) as product_impressions,
                       max(ad_video_view_rate_2s) as ad_video_view_rate_2s,
                       max(ad_video_view_rate_6s) as ad_video_view_rate_6s,
                       max(ad_video_view_rate_p100) as ad_video_view_rate_p100
                from gmvmax_product_creative_metrics_daily
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and stat_time_day >= :metric_start_date
                  {metric_campaign_filter}
                  {metric_item_filter}
                group by creative_id
            )
            select a.id, a.workspace_id, a.auth_id, a.advertiser_id,
                   a.item_id,
                   coalesce(m.item_group_id, a.item_group_id) as item_group_id,
                   a.video_id, a.title, a.preview_url, a.video_cover_url,
                   a.local_preview_path, a.local_cover_path,
                   a.preview_content_type, a.cover_content_type, a.media_cache_status,
                   a.duration,
                    a.identity_id, a.identity_type,
                    coalesce(
                      json_unquote(json_extract(a.raw_json, '$.identity_info.identity_authorized_bc_id')),
                      json_unquote(json_extract(a.raw_json, '$.identity_authorized_bc_id'))
                    ) as identity_authorized_bc_id,
                    coalesce(
                      json_unquote(json_extract(a.raw_json, '$.identity_info.identity_authorized_shop_id')),
                      json_unquote(json_extract(a.raw_json, '$.identity_authorized_shop_id'))
                    ) as identity_authorized_shop_id,
                    json_unquote(json_extract(a.raw_json, '$.can_change_anchor')) as can_change_anchor,
                    coalesce(
                      json_unquote(json_extract(a.raw_json, '$._gmv_ops_sync.active')),
                      'true'
                    ) as cache_active,
                    {partition_active_expression} as partition_active,
                    a.identity_name, a.store_id, a.fetched_at, a.updated_at,
                   coalesce(m.cost_cents, 0) as cost_cents,
                   coalesce(m.gross_revenue_cents, 0) as gross_revenue_cents,
                   coalesce(m.orders, 0) as orders,
                   coalesce(m.clicks, 0) as clicks,
                   coalesce(m.impressions, 0) as impressions,
                   coalesce(m.product_clicks, 0) as product_clicks,
                   coalesce(m.product_impressions, 0) as product_impressions,
                   coalesce(m.ad_video_view_rate_2s, 0) as ad_video_view_rate_2s,
                   coalesce(m.ad_video_view_rate_6s, 0) as ad_video_view_rate_6s,
                   coalesce(m.ad_video_view_rate_p100, 0) as ad_video_view_rate_p100
            from gmvmax_creative_asset_cache a
            left join metrics m
              on m.creative_id collate utf8mb4_0900_ai_ci
               = a.item_id collate utf8mb4_0900_ai_ci
            where a.workspace_id=:workspace_id
              and a.auth_id=:auth_id
              and a.advertiser_id=:advertiser_id
              and a.store_id=:store_id
              {asset_item_filter}
            order by
              case when coalesce(m.cost_cents, 0) > 0 then 0 else 1 end,
              coalesce(m.gross_revenue_cents, 0) / greatest(coalesce(m.cost_cents, 0), 1) desc,
              coalesce(m.orders, 0) desc,
              coalesce(m.cost_cents, 0) desc,
              a.updated_at desc,
              a.item_id asc
            """
    )
    if has_item_group_filter:
        creative_assets_stmt = creative_assets_stmt.bindparams(
            bindparam("item_group_ids", expanding=True)
        )
    rows = db.execute(
        creative_assets_stmt,
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": str(effective_advertiser_id),
            "store_id": str(effective_store_id),
            "metric_start_date": metric_start_date,
            "campaign_id": normalized_campaign_id,
            "item_group_ids": normalized_item_group_ids,
        },
    ).mappings().all()
    candidates = [_creative_asset_candidate_from_row(row) for row in rows]
    try:
        historical_removed = {
            creative_id
            for creative_id, _ in _load_historical_removed_creatives(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(effective_advertiser_id),
                store_id=str(effective_store_id),
                item_group_ids=normalized_item_group_ids or None,
            )
        }
    except Exception:  # noqa: BLE001 - optional enrichment must not break candidate loading
        logger.exception(
            "Failed to load historical removed creatives for candidate ranking",
            extra={"item_group_ids": normalized_item_group_ids},
        )
        db.rollback()
        historical_removed = set()
    for candidate in candidates:
        candidate["historically_excluded"] = str(candidate.get("item_id") or "") in historical_removed

    product_price, product_price_source = _product_price_for_hermes(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=str(effective_store_id),
        item_group_id=str(item_group_id) if item_group_id else None,
    )
    try:
        candidates, hermes_summary = rank_creative_candidates(
            candidates,
            product_price=product_price,
            minimum_roi=0.8,
            recommendation_limit=4,
        )
    except Exception:  # noqa: BLE001 - ranking is advisory; candidates should still load
        logger.exception(
            "Hermes creative ranking failed",
            extra={"item_group_id": str(item_group_id) if item_group_id else None},
        )
        hermes_summary = {
            "model": "HERMES_PERFORMANCE_RANKER_V1",
            "status": "ranking_failed",
            "evaluated": len(candidates),
            "recommended": 0,
            "product_price": product_price,
            "minimum_roi": 0.8,
            "has_proven_winners": False,
        }
    total_number = len(candidates)
    effective_offset = (
        int(offset)
        if offset is not None
        else (int(page) - 1) * int(page_size)
    )
    paged_candidates = candidates[
        effective_offset : effective_offset + int(page_size)
    ]

    upload_rows = db.execute(
        text(
            """
            select *
            from gmvmax_manual_creative_uploads
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and (
                :item_group_id is null
                or item_group_id=:item_group_id
                or item_group_id is null
            )
            order by created_at desc
            """
        ),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": str(effective_advertiser_id),
            "store_id": str(effective_store_id),
            "item_group_id": str(item_group_id) if item_group_id else None,
        },
    ).mappings().all()
    uploads = [
        _manual_upload_candidate_from_row(
            row,
            signed_file_url=build_manual_upload_url(
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                upload_id=str(row.get("upload_id")),
            ),
        )
        for row in upload_rows
    ]
    total_page = ceil(total_number / int(page_size)) if total_number else 0
    return {
        "items": paged_candidates,
        "uploads": uploads,
        "uploads_total": len(uploads),
        "page": int(page),
        "page_size": int(page_size),
        "offset": effective_offset,
        "total": total_number,
        "total_number": total_number,
        "page_info": {
            "page": int(page),
            "page_size": int(page_size),
            "offset": effective_offset,
            "total_number": total_number,
            "total_page": total_page,
            "has_more": effective_offset + len(paged_candidates) < total_number,
        },
        "upload_sync": upload_sync_result,
        "sync": sync_result,
        "scope": {
            "campaign_id": normalized_campaign_id,
            "item_group_ids": normalized_item_group_ids,
        },
        "manual_selection_ready": any(item.get("selectable") for item in candidates),
        "hermes": {
            **hermes_summary,
            "status": hermes_summary.get("status") or "ok",
            "lookback_days": int(lookback_days),
            "historically_excluded": len(historical_removed),
            "product_price_source": product_price_source,
        },
    }


@router.post(
    "/creative-assets/refresh",
    dependencies=[Depends(require_tenant_admin)],
)
async def refresh_gmvmax_creative_assets_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: str = Query(...),
    advertiser_id: Optional[str] = Query(None),
    item_group_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> dict[str, Any]:
    """Explicitly refresh upload state and the TikTok creative cache."""

    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "GMVMAX_STORE_REQUIRED", "message": "store_id is required."},
        )
    upload_sync = await _refresh_pending_manual_uploads(
        context,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=effective_advertiser_id,
        store_id=effective_store_id,
    )
    store_authorized_bc_id = (
        context.binding.bc_id
        or resolve_creative_asset_store_authorized_bc_id(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=effective_advertiser_id,
            store_id=effective_store_id,
        )
    )
    if not store_authorized_bc_id:
        sync_result: dict[str, Any] = {
            "skipped": True,
            "reason": "missing_store_authorized_bc_id",
        }
    else:
        sync_result = await sync_creative_assets_for_scope(
            context.db,
            context.client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=effective_advertiser_id,
            store_id=effective_store_id,
            store_authorized_bc_id=str(store_authorized_bc_id),
            item_group_ids=[str(item_group_id)] if item_group_id else None,
        )
    context.db.commit()
    return {"upload_sync": upload_sync, "sync": sync_result}


@router.get(
    "/creative-assets/uploads/{upload_id}/file",
    include_in_schema=False,
)
async def get_gmvmax_manual_creative_file_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_id: str,
    expires: int = Query(...),
    signature: str = Query(..., min_length=20, max_length=128),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve a manual upload only through a short-lived HMAC capability URL."""

    if not verify_manual_upload_signature(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        upload_id=upload_id,
        expires=expires,
        signature=signature,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "GMVMAX_UPLOAD_URL_INVALID", "message": "Upload URL is invalid or expired."},
        )
    row = db.execute(
        text(
            """
            select file_path, mime_type, file_name
            from gmvmax_manual_creative_uploads
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and upload_id=:upload_id
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "upload_id": str(upload_id),
        },
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="uploaded video not found")

    base_dir = MANUAL_UPLOAD_ROOT.resolve()
    file_path = LocalPath(str(row.get("file_path") or "")).resolve()
    if file_path != base_dir and base_dir not in file_path.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="uploaded video not found")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="uploaded video file missing")

    return FileResponse(
        str(file_path),
        media_type=row.get("mime_type") or "video/mp4",
        filename=row.get("file_name") or file_path.name,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/creative-assets/upload",
    dependencies=[Depends(require_tenant_admin)],
)
async def upload_gmvmax_manual_creative_route(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: str = Form(...),
    tiktok_account_id: int = Form(...),
    advertiser_id: Optional[str] = Form(None),
    item_group_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    file: UploadFile = File(...),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> dict[str, Any]:
    """Publish a video through an owned TikTok account for GMV Max use."""

    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    if not effective_advertiser_id or not effective_store_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="advertiser_id and store_id are required")
    account = context.db.get(OAuthTikTokAccount, int(tiktok_account_id))
    if (
        account is None
        or int(account.workspace_id) != int(workspace_id)
        or account.status != "active"
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="请选择一个已授权的 TikTok 账号")
    if not _tiktok_account_can_publish(account):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="该 TikTok 账号未授予 video.publish 权限，请重新授权后再上传",
        )

    original_name = (file.filename or "creative.mp4").replace("\\", "/").split("/")[-1]
    suffix = LocalPath(original_name).suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mpeg", ".avi", ".webm", ".m4v"}:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only mp4, mov, mpeg, avi, webm and m4v video files are supported",
        )

    upload_id = uuid4().hex
    public_url = build_manual_upload_url(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        upload_id=upload_id,
    )
    public_path = public_url
    target_dir = ensure_private_manual_upload_directory(
        workspace_id,
        auth_id,
        effective_store_id,
    )
    target_path = target_dir / f"{upload_id}{suffix}"

    size = 0
    with target_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 200 * 1024 * 1024:
                try:
                    target_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Video file cannot exceed 200MB")
            out.write(chunk)
    target_path.chmod(0o640)

    # TikTok fetches video_url during the upload request. Persist and commit the
    # row first so the public file endpoint can authorize that callback.
    context.db.execute(
        text(
            """
            insert into gmvmax_manual_creative_uploads (
                workspace_id, auth_id, advertiser_id, store_id, item_group_id,
                upload_id, title, file_name, mime_type, file_size, file_path,
                upload_status, tiktok_account_id, tiktok_business_id,
                public_url, raw_json, created_at, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :item_group_id,
                :upload_id, :title, :file_name, :mime_type, :file_size, :file_path,
                'TIKTOK_PUBLISHING', :tiktok_account_id, :tiktok_business_id,
                :public_url, cast(:raw_json as json),
                current_timestamp(6), current_timestamp(6)
            )
            """
        ),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": str(effective_advertiser_id),
            "store_id": str(effective_store_id),
            "item_group_id": str(item_group_id) if item_group_id else None,
            "upload_id": upload_id,
            "title": (title or original_name)[:500],
            "file_name": original_name[:500],
            "mime_type": (file.content_type or "video/mp4")[:128],
            "file_size": size,
            "file_path": str(target_path),
            "tiktok_account_id": int(account.id),
            "tiktok_business_id": str(account.open_id),
            "public_url": public_url,
            "raw_json": json.dumps(
                {
                    "stage": "publishing_to_tiktok_account",
                    "public_url": public_url,
                    "tiktok_account_alias": account.alias,
                    "tiktok_business_id": str(account.open_id),
                    "product_optional": True,
                },
                ensure_ascii=False,
            ),
        },
    )
    context.db.commit()

    publish_id: str | None = None
    upload_status = "TIKTOK_PUBLISHING"
    upload_error: str | None = None
    account_client: TikTokBusinessGMVMaxClient | None = None
    try:
        account_token, _, _ = await get_fresh_tiktok_account_token_plain(context.db, int(account.id))
        account_client = TikTokBusinessGMVMaxClient(access_token=account_token)
        publish_response = await account_client.business_video_publish(
            TikTokAccountVideoPublishRequest(
                business_id=str(account.open_id),
                video_url=public_url,
                post_info={
                    "caption": (title or LocalPath(original_name).stem)[:2200],
                    "is_brand_organic": True,
                    "is_branded_content": False,
                    "disable_comment": False,
                    "disable_duet": False,
                    "disable_stitch": False,
                    "is_ads_only": True,
                },
            )
        )
        publish_id = _normalize_identifier(getattr(publish_response.data, "share_id", None))
        if not publish_id:
            raise ValueError("TikTok 未返回 publish_id")
    except (TTBApiError, TTBHttpError) as exc:
        upload_status = "TIKTOK_PUBLISH_FAILED"
        upload_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        upload_status = "TIKTOK_PUBLISH_FAILED"
        upload_error = str(exc)
    finally:
        if account_client is not None:
            try:
                await account_client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to close TikTok account upload client",
                    extra={"upload_id": upload_id},
                    exc_info=True,
                )

    context.db.execute(
        text(
            """
            update gmvmax_manual_creative_uploads
            set upload_status=:upload_status,
                publish_id=:publish_id,
                upload_error=:upload_error,
                raw_json=cast(:raw_json as json),
                updated_at=current_timestamp(6)
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and upload_id=:upload_id
            """
        ),
        {
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "upload_id": upload_id,
            "upload_status": upload_status,
            "publish_id": publish_id,
            "upload_error": upload_error,
            "raw_json": json.dumps(
                {
                    "stage": "publish_requested" if publish_id else "publish_failed",
                    "official_api_chain": [
                        "/business/video/publish/",
                        "/business/publish/status/",
                        "/gmv_max/video/get/",
                        "/gmv_max/creation/custom_anchor_video_list/create/",
                    ],
                    "original_file_name": original_name,
                    "public_path": public_path,
                    "public_url": public_url,
                    "tiktok_account_alias": account.alias,
                    "tiktok_business_id": str(account.open_id),
                    "publish_id": publish_id,
                    "item_group_id": str(item_group_id) if item_group_id else None,
                    "upload_error": upload_error,
                },
                ensure_ascii=False,
            ),
        },
    )
    context.db.commit()
    return {
        "upload_id": upload_id,
        "status": upload_status,
        "selectable": False,
        "not_selectable_reason": (
            f"TikTok 发布失败：{upload_error}"
            if upload_error
            else "TikTok 正在发布视频；发布完成后系统会自动同步 GMV Max 身份并关联所选商品。"
        ),
        "file_name": original_name,
        "file_size": size,
        "file_url": public_url,
        "publish_id": publish_id,
        "tiktok_account_id": int(account.id),
        "tiktok_account_alias": account.alias,
        "item_group_id": str(item_group_id) if item_group_id else None,
        "preview_url": public_url,
    }


# === GMV Max campaign lifecycle & details ===
# - create/update campaigns, list from cache, and fetch TikTok details/sessions
def _validate_replacement_campaign_for_create(
    context: GMVMaxRouteContext,
    *,
    replacement_campaign_id: str | None,
    store_id: str,
    item_group_ids: Sequence[str] | None,
) -> Any | None:
    if not replacement_campaign_id:
        return None
    replacement = _load_campaign_action_source(
        context,
        str(replacement_campaign_id),
    )
    if (
        replacement is None
        or str(getattr(replacement, "store_id", "") or "") != str(store_id)
        or _campaign_is_deleted(replacement)
        or str(getattr(replacement, "operation_status", "") or "").upper()
        not in {"DISABLE", "PAUSED", "INACTIVE"}
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GMVMAX_REPLACEMENT_NOT_RECOVERABLE",
                "message": (
                    "replacement_campaign_id must identify a paused, non-deleted "
                    "campaign in the same authorized store."
                ),
            },
        )
    expected_products = {
        str(value)
        for value in (item_group_ids or [])
        if str(value).strip()
    }
    if expected_products:
        replacement_products = {
            str(value)
            for value in context.db.execute(
                select(GmvmaxProductCampaignItemGroup.item_group_id).where(
                    GmvmaxProductCampaignItemGroup.workspace_id
                    == int(context.workspace_id),
                    GmvmaxProductCampaignItemGroup.auth_id
                    == int(context.auth_id),
                    GmvmaxProductCampaignItemGroup.advertiser_id
                    == str(context.advertiser_id),
                    GmvmaxProductCampaignItemGroup.store_id == str(store_id),
                    GmvmaxProductCampaignItemGroup.campaign_id
                    == str(replacement_campaign_id),
                )
            ).scalars()
            if value
        }
        if replacement_products != expected_products:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "GMVMAX_REPLACEMENT_PRODUCT_MISMATCH",
                    "message": (
                        "The paused replacement campaign does not own the same "
                        "product set as this create request."
                    ),
                },
            )
    return replacement


@router.post(
    "",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def create_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: CreateCampaignRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Create a GMV Max campaign (POST /campaign/gmv_max/create/) then fetch detail."""
    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=payload.advertiser_id,
        store_id=payload.store_id,
    )
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "GMVMAX_STORE_REQUIRED", "message": "store_id is required."},
        )
    payload = payload.model_copy(
        update={
            "advertiser_id": effective_advertiser_id,
            "store_id": effective_store_id,
        }
    )
    if not payload.idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "GMVMAX_CREATE_IDEMPOTENCY_KEY_REQUIRED",
                "message": "idempotency_key is required for campaign creation.",
            },
        )
    client_payload_sha256 = gmvmax_create_payload_sha256(
        payload,
        advertiser_id=effective_advertiser_id,
    )
    row = None
    creation_status = "SUCCEEDED"
    creation_warnings: list[dict[str, Any]] = []
    intent_already_succeeded = False
    mutation_manager = _manual_guard_mutation_lease(context)
    mutation = mutation_manager.__enter__()
    try:
        existing_create_intent = get_gmvmax_create_intent(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=effective_advertiser_id,
            store_id=effective_store_id,
            idempotency_key=payload.idempotency_key,
        )
        if existing_create_intent is not None:
            if (
                str(existing_create_intent.client_payload_sha256)
                != str(client_payload_sha256)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "GMVMAX_CREATE_IDEMPOTENCY_CONFLICT",
                        "message": (
                            "The idempotency key is already bound to a different "
                            "GMV Max create request."
                        ),
                    },
                )
            frozen_payload = (
                dict(existing_create_intent.request_json)
                if isinstance(existing_create_intent.request_json, Mapping)
                else {}
            )
            frozen_payload.update(
                {
                    "advertiser_id": effective_advertiser_id,
                    "store_id": effective_store_id,
                    "idempotency_key": str(
                        existing_create_intent.idempotency_key
                    ),
                }
            )
            payload = CreateCampaignRequest.model_validate(frozen_payload)
        else:
            payload = apply_approved_plan_defaults_to_create_payload(
                context.db,
                payload,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=effective_advertiser_id,
            )
        if (
            existing_create_intent is None
            or str(existing_create_intent.state or "").upper() == "PREPARED"
        ):
            _validate_replacement_campaign_for_create(
                context,
                replacement_campaign_id=payload.replacement_campaign_id,
                store_id=effective_store_id,
                item_group_ids=payload.item_group_ids,
            )
        mutation.assert_current(context.db)
        store_authorized_bc_id = await ensure_gmvmax_store_authorized(
            context.client,
            advertiser_id=effective_advertiser_id,
            target_store_id=payload.store_id,
            execution_guard=lambda: mutation.assert_current(context.db),
        )
        mutation.assert_current(context.db)
        row = await svc_create_campaign(
            context.db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=effective_advertiser_id,
            client=context.client,
            payload=payload,
            store_authorized_bc_id=store_authorized_bc_id,
            client_payload_sha256=client_payload_sha256,
            execution_guard=mutation.assert_current,
        )
        create_intent = get_gmvmax_create_intent(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=effective_advertiser_id,
            store_id=effective_store_id,
            idempotency_key=payload.idempotency_key,
        )
        intent_already_succeeded = bool(
            create_intent
            and str(create_intent.state or "").upper() == "SUCCEEDED"
        )
        automation = dict(payload.automation or {})
        if (
            row
            and getattr(row, "campaign_id", None)
            and not intent_already_succeeded
        ):
            await _call_tiktok(
                context.client.campaign_status_update,
                CampaignStatusUpdateRequest(
                    advertiser_id=effective_advertiser_id,
                    campaign_ids=[str(row.campaign_id)],
                    operation_status="DISABLE",
                ),
            )
            mutation.assert_current(context.db)
            _apply_local_campaign_operation_status(
                context,
                campaign_id=str(row.campaign_id),
                campaign=row,
                operation_status="DISABLE",
            )
            strategy = _get_or_create_local_strategy_config(context, str(row.campaign_id))
            desired_strategy_enabled = bool(automation.get("enabled", True))
            product_effective_prices = automation.get("product_effective_prices")
            if isinstance(product_effective_prices, Mapping):
                for item_group_id, effective_price in product_effective_prices.items():
                    context.db.execute(
                        text(
                            """
                            update ttb_products
                            set effective_price=:effective_price,
                                effective_price_source='manual_campaign_create',
                                effective_price_updated_at=current_timestamp(6)
                            where workspace_id=:workspace_id
                              and auth_id=:auth_id
                              and store_id=:store_id
                              and product_id=:product_id
                            """
                        ),
                        {
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "store_id": payload.store_id,
                            "product_id": str(item_group_id),
                            "effective_price": effective_price,
                        },
                    )
            # Keep Guard unable to act until the strategy, inherited exclusions,
            # replacement handoff, and remote campaign status are finalized as
            # one fenced completion boundary.
            strategy.enabled = False
            strategy.target_roi = payload.roas_bid or strategy.target_roi
            strategy.min_roi = automation.get("min_roi", 0.8)
            strategy.cooldown_minutes = int(automation.get("cooldown_minutes", 30) or 30)
            strategy.min_runtime_minutes_before_first_change = int(
                automation.get("min_runtime_minutes_before_first_change", 10) or 10
            )
            daily_spend_cap_cents = automation.get("daily_spend_cap_cents")
            if daily_spend_cap_cents is None and automation.get("daily_spend_cap") is not None:
                try:
                    daily_spend_cap_cents = int(round(float(automation.get("daily_spend_cap")) * 100))
                except (TypeError, ValueError):
                    daily_spend_cap_cents = None
            config = dict(strategy.config_json or {})
            config["creation_quarantine"] = {
                "enabled": True,
                "state": "FINALIZING",
                "reason": "campaign creation finalization is in progress",
                "remote_pause_confirmed": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            config["auto_heating_enabled"] = bool(automation.get("auto_heating_enabled", True))
            config["hermes_enabled"] = bool(automation.get("hermes_enabled", True))
            smart_guard = dict(config.get("smart_guard") or {})
            smart_guard.update(
                {
                    "enabled": bool(automation.get("smart_guard_enabled", True)),
                    "monitor_interval_minutes": int(automation.get("monitor_interval_minutes", 3) or 3),
                    "fast_monitor_interval_minutes": 1,
                    "slow_monitor_interval_minutes": 5,
                    "evaluation_window_minutes": int(automation.get("evaluation_window_minutes", 60) or 60),
                    "pause_cooldown_minutes": int(automation.get("cooldown_minutes", 30) or 30),
                    "min_spend_cents": int(automation.get("min_spend_cents", 300) or 300),
                    "min_roi": automation.get("min_roi", 0.8),
                    "daily_spend_cap_enabled": bool(automation.get("daily_spend_cap_enabled", True)),
                    "daily_spend_cap_cents": daily_spend_cap_cents,
                    "daily_budget_pacing": True,
                    "use_effective_product_price": True,
                    "product_effective_prices": product_effective_prices or {},
                    "catastrophic_pause_cooldown_minutes": int(
                        automation.get("catastrophic_pause_cooldown_minutes", 120) or 120
                    ),
                    "disable_strategy_on_catastrophic_stop": bool(
                        automation.get("disable_strategy_on_catastrophic_stop", False)
                    ),
                    "hermes_enabled": bool(
                        automation.get(
                            "hermes_enabled",
                            desired_strategy_enabled,
                        )
                    ),
                }
            )
            creative_guard = dict(config.get("creative_guard") or {})
            creative_guard.update(
                {
                    "enabled": bool(automation.get("creative_guard_enabled", True)),
                    "fast_monitor_interval_minutes": 1,
                    "monitor_interval_minutes": int(automation.get("monitor_interval_minutes", 3) or 3),
                    "slow_monitor_interval_minutes": 5,
                    "use_effective_product_price": True,
                    "product_effective_prices": product_effective_prices or {},
                    "product_card_reset": {"enabled": True, "recreate": True, "disable_old_strategy": True},
                    "no_order_budget_share_floor": "0.0",
                    "no_order_min_spend_cents": 300,
                }
            )
            config["smart_guard"] = smart_guard
            config["creative_guard"] = creative_guard
            strategy.config_json = config
            context.db.add(strategy)
            mark_gmvmax_create_intent(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=effective_advertiser_id,
                store_id=effective_store_id,
                idempotency_key=payload.idempotency_key,
                state="FINALIZING",
                campaign_id=str(row.campaign_id),
                result_json={
                    "campaign_id": str(row.campaign_id),
                    "post_processing": "in_progress",
                },
            )
            mutation.commit(context.db)
            inherited_exclusions = await _inherit_historical_creative_exclusions(
                context,
                campaign_id=str(row.campaign_id),
                store_id=str(payload.store_id),
                item_group_ids=payload.item_group_ids,
                strategy_id=int(strategy.id) if getattr(strategy, "id", None) else None,
                guard_config=creative_guard,
            )
            if inherited_exclusions.get("excluded"):
                mutation.commit(context.db)
            strategy.enabled = desired_strategy_enabled
            await _call_tiktok(
                context.client.campaign_status_update,
                CampaignStatusUpdateRequest(
                    advertiser_id=effective_advertiser_id,
                    campaign_ids=[str(row.campaign_id)],
                    operation_status="ENABLE",
                ),
            )
            mutation.assert_current(context.db)
            _apply_local_campaign_operation_status(
                context,
                campaign_id=str(row.campaign_id),
                campaign=row,
                operation_status="ENABLE",
            )
            if (
                payload.replacement_campaign_id
                and str(payload.replacement_campaign_id) != str(row.campaign_id)
            ):
                replacement_strategy = _get_local_strategy_config(
                    context,
                    str(payload.replacement_campaign_id),
                )
                if replacement_strategy is not None:
                    replacement_strategy.enabled = False
                    replacement_config = dict(
                        replacement_strategy.config_json or {}
                    )
                    replacement_config["superseded_by_campaign_id"] = str(
                        row.campaign_id
                    )
                    replacement_strategy.config_json = replacement_config
                    context.db.add(replacement_strategy)
            mark_gmvmax_create_intent(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=effective_advertiser_id,
                store_id=effective_store_id,
                idempotency_key=payload.idempotency_key,
                state="SUCCEEDED",
                campaign_id=str(row.campaign_id),
                result_json={
                    "campaign_id": str(row.campaign_id),
                    "post_processing": "complete",
                },
            )
            # The durable intent and local strategy leave quarantine in the
            # same fenced transaction. A crash before this commit therefore
            # leaves FINALIZING visible and blocks every ordinary mutation.
            finalized_config = dict(strategy.config_json or {})
            finalized_config.pop("creation_quarantine", None)
            strategy.config_json = finalized_config
            context.db.add(strategy)
            mutation.commit(context.db)
    except Exception as exc:  # noqa: BLE001
        if row is None or not getattr(row, "campaign_id", None):
            await _handle_tiktok_error(exc)
        if isinstance(exc, GmvMaxMutationFenceLost):
            context.db.rollback()
            raise

        context.db.rollback()
        campaign_id = str(row.campaign_id)
        quarantine_errors: list[str] = []
        remote_pause_confirmed = False
        try:
            mutation.assert_current(context.db)
            await _call_tiktok(
                context.client.campaign_status_update,
                CampaignStatusUpdateRequest(
                    advertiser_id=effective_advertiser_id,
                    campaign_ids=[campaign_id],
                    operation_status="DISABLE",
                ),
            )
            mutation.assert_current(context.db)
            _apply_local_campaign_operation_status(
                context,
                campaign_id=campaign_id,
                campaign=row,
                operation_status="DISABLE",
            )
            remote_pause_confirmed = True
        except GmvMaxMutationFenceLost:
            context.db.rollback()
            raise
        except Exception as quarantine_exc:  # noqa: BLE001
            context.db.rollback()
            quarantine_errors.append(str(quarantine_exc))
            logger.exception(
                "GMV Max create post-processing failed and remote quarantine "
                "could not be confirmed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": effective_advertiser_id,
                    "store_id": effective_store_id,
                    "campaign_id": campaign_id,
                },
            )

        try:
            strategy = _get_or_create_local_strategy_config(context, campaign_id)
            strategy.enabled = False
            strategy_config = dict(strategy.config_json or {})
            strategy_config["creation_quarantine"] = {
                "enabled": True,
                "state": (
                    "QUARANTINED"
                    if remote_pause_confirmed
                    else "QUARANTINE_PENDING"
                ),
                "reason": str(exc),
                "remote_pause_confirmed": remote_pause_confirmed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            strategy.config_json = strategy_config
            context.db.add(strategy)
            mark_gmvmax_create_intent(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=effective_advertiser_id,
                store_id=effective_store_id,
                idempotency_key=payload.idempotency_key,
                state=(
                    "QUARANTINED"
                    if remote_pause_confirmed
                    else "REMOTE_CREATED"
                ),
                campaign_id=campaign_id,
                result_json={
                    "post_processing": (
                        "quarantined"
                        if remote_pause_confirmed
                        else "quarantine_pending"
                    ),
                    "quarantine_pending": not remote_pause_confirmed,
                },
                error_json={
                    "post_processing_error": str(exc),
                    "quarantine_errors": quarantine_errors,
                },
            )
            mutation.commit(context.db)
        except Exception as persist_exc:  # noqa: BLE001
            context.db.rollback()
            quarantine_errors.append(str(persist_exc))
            logger.exception(
                "Failed to persist GMV Max create quarantine state",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": campaign_id,
                },
            )

        logger.exception(
            "GMV Max campaign was created but automation finalization failed; "
            "the campaign was quarantined instead of reporting a retryable create failure",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": effective_advertiser_id,
                "store_id": effective_store_id,
                "campaign_id": campaign_id,
                "remote_pause_confirmed": remote_pause_confirmed,
            },
        )
        creation_status = (
            "QUARANTINED"
            if remote_pause_confirmed
            else "QUARANTINE_PENDING"
        )
        creation_warnings.append(
            {
                "code": "GMVMAX_CREATE_POSTPROCESS_QUARANTINED",
                "message": (
                    "Campaign was created, but automation initialization was "
                    "incomplete. It has been paused and can be safely retried "
                    "with the same request."
                    if remote_pause_confirmed
                    else "Campaign was created, but automation initialization "
                    "and the safety pause need operator confirmation. Do not "
                    "create another campaign."
                ),
                "details": {
                    "campaign_id": campaign_id,
                    "post_processing_error": str(exc),
                    "quarantine_errors": quarantine_errors,
                },
            }
        )
    finally:
        mutation_manager.__exit__(*sys.exc_info())

    info_request = GMVMaxCampaignInfoRequest(
        advertiser_id=effective_advertiser_id, campaign_id=str(row.campaign_id)
    )
    try:
        info_response = await _call_tiktok(
            context.client.gmv_max_campaign_info,
            info_request,
        )
        campaign_info = info_response.data
        response_request_id = info_response.request_id
    except HTTPException:
        # TikTok can be eventually consistent immediately after a successful
        # create. The campaign and its local strategy are already committed,
        # so an optional detail lookup must not turn that success into a
        # retryable "create failed" response.
        logger.warning(
            "gmvmax campaign created but immediate detail lookup was unavailable",
            exc_info=True,
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": effective_advertiser_id,
                "campaign_id": str(row.campaign_id),
            },
        )
        campaign_info = _catalog_row_to_detail(row, "PRODUCT")
        response_request_id = None

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=[],
        request_id=response_request_id,
        creation_status=creation_status,
        idempotency_key=payload.idempotency_key,
        warnings=creation_warnings,
    )


@router.put(
    "/{campaign_id}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_admin)],
)
@router.put(
    "/campaigns/{campaign_id}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: UpdateCampaignRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Update a GMV Max campaign (POST /campaign/gmv_max/update/) and refresh detail."""
    existing_row = _load_campaign_action_source(context, campaign_id)
    if existing_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )
    _ensure_campaign_not_deleted(existing_row)

    with _manual_guard_mutation_lease(context) as mutation:
        existing_row = _load_campaign_action_source(context, campaign_id)
        if existing_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Campaign not found",
            )
        _ensure_campaign_not_deleted(existing_row)
        _ensure_campaign_not_creation_quarantined(
            context,
            str(campaign_id),
        )
        try:
            update_body = payload.to_client_body(campaign_id=campaign_id)
            row = await update_gmvmax_campaign(
                context.db,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                advertiser_id=context.advertiser_id,
                client=context.client,
                body=update_body,
                execution_guard=mutation.assert_current,
            )
            _apply_local_campaign_update_body(
                context,
                campaign=row,
                body=update_body,
            )
            mutation.commit(context.db)
        except (TTBApiError, TTBHttpError) as exc:
            await _handle_tiktok_error(exc)
            raise

        info_request = GMVMaxCampaignInfoRequest(
            advertiser_id=context.advertiser_id, campaign_id=str(row.campaign_id)
        )
        try:
            mutation.assert_current(context.db)
            info_response = await _call_tiktok(
                context.client.gmv_max_campaign_info, info_request
            )
            mutation.assert_current(context.db)
            # The final ownership assertion acquires durable lease rows with
            # SELECT ... FOR UPDATE. End that read transaction before the
            # context manager releases the same generations in its dedicated
            # session, otherwise MySQL waits on our own connection until 1205.
            mutation.commit(context.db)
        except (TTBApiError, TTBHttpError) as exc:
            await _handle_tiktok_error(exc)
            raise
    campaign_info = info_response.data

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=[],
        request_id=info_response.request_id,
    )


def _normalize_promo_types(types: Optional[List[str]]) -> list[str]:
    if not types:
        return ["LIVE", "PRODUCT"]

    normalized: list[str] = []
    for promo_type in types:
        value = str(promo_type).upper()
        if value in ("PRODUCT", "PRODUCT_GMV_MAX", "PRODUCT_GMV"):
            normalized.append("PRODUCT")
        elif value in ("LIVE", "LIVE_GMV_MAX", "LIVE_GMV"):
            normalized.append("LIVE")

    return sorted(set(normalized)) or ["LIVE", "PRODUCT"]


def _parse_iso8601_utc(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _apply_catalog_filters(
    stmt,
    model,
    *,
    store_filters: list[str] | None,
    campaign_ids: list[str] | None,
    campaign_name: str | None,
    primary_status: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
    include_deleted: bool,
):
    if store_filters:
        stmt = stmt.where(model.store_id.in_(store_filters))
    if campaign_ids:
        stmt = stmt.where(model.campaign_id.in_(campaign_ids))
    if campaign_name:
        stmt = stmt.where(model.campaign_name.ilike(f"%{campaign_name}%"))
    if primary_status:
        stmt = stmt.where(model.operation_status == str(primary_status))
    if start_time:
        stmt = stmt.where(model.create_time_utc >= start_time)
    if end_time:
        stmt = stmt.where(model.create_time_utc <= end_time)
    if not include_deleted:
        stmt = stmt.where(
            or_(
                model.secondary_status.is_(None),
                model.secondary_status != "CAMPAIGN_STATUS_DELETE",
            )
        )
    return stmt


def _format_datetime_utc(dt_value: Any) -> str | None:
    if dt_value is None:
        return None
    if isinstance(dt_value, str):
        return dt_value
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo:
            dt_value = dt_value.astimezone(timezone.utc)
        else:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value.isoformat().replace("+00:00", "Z")
    return None


def _format_decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _catalog_row_to_campaign(row: Mapping[str, Any]) -> GMVMaxCampaign:
    detail_json = row.get("detail_raw_json") if isinstance(row.get("detail_raw_json"), Mapping) else {}
    roas_bid_value = row.get("roas_bid")
    if roas_bid_value is None and isinstance(detail_json, Mapping):
        roas_bid_value = detail_json.get("roas_bid")

    payload: Dict[str, Any] = {
        "campaign_id": row.get("campaign_id"),
        "campaign_name": row.get("campaign_name"),
        "advertiser_id": row.get("advertiser_id"),
        "operation_status": row.get("operation_status"),
        "secondary_status": row.get("secondary_status"),
        "objective_type": row.get("objective_type"),
        "gmv_max_promotion_type": row.get("gmv_max_promotion_type"),
        "schedule_type": row.get("schedule_type"),
        "schedule_start_time": _format_datetime_utc(row.get("schedule_start_time_utc")),
        "schedule_end_time": _format_datetime_utc(row.get("schedule_end_time_utc")),
        "create_time": _format_datetime_utc(row.get("create_time_utc")),
        "modify_time": _format_datetime_utc(row.get("modify_time_utc")),
        "roas_bid": _format_decimal_str(roas_bid_value),
        "bid_type": detail_json.get("bid_type") if isinstance(detail_json, Mapping) else None,
        "target_roi_budget": detail_json.get("target_roi_budget") if isinstance(detail_json, Mapping) else None,
        "max_delivery_budget": detail_json.get("max_delivery_budget") if isinstance(detail_json, Mapping) else None,
        "performance": row.get("performance"),
    }

    return GMVMaxCampaign.model_validate(payload)


def _load_campaign_list_performance(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_filters: Sequence[str] | None,
    rows: Sequence[Mapping[str, Any]],
    start_date: date,
    end_date: date,
    advertiser_today: date,
) -> dict[str, dict[str, Any]]:
    """Aggregate one canonical fact per campaign/day for a campaign-list page."""

    campaign_ids_by_promotion: dict[str, set[str]] = {"PRODUCT": set(), "LIVE": set()}
    for row in rows:
        campaign_id = row.get("campaign_id")
        promotion_type = str(row.get("gmv_max_promotion_type") or "").upper()
        if campaign_id and promotion_type in campaign_ids_by_promotion:
            campaign_ids_by_promotion[promotion_type].add(str(campaign_id))

    metric_selects = []
    for daily_model, hourly_model, promotion_type in (
        (
            GmvmaxProductCampaignMetricsDaily,
            GmvmaxProductCampaignMetricsHourly,
            "PRODUCT",
        ),
        (
            GmvmaxLiveCampaignMetricsDaily,
            GmvmaxLiveCampaignMetricsHourly,
            "LIVE",
        ),
    ):
        campaign_ids = campaign_ids_by_promotion[promotion_type]
        if not campaign_ids:
            continue
        for model, stat_expr, source_priority, official_daily in (
            (daily_model, daily_model.stat_time_day, literal(2), True),
            (
                hourly_model,
                func.date(hourly_model.stat_time_hour),
                case(
                    (func.date(hourly_model.stat_time_hour) >= advertiser_today, 3),
                    else_=1,
                ),
                False,
            ),
        ):
            effective_source_priority = (
                case(
                    (model.source_observed_at.is_(None), 0),
                    else_=source_priority,
                )
                if official_daily
                else source_priority
            )
            stmt = (
                select(
                    model.campaign_id.label("campaign_id"),
                    stat_expr.label("stat_time_day"),
                    effective_source_priority.label("source_priority"),
                    func.coalesce(func.sum(model.cost_cents), 0).label(
                        "cost_cents"
                    ),
                    func.coalesce(
                        func.sum(model.gross_revenue_cents), 0
                    ).label("gross_revenue_cents"),
                )
                .where(
                    model.workspace_id == int(workspace_id),
                    model.auth_id == int(auth_id),
                    model.advertiser_id == str(advertiser_id),
                    model.campaign_id.in_(campaign_ids),
                    stat_expr >= start_date,
                    stat_expr <= end_date,
                )
                .group_by(model.campaign_id, stat_expr, effective_source_priority)
            )
            if store_filters:
                stmt = stmt.where(
                    model.store_id.in_([str(item) for item in store_filters])
                )
            metric_selects.append(stmt)

    if not metric_selects:
        return {}

    source = union_all(*metric_selects).subquery()
    candidates = db.execute(
        select(source).order_by(
            source.c.stat_time_day.asc(),
            source.c.campaign_id.asc(),
            source.c.source_priority.desc(),
        )
    ).mappings()
    canonical: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for metric_row in candidates:
        key = (
            str(metric_row["campaign_id"]),
            metric_row["stat_time_day"],
        )
        current = canonical.get(key)
        if current is None or int(metric_row["source_priority"] or 0) > int(
            current["source_priority"] or 0
        ):
            canonical[key] = metric_row

    totals: dict[str, dict[str, int]] = {}
    for metric_row in canonical.values():
        campaign_id = str(metric_row["campaign_id"])
        aggregate = totals.setdefault(
            campaign_id,
            {"cost_cents": 0, "gross_revenue_cents": 0},
        )
        aggregate["cost_cents"] += int(metric_row["cost_cents"] or 0)
        aggregate["gross_revenue_cents"] += int(
            metric_row["gross_revenue_cents"] or 0
        )

    return {
        campaign_id: {
            "cost": float(Decimal(values["cost_cents"]) / Decimal(100)),
            "gmv": float(Decimal(values["gross_revenue_cents"]) / Decimal(100)),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        for campaign_id, values in totals.items()
    }


@router.get(
    "",
    response_model=CampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_campaigns_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    gmv_max_promotion_types: Optional[List[str]] = Query(None),
    store_ids: Optional[List[str]] = Query(None),
    campaign_ids: Optional[List[str]] = Query(None),
    campaign_name: Optional[str] = Query(None),
    primary_status: Optional[str] = Query(None),
    creation_filter_start_time: Optional[str] = Query(None),
    creation_filter_end_time: Optional[str] = Query(None),
    fields: Optional[List[str]] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=50),
    advertiser_id: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    performance_start_date: Optional[date] = Query(None),
    performance_end_date: Optional[date] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignListResponse:
    """List GMV Max campaigns for this advertiser account from local cache (synced from /gmv_max/campaign/get/)."""

    adv, bound_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
    )
    page_value = page or 1
    page_size_value = page_size or 20

    store_filters = [str(item) for item in store_ids] if store_ids else None
    if store_filters:
        for requested_store_id in store_filters:
            _validate_bound_scope(context, store_id=requested_store_id)
    elif bound_store_id:
        store_filters = [bound_store_id]

    promotion_types = _normalize_promo_types(gmv_max_promotion_types)
    parsed_start_dt = _parse_iso8601_utc(creation_filter_start_time)
    parsed_end_dt = _parse_iso8601_utc(creation_filter_end_time)
    campaign_id_filters = [str(item) for item in campaign_ids] if campaign_ids else None

    def _stmt(model, promo: str):
        stmt = select(
            model.campaign_id.label("campaign_id"),
            model.campaign_name.label("campaign_name"),
            model.advertiser_id.label("advertiser_id"),
            model.operation_status.label("operation_status"),
            model.secondary_status.label("secondary_status"),
            model.objective_type.label("objective_type"),
            model.schedule_type.label("schedule_type"),
            model.schedule_start_time_utc.label("schedule_start_time_utc"),
            model.schedule_end_time_utc.label("schedule_end_time_utc"),
            model.create_time_utc.label("create_time_utc"),
            model.modify_time_utc.label("modify_time_utc"),
            model.roas_bid.label("roas_bid"),
            model.detail_raw_json.label("detail_raw_json"),
            literal(promo).label("gmv_max_promotion_type"),
            model.updated_at.label("updated_at"),
        ).where(
            model.workspace_id == int(workspace_id),
            model.auth_id == int(auth_id),
            model.advertiser_id == str(adv),
        )
        return _apply_catalog_filters(
            stmt,
            model,
            store_filters=store_filters,
            campaign_ids=campaign_id_filters,
            campaign_name=campaign_name,
            primary_status=primary_status,
            start_time=parsed_start_dt,
            end_time=parsed_end_dt,
            include_deleted=include_deleted,
        )

    stmts = []
    if "PRODUCT" in promotion_types:
        stmts.append(_stmt(GmvmaxProductCampaignCatalog, "PRODUCT"))
    if "LIVE" in promotion_types:
        stmts.append(_stmt(GmvmaxLiveCampaignCatalog, "LIVE"))

    if not stmts:
        return CampaignListResponse(
            items=[],
            page_info=PageInfo(
                page=page_value,
                page_size=page_size_value,
                total_number=0,
                total_page=0,
                has_more=False,
            ),
            request_id=None,
        )

    base = (stmts[0] if len(stmts) == 1 else union_all(*stmts)).subquery()
    total = context.db.execute(select(func.count()).select_from(base)).scalar_one()

    rows = (
        context.db.execute(
            select(base)
            .order_by(
                case((base.c.operation_status == "ENABLE", 0), else_=1),
                base.c.create_time_utc.desc(),
                base.c.campaign_id.desc(),
            )
            .offset((page_value - 1) * page_size_value)
            .limit(page_size_value)
        )
        .mappings()
        .all()
    )

    if bool(performance_start_date) != bool(performance_end_date):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="performance_start_date and performance_end_date must be provided together",
        )
    if performance_start_date and performance_end_date and performance_start_date > performance_end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="performance_start_date must be on or before performance_end_date",
        )
    if (
        performance_start_date
        and performance_end_date
        and (performance_end_date - performance_start_date).days > 366
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="campaign performance supports at most 367 days",
        )

    performance_by_campaign: dict[str, dict[str, Any]] = {}
    if performance_start_date and performance_end_date and rows:
        advertiser_today = _resolve_advertiser_today(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(adv),
        )
        performance_by_campaign = _load_campaign_list_performance(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(adv),
            store_filters=store_filters,
            rows=rows,
            start_date=performance_start_date,
            end_date=performance_end_date,
            advertiser_today=advertiser_today,
        )

    items = []
    for row in rows:
        payload = dict(row)
        campaign_id = str(payload.get("campaign_id") or "")
        payload["performance"] = performance_by_campaign.get(campaign_id)
        items.append(_catalog_row_to_campaign(payload))
    total_page = ceil(int(total) / page_size_value) if total else 0
    page_info = PageInfo(
        page=page_value,
        page_size=page_size_value,
        total_number=total,
        total_page=total_page,
        has_more=page_value < total_page,
    )
    return CampaignListResponse(items=items, page_info=page_info, request_id=None)


@router.get(
    "/{campaign_id:int}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: int = Path(...),
    advertiser_id: Optional[str] = Query(None),
    include_sessions: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Return campaign detail from cache and fetch sessions on demand when requested."""

    logger.info(
        "gmvmax campaign detail route hit",
        extra={"workspace_id": workspace_id, "auth_id": auth_id, "campaign_id": campaign_id},
    )

    if context.db is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="database unavailable")

    adv, _ = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
    )
    campaign_id_str = str(campaign_id)
    catalog_row = None
    catalog_promotion = None
    for catalog_model, promotion_type in (
        (GmvmaxProductCampaignCatalog, "PRODUCT"),
        (GmvmaxLiveCampaignCatalog, "LIVE"),
    ):
        stmt = (
            select(catalog_model)
            .where(catalog_model.workspace_id == int(workspace_id))
            .where(catalog_model.auth_id == int(auth_id))
            .where(catalog_model.campaign_id == campaign_id_str)
            .order_by(catalog_model.updated_at.desc())
            .limit(1)
        )
        if adv:
            stmt = stmt.where(catalog_model.advertiser_id == str(adv))
        catalog_row = context.db.execute(stmt).scalars().first()
        if catalog_row is not None:
            catalog_promotion = promotion_type
            break

    if catalog_row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="campaign not found in cache; trigger refresh first",
        )

    sessions: List[GMVMaxSession] = []
    sessions_page_info = None
    sessions_request_id: str | None = None
    if include_sessions:
        try:
            session_resp = await context.client.gmv_max_session_list(
                GMVMaxSessionListRequest(
                    advertiser_id=str(adv),
                    campaign_id=campaign_id_str,
                )
            )
            data = getattr(session_resp, "data", None)
            sessions = gmv_max_session_entries(data)
            if getattr(data, "page_info", None):
                sessions_page_info = PageInfo.model_validate(data.page_info)
            sessions_request_id = getattr(session_resp, "request_id", None)
        except Exception:  # noqa: BLE001
            logger.warning(
                "gmvmax session list fetch failed",
                exc_info=True,
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": campaign_id_str,
                    "advertiser_id": str(adv),
                },
            )

    campaign_info = _catalog_row_to_detail(
        catalog_row, catalog_promotion or "PRODUCT"
    )
    if (catalog_promotion or "PRODUCT") == "PRODUCT" and catalog_row.store_id:
        # The normalized relation is the authority for product ownership.  A
        # temporary session/list omission must not make a persisted campaign
        # appear unbound to API consumers.
        item_group_ids = _campaign_item_group_ids(
            context.db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(adv),
            store_id=str(catalog_row.store_id),
            campaign_id=campaign_id_str,
        )
        if item_group_ids:
            campaign_payload = campaign_info.model_dump(exclude_none=True)
            campaign_payload["item_group_ids"] = item_group_ids
            campaign_info = GMVMaxCampaignInfoData.model_validate(campaign_payload)

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=sessions if include_sessions else [],
        sessions_page_info=sessions_page_info,
        request_id=None,
        sessions_request_id=sessions_request_id,
    )


@router.post(
    "/campaigns/{campaign_id}/refresh",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def refresh_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: Optional[str] = Query(None),
    include_sessions: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Enqueue TikTok campaign detail/session refresh via Celery."""

    adv, _ = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
    )
    async_res = _enqueue_owned_task(
        context,
        task_name="gmvmax.fetch_campaign_detail",
        kwargs={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "advertiser_id": str(adv),
            "campaign_id": str(campaign_id),
            "include_sessions": include_sessions,
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.campaign_refresh enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": adv,
            "campaign_id": campaign_id,
            "task_id": async_res.id,
        },
    )

    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


# === GMV Max metrics ingestion & queries ===
# - enqueue TikTok report pulls and read cached daily/hourly metrics
@router.post(
    "/balance/sync",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_advertiser_balance(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: BalanceSyncRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Trigger advertiser balance sync via Celery (no direct TikTok GMV Max call here)."""

    normalized_adv, _ = _validate_bound_scope(
        context,
        advertiser_id=payload.advertiser_id,
        store_id=payload.store_id,
    )
    requested_bc = _normalize_identifier(payload.bc_id)
    bound_bc = _normalize_identifier(context.binding.bc_id)
    if requested_bc and bound_bc and requested_bc != bound_bc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "GMVMAX_BC_SCOPE_MISMATCH",
                "message": "bc_id is outside the bound GMV Max scope.",
            },
        )
    normalized_bc = requested_bc or bound_bc

    if not normalized_bc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="bc_id is required for balance sync")
    if not normalized_adv:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="advertiser_id is required for balance sync",
        )

    async_res = _enqueue_owned_task(
        context,
        task_name="gmvmax.sync_advertiser_balance",
        kwargs={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "bc_id": str(normalized_bc),
            "advertiser_id": str(normalized_adv),
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.sync_advertiser_balance enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "bc_id": normalized_bc,
            "advertiser_id": normalized_adv,
            "task_id": async_res.id,
        },
    )

    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


@router.post(
    "/{campaign_id}/metrics/sync",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
    include_in_schema=False,
)
@router.post(
    "/campaigns/{campaign_id}/metrics/sync",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_metrics_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxManualSyncRequest,
    advertiser_id: Optional[str] = Query(None),
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Trigger one authoritative account sync for all requested metric levels."""

    _validate_bound_scope(context, advertiser_id=advertiser_id)
    # A campaign-detail endpoint is path-scoped. Request-body filters must
    # never widen it to sibling campaigns; multi-campaign sync belongs to the
    # account-level /gmvmax/sync endpoint.
    campaign_ids = [str(campaign_id)]
    normalized_payload = GmvMaxManualSyncRequest(
        start_date=payload.start_date,
        end_date=payload.end_date,
        levels=payload.levels,
        campaign_ids=campaign_ids,
        item_group_ids=payload.item_group_ids,
    )
    return sync_gmvmax_manual(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        payload=normalized_payload,
        me=me,
        context=context,
    )


def _stable_pagination_sort_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _creative_metric_row_sort_key(row: Any) -> tuple[str, ...]:
    """Return the complete persisted creative fact key in display order."""

    return (
        _stable_pagination_sort_value(getattr(row, "stat_time_day", None)),
        _stable_pagination_sort_value(getattr(row, "workspace_id", None)),
        _stable_pagination_sort_value(getattr(row, "auth_id", None)),
        _stable_pagination_sort_value(getattr(row, "advertiser_id", None)),
        _stable_pagination_sort_value(getattr(row, "store_id", None)),
        _stable_pagination_sort_value(getattr(row, "campaign_id", None)),
        _stable_pagination_sort_value(getattr(row, "item_group_id", None)),
        _stable_pagination_sort_value(
            getattr(row, "creative_id", None) or getattr(row, "item_id", None)
        ),
        _stable_pagination_sort_value(getattr(row, "id", None)),
    )


def _serialized_creative_row_sort_key(entry: Mapping[str, Any]) -> tuple[str, ...]:
    dimensions = entry.get("dimensions") or {}
    return (
        _stable_pagination_sort_value(dimensions.get("stat_time_day")),
        _stable_pagination_sort_value(dimensions.get("campaign_id")),
        _stable_pagination_sort_value(dimensions.get("product_id")),
        _stable_pagination_sort_value(dimensions.get("creative_id")),
        _stable_pagination_sort_value(dimensions.get("shop_content_id")),
    )


async def _query_gmvmax_metrics(
    request: Request,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: Optional[str],
    store_id: Optional[str],
    level: str,
    start_date: Optional[Union[date, datetime, str]],
    end_date: Optional[Union[date, datetime, str]],
    advertiser_id: Optional[str],
    campaign_ids: Optional[List[str]],
    item_group_ids: Optional[List[str]],
    context: GMVMaxRouteContext,
) -> MetricsResponse:
    """Return GMV Max performance metrics for the requested campaign and level."""

    effective_advertiser_id, effective_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    default_end = _resolve_advertiser_today(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=effective_advertiser_id,
    )
    end = _normalize_date_value(end_date, field_name="end_date") or default_end
    start = _normalize_date_value(start_date, field_name="start_date") or (end - timedelta(days=6))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_date_range",
                "message": "start_date must be earlier than or equal to end_date.",
            },
        )

    clean_campaign_ids = _sanitize_id_list(campaign_ids)
    clean_item_group_ids = _sanitize_id_list(item_group_ids)
    try:
        requested_page = max(1, int(request.query_params.get("page") or 1))
        requested_page_size = int(request.query_params.get("page_size") or 50)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_METRICS_PAGINATION_INVALID",
                "message": "page and page_size must be integers.",
            },
        ) from exc
    if requested_page_size < 1 or requested_page_size > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_METRICS_PAGE_SIZE_INVALID",
                "message": "page_size must be between 1 and 1000.",
            },
        )

    level_param = (request.query_params.get("level") or level or "campaign").lower()
    try:
        level_value = GMVMaxReportLevel(level_param)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid GMV Max metrics level: {level_param}",
        )

    if level_value not in SUPPORTED_GMVMAX_METRIC_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid GMV Max metrics level: {level_param}",
        )

    if level_value is not GMVMaxReportLevel.OVERVIEW and not (
        clean_campaign_ids or campaign_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "missing_campaign_id",
                "message": "campaign_id is required unless level is OVERVIEW.",
            },
        )

    if level_value is GMVMaxReportLevel.OVERVIEW and not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "missing_store_id",
                "message": "store_id is required for OVERVIEW metrics.",
            },
        )
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )

    if not effective_advertiser_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_advertiser", "message": "advertiser_id is required"},
        )

    if level_value is GMVMaxReportLevel.PRODUCT and not (clean_campaign_ids and len(clean_campaign_ids) > 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Item level metrics requires at least 1 campaign_id filter.",
        )

    if level_value is GMVMaxReportLevel.CREATIVE and not (
        clean_campaign_ids and len(clean_campaign_ids) > 0
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Creative level metrics requires at least 1 campaign_id filter.",
        )

    db = context.db

    def _serialize_campaign_rows(rows: list[Any]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            spend_cents = int(row.spend_cents or 0)
            net_spend_cents = (
                int(row.net_cost_cents)
                if getattr(row, "net_cost_cents", None) is not None
                else spend_cents
            )
            revenue_cents = int(row.gross_revenue_cents or 0)
            cost_value = float(Decimal(spend_cents) / Decimal(100)) if spend_cents else 0.0
            net_cost_value = (
                float(Decimal(net_spend_cents) / Decimal(100))
                if net_spend_cents
                else 0.0
            )
            gross_value = float(Decimal(revenue_cents) / Decimal(100)) if revenue_cents else 0.0
            orders_value = int(row.orders or 0)
            roas_value: float | None = None
            if spend_cents > 0:
                roas_value = float(
                    Decimal(revenue_cents) / Decimal(spend_cents)
                )

            serialized.append(
                {
                    "metrics": {
                        "spend": cost_value,
                        "cost": cost_value,
                        "net_cost": net_cost_value,
                        "gross_revenue": gross_value,
                        "gmv": gross_value,
                        "orders": orders_value,
                        "roas": roas_value,
                        "roi": roas_value,
                    },
                    "dimensions": {
                        "campaign_id": row.campaign_id,
                        "stat_time_day": row.stat_time_day.isoformat(),
                    },
                }
            )
        return serialized

    def _serialize_product_rows(rows: list[Any]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            cost_cents = int(getattr(row, "cost_cents", 0) or 0)
            raw_net_cost_cents = getattr(row, "net_cost_cents", None)
            net_cost_cents = (
                int(raw_net_cost_cents)
                if raw_net_cost_cents is not None
                else cost_cents
            )
            revenue_cents = int(getattr(row, "gross_revenue_cents", 0) or 0)
            cost_value = float(Decimal(cost_cents) / Decimal(100))
            net_cost_value = float(Decimal(net_cost_cents) / Decimal(100))
            gross_value = float(Decimal(revenue_cents) / Decimal(100))
            orders_value = int(getattr(row, "orders", 0) or 0)
            roi_value = (
                float(Decimal(revenue_cents) / Decimal(cost_cents))
                if cost_cents > 0
                else None
            )
            stat_day = getattr(row, "stat_time_day", None)
            serialized.append(
                {
                    "metrics": {
                        "spend": cost_value,
                        "cost": cost_value,
                        "net_cost": net_cost_value,
                        "gross_revenue": gross_value,
                        "gmv": gross_value,
                        "orders": orders_value,
                        "impressions": int(getattr(row, "impressions", 0) or 0),
                        "clicks": int(getattr(row, "clicks", 0) or 0),
                        "roas": roi_value,
                        "roi": roi_value,
                    },
                    "dimensions": {
                        "campaign_id": str(row.campaign_id),
                        "item_group_id": str(row.item_group_id),
                        "product_id": str(row.item_group_id),
                        "stat_time_day": (
                            stat_day.isoformat()
                            if hasattr(stat_day, "isoformat")
                            else str(stat_day)
                        ),
                    },
                }
            )
        return serialized

    def _serialize_creative_rows(
        rows: list[Any],
        creative_asset_map: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            cost_cents = getattr(row, "cost_cents", None)
            net_cost_cents = getattr(row, "net_cost_cents", None)
            gross_cents = getattr(row, "gross_revenue_cents", None)
            spend_value = (
                float(Decimal(cost_cents or 0) / Decimal(100))
                if cost_cents is not None
                else float(getattr(row, "cost", 0) or 0)
            )
            net_spend_value = (
                float(Decimal(net_cost_cents or 0) / Decimal(100))
                if net_cost_cents is not None
                else float(getattr(row, "net_cost", 0) or 0)
            )
            gross_value = (
                float(Decimal(gross_cents or 0) / Decimal(100))
                if gross_cents is not None
                else float(getattr(row, "gross_revenue", 0) or 0)
            )
            impressions_value = int(getattr(row, "impressions", 0) or 0)
            clicks_value = int(getattr(row, "clicks", 0) or 0)
            orders_value = int(getattr(row, "orders", 0) or 0)
            roas_value: float | None = None
            if spend_value > 0:
                roas_value = gross_value / spend_value

            def _coerce_rate(attr: str) -> float | None:
                val = getattr(row, attr, None)
                return float(val) if val is not None else None

            creative_id_value = getattr(row, "creative_id", None) or getattr(row, "item_id", None)
            creative_asset = (
                (creative_asset_map or {}).get(str(creative_id_value))
                if creative_id_value is not None
                else None
            ) or {}

            metrics_payload = {
                "spend": spend_value,
                "cost": float(spend_value),
                "net_cost": float(net_spend_value),
                "gross_revenue": gross_value,
                "gmv": gross_value,
                "orders": orders_value,
                "impressions": impressions_value,
                "clicks": clicks_value,
                "roas": roas_value,
                "roi": roas_value,
                "product_impressions": getattr(row, "product_impressions", None),
                "product_clicks": getattr(row, "product_clicks", None),
                "product_click_rate": _coerce_rate("product_click_rate"),
                "ad_click_rate": _coerce_rate("ad_click_rate"),
                "ad_conversion_rate": _coerce_rate("ad_conversion_rate"),
                "ad_video_view_rate_2s": _coerce_rate("ad_video_view_rate_2s"),
                "ad_video_view_rate_6s": _coerce_rate("ad_video_view_rate_6s"),
                "ad_video_view_rate_p25": _coerce_rate("ad_video_view_rate_p25"),
                "ad_video_view_rate_p50": _coerce_rate("ad_video_view_rate_p50"),
                "ad_video_view_rate_p75": _coerce_rate("ad_video_view_rate_p75"),
                "ad_video_view_rate_p100": _coerce_rate("ad_video_view_rate_p100"),
                "creative_delivery_status": canonicalize_creative_delivery_status(
                    getattr(row, "creative_delivery_status", None)
                    or getattr(row, "creative_status", None)
                ),
            }
            metrics_payload.update(creative_asset)

            serialized.append(
                {
                    "metrics": {k: v for k, v in metrics_payload.items() if v is not None},
                    "dimensions": {
                        "campaign_id": row.campaign_id,
                        "creative_id": getattr(row, "creative_id", None) or getattr(row, "item_id", None),
                        "shop_content_id": getattr(row, "item_id", None) or getattr(row, "creative_id", None),
                        "product_id": getattr(row, "item_group_id", None),
                        "stat_time_day": (
                            row.stat_time_day.isoformat()
                            if isinstance(getattr(row, "stat_time_day", None), date)
                            else getattr(row, "stat_time_day", None).date().isoformat()
                        ),
                    },
                }
            )
        return serialized

    def _append_product_card_placeholders(
        serialized: list[dict[str, Any]],
        *,
        campaign_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        if not campaign_ids:
            return serialized
        existing_keys = {
            (
                str((entry.get("dimensions") or {}).get("campaign_id") or ""),
                str((entry.get("dimensions") or {}).get("product_id") or ""),
                str((entry.get("dimensions") or {}).get("creative_id") or ""),
            )
            for entry in serialized
        }
        rows = db.execute(
            text(
                """
                select campaign_id, item_group_id
                from gmvmax_product_campaign_item_groups
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id in :campaign_ids
                order by campaign_id, item_group_id
                """
            ).bindparams(bindparam("campaign_ids", expanding=True)),
            {
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": str(effective_advertiser_id),
                "store_id": str(effective_store_id),
                "campaign_ids": [str(item) for item in campaign_ids if item],
            },
        ).mappings().all()
        for row in rows:
            campaign_id_value = str(row.get("campaign_id") or "")
            item_group_id = str(row.get("item_group_id") or "")
            if clean_item_group_ids and item_group_id not in clean_item_group_ids:
                continue
            key = (campaign_id_value, item_group_id, "-1")
            if key in existing_keys:
                continue
            existing_keys.add(key)
            serialized.insert(
                0,
                {
                    "metrics": {
                        "title": "商品卡",
                        "item_id": "-1",
                        "shop_content_type": "PRODUCT_CARD",
                        "creative_delivery_status": "NOT_DELIVERYING",
                        "spend": 0.0,
                        "cost": 0.0,
                        "net_cost": 0.0,
                        "gross_revenue": 0.0,
                        "gmv": 0.0,
                        "orders": 0,
                        "impressions": 0,
                        "clicks": 0,
                    },
                    "dimensions": {
                        "campaign_id": campaign_id_value,
                        "creative_id": "-1",
                        "shop_content_id": "-1",
                        "product_id": item_group_id,
                        "stat_time_day": end.isoformat(),
                    },
                },
            )
        return serialized

    async def _load_creative_asset_map(rows: Sequence[Any]) -> dict[str, dict[str, Any]]:
        """Fetch video cover/preview metadata for creative rows without blocking metrics."""

        if not rows or not effective_advertiser_id or not effective_store_id:
            return {}
        store_authorized_bc_id = context.binding.bc_id
        if not store_authorized_bc_id:
            return {}

        creative_ids = sorted(
            {
                str(getattr(row, "creative_id", "") or getattr(row, "item_id", "") or "")
                for row in rows
                if str(getattr(row, "creative_id", "") or getattr(row, "item_id", "") or "")
                not in {"", "-1", "0"}
            }
        )
        item_group_ids = sorted(
            {
                str(getattr(row, "item_group_id", "") or "")
                for row in rows
                if str(getattr(row, "item_group_id", "") or "")
            }
        )
        asset_map: dict[str, dict[str, Any]] = {}
        if creative_ids:
            cached_rows = db.execute(
                text(
                    """
                    select id, workspace_id, auth_id, advertiser_id, store_id,
                           item_id, item_group_id, video_id, title,
                           local_preview_path, local_cover_path,
                           preview_content_type, cover_content_type, media_cache_status,
                           duration, identity_id, identity_type, identity_name
                    from gmvmax_creative_asset_cache
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and item_id in :creative_ids
                    """
                ).bindparams(bindparam("creative_ids", expanding=True)),
                {
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": str(effective_advertiser_id),
                    "store_id": str(effective_store_id),
                    "creative_ids": creative_ids,
                },
            ).mappings().all()
            for row in cached_rows:
                asset_map[str(row.get("item_id"))] = _creative_asset_payload_from_cache(row)
        # Asset metadata is maintained by the background creative asset sync.
        # Metrics reads must stay database-only so the data panel is fast and cannot
        # block on TikTok API latency or authorization edge cases.
        return asset_map

    rows: list[Any] = []
    summary: dict[str, Any] | None = None
    serialized_rows: list[dict[str, Any]] | None = None
    creative_asset_map: dict[str, dict[str, Any]] = {}
    advertiser_timezone = _resolve_advertiser_timezone_name(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(effective_advertiser_id),
    )
    freshness: MetricsFreshness | None = None

    try:
        if level_value is GMVMaxReportLevel.OVERVIEW:
            overview_selects = []
            for metric_model, stat_expr, official_daily in (
                (
                    GmvOverviewMetricsDaily,
                    GmvOverviewMetricsDaily.stat_time_day,
                    True,
                ),
                (
                    GmvOverviewMetricsHourly,
                    func.date(GmvOverviewMetricsHourly.stat_time_hour),
                    False,
                ),
            ):
                source_priority = (
                    literal(2)
                    if official_daily
                    else case((stat_expr >= default_end, 3), else_=1)
                )
                effective_source_priority = (
                    case(
                        (metric_model.source_observed_at.is_(None), 0),
                        else_=source_priority,
                    )
                    if official_daily
                    else source_priority
                )
                overview_stmt = (
                    select(
                        stat_expr.label("stat_time_day"),
                        effective_source_priority.label("source_priority"),
                        literal(metric_model.__tablename__).label("source_name"),
                        func.max(
                            func.coalesce(
                                metric_model.ingested_at,
                                metric_model.source_observed_at,
                            )
                        ).label("source_updated_at"),
                        func.sum(func.coalesce(metric_model.cost_cents, 0)).label(
                            "cost_cents"
                        ),
                        func.sum(metric_model.net_cost_cents).label(
                            "net_cost_cents"
                        ),
                        func.sum(
                            func.coalesce(metric_model.gross_revenue_cents, 0)
                        ).label("gross_revenue_cents"),
                        func.sum(func.coalesce(metric_model.orders, 0)).label(
                            "orders"
                        ),
                    )
                    .where(metric_model.workspace_id == workspace_id)
                    .where(metric_model.auth_id == auth_id)
                    .where(
                        metric_model.advertiser_id
                        == str(effective_advertiser_id)
                    )
                        .where(metric_model.store_id == str(effective_store_id))
                        .where(stat_expr >= start)
                        .where(stat_expr <= end)
                        .group_by(stat_expr, effective_source_priority)
                    )
                overview_selects.append(overview_stmt)

            overview_source = union_all(*overview_selects).subquery()
            overview_candidates = db.execute(
                select(overview_source).order_by(
                    overview_source.c.stat_time_day.asc(),
                    overview_source.c.source_priority.desc(),
                )
            ).all()
            canonical_overview: dict[date, Any] = {}
            for row in overview_candidates:
                current = canonical_overview.get(row.stat_time_day)
                row_priority = int(row.source_priority or 0)
                current_priority = (
                    int(current.source_priority or 0)
                    if current is not None
                    else -1
                )
                if current is None or row_priority > current_priority or (
                    row_priority == current_priority
                    and (row.source_updated_at or datetime.min)
                    > (current.source_updated_at or datetime.min)
                ):
                    canonical_overview[row.stat_time_day] = row
            overview_rows = list(canonical_overview.values())
            overview_source_name: str | None = None
            overview_source_row_count = 0
            if overview_rows:
                overview_source_name = ",".join(
                    sorted({str(row.source_name) for row in overview_rows})
                )
                overview_source_row_count = len(overview_rows)
                snapshot = SimpleNamespace(
                    cost_cents=sum(int(row.cost_cents or 0) for row in overview_rows),
                    net_cost_cents=sum(
                        int(row.net_cost_cents)
                        if row.net_cost_cents is not None
                        else int(row.cost_cents or 0)
                        for row in overview_rows
                    ),
                    gross_revenue_cents=sum(
                        int(row.gross_revenue_cents or 0) for row in overview_rows
                    ),
                    orders=sum(int(row.orders or 0) for row in overview_rows),
                    roi=None,
                    cost_per_order=None,
                    start_date=start,
                    end_date=end,
                    snapshot_at=max(
                        (
                            row.source_updated_at
                            for row in overview_rows
                            if row.source_updated_at is not None
                        ),
                        default=None,
                    ),
                )
            else:
                stmt = (
                    select(GmvOverviewSnapshot)
                    .where(GmvOverviewSnapshot.workspace_id == workspace_id)
                    .where(GmvOverviewSnapshot.auth_id == auth_id)
                    .where(
                        GmvOverviewSnapshot.advertiser_id
                        == str(effective_advertiser_id)
                    )
                    .where(GmvOverviewSnapshot.store_id == str(effective_store_id))
                    .where(
                        GmvOverviewSnapshot.snapshot_type.in_(
                            ("MANUAL", "SCHEDULED")
                        )
                    )
                    .where(GmvOverviewSnapshot.start_date == start)
                    .where(GmvOverviewSnapshot.end_date == end)
                    .order_by(GmvOverviewSnapshot.snapshot_at.desc())
                    .limit(1)
                )
                snapshot = db.execute(stmt).scalars().first()

            if snapshot is None:
                logger.info(
                    "gmvmax overview snapshot missing; falling back to campaign facts",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": str(effective_advertiser_id),
                        "store_id": str(effective_store_id),
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                    },
                )
                fact_selects = []
                for metric_model, stat_expr, official_daily in (
                    (
                        GmvmaxProductCampaignMetricsDaily,
                        GmvmaxProductCampaignMetricsDaily.stat_time_day,
                        True,
                    ),
                    (
                        GmvmaxLiveCampaignMetricsDaily,
                        GmvmaxLiveCampaignMetricsDaily.stat_time_day,
                        True,
                    ),
                    (
                        GmvmaxProductCampaignMetricsHourly,
                        func.date(GmvmaxProductCampaignMetricsHourly.stat_time_hour),
                        False,
                    ),
                    (
                        GmvmaxLiveCampaignMetricsHourly,
                        func.date(GmvmaxLiveCampaignMetricsHourly.stat_time_hour),
                        False,
                    ),
                ):
                    source_priority = (
                        literal(2)
                        if official_daily
                        else case((stat_expr >= default_end, 3), else_=1)
                    )
                    effective_source_priority = (
                        case(
                            (metric_model.source_observed_at.is_(None), 0),
                            else_=source_priority,
                        )
                        if official_daily
                        else source_priority
                    )
                    fact_stmt = (
                        select(
                            metric_model.campaign_id.label("campaign_id"),
                            stat_expr.label("stat_time_day"),
                            effective_source_priority.label("source_priority"),
                            literal(metric_model.__tablename__).label("source_name"),
                            func.max(metric_model.updated_at).label(
                                "source_updated_at"
                            ),
                            func.sum(
                                func.coalesce(metric_model.cost_cents, 0)
                            ).label("cost_cents"),
                            func.sum(metric_model.net_cost_cents).label(
                                "net_cost_cents"
                            ),
                            func.sum(
                                func.coalesce(
                                    metric_model.gross_revenue_cents, 0
                                )
                            ).label("gross_revenue_cents"),
                            func.sum(func.coalesce(metric_model.orders, 0)).label(
                                "orders"
                            ),
                        )
                        .where(metric_model.workspace_id == workspace_id)
                        .where(metric_model.auth_id == auth_id)
                        .where(
                            metric_model.advertiser_id
                            == str(effective_advertiser_id)
                        )
                        .where(metric_model.store_id == str(effective_store_id))
                        .where(stat_expr >= start)
                        .where(stat_expr <= end)
                        .group_by(
                            metric_model.campaign_id,
                            stat_expr,
                            effective_source_priority,
                        )
                    )
                    fact_selects.append(fact_stmt)
                fact_source = union_all(*fact_selects).subquery()
                fact_candidates = db.execute(
                    select(fact_source).order_by(
                        fact_source.c.stat_time_day.asc(),
                        fact_source.c.campaign_id.asc(),
                        fact_source.c.source_priority.desc(),
                    )
                ).all()
                canonical_facts: dict[tuple[str, date], Any] = {}
                for row in fact_candidates:
                    key = (str(row.campaign_id), row.stat_time_day)
                    current = canonical_facts.get(key)
                    row_priority = int(row.source_priority or 0)
                    current_priority = (
                        int(current.source_priority or 0)
                        if current is not None
                        else -1
                    )
                    if current is None or row_priority > current_priority or (
                        row_priority == current_priority
                        and (row.source_updated_at or datetime.min)
                        > (current.source_updated_at or datetime.min)
                    ):
                        canonical_facts[key] = row
                fact_rows = list(canonical_facts.values())
                if not fact_rows:
                    summary = None
                    serialized_rows = []
                    freshness = _metrics_freshness(
                        source="gmvmax_campaign_metrics_fallback",
                        age_seconds=None,
                        row_count=0,
                        max_age_seconds=900,
                        advertiser_timezone=advertiser_timezone,
                        start_date=start,
                        end_date=end,
                        advertiser_today=default_end,
                    )
                else:
                    cost_cents = sum(int(row.cost_cents or 0) for row in fact_rows)
                    net_cost_cents = sum(
                        int(row.net_cost_cents)
                        if row.net_cost_cents is not None
                        else int(row.cost_cents or 0)
                        for row in fact_rows
                    )
                    gross_cents = sum(
                        int(row.gross_revenue_cents or 0) for row in fact_rows
                    )
                    orders_value = sum(int(row.orders or 0) for row in fact_rows)
                    spend_value = float(Decimal(cost_cents) / Decimal(100))
                    net_spend_value = float(
                        Decimal(net_cost_cents) / Decimal(100)
                    )
                    gross_value = float(Decimal(gross_cents) / Decimal(100))
                    roi_value = (
                        float(Decimal(gross_cents) / Decimal(cost_cents))
                        if cost_cents > 0
                        else None
                    )
                    cpo_value = (
                        float(
                            Decimal(cost_cents)
                            / Decimal(orders_value)
                            / Decimal(100)
                        )
                        if orders_value > 0
                        else None
                    )
                    summary = {
                        "spend": spend_value,
                        "cost": spend_value,
                        "net_cost": net_spend_value,
                        "gmv": gross_value,
                        "gross_revenue": gross_value,
                        "orders": orders_value,
                        "cost_per_order": cpo_value,
                        "roas": roi_value,
                        "roi": roi_value,
                    }
                    serialized_rows = [
                        {
                            "metrics": dict(summary),
                            "dimensions": {
                                "advertiser_id": str(effective_advertiser_id),
                                "store_id": str(effective_store_id),
                                "start_date": start.isoformat(),
                                "end_date": end.isoformat(),
                                "stat_time_day": end.isoformat(),
                            },
                        }
                    ]
                    fact_ages = [
                        age
                        for age in (
                            _source_age_seconds(
                                context.db, row.source_updated_at
                            )
                            for row in fact_rows
                        )
                        if age is not None
                    ]
                    freshness = _metrics_freshness(
                        source="gmvmax_campaign_metrics_fallback",
                        age_seconds=min(fact_ages) if fact_ages else None,
                        row_count=len(fact_rows),
                        max_age_seconds=900,
                        advertiser_timezone=advertiser_timezone,
                        start_date=start,
                        end_date=end,
                        advertiser_today=default_end,
                    )
            else:
                spend_cents = int(snapshot.cost_cents or 0)
                net_spend_cents = (
                    int(snapshot.net_cost_cents)
                    if snapshot.net_cost_cents is not None
                    else spend_cents
                )
                gross_cents = int(snapshot.gross_revenue_cents or 0)
                orders_value = int(snapshot.orders or 0)

                spend_value = round(float(Decimal(spend_cents) / Decimal(100)), 2) if spend_cents else 0.0
                net_spend_value = round(float(Decimal(net_spend_cents) / Decimal(100)), 2) if net_spend_cents else 0.0
                gross_value = round(float(Decimal(gross_cents) / Decimal(100)), 2) if gross_cents else 0.0

                roi_value: float | None = None
                if snapshot.roi is not None:
                    roi_value = float(snapshot.roi)
                elif spend_cents and gross_cents:
                    roi_value = float(
                        Decimal(gross_cents) / Decimal(spend_cents)
                    )

                cpo_value: float | None = None
                if snapshot.cost_per_order is not None:
                    cpo_value = float(snapshot.cost_per_order)
                elif orders_value > 0 and spend_cents:
                    cpo_value = float(
                        (Decimal(spend_cents) / Decimal(orders_value))
                        / Decimal(100)
                    )

                if cpo_value is not None:
                    cpo_value = round(cpo_value, 4)
                if roi_value is not None:
                    roi_value = round(roi_value, 4)

                summary = {
                    "spend": spend_value,
                    "cost": spend_value,
                    "net_cost": net_spend_value,
                    "gmv": gross_value,
                    "gross_revenue": gross_value,
                    "orders": orders_value,
                    "cost_per_order": cpo_value,
                    "roas": roi_value,
                    "roi": roi_value,
                }

                serialized_rows = [
                    {
                        "metrics": {
                            "spend": spend_value,
                            "cost": spend_value,
                            "net_cost": net_spend_value,
                            "gmv": gross_value,
                            "gross_revenue": gross_value,
                            "orders": orders_value,
                            "cost_per_order": cpo_value,
                            "roas": roi_value,
                            "roi": roi_value,
                        },
                        "dimensions": {
                            "advertiser_id": str(effective_advertiser_id),
                            "store_id": str(effective_store_id),
                            "start_date": snapshot.start_date.isoformat() if snapshot.start_date else None,
                            "end_date": snapshot.end_date.isoformat() if snapshot.end_date else None,
                            "stat_time_day": (snapshot.end_date or snapshot.start_date or end or start).isoformat(),
                        },
                    }
                ]
                freshness = _metrics_freshness(
                    source=overview_source_name or "gmv_overview_snapshots",
                    age_seconds=_source_age_seconds(context.db, snapshot.snapshot_at),
                    row_count=overview_source_row_count or 1,
                    max_age_seconds=900,
                    advertiser_timezone=advertiser_timezone,
                    start_date=start,
                    end_date=end,
                    advertiser_today=default_end,
                )

        elif level_value is GMVMaxReportLevel.CAMPAIGN:
            campaign_filter_ids = clean_campaign_ids or ([str(campaign_id)] if campaign_id is not None else [])
            metric_selects = []
            for metric_model, stat_expr, official_daily in (
                (
                    GmvmaxProductCampaignMetricsDaily,
                    GmvmaxProductCampaignMetricsDaily.stat_time_day,
                    True,
                ),
                (
                    GmvmaxLiveCampaignMetricsDaily,
                    GmvmaxLiveCampaignMetricsDaily.stat_time_day,
                    True,
                ),
                (
                    GmvmaxProductCampaignMetricsHourly,
                    func.date(GmvmaxProductCampaignMetricsHourly.stat_time_hour),
                    False,
                ),
                (
                    GmvmaxLiveCampaignMetricsHourly,
                    func.date(GmvmaxLiveCampaignMetricsHourly.stat_time_hour),
                    False,
                ),
            ):
                source_priority = (
                    literal(2)
                    if official_daily
                    else case((stat_expr >= default_end, 3), else_=1)
                )
                effective_source_priority = (
                    case(
                        (metric_model.source_observed_at.is_(None), 0),
                        else_=source_priority,
                    )
                    if official_daily
                    else source_priority
                )
                metric_stmt = (
                    select(
                        metric_model.campaign_id.label("campaign_id"),
                        stat_expr.label("stat_time_day"),
                        effective_source_priority.label("source_priority"),
                        literal(metric_model.__tablename__).label("source_name"),
                        func.max(metric_model.updated_at).label("source_updated_at"),
                        func.sum(func.coalesce(metric_model.cost_cents, 0)).label("spend_cents"),
                        func.sum(metric_model.net_cost_cents).label("net_cost_cents"),
                        func.sum(metric_model.gross_revenue_cents).label("gross_revenue_cents"),
                        func.sum(metric_model.orders).label("orders"),
                    )
                    .where(metric_model.workspace_id == workspace_id)
                    .where(metric_model.auth_id == auth_id)
                    .where(metric_model.advertiser_id == str(effective_advertiser_id))
                    .where(metric_model.store_id == str(effective_store_id))
                    .where(metric_model.campaign_id.in_(campaign_filter_ids))
                    .where(stat_expr >= start)
                    .where(stat_expr <= end)
                    .group_by(
                        metric_model.campaign_id,
                        stat_expr,
                        effective_source_priority,
                    )
                )
                metric_selects.append(metric_stmt)
            metrics_source = union_all(*metric_selects).subquery()
            rows = (
                db.execute(
                    select(
                        metrics_source.c.campaign_id,
                        metrics_source.c.stat_time_day,
                        metrics_source.c.source_priority,
                        metrics_source.c.source_name,
                        func.max(metrics_source.c.source_updated_at).label("source_updated_at"),
                        func.sum(metrics_source.c.spend_cents).label("spend_cents"),
                        func.sum(metrics_source.c.net_cost_cents).label("net_cost_cents"),
                        func.sum(metrics_source.c.gross_revenue_cents).label("gross_revenue_cents"),
                        func.sum(metrics_source.c.orders).label("orders"),
                    )
                    .group_by(
                        metrics_source.c.campaign_id,
                        metrics_source.c.stat_time_day,
                        metrics_source.c.source_priority,
                        metrics_source.c.source_name,
                    )
                    .order_by(metrics_source.c.stat_time_day.asc())
                )
                .all()
            )
            deduped_rows: dict[tuple[str, date], Any] = {}
            for row in rows:
                key = (str(row.campaign_id), row.stat_time_day)
                current = deduped_rows.get(key)
                row_priority = int(row.source_priority or 0)
                current_priority = (
                    int(current.source_priority or 0)
                    if current is not None
                    else -1
                )
                if current is None or row_priority > current_priority or (
                    row_priority == current_priority
                    and (row.source_updated_at or datetime.min)
                    > (current.source_updated_at or datetime.min)
                ):
                    deduped_rows[key] = row
            rows = sorted(deduped_rows.values(), key=lambda row: (row.stat_time_day, str(row.campaign_id)))

            freshness_rows = [row for row in rows if row.stat_time_day == default_end]
            if not freshness_rows:
                freshness_rows = rows
            freshness_ages = [
                age
                for age in (
                    _source_age_seconds(context.db, row.source_updated_at)
                    for row in freshness_rows
                )
                if age is not None
            ]
            freshness_sources = sorted({str(row.source_name) for row in freshness_rows})
            freshness = _metrics_freshness(
                source=",".join(freshness_sources) or "gmvmax_campaign_metrics",
                age_seconds=min(freshness_ages) if freshness_ages else None,
                row_count=len(rows),
                max_age_seconds=900,
                advertiser_timezone=advertiser_timezone,
                start_date=start,
                end_date=end,
                advertiser_today=default_end,
            )

            totals = {
                "spend_cents": 0,
                "net_cost_cents": 0,
                "gross_revenue_cents": 0,
                "orders": 0,
            }
            for row in rows:
                totals["spend_cents"] += int(row.spend_cents or 0)
                totals["net_cost_cents"] += (
                    int(row.net_cost_cents)
                    if row.net_cost_cents is not None
                    else int(row.spend_cents or 0)
                )
                totals["gross_revenue_cents"] += int(row.gross_revenue_cents or 0)
                totals["orders"] += int(row.orders or 0)

            spend_total = Decimal(totals["spend_cents"]) / Decimal(100) if totals["spend_cents"] else Decimal(0)
            net_spend_total = (
                Decimal(totals["net_cost_cents"]) / Decimal(100)
                if totals["net_cost_cents"]
                else Decimal(0)
            )
            gmv_total = Decimal(totals["gross_revenue_cents"]) / Decimal(100) if totals["gross_revenue_cents"] else Decimal(0)
            summary = {
                "spend": float(spend_total),
                "cost": float(spend_total),
                "net_cost": float(net_spend_total),
                "gmv": float(gmv_total),
                "gross_revenue": float(gmv_total),
                "orders": totals["orders"],
                "roas": (
                    float(gmv_total / spend_total)
                    if spend_total > 0
                    else None
                ),
                "roi": (
                    float(gmv_total / spend_total)
                    if spend_total > 0
                    else None
                ),
            }
        elif level_value is GMVMaxReportLevel.PRODUCT:
            campaign_filter_ids = clean_campaign_ids or (
                [str(campaign_id)] if campaign_id is not None else []
            )
            product_selects = []
            for metric_model, stat_expr, official_daily in (
                (
                    GmvProductMetricsDaily,
                    GmvProductMetricsDaily.stat_time_day,
                    True,
                ),
                (
                    GmvProductMetricsHourly,
                    func.date(GmvProductMetricsHourly.stat_time_hour),
                    False,
                ),
            ):
                source_priority = (
                    literal(2)
                    if official_daily
                    else case((stat_expr >= default_end, 3), else_=1)
                )
                effective_source_priority = (
                    case(
                        (metric_model.source_observed_at.is_(None), 0),
                        else_=source_priority,
                    )
                    if official_daily
                    else source_priority
                )
                product_stmt = (
                    select(
                        metric_model.campaign_id.label("campaign_id"),
                        metric_model.item_group_id.label("item_group_id"),
                        stat_expr.label("stat_time_day"),
                        effective_source_priority.label("source_priority"),
                        literal(metric_model.__tablename__).label("source_name"),
                        func.max(
                            func.coalesce(
                                metric_model.ingested_at,
                                metric_model.source_observed_at,
                            )
                        ).label("source_updated_at"),
                        func.sum(func.coalesce(metric_model.cost_cents, 0)).label(
                            "cost_cents"
                        ),
                        func.sum(metric_model.net_cost_cents).label("net_cost_cents"),
                        func.sum(func.coalesce(metric_model.gross_revenue_cents, 0)).label(
                            "gross_revenue_cents"
                        ),
                        func.sum(func.coalesce(metric_model.orders, 0)).label("orders"),
                        func.sum(func.coalesce(metric_model.impressions, 0)).label(
                            "impressions"
                        ),
                        func.sum(func.coalesce(metric_model.clicks, 0)).label("clicks"),
                    )
                    .where(metric_model.workspace_id == workspace_id)
                    .where(metric_model.auth_id == auth_id)
                    .where(
                        metric_model.advertiser_id == str(effective_advertiser_id)
                    )
                    .where(metric_model.store_id == str(effective_store_id))
                    .where(metric_model.campaign_id.in_(campaign_filter_ids))
                    .where(stat_expr >= start)
                    .where(stat_expr <= end)
                    .group_by(
                        metric_model.campaign_id,
                        metric_model.item_group_id,
                        stat_expr,
                        effective_source_priority,
                    )
                )
                if clean_item_group_ids:
                    product_stmt = product_stmt.where(
                        metric_model.item_group_id.in_(clean_item_group_ids)
                    )
                product_selects.append(product_stmt)

            product_source = union_all(*product_selects).subquery()
            candidate_rows = db.execute(
                select(product_source).order_by(
                    product_source.c.stat_time_day.asc(),
                    product_source.c.campaign_id.asc(),
                    product_source.c.item_group_id.asc(),
                    product_source.c.source_priority.desc(),
                )
            ).all()
            # Daily and hourly tables contain alternative snapshots for the same
            # product-day. Select one canonical source instead of summing both.
            deduped_products: dict[tuple[str, str, date], Any] = {}
            for row in candidate_rows:
                key = (
                    str(row.campaign_id),
                    str(row.item_group_id),
                    row.stat_time_day,
                )
                current = deduped_products.get(key)
                row_priority = int(row.source_priority or 0)
                current_priority = (
                    int(current.source_priority or 0)
                    if current is not None
                    else -1
                )
                if current is None or row_priority > current_priority or (
                    row_priority == current_priority
                    and (row.source_updated_at or datetime.min)
                    > (current.source_updated_at or datetime.min)
                ):
                    deduped_products[key] = row
            rows = sorted(
                deduped_products.values(),
                key=lambda row: (
                    row.stat_time_day,
                    str(row.campaign_id),
                    str(row.item_group_id),
                ),
            )

            source_names = sorted({str(row.source_name) for row in rows})
            freshness_rows = [
                row for row in rows if row.stat_time_day == default_end
            ] or rows
            freshness_ages = [
                age
                for age in (
                    _source_age_seconds(context.db, row.source_updated_at)
                    for row in freshness_rows
                )
                if age is not None
            ]
            freshness = _metrics_freshness(
                source=",".join(source_names) or "gmv_product_metrics",
                age_seconds=min(freshness_ages) if freshness_ages else None,
                row_count=len(rows),
                max_age_seconds=900,
                advertiser_timezone=advertiser_timezone,
                start_date=start,
                end_date=end,
                advertiser_today=default_end,
            )
            total_cost_cents = sum(int(row.cost_cents or 0) for row in rows)
            total_net_cost_cents = sum(
                int(row.net_cost_cents)
                if row.net_cost_cents is not None
                else int(row.cost_cents or 0)
                for row in rows
            )
            total_revenue_cents = sum(
                int(row.gross_revenue_cents or 0) for row in rows
            )
            total_orders = sum(int(row.orders or 0) for row in rows)
            total_impressions = sum(int(row.impressions or 0) for row in rows)
            total_clicks = sum(int(row.clicks or 0) for row in rows)
            cost_total = Decimal(total_cost_cents) / Decimal(100)
            net_cost_total = Decimal(total_net_cost_cents) / Decimal(100)
            gmv_total = Decimal(total_revenue_cents) / Decimal(100)
            summary = {
                "spend": float(cost_total),
                "cost": float(cost_total),
                "net_cost": float(net_cost_total),
                "gmv": float(gmv_total),
                "gross_revenue": float(gmv_total),
                "orders": total_orders,
                "impressions": total_impressions,
                "clicks": total_clicks,
                "roas": (
                    float(gmv_total / cost_total)
                    if cost_total > 0
                    else None
                ),
                "roi": (
                    float(gmv_total / cost_total)
                    if cost_total > 0
                    else None
                ),
            }
        else:
            campaign_filter_ids = clean_campaign_ids or ([str(campaign_id)] if campaign_id is not None else [])
            # source_observed_at is freshness metadata, not a validity flag.
            # Legacy official daily facts predate that column and remain the
            # sole row for their persisted creative-day natural key.
            creative_stmt = (
                select(GmvmaxProductCreativeMetricsDaily)
                .where(GmvmaxProductCreativeMetricsDaily.workspace_id == workspace_id)
                .where(GmvmaxProductCreativeMetricsDaily.auth_id == auth_id)
                .where(GmvmaxProductCreativeMetricsDaily.advertiser_id == str(effective_advertiser_id))
                .where(GmvmaxProductCreativeMetricsDaily.store_id == str(effective_store_id))
                .where(GmvmaxProductCreativeMetricsDaily.campaign_id.in_(campaign_filter_ids))
                .where(GmvmaxProductCreativeMetricsDaily.stat_time_day >= start)
                .where(GmvmaxProductCreativeMetricsDaily.stat_time_day <= end)
                .order_by(
                    GmvmaxProductCreativeMetricsDaily.stat_time_day.asc(),
                    GmvmaxProductCreativeMetricsDaily.workspace_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.auth_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.advertiser_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.store_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.campaign_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.item_group_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.creative_id.asc(),
                    GmvmaxProductCreativeMetricsDaily.id.asc(),
                )
            )
            if clean_item_group_ids:
                creative_stmt = creative_stmt.where(
                    GmvmaxProductCreativeMetricsDaily.item_group_id.in_(clean_item_group_ids)
                )
            rows = list(db.execute(creative_stmt).scalars().all())

            realtime_rows: list[Any] = []
            latest_creative_batches: list[Mapping[str, Any]] = []
            if start <= default_end <= end:
                latest_batch_sql = text(
                    """
                    select b.campaign_id, b.snapshot_at,
                           b.source_observed_at, b.row_count
                    from gmv_creative_10min_batch_manifests b
                    join (
                        select campaign_id, max(snapshot_at) as snapshot_at
                        from gmv_creative_10min_batch_manifests
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id in :campaign_ids
                          and stat_time_day=:today
                          and complete=1
                        group by campaign_id
                    ) latest
                      on latest.campaign_id=b.campaign_id
                     and latest.snapshot_at=b.snapshot_at
                    where b.workspace_id=:workspace_id
                      and b.auth_id=:auth_id
                      and b.advertiser_id=:advertiser_id
                      and b.store_id=:store_id
                      and b.stat_time_day=:today
                      and b.complete=1
                    """
                ).bindparams(bindparam("campaign_ids", expanding=True))
                latest_creative_batches = list(
                    db.execute(
                        latest_batch_sql,
                        {
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "advertiser_id": str(effective_advertiser_id),
                            "store_id": str(effective_store_id),
                            "campaign_ids": campaign_filter_ids,
                            "today": default_end,
                        },
                    ).mappings().all()
                )
                realtime_sql = text(
                    """
                    select m.id, m.workspace_id, m.auth_id, m.advertiser_id,
                           m.store_id, m.campaign_id, m.creative_id, m.stat_time_day,
                           m.item_group_id,
                           coalesce(d.creative_delivery_status, m.creative_status, 'CANDIDATE') as creative_delivery_status,
                           m.cost_cents, m.net_cost_cents, m.orders, m.gross_revenue_cents,
                           m.impressions, m.clicks,
                           coalesce(d.product_impressions, m.impressions) as product_impressions,
                           coalesce(m.product_clicks, d.product_clicks) as product_clicks,
                           d.product_click_rate, m.ad_click_rate,
                           m.conversion_rate as ad_conversion_rate,
                           m.video_view_rate_2s as ad_video_view_rate_2s,
                           m.video_view_rate_6s as ad_video_view_rate_6s,
                           m.video_view_rate_25 as ad_video_view_rate_p25,
                           m.video_view_rate_50 as ad_video_view_rate_p50,
                           m.video_view_rate_75 as ad_video_view_rate_p75,
                           m.video_view_rate_100 as ad_video_view_rate_p100,
                           m.snapshot_at as updated_at
                    from gmv_creative_metrics_10min m
                    join (
                        select workspace_id, auth_id, advertiser_id, store_id,
                               campaign_id, stat_time_day,
                               max(snapshot_at) as snapshot_at
                        from gmv_creative_10min_batch_manifests
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id in :campaign_ids
                          and stat_time_day=:today
                          and complete=1
                        group by workspace_id, auth_id, advertiser_id, store_id,
                                 campaign_id, stat_time_day
                    ) latest
                      on latest.workspace_id=m.workspace_id
                     and latest.auth_id=m.auth_id
                     and latest.advertiser_id=m.advertiser_id
                     and latest.store_id=m.store_id
                     and latest.campaign_id=m.campaign_id
                     and latest.stat_time_day=m.stat_time_day
                     and latest.snapshot_at=m.snapshot_at
                    left join (
                        select campaign_id, item_group_id, creative_id, stat_time_day,
                               max(creative_delivery_status) as creative_delivery_status,
                               max(product_impressions) as product_impressions,
                               max(product_clicks) as product_clicks,
                               max(product_click_rate) as product_click_rate
                        from gmvmax_product_creative_metrics_daily
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id in :campaign_ids
                          and stat_time_day=:today
                        group by campaign_id, item_group_id, creative_id, stat_time_day
                    ) d
                      on d.campaign_id=m.campaign_id
                     and d.item_group_id=m.item_group_id
                     and d.creative_id=m.creative_id
                     and d.stat_time_day=m.stat_time_day
                    where m.workspace_id=:workspace_id
                      and m.auth_id=:auth_id
                      and m.advertiser_id=:advertiser_id
                      and m.store_id=:store_id
                      and m.campaign_id in :campaign_ids
                      and m.stat_time_day=:today
                    order by m.stat_time_day asc,
                             m.workspace_id asc,
                             m.auth_id asc,
                             m.advertiser_id asc,
                             m.store_id asc,
                             m.campaign_id asc,
                             m.item_group_id asc,
                             m.creative_id asc,
                             m.id asc
                    """
                ).bindparams(bindparam("campaign_ids", expanding=True))
                realtime_mappings = db.execute(
                    realtime_sql,
                    {
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": str(effective_advertiser_id),
                        "store_id": str(effective_store_id),
                        "campaign_ids": campaign_filter_ids,
                        "today": default_end,
                    },
                ).mappings().all()
                realtime_rows = [SimpleNamespace(**dict(row)) for row in realtime_mappings]
                if clean_item_group_ids:
                    allowed_item_groups = {str(value) for value in clean_item_group_ids}
                    realtime_rows = [
                        row
                        for row in realtime_rows
                        if not row.item_group_id or str(row.item_group_id) in allowed_item_groups
                    ]
                authoritative_campaign_ids = {
                    str(row.get("campaign_id"))
                    for row in latest_creative_batches
                    if row.get("campaign_id")
                }
                if authoritative_campaign_ids:
                    rows = [
                        row
                        for row in rows
                        if not (
                            getattr(row, "stat_time_day", None) == default_end
                            and str(getattr(row, "campaign_id", ""))
                            in authoritative_campaign_ids
                        )
                    ] + realtime_rows
            rows = sorted(rows, key=_creative_metric_row_sort_key)

            totals = {
                "spend": Decimal("0"),
                "net_cost": Decimal("0"),
                "gmv": Decimal("0"),
                "orders": 0,
                "impressions": 0,
                "clicks": 0,
            }
            for row in rows:
                spend_cents = getattr(row, "cost_cents", None)
                spend_value = (
                    Decimal(spend_cents) / Decimal(100)
                    if spend_cents is not None
                    else Decimal(getattr(row, "cost", 0) or 0)
                )
                raw_net_cost_cents = getattr(row, "net_cost_cents", None)
                net_cost_value = (
                    Decimal(raw_net_cost_cents) / Decimal(100)
                    if raw_net_cost_cents is not None
                    else spend_value
                )
                gross_cents = getattr(row, "gross_revenue_cents", None)
                gross_value = (
                    Decimal(gross_cents) / Decimal(100)
                    if gross_cents is not None
                    else Decimal(getattr(row, "gross_revenue", 0) or 0)
                )
                totals["spend"] += spend_value
                totals["net_cost"] += net_cost_value
                totals["gmv"] += gross_value
                totals["orders"] += int(getattr(row, "orders", 0) or 0)
                totals["impressions"] += int(getattr(row, "impressions", 0) or 0)
                totals["clicks"] += int(getattr(row, "clicks", 0) or 0)

            creative_updated_values = [
                getattr(row, "updated_at", None)
                for row in rows
                if getattr(row, "updated_at", None) is not None
                and getattr(row, "stat_time_day", None) == default_end
            ]
            creative_updated_values.extend(
                row.get("source_observed_at") or row.get("snapshot_at")
                for row in latest_creative_batches
                if row.get("source_observed_at") or row.get("snapshot_at")
            )
            if not creative_updated_values:
                creative_updated_values = [
                    getattr(row, "updated_at", None)
                    for row in rows
                    if getattr(row, "updated_at", None) is not None
                ]
            creative_ages = [
                age
                for age in (
                    _source_age_seconds(context.db, updated_at)
                    for updated_at in creative_updated_values
                )
                if age is not None
            ]
            has_daily_rows = any(
                isinstance(row, GmvmaxProductCreativeMetricsDaily) for row in rows
            )
            if latest_creative_batches and has_daily_rows:
                creative_source = (
                    "gmvmax_product_creative_metrics_daily+gmv_creative_metrics_10min"
                )
            elif latest_creative_batches:
                creative_source = "gmv_creative_metrics_10min"
            else:
                creative_source = "gmvmax_product_creative_metrics_daily"
            freshness = _metrics_freshness(
                source=creative_source,
                age_seconds=min(creative_ages) if creative_ages else None,
                row_count=len(rows),
                max_age_seconds=600,
                advertiser_timezone=advertiser_timezone,
                start_date=start,
                end_date=end,
                advertiser_today=default_end,
            )

            summary = {
                "spend": float(totals["spend"]),
                "cost": float(totals["spend"]),
                "net_cost": float(totals["net_cost"]),
                "gmv": float(totals["gmv"]),
                "gross_revenue": float(totals["gmv"]),
                "orders": totals["orders"],
                "impressions": totals["impressions"],
                "clicks": totals["clicks"],
                "roas": (
                    float(totals["gmv"] / totals["spend"])
                    if totals["spend"] > 0
                    else None
                ),
                "roi": (
                    float(totals["gmv"] / totals["spend"])
                    if totals["spend"] > 0
                    else None
                ),
            }
            creative_asset_map = await _load_creative_asset_map(rows)

    except HTTPException as exc:  # noqa: PERF203 - explicit passthrough
        if (
            level_value is GMVMaxReportLevel.OVERVIEW
            and exc.status_code == status.HTTP_404_NOT_FOUND
            and isinstance(exc.detail, str)
            and "campaign not found in cache" in exc.detail
        ):
            logger.info(
                "gmvmax overview metrics fallback on missing campaign cache",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "store_id": effective_store_id,
                    "advertiser_id": effective_advertiser_id,
                    "start_date": start,
                    "end_date": end,
                },
            )
            return _empty_overview_metrics_response()

        raise

    if serialized_rows is None and level_value is GMVMaxReportLevel.CAMPAIGN:
        serialized_rows = _serialize_campaign_rows(rows)
    elif serialized_rows is None and level_value is GMVMaxReportLevel.PRODUCT:
        serialized_rows = _serialize_product_rows(rows)
    elif serialized_rows is None:
        serialized_rows = _serialize_creative_rows(rows, creative_asset_map)
        if level_value is GMVMaxReportLevel.CREATIVE:
            creative_campaign_ids = clean_campaign_ids or ([str(campaign_id)] if campaign_id is not None else [])
            serialized_rows = _append_product_card_placeholders(
                serialized_rows,
                campaign_ids=creative_campaign_ids,
            )
            serialized_rows.sort(key=_serialized_creative_row_sort_key)

    total_rows = len(serialized_rows)
    offset = (requested_page - 1) * requested_page_size
    paged_rows = serialized_rows[offset : offset + requested_page_size]
    total_pages = ceil(total_rows / requested_page_size) if total_rows else 0
    has_next = requested_page < total_pages

    return {
        "report": {
            "list": paged_rows,
            "page_info": {
                "page": requested_page,
                "page_size": requested_page_size,
                "total_number": total_rows,
                "total_page": total_pages,
                "cursor": None,
                "has_more": has_next,
                "has_next": has_next,
            },
            "summary": summary,
        },
        "request_id": None,
        "freshness": freshness,
    }


@router.get(
    "/hermes/daily-reports",
    dependencies=[Depends(require_tenant_member)],
)
async def list_hermes_daily_reports_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: Optional[str] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=60),
    limit: Optional[int] = Query(None, ge=1, le=60),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> dict[str, Any]:
    """Return saved Hermes daily ad reports for the current GMV Max scope."""

    effective_advertiser_id, bound_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    effective_store_id = str(bound_store_id or "").strip()
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_scope", "message": "advertiser_id and store_id are required"},
        )

    effective_page_size = int(page_size if page_size is not None else limit if limit is not None else 14)
    scope_params = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "advertiser_id": effective_advertiser_id,
        "store_id": effective_store_id,
    }
    total = context.db.execute(
        text(
            """
            select count(*)
            from gmv_hermes_ad_daily_reports
            where workspace_id = :workspace_id
              and auth_id = :auth_id
              and advertiser_id = :advertiser_id
              and store_id = :store_id
            """
        ),
        scope_params,
    ).scalar_one()
    rows = context.db.execute(
        text(
            """
            select
                id,
                workspace_id,
                auth_id,
                advertiser_id,
                store_id,
                report_date,
                advertiser_timezone,
                report_type,
                status,
                input_json,
                response_json,
                report_markdown,
                recommendation_json,
                hermes_response_id,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                error_message,
                created_at,
                updated_at
            from gmv_hermes_ad_daily_reports
            where workspace_id = :workspace_id
              and auth_id = :auth_id
              and advertiser_id = :advertiser_id
              and store_id = :store_id
            order by report_date desc, id desc
            limit :limit offset :offset
            """
        ),
        {
            **scope_params,
            "limit": effective_page_size,
            "offset": (int(page) - 1) * effective_page_size,
        },
    ).mappings().all()

    reports = [_serialize_hermes_daily_report(row) for row in rows]
    report_ids = [int(report["id"]) for report in reports if report.get("id") is not None]
    if report_ids:
        decision_rows = context.db.execute(
            text(
                """
                select source_report_id, item_group_id, effective_date, status,
                       confidence, params_json, decision_json, updated_at
                from gmv_hermes_ad_plan_defaults
                where source_report_id in :report_ids
                order by effective_date desc, item_group_id asc, id asc
                """
            ).bindparams(bindparam("report_ids", expanding=True)),
            {"report_ids": tuple(report_ids)},
        ).mappings().all()
        decisions_by_report: dict[int, list[dict[str, Any]]] = {}
        for row in decision_rows:
            source_report_id = int(row.get("source_report_id") or 0)
            updated_at = row.get("updated_at")
            effective_date = row.get("effective_date")
            decisions_by_report.setdefault(source_report_id, []).append(
                {
                    "item_group_id": row.get("item_group_id") or "",
                    "effective_date": effective_date.isoformat()
                    if hasattr(effective_date, "isoformat")
                    else effective_date,
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "params": _safe_json_column(row.get("params_json")) or {},
                    "decision": _safe_json_column(row.get("decision_json")) or {},
                    "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
                }
            )
        for report in reports:
            report["plan_defaults"] = decisions_by_report.get(int(report.get("id") or 0), [])
    return {
        "list": reports,
        "latest": reports[0] if reports else None,
        "page": int(page),
        "page_size": effective_page_size,
        "total": int(total or 0),
        "scope": {
            "workspace_id": int(workspace_id),
            "provider": normalize_provider(provider),
            "auth_id": int(auth_id),
            "advertiser_id": effective_advertiser_id,
            "store_id": effective_store_id,
        },
    }


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def query_gmvmax_metrics_root_provider(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: Optional[str] = Query(None),
    store_id: Optional[str] = Query(None),
    level: str = Query("campaign"),
    start_date: Optional[Union[date, datetime, str]] = Query(None),
    end_date: Optional[Union[date, datetime, str]] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    campaign_ids: Optional[List[str]] = Query(None),
    item_group_ids: Optional[List[str]] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> MetricsResponse:
    logger.info(
        "gmvmax metrics overview route hit",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "store_id": store_id,
            "level": level,
        },
    )
    level_param = (request.query_params.get("level") or level or "campaign").lower()
    try:
        level_value = GMVMaxReportLevel(level_param)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid GMV Max metrics level: {level_param}",
        )

    end = _normalize_date_value(end_date, field_name="end_date") or date.today()
    start = _normalize_date_value(start_date, field_name="start_date") or (
        end - timedelta(days=6)
    )
    effective_store_id = store_id or context.store_id
    effective_advertiser_id = advertiser_id or context.advertiser_id

    try:
        return await _query_gmvmax_metrics(
            request,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            store_id=store_id,
            level=level,
            start_date=start_date,
            end_date=end_date,
            advertiser_id=advertiser_id,
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            context=context,
        )
    except HTTPException as exc:  # noqa: PERF203 - explicit passthrough
        if (
            level_value is GMVMaxReportLevel.OVERVIEW
            and exc.status_code == status.HTTP_404_NOT_FOUND
            and isinstance(exc.detail, str)
            and "campaign not found in cache" in exc.detail
        ):
            logger.info(
                "gmvmax overview metrics fallback on missing campaign cache",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "store_id": effective_store_id,
                    "advertiser_id": effective_advertiser_id,
                    "start_date": start,
                    "end_date": end,
                },
            )
            return _empty_overview_metrics_response()

        raise


@router.get(
    "/{campaign_id:int}/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
@router.get(
    "/campaigns/{campaign_id:int}/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def query_gmvmax_metrics_provider(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: int = Path(...),
    store_id: Optional[str] = Query(None),
    level: str = Query("campaign"),
    start_date: Optional[Union[date, datetime, str]] = Query(None),
    end_date: Optional[Union[date, datetime, str]] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    campaign_ids: Optional[List[str]] = Query(None),
    item_group_ids: Optional[List[str]] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> MetricsResponse:
    """Return GMV Max performance metrics for the requested campaign and level."""
    logger.info(
        "gmvmax metrics campaign route hit",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "store_id": store_id,
            "level": level,
        },
    )
    campaign_id_str = str(campaign_id)
    level_param = (request.query_params.get("level") or level or "campaign").lower()
    try:
        level_value = GMVMaxReportLevel(level_param)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid GMV Max metrics level: {level_param}",
        )

    end = _normalize_date_value(end_date, field_name="end_date") or date.today()
    start = _normalize_date_value(start_date, field_name="start_date") or (
        end - timedelta(days=6)
    )
    effective_store_id = store_id or context.store_id
    effective_advertiser_id = advertiser_id or context.advertiser_id

    try:
        return await _query_gmvmax_metrics(
            request,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id_str,
            store_id=store_id,
            level=level,
            start_date=start_date,
            end_date=end_date,
            advertiser_id=advertiser_id,
            campaign_ids=[campaign_id_str],
            item_group_ids=item_group_ids,
            context=context,
        )
    except HTTPException as exc:  # noqa: PERF203 - explicit passthrough
        if (
            level_value is GMVMaxReportLevel.OVERVIEW
            and exc.status_code == status.HTTP_404_NOT_FOUND
            and isinstance(exc.detail, str)
            and "campaign not found in cache" in exc.detail
        ):
            logger.info(
                "gmvmax overview metrics fallback on missing campaign cache",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "store_id": effective_store_id,
                    "advertiser_id": effective_advertiser_id,
                    "start_date": start,
                    "end_date": end,
                },
            )
            return _empty_overview_metrics_response()

        raise



# === GMV Max actions, creative heating, and strategy ===
# - campaign status/strategy actions and logs
# - creative heating triggers and evaluation
# - bid recommendation previews and session strategy updates
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _coerce_decimal(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


def _is_seed_creative(
    *,
    orders: int | float | None,
    roas: float | None,
    spend: float | None,
    min_conversions: int,
    min_roas: float,
    min_spend: float,
) -> bool:
    conversions_value = int(orders or 0)
    spend_value = float(spend or 0)
    roas_value = float(roas or 0)
    return (
        conversions_value >= min_conversions
        and spend_value >= min_spend
        and roas_value >= min_roas
    )


def _serialize_heating_row(row: Any) -> CreativeHeatingRecord:
    payload: Dict[str, Any] = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "provider": "tiktok-business",
        "auth_id": row.auth_id,
        "campaign_id": row.campaign_id,
        "creative_id": row.creative_id,
        "creative_name": getattr(row, "creative_name", None),
        "product_id": getattr(row, "product_id", None),
        "item_id": getattr(row, "item_id", None),
        "mode": getattr(row, "mode", None),
        "target_daily_budget": _to_float(getattr(row, "target_daily_budget", None)),
        "budget_delta": _to_float(getattr(row, "budget_delta", None)),
        "currency": getattr(row, "currency", None),
        "max_duration_minutes": getattr(row, "max_duration_minutes", None),
        "note": getattr(row, "note", None),
        "status": getattr(row, "status", "PENDING"),
        "last_action_type": getattr(row, "last_action_type", None),
        "last_action_time": getattr(row, "last_action_at", None),
        "last_error": getattr(row, "last_error", None),
        "evaluation_window_minutes": getattr(row, "evaluation_window_minutes", 60),
        "min_clicks": getattr(row, "min_clicks", None),
        "min_ctr": _to_float(getattr(row, "min_ctr", None)),
        "min_gross_revenue": _to_float(getattr(row, "min_gross_revenue", None)),
        "auto_stop_enabled": bool(getattr(row, "auto_stop_enabled", True)),
        "is_heating_active": bool(getattr(row, "is_heating_active", False)),
        "last_evaluated_at": getattr(row, "last_evaluated_at", None),
        "last_evaluation_result": getattr(row, "last_evaluation_result", None),
    }
    return CreativeHeatingRecord.model_validate(payload)


def _extract_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
        return str(detail)
    return str(detail)


def _parse_action_log_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _guard_event_creative_id(request_payload: Mapping[str, Any]) -> str | None:
    body = request_payload.get("body")
    if isinstance(body, Mapping):
        item_list = body.get("item_list")
        if isinstance(item_list, Sequence) and not isinstance(item_list, (str, bytes)):
            for item in item_list:
                if isinstance(item, Mapping):
                    candidate = item.get("item_id") or item.get("creative_id")
                    if candidate:
                        return str(candidate)
    for section_name in ("decision", "threshold_context", "context"):
        section = request_payload.get(section_name)
        if not isinstance(section, Mapping):
            continue
        context_payload = section.get("context") if section_name == "decision" else section
        if isinstance(context_payload, Mapping):
            candidate = context_payload.get("creative_id") or context_payload.get("item_id")
            if candidate:
                return str(candidate)
    candidate = request_payload.get("creative_id") or request_payload.get("item_id")
    return str(candidate) if candidate else None


def _serialize_guard_event_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    request_payload = _parse_action_log_json(row.get("request_json"))
    response_payload = _parse_action_log_json(row.get("response_json"))
    manual_payload = request_payload.get("manual")
    if not isinstance(manual_payload, Mapping):
        manual_payload = {}

    metric_summary = {
        key: value
        for key, value in {
            "spend": _to_float(row.get("cost_cents")) / 100
            if row.get("cost_cents") is not None
            else None,
            "gmv": _to_float(row.get("gross_revenue_cents")) / 100
            if row.get("gross_revenue_cents") is not None
            else None,
            "orders": row.get("orders"),
            "roas": _to_float(row.get("roi")),
        }.items()
        if value is not None
    }
    before_value = request_payload.get("before")
    if not isinstance(before_value, Mapping):
        before_value = metric_summary
    after_value = response_payload.get("after")
    if not isinstance(after_value, Mapping):
        after_value = {"result": row.get("result")} if row.get("result") else {}

    event_type = str(row.get("event_type") or "").upper()
    operator = manual_payload.get("performed_by") or request_payload.get("actor")
    if not operator:
        operator = {
            "CREATIVE_GUARD": "素材守护",
            "SMART_GUARD": "智能守护",
            "HERMES_DECISION": "Hermes",
        }.get(event_type, "系统")

    return {
        "id": row.get("id"),
        "campaign_id": row.get("campaign_id"),
        "creative_id": _guard_event_creative_id(request_payload),
        "event_type": row.get("event_type"),
        "action_type": row.get("action"),
        "reason": row.get("reason"),
        "before": dict(before_value),
        "after": dict(after_value),
        "before_value": dict(before_value),
        "after_value": dict(after_value),
        "operator": str(operator),
        "result": row.get("result"),
        "error_message": row.get("error_message"),
        "cost_cents": row.get("cost_cents"),
        "gross_revenue_cents": row.get("gross_revenue_cents"),
        "orders": row.get("orders"),
        "roi": _to_float(row.get("roi")),
        "timestamp": row.get("created_at"),
        "created_at": row.get("created_at"),
    }


def _list_guard_action_logs(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    limit: int,
    offset: int,
    sort: str,
) -> tuple[list[Dict[str, Any]], int]:
    params = {
        "workspace_id": int(context.workspace_id),
        "auth_id": int(context.auth_id),
        "campaign_id": str(campaign_id),
        "limit": int(limit),
        "offset": int(offset),
    }
    total = int(
        context.db.execute(
            text(
                """
                select count(*)
                from gmv_campaign_guard_events
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and campaign_id=:campaign_id
                """
            ),
            params,
        ).scalar_one()
        or 0
    )
    direction = "asc" if str(sort or "").strip().lower() in {"timestamp", "created_at"} else "desc"
    rows = context.db.execute(
        text(
            f"""
            select id, workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   strategy_id, event_type, action, reason, result,
                   cost_cents, gross_revenue_cents, orders, roi,
                   request_json, response_json, error_message, created_at
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
            order by created_at {direction}, id {direction}
            limit :limit offset :offset
            """
        ),
        params,
    ).mappings().all()
    return [_serialize_guard_event_row(row) for row in rows], total


async def _apply_creative_heating_action(
    *,
    context: GMVMaxRouteContext,
    campaign_id: str,
    request: CreativeHeatingActionRequest,
    performed_by: str,
) -> CreativeHeatingActionResponse:
    campaign_row = _load_campaign_action_source(context, campaign_id)
    if campaign_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )
    _ensure_campaign_not_deleted(campaign_row)
    before_state = _snapshot_campaign_state(campaign_row)
    heating_row = await upsert_creative_heating(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        creative_id=request.creative_id,
        advertiser_id=str(campaign_row.advertiser_id),
        promotion_type=_resolve_campaign_promotion_type(campaign_row),
        mode=request.mode,
        target_daily_budget=request.target_daily_budget,
        budget_delta=request.budget_delta,
        currency=request.currency,
        max_duration_minutes=request.max_duration_minutes,
        note=request.note,
        creative_name=request.creative_name,
        product_id=request.product_id,
        item_id=request.item_id,
    )
    context.db.flush()

    updated_row, response = await apply_boost_creative_action(
        context.db,
        client=context.client,
        campaign=campaign_row,
        heating=heating_row,
        mode=request.mode,
        target_daily_budget=request.target_daily_budget,
        budget_delta=request.budget_delta,
        currency=request.currency,
        max_duration_minutes=request.max_duration_minutes,
        note=request.note,
        performed_by=performed_by,
        before_state=before_state,
    )

    return CreativeHeatingActionResponse(
        action_type="BOOST_CREATIVE",
        heating=_serialize_heating_row(updated_row),
        tiktok_response=response.data.model_dump(exclude_none=True),
        request_id=response.request_id,
    )


async def _apply_creative_status_action(
    *,
    context: GMVMaxRouteContext,
    campaign_id: str,
    request: CreativeHeatingActionRequest,
    performed_by: str,
) -> CreativeHeatingActionResponse:
    campaign_row = _load_campaign_action_source(context, campaign_id)
    if campaign_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )
    _ensure_campaign_not_deleted(campaign_row)
    before_state = _snapshot_campaign_state(campaign_row)
    action_type = str(request.action_type).upper()
    is_add_back = action_type == "ADD_BACK_CREATIVE"
    requested_action = str(
        request.action
        or request.operation_type
        or ("ADD" if is_add_back else "REMOVE")
    ).upper()
    if requested_action in {"ADD_BACK", "ADD_BACK_CREATIVE", "RESTORE", "RESTORE_CREATIVE"}:
        requested_action = "ADD"
    if requested_action in {"REMOVE_CREATIVE", "EXCLUDE", "EXCLUDE_CREATIVE"}:
        requested_action = "REMOVE"
    if requested_action not in {"REMOVE", "ADD"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_CREATIVE_ACTION_INVALID",
                "message": "action must be REMOVE or ADD",
            },
        )
    item_ids = [str(item).strip() for item in (request.item_ids or []) if str(item).strip()]
    item_id = str(request.item_id or request.creative_id).strip()
    if item_id and item_id not in item_ids:
        item_ids.insert(0, item_id)
    item_list: list[GMVMaxCreativeStatusUpdateItem] = []
    shared_spu_ids = [
        str(spu).strip()
        for spu in (request.spu_id_list or ([request.product_id] if request.product_id else []))
        if str(spu).strip()
    ]
    for raw_item in request.item_list or []:
        raw_item_id = str(raw_item.get("item_id") or raw_item.get("creative_id") or "").strip()
        if not raw_item_id:
            continue
        raw_spu_ids = raw_item.get("spu_id_list") or raw_item.get("spu_ids") or shared_spu_ids
        if isinstance(raw_spu_ids, str):
            raw_spu_ids = [raw_spu_ids]
        cleaned_spu_ids = [str(spu).strip() for spu in raw_spu_ids if str(spu).strip()]
        item_list.append(
            GMVMaxCreativeStatusUpdateItem(
                item_id=raw_item_id,
                spu_id_list=cleaned_spu_ids or None,
            )
        )
        if raw_item_id not in item_ids:
            item_ids.append(raw_item_id)
    for candidate_item_id in item_ids:
        if any(item.item_id == candidate_item_id for item in item_list):
            continue
        item_list.append(
            GMVMaxCreativeStatusUpdateItem(
                item_id=candidate_item_id,
                spu_id_list=shared_spu_ids or None,
            )
        )
    if not item_list:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_CREATIVE_ITEM_REQUIRED",
                "message": "creative_id, item_id, item_ids, or item_list is required",
            },
        )
    if len(item_list) > 400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "GMVMAX_CREATIVE_ITEM_LIMIT_EXCEEDED",
                "message": "item_list supports at most 400 creatives per request",
            },
        )
    item_id = item_list[0].item_id

    heating_row = await upsert_creative_heating(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        creative_id=request.creative_id,
        advertiser_id=str(campaign_row.advertiser_id),
        promotion_type=_resolve_campaign_promotion_type(campaign_row),
        mode=request.mode,
        target_daily_budget=request.target_daily_budget,
        budget_delta=request.budget_delta,
        currency=request.currency,
        max_duration_minutes=request.max_duration_minutes,
        note=request.note,
        creative_name=request.creative_name,
        product_id=request.product_id,
        item_id=item_id,
        auto_stop_enabled=is_add_back,
    )
    context.db.flush()

    action_time = datetime.now(timezone.utc)
    api_request = GMVMaxCreativeStatusUpdateRequest(
        advertiser_id=str(campaign_row.advertiser_id),
        body=GMVMaxCreativeStatusUpdateBody(
            campaign_id=str(campaign_id),
            item_list=item_list,
            action=requested_action,
        ),
    )
    request_payload = api_request.body.model_dump(exclude_none=True)
    request_payload["advertiser_id"] = str(campaign_row.advertiser_id)
    try:
        assert_gmvmax_mutation_current(context.db)
        response = await _call_tiktok(
            context.client.gmv_max_creative_status_update,
            api_request,
        )
        assert_gmvmax_mutation_current(context.db)
    except HTTPException as exc:
        await update_heating_action_result(
            context.db,
            heating_id=heating_row.id,
            status="FAILED",
            action_type=action_type,
            action_time=action_time,
            request_payload=request_payload,
            response_payload=exc.detail if isinstance(exc.detail, dict) else None,
            error_message=_extract_error_message(exc.detail),
        )
        context.db.flush()
        _log_action_entry(
            context,
            campaign_id=str(campaign_id),
            campaign=campaign_row,
            action=action_type,
            actor=performed_by,
            before=before_state,
            after=before_state,
            result="FAILED",
            reason=request.note,
            error_message=_extract_error_message(exc.detail),
        )
        raise

    response_payload = response.data.model_dump(exclude_none=True)
    updated_row = await update_heating_action_result(
        context.db,
        heating_id=heating_row.id,
        status="AVAILABLE" if is_add_back else "EXCLUDED",
        action_type=action_type,
        action_time=action_time,
        request_payload=request_payload,
        response_payload=response_payload,
        error_message=None,
    )
    if is_add_back:
        updated_row.auto_stop_enabled = True
        updated_row.is_heating_active = False
    else:
        updated_row.auto_stop_enabled = False
        updated_row.is_heating_active = False
    context.db.add(updated_row)
    context.db.flush()
    try:
        from app.services.gmvmax_creative_guard import (  # local import avoids router startup coupling
            _load_creatives,
            _load_scopes,
            _metric_snapshot,
        )

        matching_scope = next(
            (
                scope
                for scope in _load_scopes(context.db)
                if str(scope.campaign_id) == str(campaign_id)
            ),
            None,
        )
        metrics_by_id = {
            str(metric.creative_id): metric
            for metric in (_load_creatives(context.db, matching_scope) if matching_scope else [])
        }
        strategy_id = matching_scope.strategy_id if matching_scope else context.db.execute(
            text(
                """
                select id from gmv_strategy_configs
                where workspace_id=:workspace_id and auth_id=:auth_id
                  and campaign_id=:campaign_id
                order by id desc limit 1
                """
            ),
            {
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": str(campaign_id),
            },
        ).scalar_one_or_none()
        for item in item_list:
            creative_id = str(item.item_id)
            metric = metrics_by_id.get(creative_id)
            baseline = _metric_snapshot(metric) if metric else {
                "cost_cents": 0,
                "gross_revenue_cents": 0,
                "orders": 0,
                "product_impressions": 0,
                "product_clicks": 0,
            }
            item_request = {
                "advertiser_id": str(campaign_row.advertiser_id),
                "body": {
                    "campaign_id": str(campaign_id),
                    "item_list": [item.model_dump(exclude_none=True)],
                    "action": requested_action,
                },
                "manual": {"performed_by": performed_by, "note": request.note},
            }
            if is_add_back:
                item_request["retest"] = {
                    "attempt": 1,
                    "baseline_metrics": baseline,
                    "added_at": action_time.isoformat(),
                    "manual": True,
                }
            context.db.execute(
                text(
                    """
                    insert into gmv_campaign_guard_events (
                        workspace_id,auth_id,advertiser_id,store_id,campaign_id,
                        strategy_id,event_type,action,reason,result,cost_cents,
                        gross_revenue_cents,orders,roi,request_json,response_json,created_at
                    ) values (
                        :workspace_id,:auth_id,:advertiser_id,:store_id,:campaign_id,
                        :strategy_id,'CREATIVE_GUARD',:action,:reason,'SUCCESS',:cost_cents,
                        :gross_revenue_cents,:orders,:roi,cast(:request_json as json),
                        cast(:response_json as json),:created_at
                    )
                    """
                ),
                {
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "advertiser_id": str(campaign_row.advertiser_id),
                    "store_id": str(campaign_row.store_id),
                    "campaign_id": str(campaign_id),
                    "strategy_id": strategy_id,
                    "action": requested_action,
                    "reason": "creative_guard:manual_add" if is_add_back else "creative_guard:manual_remove",
                    "cost_cents": baseline["cost_cents"],
                    "gross_revenue_cents": baseline["gross_revenue_cents"],
                    "orders": baseline["orders"],
                    "roi": str(metric.roi) if metric and metric.roi is not None else None,
                    "request_json": _json_dumps(item_request),
                    "response_json": _json_dumps(response_payload),
                    "created_at": action_time.replace(tzinfo=None),
                },
            )
    except Exception:
        logger.warning(
            "failed to record manual creative action in guard state",
            exc_info=True,
            extra={"campaign_id": str(campaign_id), "action": requested_action},
        )
    _log_action_entry(
        context,
        campaign_id=str(campaign_id),
        campaign=campaign_row,
        action=action_type,
        actor=performed_by,
        before=before_state,
        after=before_state,
        result="SUCCESS",
        reason=request.note,
        persist_catalog_event=False,
    )

    return CreativeHeatingActionResponse(
        action_type=request.action_type,
        heating=_serialize_heating_row(updated_row),
        tiktok_response=response_payload,
        request_id=response.request_id,
    )


_CREATIVE_ACTION_ALIASES: dict[str, tuple[str, str | None]] = {
    "BOOST": ("BOOST_CREATIVE", None),
    "HEAT": ("BOOST_CREATIVE", None),
    "START_HEAT": ("BOOST_CREATIVE", None),
    "START_BOOST": ("BOOST_CREATIVE", None),
    "BOOST_CREATIVE": ("BOOST_CREATIVE", None),
    "STOP_HEAT": ("BOOST_CREATIVE", "STOP"),
    "STOP_BOOST": ("BOOST_CREATIVE", "STOP"),
    "STOP_CREATIVE": ("BOOST_CREATIVE", "STOP"),
    "STOP_CREATIVE_HEATING": ("BOOST_CREATIVE", "STOP"),
    "REMOVE_CREATIVE": ("REMOVE_CREATIVE", None),
    "ADD_BACK_CREATIVE": ("ADD_BACK_CREATIVE", None),
}


def _campaign_budget_for_creative_action(campaign: Any) -> float | None:
    """Return the campaign budget in TikTok's major currency unit."""

    for field_name in ("budget_cents", "daily_budget_cents"):
        value = getattr(campaign, field_name, None)
        if value is None:
            continue
        try:
            budget = float(value) / 100.0
        except (TypeError, ValueError):
            continue
        if budget > 0:
            return budget
    for field_name in ("budget", "daily_budget"):
        value = _to_float(getattr(campaign, field_name, None))
        if value is not None and value > 0:
            return value
    return None


def _resolve_creative_item_group_id(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    creative_id: str,
) -> str | None:
    """Resolve the SPU/item-group required by the official boost endpoint."""

    stmt = (
        select(GmvmaxProductCreativeMetricsDaily.item_group_id)
        .where(
            GmvmaxProductCreativeMetricsDaily.workspace_id == int(context.workspace_id),
            GmvmaxProductCreativeMetricsDaily.auth_id == int(context.auth_id),
            GmvmaxProductCreativeMetricsDaily.campaign_id == str(campaign_id),
            GmvmaxProductCreativeMetricsDaily.creative_id == str(creative_id),
        )
        .order_by(
            GmvmaxProductCreativeMetricsDaily.stat_time_day.desc(),
            GmvmaxProductCreativeMetricsDaily.updated_at.desc(),
        )
        .limit(1)
    )
    if context.advertiser_id:
        stmt = stmt.where(
            GmvmaxProductCreativeMetricsDaily.advertiser_id
            == str(context.advertiser_id)
        )
    if context.store_id:
        stmt = stmt.where(
            GmvmaxProductCreativeMetricsDaily.store_id == str(context.store_id)
        )
    item_group_id = context.db.execute(stmt).scalar_one_or_none()
    if item_group_id:
        return str(item_group_id)

    fallback_stmt = (
        select(GmvmaxProductCampaignItemGroup.item_group_id)
        .where(
            GmvmaxProductCampaignItemGroup.workspace_id == int(context.workspace_id),
            GmvmaxProductCampaignItemGroup.auth_id == int(context.auth_id),
            GmvmaxProductCampaignItemGroup.campaign_id == str(campaign_id),
        )
        .order_by(GmvmaxProductCampaignItemGroup.id.asc())
        .limit(2)
    )
    if context.advertiser_id:
        fallback_stmt = fallback_stmt.where(
            GmvmaxProductCampaignItemGroup.advertiser_id
            == str(context.advertiser_id)
        )
    if context.store_id:
        fallback_stmt = fallback_stmt.where(
            GmvmaxProductCampaignItemGroup.store_id == str(context.store_id)
        )
    candidates = [
        str(value)
        for value in context.db.execute(fallback_stmt).scalars().all()
        if value
    ]
    return candidates[0] if len(candidates) == 1 else None


@router.post(
    "/{campaign_id}/actions",
    response_model=Union[CampaignActionResponse, CreativeHeatingActionResponse],
)
@router.post(
    "/campaigns/{campaign_id}/actions",
    response_model=Union[CampaignActionResponse, CreativeHeatingActionResponse],
)
async def apply_gmvmax_campaign_action_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: Dict[str, Any] = Body(...),
    advertiser_id: Optional[str] = Query(None),
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> Union[CampaignActionResponse, CreativeHeatingActionResponse]:
    """Apply campaign status/strategy actions or BOOST_CREATIVE session changes."""

    adv, _ = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
    )
    actor_label = _resolve_actor_label(me)
    normalized_campaign_id = str(campaign_id)
    campaign_before = _load_campaign_action_source(
        context, normalized_campaign_id
    )
    if campaign_before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )
    before_state = _snapshot_campaign_state(campaign_before)

    raw_type = payload.get("action_type")
    if raw_type is None and "type" in payload:
        raw_type = payload["type"]
    normalized_raw_type = str(raw_type or "").strip().upper()
    normalized_type, implied_mode = _CREATIVE_ACTION_ALIASES.get(
        normalized_raw_type,
        (normalized_raw_type, None),
    )

    if normalized_type in {"BOOST_CREATIVE", "REMOVE_CREATIVE", "ADD_BACK_CREATIVE"}:
        _ensure_campaign_not_deleted(campaign_before)
        candidate = dict(payload)
        candidate.pop("type", None)
        candidate["action_type"] = normalized_type
        if "creative_id" not in candidate and candidate.get("creativeId"):
            candidate["creative_id"] = candidate.pop("creativeId")
        if "product_id" not in candidate and candidate.get("productId"):
            candidate["product_id"] = candidate.pop("productId")
        if (
            "target_daily_budget" not in candidate
            and candidate.get("targetDailyBudget") is not None
        ):
            candidate["target_daily_budget"] = candidate.pop("targetDailyBudget")
        if implied_mode and not candidate.get("mode"):
            candidate["mode"] = implied_mode
        is_stop = str(candidate.get("mode") or "").strip().upper() in {
            "STOP",
            "STOP_CREATIVE",
            "STOP_BOOST",
        }
        if (
            normalized_type == "BOOST_CREATIVE"
            and not is_stop
            and not candidate.get("product_id")
            and candidate.get("creative_id")
        ):
            item_group_id = _resolve_creative_item_group_id(
                context,
                campaign_id=normalized_campaign_id,
                creative_id=str(candidate["creative_id"]),
            )
            if item_group_id:
                candidate["product_id"] = item_group_id
        try:
            heating_request = CreativeHeatingActionRequest.model_validate(candidate)
        except ValidationError as exc:
            first_error = next(iter(exc.errors()), {})
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "GMVMAX_CREATIVE_HEATING_INVALID",
                    "message": first_error.get("msg") or "Invalid creative heating request",
                },
            ) from exc
        with _manual_guard_mutation_lease(context) as mutation:
            fresh_campaign = _load_campaign_action_source(
                context, normalized_campaign_id
            )
            if fresh_campaign is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign not found",
                )
            _ensure_campaign_not_deleted(fresh_campaign)
            if normalized_type in {"REMOVE_CREATIVE", "ADD_BACK_CREATIVE"}:
                result = await _apply_creative_status_action(
                    context=context,
                    campaign_id=normalized_campaign_id,
                    request=heating_request,
                    performed_by=actor_label,
                )
            else:
                result = await _apply_creative_heating_action(
                    context=context,
                    campaign_id=normalized_campaign_id,
                    request=heating_request,
                    performed_by=actor_label,
                )
            mutation.commit(context.db)
            return result

    action_request = CampaignActionRequest.model_validate(payload)
    action_label = _ACTION_LOG_TYPES.get(
        action_request.type, action_request.type.upper()
    )
    def _log_success() -> None:
        campaign_after = _load_campaign_action_source(
            context, normalized_campaign_id
        )
        after_state = _snapshot_campaign_state(campaign_after)
        _log_action_entry(
            context,
            campaign_id=normalized_campaign_id,
            campaign=campaign_after or campaign_before,
            action=action_label,
            actor=actor_label,
            before=before_state,
            after=after_state or before_state,
            result="SUCCESS",
        )

    def _log_failure(detail: Any) -> None:
        _log_action_entry(
            context,
            campaign_id=normalized_campaign_id,
            campaign=campaign_before,
            action=action_label,
            actor=actor_label,
            before=before_state,
            after=before_state,
            result="FAILED",
            error_message=_extract_error_message(detail),
        )

    if action_request.type == "delete" and _campaign_is_deleted(campaign_before):
        _log_action_entry(
            context,
            campaign_id=normalized_campaign_id,
            campaign=campaign_before,
            action=action_label,
            actor=actor_label,
            before=before_state,
            after=before_state,
            result="SUCCESS",
            reason="already_deleted",
        )
        return CampaignActionResponse(
            type="delete",
            status="success",
            response={"already_deleted": True},
        )

    _ensure_campaign_not_deleted(campaign_before)

    try:
        if action_request.type in {"pause", "enable", "delete"}:
            try:
                with _manual_guard_mutation_lease(
                    context,
                    priority_pause=action_request.type == "pause",
                ) as mutation:
                    campaign_before = _load_campaign_action_source(
                        context, normalized_campaign_id
                    )
                    if campaign_before is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Campaign not found",
                        )
                    _ensure_campaign_not_deleted(campaign_before)
                    before_state = _snapshot_campaign_state(campaign_before)
                    campaign_store_id = (
                        getattr(campaign_before, "store_id", None) or context.store_id
                    )
                    if action_request.type == "enable":
                        _ensure_campaign_not_creation_quarantined(
                            context,
                            normalized_campaign_id,
                        )
                        await _ensure_campaign_products_available(
                            context, campaign=campaign_before, advertiser_id=str(adv)
                        )
                    operation_status_map = {
                        "pause": "DISABLE",
                        "enable": "ENABLE",
                        "delete": "DELETE",
                    }
                    operation_status = operation_status_map[action_request.type]
                    status_request = CampaignStatusUpdateRequest(
                        advertiser_id=adv,
                        campaign_ids=[normalized_campaign_id],
                        operation_status=operation_status,
                    )
                    mutation.assert_current(context.db)
                    response = await _call_tiktok(
                        context.client.campaign_status_update, status_request
                    )
                    mutation.assert_current(context.db)
                    _apply_local_campaign_operation_status(
                        context,
                        campaign_id=normalized_campaign_id,
                        campaign=campaign_before,
                        operation_status=operation_status,
                    )
                    if action_request.type == "pause":
                        if action_request.disable_strategy:
                            _disable_local_automation_strategy(
                                context,
                                normalized_campaign_id,
                            )
                        if not payload.get("_system_pause_intent_id"):
                            cancel_pending_campaign_pause_intent(
                                context.db,
                                workspace_id=context.workspace_id,
                                auth_id=context.auth_id,
                                advertiser_id=str(adv),
                                store_id=campaign_store_id,
                                campaign_id=normalized_campaign_id,
                            )
                        set_manual_pause_override(
                            context.db,
                            workspace_id=context.workspace_id,
                            auth_id=context.auth_id,
                            advertiser_id=str(adv),
                            store_id=campaign_store_id,
                            campaign_id=normalized_campaign_id,
                            actor=actor_label,
                            reason=str(payload.get("reason") or "") or None,
                        )
                    else:
                        # A deliberate enable/delete supersedes any older pause hold.
                        cancel_pending_campaign_pause_intent(
                            context.db,
                            workspace_id=context.workspace_id,
                            auth_id=context.auth_id,
                            advertiser_id=str(adv),
                            store_id=campaign_store_id,
                            campaign_id=normalized_campaign_id,
                        )
                        clear_manual_override(
                            context.db,
                            workspace_id=context.workspace_id,
                            auth_id=context.auth_id,
                            advertiser_id=str(adv),
                            store_id=campaign_store_id,
                            campaign_id=normalized_campaign_id,
                        )
                    _log_success()
                    # The official mutation and local control state form one
                    # critical boundary protected from both Guard cycles.
                    mutation.commit(context.db)
                    # Do not immediately call campaign/info here.  TikTok can
                    # briefly serve the pre-mutation status; because that request
                    # starts later, timestamp ordering alone cannot identify the
                    # eventual-consistency response as stale.  The next scheduled
                    # catalog snapshot will reconcile after propagation.
                return CampaignActionResponse(
                    type=action_request.type,
                    status="success",
                    response=response.data.model_dump(exclude_none=True),
                    request_id=response.request_id,
                )
            except HTTPException as exc:
                if (
                    action_request.type == "pause"
                    and _is_retryable_manual_mutation_conflict(exc.detail)
                ):
                    # A worker already owns this durable intent. Let that
                    # worker defer the same row; recursively queuing from the
                    # router creates duplicate broker messages and audit rows.
                    if payload.get("_system_pause_intent_id"):
                        raise
                    return _queue_campaign_pause_after_mutation_conflict(
                        context,
                        campaign_id=normalized_campaign_id,
                        campaign=campaign_before,
                        actor=actor_label,
                        reason=str(payload.get("reason") or "") or None,
                        before=before_state,
                        disable_strategy=action_request.disable_strategy,
                    )
                raise
        if (
            action_request.type == "update_strategy"
            and action_request.payload.get("session_id")
        ):
            body = _build_session_update_body(
                normalized_campaign_id, action_request.payload, context.store_id
            )
            request = GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body)
            with _manual_guard_mutation_lease(context) as mutation:
                campaign_before = _load_campaign_action_source(
                    context, normalized_campaign_id
                )
                if campaign_before is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Campaign not found",
                    )
                _ensure_campaign_not_deleted(campaign_before)
                _ensure_campaign_not_creation_quarantined(
                    context,
                    normalized_campaign_id,
                )
                before_state = _snapshot_campaign_state(campaign_before)
                mutation.assert_current(context.db)
                response = await _call_tiktok(
                    context.client.gmv_max_session_update, request
                )
                mutation.assert_current(context.db)
                session_list_response = await _call_tiktok(
                    context.client.gmv_max_session_list,
                    GMVMaxSessionListRequest(
                        advertiser_id=adv,
                        campaign_id=normalized_campaign_id,
                    ),
                )
                _log_success()
                mutation.commit(context.db)
            return CampaignActionResponse(
                type=action_request.type,
                status="success",
                response={
                    "sessions": [
                        item.model_dump()
                        for item in gmv_max_session_entries(
                            session_list_response.data
                        )
                    ],
                },
                request_id=response.request_id,
            )

        body = _build_campaign_update_body(
            normalized_campaign_id, action_request.type, action_request.payload
        )
        request = GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body)
        with _manual_guard_mutation_lease(context) as mutation:
            campaign_before = _load_campaign_action_source(
                context, normalized_campaign_id
            )
            if campaign_before is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign not found",
                )
            _ensure_campaign_not_deleted(campaign_before)
            _ensure_campaign_not_creation_quarantined(
                context,
                normalized_campaign_id,
            )
            before_state = _snapshot_campaign_state(campaign_before)
            mutation.assert_current(context.db)
            response = await _call_tiktok(
                context.client.gmv_max_campaign_update, request
            )
            mutation.assert_current(context.db)
            _apply_local_campaign_update_body(
                context,
                campaign=campaign_before,
                body=body,
            )
            _log_success()
            mutation.commit(context.db)
        return CampaignActionResponse(
            type=action_request.type,
            status="success",
            response=response.data.model_dump(exclude_none=True),
            request_id=response.request_id,
        )
    except HTTPException as exc:
        _log_failure(exc.detail)
        raise


@router.get(
    "/{campaign_id}/actions",
    response_model=ActionLogEntry,
    dependencies=[Depends(require_tenant_member)],
)
@router.get(
    "/campaigns/{campaign_id}/actions",
    response_model=ActionLogEntry,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_action_logs_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=200, alias="page_size"),
    sort: str = Query("-timestamp", description="Sort by timestamp, use - for desc"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> ActionLogEntry:
    """Return stored campaign action logs (paginated)."""

    limit = page_size
    offset = (page - 1) * page_size

    try:
        if _load_campaign_action_source(context, str(campaign_id)) is None:
            return ActionLogEntry(
                entries=[],
                total=0,
                page=page,
                page_size=page_size,
            )
        entries, total = _list_guard_action_logs(
            context,
            campaign_id=str(campaign_id),
            limit=limit,
            offset=offset,
            sort=sort,
        )
        return ActionLogEntry(
            entries=entries,
            total=total,
            page=page,
            page_size=page_size,
        )
    except SQLAlchemyError as exc:
        logger.info(
            "gmvmax action log storage unavailable",
            exc_info=True,
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": str(campaign_id),
            },
        )
        context.db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "GMVMAX_ACTION_LOG_STORAGE_UNAVAILABLE",
                "message": "Campaign action log storage is temporarily unavailable.",
            },
        ) from exc


@router.get(
    "/{campaign_id}/creatives/heating",
    response_model=CreativeHeatingListResponse,
    dependencies=[Depends(require_tenant_member)],
)
@router.get(
    "/campaigns/{campaign_id}/creatives/heating",
    response_model=CreativeHeatingListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_creative_heating_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    status: Optional[str] = Query(None, description="Optional heating status filter"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CreativeHeatingListResponse:
    """Return stored heating state rows for a campaign's creatives (no live API call)."""

    rows = await list_heating_configs(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        status=str(status) if status is not None else None,
    )
    return CreativeHeatingListResponse(items=[_serialize_heating_row(row) for row in rows])


@router.get(
    "/{campaign_id}/strategy",
    response_model=StrategyResponse,
    dependencies=[Depends(require_tenant_member)],
)
@router.get(
    "/campaigns/{campaign_id}/strategy",
    response_model=StrategyResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = Query(None),
    include_recommendation: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> StrategyResponse:
    """Fetch campaign + session details and optional bid recommendation from TikTok APIs."""

    adv, bound_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
    )
    campaign_resp = await _call_tiktok(
        context.client.gmv_max_campaign_info,
        GMVMaxCampaignInfoRequest(advertiser_id=adv, campaign_id=str(campaign_id)),
    )
    sessions_resp = await _call_tiktok(
        context.client.gmv_max_session_list,
        GMVMaxSessionListRequest(advertiser_id=adv, campaign_id=str(campaign_id)),
    )
    sessions = gmv_max_session_entries(sessions_resp.data)
    recommendation = None
    recommendation_request_id = None
    if include_recommendation:
        item_group_ids = _extract_item_group_ids(sessions)
        store_id = campaign_resp.data.store_id or bound_store_id
        _, store_id = _validate_bound_scope(context, store_id=store_id)
        shopping_ads_type = campaign_resp.data.shopping_ads_type
        optimization_goal = campaign_resp.data.optimization_goal
        if (
            store_id
            and shopping_ads_type
            and optimization_goal
            and item_group_ids
        ):
            bid_request = GMVMaxBidRecommendRequest(
                advertiser_id=adv,
                store_id=str(store_id),
                shopping_ads_type=str(shopping_ads_type),
                optimization_goal=str(optimization_goal),
                item_group_ids=item_group_ids,
            )
            recommendation_resp = await _call_tiktok(
                context.client.gmv_max_bid_recommend,
                bid_request,
            )
            recommendation = recommendation_resp.data
            recommendation_request_id = recommendation_resp.request_id
    local_strategy = _get_local_strategy_config(context, str(campaign_id))
    local_payload = _serialize_strategy_config(local_strategy)
    return StrategyResponse(
        campaign=campaign_resp.data,
        sessions=sessions,
        sessions_page_info=getattr(sessions_resp.data, "page_info", None),
        recommendation=recommendation,
        **local_payload,
        campaign_request_id=campaign_resp.request_id,
        sessions_request_id=sessions_resp.request_id,
        recommendation_request_id=recommendation_request_id,
    )


@router.put(
    "/{campaign_id}/strategy",
    response_model=StrategyUpdateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
@router.put(
    "/campaigns/{campaign_id}/strategy",
    response_model=StrategyUpdateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: StrategyUpdateRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> StrategyUpdateResponse:
    """Update campaign/session strategy via TikTok update endpoints then return status."""

    adv, bound_store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=payload.session.store_id if payload.session else None,
    )
    has_remote_mutation = bool(payload.campaign or payload.session)
    with _manual_guard_mutation_lease(context) as mutation:
        # Acquiring the lease ends any earlier snapshot. Reload the complete
        # bound scope and its create quarantine before every strategy write,
        # including local-only changes.
        campaign_row = _load_campaign_action_source(
            context, str(campaign_id)
        )
        if campaign_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Campaign not found",
            )
        _ensure_campaign_not_deleted(campaign_row)
        _ensure_campaign_not_creation_quarantined(context, str(campaign_id))
        campaign_resp = None
        session_resp = None
        session_list_resp = None
        strategy_payload = None
        local_keys = set(payload.model_dump(exclude_unset=True).keys()) - {
            "campaign",
            "session",
        }
        if local_keys:
            local_strategy = _get_or_create_local_strategy_config(
                context, str(campaign_id)
            )
            strategy_payload = _apply_local_strategy_update(local_strategy, payload)
            _persist_strategy_effective_prices(context, payload)
            context.db.add(local_strategy)
            context.db.flush()
        if payload.campaign:
            body = GMVMaxCampaignUpdateBody(
                campaign_id=str(campaign_id),
                budget=payload.campaign.budget,
                roas_bid=payload.campaign.roas_bid,
                promotion_days=payload.campaign.promotion_days,
                schedule_type=payload.campaign.schedule_type,
                schedule_end_time=payload.campaign.schedule_end_time,
            )
            mutation.assert_current(context.db)
            campaign_resp = await _call_tiktok(
                context.client.gmv_max_campaign_update,
                GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body),
            )
            mutation.assert_current(context.db)
            _apply_local_campaign_update_body(
                context,
                campaign=campaign_row,
                body=body,
            )
        if payload.session:
            body = _build_session_update_body(
                campaign_id,
                payload.session.model_dump(exclude_none=True),
                bound_store_id,
            )
            mutation.assert_current(context.db)
            session_resp = await _call_tiktok(
                context.client.gmv_max_session_update,
                GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body),
            )
            mutation.assert_current(context.db)
            session_list_resp = await _call_tiktok(
                context.client.gmv_max_session_list,
                GMVMaxSessionListRequest(
                    advertiser_id=adv,
                    campaign_id=str(campaign_id),
                ),
            )
        mutation.commit(context.db)

    if not has_remote_mutation:
        return StrategyUpdateResponse(
            status="success" if strategy_payload else "noop",
            strategy=strategy_payload,
        )
    return StrategyUpdateResponse(
        status="success",
        campaign=campaign_resp.data if campaign_resp else None,
        sessions=(
            gmv_max_session_entries(session_list_resp.data)
            if session_list_resp
            else None
        ),
        strategy=strategy_payload,
        campaign_request_id=campaign_resp.request_id if campaign_resp else None,
        session_request_id=session_resp.request_id if session_resp else None,
    )


@router.post(
    "/{campaign_id}/strategies/preview",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_member)],
)
@router.post(
    "/campaigns/{campaign_id}/strategies/preview",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def preview_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: StrategyPreviewRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),

) -> AsyncTaskResponse:
    """Preview bid recommendations via TikTok GET /gmv_max/bid/recommend/ through Celery."""

    adv, store_id = _validate_bound_scope(
        context,
        advertiser_id=advertiser_id,
        store_id=payload.store_id,
    )
    if not store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )
    if not payload.shopping_ads_type or not payload.optimization_goal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="shopping_ads_type and optimization_goal are required",
        )
    request = GMVMaxBidRecommendRequest(
        advertiser_id=adv,
        store_id=str(store_id),
        shopping_ads_type=str(payload.shopping_ads_type),
        optimization_goal=str(payload.optimization_goal),
        item_group_ids=(
            [str(item) for item in payload.item_group_ids]
            if payload.item_group_ids is not None
            else None
        ),
        identity_id=payload.identity_id,
    )
    async_res = _enqueue_owned_task(
        context,
        task_name="gmvmax.strategy_preview",
        kwargs={
            "auth_id": context.auth_id,
            "bid_request": request.model_dump(exclude_none=True, by_alias=True),
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.strategy_preview enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "task_id": async_res.id,
        },
    )
    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
