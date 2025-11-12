from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select, case
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action as svc_apply_campaign_action,
    decide_campaign_action,
    get_or_create_strategy_config,
    sync_gmvmax_campaigns as svc_sync_campaigns,
    sync_gmvmax_metrics_daily as svc_sync_metrics_daily,
    sync_gmvmax_metrics_hourly as svc_sync_metrics_hourly,
)

from ._helpers import (
    ensure_ttb_auth_in_workspace,
    get_advertiser_id_for_account,
    get_ttb_client_for_account,
    normalize_provider,
)


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
        .where(TTBGmvMaxCampaign.auth_id == int(auth_id))
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
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
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

    query = (
        db.query(TTBGmvMaxCampaign)
        .filter(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .filter(TTBGmvMaxCampaign.auth_id == int(auth_id))
        .filter(TTBGmvMaxCampaign.advertiser_id == str(resolved_advertiser))
    )
    if status_filter:
        query = query.filter(TTBGmvMaxCampaign.status == status_filter)
    if search:
        pattern = f"%{search}%"
        query = query.filter(TTBGmvMaxCampaign.name.ilike(pattern))

    total = query.count()
    offset = (page - 1) * page_size
    items = (
        query.order_by(*_order_desc_nulls_last(TTBGmvMaxCampaign.ext_created_time))
        .offset(offset)
        .limit(page_size)
        .all()
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
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
    )

    client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
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
        TTBGmvMaxMetricsHourly.campaign_id == campaign.id
    )
    if start:
        query = query.where(TTBGmvMaxMetricsHourly.interval_start >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsHourly.interval_start < end)
    query = (
        query.order_by(TTBGmvMaxMetricsHourly.interval_start.asc())
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
        TTBGmvMaxMetricsDaily.campaign_id == campaign.id
    )
    if start:
        query = query.where(TTBGmvMaxMetricsDaily.date >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsDaily.date < end)
    query = query.order_by(TTBGmvMaxMetricsDaily.date.asc()).limit(limit).offset(offset)
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
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    advertiser_id = get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
    try:
        log_entry = await svc_apply_campaign_action(
            db,
            ttb_client=client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            action=action,
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
) -> tuple[TTBGmvMaxCampaign, Sequence[TTBGmvMaxActionLog]]:
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    query = (
        select(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
        .order_by(TTBGmvMaxActionLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(query).scalars().all()
    return campaign, rows


def get_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
) -> TTBGmvMaxStrategyConfig:
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

