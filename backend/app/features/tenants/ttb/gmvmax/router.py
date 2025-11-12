from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import get_db
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.services.ttb_client_factory import build_ttb_client
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action,
    decide_campaign_action,
    get_or_create_strategy_config,
    sync_gmvmax_campaigns,
    sync_gmvmax_metrics_daily,
    sync_gmvmax_metrics_hourly,
)

router = APIRouter(
    prefix="/{workspace_id}/ttb/gmvmax",
    tags=["GMV Max"],
)


def _serialize_campaign(instance: TTBGmvMaxCampaign) -> Dict[str, Any]:
    return {
        "id": instance.id,
        "workspace_id": instance.workspace_id,
        "auth_id": instance.auth_id,
        "advertiser_id": instance.advertiser_id,
        "campaign_id": instance.campaign_id,
        "name": instance.name,
        "status": instance.status,
        "shopping_ads_type": instance.shopping_ads_type,
        "optimization_goal": instance.optimization_goal,
        "roas_bid": str(instance.roas_bid) if instance.roas_bid is not None else None,
        "daily_budget_cents": instance.daily_budget_cents,
        "currency": instance.currency,
        "ext_created_time": instance.ext_created_time.isoformat() if instance.ext_created_time else None,
        "ext_updated_time": instance.ext_updated_time.isoformat() if instance.ext_updated_time else None,
    }


def _require(value: Optional[str], name: str) -> str:
    if not value:
        raise HTTPException(status_code=422, detail=f"{name} is required")
    return value


def _serialize_metric_hourly(instance: TTBGmvMaxMetricsHourly) -> Dict[str, Any]:
    return {
        "interval_start": instance.interval_start.isoformat() if instance.interval_start else None,
        "interval_end": instance.interval_end.isoformat() if instance.interval_end else None,
        "impressions": instance.impressions,
        "clicks": instance.clicks,
        "cost_cents": instance.cost_cents,
        "net_cost_cents": instance.net_cost_cents,
        "orders": instance.orders,
        "gross_revenue_cents": instance.gross_revenue_cents,
        "roi": str(instance.roi) if instance.roi is not None else None,
        "product_impressions": instance.product_impressions,
        "product_clicks": instance.product_clicks,
        "product_click_rate": str(instance.product_click_rate) if instance.product_click_rate is not None else None,
        "ad_click_rate": str(instance.ad_click_rate) if instance.ad_click_rate is not None else None,
        "ad_conversion_rate": str(instance.ad_conversion_rate) if instance.ad_conversion_rate is not None else None,
        "video_views_2s": instance.video_views_2s,
        "video_views_6s": instance.video_views_6s,
        "video_views_p25": instance.video_views_p25,
        "video_views_p50": instance.video_views_p50,
        "video_views_p75": instance.video_views_p75,
        "video_views_p100": instance.video_views_p100,
        "live_views": instance.live_views,
        "live_follows": instance.live_follows,
    }


def _serialize_metric_daily(instance: TTBGmvMaxMetricsDaily) -> Dict[str, Any]:
    return {
        "date": instance.date.isoformat() if instance.date else None,
        "impressions": instance.impressions,
        "clicks": instance.clicks,
        "cost_cents": instance.cost_cents,
        "net_cost_cents": instance.net_cost_cents,
        "orders": instance.orders,
        "gross_revenue_cents": instance.gross_revenue_cents,
        "roi": str(instance.roi) if instance.roi is not None else None,
        "product_impressions": instance.product_impressions,
        "product_clicks": instance.product_clicks,
        "product_click_rate": str(instance.product_click_rate) if instance.product_click_rate is not None else None,
        "ad_click_rate": str(instance.ad_click_rate) if instance.ad_click_rate is not None else None,
        "ad_conversion_rate": str(instance.ad_conversion_rate) if instance.ad_conversion_rate is not None else None,
        "live_views": instance.live_views,
        "live_follows": instance.live_follows,
    }


def _serialize_action_log(instance: TTBGmvMaxActionLog) -> Dict[str, Any]:
    return {
        "id": instance.id,
        "campaign_id": instance.campaign_id,
        "action": instance.action,
        "reason": instance.reason,
        "performed_by": instance.performed_by,
        "result": instance.result,
        "error_message": instance.error_message,
        "before": instance.before_json,
        "after": instance.after_json,
        "created_at": instance.created_at.isoformat() if getattr(instance, "created_at", None) else None,
    }


def _decimal_to_str(value: Decimal | None) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def _decimal_to_float(value: Decimal | None) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _strategy_to_response(cfg: TTBGmvMaxStrategyConfig) -> Dict[str, Any]:
    return {
        "workspace_id": cfg.workspace_id,
        "auth_id": cfg.auth_id,
        "campaign_id": cfg.campaign_id,
        "enabled": bool(cfg.enabled),
        "target_roi": _decimal_to_str(cfg.target_roi),
        "min_roi": _decimal_to_str(cfg.min_roi),
        "max_roi": _decimal_to_str(cfg.max_roi),
        "min_impressions": cfg.min_impressions,
        "min_clicks": cfg.min_clicks,
        "max_budget_raise_pct_per_day": _decimal_to_float(cfg.max_budget_raise_pct_per_day),
        "max_budget_cut_pct_per_day": _decimal_to_float(cfg.max_budget_cut_pct_per_day),
        "max_roas_step_per_adjust": _decimal_to_str(cfg.max_roas_step_per_adjust),
        "cooldown_minutes": cfg.cooldown_minutes,
        "min_runtime_minutes_before_first_change": cfg.min_runtime_minutes_before_first_change,
    }


class StrategyConfigIn(BaseModel):
    enabled: bool = False
    target_roi: Optional[str] = None
    min_roi: Optional[str] = None
    max_roi: Optional[str] = None
    min_impressions: Optional[int] = None
    min_clicks: Optional[int] = None
    max_budget_raise_pct_per_day: Optional[float] = None
    max_budget_cut_pct_per_day: Optional[float] = None
    max_roas_step_per_adjust: Optional[str] = None
    cooldown_minutes: Optional[int] = None
    min_runtime_minutes_before_first_change: Optional[int] = None


class StrategyConfigOut(StrategyConfigIn):
    workspace_id: int
    auth_id: int
    campaign_id: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid date format") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid datetime format") from exc


@router.get("/campaigns")
async def list_campaigns(
    workspace_id: int = Path(..., ge=1),
    advertiser_id: str = Query(...),
    auth_id: int = Query(..., ge=1),
    sync: bool = Query(False),
    db: Session = Depends(get_db),
):
    """List campaigns and optionally refresh from the TikTok API."""

    advertiser = _require(advertiser_id, "advertiser_id")

    sync_result: Optional[Dict[str, Any]] = None
    if sync:
        client = build_ttb_client(db, auth_id)
        try:
            sync_result = await sync_gmvmax_campaigns(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser,
            )
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser)
        .order_by(TTBGmvMaxCampaign.id.desc())
    )
    rows: Sequence[TTBGmvMaxCampaign] = db.execute(stmt).scalars().all()

    response: Dict[str, Any] = {
        "items": [_serialize_campaign(row) for row in rows],
        "count": len(rows),
    }
    if sync_result is not None:
        response["synced"] = sync_result.get("synced")
    return response


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    advertiser_id: str = Query(...),
    auth_id: int = Query(..., ge=1),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Return a single campaign, optionally refreshing from the TikTok API."""

    advertiser = _require(advertiser_id, "advertiser_id")

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    instance = db.execute(stmt).scalars().first()

    if instance is None and refresh:
        client = build_ttb_client(db, auth_id)
        try:
            await sync_gmvmax_campaigns(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser,
                campaign_ids=[str(campaign_id)],
            )
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
            instance = db.execute(stmt).scalars().first()

    if instance is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    return {"campaign": _serialize_campaign(instance)}


@router.post("/campaigns/{campaign_id}/metrics/sync")
async def sync_metrics(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Synchronize metrics for a given campaign."""

    advertiser = _require(payload.get("advertiser_id"), "advertiser_id")
    auth_id_value = payload.get("auth_id")
    try:
        auth_id = int(auth_id_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="auth_id is required") from None
    if auth_id <= 0:
        raise HTTPException(status_code=422, detail="auth_id is required")

    granularity = str(payload.get("granularity") or "DAY").upper()
    start_date = _require(payload.get("start_date"), "start_date")
    end_date = _require(payload.get("end_date"), "end_date")

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    client = build_ttb_client(db, auth_id)
    try:
        if granularity == "HOUR":
            result = await sync_gmvmax_metrics_hourly(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser,
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            result = await sync_gmvmax_metrics_daily(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser,
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
        return {"synced_rows": result.get("synced_rows")}


@router.post("/campaigns/actions")
async def campaign_action(
    workspace_id: int = Path(..., ge=1),
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Apply a campaign action through the GMV Max service."""

    advertiser = _require(payload.get("advertiser_id"), "advertiser_id")
    auth_id_value = payload.get("auth_id")
    try:
        auth_id = int(auth_id_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="auth_id is required") from None
    if auth_id <= 0:
        raise HTTPException(status_code=422, detail="auth_id is required")

    campaign_identifier = _require(payload.get("campaign_id"), "campaign_id")
    action = _require(payload.get("action"), "action").upper()
    reason = payload.get("reason")

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.advertiser_id == advertiser)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_identifier))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    client = build_ttb_client(db, auth_id)

    action_payload: Dict[str, Any] = {}
    if "daily_budget_cents" in payload:
        action_payload["daily_budget_cents"] = payload["daily_budget_cents"]
    if "roas_bid" in payload:
        action_payload["roas_bid"] = payload["roas_bid"]

    try:
        log_row = await apply_campaign_action(
            db,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser,
            campaign=campaign,
            action=action,
            payload=action_payload,
            reason=reason,
            performed_by="api",
        )
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
        return {"result": log_row.result, "log_id": log_row.id}


@router.get("/campaigns/{campaign_id}/metrics")
async def query_metrics(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    granularity: str = Query("DAY", pattern=r"^(?i)(DAY|HOUR)$"),
    start: Optional[str] = Query(None, description="ISO date or datetime"),
    end: Optional[str] = Query(None, description="ISO date or datetime"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    auth_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    """Return stored metrics for a campaign in either hourly or daily granularity."""

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    if auth_id:
        stmt = stmt.where(TTBGmvMaxCampaign.auth_id == int(auth_id))
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    gran = granularity.upper()

    if gran == "HOUR":
        query = select(TTBGmvMaxMetricsHourly).where(
            TTBGmvMaxMetricsHourly.campaign_id == campaign.id
        )
        if start:
            query = query.where(
                TTBGmvMaxMetricsHourly.interval_start >= _parse_datetime(start)
            )
        if end:
            query = query.where(
                TTBGmvMaxMetricsHourly.interval_start < _parse_datetime(end)
            )
        query = (
            query.order_by(TTBGmvMaxMetricsHourly.interval_start.asc())
            .limit(limit)
            .offset(offset)
        )
        rows: Sequence[TTBGmvMaxMetricsHourly] = db.execute(query).scalars().all()
        return {
            "granularity": "HOUR",
            "count": len(rows),
            "items": [_serialize_metric_hourly(row) for row in rows],
        }

    query = select(TTBGmvMaxMetricsDaily).where(
        TTBGmvMaxMetricsDaily.campaign_id == campaign.id
    )
    if start:
        query = query.where(TTBGmvMaxMetricsDaily.date >= _parse_date(start))
    if end:
        query = query.where(TTBGmvMaxMetricsDaily.date < _parse_date(end))
    query = (
        query.order_by(TTBGmvMaxMetricsDaily.date.asc()).limit(limit).offset(offset)
    )
    rows: Sequence[TTBGmvMaxMetricsDaily] = db.execute(query).scalars().all()
    return {
        "granularity": "DAY",
        "count": len(rows),
        "items": [_serialize_metric_daily(row) for row in rows],
    }


@router.get("/campaigns/{campaign_id}/actions")
async def list_action_logs(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Return action logs for a campaign."""

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    query = (
        select(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
        .order_by(TTBGmvMaxActionLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows: Sequence[TTBGmvMaxActionLog] = db.execute(query).scalars().all()
    return {"count": len(rows), "items": [_serialize_action_log(row) for row in rows]}


def _parse_decimal(value: Optional[str | float | int | Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):  # pragma: no cover - guard rails
        raise HTTPException(status_code=422, detail="invalid decimal value")


@router.get("/campaigns/{campaign_id}/strategy", response_model=StrategyConfigOut)
async def get_strategy(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    auth_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
):
    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )
    db.commit()
    db.refresh(cfg)
    return StrategyConfigOut(**_strategy_to_response(cfg))


@router.put("/campaigns/{campaign_id}/strategy", response_model=StrategyConfigOut)
async def update_strategy(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    auth_id: int = Query(..., ge=1),
    payload: StrategyConfigIn = Body(...),
    db: Session = Depends(get_db),
):
    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )

    cfg.enabled = payload.enabled
    cfg.target_roi = _parse_decimal(payload.target_roi)
    cfg.min_roi = _parse_decimal(payload.min_roi)
    cfg.max_roi = _parse_decimal(payload.max_roi)
    cfg.min_impressions = payload.min_impressions
    cfg.min_clicks = payload.min_clicks
    cfg.max_budget_raise_pct_per_day = _parse_decimal(payload.max_budget_raise_pct_per_day)
    cfg.max_budget_cut_pct_per_day = _parse_decimal(payload.max_budget_cut_pct_per_day)
    cfg.max_roas_step_per_adjust = _parse_decimal(payload.max_roas_step_per_adjust)
    cfg.cooldown_minutes = payload.cooldown_minutes
    cfg.min_runtime_minutes_before_first_change = payload.min_runtime_minutes_before_first_change

    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return StrategyConfigOut(**_strategy_to_response(cfg))


@router.get("/campaigns/{campaign_id}/strategy/preview")
async def preview_strategy(
    workspace_id: int = Path(..., ge=1),
    campaign_id: str = Path(...),
    auth_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
):
    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    campaign = db.execute(stmt).scalars().first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")

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
    metrics["roi"] = _decimal_to_str(metrics.get("roi"))

    decision = decide_campaign_action(
        campaign=campaign,
        strategy=cfg,
        metrics=metrics_raw,
    )
    decision_payload = dict(decision) if decision else None

    return {
        "enabled": True,
        "metrics": metrics,
        "decision": decision_payload,
    }
