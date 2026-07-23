from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBAdvertiser
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsCampaign,
    WebsiteAdsMetricHourly,
)
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.providers.tiktok_business.website_ads_pagination import (
    report_payload_has_complete_pagination,
)
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_delivery_optimizer import (
    backfill_campaign_creatives,
    run_group_racing,
    sync_platform_review_results,
)
from app.services.website_ads_conversion_guard import evaluate_campaign_conversion_guard
from app.services.website_ads_hermes_planner import review_website_ad_guard_action
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
    website_ads_execution_lease,
)
from app.services.website_ads_tiktok_contract import WEBSITE_ADS_OPTIMIZATION_EVENT


def _decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "", "-") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _advertiser_local_now(db: Session, campaign: WebsiteAdsCampaign) -> datetime:
    advertiser = db.scalar(
        select(TTBAdvertiser).where(
            TTBAdvertiser.workspace_id == campaign.workspace_id,
            TTBAdvertiser.auth_id == campaign.auth_id,
            TTBAdvertiser.advertiser_id == campaign.advertiser_id,
        )
    )
    timezone_name = str((advertiser.display_timezone or advertiser.timezone) if advertiser else "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return datetime.now(zone)


def _report_days_for_local_now(local_now: datetime) -> list[str]:
    today = local_now.date()
    days = [today.isoformat()]
    if local_now.hour < 4 and local_now.minute in {0, 30}:
        days.append((today - timedelta(days=1)).isoformat())
    return days


def _parse_hour(value: str | None, *, expected_day: str | None = None) -> datetime | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parsed.minute or parsed.second or parsed.microsecond:
        return None
    if expected_day is not None and parsed.date().isoformat() != str(expected_day):
        return None
    return parsed


def _extract_report_rows(payload: dict) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    rows = data.get("list")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _validated_report_day_rows(
    payload: dict,
    *,
    report_day: str,
    by_remote_id: dict[str, WebsiteAdsAd],
) -> tuple[list[tuple[dict, WebsiteAdsAd, datetime]], int, bool]:
    """Validate a full report day before allowing any fact mutation."""

    rows = _extract_report_rows(payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    raw_rows = data.get("list") if isinstance(data, dict) else None
    pagination_complete = report_payload_has_complete_pagination(payload)
    validated: list[tuple[dict, WebsiteAdsAd, datetime]] = []
    returned_keys: set[tuple[int, datetime]] = set()
    invalid_rows = (
        max(0, len(raw_rows) - len(rows))
        if isinstance(raw_rows, list)
        else 0
    )
    for row in rows:
        dimensions = row.get("dimensions")
        if not isinstance(dimensions, dict):
            invalid_rows += 1
            continue
        remote_id = str(dimensions.get("ad_id_v2") or "").strip()
        ad = by_remote_id.get(remote_id)
        stat_hour = _parse_hour(
            dimensions.get("stat_time_hour"),
            expected_day=report_day,
        )
        key = (int(ad.id), stat_hour) if ad is not None and stat_hour is not None else None
        if ad is None or stat_hour is None or key in returned_keys:
            invalid_rows += 1
            continue
        returned_keys.add(key)
        validated.append((row, ad, stat_hour))
    return validated, invalid_rows, pagination_complete


def _click_quality_guard_evidence(
    *,
    spend: Decimal,
    impressions: int,
    clicks: int,
    config: dict,
    emergency_spend_threshold: Decimal,
    video_play_actions: int = 0,
    video_watched_2s: int = 0,
    video_watched_6s: int = 0,
) -> dict:
    min_ctr = _decimal(config.get("min_ctr"), "0.04")
    max_cpc = _decimal(config.get("max_cpc"), "0.30")
    min_impressions = max(20, _int(config.get("min_impressions_before_action") or 100))
    min_clicks = max(1, _int(config.get("min_clicks_for_cpc") or 3))
    min_spend = _decimal(config.get("min_spend_before_action"), "0.90")
    min_video_2s_rate = _decimal(config.get("min_video_2s_rate"), "0.20")
    min_video_6s_rate = _decimal(config.get("min_video_6s_rate"), "0.06")
    min_video_impressions = max(
        50,
        _int(config.get("min_video_impressions_before_action") or 150),
    )
    min_video_spend = _decimal(config.get("min_video_spend_before_action"), "0.75")
    qualified_click_override_ctr = _decimal(
        config.get("qualified_click_override_ctr"),
        "0.04",
    )
    qualified_click_override_cpc = _decimal(
        config.get("qualified_click_override_cpc"),
        "0.30",
    )
    ctr = Decimal(clicks) / impressions if impressions else None
    cpc = spend / clicks if clicks else None
    video_2s_rate = Decimal(video_watched_2s) / impressions if impressions else None
    video_6s_rate = Decimal(video_watched_6s) / impressions if impressions else None
    reasons: list[str] = []

    ctr_sample_ready = impressions >= min_impressions and spend >= min_spend
    if ctr_sample_ready and ctr is not None and ctr < min_ctr:
        reasons.append("LOW_CTR")

    cpc_sample_ready = spend >= min_spend and (
        clicks >= min_clicks or (clicks == 0 and impressions >= min_impressions)
    )
    if cpc_sample_ready and (cpc is None or cpc > max_cpc):
        reasons.append("HIGH_CPC")

    if clicks == 0 and spend >= emergency_spend_threshold and "HIGH_CPC" not in reasons:
        reasons.append("ZERO_CLICK_SPEND_CAP")

    video_sample_ready = (
        impressions >= min_video_impressions
        and spend >= min_video_spend
        and video_play_actions > 0
    )
    qualified_click_override = (
        ctr is not None
        and cpc is not None
        and clicks >= min_clicks
        and ctr >= qualified_click_override_ctr
        and cpc <= qualified_click_override_cpc
    )
    low_video_retention = (
        video_sample_ready
        and video_2s_rate is not None
        and video_6s_rate is not None
        and video_2s_rate < min_video_2s_rate
        and video_6s_rate < min_video_6s_rate
    )
    if low_video_retention and not qualified_click_override:
        reasons.append("LOW_VIDEO_RETENTION")

    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "ctr": ctr,
        "cpc": cpc,
        "video_2s_rate": video_2s_rate,
        "video_6s_rate": video_6s_rate,
        "thresholds": {
            "min_ctr": min_ctr,
            "max_cpc": max_cpc,
            "min_impressions_before_action": min_impressions,
            "min_clicks_for_cpc": min_clicks,
            "min_spend_before_action": min_spend,
            "emergency_spend_threshold": emergency_spend_threshold,
            "min_video_2s_rate": min_video_2s_rate,
            "min_video_6s_rate": min_video_6s_rate,
            "min_video_impressions_before_action": min_video_impressions,
            "min_video_spend_before_action": min_video_spend,
            "qualified_click_override_ctr": qualified_click_override_ctr,
            "qualified_click_override_cpc": qualified_click_override_cpc,
        },
        "sample": {
            "ctr_ready": ctr_sample_ready,
            "cpc_ready": cpc_sample_ready,
            "video_ready": video_sample_ready,
            "qualified_click_override": qualified_click_override,
        },
    }


async def _run_website_ads_monitor_cycle_unlocked(
    db: Session,
    *,
    workspace_id: int | None = None,
) -> dict:
    assert_website_ads_execution_lock(db, required=True)
    query = (
        select(WebsiteAdsAd)
        .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsAd.campaign_local_id)
        .where(
            WebsiteAdsAd.guard_enabled.is_(True),
            WebsiteAdsAd.ad_id.is_not(None),
            WebsiteAdsCampaign.local_status.in_(["ACTIVE", "PAUSED"]),
        )
    )
    if workspace_id is not None:
        query = query.where(WebsiteAdsCampaign.workspace_id == int(workspace_id))
    ads = db.scalars(query).all()
    grouped: dict[tuple[int, int, str], list[WebsiteAdsAd]] = defaultdict(list)
    campaigns: dict[int, WebsiteAdsCampaign] = {}
    for ad in ads:
        campaign = db.get(WebsiteAdsCampaign, ad.campaign_local_id)
        if not campaign:
            continue
        campaigns[campaign.id] = campaign
        grouped[(campaign.workspace_id, campaign.auth_id, campaign.advertiser_id)].append(ad)

    result = {
        "scopes": 0,
        "ads": 0,
        "rows": 0,
        "report_pages": 0,
        "report_days": 0,
        "invalid_report_rows": 0,
        "incomplete_scopes": 0,
        "incomplete_ad_snapshots": 0,
        "incomplete_report_days": 0,
        "reconciled_absent_rows": 0,
        "decision_holds": 0,
        "reviewed": 0,
        "paused": 0,
        "audit_checked": 0,
        "audit_rejected": 0,
        "terminal_ads": 0,
        "terminal_campaigns": 0,
        "replacement_ads": 0,
        "groups_scaled": 0,
        "cross_channel_checked": 0,
        "cross_channel_paused": 0,
        "cross_channel_resumed": 0,
        "cross_channel_orders": 0,
        "cross_channel_data_holds": 0,
        "errors": [],
    }
    for (workspace_id, auth_id, advertiser_id), scope_ads in grouped.items():
        scope_ad_count = len(scope_ads)
        assert_website_ads_execution_lock(db, required=True)
        api = TikTokWebsiteAdsClient(build_ttb_client(db, auth_id))
        try:
            local_now = _advertiser_local_now(db, campaigns[scope_ads[0].campaign_local_id])
            day = local_now.date().isoformat()
            report_days = _report_days_for_local_now(local_now)
            audit_result = await sync_platform_review_results(
                db,
                api=api,
                advertiser_id=advertiser_id,
                ads=scope_ads,
            )
            result["audit_checked"] += int(audit_result["checked"])
            result["audit_rejected"] += int(audit_result["rejected"])
            result["terminal_ads"] += int(
                audit_result.get("terminal_ads") or 0
            )
            result["terminal_campaigns"] += int(
                audit_result.get("terminal_campaigns") or 0
            )
            scope_campaign_ids = sorted({int(ad.campaign_local_id) for ad in scope_ads})
            if audit_result.get("snapshot_complete") is not True:
                result["incomplete_scopes"] += 1
                result["incomplete_ad_snapshots"] += 1
                result["decision_holds"] += len(scope_campaign_ids)
                result["errors"].append(
                    {
                        "advertiser_id": advertiser_id,
                        "stage": "AD_SNAPSHOT_INCOMPLETE",
                        "requested_ad_id_count": int(
                            audit_result.get("requested") or 0
                        ),
                        "returned_ad_id_count": int(
                            audit_result.get("returned") or 0
                        ),
                        "missing_ad_ids": list(audit_result.get("missing_ad_ids") or []),
                        "unexpected_ad_ids": list(
                            audit_result.get("unexpected_ad_ids") or []
                        ),
                        "duplicate_local_ad_ids": list(
                            audit_result.get("duplicate_local_ad_ids") or []
                        ),
                        "duplicate_local_report_ids": list(
                            audit_result.get("duplicate_local_report_ids") or []
                        ),
                        "duplicate_official_ad_ids": list(
                            audit_result.get("duplicate_official_ad_ids") or []
                        ),
                        "missing_campaign_ids": list(
                            audit_result.get("missing_campaign_ids") or []
                        ),
                        "unexpected_campaign_ids": list(
                            audit_result.get("unexpected_campaign_ids") or []
                        ),
                        "duplicate_official_campaign_ids": list(
                            audit_result.get("duplicate_official_campaign_ids")
                            or []
                        ),
                        "error": (
                            "Official campaign/ad status snapshots did not return "
                            "the exact requested ID sets. "
                            "All status, pause, racing, replacement, and conversion decisions "
                            "for this advertiser scope were held."
                        ),
                    }
                )
                result["scopes"] += 1
                result["ads"] += scope_ad_count
                continue

            # Status reconciliation can retire an ad, ad group, or campaign.
            # Never let the pre-snapshot ORM list flow into reports or
            # optimizers after a terminal status was learned.
            live_scope_ads: list[WebsiteAdsAd] = []
            for ad in scope_ads:
                campaign = campaigns.get(int(ad.campaign_local_id)) or db.get(
                    WebsiteAdsCampaign,
                    int(ad.campaign_local_id),
                )
                if (
                    not bool(ad.guard_enabled)
                    or str(ad.operation_status or "").upper() == "DELETE"
                    or campaign is None
                    or str(campaign.local_status or "").upper() == "DELETED"
                    or str(campaign.operation_status or "").upper() == "DELETE"
                ):
                    continue
                live_scope_ads.append(ad)
            scope_ads = live_scope_ads
            scope_campaign_ids = sorted(
                {int(ad.campaign_local_id) for ad in scope_ads}
            )
            if not scope_ads:
                result["scopes"] += 1
                result["ads"] += scope_ad_count
                continue
            remote_ids = [str(ad.ad_id_v2 or ad.ad_id) for ad in scope_ads if ad.ad_id_v2 or ad.ad_id]
            by_remote_id = {str(ad.ad_id_v2 or ad.ad_id): ad for ad in scope_ads}
            conversion_signal_available = False
            complete_rows_by_day: dict[
                str,
                list[tuple[dict, WebsiteAdsAd, datetime]],
            ] = {}
            incomplete_days: dict[str, dict[str, int | bool]] = {}
            for report_day in report_days:
                assert_website_ads_execution_lock(db, required=True)
                try:
                    report = await api.report_ads(
                        advertiser_id,
                        remote_ids,
                        report_day,
                        report_day,
                        hourly=True,
                    )
                except Exception as exc:
                    if report_day == day:
                        raise
                    result["errors"].append(
                        {
                            "advertiser_id": advertiser_id,
                            "stage": "PREVIOUS_DAY_FINALIZATION",
                            "report_day": report_day,
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
                    continue
                validated_rows, invalid_rows, pagination_complete = (
                    _validated_report_day_rows(
                        report,
                        report_day=report_day,
                        by_remote_id=by_remote_id,
                    )
                )
                pagination = report.get("_report_pagination")
                result["report_pages"] += int(
                    (pagination or {}).get("pages_fetched") or 0
                )
                result["report_days"] += 1
                result["invalid_report_rows"] += invalid_rows
                if invalid_rows or not pagination_complete:
                    incomplete_days[report_day] = {
                        "invalid_rows": invalid_rows,
                        "pagination_complete": pagination_complete,
                    }
                    continue
                complete_rows_by_day[report_day] = validated_rows
                if report_day == day:
                    conversion_signal_available = report.get(
                        "_metric_fidelity"
                    ) in {"conversion", "conversion_video"}

            if incomplete_days:
                result["incomplete_scopes"] += 1
                result["incomplete_report_days"] += len(incomplete_days)
                for report_day, detail in sorted(incomplete_days.items()):
                    result["errors"].append(
                        {
                            "advertiser_id": advertiser_id,
                            "stage": "REPORT_INCOMPLETE",
                            "report_day": report_day,
                            "invalid_report_rows": int(detail["invalid_rows"]),
                            "pagination_complete": bool(
                                detail["pagination_complete"]
                            ),
                            "error": (
                                "Report day was not mutated or absence-reconciled "
                                "because pagination or row dimensions could not be "
                                "proven complete."
                            ),
                        }
                    )

            for report_day, validated_rows in complete_rows_by_day.items():
                # The API walk is complete, but no fact mutation or absence
                # deletion is allowed after losing the shared lease.
                assert_website_ads_execution_lock(db, required=True)
                returned_keys = {
                    (int(ad.id), stat_hour)
                    for _row, ad, stat_hour in validated_rows
                }
                for row, ad, stat_hour in validated_rows:
                    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
                    spend = _decimal(metrics.get("spend"))
                    impressions = _int(metrics.get("impressions"))
                    clicks = _int(metrics.get("clicks"))
                    video_play_actions = _int(metrics.get("video_play_actions"))
                    video_watched_2s = _int(metrics.get("video_watched_2s"))
                    video_watched_6s = _int(metrics.get("video_watched_6s"))
                    video_views_p25 = _int(metrics.get("video_views_p25"))
                    video_views_p50 = _int(metrics.get("video_views_p50"))
                    video_views_p75 = _int(metrics.get("video_views_p75"))
                    video_views_p100 = _int(metrics.get("video_views_p100"))
                    average_video_play = _decimal(metrics.get("average_video_play"))
                    conversions = _decimal(metrics.get("conversion") or metrics.get("complete_payment"))
                    conversion_value = _decimal(
                        metrics.get("total_purchase_value")
                        or metrics.get("complete_payment_value")
                        or metrics.get("total_complete_payment_value")
                    )
                    cpc = spend / clicks if clicks else None
                    cpm = spend * Decimal("1000") / impressions if impressions else None
                    ctr = Decimal(clicks) / impressions if impressions else None
                    cpa = spend / conversions if conversions else None
                    roas = conversion_value / spend if spend else None
                    metric = db.scalar(
                        select(WebsiteAdsMetricHourly).where(
                            WebsiteAdsMetricHourly.ad_local_id == ad.id,
                            WebsiteAdsMetricHourly.stat_hour == stat_hour,
                        )
                    ) or WebsiteAdsMetricHourly(
                        workspace_id=workspace_id,
                        advertiser_id=advertiser_id,
                        ad_local_id=ad.id,
                        stat_hour=stat_hour,
                    )
                    metric.spend = spend
                    metric.impressions = impressions
                    metric.clicks = clicks
                    metric.video_play_actions = video_play_actions
                    metric.video_watched_2s = video_watched_2s
                    metric.video_watched_6s = video_watched_6s
                    metric.video_views_p25 = video_views_p25
                    metric.video_views_p50 = video_views_p50
                    metric.video_views_p75 = video_views_p75
                    metric.video_views_p100 = video_views_p100
                    metric.average_video_play = average_video_play
                    metric.conversions = conversions
                    metric.conversion_value = conversion_value
                    metric.cpc = cpc
                    metric.cpm = cpm
                    metric.ctr = ctr
                    metric.cpa = cpa
                    metric.roas = roas
                    metric.raw_json = row
                    metric.synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    db.add(metric)
                    result["rows"] += 1

                report_start = datetime.strptime(report_day, "%Y-%m-%d")
                report_end = report_start + timedelta(days=1)
                existing = list(
                    db.scalars(
                        select(WebsiteAdsMetricHourly).where(
                            WebsiteAdsMetricHourly.ad_local_id.in_(
                                [int(ad.id) for ad in scope_ads]
                            ),
                            WebsiteAdsMetricHourly.stat_hour >= report_start,
                            WebsiteAdsMetricHourly.stat_hour < report_end,
                        )
                    ).all()
                )
                for metric in existing:
                    if (int(metric.ad_local_id), metric.stat_hour) in returned_keys:
                        continue
                    db.delete(metric)
                    result["reconciled_absent_rows"] += 1
            # A complete response is not enough if this worker lost ownership
            # while materializing rows.  Keep the entire advertiser-day write
            # atomic and let the outer lease handler roll it back.
            assert_website_ads_execution_lock(db, required=True)
            db.commit()

            if day in incomplete_days:
                result["decision_holds"] += len(scope_campaign_ids)
                result["scopes"] += 1
                result["ads"] += scope_ad_count
                continue

            for campaign_id in scope_campaign_ids:
                campaign = campaigns.get(campaign_id) or db.get(WebsiteAdsCampaign, campaign_id)
                if campaign is None:
                    continue
                assert_website_ads_execution_lock(db, required=True)
                try:
                    cross_channel = await evaluate_campaign_conversion_guard(
                        db,
                        api=api,
                        campaign=campaign,
                        require_execution_lease=True,
                    )
                    result["cross_channel_checked"] += 1
                    status = str(cross_channel.get("status") or "").upper()
                    if status == "PAUSED":
                        result["cross_channel_paused"] += 1
                    elif status in {"RESUMED", "CONVERTING"}:
                        result["cross_channel_resumed"] += int(status == "RESUMED")
                    if int(cross_channel.get("new_orders") or 0) > 0:
                        result["cross_channel_orders"] += int(cross_channel["new_orders"])
                    if status == "DATA_HOLD":
                        result["cross_channel_data_holds"] += 1
                except WebsiteAdsExecutionLockLost:
                    raise
                except Exception as exc:
                    db.rollback()
                    result["errors"].append(
                        {
                            "campaign_local_id": campaign_id,
                            "stage": "CROSS_CHANNEL_GMV_GUARD",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )

            for ad in scope_ads:
                ad.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.add(ad)
                campaign = campaigns.get(int(ad.campaign_local_id)) or db.get(
                    WebsiteAdsCampaign, int(ad.campaign_local_id)
                )
                if (
                    campaign is None
                    or str(campaign.local_status or "").upper() != "ACTIVE"
                    or str(campaign.operation_status or "").upper() != "ENABLE"
                ):
                    continue
                if str(ad.operation_status or "").upper() != "ENABLE":
                    continue
                totals = db.execute(
                    select(
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_play_actions), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_2s), 0),
                        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_6s), 0),
                    ).where(
                        WebsiteAdsMetricHourly.ad_local_id == ad.id,
                        func.date(WebsiteAdsMetricHourly.stat_hour) == day,
                    )
                ).one()
                (
                    spend,
                    impressions,
                    clicks,
                    conversions,
                    video_play_actions,
                    video_watched_2s,
                    video_watched_6s,
                ) = map(_decimal, totals)
                click_count = int(clicks)
                impression_count = int(impressions)
                video_play_count = int(video_play_actions)
                video_2s_count = int(video_watched_2s)
                video_6s_count = int(video_watched_6s)
                config = dict(ad.guard_config_json or {})
                threshold = _decimal(ad.max_unprofitable_spend, "5")
                if threshold <= 0:
                    threshold = Decimal("5")
                optimization_event = str(
                    config.get("optimization_event") or WEBSITE_ADS_OPTIMIZATION_EVENT
                ).upper()
                current_cpc = spend / clicks if clicks else None
                current_cpm = spend * Decimal("1000") / impressions if impressions else None
                cost_per_view_content = spend / conversions if conversions else None
                runtime_minutes = max(
                    0,
                    int(
                        (
                            datetime.now(timezone.utc).replace(tzinfo=None)
                            - (ad.created_at or datetime.now(timezone.utc).replace(tzinfo=None))
                        ).total_seconds()
                        / 60
                    ),
                )
                evidence = _click_quality_guard_evidence(
                    spend=spend,
                    impressions=impression_count,
                    clicks=click_count,
                    config=config,
                    emergency_spend_threshold=threshold,
                    video_play_actions=video_play_count,
                    video_watched_2s=video_2s_count,
                    video_watched_6s=video_6s_count,
                )
                if not evidence["triggered"]:
                    continue
                result["reviewed"] += 1
                min_ctr = evidence["thresholds"]["min_ctr"]
                max_cpc = evidence["thresholds"]["max_cpc"]
                reason = (
                    f"Creative-quality guard: {','.join(evidence['reasons'])}; spend={spend}, "
                    f"impressions={impression_count}, clicks={click_count}, "
                    f"CTR={evidence['ctr'] if evidence['ctr'] is not None else 'n/a'} "
                    f"(minimum {min_ctr}), CPC={evidence['cpc'] if evidence['cpc'] is not None else 'n/a'} "
                    f"(maximum {max_cpc}), 2s_rate="
                    f"{evidence['video_2s_rate'] if evidence['video_2s_rate'] is not None else 'n/a'}, "
                    f"6s_rate={evidence['video_6s_rate'] if evidence['video_6s_rate'] is not None else 'n/a'}"
                )
                guard_metrics = {
                    "advertiser_day": day,
                    "spend": float(spend),
                    "impressions": impression_count,
                    "clicks": click_count,
                    "ctr": float(evidence["ctr"]) if evidence["ctr"] is not None else None,
                    "cpc": float(evidence["cpc"]) if evidence["cpc"] is not None else None,
                    "cpm": float(current_cpm) if current_cpm is not None else None,
                    "video_play_actions": video_play_count,
                    "video_watched_2s": video_2s_count,
                    "video_watched_6s": video_6s_count,
                    "video_2s_rate": (
                        float(evidence["video_2s_rate"])
                        if evidence["video_2s_rate"] is not None
                        else None
                    ),
                    "video_6s_rate": (
                        float(evidence["video_6s_rate"])
                        if evidence["video_6s_rate"] is not None
                        else None
                    ),
                    "view_content_events": float(conversions),
                    "cost_per_view_content": float(cost_per_view_content) if cost_per_view_content is not None else None,
                    "optimization_event": optimization_event,
                    "runtime_minutes": runtime_minutes,
                    "report_has_conversion_signal": conversion_signal_available,
                    "trigger_reasons": list(evidence["reasons"]),
                    "thresholds": {
                        key: float(value) if isinstance(value, Decimal) else value
                        for key, value in evidence["thresholds"].items()
                    },
                    "sample": evidence["sample"],
                }
                review = await review_website_ad_guard_action(
                    ad=ad,
                    metrics=guard_metrics,
                    proposed_reason=reason,
                )
                if review["decision"] == "HOLD":
                    db.add(
                        WebsiteAdsActionLog(
                            workspace_id=workspace_id,
                            auth_id=auth_id,
                            ad_local_id=ad.id,
                            actor_type="HERMES_GUARD",
                            action="HOLD_AD",
                            reason=review["reason"],
                            result="SKIPPED",
                            response_json={"hermes_review": review},
                            metrics_json=guard_metrics,
                        )
                    )
                    continue
                assert_website_ads_execution_lock(db, required=True)
                response = await api.update_ad_status(advertiser_id, [str(ad.ad_id)], "DISABLE")
                ad.operation_status = "DISABLE"
                db.add(ad)
                db.add(
                    WebsiteAdsActionLog(
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        ad_local_id=ad.id,
                        actor_type="HERMES_GUARD",
                        action="PAUSE_AD",
                        reason=reason,
                        result="SUCCESS",
                        response_json={"tiktok": response, "hermes_review": review},
                        metrics_json=guard_metrics,
                    )
                )
                result["paused"] += 1
            # The remote pause may have awaited long enough for ownership to
            # change. Never commit the local action batch without re-proving
            # the owner token.
            assert_website_ads_execution_lock(db, required=True)
            db.commit()

            for campaign_id in scope_campaign_ids:
                campaign = campaigns.get(campaign_id) or db.get(WebsiteAdsCampaign, campaign_id)
                if campaign is None:
                    continue
                assert_website_ads_execution_lock(db, required=True)
                try:
                    race = await run_group_racing(
                        db,
                        api=api,
                        campaign=campaign,
                        day=day,
                        require_execution_lease=True,
                    )
                    if race.get("status") == "SCALED":
                        result["groups_scaled"] += 1
                except WebsiteAdsExecutionLockLost:
                    raise
                except Exception as exc:
                    db.rollback()
                    result["errors"].append(
                        {
                            "campaign_local_id": campaign_id,
                            "stage": "GROUP_RACING",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
                assert_website_ads_execution_lock(db, required=True)
                try:
                    replacement = await backfill_campaign_creatives(
                        db,
                        api=api,
                        campaign=campaign,
                        require_execution_lease=True,
                    )
                    result["replacement_ads"] += int(replacement.get("created") or 0)
                    for error in replacement.get("errors") or []:
                        result["errors"].append(
                            {
                                "campaign_local_id": campaign_id,
                                "stage": "CREATIVE_REPLACEMENT",
                                **(error if isinstance(error, dict) else {"error": str(error)}),
                            }
                        )
                except WebsiteAdsExecutionLockLost:
                    raise
                except Exception as exc:
                    db.rollback()
                    result["errors"].append(
                        {
                            "campaign_local_id": campaign_id,
                            "stage": "CREATIVE_REPLACEMENT",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
            result["scopes"] += 1
            result["ads"] += scope_ad_count
        except WebsiteAdsExecutionLockLost:
            raise
        except Exception as exc:
            db.rollback()
            result["errors"].append({"advertiser_id": advertiser_id, "error": str(exc)[:500]})
        finally:
            await api.aclose()
    return result


def _lock_hold_result(*, status: str, reason: str) -> dict:
    return {
        "status": status,
        "reason": reason,
        "scopes": 0,
        "ads": 0,
        "rows": 0,
        "report_pages": 0,
        "report_days": 0,
        "invalid_report_rows": 0,
        "incomplete_scopes": 0,
        "incomplete_ad_snapshots": 0,
        "incomplete_report_days": 0,
        "reconciled_absent_rows": 0,
        "decision_holds": 1,
        "reviewed": 0,
        "paused": 0,
        "audit_checked": 0,
        "audit_rejected": 0,
        "replacement_ads": 0,
        "groups_scaled": 0,
        "cross_channel_checked": 0,
        "cross_channel_paused": 0,
        "cross_channel_resumed": 0,
        "cross_channel_orders": 0,
        "cross_channel_data_holds": 0,
        "errors": [],
    }


async def run_website_ads_monitor_cycle(
    db: Session,
    *,
    workspace_id: int | None = None,
    _lock_factory=None,
) -> dict:
    """Run one monitor cycle under the shared Website Ads execution lease."""

    with website_ads_execution_lease(
        db,
        operation="monitor",
        workspace_id=workspace_id,
        lock_factory=_lock_factory,
    ) as lease:
        if lease is None:
            db.rollback()
            return _lock_hold_result(
                status="SKIPPED",
                reason="EXECUTION_LOCK_UNAVAILABLE",
            )
        try:
            result = await _run_website_ads_monitor_cycle_unlocked(
                db,
                workspace_id=workspace_id,
            )
            lease.assert_active()
            result["status"] = "COMPLETED"
            return result
        except WebsiteAdsExecutionLockLost:
            db.rollback()
            return _lock_hold_result(
                status="HOLD",
                reason="EXECUTION_LOCK_LOST",
            )
