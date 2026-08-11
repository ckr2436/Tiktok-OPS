from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsLandingPage,
)
from app.data.models.ttb_entities import TTBAdvertiser
from app.features.tenants.ttb.gmv_max._helpers import resolve_account_binding
from app.features.tenants.ttb.website_ads.schemas import WebsiteAdLaunchRequest
from app.features.tenants.ttb.website_ads.scope import resolve_bound_advertiser_id
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
)
from app.services.website_ads_tiktok_contract import (
    WEBSITE_ADS_OPTIMIZATION_EVENT,
    compensate_created_campaign,
    enforce_website_ads_location_policy,
    enforce_website_ads_placement_policy,
    normalize_tiktok_call_to_action,
    select_website_ads_pixel,
    select_tiktok_video_identity,
    website_ads_optimization_fields,
)
from app.services.website_ads_tracking import build_tracking_url

def _data(payload: dict) -> dict:
    value = payload.get("data")
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


def _advertiser_video_ids(payload: object) -> set[str]:
    data = (
        payload.get("data")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict)
        else payload
    )
    if isinstance(data, dict):
        candidates = data.get("list") or data.get("videos") or data.get("video_list") or []
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []
    video_ids: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        video = raw.get("video_info") if isinstance(raw.get("video_info"), dict) else raw
        video_id = str(video.get("video_id") or video.get("id") or "").strip()
        if video_id:
            video_ids.add(video_id)
    return video_ids


def _validate_advertiser_identity(
    payload: object,
    *,
    identity_id: str,
    identity_type: str,
    identity_authorized_bc_id: str | None,
) -> dict:
    selected = select_tiktok_video_identity(
        payload,
        preferred_identity_id=str(identity_id),
    )
    if (
        selected["identity_id"] != str(identity_id)
        or selected["identity_type"] != str(identity_type).upper()
    ):
        raise ValueError("The selected identity is unavailable for the bound advertiser")
    selected_bc_id = str(selected.get("identity_authorized_bc_id") or "")
    requested_bc_id = str(identity_authorized_bc_id or "")
    if selected["identity_type"] == "BC_AUTH_TT" and selected_bc_id != requested_bc_id:
        raise ValueError("The selected identity authorization is outside the bound advertiser")
    return selected


async def launch_website_ad(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    provider: str,
    request: WebsiteAdLaunchRequest,
    require_execution_lease: bool = False,
) -> dict:
    def _assert_execution() -> None:
        assert_website_ads_execution_lock(
            db,
            required=require_execution_lease,
        )

    _assert_execution()
    landing = db.get(WebsiteAdsLandingPage, int(request.landing_page_id))
    if not landing or landing.workspace_id != int(workspace_id) or not landing.is_active:
        raise ValueError("Landing page is unavailable")

    binding = resolve_account_binding(db, workspace_id, provider, auth_id)
    advertiser_id = resolve_bound_advertiser_id(
        binding.advertiser_id,
        request.advertiser_id,
    )

    request_key = str(request.request_key or uuid4())
    existing = db.scalar(
        select(WebsiteAdsCampaign).where(
            WebsiteAdsCampaign.workspace_id == int(workspace_id),
            WebsiteAdsCampaign.auth_id == int(auth_id),
            WebsiteAdsCampaign.request_key == request_key,
        )
    )
    if existing:
        if (
            str(existing.advertiser_id) != advertiser_id
            or int(existing.landing_page_id) != int(request.landing_page_id)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "WEBSITE_ADS_REQUEST_KEY_SCOPE_CONFLICT",
                    "message": "request_key is already used by a different Website Ads request.",
                },
            )
        return {
            "id": existing.id,
            "campaign_id": existing.campaign_id,
            "status": existing.local_status,
            "idempotent": True,
        }

    preflight_api = TikTokWebsiteAdsClient(build_ttb_client(db, int(auth_id)))
    try:
        pixel_payload = await preflight_api.list_all_pixels(advertiser_id)
        identity_payload = await preflight_api.list_all_identities(advertiser_id)
        video_payload = await preflight_api.list_all_videos(advertiser_id)
        selected_pixel = select_website_ads_pixel(
            pixel_payload,
            preferred_pixel_id=str(request.pixel_id),
        )
        selected_pixel_id = str(
            selected_pixel.get("pixel_id") or selected_pixel.get("pixel_code") or selected_pixel.get("id") or ""
        )
        if selected_pixel_id != str(request.pixel_id):
            raise ValueError("The selected Pixel does not support View Content optimization")
        _validate_advertiser_identity(
            identity_payload,
            identity_id=str(request.identity_id),
            identity_type=str(request.identity_type),
            identity_authorized_bc_id=request.identity_authorized_bc_id,
        )
        if str(request.video_id) not in _advertiser_video_ids(video_payload):
            raise ValueError("The selected video is unavailable for the bound advertiser")
    finally:
        await preflight_api.aclose()
    _assert_execution()

    advertiser = db.scalar(
        select(TTBAdvertiser).where(
            TTBAdvertiser.workspace_id == int(workspace_id),
            TTBAdvertiser.auth_id == int(auth_id),
            TTBAdvertiser.advertiser_id == advertiser_id,
        )
    )
    timezone_name = str((advertiser.display_timezone or advertiser.timezone) if advertiser else "UTC")
    try:
        advertiser_zone = ZoneInfo(timezone_name)
    except Exception:
        advertiser_zone = timezone.utc
    schedule_start = request.schedule_start_time or datetime.now(advertiser_zone)
    if schedule_start.tzinfo is None:
        schedule_start = schedule_start.replace(tzinfo=advertiser_zone)
    schedule_text = schedule_start.astimezone(advertiser_zone).strftime("%Y-%m-%d %H:%M:%S")
    tracking_url, utm_params = build_tracking_url(landing.landing_url)
    call_to_action = normalize_tiktok_call_to_action(request.call_to_action)
    guard = request.guard.model_dump()
    guard["optimization_event"] = WEBSITE_ADS_OPTIMIZATION_EVENT
    guard["guard_strategy"] = "CREATIVE_QUALITY_V2"
    guard["target_roas"] = None
    guard["target_roas_source"] = "not_applicable_for_view_content"
    guard["min_runtime_minutes"] = 0
    if guard.get("max_unprofitable_spend") is None:
        guard["max_unprofitable_spend"] = round(max(request.daily_budget * 0.25, 5.0), 2)
        guard["max_unprofitable_spend_source"] = "view_content_budget_guard"
    effective_targeting = request.targeting.model_dump(exclude_none=True)
    effective_targeting["location_ids"] = enforce_website_ads_location_policy(
        effective_targeting.get("location_ids")
    )
    placement_type, placements = enforce_website_ads_placement_policy(
        effective_targeting.get("placement_type"),
        effective_targeting.get("placements"),
    )
    effective_targeting["placement_type"] = placement_type
    effective_targeting["placements"] = placements

    campaign = WebsiteAdsCampaign(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=advertiser_id,
        landing_page_id=landing.id,
        request_key=request_key,
        name=request.campaign_name,
        local_status="CREATING",
        operation_status="DISABLE",
    )
    db.add(campaign)
    _assert_execution()
    db.flush()
    adgroup = WebsiteAdsAdGroup(
        campaign_local_id=campaign.id,
        name=request.adgroup_name,
        pixel_id=request.pixel_id,
        targeting_json=effective_targeting,
        budget_mode=request.budget_mode,
        budget=Decimal(str(request.daily_budget)),
        bid_type="BID_TYPE_NO_BID" if request.bid_strategy == "LOWEST_COST" else "BID_TYPE_CUSTOM",
        conversion_bid_price=Decimal(str(request.conversion_bid_price)) if request.conversion_bid_price else None,
        schedule_start_time=schedule_text,
        operation_status="DISABLE",
    )
    db.add(adgroup)
    _assert_execution()
    db.flush()
    ad = WebsiteAdsAd(
        campaign_local_id=campaign.id,
        adgroup_local_id=adgroup.id,
        name=request.ad_name,
        video_id=request.video_id,
        identity_type=request.identity_type,
        identity_id=request.identity_id,
        landing_page_url=tracking_url,
        operation_status="DISABLE",
        guard_enabled=bool(request.guard.enabled),
        target_roas=None,
        max_unprofitable_spend=Decimal(str(guard["max_unprofitable_spend"])),
        guard_config_json=guard,
    )
    db.add(ad)
    _assert_execution()
    db.commit()

    api = TikTokWebsiteAdsClient(build_ttb_client(db, int(auth_id)))
    campaign_response: dict = {}
    adgroup_response: dict = {}
    ad_response: dict = {}
    try:
        _assert_execution()
        campaign_response = await api.create_campaign(
            {
                "advertiser_id": advertiser_id,
                "objective_type": "WEB_CONVERSIONS",
                "virtual_objective_type": "SALES",
                "sales_destination": "WEBSITE",
                "campaign_name": request.campaign_name,
                "budget_optimize_on": False,
                "budget_mode": "BUDGET_MODE_INFINITE",
                "operation_status": "DISABLE",
            }
        )
        campaign.campaign_id = _first_id(_data(campaign_response), "campaign_id", "campaign_ids")
        if not campaign.campaign_id:
            raise RuntimeError("TikTok did not return campaign_id")
        campaign.raw_json = campaign_response
        db.add(campaign)
        _assert_execution()
        db.commit()

        targeting = dict(effective_targeting)
        placement_type = targeting.pop("placement_type")
        placements = targeting.pop("placements")
        adgroup_body = {
            "advertiser_id": advertiser_id,
            "campaign_id": campaign.campaign_id,
            "adgroup_name": request.adgroup_name,
            "promotion_type": "WEBSITE",
            "pixel_id": request.pixel_id,
            "placement_type": placement_type,
            **{key: value for key, value in targeting.items() if value not in (None, [], "")},
            "budget_mode": request.budget_mode,
            "budget": request.daily_budget,
            "schedule_type": "SCHEDULE_FROM_NOW",
            "schedule_start_time": schedule_text,
            "bid_type": adgroup.bid_type,
            **website_ads_optimization_fields(),
            "pacing": "PACING_MODE_SMOOTH",
            "operation_status": "DISABLE",
        }
        if placement_type == "PLACEMENT_TYPE_NORMAL":
            adgroup_body["placements"] = placements
        if request.bid_strategy == "COST_CAP":
            adgroup_body["conversion_bid_price"] = request.conversion_bid_price
        _assert_execution()
        adgroup_response = await api.create_adgroup(adgroup_body)
        adgroup.adgroup_id = _first_id(_data(adgroup_response), "adgroup_id", "adgroup_ids")
        if not adgroup.adgroup_id:
            raise RuntimeError("TikTok did not return adgroup_id")
        adgroup.raw_json = adgroup_response
        db.add(adgroup)
        _assert_execution()
        db.commit()

        cover_response = await api.suggest_video_covers(advertiser_id, request.video_id)
        cover_data = _data(cover_response)
        covers = cover_data.get("list") if isinstance(cover_data.get("list"), list) else []
        suggested_image_ids = [
            str(item.get("id"))
            for item in covers
            if isinstance(item, dict) and item.get("id")
        ]
        image_ids = list(request.image_ids)
        if image_ids and any(str(image_id) not in suggested_image_ids for image_id in image_ids):
            raise ValueError("The selected video cover is unavailable for the bound advertiser")
        if not image_ids:
            image_ids = suggested_image_ids[:1]
        if not image_ids:
            raise RuntimeError("TikTok did not return a suggested video cover")

        creative = {
            "ad_name": request.ad_name,
            "identity_type": request.identity_type,
            "identity_id": request.identity_id,
            "ad_format": "SINGLE_VIDEO",
            "video_id": request.video_id,
            "ad_text": request.ad_text,
            "call_to_action": call_to_action,
            "landing_page_url": tracking_url,
            "utm_params": utm_params,
            "operation_status": "DISABLE",
            "image_ids": image_ids,
        }
        if request.identity_authorized_bc_id:
            creative["identity_authorized_bc_id"] = request.identity_authorized_bc_id
        _assert_execution()
        ad_response = await api.create_ads(
            {"advertiser_id": advertiser_id, "adgroup_id": adgroup.adgroup_id, "creatives": [creative]}
        )
        ad.ad_id = _first_id(_data(ad_response), "ad_id", "ad_ids")
        ad.ad_id_v2 = _first_id(_data(ad_response), "ad_id_v2", "ad_ids_v2") or ad.ad_id
        if not ad.ad_id:
            raise RuntimeError("TikTok did not return ad_id")
        ad.raw_json = ad_response
        db.add(ad)

        if request.activate_after_create:
            _assert_execution()
            await api.update_ad_status(advertiser_id, [ad.ad_id], "ENABLE")
            _assert_execution()
            await api.update_adgroup_status(advertiser_id, [adgroup.adgroup_id], "ENABLE")
            _assert_execution()
            await api.update_campaign_status(advertiser_id, [campaign.campaign_id], "ENABLE")
            ad.operation_status = "ENABLE"
            adgroup.operation_status = "ENABLE"
            campaign.operation_status = "ENABLE"
            campaign.local_status = "ACTIVE"
        else:
            campaign.local_status = "PAUSED"
        campaign.error_message = None
        db.add_all([campaign, adgroup, ad])
        db.add(
            WebsiteAdsActionLog(
                workspace_id=workspace_id,
                auth_id=auth_id,
                ad_local_id=ad.id,
                actor_type="USER",
                action="CREATE_AND_ENABLE" if request.activate_after_create else "CREATE_DISABLED",
                result="SUCCESS",
                request_json={
                    **request.model_dump(mode="json"),
                    "targeting": effective_targeting,
                },
                response_json={"campaign": campaign_response, "adgroup": adgroup_response, "ad": ad_response},
            )
        )
        _assert_execution()
        db.commit()
        return {
            "id": campaign.id,
            "campaign_id": campaign.campaign_id,
            "adgroup_id": adgroup.adgroup_id,
            "ad_id": ad.ad_id,
            "status": campaign.local_status,
            "landing_page_url": tracking_url,
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
                advertiser_id=advertiser_id,
                campaign_id=campaign.campaign_id,
                before_mutation=_assert_execution,
            )
        compensated_status = str(compensation.get("operation_status") or "")
        campaign.local_status = "DELETED" if compensated_status == "DELETE" else "FAILED"
        campaign.operation_status = compensated_status or "DISABLE"
        campaign.error_message = str(exc)[:4000]
        if compensated_status == "DELETE":
            adgroup.operation_status = "DELETE"
            ad.operation_status = "DELETE"
        db.add_all([campaign, adgroup, ad])
        db.add(
            WebsiteAdsActionLog(
                workspace_id=workspace_id,
                auth_id=auth_id,
                ad_local_id=ad.id,
                actor_type="SYSTEM",
                action="CREATE",
                reason=str(exc)[:1024],
                result="FAILED",
                response_json={
                    "campaign": campaign_response,
                    "adgroup": adgroup_response,
                    "ad": ad_response,
                    "compensation": compensation,
                },
            )
        )
        _assert_execution()
        db.commit()
        raise
    finally:
        await api.aclose()
