from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.gmv_restructured import GmvProductMetricsHourly, GmvProductOrderEvent
from app.data.models.ttb_entities import TTBAdvertiser
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsDailyReport,
    WebsiteAdsLandingPage,
    WebsiteAdsMetricHourly,
)
from app.services.hermes_agent.client import (
    HermesAdsReviewClient,
    extract_output_text,
    extract_usage,
)
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.providers.tiktok_business.website_ads_pagination import (
    report_payload_has_complete_pagination,
)
from app.services.gmv_product_order_events import sync_product_order_events_from_hourly
from app.services.website_ads_conversion_guard import resolve_website_ads_store_id
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
    website_ads_execution_lease,
)
from app.services.ttb_client_factory import build_ttb_client


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "", "-") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _json_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return float(_decimal(value))


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


def _extract_report_rows(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("list") if isinstance(data, Mapping) else None
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


class WebsiteAdsReportIncompleteError(RuntimeError):
    """The official hourly report cannot safely drive a final report."""


def _parse_hour(value: Any, report_date: date) -> datetime | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parsed.minute or parsed.second or parsed.microsecond:
        return None
    if parsed.date() != report_date:
        return None
    return parsed


def _derived_metrics(
    *,
    spend: Decimal,
    impressions: int,
    clicks: int,
    conversions: Decimal,
    conversion_value: Decimal,
    video_watched_2s: int,
    video_watched_6s: int,
    video_views_p100: int,
) -> dict[str, Any]:
    return {
        "spend": float(spend),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": float(Decimal(clicks) / impressions) if impressions else None,
        "cpc": float(spend / clicks) if clicks else None,
        "cpm": float(spend * Decimal("1000") / impressions) if impressions else None,
        "view_content_events": float(conversions),
        "cost_per_view_content": float(spend / conversions) if conversions else None,
        "conversion_value": float(conversion_value),
        "roas": float(conversion_value / spend) if spend else None,
        "video_watched_2s": video_watched_2s,
        "video_watched_6s": video_watched_6s,
        "video_views_p100": video_views_p100,
        "video_2s_rate": float(Decimal(video_watched_2s) / impressions) if impressions else None,
        "video_6s_rate": float(Decimal(video_watched_6s) / impressions) if impressions else None,
        "video_completion_rate": float(Decimal(video_views_p100) / impressions) if impressions else None,
    }


def _has_delivery_activity(metrics: Mapping[str, Any], actions: Mapping[str, Any]) -> bool:
    return any(
        float(metrics.get(key) or 0) > 0
        for key in ("spend", "impressions", "clicks", "view_content_events")
    ) or int(actions.get("total") or 0) > 0


async def _refresh_campaign_day_metrics(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    report_date: date,
    require_execution_lease: bool = False,
) -> dict[str, Any]:
    ads = list(
        db.scalars(
            select(WebsiteAdsAd).where(
                WebsiteAdsAd.campaign_local_id == int(campaign.id),
                WebsiteAdsAd.ad_id.is_not(None),
            )
        ).all()
    )
    remote_ids = [str(row.ad_id_v2 or row.ad_id) for row in ads if row.ad_id_v2 or row.ad_id]
    if not remote_ids:
        return {
            "rows": 0,
            "rows_returned": 0,
            "invalid_report_rows": 0,
            "invalid_stat_time_hour_rows": 0,
            "pagination_complete": True,
            "reconciled_absent_rows": 0,
            "complete": True,
            "metric_fidelity": "NO_ADS",
            "request_id": None,
        }
    duplicate_remote_ids = sorted(
        remote_id
        for remote_id, count in Counter(remote_ids).items()
        if count > 1
    )
    if duplicate_remote_ids:
        # A dict keyed by ad_id_v2 would otherwise silently collapse multiple
        # local ads and could reconcile the wrong hourly rows as absent.
        return {
            "rows": 0,
            "rows_returned": 0,
            "invalid_report_rows": len(duplicate_remote_ids),
            "invalid_stat_time_hour_rows": 0,
            "pagination_complete": False,
            "reconciled_absent_rows": 0,
            "complete": False,
            "metric_fidelity": "LOCAL_ID_CONFLICT",
            "request_id": None,
            "duplicate_local_report_ids": duplicate_remote_ids,
        }
    payload = await api.report_ads(
        str(campaign.advertiser_id),
        remote_ids,
        report_date.isoformat(),
        report_date.isoformat(),
        hourly=True,
    )
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    by_remote_id = {str(row.ad_id_v2 or row.ad_id): row for row in ads}
    report_rows = _extract_report_rows(payload)
    raw_data = payload.get("data") if isinstance(payload, Mapping) else None
    raw_rows = raw_data.get("list") if isinstance(raw_data, Mapping) else None
    invalid_rows = (
        max(0, len(raw_rows) - len(report_rows))
        if isinstance(raw_rows, list)
        else 0
    )
    pagination_complete = report_payload_has_complete_pagination(payload)
    validated_rows: list[
        tuple[dict[str, Any], WebsiteAdsAd, datetime, Mapping[str, Any]]
    ] = []
    returned_keys: set[tuple[int, datetime]] = set()
    for row in report_rows:
        dimensions = row.get("dimensions") if isinstance(row.get("dimensions"), Mapping) else {}
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
        stat_hour = _parse_hour(dimensions.get("stat_time_hour"), report_date)
        remote_id = str(dimensions.get("ad_id_v2") or "").strip()
        ad = by_remote_id.get(remote_id)
        key = (int(ad.id), stat_hour) if ad is not None and stat_hour is not None else None
        if stat_hour is None or ad is None or key in returned_keys:
            invalid_rows += 1
            continue
        returned_keys.add(key)
        validated_rows.append((row, ad, stat_hour, metrics))

    if invalid_rows or not pagination_complete:
        return {
            "rows": 0,
            "rows_returned": len(report_rows),
            "invalid_report_rows": invalid_rows,
            "invalid_stat_time_hour_rows": invalid_rows,
            "pagination_complete": pagination_complete,
            "reconciled_absent_rows": 0,
            "complete": False,
            "metric_fidelity": str(payload.get("_metric_fidelity") or "UNKNOWN"),
            "request_id": str(payload.get("request_id") or "") or None,
        }

    synced = 0
    for row, ad, stat_hour, metrics in validated_rows:
        metric = db.scalar(
            select(WebsiteAdsMetricHourly).where(
                WebsiteAdsMetricHourly.ad_local_id == int(ad.id),
                WebsiteAdsMetricHourly.stat_hour == stat_hour,
            )
        ) or WebsiteAdsMetricHourly(
            workspace_id=int(campaign.workspace_id),
            advertiser_id=str(campaign.advertiser_id),
            ad_local_id=int(ad.id),
            stat_hour=stat_hour,
        )
        spend = _decimal(metrics.get("spend"))
        impressions = _int(metrics.get("impressions"))
        clicks = _int(metrics.get("clicks"))
        conversions = _decimal(metrics.get("conversion") or metrics.get("complete_payment"))
        conversion_value = _decimal(
            metrics.get("total_purchase_value")
            or metrics.get("complete_payment_value")
            or metrics.get("total_complete_payment_value")
        )
        metric.spend = spend
        metric.impressions = impressions
        metric.clicks = clicks
        metric.video_play_actions = _int(metrics.get("video_play_actions"))
        metric.video_watched_2s = _int(metrics.get("video_watched_2s"))
        metric.video_watched_6s = _int(metrics.get("video_watched_6s"))
        metric.video_views_p25 = _int(metrics.get("video_views_p25"))
        metric.video_views_p50 = _int(metrics.get("video_views_p50"))
        metric.video_views_p75 = _int(metrics.get("video_views_p75"))
        metric.video_views_p100 = _int(metrics.get("video_views_p100"))
        metric.average_video_play = _decimal(metrics.get("average_video_play"))
        metric.conversions = conversions
        metric.conversion_value = conversion_value
        metric.cpc = spend / clicks if clicks else None
        metric.cpm = spend * Decimal("1000") / impressions if impressions else None
        metric.ctr = Decimal(clicks) / impressions if impressions else None
        metric.cpa = spend / conversions if conversions else None
        metric.roas = conversion_value / spend if spend else None
        metric.raw_json = row
        metric.synced_at = _utcnow()
        db.add(metric)
        synced += 1

    start = datetime.combine(report_date, time.min)
    end = start + timedelta(days=1)
    existing = list(
        db.scalars(
            select(WebsiteAdsMetricHourly).where(
                WebsiteAdsMetricHourly.ad_local_id.in_([int(row.id) for row in ads]),
                WebsiteAdsMetricHourly.stat_hour >= start,
                WebsiteAdsMetricHourly.stat_hour < end,
            )
        ).all()
    )
    reconciled_absent_rows = 0
    for metric in existing:
        if (int(metric.ad_local_id), metric.stat_hour) in returned_keys:
            continue
        db.delete(metric)
        reconciled_absent_rows += 1
    db.flush()
    return {
        "rows": synced,
        "rows_returned": len(report_rows),
        "invalid_report_rows": 0,
        "invalid_stat_time_hour_rows": invalid_rows,
        "pagination_complete": pagination_complete,
        "reconciled_absent_rows": reconciled_absent_rows,
        "complete": True,
        "metric_fidelity": str(payload.get("_metric_fidelity") or "UNKNOWN"),
        "request_id": str(payload.get("request_id") or "") or None,
    }


def _campaign_metrics(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    report_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    start = datetime.combine(report_date, time.min)
    end = start + timedelta(days=1)
    values = db.execute(
        select(
            func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversion_value), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_2s), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_6s), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p100), 0),
            func.max(WebsiteAdsMetricHourly.synced_at),
        )
        .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
        .where(
            WebsiteAdsAd.campaign_local_id == int(campaign.id),
            WebsiteAdsMetricHourly.stat_hour >= start,
            WebsiteAdsMetricHourly.stat_hour < end,
        )
    ).one()
    metrics = _derived_metrics(
        spend=_decimal(values[0]),
        impressions=_int(values[1]),
        clicks=_int(values[2]),
        conversions=_decimal(values[3]),
        conversion_value=_decimal(values[4]),
        video_watched_2s=_int(values[5]),
        video_watched_6s=_int(values[6]),
        video_views_p100=_int(values[7]),
    )
    metrics["last_synced_at"] = values[8].isoformat() if values[8] else None

    audience_rows: list[dict[str, Any]] = []
    groups = list(
        db.scalars(
            select(WebsiteAdsAdGroup)
            .where(WebsiteAdsAdGroup.campaign_local_id == int(campaign.id))
            .order_by(WebsiteAdsAdGroup.id)
        ).all()
    )
    for group in groups:
        group_values = db.execute(
            select(
                func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.conversion_value), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_2s), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_6s), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p100), 0),
            )
            .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
            .where(
                WebsiteAdsAd.adgroup_local_id == int(group.id),
                WebsiteAdsMetricHourly.stat_hour >= start,
                WebsiteAdsMetricHourly.stat_hour < end,
            )
        ).one()
        targeting = dict(group.targeting_json or {})
        audience_rows.append(
            {
                "adgroup_local_id": int(group.id),
                "adgroup_id": str(group.adgroup_id or "") or None,
                "name": str(group.name),
                "status": str(group.operation_status or "UNKNOWN"),
                "audience_segment": targeting.get("audience_segment"),
                "hypothesis": targeting.get("hypothesis"),
                "interest_category_ids": list(targeting.get("interest_category_ids") or []),
                "audience_size_estimate": targeting.get("audience_size_estimate"),
                "metrics": _derived_metrics(
                    spend=_decimal(group_values[0]),
                    impressions=_int(group_values[1]),
                    clicks=_int(group_values[2]),
                    conversions=_decimal(group_values[3]),
                    conversion_value=_decimal(group_values[4]),
                    video_watched_2s=_int(group_values[5]),
                    video_watched_6s=_int(group_values[6]),
                    video_views_p100=_int(group_values[7]),
                ),
            }
        )
    return metrics, audience_rows


def _gmv_product_signal(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    product_id: str,
    report_date: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_id = resolve_website_ads_store_id(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
    )
    if store_id is None:
        return (
            {
                "scope": "UNAVAILABLE",
                "attribution_warning": (
                    "Product-level GMV Max signal is unavailable because the Website Ads "
                    "advertiser does not resolve to exactly one authorized store."
                ),
                "product_id": str(product_id),
                "orders": 0,
                "gross_revenue": 0.0,
                "gmv_max_spend": 0.0,
                "last_order_hour": None,
            },
            {
                "gmv_product_day_complete": False,
                "reason": "STORE_SCOPE_NOT_UNIQUE",
            },
        )
    sync_product_order_events_from_hourly(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        store_id=store_id,
        item_group_id=str(product_id),
        start_date=report_date,
        end_date=report_date,
    )
    start = datetime.combine(report_date, time.min)
    end = start + timedelta(days=1)
    values = db.execute(
        select(
            func.coalesce(func.sum(GmvProductOrderEvent.order_count), 0),
            func.coalesce(func.sum(GmvProductOrderEvent.gross_revenue_cents), 0),
            func.coalesce(func.sum(GmvProductOrderEvent.cost_cents), 0),
            func.max(GmvProductOrderEvent.order_time_hour),
        ).where(
            GmvProductOrderEvent.workspace_id == int(campaign.workspace_id),
            GmvProductOrderEvent.auth_id == int(campaign.auth_id),
            GmvProductOrderEvent.advertiser_id == str(campaign.advertiser_id),
            GmvProductOrderEvent.store_id == store_id,
            GmvProductOrderEvent.item_group_id == str(product_id),
            GmvProductOrderEvent.order_time_hour >= start,
            GmvProductOrderEvent.order_time_hour < end,
        )
    ).one()
    latest_source = db.scalar(
        select(func.max(GmvProductMetricsHourly.stat_time_hour)).where(
            GmvProductMetricsHourly.workspace_id == int(campaign.workspace_id),
            GmvProductMetricsHourly.auth_id == int(campaign.auth_id),
            GmvProductMetricsHourly.advertiser_id == str(campaign.advertiser_id),
            GmvProductMetricsHourly.store_id == store_id,
            GmvProductMetricsHourly.item_group_id == str(product_id),
            GmvProductMetricsHourly.stat_time_hour >= start,
            GmvProductMetricsHourly.stat_time_hour < end,
        )
    )
    expected_last_hour = datetime.combine(report_date, time(hour=23))
    complete = bool(latest_source and latest_source >= expected_last_hour)
    signal = {
        "scope": "PRODUCT_LEVEL_GMV_MAX_DIRECTIONAL_SIGNAL",
        "attribution_warning": (
            "Product-level GMV Max orders are a directional demand signal and are not deterministic Website Ads attribution."
        ),
        "product_id": str(product_id),
        "orders": _int(values[0]),
        "gross_revenue": float(_decimal(values[1]) / Decimal("100")),
        "gmv_max_spend": float(_decimal(values[2]) / Decimal("100")),
        "last_order_hour": values[3].isoformat() if values[3] else None,
    }
    freshness = {
        "gmv_product_latest_hour": latest_source.isoformat() if latest_source else None,
        "expected_last_hour": expected_last_hour.isoformat(),
        "gmv_product_day_complete": complete,
        "gmv_product_store_id": store_id,
    }
    return signal, freshness


def _action_summary(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    report_date: date,
    zone: ZoneInfo | timezone,
) -> dict[str, Any]:
    local_start = datetime.combine(report_date, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    campaign_ad_ids = set(
        int(value)
        for value in db.scalars(
            select(WebsiteAdsAd.id).where(WebsiteAdsAd.campaign_local_id == int(campaign.id))
        ).all()
    )
    rows = list(
        db.scalars(
            select(WebsiteAdsActionLog).where(
                WebsiteAdsActionLog.workspace_id == int(campaign.workspace_id),
                WebsiteAdsActionLog.auth_id == int(campaign.auth_id),
                WebsiteAdsActionLog.created_at >= utc_start,
                WebsiteAdsActionLog.created_at < utc_end,
            )
        ).all()
    )
    matched: list[WebsiteAdsActionLog] = []
    for row in rows:
        request = dict(row.request_json or {})
        if row.ad_local_id in campaign_ad_ids or _int(request.get("campaign_local_id")) == int(campaign.id):
            matched.append(row)
    counts = Counter(str(row.action or "UNKNOWN") for row in matched)
    return {
        "total": len(matched),
        "by_action": dict(sorted(counts.items())),
        "recent": [
            {
                "action": str(row.action),
                "result": str(row.result),
                "reason": str(row.reason or "")[:500],
                "created_at": row.created_at.isoformat(),
            }
            for row in sorted(matched, key=lambda item: item.created_at)[-30:]
        ],
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        payload = json.loads(candidate)
        return dict(payload) if isinstance(payload, Mapping) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(candidate[start : end + 1])
            return dict(payload) if isinstance(payload, Mapping) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


async def _hermes_daily_review(context: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    client = HermesAdsReviewClient()
    try:
        response, latency_ms = await client.create_response(
            instructions=(
                "You are the isolated Hermes retrospective analyst for TikTok Website Ads. Analyze one advertiser-local day. "
                "The campaign optimizes View Content and sends users through a web landing page into TikTok Shop. Product-level "
                "GMV Max orders are directional demand pulses, never deterministic Website Ads attribution. Compare audience "
                "hypotheses using weighted totals, not averages of ratios. Recommend adequately broad but precise audience tests. "
                "Do not execute changes. Return strict JSON with keys summary, findings, audience_learning, recommendations, "
                "next_audience_tests, and risk_controls. next_audience_tests must be a list of objects with name, hypothesis, "
                "interest_keywords, minimum_audience_stage. Keep the response concise and evidence-based."
            ),
            input_text=json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str),
            metadata={
                "prompt_version": "website_ads_daily_report_v1",
                "campaign_local_id": str(context.get("campaign_local_id") or ""),
                "report_date": str(context.get("report_date") or ""),
            },
            store=False,
        )
        output_text = extract_output_text(response)
        parsed = _parse_json_object(output_text)
        if not parsed:
            raise ValueError("Hermes review did not return a JSON object")
        parsed["_model_audit"] = {
            "role": "ads_review",
            "model": str((response.get("_gmv_meta") or {}).get("model") or client.model),
            "latency_ms": int(latency_ms),
            "usage": extract_usage(response),
            "prompt_version": "website_ads_daily_report_v1",
        }
        return parsed, str(parsed.get("summary") or output_text)[:12000]
    except Exception as exc:
        fallback = {
            "summary": "Daily metrics were persisted, but the Hermes retrospective model was unavailable.",
            "findings": [],
            "audience_learning": [],
            "recommendations": ["Retain deterministic safeguards and retry the review model on the next report cycle."],
            "next_audience_tests": [],
            "risk_controls": ["No model-generated action was executed."],
            "_model_audit": {
                "role": "ads_review",
                "model": str(client.model),
                "status": "UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
                "prompt_version": "website_ads_daily_report_v1",
            },
        }
        return fallback, str(fallback["summary"])


async def generate_campaign_daily_report(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    report_date: date,
    final: bool,
    require_execution_lease: bool = False,
) -> WebsiteAdsDailyReport:
    timezone_name, zone = _advertiser_zone(db, campaign)
    product = db.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    product_id = str(product.product_id or "") if product else ""
    refresh = await _refresh_campaign_day_metrics(
        db,
        api=api,
        campaign=campaign,
        report_date=report_date,
        require_execution_lease=require_execution_lease,
    )
    if not bool(refresh.get("complete", False)):
        raise WebsiteAdsReportIncompleteError(
            "TikTok hourly report is incomplete for "
            f"{report_date.isoformat()}: "
            f"{int(refresh.get('invalid_report_rows') or 0)} row(s) had "
            "missing/invalid dimensions, an escaped ad_id_v2, or a duplicate "
            "hour; complete pagination evidence="
            f"{bool(refresh.get('pagination_complete', False))}"
        )
    metrics, audiences = _campaign_metrics(db, campaign=campaign, report_date=report_date)
    if product_id:
        gmv_signal, gmv_freshness = _gmv_product_signal(
            db,
            campaign=campaign,
            product_id=product_id,
            report_date=report_date,
        )
    else:
        gmv_signal = {
            "scope": "UNAVAILABLE",
            "attribution_warning": "No product mapping is available.",
            "orders": 0,
            "gross_revenue": 0.0,
        }
        gmv_freshness = {"gmv_product_day_complete": False, "reason": "PRODUCT_MAPPING_MISSING"}
    actions = _action_summary(
        db,
        campaign=campaign,
        report_date=report_date,
        zone=zone,
    )
    source_freshness = {
        "tiktok_report": refresh,
        **gmv_freshness,
        "report_stage": "FINAL" if final else "PRELIMINARY",
    }
    context = {
        "campaign_local_id": int(campaign.id),
        "campaign_id": str(campaign.campaign_id or ""),
        "campaign_name": str(campaign.name),
        "report_date": report_date.isoformat(),
        "advertiser_timezone": timezone_name,
        "product": {
            "id": product_id or None,
            "name": str(product.title or "") if product else None,
            "reference_price": _json_number(product.reference_price) if product else None,
        },
        "website_ads_metrics": metrics,
        "audience_performance": audiences,
        "gmv_product_signal": gmv_signal,
        "automation_actions": actions,
        "source_freshness": source_freshness,
    }
    if _has_delivery_activity(metrics, actions):
        hermes_report, report_text = await _hermes_daily_review(context)
    else:
        hermes_report = {
            "summary": "No Website Ads delivery or automation activity was recorded for this advertiser day.",
            "findings": [],
            "audience_learning": [],
            "recommendations": [],
            "next_audience_tests": [],
            "risk_controls": [],
            "_model_audit": {
                "role": "ads_review",
                "model": None,
                "status": "NOT_CALLED_NO_ACTIVITY",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "prompt_version": "website_ads_daily_report_v1",
            },
        }
        report_text = str(hermes_report["summary"])
    # Hermes/report calls can outlive a Redis heartbeat interval. Refuse the
    # fact/report write if ownership changed while awaiting either provider.
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    row = db.scalar(
        select(WebsiteAdsDailyReport).where(
            WebsiteAdsDailyReport.campaign_local_id == int(campaign.id),
            WebsiteAdsDailyReport.report_date == report_date,
        )
    ) or WebsiteAdsDailyReport(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        campaign_local_id=int(campaign.id),
        landing_page_id=int(campaign.landing_page_id),
        report_date=report_date,
        advertiser_timezone=timezone_name,
        generated_at=_utcnow(),
    )
    row.status = "FINAL" if final else "PRELIMINARY"
    row.metrics_json = metrics
    row.audience_performance_json = audiences
    row.gmv_signal_json = gmv_signal
    row.action_summary_json = actions
    row.hermes_report_json = hermes_report
    row.report_text = report_text
    row.source_freshness_json = source_freshness
    row.generated_at = _utcnow()
    db.add(row)
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    db.flush()
    db.add(
        WebsiteAdsActionLog(
            workspace_id=int(campaign.workspace_id),
            auth_id=int(campaign.auth_id),
            actor_type="HERMES_REPORT",
            action="GENERATE_DAILY_REPORT",
            reason=f"Generated {row.status.lower()} advertiser-day Website Ads report.",
            result="SUCCESS",
            request_json={
                "campaign_local_id": int(campaign.id),
                "report_date": report_date.isoformat(),
            },
            response_json={"model_audit": hermes_report.get("_model_audit")},
            metrics_json={"metrics": metrics, "gmv_signal": gmv_signal},
        )
    )
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    db.commit()
    db.refresh(row)
    return row


async def _run_website_ads_daily_report_cycle_unlocked(
    db: Session,
    *,
    workspace_id: int | None = None,
    force_date: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    assert_website_ads_execution_lock(db, required=True)
    remote_ad_exists = select(WebsiteAdsAd.id).where(
        WebsiteAdsAd.campaign_local_id == WebsiteAdsCampaign.id,
        WebsiteAdsAd.ad_id.is_not(None),
    ).exists()
    query = select(WebsiteAdsCampaign).where(
        WebsiteAdsCampaign.campaign_id.is_not(None),
        remote_ad_exists,
    )
    if workspace_id is not None:
        query = query.where(WebsiteAdsCampaign.workspace_id == int(workspace_id))
    campaigns = list(db.scalars(query.order_by(WebsiteAdsCampaign.id)).all())
    result: dict[str, Any] = {"campaigns": len(campaigns), "generated": [], "skipped": [], "errors": []}
    apis: dict[int, TikTokWebsiteAdsClient] = {}
    try:
        for campaign in campaigns:
            assert_website_ads_execution_lock(db, required=True)
            timezone_name, zone = _advertiser_zone(db, campaign)
            local_now = datetime.now(zone)
            report_date = force_date or (local_now.date() - timedelta(days=1))
            due_at = time(
                hour=0,
                minute=max(0, min(59, int(settings.WEBSITE_ADS_DAILY_REPORT_LOCAL_MINUTE))),
            )
            if force_date is None and local_now.time() < due_at:
                result["skipped"].append(
                    {"campaign_local_id": int(campaign.id), "reason": "BEFORE_LOCAL_0030", "timezone": timezone_name}
                )
                continue
            final_due = force or local_now.hour >= int(settings.WEBSITE_ADS_DAILY_REPORT_FINAL_REFRESH_HOUR)
            existing = db.scalar(
                select(WebsiteAdsDailyReport).where(
                    WebsiteAdsDailyReport.campaign_local_id == int(campaign.id),
                    WebsiteAdsDailyReport.report_date == report_date,
                )
            )
            if not force and existing is not None:
                if str(existing.status).upper() == "FINAL" or not final_due:
                    result["skipped"].append(
                        {"campaign_local_id": int(campaign.id), "reason": "ALREADY_CURRENT", "status": existing.status}
                    )
                    continue
            api = apis.get(int(campaign.auth_id))
            if api is None:
                api = TikTokWebsiteAdsClient(build_ttb_client(db, int(campaign.auth_id)))
                apis[int(campaign.auth_id)] = api
            try:
                row = await generate_campaign_daily_report(
                    db,
                    api=api,
                    campaign=campaign,
                    report_date=report_date,
                    final=bool(final_due),
                    require_execution_lease=True,
                )
                result["generated"].append(
                    {
                        "id": int(row.id),
                        "campaign_local_id": int(campaign.id),
                        "report_date": report_date.isoformat(),
                        "status": row.status,
                        "timezone": timezone_name,
                    }
                )
            except WebsiteAdsExecutionLockLost:
                raise
            except Exception as exc:
                db.rollback()
                result["errors"].append(
                    {
                        "campaign_local_id": int(campaign.id),
                        "report_date": report_date.isoformat(),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
    finally:
        for api in apis.values():
            await api.aclose()
    return result


def _daily_lock_hold_result(*, status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "campaigns": 0,
        "generated": [],
        "skipped": [],
        "errors": [],
        "decision": "HOLD",
    }


async def run_website_ads_daily_report_cycle(
    db: Session,
    *,
    workspace_id: int | None = None,
    force_date: date | None = None,
    force: bool = False,
    _lock_factory=None,
) -> dict[str, Any]:
    """Generate reports under the same lease used by the live monitor."""

    with website_ads_execution_lease(
        db,
        operation="daily_report",
        workspace_id=workspace_id,
        lock_factory=_lock_factory,
    ) as lease:
        if lease is None:
            db.rollback()
            return _daily_lock_hold_result(
                status="SKIPPED",
                reason="EXECUTION_LOCK_UNAVAILABLE",
            )
        try:
            result = await _run_website_ads_daily_report_cycle_unlocked(
                db,
                workspace_id=workspace_id,
                force_date=force_date,
                force=force,
            )
            lease.assert_active()
            result["status"] = "COMPLETED"
            return result
        except WebsiteAdsExecutionLockLost:
            db.rollback()
            return _daily_lock_hold_result(
                status="HOLD",
                reason="EXECUTION_LOCK_LOST",
            )
