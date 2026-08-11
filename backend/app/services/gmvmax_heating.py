"""Creative heating auto-monitoring helpers for GMV Max."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    CreativeMetricsAggregate,
    get_recent_creative_metrics,
)
from app.data.repositories.tiktok_business.gmvmax_heating import (
    update_heating_evaluation,
)
from app.providers.tiktok_business.gmvmax_client import TikTokBusinessGMVMaxClient
from app.services.gmvmax_heating_actions import stop_boost_creative_session
from app.services.ttb_client_factory import build_ttb_gmvmax_client

logger = logging.getLogger("gmv.services.gmvmax.heating")


@dataclass
class HeatingEvaluationResult:
    """Result of evaluating a heating configuration against metrics."""

    result: str
    should_stop: bool
    ready_to_heat: bool = False


@dataclass(frozen=True)
class HeatingCampaign:
    """Canonical catalog identity required by creative heating."""

    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str
    campaign_id: str
    promotion_type: str
    campaign_name: str | None
    operation_status: str | None
    secondary_status: str | None
    budget_cents: int | None


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

    return HeatingEvaluationResult(result="ready_to_heat", should_stop=False, ready_to_heat=True)


def _group_configs(
    rows: Iterable[TTBGmvMaxCreativeHeating],
) -> Dict[Tuple[int, int, str, str, str], List[TTBGmvMaxCreativeHeating]]:
    groups: Dict[
        Tuple[int, int, str, str, str],
        List[TTBGmvMaxCreativeHeating],
    ] = defaultdict(list)
    for row in rows:
        promotion_type = getattr(row.promotion_type, "value", row.promotion_type)
        key = (
            int(row.workspace_id),
            int(row.auth_id),
            str(row.advertiser_id),
            str(row.campaign_id),
            str(promotion_type or "").upper(),
        )
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
    advertiser_id: str,
    campaign_id: str,
    promotion_type: str,
) -> HeatingCampaign | None:
    """Load one campaign through the authoritative account binding and catalog."""

    binding = db.execute(
        select(TTBBindingConfig)
        .where(TTBBindingConfig.workspace_id == int(workspace_id))
        .where(TTBBindingConfig.auth_id == int(auth_id))
        .limit(1)
    ).scalars().first()
    bound_advertiser = str(getattr(binding, "advertiser_id", "") or "").strip()
    bound_store = str(getattr(binding, "store_id", "") or "").strip()
    if (
        binding is None
        or not bound_advertiser
        or not bound_store
        or bound_advertiser != str(advertiser_id)
    ):
        return None

    normalized_promotion = str(promotion_type).upper()
    if normalized_promotion == "PRODUCT":
        model = GmvmaxProductCampaignCatalog
    elif normalized_promotion == "LIVE":
        model = GmvmaxLiveCampaignCatalog
    else:
        return None
    stmt: Select[Any] = (
        select(model)
        .where(model.workspace_id == int(workspace_id))
        .where(model.auth_id == int(auth_id))
        .where(model.advertiser_id == bound_advertiser)
        .where(model.store_id == bound_store)
        .where(model.campaign_id == str(campaign_id))
        .order_by(model.updated_at.desc())
        .limit(2)
    )
    rows = list(db.execute(stmt).scalars().all())
    if len(rows) != 1:
        return None
    row = rows[0]
    return HeatingCampaign(
        workspace_id=int(row.workspace_id),
        auth_id=int(row.auth_id),
        advertiser_id=str(row.advertiser_id),
        store_id=str(row.store_id),
        campaign_id=str(row.campaign_id),
        promotion_type=str(promotion_type).upper(),
        campaign_name=getattr(row, "campaign_name", None),
        operation_status=getattr(row, "operation_status", None),
        secondary_status=getattr(row, "secondary_status", None),
        budget_cents=getattr(row, "budget_cents", None),
    )


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
    campaign: HeatingCampaign,
    heating: TTBGmvMaxCreativeHeating,
    evaluation_result: str,
    evaluation_time: datetime,
) -> bool:
    try:
        await stop_boost_creative_session(
            db,
            client=client,
            campaign=campaign,
            heating=heating,
            note=evaluation_result,
        )
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
        await update_heating_evaluation(
            db,
            heating_id=heating.id,
            evaluated_at=evaluation_time,
            evaluation_result=f"{evaluation_result}_failed",
            is_heating_active=True,
        )
        return False

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
        for (
            workspace_id,
            auth_id,
            advertiser_id,
            campaign_id,
            promotion_type,
        ), items in grouped.items():
            campaign = _load_campaign(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                campaign_id=campaign_id,
                promotion_type=promotion_type,
            )
            if campaign is None:
                logger.warning(
                    "canonical campaign scope missing for heating config",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "campaign_id": campaign_id,
                        "promotion_type": promotion_type,
                    },
                )
                for heating in items:
                    await update_heating_evaluation(
                        db,
                        heating_id=heating.id,
                        evaluated_at=cycle_time,
                        evaluation_result="campaign_scope_missing",
                        is_heating_active=heating.is_heating_active,
                    )
                continue

            for heating in items:
                try:
                    item_group_id = str(
                        getattr(heating, "item_group_id", None)
                        or getattr(heating, "product_id", None)
                        or ""
                    ).strip()
                    if promotion_type != "PRODUCT" or not item_group_id:
                        await update_heating_evaluation(
                            db,
                            heating_id=heating.id,
                            evaluated_at=cycle_time,
                            evaluation_result="item_group_scope_missing",
                            is_heating_active=heating.is_heating_active,
                        )
                        continue

                    window = heating.evaluation_window_minutes or 60
                    metrics_map = await get_recent_creative_metrics(
                        db,
                        workspace_id=workspace_id,
                        provider="tiktok-business",
                        auth_id=auth_id,
                        advertiser_id=campaign.advertiser_id,
                        store_id=campaign.store_id,
                        campaign_id=campaign_id,
                        item_group_id=item_group_id,
                        window_minutes=window,
                        creative_ids=[heating.creative_id],
                        now=cycle_time,
                    )
                    metrics = metrics_map.get(str(heating.creative_id))
                    evaluation = evaluate_heating_rule(heating, metrics)

                    if evaluation.should_stop:
                        try:
                            client = await _ensure_client(
                                clients,
                                db,
                                auth_id=auth_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "failed to build GMV Max client",
                                extra={
                                    "workspace_id": workspace_id,
                                    "auth_id": auth_id,
                                    "campaign_id": campaign_id,
                                },
                            )
                            await update_heating_evaluation(
                                db,
                                heating_id=heating.id,
                                evaluated_at=cycle_time,
                                evaluation_result="client_error",
                                is_heating_active=True,
                            )
                            continue
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
