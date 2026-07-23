from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from statistics import median
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.gmv_restructured import GmvProductMetricsHourly, GmvProductOrderEvent
from app.data.models.ttb_entities import TTBAdvertiser, TTBAdvertiserStoreLink
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsCampaign,
    WebsiteAdsConversionGuardState,
    WebsiteAdsLandingPage,
    WebsiteAdsMetricHourly,
)
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.gmv_product_order_events import sync_product_order_events_from_hourly
from app.services.website_ads_execution_lock import (
    assert_website_ads_execution_lock,
)
from app.services.website_ads_hermes_planner import review_website_campaign_conversion_guard_action


ACTIVE_CAMPAIGN_STATUSES = {"ACTIVE", "ENABLE"}
PAUSED_CAMPAIGN_STATUSES = {"PAUSED", "DISABLE"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "", "-") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _campaign_enabled(campaign: WebsiteAdsCampaign) -> bool:
    return (
        str(campaign.local_status or "").upper() in ACTIVE_CAMPAIGN_STATUSES
        and str(campaign.operation_status or "").upper() in ACTIVE_CAMPAIGN_STATUSES
    )


def _campaign_paused(campaign: WebsiteAdsCampaign) -> bool:
    return (
        str(campaign.local_status or "").upper() in PAUSED_CAMPAIGN_STATUSES
        or str(campaign.operation_status or "").upper() in PAUSED_CAMPAIGN_STATUSES
    )


def _advertiser_zone(db: Session, campaign: WebsiteAdsCampaign) -> tuple[str, ZoneInfo | timezone]:
    advertiser = db.scalar(
        select(TTBAdvertiser).where(
            TTBAdvertiser.workspace_id == int(campaign.workspace_id),
            TTBAdvertiser.auth_id == int(campaign.auth_id),
            TTBAdvertiser.advertiser_id == str(campaign.advertiser_id),
        )
    )
    name = str((advertiser.display_timezone or advertiser.timezone) if advertiser else "UTC")
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC", timezone.utc


def resolve_website_ads_store_id(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    """Return the only authorized store for an ads scope, otherwise fail closed."""

    store_ids = {
        str(value).strip()
        for value in db.scalars(
            select(TTBAdvertiserStoreLink.store_id).where(
                TTBAdvertiserStoreLink.workspace_id == int(workspace_id),
                TTBAdvertiserStoreLink.auth_id == int(auth_id),
                TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id),
            )
        ).all()
        if str(value or "").strip()
    }
    if len(store_ids) != 1:
        return None
    return next(iter(store_ids))


def _campaign_totals(db: Session, campaign_id: int) -> tuple[Decimal, int]:
    values = db.execute(
        select(
            func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
        )
        .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
        .where(WebsiteAdsAd.campaign_local_id == int(campaign_id))
    ).one()
    return _decimal(values[0]), int(values[1] or 0)


def _order_snapshot(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    product_id: str,
    store_id: str,
    lookback_start: datetime,
) -> dict[str, Any]:
    total, last_order_at = db.execute(
        select(
            func.coalesce(func.sum(GmvProductOrderEvent.order_count), 0),
            func.max(GmvProductOrderEvent.order_time_hour),
        ).where(
            GmvProductOrderEvent.workspace_id == int(campaign.workspace_id),
            GmvProductOrderEvent.auth_id == int(campaign.auth_id),
            GmvProductOrderEvent.advertiser_id == str(campaign.advertiser_id),
            GmvProductOrderEvent.store_id == str(store_id),
            GmvProductOrderEvent.item_group_id == str(product_id),
            GmvProductOrderEvent.order_time_hour >= lookback_start,
        )
    ).one()
    event_hours = list(
        db.scalars(
            select(GmvProductOrderEvent.order_time_hour)
            .where(
                GmvProductOrderEvent.workspace_id == int(campaign.workspace_id),
                GmvProductOrderEvent.auth_id == int(campaign.auth_id),
                GmvProductOrderEvent.advertiser_id == str(campaign.advertiser_id),
                GmvProductOrderEvent.store_id == str(store_id),
                GmvProductOrderEvent.item_group_id == str(product_id),
                GmvProductOrderEvent.order_time_hour >= lookback_start,
            )
            .order_by(GmvProductOrderEvent.order_time_hour)
        ).all()
    )
    return {
        "order_count": int(total or 0),
        "last_order_at": last_order_at,
        "event_hours": event_hours,
    }


def _source_snapshot(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    product_id: str,
    store_id: str,
    advertiser_now: datetime,
) -> dict[str, Any]:
    latest_hour = db.scalar(
        select(func.max(GmvProductMetricsHourly.stat_time_hour)).where(
            GmvProductMetricsHourly.workspace_id == int(campaign.workspace_id),
            GmvProductMetricsHourly.auth_id == int(campaign.auth_id),
            GmvProductMetricsHourly.advertiser_id == str(campaign.advertiser_id),
            GmvProductMetricsHourly.store_id == str(store_id),
            GmvProductMetricsHourly.item_group_id == str(product_id),
        )
    )
    lag_minutes: int | None = None
    if latest_hour is not None:
        lag_minutes = max(0, int((advertiser_now.replace(tzinfo=None) - latest_hour).total_seconds() / 60))
    max_lag = max(60, int(settings.WEBSITE_ADS_CONVERSION_GUARD_SOURCE_MAX_LAG_MINUTES))
    return {
        "latest_hour": latest_hour,
        "lag_minutes": lag_minutes,
        "max_lag_minutes": max_lag,
        "fresh": latest_hour is not None and lag_minutes is not None and lag_minutes <= max_lag,
    }


def _historical_gap_minutes(event_hours: list[datetime]) -> float | None:
    values = [
        max(0.0, (right - left).total_seconds() / 60)
        for left, right in zip(event_hours, event_hours[1:])
        if right > left
    ]
    if not values:
        return None
    values.sort()
    rank = max(0, math.ceil(len(values) * 0.75) - 1)
    return float(values[rank])


def _probe_interval_minutes() -> int:
    return max(
        30,
        int(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_INTERVAL_MINUTES),
    )


def _next_probe_at(*, probe_started_at: datetime, now: datetime, interval_minutes: int) -> datetime:
    scheduled = probe_started_at + timedelta(minutes=max(1, int(interval_minutes)))
    return scheduled if scheduled > now else now + timedelta(minutes=1)


def _hourly_probe_evidence(
    *,
    elapsed_minutes: int,
    incremental_spend: Decimal,
    incremental_clicks: int,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    minimum_runtime = max(1, int(policy["probe_min_runtime_minutes"]))
    maximum_runtime = max(minimum_runtime + 1, int(policy["probe_max_runtime_minutes"]))
    target_spend = _decimal(policy["probe_target_spend"])
    target_clicks = max(1, int(policy["probe_target_clicks"]))
    spend_cap_reached = incremental_spend >= target_spend
    click_sample_ready = incremental_clicks >= target_clicks
    sample_ready = spend_cap_reached and click_sample_ready
    timed_out = elapsed_minutes >= maximum_runtime
    should_pause = (
        spend_cap_reached
        or timed_out
        or (elapsed_minutes >= minimum_runtime and click_sample_ready)
    )
    return {
        "elapsed_minutes": max(0, int(elapsed_minutes)),
        "incremental_website_spend": float(max(Decimal("0"), incremental_spend)),
        "incremental_website_clicks": max(0, int(incremental_clicks)),
        "target_spend": float(target_spend),
        "target_clicks": target_clicks,
        "minimum_runtime_minutes": minimum_runtime,
        "maximum_runtime_minutes": maximum_runtime,
        "sample_ready": sample_ready,
        "spend_cap_reached": spend_cap_reached,
        "click_sample_ready": click_sample_ready,
        "timed_out": timed_out,
        "should_pause": should_pause,
    }


def derive_cross_channel_policy(
    *,
    reference_price: Decimal,
    event_hours: list[datetime],
    pause_count: int,
    incremental_spend: Decimal = Decimal("0"),
    incremental_clicks: int = 0,
) -> dict[str, Any]:
    """Derive evidence and cooldown gates from price, history and current velocity."""

    price = max(Decimal("1"), _decimal(reference_price, "10"))
    min_spend = Decimal(str(_clamp(float(price * Decimal("0.60")), 4.0, 12.0))).quantize(Decimal("0.01"))
    min_clicks = int(round(_clamp(float(price), 10.0, 24.0)))
    historical_gap = _historical_gap_minutes(event_hours)
    default_window = float(settings.WEBSITE_ADS_CONVERSION_GUARD_DEFAULT_OBSERVATION_MINUTES)
    inferred_window = max(default_window, (historical_gap or default_window) * 1.15)
    if incremental_spend >= min_spend * Decimal("2") and incremental_clicks >= min_clicks * 2:
        inferred_window *= 0.8
    observation = int(round(_clamp(
        inferred_window,
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_MIN_OBSERVATION_MINUTES),
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_MAX_OBSERVATION_MINUTES),
    )))
    probe_interval = _probe_interval_minutes()
    probe_min_runtime = max(
        5,
        min(
            int(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_RUNTIME_MINUTES),
            probe_interval - 10,
        ),
    )
    probe_max_runtime = max(
        probe_min_runtime + 5,
        min(
            int(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_RUNTIME_MINUTES),
            probe_interval - 5,
        ),
    )
    probe_target_clicks = int(round(_clamp(
        float(price),
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_CLICKS),
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_CLICKS),
    )))
    observed_cpc = (
        incremental_spend / Decimal(max(1, incremental_clicks))
        if incremental_clicks > 0
        else None
    )
    repeated_failure_factor = _clamp(
        1.0 - (0.05 * max(0, int(pause_count) - 1)),
        0.75,
        1.0,
    )
    price_target = float(price) * float(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_PRICE_RATIO)
    click_target = (
        float(observed_cpc) * probe_target_clicks
        if observed_cpc is not None
        else 0.0
    )
    probe_target_spend = Decimal(str(_clamp(
        max(price_target * repeated_failure_factor, click_target),
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_SPEND),
        float(settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_SPEND),
    ))).quantize(Decimal("0.01"))
    return {
        "reference_price": float(price),
        "minimum_incremental_spend": float(min_spend),
        "minimum_incremental_clicks": min_clicks,
        "observation_minutes": observation,
        "cooldown_minutes": probe_interval,
        "probe_interval_minutes": probe_interval,
        "probe_min_runtime_minutes": probe_min_runtime,
        "probe_max_runtime_minutes": probe_max_runtime,
        "probe_target_spend": float(probe_target_spend),
        "probe_target_clicks": probe_target_clicks,
        "observed_cpc": round(float(observed_cpc), 4) if observed_cpc is not None else None,
        "historical_p75_order_gap_minutes": round(historical_gap, 2) if historical_gap is not None else None,
        "early_resume_min_minutes": max(1, int(settings.WEBSITE_ADS_CONVERSION_GUARD_EARLY_RESUME_MINUTES)),
    }


def _new_state(
    campaign: WebsiteAdsCampaign,
    product: WebsiteAdsLandingPage,
    *,
    status: str,
) -> WebsiteAdsConversionGuardState:
    return WebsiteAdsConversionGuardState(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        campaign_local_id=int(campaign.id),
        product_id=str(product.product_id or ""),
        status=status,
        control_enabled=True,
    )


def _reset_observation(
    state: WebsiteAdsConversionGuardState,
    *,
    now: datetime,
    advertiser_now: datetime,
    website_spend: Decimal,
    website_clicks: int,
    orders: Mapping[str, Any],
) -> None:
    state.observation_started_at = now
    state.source_window_start_hour = advertiser_now.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    state.baseline_website_spend = website_spend
    state.baseline_website_clicks = int(website_clicks)
    state.baseline_order_count = int(orders.get("order_count") or 0)
    state.last_observed_order_count = int(orders.get("order_count") or 0)
    state.last_order_at = orders.get("last_order_at")


def apply_manual_campaign_override(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    operation_status: str,
) -> WebsiteAdsConversionGuardState | None:
    """Make operator intent authoritative over automatic cooldown/resume."""

    product = db.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    if product is None or not str(product.product_id or ""):
        return None
    state = db.scalar(
        select(WebsiteAdsConversionGuardState).where(
            WebsiteAdsConversionGuardState.campaign_local_id == int(campaign.id)
        )
    )
    if state is None:
        state = _new_state(campaign, product, status="OBSERVING")
    now = _utcnow()
    if str(operation_status).upper() == "ENABLE":
        state.status = "OBSERVING"
        state.observation_started_at = None
        state.source_window_start_hour = None
        state.paused_at = None
        state.resume_at = None
        state.state_json = {"manual_override": "ENABLE", "at": now.isoformat()}
    else:
        state.status = "MANUAL_PAUSE"
        state.paused_at = now
        state.resume_at = None
        state.state_json = {"manual_override": "DISABLE", "at": now.isoformat()}
    state.last_evaluated_at = now
    db.add(state)
    return state


async def evaluate_campaign_conversion_guard(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    require_execution_lease: bool = False,
) -> dict[str, Any]:
    def _assert_execution() -> None:
        assert_website_ads_execution_lock(
            db,
            required=require_execution_lease,
        )

    _assert_execution()
    if not bool(settings.WEBSITE_ADS_CONVERSION_GUARD_ENABLED):
        return {"status": "SKIPPED", "reason": "DISABLED"}
    if not campaign.campaign_id:
        return {"status": "SKIPPED", "reason": "REMOTE_CAMPAIGN_MISSING"}
    product = db.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    product_id = str(product.product_id or "") if product else ""
    if product is None or not product_id:
        return {"status": "SKIPPED", "reason": "PRODUCT_MAPPING_MISSING"}

    now = _utcnow()
    state = db.scalar(
        select(WebsiteAdsConversionGuardState).where(
            WebsiteAdsConversionGuardState.campaign_local_id == int(campaign.id)
        )
    )
    if state is not None and not state.control_enabled:
        return {"status": "SKIPPED", "reason": "CONTROL_DISABLED"}
    if state is None:
        state = _new_state(
            campaign,
            product,
            status="MANUAL_PAUSE" if _campaign_paused(campaign) else "OBSERVING",
        )
        db.add(state)
        _assert_execution()
        db.flush()
    if str(state.status or "").upper() == "MANUAL_PAUSE":
        return {"status": "MANUAL_PAUSE", "campaign_local_id": int(campaign.id)}
    if _campaign_paused(campaign) and str(state.status or "").upper() != "COOLDOWN":
        state.status = "MANUAL_PAUSE"
        state.resume_at = None
        state.last_evaluated_at = now
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": "MANUAL_PAUSE", "reason": "EXTERNAL_OR_OPERATOR_PAUSE"}

    if str(state.status or "").upper() == "COOLDOWN" and state.paused_at is not None:
        bounded_resume_at = state.paused_at + timedelta(minutes=_probe_interval_minutes())
        if state.resume_at is None or state.resume_at > bounded_resume_at:
            state.resume_at = bounded_resume_at
            state.policy_json = {
                **dict(state.policy_json or {}),
                "cooldown_minutes": _probe_interval_minutes(),
                "probe_interval_minutes": _probe_interval_minutes(),
                "legacy_cooldown_shortened": True,
            }
            db.add(state)
            _assert_execution()
            db.commit()

    store_id = resolve_website_ads_store_id(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
    )
    if store_id is None:
        previous_status = str(state.status or "OBSERVING").upper()
        state.status = "DATA_HOLD"
        state.last_evaluated_at = now
        state.state_json = {
            **dict(state.state_json or {}),
            "data_hold": True,
            "previous_status": previous_status,
            "reason": "STORE_SCOPE_NOT_UNIQUE",
        }
        db.add(state)
        _assert_execution()
        db.commit()
        return {
            "status": "DATA_HOLD",
            "reason": "STORE_SCOPE_NOT_UNIQUE",
        }

    evaluation_minutes = max(1, int(settings.WEBSITE_ADS_CONVERSION_GUARD_EVALUATION_MINUTES))
    if (
        state.last_evaluated_at is not None
        and now - state.last_evaluated_at < timedelta(minutes=evaluation_minutes)
        and not (str(state.status).upper() == "COOLDOWN" and state.resume_at and state.resume_at <= now)
    ):
        return {"status": "SKIPPED", "reason": "EVALUATION_INTERVAL"}

    timezone_name, zone = _advertiser_zone(db, campaign)
    advertiser_now = datetime.now(zone)
    lookback_days = max(2, int(settings.WEBSITE_ADS_CONVERSION_GUARD_LOOKBACK_DAYS))
    lookback_start = advertiser_now.replace(tzinfo=None) - timedelta(days=lookback_days)
    _assert_execution()
    sync_product_order_events_from_hourly(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        store_id=store_id,
        item_group_id=product_id,
        start_date=lookback_start.date(),
        end_date=advertiser_now.date(),
    )
    orders = _order_snapshot(
        db,
        campaign=campaign,
        product_id=product_id,
        store_id=store_id,
        lookback_start=lookback_start,
    )
    source = _source_snapshot(
        db,
        campaign=campaign,
        product_id=product_id,
        store_id=store_id,
        advertiser_now=advertiser_now,
    )
    website_spend, website_clicks = _campaign_totals(db, int(campaign.id))
    state.last_evaluated_at = now
    state.last_source_hour = source.get("latest_hour")

    if not source["fresh"]:
        previous_status = str(state.status or "OBSERVING").upper()
        if previous_status == "PROBING" and _campaign_enabled(campaign):
            _assert_execution()
            response = await api.update_campaign_status(
                str(campaign.advertiser_id), [str(campaign.campaign_id)], "DISABLE"
            )
            probe_started_at = state.observation_started_at or now
            state.status = "COOLDOWN"
            state.paused_at = now
            state.resume_at = _next_probe_at(
                probe_started_at=probe_started_at,
                now=now,
                interval_minutes=_probe_interval_minutes(),
            )
            campaign.operation_status = "DISABLE"
            campaign.local_status = "PAUSED"
            state.state_json = _json_safe({
                **dict(state.state_json or {}),
                "data_hold": True,
                "previous_status": previous_status,
                "source": source,
                "timezone": timezone_name,
                "resume_at": state.resume_at,
            })
            db.add(campaign)
            db.add(state)
            db.add(
                WebsiteAdsActionLog(
                    workspace_id=int(campaign.workspace_id),
                    auth_id=int(campaign.auth_id),
                    actor_type="HERMES_CROSS_CHANNEL",
                    action="PAUSE_HOURLY_PROBE_DATA_HOLD",
                    reason="Product-order source became stale during an hourly probe; pause spend until the next bounded probe.",
                    result="SUCCESS",
                    request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                    response_json={"tiktok": response},
                    metrics_json=_json_safe({"source": source, "resume_at": state.resume_at}),
                )
            )
            _assert_execution()
            db.commit()
            return {
                "status": "COOLDOWN",
                "reason": "PROBE_DATA_HOLD",
                "resume_at": state.resume_at.isoformat(),
            }
        if previous_status != "COOLDOWN":
            state.status = "DATA_HOLD"
        state.state_json = _json_safe({
            **dict(state.state_json or {}),
            "data_hold": True,
            "previous_status": previous_status,
            "source": source,
            "timezone": timezone_name,
        })
        if previous_status != "DATA_HOLD":
            db.add(
                WebsiteAdsActionLog(
                    workspace_id=int(campaign.workspace_id),
                    auth_id=int(campaign.auth_id),
                    actor_type="HERMES_CROSS_CHANNEL",
                    action="CROSS_CHANNEL_DATA_HOLD",
                    reason="GMV Max product-hour source is missing or stale; no Website Ads action was allowed.",
                    result="SKIPPED",
                    request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                    metrics_json=_json_safe({"source": source, "timezone": timezone_name}),
                )
            )
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": "DATA_HOLD", "source": _json_safe(source)}

    if str(state.status or "").upper() == "DATA_HOLD" or state.observation_started_at is None:
        _reset_observation(
            state,
            now=now,
            advertiser_now=advertiser_now,
            website_spend=website_spend,
            website_clicks=website_clicks,
            orders=orders,
        )
        state.status = "OBSERVING" if _campaign_enabled(campaign) else "MANUAL_PAUSE"
        state.state_json = {"data_hold": False, "source_recovered_at": now.isoformat()}
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": state.status, "reason": "BASELINE_INITIALIZED"}

    order_count = int(orders["order_count"])
    new_orders = max(0, order_count - int(state.baseline_order_count or 0))
    incremental_spend = max(Decimal("0"), website_spend - _decimal(state.baseline_website_spend))
    incremental_clicks = max(0, website_clicks - int(state.baseline_website_clicks or 0))
    policy = derive_cross_channel_policy(
        reference_price=_decimal(product.reference_price, "10"),
        event_hours=list(orders["event_hours"]),
        pause_count=int(state.pause_count or 0),
        incremental_spend=incremental_spend,
        incremental_clicks=incremental_clicks,
    )
    state.policy_json = policy
    state.last_observed_order_count = order_count
    state.last_order_at = orders.get("last_order_at")

    if new_orders > 0:
        state.last_order_detected_at = now
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_CROSS_CHANNEL",
                action="CROSS_CHANNEL_ORDER_DETECTED",
                reason=f"Detected {new_orders} new product-level GMV Max order pulse(s).",
                result="SUCCESS",
                request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                metrics_json=_json_safe({
                    "new_orders": new_orders,
                    "order_count": order_count,
                    "last_order_at": orders.get("last_order_at"),
                    "incremental_website_spend": float(incremental_spend),
                    "incremental_website_clicks": incremental_clicks,
                }),
            )
        )
        if str(state.status).upper() == "COOLDOWN" and state.paused_at is not None:
            paused_minutes = int((now - state.paused_at).total_seconds() / 60)
            if paused_minutes >= int(policy["early_resume_min_minutes"]):
                _assert_execution()
                response = await api.update_campaign_status(
                    str(campaign.advertiser_id), [str(campaign.campaign_id)], "ENABLE"
                )
                campaign.operation_status = "ENABLE"
                campaign.local_status = "ACTIVE"
                state.status = "CONVERTING"
                state.paused_at = None
                state.resume_at = None
                db.add(
                    WebsiteAdsActionLog(
                        workspace_id=int(campaign.workspace_id),
                        auth_id=int(campaign.auth_id),
                        actor_type="HERMES_CROSS_CHANNEL",
                        action="RESUME_CAMPAIGN_AFTER_ORDER",
                        reason="A new product order pulse arrived after the minimum cooldown.",
                        result="SUCCESS",
                        request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                        response_json=response,
                    )
                )
        else:
            state.status = "CONVERTING"
        _reset_observation(
            state,
            now=now,
            advertiser_now=advertiser_now,
            website_spend=website_spend,
            website_clicks=website_clicks,
            orders=orders,
        )
        db.add(campaign)
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": state.status, "new_orders": new_orders}

    if str(state.status).upper() == "COOLDOWN":
        if not _campaign_paused(campaign):
            state.status = "PROBING"
            _reset_observation(
                state,
                now=now,
                advertiser_now=advertiser_now,
                website_spend=website_spend,
                website_clicks=website_clicks,
                orders=orders,
            )
            state.state_json = {
                "probe_round": int(dict(state.state_json or {}).get("probe_round") or 0) + 1,
                "started_at": now.isoformat(),
                "reason": "CAMPAIGN_ALREADY_ACTIVE",
            }
            db.add(state)
            _assert_execution()
            db.commit()
            return {"status": "PROBING", "reason": "CAMPAIGN_ALREADY_ACTIVE"}
        if state.resume_at is None or state.resume_at > now:
            db.add(state)
            _assert_execution()
            db.commit()
            return {
                "status": "COOLDOWN",
                "resume_at": state.resume_at.isoformat() if state.resume_at else None,
            }
        _assert_execution()
        response = await api.update_campaign_status(
            str(campaign.advertiser_id), [str(campaign.campaign_id)], "ENABLE"
        )
        campaign.operation_status = "ENABLE"
        campaign.local_status = "ACTIVE"
        previous_probe_round = int(dict(state.state_json or {}).get("probe_round") or 0)
        state.status = "PROBING"
        state.paused_at = None
        state.resume_at = None
        _reset_observation(
            state,
            now=now,
            advertiser_now=advertiser_now,
            website_spend=website_spend,
            website_clicks=website_clicks,
            orders=orders,
        )
        state.state_json = {
            "probe_round": previous_probe_round + 1,
            "started_at": now.isoformat(),
            "policy": policy,
        }
        db.add(campaign)
        db.add(state)
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_CROSS_CHANNEL",
                action="START_HOURLY_PROBE",
                reason="Hourly cooldown completed; begin a bounded small-spend product-demand probe.",
                result="SUCCESS",
                request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                response_json=response,
                metrics_json={"policy": policy},
            )
        )
        _assert_execution()
        db.commit()
        return {"status": "PROBING", "policy": policy}

    if not _campaign_enabled(campaign):
        state.status = "MANUAL_PAUSE"
        state.resume_at = None
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": "MANUAL_PAUSE", "reason": "EXTERNAL_OR_OPERATOR_PAUSE"}

    elapsed_minutes = max(0, int((now - state.observation_started_at).total_seconds() / 60))

    if str(state.status or "").upper() == "PROBING":
        probe = _hourly_probe_evidence(
            elapsed_minutes=elapsed_minutes,
            incremental_spend=incremental_spend,
            incremental_clicks=incremental_clicks,
            policy=policy,
        )
        probe_round = int(dict(state.state_json or {}).get("probe_round") or 1)
        probe_evidence = _json_safe({
            **probe,
            "probe_round": probe_round,
            "advertiser_timezone": timezone_name,
            "source": source,
            "baseline_order_count": int(state.baseline_order_count or 0),
            "current_order_count": order_count,
            "policy": policy,
        })
        state.state_json = probe_evidence
        if not probe["should_pause"]:
            db.add(state)
            _assert_execution()
            db.commit()
            return {"status": "PROBING", "evidence": probe_evidence}

        _assert_execution()
        response = await api.update_campaign_status(
            str(campaign.advertiser_id), [str(campaign.campaign_id)], "DISABLE"
        )
        probe_started_at = state.observation_started_at or now
        state.status = "COOLDOWN"
        state.paused_at = now
        state.resume_at = _next_probe_at(
            probe_started_at=probe_started_at,
            now=now,
            interval_minutes=int(policy["probe_interval_minutes"]),
        )
        campaign.operation_status = "DISABLE"
        campaign.local_status = "PAUSED"
        state.state_json = _json_safe({**probe_evidence, "resume_at": state.resume_at})
        db.add(campaign)
        db.add(state)
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_CROSS_CHANNEL",
                action="PAUSE_HOURLY_PROBE_NO_ORDER",
                reason="Hourly probe reached its dynamic evidence or time boundary without a new product-order pulse.",
                result="SUCCESS",
                request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                response_json={"tiktok": response},
                metrics_json=state.state_json,
            )
        )
        _assert_execution()
        db.commit()
        return {
            "status": "COOLDOWN",
            "reason": "HOURLY_PROBE_COMPLETE",
            "resume_at": state.resume_at.isoformat(),
            "evidence": probe_evidence,
        }

    min_spend = _decimal(policy["minimum_incremental_spend"])
    min_clicks = int(policy["minimum_incremental_clicks"])
    hard_gates = {
        "source_fresh": bool(source["fresh"]),
        "campaign_active": _campaign_enabled(campaign),
        "observation_complete": elapsed_minutes >= int(policy["observation_minutes"]),
        "minimum_spend_met": incremental_spend >= min_spend,
        "minimum_clicks_met": incremental_clicks >= min_clicks,
        "no_new_order": new_orders == 0,
    }
    evidence = _json_safe({
        "hard_gates": hard_gates,
        "advertiser_timezone": timezone_name,
        "source": source,
        "elapsed_minutes": elapsed_minutes,
        "incremental_website_spend": float(incremental_spend),
        "incremental_website_clicks": incremental_clicks,
        "baseline_order_count": int(state.baseline_order_count or 0),
        "current_order_count": order_count,
        "last_order_at": orders.get("last_order_at"),
        "policy": policy,
    })
    state.state_json = evidence
    state.status = "OBSERVING"
    if not all(hard_gates.values()):
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": "OBSERVING", "evidence": evidence}

    review = await review_website_campaign_conversion_guard_action(
        campaign=campaign,
        product=product,
        evidence=evidence,
    )
    if str(review.get("decision") or "").upper() != "APPROVE":
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_CROSS_CHANNEL",
                action="HOLD_CAMPAIGN_NO_GMV_ORDER",
                reason=str(review.get("reason") or "Hermes held the bounded pause")[:1024],
                result="SKIPPED",
                request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
                response_json={"hermes_review": review},
                metrics_json=evidence,
            )
        )
        db.add(state)
        _assert_execution()
        db.commit()
        return {"status": "HOLD", "review": review}

    _assert_execution()
    response = await api.update_campaign_status(
        str(campaign.advertiser_id), [str(campaign.campaign_id)], "DISABLE"
    )
    state.pause_count = int(state.pause_count or 0) + 1
    policy = derive_cross_channel_policy(
        reference_price=_decimal(product.reference_price, "10"),
        event_hours=list(orders["event_hours"]),
        pause_count=int(state.pause_count),
        incremental_spend=incremental_spend,
        incremental_clicks=incremental_clicks,
    )
    state.policy_json = policy
    state.status = "COOLDOWN"
    state.paused_at = now
    state.resume_at = now + timedelta(minutes=int(policy["cooldown_minutes"]))
    campaign.operation_status = "DISABLE"
    campaign.local_status = "PAUSED"
    db.add(campaign)
    db.add(state)
    db.add(
        WebsiteAdsActionLog(
            workspace_id=int(campaign.workspace_id),
            auth_id=int(campaign.auth_id),
            actor_type="HERMES_CROSS_CHANNEL",
            action="PAUSE_CAMPAIGN_NO_GMV_ORDER",
            reason=str(review.get("reason") or "No new GMV Max product order pulse")[:1024],
            result="SUCCESS",
            request_json={"campaign_local_id": int(campaign.id), "product_id": product_id},
            response_json={"tiktok": response, "hermes_review": review},
            metrics_json=_json_safe({**evidence, "resume_at": state.resume_at, "policy": policy}),
        )
    )
    _assert_execution()
    db.commit()
    return {
        "status": "PAUSED",
        "resume_at": state.resume_at.isoformat() if state.resume_at else None,
        "policy": policy,
    }
