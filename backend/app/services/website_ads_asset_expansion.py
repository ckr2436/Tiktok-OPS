from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any, Mapping

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsMetricHourly,
)
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
    website_ads_execution_lease,
)
from app.services.website_ads_hermes_planner import (
    PRODUCTION_ORIGIN_PRIORITY,
    _asset_performance,
    _asset_product_confidence,
    _creative_selection_score,
    _json_safe,
    _production_origin,
    review_website_asset_expansion_action,
)
from app.services.website_ads_plan_launch import (
    TIKTOK_TARGETING_FIELDS,
    _data,
    _first_id,
    _schedule_text,
    _spark_asset_delivery,
)
from app.services.website_ads_tiktok_contract import (
    WEBSITE_ADS_OPTIMIZATION_EVENT,
    enforce_website_ads_location_policy,
    enforce_website_ads_placement_policy,
    normalize_website_sales_call_to_action,
    select_tiktok_video_identity,
    website_ads_optimization_fields,
)
from app.services.website_ads_tracking import build_tracking_url


logger = logging.getLogger("gmv.services.website_ads_asset_expansion")

MIN_ADGROUP_DAILY_BUDGET = Decimal("20.00")
CLAIMABLE_STATUSES = {
    "PENDING",
    "PARTIAL",
    "RETRY",
    "WAITING_PRODUCT",
    "WAITING_CAMPAIGN",
    "WAITING_CAPACITY",
    "WAITING_HERMES",
}
AUTO_LAUNCH_STATE_PRIORITY = {
    "PENDING": 0,
    "PARTIAL": 1,
    "RETRY": 2,
    "WAITING_HERMES": 3,
    "WAITING_CAPACITY": 4,
    "WAITING_PRODUCT": 5,
    "WAITING_CAMPAIGN": 6,
    "DEPLOYING": 7,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "", "-") else default))
    except Exception:
        return Decimal(default)


def _campaign_enabled(campaign: WebsiteAdsCampaign) -> bool:
    return (
        str(campaign.local_status or "").upper() == "ACTIVE"
        and str(campaign.operation_status or "").upper() == "ENABLE"
    )


def _group_capacity(
    *,
    total_ads: int,
    enabled_ads: int,
    campaign_enabled: bool,
    max_active_ads: int,
    max_total_ads: int,
) -> int:
    effective_active = enabled_ads if campaign_enabled else total_ads
    return max(0, min(max_active_ads - effective_active, max_total_ads - total_ads))


def _clone_budget_allocation(
    *,
    plan_budget: Decimal,
    group_budgets: list[Decimal],
    minimum_budget: Decimal = MIN_ADGROUP_DAILY_BUDGET,
    donor_indices: set[int] | None = None,
) -> dict[str, Any] | None:
    if not group_budgets:
        return None
    current_total = sum(group_budgets, Decimal("0"))
    if current_total > plan_budget + Decimal("0.01"):
        return None
    headroom = max(Decimal("0"), plan_budget - current_total)
    reduction_required = max(Decimal("0"), minimum_budget - headroom)
    if reduction_required <= Decimal("0.01"):
        return {
            "donor_index": None,
            "donor_budget_before": None,
            "donor_budget_after": None,
            "new_group_budget": minimum_budget,
        }
    candidates = [
        (index, budget)
        for index, budget in enumerate(group_budgets)
        if (donor_indices is None or index in donor_indices)
        and budget - reduction_required >= minimum_budget
    ]
    if not candidates:
        return None
    donor_index, donor_budget = max(candidates, key=lambda item: item[1])
    return {
        "donor_index": donor_index,
        "donor_budget_before": donor_budget,
        "donor_budget_after": donor_budget - reduction_required,
        "new_group_budget": minimum_budget,
    }


def _group_runtime(db: Session, group: WebsiteAdsAdGroup, *, campaign_enabled: bool) -> dict[str, Any]:
    ads = list(
        db.scalars(
            select(WebsiteAdsAd).where(
                WebsiteAdsAd.adgroup_local_id == int(group.id),
                WebsiteAdsAd.ad_id.is_not(None),
                or_(WebsiteAdsAd.operation_status.is_(None), WebsiteAdsAd.operation_status != "DELETE"),
            )
        ).all()
    )
    ad_ids = [int(ad.id) for ad in ads]
    spend = Decimal("0")
    impressions = 0
    clicks = 0
    conversions = Decimal("0")
    if ad_ids:
        spend, impressions, clicks, conversions = db.execute(
            select(
                func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
            ).where(WebsiteAdsMetricHourly.ad_local_id.in_(ad_ids))
        ).one()
    spend_value = _decimal(spend)
    impression_count = int(impressions or 0)
    click_count = int(clicks or 0)
    enabled_count = sum(str(ad.operation_status or "").upper() == "ENABLE" for ad in ads)
    capacity = _group_capacity(
        total_ads=len(ads),
        enabled_ads=enabled_count,
        campaign_enabled=campaign_enabled,
        max_active_ads=max(1, int(settings.WEBSITE_ADS_MAX_ACTIVE_ADS_PER_GROUP)),
        max_total_ads=max(1, int(settings.WEBSITE_ADS_MAX_TOTAL_ADS_PER_GROUP)),
    )
    ctr = click_count / impression_count if impression_count else 0.0
    cpc = float(spend_value / click_count) if click_count else None
    quality_score = min(12.0, click_count * 0.35)
    if impression_count >= 100:
        quality_score += min(20.0, ctr * 250.0)
    if cpc is not None:
        quality_score += max(-12.0, 8.0 - cpc * 8.0)
    if _decimal(conversions) > 0:
        quality_score += min(12.0, float(_decimal(conversions)) * 2.0)
    return {
        "group": group,
        "total_ads": len(ads),
        "enabled_ads": enabled_count,
        "capacity": capacity,
        "spend": float(spend_value),
        "impressions": impression_count,
        "clicks": click_count,
        "ctr": ctr,
        "cpc": cpc,
        "view_content_events": float(_decimal(conversions)),
        "quality_score": round(quality_score, 4),
    }


def _eligible_plan(
    db: Session,
    asset: WebsiteAdsCreativeAsset,
) -> tuple[WebsiteAdsMediaPlan, WebsiteAdsCampaign, WebsiteAdsLandingPage] | None:
    row = db.execute(
        select(WebsiteAdsMediaPlan, WebsiteAdsCampaign, WebsiteAdsLandingPage)
        .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsMediaPlan.campaign_local_id)
        .join(WebsiteAdsLandingPage, WebsiteAdsLandingPage.id == WebsiteAdsMediaPlan.landing_page_id)
        .where(
            WebsiteAdsMediaPlan.workspace_id == int(asset.workspace_id),
            WebsiteAdsMediaPlan.auth_id == int(asset.auth_id),
            WebsiteAdsMediaPlan.advertiser_id == str(asset.advertiser_id),
            WebsiteAdsMediaPlan.landing_page_id == int(asset.landing_page_id),
            WebsiteAdsMediaPlan.status == "ACTIVE",
            WebsiteAdsCampaign.campaign_id.is_not(None),
            WebsiteAdsCampaign.local_status.in_(("ACTIVE", "PAUSED")),
            or_(WebsiteAdsCampaign.operation_status.is_(None), WebsiteAdsCampaign.operation_status != "DELETE"),
            WebsiteAdsLandingPage.is_active.is_(True),
        )
        .order_by(WebsiteAdsMediaPlan.id.desc())
        .limit(1)
    ).first()
    return tuple(row) if row else None


def _asset_rank(db: Session, asset: WebsiteAdsCreativeAsset) -> tuple[float, int, datetime]:
    product = db.get(WebsiteAdsLandingPage, asset.landing_page_id) if asset.landing_page_id else None
    analysis = dict(asset.hermes_analysis_json or {})
    performance = _asset_performance(db, asset)
    origin = _production_origin(asset, analysis)
    score = _creative_selection_score(
        performance,
        product_confidence=_asset_product_confidence(asset, int(asset.landing_page_id)) if asset.landing_page_id else 0.0,
        analysis_status=str(asset.analysis_status or ""),
        production_origin=origin,
        reference_price=float(product.reference_price or 0) if product else 0.0,
    )
    return (
        score,
        PRODUCTION_ORIGIN_PRIORITY.get(origin, 0),
        asset.created_at or datetime.min,
    )


def _asset_queue_key(
    asset: WebsiteAdsCreativeAsset,
    *,
    has_managed_plan: bool,
    rank: tuple[float, int, datetime],
) -> tuple[Any, ...]:
    score, origin_priority, _ = rank
    return (
        0 if has_managed_plan else 1,
        AUTO_LAUNCH_STATE_PRIORITY.get(str(asset.auto_launch_status or "PENDING").upper(), 99),
        asset.auto_launch_next_retry_at or datetime.min,
        asset.updated_at or datetime.min,
        -float(score),
        -int(origin_priority),
        int(asset.id),
    )


def _set_state(
    db: Session,
    asset: WebsiteAdsCreativeAsset,
    status: str,
    *,
    decision: Mapping[str, Any] | None = None,
    error: str | None = None,
    retry_minutes: int | None = None,
    launched: bool = False,
) -> None:
    asset.auto_launch_status = status
    asset.auto_launch_decision_json = _json_safe(dict(decision or {})) or None
    asset.auto_launch_error = str(error or "")[:4000] or None
    asset.auto_launch_next_retry_at = (
        _utcnow() + timedelta(minutes=max(1, int(retry_minutes)))
        if retry_minutes is not None
        else None
    )
    if launched:
        asset.auto_launched_at = _utcnow()
    db.add(asset)
    assert_website_ads_execution_lock(db, required=True)
    db.commit()


def _claim_asset(db: Session, asset: WebsiteAdsCreativeAsset) -> bool:
    assert_website_ads_execution_lock(db, required=True)
    now = _utcnow()
    stale_before = now - timedelta(minutes=30)
    status = str(asset.auto_launch_status or "PENDING").upper()
    conditions = [WebsiteAdsCreativeAsset.id == int(asset.id)]
    if status == "DEPLOYING":
        conditions.extend(
            [
                WebsiteAdsCreativeAsset.auto_launch_status == "DEPLOYING",
                WebsiteAdsCreativeAsset.updated_at <= stale_before,
            ]
        )
    else:
        conditions.append(WebsiteAdsCreativeAsset.auto_launch_status == status)
    claim = db.execute(
        update(WebsiteAdsCreativeAsset)
        .where(*conditions)
        .values(
            auto_launch_status="DEPLOYING",
            auto_launch_attempts=WebsiteAdsCreativeAsset.auto_launch_attempts + 1,
            auto_launch_error=None,
            updated_at=now,
        )
    )
    assert_website_ads_execution_lock(db, required=True)
    db.commit()
    return int(claim.rowcount or 0) == 1


def _existing_group_ids(
    db: Session,
    *,
    campaign_id: int,
    video_id: str,
    require_enabled: bool = False,
) -> set[int]:
    query = select(WebsiteAdsAd.adgroup_local_id).where(
        WebsiteAdsAd.campaign_local_id == int(campaign_id),
        WebsiteAdsAd.video_id == str(video_id),
        WebsiteAdsAd.ad_id.is_not(None),
        or_(
            WebsiteAdsAd.operation_status.is_(None),
            WebsiteAdsAd.operation_status.in_(("ENABLE", "DISABLE")),
        ),
    )
    if require_enabled:
        query = query.where(WebsiteAdsAd.operation_status == "ENABLE")
    return {
        int(value)
        for value in db.scalars(query).all()
    }


def _clone_group_plan(
    *,
    plan: WebsiteAdsMediaPlan,
    groups: list[WebsiteAdsAdGroup],
    runtimes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(groups) >= max(1, int(settings.WEBSITE_ADS_MAX_ADGROUPS_PER_PLAN)):
        return None
    precise = [
        runtime
        for runtime in runtimes
        if runtime["group"].adgroup_id
        and (
            list((runtime["group"].targeting_json or {}).get("interest_category_ids") or [])
            or list((runtime["group"].targeting_json or {}).get("audience_ids") or [])
        )
    ]
    if not precise:
        return None
    precise.sort(key=lambda item: (item["quality_score"], float(item["group"].budget)), reverse=True)
    usable_group_ids = {int(runtime["group"].id) for runtime in runtimes}
    allocation = _clone_budget_allocation(
        plan_budget=_decimal(plan.daily_budget),
        group_budgets=[_decimal(group.budget) for group in groups],
        donor_indices={index for index, group in enumerate(groups) if int(group.id) in usable_group_ids},
    )
    if allocation is None:
        return None
    donor = groups[int(allocation["donor_index"])] if allocation["donor_index"] is not None else None
    template = precise[0]["group"]
    return {
        "template": template,
        "donor": donor,
        "allocation": allocation,
        "public": {
            "reason": "CLONE_PRECISE_GROUP_WITHIN_PLAN_BUDGET",
            "template_group_id": int(template.id),
            "donor_group_id": int(donor.id) if donor is not None else None,
            "donor_budget_before": (
                float(allocation["donor_budget_before"])
                if allocation["donor_budget_before"] is not None
                else None
            ),
            "donor_budget_after": (
                float(allocation["donor_budget_after"])
                if allocation["donor_budget_after"] is not None
                else None
            ),
            "new_group_budget": float(allocation["new_group_budget"]),
            "plan_daily_budget": float(_decimal(plan.daily_budget)),
            "current_group_budget_total": float(
                sum((_decimal(group.budget) for group in groups), Decimal("0"))
            ),
        },
    }


async def _clone_group_within_budget(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    plan: WebsiteAdsMediaPlan,
    campaign: WebsiteAdsCampaign,
    groups: list[WebsiteAdsAdGroup],
    runtimes: list[dict[str, Any]],
    asset: WebsiteAdsCreativeAsset,
) -> tuple[WebsiteAdsAdGroup | None, dict[str, Any]]:
    assert_website_ads_execution_lock(db, required=True)
    clone_plan = _clone_group_plan(plan=plan, groups=groups, runtimes=runtimes)
    if clone_plan is None:
        return None, {"reason": "PLAN_BUDGET_HAS_NO_SAFE_CLONE_CAPACITY"}
    allocation = clone_plan["allocation"]
    donor = clone_plan["donor"]
    template = clone_plan["template"]
    donor_response: dict[str, Any] | None = None
    if donor is not None:
        assert_website_ads_execution_lock(db, required=True)
        donor_response = await api.update_adgroup_budget(
            str(campaign.advertiser_id),
            str(donor.adgroup_id),
            float(allocation["donor_budget_after"]),
        )
        donor.budget = allocation["donor_budget_after"]
        db.add(donor)
        assert_website_ads_execution_lock(db, required=True)
        db.commit()

    targeting = dict(template.targeting_json or {})
    targeting["location_ids"] = enforce_website_ads_location_policy(
        targeting.get("location_ids") or ["6252001"]
    )
    api_targeting = {
        key: value
        for key, value in targeting.items()
        if key in TIKTOK_TARGETING_FIELDS and value not in (None, [], "")
    }
    placement_type, placements = enforce_website_ads_placement_policy(
        targeting.get("placement_type"), targeting.get("placements")
    )
    clone_name = f"{template.name[:390]} | Hermes new creative {asset.id} | {_utcnow():%Y%m%d%H%M}"
    budget = _decimal(allocation["new_group_budget"])
    schedule_text = _schedule_text(db, plan)
    operation_status = "ENABLE" if _campaign_enabled(campaign) else "DISABLE"
    body = {
        "advertiser_id": str(campaign.advertiser_id),
        "campaign_id": str(campaign.campaign_id),
        "adgroup_name": clone_name,
        "promotion_type": "WEBSITE",
        "pixel_id": str(template.pixel_id),
        "placement_type": placement_type,
        "placements": placements,
        **api_targeting,
        "budget_mode": "BUDGET_MODE_DAY",
        "budget": float(budget),
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": schedule_text,
        "bid_type": str(template.bid_type),
        **website_ads_optimization_fields(),
        "pacing": "PACING_MODE_SMOOTH",
        "operation_status": operation_status,
    }
    if str(template.bid_type).upper() == "BID_TYPE_CUSTOM" and template.conversion_bid_price is not None:
        body["conversion_bid_price"] = float(template.conversion_bid_price)
    try:
        assert_website_ads_execution_lock(db, required=True)
        response = await api.create_adgroup(body)
        remote_id = _first_id(_data(response), "adgroup_id", "adgroup_ids")
        if not remote_id:
            raise RuntimeError("TikTok did not return adgroup_id for automatic creative expansion")
    except WebsiteAdsExecutionLockLost:
        db.rollback()
        raise
    except Exception:
        if donor is not None and allocation["donor_budget_before"] is not None:
            try:
                assert_website_ads_execution_lock(db, required=True)
                await api.update_adgroup_budget(
                    str(campaign.advertiser_id),
                    str(donor.adgroup_id),
                    float(allocation["donor_budget_before"]),
                )
                donor.budget = allocation["donor_budget_before"]
                db.add(donor)
                assert_website_ads_execution_lock(db, required=True)
                db.commit()
            except WebsiteAdsExecutionLockLost:
                raise
            except Exception:
                logger.exception("Failed to restore donor ad-group budget after clone failure")
        raise

    clone = WebsiteAdsAdGroup(
        campaign_local_id=int(campaign.id),
        adgroup_id=remote_id,
        name=clone_name,
        pixel_id=str(template.pixel_id),
        targeting_json=targeting,
        budget_mode="BUDGET_MODE_DAY",
        budget=budget,
        bid_type=str(template.bid_type),
        conversion_bid_price=template.conversion_bid_price,
        schedule_start_time=schedule_text,
        operation_status=operation_status,
        raw_json=response,
    )
    db.add(clone)
    assert_website_ads_execution_lock(db, required=True)
    db.commit()
    db.refresh(clone)
    return clone, {
        **clone_plan["public"],
        "reason": "CLONED_PRECISE_GROUP_WITHIN_PLAN_BUDGET",
        "donor_budget_response": donor_response,
        "remote_response": response,
    }


async def _create_ad_for_asset(
    db: Session,
    *,
    api: TikTokWebsiteAdsClient,
    plan: WebsiteAdsMediaPlan,
    campaign: WebsiteAdsCampaign,
    product: WebsiteAdsLandingPage,
    group: WebsiteAdsAdGroup,
    asset: WebsiteAdsCreativeAsset,
    expansion_mode: str,
    score: float,
    require_execution_lease: bool = False,
) -> tuple[WebsiteAdsAd, dict[str, Any]]:
    def _assert_execution() -> None:
        assert_website_ads_execution_lock(
            db,
            required=require_execution_lease,
        )

    _assert_execution()
    analysis = dict(asset.hermes_analysis_json or {})
    policy = assess_website_ads_creative_policy(analysis)
    if str(asset.analysis_status or "").upper() != "READY" or not policy["eligible_for_automatic_launch"]:
        raise RuntimeError("Creative does not have usable analysis for automatic launch")

    tracking_url, utm_params = build_tracking_url(product.landing_url)
    opening_hook = str(analysis.get("opening_hook") or asset.title or product.title).strip()
    ad_text = opening_hook[:100] or str(product.title)[:100]
    rationale = "[HERMES_AUTO_EXPANSION] Analyzed creative matched to an existing product and audience experiment."
    call_to_action = normalize_website_sales_call_to_action(
        analysis.get("call_to_action"), rationale=rationale
    )
    desired_enable = _campaign_enabled(campaign) and str(group.operation_status or "").upper() == "ENABLE"
    max_unprofitable_spend = max(_decimal(group.budget) * Decimal("0.25"), Decimal("5"))
    guard_config = {
        "media_plan_id": int(plan.id),
        "auto_expansion_asset_id": int(asset.id),
        "expansion_mode": expansion_mode,
        "asset_score": float(score),
        "targeting": dict(group.targeting_json or {}),
        "optimization_event": WEBSITE_ADS_OPTIMIZATION_EVENT,
        "guard_strategy": "CREATIVE_QUALITY_V2",
        "min_ctr": 0.04,
        "max_cpc": 0.30,
        "min_impressions_before_action": 100,
        "min_clicks_for_cpc": 3,
        "min_spend_before_action": 0.90,
        "min_video_2s_rate": 0.20,
        "min_video_6s_rate": 0.06,
        "min_video_impressions_before_action": 150,
        "min_video_spend_before_action": 0.75,
        "qualified_click_override_ctr": 0.04,
        "qualified_click_override_cpc": 0.30,
        "hermes_review_required": True,
    }
    ad_name = f"Hermes auto | asset {asset.id} | {str(asset.title or product.title)[:350]} | {_utcnow():%Y%m%d%H%M}"

    existing_ad = db.scalar(
        select(WebsiteAdsAd)
        .where(
            WebsiteAdsAd.adgroup_local_id == int(group.id),
            WebsiteAdsAd.video_id == str(asset.video_id),
            WebsiteAdsAd.ad_id.is_not(None),
            or_(WebsiteAdsAd.operation_status.is_(None), WebsiteAdsAd.operation_status != "DELETE"),
        )
        .order_by(WebsiteAdsAd.id.desc())
        .limit(1)
    )
    if existing_ad is not None:
        expected_status = "ENABLE" if desired_enable else "DISABLE"
        if str(existing_ad.operation_status or "").upper() != expected_status:
            _assert_execution()
            await api.update_ad_status(
                str(campaign.advertiser_id), [str(existing_ad.ad_id)], expected_status
            )
            existing_ad.operation_status = expected_status
        existing_ad.guard_enabled = True
        existing_ad.max_unprofitable_spend = max_unprofitable_spend
        existing_ad.guard_config_json = guard_config
        existing_ad.landing_page_url = tracking_url
        db.add(existing_ad)
        _assert_execution()
        db.commit()
        return existing_ad, {
            "reused": True,
            "ad_id": str(existing_ad.ad_id),
            "operation_status": expected_status,
        }

    context = dict(plan.execution_context_json or {})
    spark_delivery = _spark_asset_delivery(asset)
    if spark_delivery:
        identity_type = spark_delivery["identity_type"]
        identity_id = spark_delivery["identity_id"]
    else:
        identities = await api.list_all_identities(str(campaign.advertiser_id))
        resolved = select_tiktok_video_identity(
            identities,
            preferred_identity_id=str(context.get("identity_id") or "") or None,
        )
        identity_type = resolved["identity_type"]
        identity_id = resolved["identity_id"]
        context.update(resolved)
        plan.execution_context_json = context
        db.add(plan)
        _assert_execution()
        db.commit()

    image_ids: list[str] = []
    if spark_delivery is None:
        cover_response = await api.suggest_video_covers(str(campaign.advertiser_id), str(asset.video_id))
        covers = _data(cover_response).get("list")
        image_ids = [str(covers[0].get("id"))] if isinstance(covers, list) and covers and covers[0].get("id") else []
        if not image_ids:
            raise RuntimeError("TikTok did not return a usable video cover")

    local_ad = db.scalar(
        select(WebsiteAdsAd)
        .where(
            WebsiteAdsAd.adgroup_local_id == int(group.id),
            WebsiteAdsAd.video_id == str(asset.video_id),
            WebsiteAdsAd.ad_id.is_(None),
        )
        .order_by(WebsiteAdsAd.id.desc())
        .limit(1)
    )
    if local_ad is None:
        local_ad = WebsiteAdsAd(
            campaign_local_id=int(campaign.id),
            adgroup_local_id=int(group.id),
            name=ad_name,
            video_id=str(asset.video_id),
            identity_type=identity_type,
            identity_id=identity_id,
            landing_page_url=tracking_url,
            operation_status="CREATING",
            guard_enabled=True,
            max_unprofitable_spend=max_unprofitable_spend,
            guard_config_json=guard_config,
        )
    else:
        local_ad.name = ad_name
        local_ad.identity_type = identity_type
        local_ad.identity_id = identity_id
        local_ad.landing_page_url = tracking_url
        local_ad.operation_status = "CREATING"
        local_ad.guard_config_json = guard_config
        local_ad.max_unprofitable_spend = max_unprofitable_spend
    db.add(local_ad)
    _assert_execution()
    db.commit()
    db.refresh(local_ad)

    creative: dict[str, Any] = {
        "ad_name": ad_name,
        "identity_type": identity_type,
        "identity_id": identity_id,
        "ad_format": "SINGLE_VIDEO",
        "call_to_action": call_to_action,
        "landing_page_url": tracking_url,
        "utm_params": utm_params,
        "operation_status": "DISABLE",
    }
    if spark_delivery:
        creative["tiktok_item_id"] = spark_delivery["tiktok_item_id"]
        if spark_delivery.get("identity_authorized_bc_id"):
            creative["identity_authorized_bc_id"] = spark_delivery["identity_authorized_bc_id"]
    else:
        creative["video_id"] = str(asset.video_id)
        creative["ad_text"] = ad_text
        creative["image_ids"] = image_ids
        if context.get("identity_authorized_bc_id"):
            creative["identity_authorized_bc_id"] = context["identity_authorized_bc_id"]

    try:
        _assert_execution()
        response = await api.create_ads(
            {
                "advertiser_id": str(campaign.advertiser_id),
                "adgroup_id": str(group.adgroup_id),
                "creatives": [creative],
            }
        )
        local_ad.ad_id = _first_id(_data(response), "ad_id", "ad_ids")
        local_ad.ad_id_v2 = _first_id(_data(response), "ad_id_v2", "ad_ids_v2") or local_ad.ad_id
        if not local_ad.ad_id:
            raise RuntimeError("TikTok did not return ad_id for automatic creative expansion")
        local_ad.raw_json = response
        local_ad.operation_status = "DISABLE"
        db.add(local_ad)
        _assert_execution()
        db.commit()
        if desired_enable:
            _assert_execution()
            await api.update_ad_status(
                str(campaign.advertiser_id), [str(local_ad.ad_id)], "ENABLE"
            )
            local_ad.operation_status = "ENABLE"
            db.add(local_ad)
            _assert_execution()
            db.commit()
        return local_ad, response
    except WebsiteAdsExecutionLockLost:
        db.rollback()
        raise
    except Exception as exc:
        local_ad.operation_status = "ERROR"
        local_ad.raw_json = {"error": f"{type(exc).__name__}: {exc}"[:2000]}
        db.add(local_ad)
        _assert_execution()
        db.commit()
        raise


async def _expand_asset(db: Session, asset: WebsiteAdsCreativeAsset) -> dict[str, Any]:
    assert_website_ads_execution_lock(db, required=True)
    policy = assess_website_ads_creative_policy(asset.hermes_analysis_json)
    if str(asset.analysis_status or "").upper() != "READY":
        _set_state(db, asset, "WAITING_ANALYSIS", retry_minutes=15)
        return {"status": "WAITING_ANALYSIS"}
    if not policy["eligible_for_automatic_launch"]:
        decision = {"policy": policy}
        _set_state(
            db,
            asset,
            "WAITING_ANALYSIS",
            decision=decision,
            error="; ".join(policy["flags"][:4]),
            retry_minutes=15,
        )
        return {"status": "WAITING_ANALYSIS", **decision}
    if not asset.landing_page_id:
        _set_state(db, asset, "WAITING_PRODUCT", retry_minutes=30)
        return {"status": "WAITING_PRODUCT"}

    managed = _eligible_plan(db, asset)
    if managed is None:
        _set_state(db, asset, "WAITING_CAMPAIGN", retry_minutes=30)
        return {"status": "WAITING_CAMPAIGN"}
    plan, campaign, product = managed
    all_groups = list(
        db.scalars(
            select(WebsiteAdsAdGroup)
            .where(
                WebsiteAdsAdGroup.campaign_local_id == int(campaign.id),
                WebsiteAdsAdGroup.adgroup_id.is_not(None),
                or_(WebsiteAdsAdGroup.operation_status.is_(None), WebsiteAdsAdGroup.operation_status != "DELETE"),
            )
            .order_by(WebsiteAdsAdGroup.id)
        ).all()
    )
    groups = all_groups
    campaign_is_enabled = _campaign_enabled(campaign)
    if campaign_is_enabled:
        groups = [group for group in groups if str(group.operation_status or "").upper() == "ENABLE"]
    if not groups:
        _set_state(db, asset, "WAITING_CAMPAIGN", retry_minutes=30)
        return {"status": "WAITING_CAMPAIGN", "reason": "NO_USABLE_ADGROUP"}

    existing_group_ids = _existing_group_ids(
        db,
        campaign_id=int(campaign.id),
        video_id=str(asset.video_id),
        require_enabled=campaign_is_enabled,
    )
    target_count = max(
        1,
        min(int(settings.WEBSITE_ADS_ASSET_EXPANSION_TARGET_GROUPS), len(groups)),
    )
    if len(existing_group_ids) >= target_count:
        decision = {
            "status": "DEPLOYED",
            "campaign_local_id": int(campaign.id),
            "adgroup_local_ids": sorted(existing_group_ids),
            "delivery_enabled": campaign_is_enabled,
        }
        _set_state(db, asset, "DEPLOYED", decision=decision, launched=True)
        return decision

    runtimes = [_group_runtime(db, group, campaign_enabled=campaign_is_enabled) for group in groups]
    candidates = [
        runtime
        for runtime in runtimes
        if runtime["capacity"] > 0 and int(runtime["group"].id) not in existing_group_ids
    ]
    candidates.sort(
        key=lambda item: (item["quality_score"], item["capacity"], -int(item["group"].id)),
        reverse=True,
    )
    needed = target_count - len(existing_group_ids)
    selected = candidates[:needed]
    clone_details: dict[str, Any] | None = None
    expansion_mode = "ADD_TO_EXISTING"
    clone_preview = None
    if not selected and bool(settings.WEBSITE_ADS_ASSET_EXPANSION_ALLOW_CLONE):
        clone_plan = _clone_group_plan(plan=plan, groups=all_groups, runtimes=runtimes)
        clone_preview = clone_plan["public"] if clone_plan is not None else None
        if clone_preview is not None:
            expansion_mode = "CLONE_ADGROUP"
    capacity_evidence = [
        {
            "adgroup_local_id": int(runtime["group"].id),
            "capacity": runtime["capacity"],
            "total_ads": runtime["total_ads"],
            "enabled_ads": runtime["enabled_ads"],
        }
        for runtime in runtimes
    ]
    if not selected and clone_preview is None:
        decision = {
            "status": "WAITING_CAPACITY",
            "campaign_local_id": int(campaign.id),
            "existing_group_ids": sorted(existing_group_ids),
            "group_capacity": capacity_evidence,
            "clone": None,
        }
        _set_state(db, asset, "WAITING_CAPACITY", decision=decision, retry_minutes=15)
        return decision

    analysis = dict(asset.hermes_analysis_json or {})
    performance = _asset_performance(db, asset)
    origin = _production_origin(asset, analysis)
    score = _creative_selection_score(
        performance,
        product_confidence=_asset_product_confidence(asset, int(product.id)),
        analysis_status=str(asset.analysis_status or ""),
        production_origin=origin,
        reference_price=float(product.reference_price or 0),
    )
    proposed = {
        "action": expansion_mode,
        "campaign_local_id": int(campaign.id),
        "target_group_ids": [int(item["group"].id) for item in selected],
        "delivery_enabled": campaign_is_enabled,
        "asset_score": score,
        "product_match_confidence": _asset_product_confidence(asset, int(product.id)),
        "policy": policy,
        "group_evidence": [
            {key: value for key, value in item.items() if key != "group"}
            for item in selected
        ],
        "clone_plan": clone_preview,
    }
    review = await review_website_asset_expansion_action(
        asset=asset,
        product=product,
        proposed_action=proposed,
    )
    assert_website_ads_execution_lock(db, required=True)
    if review["decision"] == "HOLD":
        decision = {**proposed, "hermes_review": review}
        _set_state(db, asset, "WAITING_HERMES", decision=decision, retry_minutes=60)
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(asset.workspace_id),
                auth_id=int(asset.auth_id),
                actor_type="HERMES_ASSET_EXPANSION",
                action="AUTO_HOLD_CREATIVE",
                reason=review["reason"],
                result="SKIPPED",
                request_json={"asset_id": int(asset.id), "proposal": proposed},
                response_json={"hermes_review": review},
            )
        )
        assert_website_ads_execution_lock(db, required=True)
        db.commit()
        return {"status": "WAITING_HERMES", "hermes_review": review}

    api = TikTokWebsiteAdsClient(build_ttb_client(db, int(asset.auth_id)))
    try:
        if expansion_mode == "CLONE_ADGROUP":
            clone, clone_details = await _clone_group_within_budget(
                db,
                api=api,
                plan=plan,
                campaign=campaign,
                groups=all_groups,
                runtimes=runtimes,
                asset=asset,
            )
            if clone is None:
                decision = {
                    **proposed,
                    "status": "WAITING_CAPACITY",
                    "clone": clone_details,
                }
                _set_state(db, asset, "WAITING_CAPACITY", decision=decision, retry_minutes=15)
                return decision
            selected = [_group_runtime(db, clone, campaign_enabled=campaign_is_enabled)]

        created: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for runtime in selected:
            group = runtime["group"]
            try:
                ad, response = await _create_ad_for_asset(
                    db,
                    api=api,
                    plan=plan,
                    campaign=campaign,
                    product=product,
                    group=group,
                    asset=asset,
                    expansion_mode=expansion_mode,
                    score=score,
                    require_execution_lease=True,
                )
                created.append(
                    {
                        "ad_local_id": int(ad.id),
                        "ad_id": ad.ad_id,
                        "adgroup_local_id": int(group.id),
                        "adgroup_id": group.adgroup_id,
                        "response": response,
                    }
                )
            except WebsiteAdsExecutionLockLost:
                raise
            except Exception as exc:
                failures.append(
                    {
                        "adgroup_local_id": int(group.id),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )

        deployed_group_ids = _existing_group_ids(
            db,
            campaign_id=int(campaign.id),
            video_id=str(asset.video_id),
            require_enabled=campaign_is_enabled,
        )
        complete = len(deployed_group_ids) >= target_count
        status = "DEPLOYED" if complete else ("PARTIAL" if created else "RETRY")
        decision = {
            **proposed,
            "status": status,
            "hermes_review": review,
            "created": created,
            "failures": failures,
            "deployed_group_ids": sorted(deployed_group_ids),
            "clone": clone_details,
        }
        _set_state(
            db,
            asset,
            status,
            decision=decision,
            error="; ".join(item["error"] for item in failures) or None,
            retry_minutes=None if complete else 15,
            launched=bool(deployed_group_ids),
        )
        db.add(
            WebsiteAdsActionLog(
                workspace_id=int(asset.workspace_id),
                auth_id=int(asset.auth_id),
                ad_local_id=int(created[0]["ad_local_id"]) if created else None,
                actor_type="HERMES_ASSET_EXPANSION",
                action="AUTO_CLONE_ADGROUP" if expansion_mode == "CLONE_ADGROUP" else "AUTO_ADD_CREATIVE",
                reason=review["reason"],
                result="SUCCESS" if created else "FAILED",
                request_json={"asset_id": int(asset.id), "proposal": proposed},
                response_json={"created": created, "failures": failures, "clone": clone_details},
                metrics_json=_json_safe({"asset_performance": performance, "asset_score": score}),
            )
        )
        assert_website_ads_execution_lock(db, required=True)
        db.commit()
        return decision
    finally:
        await api.aclose()


async def _run_website_ads_asset_expansion_cycle_unlocked(
    db: Session,
    *,
    workspace_id: int | None = None,
) -> dict[str, Any]:
    assert_website_ads_execution_lock(db, required=True)
    if not bool(settings.WEBSITE_ADS_ASSET_EXPANSION_ENABLED):
        return {"enabled": False, "scanned": 0, "launched": 0, "errors": []}

    now = _utcnow()
    stale_before = now - timedelta(minutes=30)
    max_launches = max(1, int(settings.WEBSITE_ADS_ASSET_EXPANSION_MAX_ASSETS_PER_CYCLE))
    max_scans = max(12, max_launches * 12)
    query = select(WebsiteAdsCreativeAsset).where(
        WebsiteAdsCreativeAsset.is_active.is_(True),
        WebsiteAdsCreativeAsset.analysis_status == "READY",
        WebsiteAdsCreativeAsset.landing_page_id.is_not(None),
        or_(
            WebsiteAdsCreativeAsset.auto_launch_status.in_(tuple(CLAIMABLE_STATUSES)),
            (
                (WebsiteAdsCreativeAsset.auto_launch_status == "DEPLOYING")
                & (WebsiteAdsCreativeAsset.updated_at <= stale_before)
            ),
        ),
        or_(
            WebsiteAdsCreativeAsset.auto_launch_next_retry_at.is_(None),
            WebsiteAdsCreativeAsset.auto_launch_next_retry_at <= now,
        ),
    )
    if workspace_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.workspace_id == int(workspace_id))
    assets = list(
        db.scalars(
            query.order_by(
                WebsiteAdsCreativeAsset.auto_launch_next_retry_at.asc(),
                WebsiteAdsCreativeAsset.updated_at.asc(),
                WebsiteAdsCreativeAsset.id.asc(),
            ).limit(max(100, max_scans))
        ).all()
    )
    managed_plan_keys = {
        (int(workspace), int(auth), str(advertiser), int(landing_page))
        for workspace, auth, advertiser, landing_page in db.execute(
            select(
                WebsiteAdsMediaPlan.workspace_id,
                WebsiteAdsMediaPlan.auth_id,
                WebsiteAdsMediaPlan.advertiser_id,
                WebsiteAdsMediaPlan.landing_page_id,
            )
            .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsMediaPlan.campaign_local_id)
            .join(WebsiteAdsLandingPage, WebsiteAdsLandingPage.id == WebsiteAdsMediaPlan.landing_page_id)
            .where(
                WebsiteAdsMediaPlan.status == "ACTIVE",
                WebsiteAdsCampaign.campaign_id.is_not(None),
                WebsiteAdsCampaign.local_status.in_(("ACTIVE", "PAUSED")),
                or_(WebsiteAdsCampaign.operation_status.is_(None), WebsiteAdsCampaign.operation_status != "DELETE"),
                WebsiteAdsLandingPage.is_active.is_(True),
            )
        ).all()
    }
    ranked_assets = [(asset, _asset_rank(db, asset)) for asset in assets]
    ranked_assets.sort(
        key=lambda item: _asset_queue_key(
            item[0],
            has_managed_plan=(
                int(item[0].workspace_id),
                int(item[0].auth_id),
                str(item[0].advertiser_id),
                int(item[0].landing_page_id),
            ) in managed_plan_keys,
            rank=item[1],
        )
    )
    assets = [item[0] for item in ranked_assets]

    result: dict[str, Any] = {
        "enabled": True,
        "candidates": len(assets),
        "scanned": 0,
        "launched": 0,
        "waiting": 0,
        "blocked": 0,
        "errors": [],
        "assets": [],
    }
    for asset in assets[:max_scans]:
        assert_website_ads_execution_lock(db, required=True)
        if result["launched"] >= max_launches:
            break
        if not _claim_asset(db, asset):
            continue
        asset = db.get(WebsiteAdsCreativeAsset, int(asset.id))
        if asset is None:
            continue
        result["scanned"] += 1
        try:
            outcome = await _expand_asset(db, asset)
            assert_website_ads_execution_lock(db, required=True)
            status = str(outcome.get("status") or "")
            if status in {"DEPLOYED", "PARTIAL"} and outcome.get("created"):
                result["launched"] += 1
            elif status == "BLOCKED":
                result["blocked"] += 1
            else:
                result["waiting"] += 1
            result["assets"].append({"asset_id": int(asset.id), "status": status})
        except WebsiteAdsExecutionLockLost:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            asset = db.get(WebsiteAdsCreativeAsset, int(asset.id))
            attempts = int(asset.auto_launch_attempts or 1) if asset else 1
            retry_minutes = min(360, 5 * (2 ** min(max(0, attempts - 1), 6)))
            if asset is not None:
                _set_state(
                    db,
                    asset,
                    "RETRY",
                    error=f"{type(exc).__name__}: {exc}",
                    retry_minutes=retry_minutes,
                )
                db.add(
                    WebsiteAdsActionLog(
                        workspace_id=int(asset.workspace_id),
                        auth_id=int(asset.auth_id),
                        actor_type="HERMES_ASSET_EXPANSION",
                        action="AUTO_ADD_CREATIVE",
                        reason=f"{type(exc).__name__}: {exc}"[:1024],
                        result="FAILED",
                        request_json={"asset_id": int(asset.id)},
                    )
                )
                assert_website_ads_execution_lock(db, required=True)
                db.commit()
            result["errors"].append(
                {"asset_id": int(asset.id) if asset else None, "error": str(exc)[:500]}
            )
            logger.exception("Website Ads automatic creative expansion failed")
    return result


def _asset_expansion_lock_result(*, status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "enabled": bool(settings.WEBSITE_ADS_ASSET_EXPANSION_ENABLED),
        "candidates": 0,
        "scanned": 0,
        "launched": 0,
        "waiting": 0,
        "blocked": 0,
        "errors": [],
        "assets": [],
        "decision": "HOLD",
    }


async def run_website_ads_asset_expansion_cycle(
    db: Session,
    *,
    workspace_id: int | None = None,
    _lock_factory=None,
) -> dict[str, Any]:
    """Run expansion under the same global lease as monitor and manual writes."""

    with website_ads_execution_lease(
        db,
        operation="asset_expansion",
        workspace_id=workspace_id,
        lock_factory=_lock_factory,
    ) as lease:
        if lease is None:
            db.rollback()
            return _asset_expansion_lock_result(
                status="SKIPPED",
                reason="EXECUTION_LOCK_UNAVAILABLE",
            )
        # A preceding monitor/manual owner may have changed campaign, group,
        # or ad state while this session waited.
        db.rollback()
        db.expire_all()
        try:
            result = await _run_website_ads_asset_expansion_cycle_unlocked(
                db,
                workspace_id=workspace_id,
            )
            lease.assert_active()
            result["status"] = "COMPLETED"
            return result
        except WebsiteAdsExecutionLockLost:
            db.rollback()
            return _asset_expansion_lock_result(
                status="HOLD",
                reason="EXECUTION_LOCK_LOST",
            )
