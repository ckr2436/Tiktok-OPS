from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
"""Provider-scoped service helpers bridging GMV Max routers to core services."""

from typing import Any, Iterable, Mapping, Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select, case, func
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import PromotionTypeEnum
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.data.repositories.tiktok_business.gmvmax import list_gmvmax_campaigns
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action as svc_apply_campaign_action,
    decide_campaign_action,
    get_item_group_ids_for_campaign,
    get_or_create_strategy_config,
    sync_gmvmax_campaigns as svc_sync_campaigns,
    sync_gmvmax_metrics_daily as svc_sync_metrics_daily,
    sync_gmvmax_metrics_hourly as svc_sync_metrics_hourly,
)
from app.services.ttb_api import TTBApiClient, TTBBusinessError

from ._helpers import (
    ensure_ttb_auth_in_workspace,
    get_advertiser_id_for_account,
    get_gmvmax_client_for_account,
    get_ttb_client_for_account,
    normalize_provider,
    resolve_account_binding,
)
from app.providers.tiktok_business.gmvmax_client import GMVMaxStoreAdUsageCheckRequest


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


def _ensure_provider(provider: str) -> str:
    return normalize_provider(provider)


def _order_desc_nulls_last(col):
    """
    Vendor-agnostic 等价实现：ORDER BY col DESC NULLS LAST
    在 MySQL/MariaDB 上编译为：
      ORDER BY (col IS NULL) ASC, col DESC
    在支持 NULLS LAST 的方言上也安全。
    """
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


def _normalize_action(action: str) -> str:
    raw = str(action or "").strip().upper()
    return _ACTION_NORMALIZATION.get(raw, raw)


def _load_campaign_payload(campaign: TTBGmvMaxCampaign) -> Mapping[str, Any]:
    raw = getattr(campaign, "raw_json", None)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001 - defensive parsing of legacy payloads
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    if isinstance(raw, Mapping):
        return raw
    return {}


def _resolve_product_specific_type(campaign: TTBGmvMaxCampaign) -> str:
    payload = _load_campaign_payload(campaign)
    specific_type = payload.get("product_specific_type")
    if not specific_type and isinstance(payload.get("_campaign_info"), Mapping):
        specific_type = payload["_campaign_info"].get("product_specific_type")

    if specific_type:
        return str(specific_type).upper()

    item_group_ids = getattr(campaign, "item_group_ids", None)
    if not item_group_ids:
        item_group_ids = payload.get("item_group_ids")
    if item_group_ids:
        return "CUSTOMIZED_PRODUCTS"
    return "ALL"


def _resolve_store_authorized_bc_id(
    campaign: TTBGmvMaxCampaign, *, binding_bc_id: str | None
) -> str | None:
    payload = _load_campaign_payload(campaign)
    store_bc_id = payload.get("store_authorized_bc_id")
    if not store_bc_id and isinstance(payload.get("_campaign_info"), Mapping):
        store_bc_id = payload.get("_campaign_info", {}).get("store_authorized_bc_id")
    return str(store_bc_id or binding_bc_id or "").strip() or None


def _is_product_campaign(campaign: TTBGmvMaxCampaign) -> bool:
    if getattr(campaign, "promotion_type", None) == PromotionTypeEnum.PRODUCT:
        return True
    return (getattr(campaign, "shopping_ads_type", None) or "").upper() == "PRODUCT"


async def _check_promote_all_conflict(
    *,
    campaign: TTBGmvMaxCampaign,
    advertiser_id: str,
    client,
    store_authorized_bc_id: str | None,
) -> None:
    request = GMVMaxStoreAdUsageCheckRequest(
        advertiser_id=str(advertiser_id),
        store_id=str(campaign.store_id or ""),
        store_authorized_bc_id=store_authorized_bc_id,
    )
    response = await client.gmv_max_store_shop_ad_usage_check(request)
    allowed = getattr(getattr(response, "data", None), "promote_all_products_allowed", None)
    if allowed is False:
        raise TTBBusinessError(
            "All-products GMV Max promotion is not allowed for this store",
            code="GMVMAX_PROMOTE_ALL_CONFLICT",
            payload={
                "campaign_id": getattr(campaign, "campaign_id", None),
                "store_id": getattr(campaign, "store_id", None),
            },
        )


async def _check_customized_products_conflict(
    *,
    ttb_client: TTBApiClient,
    campaign: TTBGmvMaxCampaign,
    advertiser_id: str,
    bc_id: str | None,
    item_group_ids: list[str],
) -> None:
    products = await ttb_client.get_store_products_for_gmvmax_item_group_ids(
        bc_id=bc_id,
        store_id=str(campaign.store_id or ""),
        advertiser_id=str(advertiser_id),
        item_group_ids=item_group_ids,
    )

    status_map = {
        str(product.get("item_group_id")): {
            "status": product.get("status"),
            "gmv_max_ads_status": product.get("gmv_max_ads_status"),
        }
        for product in products
        if product.get("item_group_id")
    }

    conflicting: list[str] = []
    for spu_id in item_group_ids:
        info = status_map.get(spu_id)
        if not info:
            conflicting.append(spu_id)
            continue

        if not (
            info.get("status") == "AVAILABLE"
            and info.get("gmv_max_ads_status") == "UNOCCUPIED"
        ):
            conflicting.append(spu_id)

    if conflicting:
        raise TTBBusinessError(
            "Some SPUs are occupied by other Product GMV Max campaigns",
            code="GMVMAX_PRODUCT_OCCUPIED",
            payload={"item_group_ids": conflicting},
        )


async def _ensure_product_campaign_conflict_free(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign: TTBGmvMaxCampaign,
    advertiser_id: str,
    gmvmax_client,
) -> TTBApiClient | None:
    if not _is_product_campaign(campaign):
        # TODO: extend mutual exclusion rules for LIVE GMV Max when spec is ready
        return None

    product_type = _resolve_product_specific_type(campaign)
    binding = resolve_account_binding(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        allow_missing_advertiser=True,
    )
    store_authorized_bc_id = _resolve_store_authorized_bc_id(
        campaign, binding_bc_id=binding.bc_id
    )

    if product_type == "ALL":
        await _check_promote_all_conflict(
            campaign=campaign,
            advertiser_id=advertiser_id,
            client=gmvmax_client,
            store_authorized_bc_id=store_authorized_bc_id,
        )
        return None

    if product_type == "CUSTOMIZED_PRODUCTS":
        item_group_ids = get_item_group_ids_for_campaign(db, campaign=campaign)
        if not item_group_ids:
            return None

        ttb_client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
        try:
            await _check_customized_products_conflict(
                ttb_client=ttb_client,
                campaign=campaign,
                advertiser_id=advertiser_id,
                bc_id=store_authorized_bc_id or binding.bc_id,
                item_group_ids=item_group_ids,
            )
        except Exception:
            await ttb_client.aclose()
            raise
        return ttb_client

    return None


def _ensure_campaign(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
) -> TTBGmvMaxCampaign:
    query = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    if advertiser_id:
        query = query.where(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))

    instance = db.execute(query).scalars().first()
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    return instance


async def sync_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    campaign_ids: Optional[Iterable[str]] = None,
) -> int:
    """Sync campaigns for a binding using ``ttb_gmvmax.sync_gmvmax_campaigns`` and TikTok client."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    try:
        result = await svc_sync_campaigns(
            db,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(resolved_advertiser),
            status=status_filter,
            campaign_ids=[str(cid) for cid in campaign_ids] if campaign_ids else None,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()

    return int(result.get("synced", 0))


async def list_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    store_id: Optional[str] = None,
    business_center_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    sync: bool = False,
) -> dict[str, Any]:
    """Return cached GMV Max campaigns with optional pre-sync; relies on repository filters."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    synced: Optional[int] = None
    if sync:
        synced = await sync_campaigns(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=resolved_advertiser,
            status_filter=status_filter,
        )

    if not store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )

    items, total = list_gmvmax_campaigns(
        db,
        workspace_id=workspace_id,
        advertiser_id=str(resolved_advertiser),
        store_id=str(store_id),
        status_filter=status_filter,
        search=search,
        page=page,
        page_size=page_size,
    )

    payload: dict[str, Any] = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if synced is not None:
        payload["synced"] = synced
    return payload


async def get_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
    refresh: bool = False,
) -> TTBGmvMaxCampaign:
    """Load a single campaign, optionally triggering a targeted sync when missing."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    try:
        instance = _ensure_campaign(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
            advertiser_id=resolved_advertiser,
        )
        return instance
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND or not refresh:
            raise

    await sync_campaigns(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=resolved_advertiser,
        campaign_ids=[campaign_id],
    )

    instance = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=resolved_advertiser,
    )
    return instance


async def sync_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str,
    granularity: str,
    start_date: date,
    end_date: date,
) -> int:
    """Sync hourly/daily metrics for a campaign by calling TikTok report API through the client."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
    )

    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    try:
        if granularity.upper() == "HOUR":
            result = await svc_sync_metrics_hourly(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            result = await svc_sync_metrics_daily(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()

    return int(result.get("synced_rows", 0))


def _query_metrics_hourly(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsHourly]:
    query = select(TTBGmvMaxMetricsHourly).where(
        TTBGmvMaxMetricsHourly.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour < end)
    query = (
        query.order_by(TTBGmvMaxMetricsHourly.stat_time_hour.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def _query_metrics_daily(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[date],
    end: Optional[date],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsDaily]:
    query = select(TTBGmvMaxMetricsDaily).where(
        TTBGmvMaxMetricsDaily.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day < end)
    query = (
        query.order_by(TTBGmvMaxMetricsDaily.stat_time_day.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def query_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    granularity: str,
    start: Optional[datetime | date],
    end: Optional[datetime | date],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Query stored hourly or daily metrics for a campaign (DB only)."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    gran = granularity.upper()
    if gran == "HOUR":
        rows = _query_metrics_hourly(
            db,
            campaign=campaign,
            start=start if isinstance(start, datetime) else None,
            end=end if isinstance(end, datetime) else None,
            limit=limit,
            offset=offset,
        )
        return {
            "granularity": "HOUR",
            "items": rows,
            "count": len(rows),
        }

    rows = _query_metrics_daily(
        db,
        campaign=campaign,
        start=start if isinstance(start, date) else None,
        end=end if isinstance(end, date) else None,
        limit=limit,
        offset=offset,
    )
    return {
        "granularity": "DAY",
        "items": rows,
        "count": len(rows),
    }


async def apply_campaign_action(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    action: str,
    payload: dict[str, Any],
    reason: Optional[str],
    performed_by: str,
    audit_hook: Any | None = None,
) -> tuple[TTBGmvMaxCampaign, TTBGmvMaxActionLog]:
    """Apply a campaign action via TikTok and persist an action log snapshot."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    advertiser_id = get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    normalized_action = _normalize_action(action)
    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    ttb_client: TTBApiClient | None = None
    try:
        if normalized_action == "START":
            ttb_client = await _ensure_product_campaign_conflict_free(
                db,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                campaign=campaign,
                advertiser_id=advertiser_id,
                gmvmax_client=client,
            )

        log_entry = await svc_apply_campaign_action(
            db,
            ttb_client=client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            action=normalized_action,
            payload=payload,
            reason=reason,
            performed_by=performed_by,
            audit_hook=audit_hook,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()
        if ttb_client is not None:
            await ttb_client.aclose()

    db.refresh(campaign)
    return campaign, log_entry


def list_action_logs(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    limit: int,
    offset: int,
    sort: str = "-timestamp",
) -> tuple[TTBGmvMaxCampaign, Sequence[TTBGmvMaxActionLog], int]:
    """Return campaign with paginated action logs stored locally."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    sort_key = str(sort or "").lower()
    sort_desc = sort_key.startswith("-")
    order_clause = (
        TTBGmvMaxActionLog.created_at.desc()
        if sort_desc
        else TTBGmvMaxActionLog.created_at.asc()
    )

    query = (
        select(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
        .order_by(order_clause, TTBGmvMaxActionLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(query).scalars().all()

    count_query = (
        select(func.count())
        .select_from(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
    )
    total = db.scalar(count_query) or 0

    return campaign, rows, int(total)


def get_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
) -> TTBGmvMaxStrategyConfig:
    """Load or create the local strategy configuration for a GMV Max campaign."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )
    db.commit()
    db.refresh(cfg)
    return cfg


def _parse_decimal(value: Optional[str | float | int | Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):  # pragma: no cover
        raise HTTPException(status_code=422, detail="invalid decimal value")


def update_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: dict[str, Any],
) -> Optional[TTBGmvMaxStrategyConfig]:
    """Persist strategy config adjustments used by automated decision making."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )

    if not payload:
        return None

    if "enabled" in payload:
        cfg.enabled = bool(payload["enabled"])

    if "target_roi" in payload:
        cfg.target_roi = _parse_decimal(payload["target_roi"])

    if "min_roi" in payload:
        cfg.min_roi = _parse_decimal(payload["min_roi"])

    if "max_roi" in payload:
        cfg.max_roi = _parse_decimal(payload["max_roi"])

    if "min_impressions" in payload:
        cfg.min_impressions = payload["min_impressions"]

    if "min_clicks" in payload:
        cfg.min_clicks = payload["min_clicks"]

    if "max_budget_raise_pct_per_day" in payload:
        cfg.max_budget_raise_pct_per_day = _parse_decimal(
            payload["max_budget_raise_pct_per_day"]
        )

    if "max_budget_cut_pct_per_day" in payload:
        cfg.max_budget_cut_pct_per_day = _parse_decimal(
            payload["max_budget_cut_pct_per_day"]
        )

    if "max_roas_step_per_adjust" in payload:
        cfg.max_roas_step_per_adjust = _parse_decimal(
            payload["max_roas_step_per_adjust"]
        )

    if "cooldown_minutes" in payload:
        cfg.cooldown_minutes = payload["cooldown_minutes"]

    if "min_runtime_minutes_before_first_change" in payload:
        cfg.min_runtime_minutes_before_first_change = payload[
            "min_runtime_minutes_before_first_change"
        ]

    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def preview_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
) -> dict[str, Any]:
    """Compute a dry-run decision using cached metrics and strategy thresholds."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )
    if not cfg.enabled:
        return {"enabled": False, "reason": "strategy.disabled"}

    metrics_raw = aggregate_recent_metrics(db, campaign=campaign)
    metrics = dict(metrics_raw)
    roi = metrics.get("roi")
    metrics["roi"] = str(roi) if roi is not None else None

    decision = decide_campaign_action(
        campaign=campaign,
        strategy=cfg,
        metrics=metrics_raw,
    )
    return {
        "enabled": True,
        "metrics": metrics,
        "decision": dict(decision) if decision else None,
    }

