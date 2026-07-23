from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBAdvertiser
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsMediaPlanCreative,
    WebsiteAdsMediaPlanGroup,
)
from app.features.tenants.ttb.gmv_max._helpers import resolve_account_binding
from app.features.tenants.ttb.website_ads.scope import resolve_bound_advertiser_id
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
    website_ads_execution_lease,
)
from app.services.website_ads_tiktok_contract import (
    WEBSITE_ADS_OPTIMIZATION_EVENT,
    compensate_created_campaign,
    enforce_website_ads_location_policy,
    enforce_website_ads_placement_policy,
    normalize_website_sales_call_to_action,
    select_tiktok_video_identity,
    website_ads_optimization_fields,
)
from app.services.website_ads_tracking import build_tracking_url


TIKTOK_TARGETING_FIELDS = {
    "location_ids",
    "zipcode_ids",
    "gender",
    "age_groups",
    "languages",
    "interest_category_ids",
    "audience_ids",
    "excluded_audience_ids",
}


def _data(payload: dict) -> dict:
    value = payload.get("data") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _first_id(data: dict, singular: str, plural: str) -> str | None:
    direct = data.get(singular)
    if direct not in (None, ""):
        return str(direct)
    values = data.get(plural)
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return str(first.get(singular) or first.get("id") or "") or None
        return str(first)
    return None


def _spark_asset_delivery(asset: WebsiteAdsCreativeAsset) -> dict[str, str] | None:
    if str(asset.source or "").upper() != "SPARK_AUTHORIZED_POST":
        return None
    raw = asset.raw_json if isinstance(asset.raw_json, dict) else {}
    item_info = raw.get("item_info") if isinstance(raw.get("item_info"), dict) else {}
    user_info = raw.get("user_info") if isinstance(raw.get("user_info"), dict) else {}
    auth_info = raw.get("auth_info") if isinstance(raw.get("auth_info"), dict) else {}
    item_id = str(item_info.get("item_id") or asset.video_id or "").strip()
    identity_id = str(user_info.get("identity_id") or "").strip()
    identity_type = str(user_info.get("identity_type") or "").strip().upper()
    authorization_status = str(auth_info.get("ad_auth_status") or "").strip().upper()
    auth_end_text = str(auth_info.get("auth_end_time") or "").strip()
    if authorization_status != "AUTHORIZED":
        raise RuntimeError(f"Spark authorization is not active for creative {asset.id}")
    if auth_end_text:
        try:
            auth_end_at = datetime.strptime(auth_end_text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise RuntimeError(f"Spark authorization expiry is invalid for creative {asset.id}") from exc
        if auth_end_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            raise RuntimeError(f"Spark authorization has expired for creative {asset.id}")
    if not item_id or not identity_id or identity_type not in {"AUTH_CODE", "TT_USER", "BC_AUTH_TT"}:
        raise RuntimeError(f"Spark identity metadata is incomplete for creative {asset.id}")
    result = {
        "tiktok_item_id": item_id,
        "identity_id": identity_id,
        "identity_type": identity_type,
    }
    authorized_bc_id = str(user_info.get("identity_authorized_bc_id") or "").strip()
    if identity_type == "BC_AUTH_TT":
        if not authorized_bc_id:
            raise RuntimeError(f"Spark Business Center identity is incomplete for creative {asset.id}")
        result["identity_authorized_bc_id"] = authorized_bc_id
    return result


def _schedule_text(db: Session, plan: WebsiteAdsMediaPlan) -> str:
    advertiser = db.scalar(
        select(TTBAdvertiser).where(
            TTBAdvertiser.workspace_id == plan.workspace_id,
            TTBAdvertiser.auth_id == plan.auth_id,
            TTBAdvertiser.advertiser_id == plan.advertiser_id,
        )
    )
    timezone_name = str((advertiser.display_timezone or advertiser.timezone) if advertiser else "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = timezone.utc
    return datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S")


async def _execute_media_plan_unlocked(
    db: Session,
    plan: WebsiteAdsMediaPlan,
    *,
    claimed: bool = False,
) -> dict:
    def _assert_execution() -> None:
        assert_website_ads_execution_lock(db, required=True)

    _assert_execution()
    binding = resolve_account_binding(
        db,
        int(plan.workspace_id),
        "tiktok-business",
        int(plan.auth_id),
    )
    advertiser_id = resolve_bound_advertiser_id(binding.advertiser_id)
    if str(plan.advertiser_id) != advertiser_id:
        raise PermissionError("Media plan is outside the active advertiser binding")

    expected_status = "EXECUTING" if claimed else "READY"
    if plan.status != expected_status:
        if plan.campaign_local_id:
            campaign = db.get(WebsiteAdsCampaign, plan.campaign_local_id)
            return {
                "id": plan.id,
                "status": plan.status,
                "campaign_local_id": plan.campaign_local_id,
                "campaign_id": campaign.campaign_id if campaign else None,
                "idempotent": True,
            }
        raise ValueError(f"Media plan cannot be executed from status {plan.status}")
    if not claimed:
        plan.status = "EXECUTING"
        plan.error_message = None
        db.add(plan)
        _assert_execution()
        db.commit()

    product = db.get(WebsiteAdsLandingPage, plan.landing_page_id)
    if (
        not product
        or int(product.workspace_id) != int(plan.workspace_id)
        or not product.is_active
    ):
        raise ValueError("Product is unavailable")
    groups = list(db.scalars(
        select(WebsiteAdsMediaPlanGroup)
        .where(WebsiteAdsMediaPlanGroup.media_plan_id == plan.id)
        .order_by(WebsiteAdsMediaPlanGroup.sort_order)
    ).all())
    if not groups:
        raise ValueError("Media plan contains no ad groups")
    context = dict(plan.execution_context_json or {})
    context["location_ids"] = enforce_website_ads_location_policy(
        context.get("location_ids") or ["6252001"]
    )
    context["location_policy"] = "US_CONTIGUOUS_48_PLUS_DC"
    pixel_id = str(context.get("pixel_id") or "")
    identity_id = str(context.get("identity_id") or "")
    identity_type = str(context.get("identity_type") or "")
    if not pixel_id or not identity_id or not identity_type:
        raise ValueError("Media plan execution identity is incomplete")

    tracking_url, utm_params = build_tracking_url(product.landing_url)
    schedule_text = _schedule_text(db, plan)
    campaign = WebsiteAdsCampaign(
        workspace_id=plan.workspace_id,
        auth_id=plan.auth_id,
        advertiser_id=plan.advertiser_id,
        landing_page_id=product.id,
        request_key=f"media-plan-{plan.id}",
        name=plan.name,
        local_status="CREATING",
        operation_status="DISABLE",
    )
    db.add(campaign)
    _assert_execution()
    db.flush()
    plan.campaign_local_id = campaign.id
    plan.status = "EXECUTING"
    plan.error_message = None
    db.add(plan)
    _assert_execution()
    db.commit()

    api = TikTokWebsiteAdsClient(build_ttb_client(db, plan.auth_id))
    remote_campaign_response: dict = {}
    created_adgroups: list[WebsiteAdsAdGroup] = []
    created_ads: list[WebsiteAdsAd] = []
    try:
        identity_response = await api.list_all_identities(str(plan.advertiser_id))
        resolved_identity = select_tiktok_video_identity(
            identity_response,
            preferred_identity_id=identity_id,
        )
        identity_id = resolved_identity["identity_id"]
        identity_type = resolved_identity["identity_type"]
        context.update(resolved_identity)
        plan.execution_context_json = context
        db.add(plan)
        _assert_execution()
        db.commit()

        prepared_creatives_by_group: dict[int, list[dict]] = {}
        covers_by_video_id: dict[str, list[str]] = {}
        for group in groups:
            plan_creatives = list(db.scalars(
                select(WebsiteAdsMediaPlanCreative)
                .where(WebsiteAdsMediaPlanCreative.media_plan_group_id == group.id)
                .order_by(WebsiteAdsMediaPlanCreative.sort_order)
            ).all())
            if not plan_creatives:
                raise RuntimeError(f"Media plan group {group.name} contains no creatives")
            prepared_creatives: list[dict] = []
            for planned_creative in plan_creatives:
                asset = db.get(WebsiteAdsCreativeAsset, planned_creative.creative_asset_id)
                if (
                    not asset
                    or int(asset.workspace_id) != int(plan.workspace_id)
                    or int(asset.auth_id) != int(plan.auth_id)
                    or str(asset.advertiser_id) != advertiser_id
                    or not asset.is_active
                ):
                    raise RuntimeError("A planned creative is unavailable")
                policy = assess_website_ads_creative_policy(asset.hermes_analysis_json)
                if str(asset.analysis_status or "").upper() != "READY" or not policy["eligible_for_automatic_launch"]:
                    reasons = "; ".join(policy["flags"][:3]) or policy["readiness"]
                    raise RuntimeError(
                        f"Creative {asset.id} is not approved for automatic launch: {reasons}"
                    )
                video_id = str(asset.video_id or "").strip()
                if not video_id:
                    raise RuntimeError(f"Creative {asset.title} has no TikTok video_id")
                spark_delivery = _spark_asset_delivery(asset)
                call_to_action = normalize_website_sales_call_to_action(
                    planned_creative.call_to_action,
                    rationale=planned_creative.rationale,
                )
                if planned_creative.call_to_action != call_to_action:
                    planned_creative.call_to_action = call_to_action
                    db.add(planned_creative)
                image_ids: list[str] = []
                if spark_delivery is None:
                    image_ids = covers_by_video_id.get(video_id) or []
                if spark_delivery is None and not image_ids:
                    cover_response = await api.suggest_video_covers(plan.advertiser_id, video_id)
                    covers = _data(cover_response).get("list")
                    image_ids = [
                        str(covers[0].get("id"))
                    ] if isinstance(covers, list) and covers and covers[0].get("id") else []
                    if not image_ids:
                        raise RuntimeError(f"TikTok did not return a video cover for {asset.title}")
                    covers_by_video_id[video_id] = image_ids
                prepared_creatives.append({
                    "planned": planned_creative,
                    "asset": asset,
                    "call_to_action": call_to_action,
                    "image_ids": image_ids,
                    "spark_delivery": spark_delivery,
                })
            prepared_creatives_by_group[int(group.id)] = prepared_creatives
        _assert_execution()
        db.commit()

        _assert_execution()
        remote_campaign_response = await api.create_campaign({
            "advertiser_id": plan.advertiser_id,
            "objective_type": "WEB_CONVERSIONS",
            "virtual_objective_type": "SALES",
            "sales_destination": "WEBSITE",
            "campaign_name": plan.name,
            "budget_optimize_on": False,
            "budget_mode": "BUDGET_MODE_INFINITE",
            "operation_status": "DISABLE",
        })
        campaign.campaign_id = _first_id(_data(remote_campaign_response), "campaign_id", "campaign_ids")
        if not campaign.campaign_id:
            raise RuntimeError("TikTok did not return campaign_id")
        campaign.raw_json = remote_campaign_response
        db.add(campaign)
        _assert_execution()
        db.commit()

        for group in groups:
            targeting = dict(group.targeting_json or {})
            targeting["location_ids"] = enforce_website_ads_location_policy(
                targeting.get("location_ids") or context["location_ids"]
            )
            group.targeting_json = dict(targeting)
            db.add(group)
            api_targeting = {
                key: value
                for key, value in targeting.items()
                if key in TIKTOK_TARGETING_FIELDS and value not in (None, [], "")
            }
            placement_type, placements = enforce_website_ads_placement_policy(
                targeting.get("placement_type"),
                targeting.get("placements"),
            )
            local_group = WebsiteAdsAdGroup(
                campaign_local_id=campaign.id,
                name=group.name,
                pixel_id=pixel_id,
                targeting_json=targeting,
                budget_mode="BUDGET_MODE_DAY",
                budget=group.daily_budget,
                bid_type="BID_TYPE_NO_BID" if group.bid_strategy == "LOWEST_COST" else "BID_TYPE_CUSTOM",
                conversion_bid_price=group.conversion_bid_price,
                schedule_start_time=schedule_text,
                operation_status="DISABLE",
            )
            db.add(local_group)
            _assert_execution()
            db.flush()
            adgroup_body = {
                "advertiser_id": plan.advertiser_id,
                "campaign_id": campaign.campaign_id,
                "adgroup_name": group.name,
                "promotion_type": "WEBSITE",
                "pixel_id": pixel_id,
                "placement_type": placement_type,
                **api_targeting,
                "budget_mode": "BUDGET_MODE_DAY",
                "budget": float(group.daily_budget),
                "schedule_type": "SCHEDULE_FROM_NOW",
                "schedule_start_time": schedule_text,
                "bid_type": local_group.bid_type,
                **website_ads_optimization_fields(),
                "pacing": "PACING_MODE_SMOOTH",
                "operation_status": "DISABLE",
            }
            adgroup_body["placements"] = placements
            if group.bid_strategy == "COST_CAP" and group.conversion_bid_price is not None:
                adgroup_body["conversion_bid_price"] = float(group.conversion_bid_price)
            _assert_execution()
            group_response = await api.create_adgroup(adgroup_body)
            local_group.adgroup_id = _first_id(_data(group_response), "adgroup_id", "adgroup_ids")
            if not local_group.adgroup_id:
                raise RuntimeError(f"TikTok did not return adgroup_id for {group.name}")
            local_group.raw_json = group_response
            created_adgroups.append(local_group)
            db.add(local_group)
            _assert_execution()
            db.commit()

            for prepared_creative in prepared_creatives_by_group[int(group.id)]:
                planned_creative = prepared_creative["planned"]
                asset = prepared_creative["asset"]
                spark_delivery = prepared_creative["spark_delivery"]
                creative_identity_type = spark_delivery["identity_type"] if spark_delivery else identity_type
                creative_identity_id = spark_delivery["identity_id"] if spark_delivery else identity_id
                max_unprofitable_spend = max(group.daily_budget * Decimal("0.25"), Decimal("5"))
                guard_config = {
                    "media_plan_id": int(plan.id),
                    "media_plan_group_id": int(group.id),
                    "group_role": group.role,
                    "hypothesis": group.hypothesis,
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
                    "min_runtime_minutes": 0,
                    "pause_minutes": 60,
                    "hermes_review_required": True,
                }
                local_ad = WebsiteAdsAd(
                    campaign_local_id=campaign.id,
                    adgroup_local_id=local_group.id,
                    name=planned_creative.ad_name,
                    video_id=asset.video_id,
                    identity_type=creative_identity_type,
                    identity_id=creative_identity_id,
                    landing_page_url=tracking_url,
                    operation_status="DISABLE",
                    guard_enabled=True,
                    max_unprofitable_spend=max_unprofitable_spend,
                    guard_config_json=guard_config,
                )
                db.add(local_ad)
                _assert_execution()
                db.flush()
                creative = {
                    "ad_name": planned_creative.ad_name,
                    "identity_type": creative_identity_type,
                    "identity_id": creative_identity_id,
                    "ad_format": "SINGLE_VIDEO",
                    "call_to_action": prepared_creative["call_to_action"],
                    "landing_page_url": tracking_url,
                    "utm_params": utm_params,
                    "operation_status": "DISABLE",
                }
                if spark_delivery:
                    creative["tiktok_item_id"] = spark_delivery["tiktok_item_id"]
                    if spark_delivery.get("identity_authorized_bc_id"):
                        creative["identity_authorized_bc_id"] = spark_delivery["identity_authorized_bc_id"]
                else:
                    creative["video_id"] = asset.video_id
                    creative["ad_text"] = planned_creative.ad_text
                    creative["image_ids"] = prepared_creative["image_ids"]
                    if context.get("identity_authorized_bc_id"):
                        creative["identity_authorized_bc_id"] = context["identity_authorized_bc_id"]
                _assert_execution()
                ad_response = await api.create_ads({
                    "advertiser_id": plan.advertiser_id,
                    "adgroup_id": local_group.adgroup_id,
                    "creatives": [creative],
                })
                local_ad.ad_id = _first_id(_data(ad_response), "ad_id", "ad_ids")
                local_ad.ad_id_v2 = _first_id(_data(ad_response), "ad_id_v2", "ad_ids_v2") or local_ad.ad_id
                if not local_ad.ad_id:
                    raise RuntimeError(f"TikTok did not return ad_id for {asset.title}")
                local_ad.raw_json = ad_response
                created_ads.append(local_ad)
                db.add(local_ad)
                _assert_execution()
                db.commit()

        if plan.activate_after_create:
            ad_ids = [str(item.ad_id) for item in created_ads if item.ad_id]
            group_ids = [str(item.adgroup_id) for item in created_adgroups if item.adgroup_id]
            if ad_ids:
                _assert_execution()
                await api.update_ad_status(plan.advertiser_id, ad_ids, "ENABLE")
            if group_ids:
                _assert_execution()
                await api.update_adgroup_status(plan.advertiser_id, group_ids, "ENABLE")
            _assert_execution()
            await api.update_campaign_status(plan.advertiser_id, [str(campaign.campaign_id)], "ENABLE")
            campaign.operation_status = "ENABLE"
            campaign.local_status = "ACTIVE"
            for row in created_adgroups:
                row.operation_status = "ENABLE"
            for row in created_ads:
                row.operation_status = "ENABLE"
            plan.status = "ACTIVE"
        else:
            campaign.local_status = "PAUSED"
            plan.status = "CREATED"
        plan.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add_all([campaign, plan, *created_adgroups, *created_ads])
        db.execute(
            update(WebsiteAdsCreativeAsset)
            .where(
                WebsiteAdsCreativeAsset.workspace_id == int(plan.workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(plan.auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(plan.advertiser_id),
                WebsiteAdsCreativeAsset.landing_page_id == int(plan.landing_page_id),
                WebsiteAdsCreativeAsset.auto_launch_status.in_(("WAITING_CAMPAIGN", "WAITING_CAPACITY")),
            )
            .values(
                auto_launch_status="PENDING",
                auto_launch_next_retry_at=None,
                auto_launch_error=None,
            )
        )
        db.add(WebsiteAdsActionLog(
            workspace_id=plan.workspace_id,
            auth_id=plan.auth_id,
            actor_type="HERMES_PLANNER",
            action="EXECUTE_MEDIA_PLAN",
            reason=plan.strategy_summary,
            result="SUCCESS",
            request_json={"media_plan_id": int(plan.id), "strategy_source": plan.strategy_source},
            response_json={
                "campaign_id": campaign.campaign_id,
                "adgroup_ids": [row.adgroup_id for row in created_adgroups],
                "ad_ids": [row.ad_id for row in created_ads],
            },
        ))
        _assert_execution()
        db.commit()
        return {
            "id": plan.id,
            "status": plan.status,
            "campaign_local_id": campaign.id,
            "campaign_id": campaign.campaign_id,
            "adgroup_count": len(created_adgroups),
            "ad_count": len(created_ads),
        }
    except WebsiteAdsExecutionLockLost:
        db.rollback()
        raise
    except Exception as exc:
        compensation: dict = {}
        if campaign.campaign_id:
            _assert_execution()
            compensation = await compensate_created_campaign(
                api,
                advertiser_id=plan.advertiser_id,
                campaign_id=campaign.campaign_id,
                before_mutation=_assert_execution,
            )
        compensated_status = str(compensation.get("operation_status") or "")
        plan.status = "FAILED"
        plan.error_message = str(exc)[:4000]
        campaign.local_status = "DELETED" if compensated_status == "DELETE" else "FAILED"
        campaign.operation_status = compensated_status or "DISABLE"
        campaign.error_message = str(exc)[:4000]
        local_groups = list(db.scalars(
            select(WebsiteAdsAdGroup).where(WebsiteAdsAdGroup.campaign_local_id == campaign.id)
        ).all())
        local_ads = list(db.scalars(
            select(WebsiteAdsAd).where(WebsiteAdsAd.campaign_local_id == campaign.id)
        ).all())
        if compensated_status == "DELETE":
            for row in local_groups:
                row.operation_status = "DELETE"
            for row in local_ads:
                row.operation_status = "DELETE"
        db.add_all([plan, campaign, *local_groups, *local_ads])
        db.add(WebsiteAdsActionLog(
            workspace_id=plan.workspace_id,
            auth_id=plan.auth_id,
            actor_type="HERMES_PLANNER",
            action="EXECUTE_MEDIA_PLAN",
            reason=str(exc)[:1024],
            result="FAILED",
            request_json={"media_plan_id": int(plan.id)},
            response_json={"campaign": remote_campaign_response, "compensation": compensation},
        ))
        _assert_execution()
        db.commit()
        raise
    finally:
        await api.aclose()


def _media_plan_lock_result(
    *,
    plan_id: int,
    status: str,
    reason: str,
) -> dict:
    return {
        "id": int(plan_id),
        "status": status,
        "reason": reason,
        "decision": "HOLD",
        "campaign_local_id": None,
        "campaign_id": None,
        "adgroup_count": 0,
        "ad_count": 0,
    }


async def execute_media_plan(
    db: Session,
    plan: WebsiteAdsMediaPlan,
    *,
    claimed: bool = False,
    _lock_factory=None,
) -> dict:
    """Execute a plan under the global Website Ads official-mutation lease."""

    plan_id = int(plan.id)
    workspace_id = int(plan.workspace_id)
    with website_ads_execution_lease(
        db,
        operation="execute_media_plan",
        workspace_id=workspace_id,
        lock_factory=_lock_factory,
    ) as lease:
        if lease is None:
            db.rollback()
            return _media_plan_lock_result(
                plan_id=plan_id,
                status="SKIPPED",
                reason="EXECUTION_LOCK_UNAVAILABLE",
            )
        db.rollback()
        db.expire_all()
        fresh_plan = db.get(WebsiteAdsMediaPlan, plan_id)
        if fresh_plan is None:
            raise RuntimeError("Media plan no longer exists")
        try:
            result = await _execute_media_plan_unlocked(
                db,
                fresh_plan,
                claimed=claimed,
            )
            lease.assert_active()
            return result
        except WebsiteAdsExecutionLockLost:
            db.rollback()
            return _media_plan_lock_result(
                plan_id=plan_id,
                status="HOLD",
                reason="EXECUTION_LOCK_LOST",
            )
