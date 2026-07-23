from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Iterable, Mapping

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
    WebsiteAdsDailyReport,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsMetricHourly,
)
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.website_ads_asset_expansion import _create_ad_for_asset
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
)
from app.services.website_ads_hermes_planner import (
    PRODUCTION_ORIGIN_PRIORITY,
    ALLOWED_AGE_GROUPS,
    _audience_estimate_body,
    _audience_size_summary,
    _asset_performance,
    _asset_product_confidence,
    _creative_selection_score,
    _json_safe,
    _production_origin,
    review_website_adgroup_race_action,
)
from app.services.website_ads_targeting_catalog import rank_general_interest_categories
from app.services.website_ads_plan_launch import TIKTOK_TARGETING_FIELDS, _data, _first_id, _schedule_text
from app.services.website_ads_tiktok_contract import (
    enforce_website_ads_location_policy,
    enforce_website_ads_placement_policy,
    website_ads_optimization_fields,
)


logger = logging.getLogger("gmv.services.website_ads_delivery_optimizer")
PLATFORM_REJECTION_MARKERS = ("REJECT", "DISAPPROV", "AUDIT_DENIED", "NOT_APPROVED")
CREATIVE_REJECTION_MARKERS = (
    "CREATIVE",
    "VIDEO",
    "AD_TEXT",
    "CAPTION",
    "MUSIC",
    "CONTENT",
    "CLAIM",
    "MISLEADING",
)
AD_DELETE_SECONDARY_STATUSES = {
    "AD_STATUS_DELETE",
    "AD_STATUS_ADGROUP_DELETE",
    "AD_STATUS_CAMPAIGN_DELETE",
}
CAMPAIGN_DELETE_SECONDARY_STATUSES = {
    "CAMPAIGN_STATUS_DELETE",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "", "-") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value: object) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _campaign_enabled(campaign: WebsiteAdsCampaign) -> bool:
    return (
        str(campaign.local_status or "").upper() == "ACTIVE"
        and str(campaign.operation_status or "").upper() == "ENABLE"
    )


def _payload_rows(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("list") if isinstance(data, Mapping) else None
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _status_text(row: Mapping[str, Any]) -> str:
    values = [
        row.get("secondary_status"),
        row.get("primary_status"),
        row.get("review_status"),
        row.get("status"),
        row.get("reject_reason"),
        row.get("reject_reason_list"),
    ]
    return " | ".join(str(value).upper() for value in values if value not in (None, "", []))


def _is_platform_rejected(row: Mapping[str, Any]) -> bool:
    status = _status_text(row)
    return any(marker in status for marker in PLATFORM_REJECTION_MARKERS)


def _is_creative_specific_rejection(row: Mapping[str, Any]) -> bool:
    status = _status_text(row)
    return any(marker in status for marker in CREATIVE_REJECTION_MARKERS)


def _normalized_status(row: Mapping[str, Any], field: str) -> str:
    return str(row.get(field) or "").strip().upper()


def _is_deleted_ad(row: Mapping[str, Any]) -> bool:
    return (
        _normalized_status(row, "primary_status") == "STATUS_DELETE"
        or _normalized_status(row, "operation_status") == "DELETE"
        or _normalized_status(row, "secondary_status")
        in AD_DELETE_SECONDARY_STATUSES
    )


def _is_deleted_campaign(row: Mapping[str, Any]) -> bool:
    return (
        _normalized_status(row, "primary_status") == "STATUS_DELETE"
        or _normalized_status(row, "operation_status") == "DELETE"
        or _normalized_status(row, "secondary_status")
        in CAMPAIGN_DELETE_SECONDARY_STATUSES
    )


async def sync_platform_review_results(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    advertiser_id: str,
    ads: Iterable[WebsiteAdsAd],
) -> dict[str, Any]:
    """Persist one complete TikTok ad snapshot and learn true rejections.

    ``/ad/get/`` is filtered by explicit IDs, so an HTTP-successful response is
    not sufficient evidence that every requested ad was returned.  Collect and
    validate every batch before mutating local state; otherwise a silently
    omitted ad would retain an old ``ENABLE`` value and could drive an unsafe
    optimization decision later in the monitor cycle.
    """

    ad_list = [ad for ad in ads if str(ad.ad_id or "").strip()]
    local_ad_ids = [str(ad.ad_id).strip() for ad in ad_list]
    local_report_ids = [
        str(ad.ad_id_v2 or ad.ad_id).strip()
        for ad in ad_list
        if str(ad.ad_id_v2 or ad.ad_id or "").strip()
    ]
    campaigns_by_local_id: dict[int, WebsiteAdsCampaign] = {}
    invalid_campaign_refs: list[int] = []
    for ad in ad_list:
        campaign = db.get(WebsiteAdsCampaign, int(ad.campaign_local_id))
        if (
            campaign is None
            or not str(campaign.campaign_id or "").strip()
            or str(campaign.advertiser_id) != str(advertiser_id)
        ):
            invalid_campaign_refs.append(int(ad.id))
            continue
        campaigns_by_local_id[int(campaign.id)] = campaign
    local_campaign_ids = [
        str(campaign.campaign_id).strip()
        for campaign in campaigns_by_local_id.values()
    ]
    duplicate_local_ad_ids = sorted(
        remote_id
        for remote_id, count in Counter(local_ad_ids).items()
        if count > 1
    )
    duplicate_local_report_ids = sorted(
        remote_id
        for remote_id, count in Counter(local_report_ids).items()
        if count > 1
    )
    duplicate_local_campaign_ids = sorted(
        remote_id
        for remote_id, count in Counter(local_campaign_ids).items()
        if count > 1
    )
    if (
        duplicate_local_ad_ids
        or duplicate_local_report_ids
        or duplicate_local_campaign_ids
        or invalid_campaign_refs
    ):
        logger.error(
            "Website Ads local scope contains duplicate remote IDs for advertiser %s",
            advertiser_id,
            extra={
                "duplicate_local_ad_ids": duplicate_local_ad_ids,
                "duplicate_local_report_ids": duplicate_local_report_ids,
                "duplicate_local_campaign_ids": duplicate_local_campaign_ids,
                "invalid_campaign_ad_local_ids": invalid_campaign_refs,
            },
        )
        return {
            "status": "INCOMPLETE",
            "snapshot_complete": False,
            "requested": len(ad_list),
            "returned": 0,
            "batches_fetched": 0,
            "missing_ad_ids": [],
            "unexpected_ad_ids": [],
            "requested_campaigns": len(local_campaign_ids),
            "returned_campaigns": 0,
            "campaign_batches_fetched": 0,
            "missing_campaign_ids": [],
            "unexpected_campaign_ids": [],
            "duplicate_local_ad_ids": duplicate_local_ad_ids,
            "duplicate_local_report_ids": duplicate_local_report_ids,
            "duplicate_local_campaign_ids": duplicate_local_campaign_ids,
            "invalid_campaign_ad_local_ids": invalid_campaign_refs,
            "checked": 0,
            "rejected": 0,
            "terminal_ads": 0,
            "terminal_campaigns": 0,
        }

    by_remote_id = {str(ad.ad_id).strip(): ad for ad in ad_list}
    campaigns_by_remote_id = {
        str(campaign.campaign_id).strip(): campaign
        for campaign in campaigns_by_local_id.values()
    }
    requested_ids = list(by_remote_id)
    requested_campaign_ids = list(campaigns_by_remote_id)
    rows_by_remote_id: dict[str, dict[str, Any]] = {}
    campaign_rows_by_remote_id: dict[str, dict[str, Any]] = {}
    missing_ids: set[str] = set()
    unexpected_ids: set[str] = set()
    returned_requested_ids: set[str] = set()
    duplicate_official_ad_ids: set[str] = set()
    invalid_scope_ad_ids: set[str] = set()
    missing_campaign_ids: set[str] = set()
    unexpected_campaign_ids: set[str] = set()
    returned_campaign_ids: set[str] = set()
    duplicate_official_campaign_ids: set[str] = set()
    batches_fetched = 0
    campaign_batches_fetched = 0

    for offset in range(0, len(requested_campaign_ids), 50):
        batch_ids = requested_campaign_ids[offset : offset + 50]
        expected_batch_ids = set(batch_ids)
        payload = await api.get_campaigns(advertiser_id, batch_ids)
        campaign_batches_fetched += 1
        batch_rows = _payload_rows(payload)
        returned_id_values = [
            str(row.get("campaign_id") or row.get("id") or "").strip()
            for row in batch_rows
        ]
        returned_id_counts = Counter(
            remote_id for remote_id in returned_id_values if remote_id
        )
        duplicate_official_campaign_ids.update(
            remote_id
            for remote_id, count in returned_id_counts.items()
            if count > 1
        )
        returned_batch_ids = set(returned_id_values)
        returned_batch_ids.discard("")
        missing_campaign_ids.update(expected_batch_ids - returned_batch_ids)
        unexpected_campaign_ids.update(returned_batch_ids - expected_batch_ids)
        returned_campaign_ids.update(returned_batch_ids & expected_batch_ids)
        for row in batch_rows:
            remote_id = str(
                row.get("campaign_id") or row.get("id") or ""
            ).strip()
            if remote_id in expected_batch_ids:
                campaign_rows_by_remote_id[remote_id] = row

    for offset in range(0, len(requested_ids), 50):
        batch_ids = requested_ids[offset : offset + 50]
        expected_batch_ids = set(batch_ids)
        payload = await api.get_ads(advertiser_id, batch_ids)
        batches_fetched += 1
        batch_rows = _payload_rows(payload)
        returned_id_values = [
            str(row.get("ad_id") or row.get("id") or "").strip()
            for row in batch_rows
        ]
        returned_id_counts = Counter(
            remote_id for remote_id in returned_id_values if remote_id
        )
        duplicate_official_ad_ids.update(
            remote_id
            for remote_id, count in returned_id_counts.items()
            if count > 1
        )
        returned_batch_ids = set(returned_id_values)
        returned_batch_ids.discard("")
        missing_ids.update(expected_batch_ids - returned_batch_ids)
        unexpected_ids.update(returned_batch_ids - expected_batch_ids)
        returned_requested_ids.update(returned_batch_ids & expected_batch_ids)
        for row in batch_rows:
            remote_id = str(row.get("ad_id") or row.get("id") or "").strip()
            if remote_id in expected_batch_ids:
                ad = by_remote_id[remote_id]
                campaign = campaigns_by_local_id.get(int(ad.campaign_local_id))
                returned_advertiser_id = str(
                    row.get("advertiser_id") or advertiser_id
                ).strip()
                returned_campaign_id = str(
                    row.get("campaign_id")
                    or (campaign.campaign_id if campaign is not None else "")
                ).strip()
                if (
                    returned_advertiser_id != str(advertiser_id)
                    or campaign is None
                    or returned_campaign_id != str(campaign.campaign_id)
                ):
                    invalid_scope_ad_ids.add(remote_id)
                rows_by_remote_id[remote_id] = row

    contradictory_campaign_id_set: set[str] = set()
    for remote_id, row in rows_by_remote_id.items():
        if (
            _normalized_status(row, "secondary_status")
            != "AD_STATUS_CAMPAIGN_DELETE"
        ):
            continue
        ad = by_remote_id[remote_id]
        campaign = campaigns_by_local_id[int(ad.campaign_local_id)]
        campaign_remote_id = str(campaign.campaign_id)
        if not _is_deleted_campaign(
            campaign_rows_by_remote_id.get(campaign_remote_id, {})
        ):
            contradictory_campaign_id_set.add(campaign_remote_id)
    contradictory_campaign_ids = sorted(contradictory_campaign_id_set)
    snapshot_complete = bool(
        not missing_ids
        and not unexpected_ids
        and not duplicate_official_ad_ids
        and not invalid_scope_ad_ids
        and not missing_campaign_ids
        and not unexpected_campaign_ids
        and not duplicate_official_campaign_ids
        and not contradictory_campaign_ids
    )
    result: dict[str, Any] = {
        "status": "COMPLETE" if snapshot_complete else "INCOMPLETE",
        "snapshot_complete": snapshot_complete,
        "requested": len(requested_ids),
        "returned": len(returned_requested_ids),
        "batches_fetched": batches_fetched,
        "missing_ad_ids": sorted(missing_ids),
        "unexpected_ad_ids": sorted(unexpected_ids),
        "requested_campaigns": len(requested_campaign_ids),
        "returned_campaigns": len(returned_campaign_ids),
        "campaign_batches_fetched": campaign_batches_fetched,
        "missing_campaign_ids": sorted(missing_campaign_ids),
        "unexpected_campaign_ids": sorted(unexpected_campaign_ids),
        "checked": 0,
        "rejected": 0,
        "terminal_ads": 0,
        "terminal_campaigns": 0,
    }
    if duplicate_official_ad_ids:
        result["duplicate_official_ad_ids"] = sorted(
            duplicate_official_ad_ids
        )
    if duplicate_official_campaign_ids:
        result["duplicate_official_campaign_ids"] = sorted(
            duplicate_official_campaign_ids
        )
    if invalid_scope_ad_ids:
        result["invalid_scope_ad_ids"] = sorted(invalid_scope_ad_ids)
    if contradictory_campaign_ids:
        result["contradictory_campaign_ids"] = contradictory_campaign_ids
    if not snapshot_complete:
        logger.error(
            "Website Ads official delivery snapshot incomplete for advertiser %s: "
            "ads=%d/%d campaigns=%d/%d missing_ads=%s unexpected_ads=%s "
            "missing_campaigns=%s unexpected_campaigns=%s",
            advertiser_id,
            len(returned_requested_ids),
            len(requested_ids),
            len(returned_campaign_ids),
            len(requested_campaign_ids),
            sorted(missing_ids),
            sorted(unexpected_ids),
            sorted(missing_campaign_ids),
            sorted(unexpected_campaign_ids),
        )
        return result

    assert_website_ads_execution_lock(db)
    terminal_campaign_local_ids: set[int] = set()
    terminal_adgroup_local_ids: set[int] = set()
    terminal_ad_local_ids: set[int] = set()

    for remote_id in requested_campaign_ids:
        row = campaign_rows_by_remote_id[remote_id]
        campaign = campaigns_by_remote_id[remote_id]
        operation_status = _normalized_status(row, "operation_status")
        secondary_status = str(
            row.get("secondary_status")
            or row.get("primary_status")
            or row.get("status")
            or ""
        )[:128]
        if _is_deleted_campaign(row):
            terminal_campaign_local_ids.add(int(campaign.id))
            campaign.local_status = "DELETED"
            campaign.operation_status = "DELETE"
        elif operation_status:
            campaign.operation_status = operation_status
            if operation_status == "ENABLE":
                campaign.local_status = "ACTIVE"
            elif operation_status == "DISABLE":
                campaign.local_status = "PAUSED"
        if secondary_status:
            campaign.secondary_status = secondary_status
        campaign.raw_json = _json_safe(row)
        campaign.last_synced_at = _utcnow()
        db.add(campaign)

    if terminal_campaign_local_ids:
        terminal_groups = list(
            db.scalars(
                select(WebsiteAdsAdGroup).where(
                    WebsiteAdsAdGroup.campaign_local_id.in_(
                        terminal_campaign_local_ids
                    )
                )
            ).all()
        )
        terminal_adgroup_local_ids.update(
            int(group.id) for group in terminal_groups
        )
        for group in terminal_groups:
            group.operation_status = "DELETE"
            db.add(group)
        campaign_ads = list(
            db.scalars(
                select(WebsiteAdsAd).where(
                    WebsiteAdsAd.campaign_local_id.in_(
                        terminal_campaign_local_ids
                    )
                )
            ).all()
        )
        for terminal_ad in campaign_ads:
            terminal_ad_local_ids.add(int(terminal_ad.id))
            terminal_ad.operation_status = "DELETE"
            terminal_ad.guard_enabled = False
            db.add(terminal_ad)

    for remote_id in requested_ids:
        row = rows_by_remote_id[remote_id]
        ad = by_remote_id[remote_id]
        result["checked"] += 1
        operation_status = _normalized_status(row, "operation_status")
        secondary_status = str(
            row.get("secondary_status")
            or row.get("primary_status")
            or row.get("status")
            or ""
        )[:128]
        secondary_status_upper = secondary_status.upper()
        if _is_deleted_ad(row):
            terminal_ad_local_ids.add(int(ad.id))
            ad.operation_status = "DELETE"
            ad.guard_enabled = False
            if secondary_status_upper == "AD_STATUS_ADGROUP_DELETE":
                terminal_adgroup_local_ids.add(int(ad.adgroup_local_id))
        elif operation_status:
            ad.operation_status = operation_status
        if secondary_status:
            ad.secondary_status = secondary_status
        ad.raw_json = _json_safe(row)
        db.add(ad)
        if _is_deleted_ad(row) or not _is_platform_rejected(row):
            continue

        campaign = db.get(WebsiteAdsCampaign, int(ad.campaign_local_id))
        if campaign is None:
            raise RuntimeError(
                f"Website Ads ad {ad.id} has no campaign for platform-review attribution"
            )
        existing = db.scalar(
            select(WebsiteAdsActionLog.id).where(
                WebsiteAdsActionLog.auth_id == int(campaign.auth_id),
                WebsiteAdsActionLog.ad_local_id == int(ad.id),
                WebsiteAdsActionLog.actor_type == "TIKTOK_PLATFORM_REVIEW",
                WebsiteAdsActionLog.action == "AUDIT_REJECTED",
            ).limit(1)
        )
        if existing:
            continue
        asset = db.scalar(
            select(WebsiteAdsCreativeAsset).where(
                WebsiteAdsCreativeAsset.workspace_id == int(campaign.workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(campaign.auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(campaign.advertiser_id),
                WebsiteAdsCreativeAsset.video_id == str(ad.video_id),
            ).limit(1)
        )
        audit_record = {
            "ad_local_id": int(ad.id),
            "ad_id": str(ad.ad_id),
            "status": _status_text(row),
            "recorded_at": _utcnow().isoformat(timespec="seconds") + "Z",
            "response": _json_safe(row),
        }
        if asset is not None:
            analysis = dict(asset.hermes_analysis_json or {})
            history = list(analysis.get("platform_review_history") or [])
            history.append(audit_record)
            analysis["platform_review_history"] = history[-20:]
            hard_exclusion = (
                _is_creative_specific_rejection(row)
                or len(history) >= max(1, int(settings.WEBSITE_ADS_PLATFORM_REJECTION_STRIKES))
            )
            analysis["platform_review_readiness"] = (
                "REJECTED" if hard_exclusion else "RISK_RECORDED"
            )
            asset.hermes_analysis_json = analysis
            if hard_exclusion:
                asset.auto_launch_status = "AUDIT_REJECTED"
            asset.auto_launch_error = _status_text(row)[:2000]
            asset.auto_launch_next_retry_at = None
            db.add(asset)
        else:
            hard_exclusion = False
        ad.operation_status = "DISABLE"
        ad.guard_enabled = False
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                ad_local_id=int(ad.id),
                actor_type="TIKTOK_PLATFORM_REVIEW",
                action="AUDIT_REJECTED",
                reason=_status_text(row)[:1024],
                result="RECORDED",
                response_json={
                    "asset_id": int(asset.id) if asset else None,
                    "hard_asset_exclusion": hard_exclusion,
                    "tiktok": _json_safe(row),
                },
            )
        )
        result["rejected"] += 1

    if terminal_adgroup_local_ids:
        terminal_groups = list(
            db.scalars(
                select(WebsiteAdsAdGroup).where(
                    WebsiteAdsAdGroup.id.in_(terminal_adgroup_local_ids)
                )
            ).all()
        )
        for group in terminal_groups:
            group.operation_status = "DELETE"
            db.add(group)
        group_ads = list(
            db.scalars(
                select(WebsiteAdsAd).where(
                    WebsiteAdsAd.adgroup_local_id.in_(
                        terminal_adgroup_local_ids
                    )
                )
            ).all()
        )
        for terminal_ad in group_ads:
            terminal_ad_local_ids.add(int(terminal_ad.id))
            terminal_ad.operation_status = "DELETE"
            terminal_ad.guard_enabled = False
            db.add(terminal_ad)

    result["terminal_ads"] = len(terminal_ad_local_ids)
    result["terminal_campaigns"] = len(terminal_campaign_local_ids)
    assert_website_ads_execution_lock(db)
    db.commit()
    return result


def _candidate_assets(
    db: Session,
    *,
    campaign: WebsiteAdsCampaign,
    product: WebsiteAdsLandingPage,
    exclude_video_ids: set[str] | None = None,
) -> list[tuple[WebsiteAdsCreativeAsset, float]]:
    excluded = exclude_video_ids or set()
    assets = list(
        db.scalars(
            select(WebsiteAdsCreativeAsset).where(
                WebsiteAdsCreativeAsset.workspace_id == int(campaign.workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(campaign.auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(campaign.advertiser_id),
                WebsiteAdsCreativeAsset.landing_page_id == int(product.id),
                WebsiteAdsCreativeAsset.is_active.is_(True),
                WebsiteAdsCreativeAsset.analysis_status == "READY",
                or_(
                    WebsiteAdsCreativeAsset.auto_launch_status.is_(None),
                    WebsiteAdsCreativeAsset.auto_launch_status != "AUDIT_REJECTED",
                ),
            )
        ).all()
    )
    ranked: list[tuple[WebsiteAdsCreativeAsset, float, int, int]] = []
    reference_price = float(product.reference_price or 0)
    for asset in assets:
        if str(asset.video_id) in excluded:
            continue
        analysis = dict(asset.hermes_analysis_json or {})
        policy = assess_website_ads_creative_policy(analysis)
        if not policy["eligible_for_automatic_launch"]:
            continue
        confidence = _asset_product_confidence(asset, int(product.id))
        if confidence < 0.35:
            continue
        performance = _asset_performance(db, asset)
        origin = _production_origin(asset, analysis)
        score = _creative_selection_score(
            performance,
            product_confidence=confidence,
            analysis_status=str(asset.analysis_status or ""),
            production_origin=origin,
            reference_price=reference_price,
        )
        ranked.append(
            (
                asset,
                score,
                PRODUCTION_ORIGIN_PRIORITY.get(origin, 0),
                int(performance.get("clicks") or 0),
            )
        )
    ranked.sort(key=lambda item: (item[2], item[1], item[3], int(item[0].id)), reverse=True)
    return [(asset, score) for asset, score, _, _ in ranked]


def _group_used_video_ids(db: Session, group_id: int) -> set[str]:
    return {
        str(value)
        for value in db.scalars(
            select(WebsiteAdsAd.video_id).where(
                WebsiteAdsAd.adgroup_local_id == int(group_id),
                WebsiteAdsAd.ad_id.is_not(None),
                or_(WebsiteAdsAd.operation_status.is_(None), WebsiteAdsAd.operation_status != "DELETE"),
            )
        ).all()
        if value
    }


async def backfill_campaign_creatives(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    require_execution_lease: bool = False,
) -> dict[str, Any]:
    """Fill a paused/rejected ad's slot with a never-used asset in the same group."""

    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    if not _campaign_enabled(campaign):
        return {"created": 0, "errors": []}
    plan = db.scalar(
        select(WebsiteAdsMediaPlan)
        .where(
            WebsiteAdsMediaPlan.campaign_local_id == int(campaign.id),
            WebsiteAdsMediaPlan.status == "ACTIVE",
        )
        .order_by(WebsiteAdsMediaPlan.id.desc())
        .limit(1)
    )
    product = db.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    if plan is None or product is None:
        return {"created": 0, "errors": ["ACTIVE_PLAN_OR_PRODUCT_MISSING"]}
    groups = list(
        db.scalars(
            select(WebsiteAdsAdGroup).where(
                WebsiteAdsAdGroup.campaign_local_id == int(campaign.id),
                WebsiteAdsAdGroup.adgroup_id.is_not(None),
                WebsiteAdsAdGroup.operation_status == "ENABLE",
            ).order_by(WebsiteAdsAdGroup.id)
        ).all()
    )
    target = max(1, int(settings.WEBSITE_ADS_TARGET_ACTIVE_ADS_PER_GROUP))
    remaining = max(1, int(settings.WEBSITE_ADS_REPLACEMENT_MAX_ADS_PER_CYCLE))
    result: dict[str, Any] = {"created": 0, "groups": [], "errors": []}
    for group in groups:
        if remaining <= 0:
            break
        enabled_count = _int(
            db.scalar(
                select(func.count(WebsiteAdsAd.id)).where(
                    WebsiteAdsAd.adgroup_local_id == int(group.id),
                    WebsiteAdsAd.ad_id.is_not(None),
                    WebsiteAdsAd.operation_status == "ENABLE",
                )
            )
        )
        deficit = min(max(0, target - enabled_count), remaining)
        if deficit <= 0:
            continue
        used = _group_used_video_ids(db, int(group.id))
        candidates = _candidate_assets(db, campaign=campaign, product=product, exclude_video_ids=used)
        created: list[dict[str, Any]] = []
        for asset, score in candidates:
            if len(created) >= deficit:
                break
            try:
                ad, response = await _create_ad_for_asset(
                    db,
                    api=api,
                    plan=plan,
                    campaign=campaign,
                    product=product,
                    group=group,
                    asset=asset,
                    expansion_mode="SAME_GROUP_REPLACEMENT",
                    score=score,
                    require_execution_lease=require_execution_lease,
                )
                asset.auto_launch_status = "DEPLOYED"
                asset.auto_launched_at = asset.auto_launched_at or _utcnow()
                db.add(asset)
                db.add(
                    WebsiteAdsActionLog(
                        workspace_id=int(campaign.workspace_id),
                        auth_id=int(campaign.auth_id),
                        ad_local_id=int(ad.id),
                        actor_type="HERMES_CREATIVE_ROTATION",
                        action="REPLACE_CREATIVE",
                        reason="A stopped or rejected ad left an open slot; added a never-used analyzed asset in the same audience group.",
                        result="SUCCESS",
                        request_json={
                            "campaign_local_id": int(campaign.id),
                            "adgroup_local_id": int(group.id),
                            "asset_id": int(asset.id),
                            "risk": assess_website_ads_creative_policy(asset.hermes_analysis_json),
                        },
                        response_json={"tiktok": _json_safe(response)},
                    )
                )
                assert_website_ads_execution_lock(
                    db,
                    required=require_execution_lease,
                )
                db.commit()
                created.append({"ad_local_id": int(ad.id), "asset_id": int(asset.id)})
                remaining -= 1
            except WebsiteAdsExecutionLockLost:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                result["errors"].append(
                    {
                        "adgroup_local_id": int(group.id),
                        "asset_id": int(asset.id),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
                continue
        if created:
            result["groups"].append({"adgroup_local_id": int(group.id), "created": created})
            result["created"] += len(created)
    return result


def _group_race_evidence(
    *,
    spend: Decimal,
    impressions: int,
    clicks: int,
    min_spend: Decimal | None = None,
    winner_min_impressions: int | None = None,
    min_impressions: int | None = None,
    min_clicks: int | None = None,
) -> dict[str, Any]:
    spend_gate = min_spend or _decimal(settings.WEBSITE_ADS_GROUP_RACING_MIN_SPEND, "2.40")
    winner_impression_gate = max(
        1,
        int(
            winner_min_impressions
            or settings.WEBSITE_ADS_GROUP_RACING_WIN_MIN_IMPRESSIONS
        ),
    )
    impression_gate = max(1, int(min_impressions or settings.WEBSITE_ADS_GROUP_RACING_MIN_IMPRESSIONS))
    click_gate = max(1, int(min_clicks or settings.WEBSITE_ADS_GROUP_RACING_MIN_CLICKS))
    ctr = Decimal(clicks) / impressions if impressions else None
    cpc = spend / clicks if clicks else None
    winner_sample_ready = impressions >= winner_impression_gate and clicks >= click_gate
    low_ctr_sample_ready = impressions >= impression_gate
    high_cpc_sample_ready = spend >= spend_gate and clicks >= click_gate
    zero_click_sample_ready = (
        impressions >= impression_gate and clicks == 0 and spend >= spend_gate
    )
    winner = bool(
        winner_sample_ready
        and ctr is not None
        and ctr >= _decimal(settings.WEBSITE_ADS_GROUP_RACING_WIN_CTR, "0.04")
        and cpc is not None
        and cpc <= _decimal(settings.WEBSITE_ADS_GROUP_RACING_WIN_MAX_CPC, "0.30")
    )
    loser_reasons: list[str] = []
    if (
        low_ctr_sample_ready
        and ctr is not None
        and ctr < _decimal(settings.WEBSITE_ADS_GROUP_RACING_LOSE_CTR, "0.03")
    ):
        loser_reasons.append("LOW_CTR")
    if (
        high_cpc_sample_ready
        and cpc is not None
        and cpc > _decimal(settings.WEBSITE_ADS_GROUP_RACING_LOSE_CPC, "0.45")
    ):
        loser_reasons.append("HIGH_CPC")
    if zero_click_sample_ready:
        loser_reasons.append("ZERO_CLICK_SPEND")
    sample_ready = winner_sample_ready or bool(loser_reasons)
    score = float((ctr or Decimal("0")) * Decimal("1000")) - float(cpc or spend) + clicks * 0.2
    return {
        "sample_ready": sample_ready,
        "sample": {
            "winner_ready": winner_sample_ready,
            "low_ctr_ready": low_ctr_sample_ready,
            "high_cpc_ready": high_cpc_sample_ready,
            "zero_click_ready": zero_click_sample_ready,
        },
        "thresholds": {
            "winner_min_impressions": winner_impression_gate,
            "loser_min_impressions": impression_gate,
            "min_clicks": click_gate,
            "min_spend_for_cost_signal": float(spend_gate),
        },
        "winner": winner,
        "loser": bool(loser_reasons),
        "loser_reasons": loser_reasons,
        "spend": float(spend),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": float(ctr) if ctr is not None else None,
        "cpc": float(cpc) if cpc is not None else None,
        "score": round(score, 6),
    }


def _group_today_evidence(db: Session, group: WebsiteAdsAdGroup, day: str) -> dict[str, Any]:
    ad_ids = list(
        db.scalars(
            select(WebsiteAdsAd.id).where(WebsiteAdsAd.adgroup_local_id == int(group.id))
        ).all()
    )
    spend = Decimal("0")
    impressions = 0
    clicks = 0
    if ad_ids:
        values = db.execute(
            select(
                func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
            ).where(
                WebsiteAdsMetricHourly.ad_local_id.in_(ad_ids),
                func.date(WebsiteAdsMetricHourly.stat_hour) == day,
            )
        ).one()
        spend = _decimal(values[0])
        impressions = _int(values[1])
        clicks = _int(values[2])
    return {
        "adgroup_local_id": int(group.id),
        "adgroup_id": str(group.adgroup_id),
        "name": str(group.name),
        **_group_race_evidence(spend=spend, impressions=impressions, clicks=clicks),
    }


def _recent_race_exists(db: Session, campaign: WebsiteAdsCampaign) -> bool:
    cutoff = _utcnow() - timedelta(minutes=max(1, int(settings.WEBSITE_ADS_GROUP_RACING_COOLDOWN_MINUTES)))
    campaign_request_id = WebsiteAdsActionLog.request_json["campaign_local_id"].as_integer()
    matching_log_id = db.scalar(
        select(WebsiteAdsActionLog.id)
        .where(
            WebsiteAdsActionLog.workspace_id == int(campaign.workspace_id),
            WebsiteAdsActionLog.auth_id == int(campaign.auth_id),
            WebsiteAdsActionLog.actor_type == "HERMES_GROUP_RACING",
            WebsiteAdsActionLog.created_at >= cutoff,
            campaign_request_id == int(campaign.id),
        )
        .order_by(WebsiteAdsActionLog.created_at.desc(), WebsiteAdsActionLog.id.desc())
        .limit(1)
    )
    return matching_log_id is not None


def _audience_exploration_keywords(
    db: Session,
    *,
    product: WebsiteAdsLandingPage,
) -> list[str]:
    values: list[str] = []
    analysis = dict(product.hermes_analysis_json or {})
    for value in analysis.get("interest_keywords") or []:
        if str(value).strip():
            values.append(str(value).strip())
    for hypothesis in analysis.get("audience_hypotheses") or []:
        if not isinstance(hypothesis, Mapping):
            continue
        for value in hypothesis.get("interest_keywords") or []:
            if str(value).strip():
                values.append(str(value).strip())
    reports = list(
        db.scalars(
            select(WebsiteAdsDailyReport)
            .where(
                WebsiteAdsDailyReport.workspace_id == int(product.workspace_id),
                WebsiteAdsDailyReport.landing_page_id == int(product.id),
            )
            .order_by(WebsiteAdsDailyReport.report_date.desc(), WebsiteAdsDailyReport.id.desc())
            .limit(3)
        ).all()
    )
    for report in reports:
        review = dict(report.hermes_report_json or {})
        for proposal in review.get("next_audience_tests") or []:
            if not isinstance(proposal, Mapping):
                continue
            for value in proposal.get("interest_keywords") or []:
                if str(value).strip():
                    values.append(str(value).strip())
    values.extend(
        str(value).strip()
        for value in (product.content_category, product.title, product.brand)
        if str(value or "").strip()
    )
    return list(dict.fromkeys(value.casefold() for value in values))


async def _build_race_targeting(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    product: WebsiteAdsLandingPage,
    winner: WebsiteAdsAdGroup,
    all_groups: list[WebsiteAdsAdGroup],
) -> tuple[dict[str, Any], dict[str, Any]]:
    targeting = dict(winner.targeting_json or {})
    targeting["hermes_race_mode"] = "WINNER_SCALE"
    audit: dict[str, Any] = {"mode": "WINNER_SCALE", "reason": "NO_UNTESTED_OFFICIAL_INTEREST"}
    if not bool(settings.WEBSITE_ADS_AUDIENCE_EXPLORATION_ENABLED):
        return targeting, {"mode": "WINNER_SCALE", "reason": "EXPLORATION_DISABLED"}

    used_ids = {
        str(value)
        for group in all_groups
        for value in dict(group.targeting_json or {}).get("interest_category_ids") or []
        if str(value)
    }
    keywords = _audience_exploration_keywords(db, product=product)
    ranked = rank_general_interest_categories(
        str(campaign.advertiser_id),
        keywords,
        exclude_ids=used_ids,
        limit=max(4, int(settings.WEBSITE_ADS_AUDIENCE_EXPLORATION_MAX_CANDIDATES)),
    )
    minimum_ids = max(1, int(settings.WEBSITE_ADS_AUDIENCE_MIN_INTEREST_IDS))
    maximum_ids = max(minimum_ids, int(settings.WEBSITE_ADS_AUDIENCE_MAX_INTEREST_IDS))
    if len(ranked) < minimum_ids:
        return targeting, audit

    selected = ranked[:minimum_ids]
    targeting["interest_category_ids"] = [str(item["id"]) for item in selected]
    targeting["interest_keywords"] = [str(item["name"]) for item in selected]
    targeting["audience_segment"] = "Hermes challenger: " + ", ".join(
        str(item["name"]) for item in selected
    )
    targeting["hypothesis"] = (
        "Test an untried official TikTok interest cluster while holding creative mix and total budget constant."
    )
    targeting["hermes_race_mode"] = "AUDIENCE_EXPLORATION"
    targeting["hermes_exploration_catalog_candidates"] = ranked[:maximum_ids]
    target_grade = max(1, int(settings.WEBSITE_ADS_AUDIENCE_ESTIMATE_TARGET_GRADE))
    minimum_grade = max(1, int(settings.WEBSITE_ADS_AUDIENCE_ESTIMATE_MIN_GRADE))
    adjustments: list[str] = []
    estimate: dict[str, Any]
    try:
        payload = await api.estimate_audience_size(
            _audience_estimate_body(
                advertiser_id=str(campaign.advertiser_id),
                pixel_id=str(winner.pixel_id),
                group=targeting,
            )
        )
        estimate = _audience_size_summary(payload)
        if int(estimate.get("stage") or 0) < target_grade:
            targeting["gender"] = "GENDER_UNLIMITED"
            targeting["age_groups"] = sorted(ALLOWED_AGE_GROUPS)
            adjustments.append("BROADEN_DEMOGRAPHICS")
            while len(selected) < min(maximum_ids, len(ranked)):
                selected.append(ranked[len(selected)])
                targeting["interest_category_ids"] = [str(item["id"]) for item in selected]
                targeting["interest_keywords"] = [str(item["name"]) for item in selected]
                payload = await api.estimate_audience_size(
                    _audience_estimate_body(
                        advertiser_id=str(campaign.advertiser_id),
                        pixel_id=str(winner.pixel_id),
                        group=targeting,
                    )
                )
                estimate = _audience_size_summary(payload)
                adjustments.append("ADD_RELATED_OFFICIAL_INTEREST")
                if int(estimate.get("stage") or 0) >= target_grade:
                    break
        estimate["target_grade"] = target_grade
        estimate["minimum_grade"] = minimum_grade
        estimate["meets_minimum"] = int(estimate.get("stage") or 0) >= minimum_grade
        estimate["meets_target"] = int(estimate.get("stage") or 0) >= target_grade
        if not estimate["meets_minimum"]:
            targeting = dict(winner.targeting_json or {})
            targeting["hermes_race_mode"] = "WINNER_SCALE"
            return targeting, {
                "mode": "WINNER_SCALE",
                "reason": "CHALLENGER_TOO_NARROW",
                "rejected_estimate": estimate,
                "adjustments": adjustments,
            }
    except Exception as exc:
        estimate = {
            "stage": None,
            "label": "UNAVAILABLE",
            "meets_minimum": None,
            "meets_target": None,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }
        adjustments.append("ESTIMATE_UNAVAILABLE_NON_BLOCKING")

    targeting["audience_size_estimate"] = estimate
    targeting["audience_width_adjustments"] = adjustments
    audit = {
        "mode": "AUDIENCE_EXPLORATION",
        "selected_official_interests": selected,
        "estimate": estimate,
        "adjustments": adjustments,
        "used_interest_count": len(used_ids),
    }
    return targeting, audit


async def _create_race_clone(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    plan: WebsiteAdsMediaPlan,
    campaign: WebsiteAdsCampaign,
    winner: WebsiteAdsAdGroup,
    loser: WebsiteAdsAdGroup,
    targeting_override: Mapping[str, Any] | None = None,
    require_execution_lease: bool = False,
) -> tuple[WebsiteAdsAdGroup, dict[str, Any]]:
    targeting = dict(targeting_override or winner.targeting_json or {})
    targeting["location_ids"] = enforce_website_ads_location_policy(
        targeting.get("location_ids") or ["6252001"]
    )
    targeting["hermes_race_parent_group_id"] = int(winner.id)
    targeting["hermes_race_replaced_group_id"] = int(loser.id)
    api_targeting = {
        key: value
        for key, value in targeting.items()
        if key in TIKTOK_TARGETING_FIELDS and value not in (None, [], "")
    }
    placement_type, placements = enforce_website_ads_placement_policy(
        targeting.get("placement_type"), targeting.get("placements")
    )
    clone_mode = str(targeting.get("hermes_race_mode") or "WINNER_SCALE").upper()
    suffix = "audience challenger" if clone_mode == "AUDIENCE_EXPLORATION" else "winner scale"
    name = f"{winner.name[:360]} | Hermes {suffix} | {_utcnow():%Y%m%d%H%M}"
    body: dict[str, Any] = {
        "advertiser_id": str(campaign.advertiser_id),
        "campaign_id": str(campaign.campaign_id),
        "adgroup_name": name,
        "promotion_type": "WEBSITE",
        "pixel_id": str(winner.pixel_id),
        "placement_type": placement_type,
        "placements": placements,
        **api_targeting,
        "budget_mode": "BUDGET_MODE_DAY",
        "budget": float(_decimal(loser.budget)),
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": _schedule_text(db, plan),
        "bid_type": str(winner.bid_type),
        **website_ads_optimization_fields(),
        "pacing": "PACING_MODE_SMOOTH",
        "operation_status": "DISABLE",
    }
    if str(winner.bid_type).upper() == "BID_TYPE_CUSTOM" and winner.conversion_bid_price is not None:
        body["conversion_bid_price"] = float(winner.conversion_bid_price)
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    response = await api.create_adgroup(body)
    remote_id = _first_id(_data(response), "adgroup_id", "adgroup_ids")
    if not remote_id:
        raise RuntimeError("TikTok did not return adgroup_id for audience-race clone")
    clone = WebsiteAdsAdGroup(
        campaign_local_id=int(campaign.id),
        adgroup_id=remote_id,
        name=name,
        pixel_id=str(winner.pixel_id),
        targeting_json=targeting,
        budget_mode="BUDGET_MODE_DAY",
        budget=_decimal(loser.budget),
        bid_type=str(winner.bid_type),
        conversion_bid_price=winner.conversion_bid_price,
        schedule_start_time=body["schedule_start_time"],
        operation_status="DISABLE",
        raw_json=_json_safe(response),
    )
    db.add(clone)
    assert_website_ads_execution_lock(
        db,
        required=require_execution_lease,
    )
    db.commit()
    db.refresh(clone)
    return clone, response


async def run_group_racing(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    campaign: WebsiteAdsCampaign,
    day: str,
    require_execution_lease: bool = False,
) -> dict[str, Any]:
    """Replace one clear targeting loser with a budget-neutral winner clone."""

    def _assert_execution() -> None:
        assert_website_ads_execution_lock(
            db,
            required=require_execution_lease,
        )

    _assert_execution()
    if not bool(settings.WEBSITE_ADS_GROUP_RACING_ENABLED) or not _campaign_enabled(campaign):
        return {"status": "SKIPPED", "reason": "DISABLED_OR_CAMPAIGN_NOT_ACTIVE"}
    if _recent_race_exists(db, campaign):
        return {"status": "SKIPPED", "reason": "COOLDOWN"}
    plan = db.scalar(
        select(WebsiteAdsMediaPlan)
        .where(
            WebsiteAdsMediaPlan.campaign_local_id == int(campaign.id),
            WebsiteAdsMediaPlan.status == "ACTIVE",
        )
        .order_by(WebsiteAdsMediaPlan.id.desc())
        .limit(1)
    )
    product = db.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    if plan is None or product is None:
        return {"status": "SKIPPED", "reason": "ACTIVE_PLAN_OR_PRODUCT_MISSING"}
    all_groups = list(
        db.scalars(
            select(WebsiteAdsAdGroup).where(
                WebsiteAdsAdGroup.campaign_local_id == int(campaign.id),
                WebsiteAdsAdGroup.adgroup_id.is_not(None),
                or_(WebsiteAdsAdGroup.operation_status.is_(None), WebsiteAdsAdGroup.operation_status != "DELETE"),
            ).order_by(WebsiteAdsAdGroup.id)
        ).all()
    )
    groups = [group for group in all_groups if str(group.operation_status or "").upper() == "ENABLE"]
    if len(groups) < 2:
        return {"status": "SKIPPED", "reason": "NOT_ENOUGH_ACTIVE_GROUPS"}
    if len(all_groups) >= max(1, int(settings.WEBSITE_ADS_GROUP_RACING_MAX_HISTORY_GROUPS)):
        return {"status": "SKIPPED", "reason": "GROUP_HISTORY_LIMIT"}
    evidence = [_group_today_evidence(db, group, day) for group in groups]
    winners = [item for item in evidence if item["winner"]]
    losers = [item for item in evidence if item["loser"]]
    if not winners or not losers:
        return {"status": "SKIPPED", "reason": "NO_CLEAR_WINNER_AND_LOSER", "groups": evidence}
    winner_evidence = max(winners, key=lambda item: (item["score"], item["clicks"]))
    loser_evidence = min(losers, key=lambda item: (item["score"], -item["spend"]))
    if winner_evidence["adgroup_local_id"] == loser_evidence["adgroup_local_id"]:
        return {"status": "SKIPPED", "reason": "AMBIGUOUS_COMPARISON", "groups": evidence}
    review = await review_website_adgroup_race_action(
        campaign_id=int(campaign.id), winner=winner_evidence, loser=loser_evidence
    )
    if review["decision"] == "HOLD":
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_GROUP_RACING",
                action="HOLD_GROUP_RACE",
                reason=review["reason"],
                result="SKIPPED",
                request_json={"campaign_local_id": int(campaign.id), "winner": winner_evidence, "loser": loser_evidence},
                response_json={"hermes_review": review},
            )
        )
        _assert_execution()
        db.commit()
        return {"status": "HOLD", "review": review}

    winner = db.get(WebsiteAdsAdGroup, int(winner_evidence["adgroup_local_id"]))
    loser = db.get(WebsiteAdsAdGroup, int(loser_evidence["adgroup_local_id"]))
    if winner is None or loser is None:
        return {"status": "SKIPPED", "reason": "GROUP_NOT_FOUND"}
    winner_video_ids = [
        str(value)
        for value in db.scalars(
            select(WebsiteAdsAd.video_id).where(
                WebsiteAdsAd.adgroup_local_id == int(winner.id),
                WebsiteAdsAd.operation_status == "ENABLE",
            )
        ).all()
        if value
    ]
    ranked = _candidate_assets(db, campaign=campaign, product=product)
    winner_order = {video_id: index for index, video_id in enumerate(winner_video_ids)}
    ranked.sort(
        key=lambda item: (
            1 if str(item[0].video_id) in winner_order else 0,
            -winner_order.get(str(item[0].video_id), 10_000),
            item[1],
        ),
        reverse=True,
    )
    target = max(2, int(settings.WEBSITE_ADS_TARGET_ACTIVE_ADS_PER_GROUP))
    seed_assets = ranked[:target]
    if len(seed_assets) < 2:
        return {"status": "SKIPPED", "reason": "NOT_ENOUGH_SEED_ASSETS"}

    clone: WebsiteAdsAdGroup | None = None
    created_ads: list[WebsiteAdsAd] = []
    clone_response: dict[str, Any] | None = None
    race_targeting, audience_exploration = await _build_race_targeting(
        db,
        api=api,
        campaign=campaign,
        product=product,
        winner=winner,
        all_groups=all_groups,
    )
    try:
        clone, clone_response = await _create_race_clone(
            db,
            api=api,
            plan=plan,
            campaign=campaign,
            winner=winner,
            loser=loser,
            targeting_override=race_targeting,
            require_execution_lease=require_execution_lease,
        )
        for asset, score in seed_assets:
            try:
                ad, _ = await _create_ad_for_asset(
                    db,
                    api=api,
                    plan=plan,
                    campaign=campaign,
                    product=product,
                    group=clone,
                    asset=asset,
                    expansion_mode="GROUP_RACE_WINNER_CLONE",
                    score=score,
                    require_execution_lease=require_execution_lease,
                )
                created_ads.append(ad)
            except WebsiteAdsExecutionLockLost:
                raise
            except Exception:
                logger.exception("Failed to seed one creative into an audience-race clone")
        if len(created_ads) < 2:
            _assert_execution()
            await api.update_adgroup_status(str(campaign.advertiser_id), [str(clone.adgroup_id)], "DISABLE")
            clone.operation_status = "DISABLE"
            db.add(clone)
            _assert_execution()
            db.commit()
            return {"status": "FAILED", "reason": "CLONE_HAS_TOO_FEW_CREATIVES"}

        loser_ads = list(
            db.scalars(select(WebsiteAdsAd).where(WebsiteAdsAd.adgroup_local_id == int(loser.id))).all()
        )
        _assert_execution()
        await api.update_adgroup_status(str(campaign.advertiser_id), [str(loser.adgroup_id)], "DISABLE")
        loser.operation_status = "DISABLE"
        for ad in loser_ads:
            if str(ad.operation_status or "").upper() == "ENABLE":
                ad.operation_status = "DISABLE"
                db.add(ad)
        db.add(loser)
        _assert_execution()
        db.commit()
        try:
            _assert_execution()
            await api.update_adgroup_status(str(campaign.advertiser_id), [str(clone.adgroup_id)], "ENABLE")
            _assert_execution()
            await api.update_ad_status(
                str(campaign.advertiser_id), [str(ad.ad_id) for ad in created_ads if ad.ad_id], "ENABLE"
            )
        except WebsiteAdsExecutionLockLost:
            raise
        except Exception:
            _assert_execution()
            await api.update_adgroup_status(str(campaign.advertiser_id), [str(loser.adgroup_id)], "ENABLE")
            loser.operation_status = "ENABLE"
            for ad in loser_ads:
                if ad.ad_id:
                    ad.operation_status = "ENABLE"
                    db.add(ad)
            db.add(loser)
            _assert_execution()
            db.commit()
            raise
        clone.operation_status = "ENABLE"
        for ad in created_ads:
            ad.operation_status = "ENABLE"
            db.add(ad)
        db.add(clone)
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                actor_type="HERMES_GROUP_RACING",
                action=(
                    "TEST_AUDIENCE_CHALLENGER"
                    if audience_exploration.get("mode") == "AUDIENCE_EXPLORATION"
                    else "SCALE_WINNER_TARGETING"
                ),
                reason=review["reason"],
                result="SUCCESS",
                request_json={
                    "campaign_local_id": int(campaign.id),
                    "day": day,
                    "winner": winner_evidence,
                    "loser": loser_evidence,
                    "budget_delta": 0,
                    "audience_exploration": audience_exploration,
                },
                response_json={
                    "hermes_review": review,
                    "clone_adgroup_local_id": int(clone.id),
                    "clone_adgroup_id": str(clone.adgroup_id),
                    "created_ad_ids": [str(ad.ad_id) for ad in created_ads],
                    "tiktok": _json_safe(clone_response),
                },
                metrics_json={
                    "winner": winner_evidence,
                    "loser": loser_evidence,
                    "audience_exploration": audience_exploration,
                },
            )
        )
        _assert_execution()
        db.commit()
        return {
            "status": "SCALED",
            "winner_group_id": int(winner.id),
            "paused_group_id": int(loser.id),
            "clone_group_id": int(clone.id),
            "created_ads": len(created_ads),
            "race_mode": audience_exploration.get("mode"),
        }
    except WebsiteAdsExecutionLockLost:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        if clone is not None and clone.adgroup_id:
            try:
                _assert_execution()
                await api.update_adgroup_status(str(campaign.advertiser_id), [str(clone.adgroup_id)], "DISABLE")
            except Exception:
                logger.exception("Failed to disable a failed audience-race clone")
        raise
