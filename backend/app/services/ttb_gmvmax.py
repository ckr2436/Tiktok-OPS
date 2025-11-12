from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Optional, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.services.ttb_api import TTBApiClient


logger = logging.getLogger("gmv.tenants.gmvmax")

__all__ = [
    "upsert_campaign_from_api",
    "sync_gmvmax_campaigns",
    "upsert_metrics_hourly_row",
    "sync_gmvmax_metrics_hourly",
    "upsert_metrics_daily_row",
    "sync_gmvmax_metrics_daily",
    "log_campaign_action",
    "apply_campaign_action",
    "get_or_create_strategy_config",
    "aggregate_recent_metrics",
    "decide_campaign_action",
]


_DECIMAL_FOUR = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")
_DEFAULT_REPORT_METRICS = [
    "impressions",
    "clicks",
    "cost",
    "net_cost",
    "orders",
    "gross_revenue",
    "roi",
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "video_views_2s",
    "video_views_6s",
    "video_views_p25",
    "video_views_p50",
    "video_views_p75",
    "video_views_p100",
    "live_views",
    "live_follows",
]


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


async def sync_gmvmax_campaigns(
    db: Session,
    ttb_client: TTBApiClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    **filters: Any,
) -> dict:
    synced = 0
    async for payload in ttb_client.iter_gmvmax_campaigns(advertiser_id, **filters):
        if not isinstance(payload, dict):
            continue
        upsert_campaign_from_api(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            payload=payload,
        )
        synced += 1
    db.flush()
    return {"synced": synced}


def upsert_campaign_from_api(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: dict,
) -> TTBGmvMaxCampaign:
    if not isinstance(payload, dict):
        raise ValueError("payload must be dict")
    campaign_identifier = _extract_field(payload, "campaign_id", "id")
    if not campaign_identifier:
        raise ValueError("campaign_id missing in payload")
    campaign_id = str(campaign_identifier)

    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == campaign_id)
    )
    result = db.execute(stmt).scalars().first()
    if result is None:
        result = TTBGmvMaxCampaign(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=campaign_id,
        )
        db.add(result)

    result.advertiser_id = str(advertiser_id)
    result.name = _extract_field(payload, "campaign_name", "name")
    result.status = _extract_field(payload, "status", "campaign_status")
    result.shopping_ads_type = _extract_field(payload, "shopping_ads_type")
    result.optimization_goal = _extract_field(payload, "optimization_goal")

    roas_value = _extract_field(payload, "roas_bid", "roi_target")
    result.roas_bid = _to_decimal(roas_value, quantize=_DECIMAL_FOUR)

    budget_cents_value = _extract_field(payload, "daily_budget_cents")
    if budget_cents_value is not None:
        result.daily_budget_cents = _to_int(budget_cents_value)
    else:
        budget_value = _extract_field(payload, "daily_budget", "budget")
        result.daily_budget_cents = _to_cents(budget_value)

    currency_value = _extract_field(payload, "currency", "budget_currency")
    result.currency = str(currency_value) if currency_value is not None else None

    created_time = _extract_field(payload, "create_time", "created_time", "ext_created_time")
    updated_time = _extract_field(payload, "update_time", "updated_time", "ext_updated_time")
    result.ext_created_time = _parse_datetime(created_time)
    result.ext_updated_time = _parse_datetime(updated_time)

    result.raw_json = payload
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

    db.flush()
    return instance


async def sync_gmvmax_metrics_hourly(
    db: Session,
    ttb_client: TTBApiClient,
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
    page = 1
    while True:
        data = await ttb_client.report_gmvmax(
            advertiser_id,
            start_date=start_date_str,
            end_date=end_date_str,
            time_granularity="HOUR",
            metrics=_DEFAULT_REPORT_METRICS,
            campaign_ids=[campaign.campaign_id],
            page=page,
        )
        if not isinstance(data, dict):
            break
        rows_raw = data.get("list") or data.get("items") or []
        rows = [item for item in rows_raw if isinstance(item, dict)]
        if not rows:
            break
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
        page_info = data.get("page_info")
        if not isinstance(page_info, dict):
            break
        has_more = page_info.get("has_more") or page_info.get("has_next")
        total_page = page_info.get("total_page")
        if has_more in (True, 1):
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

    db.flush()
    return instance


async def sync_gmvmax_metrics_daily(
    db: Session,
    ttb_client: TTBApiClient,
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
    page = 1
    while True:
        data = await ttb_client.report_gmvmax(
            advertiser_id,
            start_date=start_date_str,
            end_date=end_date_str,
            time_granularity="DAY",
            metrics=_DEFAULT_REPORT_METRICS,
            campaign_ids=[campaign.campaign_id],
            page=page,
        )
        if not isinstance(data, dict):
            break
        rows_raw = data.get("list") or data.get("items") or []
        rows = [item for item in rows_raw if isinstance(item, dict)]
        if not rows:
            break
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
        page_info = data.get("page_info")
        if not isinstance(page_info, dict):
            break
        has_more = page_info.get("has_more") or page_info.get("has_next")
        total_page = page_info.get("total_page")
        if has_more in (True, 1):
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


async def apply_campaign_action(
    db: Session,
    ttb_client: TTBApiClient,
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
    normalized_action = action.upper()
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
