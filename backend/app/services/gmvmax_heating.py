"""Creative heating auto-monitoring helpers for GMV Max."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    CreativeMetricsAggregate,
    get_recent_creative_metrics,
    upsert_creative_metrics,
)
from app.data.repositories.tiktok_business.gmvmax_heating import (
    update_heating_action_result,
    update_heating_evaluation,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignActionApplyBody,
    GMVMaxCampaignActionApplyRequest,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.gmvmax_spec import GMVMAX_CREATIVE_METRICS
from app.services.ttb_client_factory import build_ttb_gmvmax_client

logger = logging.getLogger("gmv.services.gmvmax.heating")

_CREATIVE_DIMENSIONS = ["campaign_id", "creative_id", "stat_time_day"]
_CREATIVE_METRICS = list(GMVMAX_CREATIVE_METRICS)
_REPORT_PAGE_SIZE = 200
_DEFAULT_PROVIDER = "tiktok-business"


@dataclass
class HeatingEvaluationResult:
    """Result of evaluating a heating configuration against metrics."""

    result: str
    should_stop: bool


def evaluate_heating_rule(
    config: TTBGmvMaxCreativeHeating,
    metrics: CreativeMetricsAggregate | None,
) -> HeatingEvaluationResult:
    """Return whether the creative passes thresholds and if auto-stop is needed."""

    if metrics is None:
        return HeatingEvaluationResult(result="metrics_missing", should_stop=False)

    clicks_actual = metrics.clicks or 0
    ctr_actual = metrics.ad_click_rate if metrics.ad_click_rate is not None else 0.0
    revenue_actual = metrics.gross_revenue if metrics.gross_revenue is not None else 0

    if config.min_clicks is not None and clicks_actual < int(config.min_clicks):
        if config.auto_stop_enabled and config.is_heating_active:
            return HeatingEvaluationResult("auto_stopped_low_clicks", True)
        return HeatingEvaluationResult("threshold_failed_low_clicks", False)

    if config.min_ctr is not None:
        try:
            threshold_ctr = float(config.min_ctr)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            threshold_ctr = float(config.min_ctr or 0)
        if ctr_actual is None or ctr_actual < threshold_ctr:
            if config.auto_stop_enabled and config.is_heating_active:
                return HeatingEvaluationResult("auto_stopped_low_ctr", True)
            return HeatingEvaluationResult("threshold_failed_low_ctr", False)

    if config.min_gross_revenue is not None:
        try:
            threshold_revenue = float(config.min_gross_revenue)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            threshold_revenue = float(config.min_gross_revenue or 0)
        revenue_value = float(revenue_actual or 0)
        if revenue_value < threshold_revenue:
            if config.auto_stop_enabled and config.is_heating_active:
                return HeatingEvaluationResult("auto_stopped_low_revenue", True)
            return HeatingEvaluationResult("threshold_failed_low_revenue", False)

    return HeatingEvaluationResult(result="ok", should_stop=False)


async def _sync_creative_metrics_for_campaign(
    db: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign: TTBGmvMaxCampaign,
    start_date: date,
    end_date: date,
) -> int:
    if not campaign.store_id or not campaign.advertiser_id:
        logger.debug(
            "skip creative metrics sync because store_id or advertiser_id missing",
            extra={
                "campaign_id": campaign.campaign_id,
                "workspace_id": workspace_id,
                "auth_id": auth_id,
            },
        )
        return 0

    page = 1
    rows = 0
    filtering = GMVMaxReportFiltering.model_validate({"campaign_ids": [campaign.campaign_id]})

    while True:
        request = GMVMaxReportGetRequest(
            advertiser_id=str(campaign.advertiser_id),
            store_ids=[str(campaign.store_id)],
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            metrics=_CREATIVE_METRICS,
            dimensions=_CREATIVE_DIMENSIONS,
            filtering=filtering,
            page=page,
            page_size=_REPORT_PAGE_SIZE,
        )
        response = await client.gmv_max_report_get(request)
        data = response.data
        entries = list(data.list)
        if not entries:
            break

        for entry in entries:
            dims = entry.dimensions or {}
            metrics_payload = dict(entry.metrics or {})
            creative_id = dims.get("creative_id")
            stat_time = dims.get("stat_time_day") or dims.get("date")
            if not creative_id or not stat_time:
                continue
            for meta_field in ("creative_name", "adgroup_id", "product_id", "item_id"):
                if meta_field in dims and dims[meta_field] is not None:
                    metrics_payload.setdefault(meta_field, dims[meta_field])
            stat_datetime = _parse_stat_time(stat_time)
            await upsert_creative_metrics(
                db,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                campaign_id=campaign.campaign_id,
                creative_id=str(creative_id),
                stat_time_day=stat_datetime,
                metrics=metrics_payload,
            )
            rows += 1

        page_info = (
            data.page_info.model_dump(exclude_none=True) if data.page_info else {}
        )
        has_more = bool(page_info.get("has_more")) or bool(page_info.get("has_next"))
        total_page = page_info.get("total_page")
        if has_more:
            page += 1
            continue
        try:
            total_page_int = int(total_page) if total_page is not None else None
        except (TypeError, ValueError):  # pragma: no cover - defensive
            total_page_int = None
        if total_page_int is not None and page < total_page_int:
            page += 1
            continue
        break

    db.flush()
    return rows


def _parse_stat_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("empty stat_time_day value")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return datetime.fromisoformat(cleaned)
    raise ValueError(f"unsupported stat_time_day value: {value!r}")


def _group_configs(
    rows: Iterable[TTBGmvMaxCreativeHeating],
) -> Dict[Tuple[int, str, int, str], List[TTBGmvMaxCreativeHeating]]:
    groups: Dict[Tuple[int, str, int, str], List[TTBGmvMaxCreativeHeating]] = defaultdict(list)
    for row in rows:
        key = (row.workspace_id, row.provider, row.auth_id, row.campaign_id)
        groups[key].append(row)
    return groups


def _load_active_heating_configs(db: Session) -> List[TTBGmvMaxCreativeHeating]:
    stmt: Select[TTBGmvMaxCreativeHeating] = (
        select(TTBGmvMaxCreativeHeating)
        .where(TTBGmvMaxCreativeHeating.auto_stop_enabled.is_(True))
        .where(TTBGmvMaxCreativeHeating.is_heating_active.is_(True))
        .order_by(
            TTBGmvMaxCreativeHeating.workspace_id.asc(),
            TTBGmvMaxCreativeHeating.auth_id.asc(),
            TTBGmvMaxCreativeHeating.campaign_id.asc(),
            TTBGmvMaxCreativeHeating.id.asc(),
        )
    )
    return list(db.execute(stmt).scalars().all())


def _load_campaign(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
) -> TTBGmvMaxCampaign | None:
    stmt: Select[TTBGmvMaxCampaign] = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    return db.execute(stmt).scalars().first()


async def _ensure_client(
    clients: Dict[int, TikTokBusinessGMVMaxClient],
    db: Session,
    *,
    auth_id: int,
) -> TikTokBusinessGMVMaxClient:
    if auth_id in clients:
        return clients[auth_id]
    client = build_ttb_gmvmax_client(db, auth_id=auth_id)
    clients[auth_id] = client
    return client


async def _auto_stop_creative(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    campaign: TTBGmvMaxCampaign,
    heating: TTBGmvMaxCreativeHeating,
    evaluation_result: str,
    evaluation_time: datetime,
) -> bool:
    action_body = {
        "campaign_id": str(heating.campaign_id),
        "action_type": "STOP_CREATIVE",
        "creative_id": str(heating.creative_id),
    }
    request = GMVMaxCampaignActionApplyRequest(
        advertiser_id=str(campaign.advertiser_id),
        body=GMVMaxCampaignActionApplyBody(**action_body),
    )

    try:
        response = await client.gmv_max_campaign_action_apply(request)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "auto-stop creative heating failed",
            extra={
                "workspace_id": heating.workspace_id,
                "auth_id": heating.auth_id,
                "campaign_id": heating.campaign_id,
                "creative_id": heating.creative_id,
            },
        )
        await update_heating_action_result(
            db,
            heating_id=heating.id,
            status="FAILED",
            action_type="STOP_CREATIVE",
            action_time=evaluation_time,
            request_payload=action_body,
            response_payload=None,
            error_message=str(exc),
        )
        await update_heating_evaluation(
            db,
            heating_id=heating.id,
            evaluated_at=evaluation_time,
            evaluation_result=f"{evaluation_result}_failed",
            is_heating_active=True,
        )
        return False

    payload = response.data.model_dump(exclude_none=True)
    await update_heating_action_result(
        db,
        heating_id=heating.id,
        status="CANCELLED",
        action_type="STOP_CREATIVE",
        action_time=evaluation_time,
        request_payload=action_body,
        response_payload=payload,
        error_message=None,
    )
    await update_heating_evaluation(
        db,
        heating_id=heating.id,
        evaluated_at=evaluation_time,
        evaluation_result=evaluation_result,
        is_heating_active=False,
    )
    return True


async def run_creative_heating_cycle(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate heating configs and auto-stop under-performing creatives."""

    configs = _load_active_heating_configs(db)
    if not configs:
        return {"processed": 0, "stopped": 0, "campaigns": 0}

    cycle_time = now or datetime.now(timezone.utc)
    grouped = _group_configs(configs)
    summary = {
        "processed": len(configs),
        "stopped": 0,
        "campaigns": len(grouped),
    }

    logger.info(
        "starting creative heating evaluation",
        extra={"groups": len(grouped), "configs": len(configs)},
    )

    clients: Dict[int, TikTokBusinessGMVMaxClient] = {}
    try:
        for (workspace_id, provider, auth_id, campaign_id), items in grouped.items():
            if provider and provider != _DEFAULT_PROVIDER:
                for heating in items:
                    await update_heating_evaluation(
                        db,
                        heating_id=heating.id,
                        evaluated_at=cycle_time,
                        evaluation_result="unsupported_provider",
                        is_heating_active=False,
                    )
                continue

            campaign = _load_campaign(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                campaign_id=campaign_id,
            )
            if campaign is None:
                logger.warning(
                    "campaign missing for heating config",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "campaign_id": campaign_id,
                    },
                )
                for heating in items:
                    await update_heating_evaluation(
                        db,
                        heating_id=heating.id,
                        evaluated_at=cycle_time,
                        evaluation_result="campaign_missing",
                        is_heating_active=False,
                    )
                continue

            try:
                client = await _ensure_client(clients, db, auth_id=auth_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to build GMV Max client",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                    },
                )
                for heating in items:
                    await update_heating_evaluation(
                        db,
                        heating_id=heating.id,
                        evaluated_at=cycle_time,
                        evaluation_result="client_error",
                        is_heating_active=True,
                    )
                continue

            try:
                max_window = max((item.evaluation_window_minutes or 60) for item in items)
                start_day = (cycle_time - timedelta(minutes=max_window)).date()
                end_day = cycle_time.date()
                await _sync_creative_metrics_for_campaign(
                    db,
                    client,
                    workspace_id=workspace_id,
                    provider=provider or _DEFAULT_PROVIDER,
                    auth_id=auth_id,
                    campaign=campaign,
                    start_date=start_day,
                    end_date=end_day,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "syncing creative metrics failed",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "campaign_id": campaign_id,
                    },
                )

            for heating in items:
                try:
                    window = heating.evaluation_window_minutes or 60
                    metrics_map = await get_recent_creative_metrics(
                        db,
                        workspace_id=workspace_id,
                        provider=provider or _DEFAULT_PROVIDER,
                        auth_id=auth_id,
                        campaign_id=campaign_id,
                        window_minutes=window,
                        creative_ids=[heating.creative_id],
                    )
                    metrics = metrics_map.get(str(heating.creative_id))
                    evaluation = evaluate_heating_rule(heating, metrics)

                    if evaluation.should_stop:
                        stopped = await _auto_stop_creative(
                            db,
                            client=client,
                            campaign=campaign,
                            heating=heating,
                            evaluation_result=evaluation.result,
                            evaluation_time=cycle_time,
                        )
                        if stopped:
                            summary["stopped"] += 1
                    else:
                        await update_heating_evaluation(
                            db,
                            heating_id=heating.id,
                            evaluated_at=cycle_time,
                            evaluation_result=evaluation.result,
                            is_heating_active=heating.is_heating_active,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "heating evaluation failed",
                        extra={
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "campaign_id": campaign_id,
                            "creative_id": heating.creative_id,
                        },
                    )
                    await update_heating_evaluation(
                        db,
                        heating_id=heating.id,
                        evaluated_at=cycle_time,
                        evaluation_result="evaluation_error",
                        is_heating_active=heating.is_heating_active,
                    )
    finally:
        for client in clients.values():
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("gmvmax client close failed", exc_info=True)

    logger.info(
        "creative heating evaluation completed",
        extra=summary,
    )
    return summary
