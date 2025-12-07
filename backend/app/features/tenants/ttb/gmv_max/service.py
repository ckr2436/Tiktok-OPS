from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
"""Provider-scoped service helpers bridging GMV Max routers to core services."""

from typing import Any, Iterable, Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select, case, func
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import PromotionTypeEnum
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxActionLog,
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
    TTBGmvMaxStrategyConfig,
)
from app.data.repositories.tiktok_business.gmvmax import list_gmvmax_campaigns
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action as svc_apply_campaign_action,
    decide_campaign_action,
    create_gmvmax_campaign as svc_create_campaign,
    get_item_group_ids_for_campaign,
    get_or_create_strategy_config,
    sync_gmvmax_campaigns as svc_sync_campaigns,
    sync_gmvmax_metrics_daily as svc_sync_metrics_daily,
    sync_gmvmax_metrics_hourly as svc_sync_metrics_hourly,
)
from app.services.ttb_api import TTBApiClient, TTBBusinessError
from app.services.ttb_client_factory import build_ttb_client, build_ttb_gmvmax_client
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxStoreListRequest,
    GMVMaxStoreAdUsageCheckRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxVideoGetRequest,
    GMVMaxCustomAnchorVideoListGetRequest,
    GMVMaxResponse,
    TikTokBusinessGMVMaxClient,
)
from .schemas import (
    CreateCampaignRequest,
    GMVMaxPrecheckRequest,
    GMVMaxPrecheckResponse,
    IdentitySummary,
    VideoSummary,
    CustomAnchorVideoSummary,
    OccupiedAdSummary,
)

from ._helpers import (
    ensure_ttb_auth_in_workspace,
    get_advertiser_id_for_account,
    get_gmvmax_client_for_account,
    normalize_provider,
)


def _ensure_provider(provider: str) -> str:
    return normalize_provider(provider)


def _normalize_action(action: str | None) -> str:
    mapping = {
        "START": "START",
        "ENABLE": "START",
        "RESUME": "START",
        "RUN": "START",
        "PAUSE": "PAUSE",
        "STOP": "PAUSE",
        "DISABLE": "PAUSE",
        "SUSPEND": "PAUSE",
        "SET_BUDGET": "SET_BUDGET",
        "UPDATE_BUDGET": "SET_BUDGET",
        "SET_ROAS": "SET_ROAS",
        "UPDATE_ROAS": "SET_ROAS",
        "ADJUST_ROI": "SET_ROAS",
    }
    key = str(action or "").strip().upper()
    return mapping.get(key, key)


def _order_desc_nulls_last(col):
    """
    Vendor-agnostic 等价实现：ORDER BY col DESC NULLS LAST
    在 MySQL/MariaDB 上编译为：
      ORDER BY (col IS NULL) ASC, col DESC
    在支持 NULLS LAST 的方言上也安全。
    """
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


def _ensure_campaign(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
) -> TTBGmvMaxCampaign:
    query = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    if advertiser_id:
        query = query.where(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))

    instance = db.execute(query).scalars().first()
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    return instance


async def _gather_store_entry(
    client: TikTokBusinessGMVMaxClient, *, advertiser_id: str, store_id: str
) -> dict[str, Any] | None:
    request = GMVMaxStoreListRequest(advertiser_id=str(advertiser_id))
    response: GMVMaxResponse[Any] = await client.gmv_max_store_list(request)
    store_list = getattr(response.data, "store_list", []) if response.data else []
    for entry in store_list:
        raw = entry.model_dump(exclude_none=False) if hasattr(entry, "model_dump") else entry
        if isinstance(raw, dict) and str(raw.get("store_id")) == str(store_id):
            return raw
    return None


async def _list_products_for_gmvmax(
    ttb_client: TTBApiClient,
    *,
    advertiser_id: str,
    store_id: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async for product in ttb_client.iter_products(
        store_id=str(store_id),
        advertiser_id=str(advertiser_id),
        eligibility="GMV_MAX",
    ):
        if isinstance(product, dict):
            results.append(product)
    return results


def _resolve_product_specific_type(campaign: TTBGmvMaxCampaign) -> str:
    raw = campaign.raw_json or {}
    if isinstance(raw, dict):
        payload = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        candidate = payload.get("product_specific_type")
        if isinstance(candidate, str) and candidate:
            return candidate.upper()

    item_group_ids = getattr(campaign, "item_group_ids", None)
    if item_group_ids:
        return "CUSTOMIZED_PRODUCTS"

    return "ALL"


async def create_gmvmax_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    payload: CreateCampaignRequest,
    store_authorized_bc_id: str | None = None,
    client: TikTokBusinessGMVMaxClient | None = None,
) -> TTBGmvMaxCampaign:
    """Create a *Product* GMV Max campaign with normalized TikTok payload mapping.

    NOTE:
    - This helper currently only supports Product GMV Max campaigns.
    - Live GMV Max (LIVE shopping) should be handled by a dedicated helper or
      an explicit branch once the payload semantics are defined, to avoid
      overloading the PRODUCT-specific assumptions here.
    """

    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    client_provided = client is not None
    client = client or build_ttb_gmvmax_client(db, auth_id=auth_id)

    try:
        body = payload.to_client_body(store_authorized_bc_id=store_authorized_bc_id)
        row = await svc_create_campaign(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            client=client,
            body=body,
        )
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise
    finally:
        if not client_provided:
            await client.aclose()


async def gmvmax_precheck(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    payload: GMVMaxPrecheckRequest,
    client: TikTokBusinessGMVMaxClient | None = None,
    ttb_client: TTBApiClient | None = None,
) -> GMVMaxPrecheckResponse:
    """Run comprehensive Product GMV Max precheck for a tenant binding."""

    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    client_provided = client is not None
    ttb_client_provided = ttb_client is not None
    client = client or build_ttb_gmvmax_client(db, auth_id=auth_id)
    ttb_client = ttb_client or build_ttb_client(db, auth_id=auth_id)

    try:
        store_entry = await _gather_store_entry(
            client, advertiser_id=advertiser_id, store_id=payload.store_id
        )
        is_gmv_max_available = bool(store_entry.get("is_gmv_max_available")) if store_entry else False
        current_authorized_advertiser_id: str | None = None
        needs_exclusive_auth = False

        if store_entry:
            exclusive_info = store_entry.get("exclusive_authorized_advertiser_info")
            if isinstance(exclusive_info, dict):
                current_authorized_advertiser_id = exclusive_info.get("advertiser_id")
                if current_authorized_advertiser_id and str(current_authorized_advertiser_id) != str(advertiser_id):
                    needs_exclusive_auth = True
            elif store_entry.get("advertiser_id") and str(store_entry.get("advertiser_id")) != str(advertiser_id):
                current_authorized_advertiser_id = str(store_entry.get("advertiser_id"))
                needs_exclusive_auth = True

        if not is_gmv_max_available:
            return GMVMaxPrecheckResponse(
                is_gmv_max_available=False,
                needs_exclusive_auth=False,
                current_authorized_advertiser_id=current_authorized_advertiser_id,
                promote_all_products_allowed=False,
                has_running_custom_shop_ads=False,
                occupied_custom_shop_ads=[],
                unoccupied_item_group_ids=[],
                occupied_item_group_ids=[],
                available_identities=[],
                available_videos=[],
                available_custom_anchor_videos=[],
                recommended_roas_bid=None,
                recommended_budget=None,
                store_usage=None,
                identities=[],
                occupancy=None,
                request_ids={
                    "store_usage": None,
                    "identities": None,
                    "occupancy": None,
                },
            )

        usage_resp = await client.gmv_max_store_shop_ad_usage_check(
            GMVMaxStoreAdUsageCheckRequest(
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                store_authorized_bc_id=str(payload.store_authorized_bc_id),
            )
        )
        store_usage = usage_resp.data
        promote_all_products_allowed = bool(
            getattr(store_usage, "promote_all_products_allowed", False)
        )
        has_running_custom_shop_ads = bool(
            getattr(store_usage, "is_running_custom_shop_ads", False)
        )

        occupied_ads: list[OccupiedAdSummary] = []
        occupancy_resp = None
        occupancy_data = None
        if has_running_custom_shop_ads:
            spu_ids = list(payload.item_group_ids or payload.product_item_group_ids or [])
            occupancy_resp = await client.gmv_max_occupied_custom_shop_ads_list(
                GMVMaxOccupiedCustomShopAdsListRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(payload.store_id),
                    occupied_asset_type="SPU",
                    asset_ids=[str(item) for item in spu_ids] if spu_ids else [],
                )
            )
            occupancy_data = occupancy_resp.data
            if occupancy_data:
                for entry in getattr(occupancy_data, "occupied_custom_shop_ads", []) or []:
                    occupied_ads.append(
                        OccupiedAdSummary(
                            ad_id=getattr(entry, "ad_id", None),
                            campaign_id=getattr(entry, "campaign_id", None),
                            advertiser_id=getattr(entry, "advertiser_id", None),
                            item_group_id=getattr(entry, "item_group_id", None),
                            create_time=getattr(entry, "create_time", None),
                        )
                    )
        products = await _list_products_for_gmvmax(
            ttb_client,
            advertiser_id=advertiser_id,
            store_id=payload.store_id,
        )
        unoccupied_item_group_ids: list[str] = []
        occupied_item_group_ids: list[str] = []
        for product in products:
            if product.get("status") != "AVAILABLE":
                continue
            group_id = str(product.get("item_group_id")) if product.get("item_group_id") else None
            if not group_id:
                continue
            gmv_status = product.get("gmv_max_ads_status")
            if gmv_status == "UNOCCUPIED":
                unoccupied_item_group_ids.append(group_id)
            else:
                occupied_item_group_ids.append(group_id)

        identity_resp = await client.gmv_max_identity_get(
            GMVMaxIdentityGetRequest(
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                store_authorized_bc_id=str(payload.store_authorized_bc_id),
            )
        )
        identity_data = identity_resp.data if identity_resp else None
        identities = []
        for entry in getattr(identity_data, "identity_list", []) or []:
            info = getattr(entry, "identity_info", None)
            summary = IdentitySummary(
                identity_id=getattr(info, "identity_id", None),
                identity_type=getattr(info, "identity_type", None),
                user_name=getattr(info, "user_name", None),
                profile_image=getattr(info, "profile_image", None),
                product_gmv_max_available=getattr(entry, "product_gmv_max_available", None),
            )
            if summary.product_gmv_max_available is False:
                continue
            identities.append(summary)

        videos: list[VideoSummary] = []
        try:
            video_resp = await client.gmv_max_video_get(
                GMVMaxVideoGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(payload.store_id),
                    store_authorized_bc_id=str(payload.store_authorized_bc_id),
                )
            )
            for entry in getattr(video_resp.data, "list", []) or []:
                video_info = getattr(entry, "video_info", None)
                videos.append(
                    VideoSummary(
                        item_id=getattr(entry, "item_id", None),
                        video_id=getattr(video_info, "video_id", None),
                        preview_url=getattr(video_info, "preview_url", None),
                        video_cover_url=getattr(video_info, "video_cover_url", None),
                        duration=getattr(video_info, "duration", None),
                    )
                )
        except Exception:
            videos = []

        custom_anchor_videos: list[CustomAnchorVideoSummary] = []
        try:
            anchor_resp = await client.gmv_max_custom_anchor_video_list_get(
                GMVMaxCustomAnchorVideoListGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(payload.store_id),
                )
            )
            for entry in getattr(anchor_resp.data, "custom_anchor_video_list", []) or []:
                video_info = getattr(entry, "video_info", None)
                custom_anchor_videos.append(
                    CustomAnchorVideoSummary(
                        custom_anchor_video_id=getattr(entry, "custom_anchor_video_id", None),
                        video_id=getattr(video_info, "video_id", None),
                        preview_url=getattr(video_info, "preview_url", None),
                        video_cover_url=getattr(video_info, "video_cover_url", None),
                        duration=getattr(video_info, "duration", None),
                    )
                )
        except Exception:
            custom_anchor_videos = []

        recommended_roas_bid = None
        recommended_budget = None
        try:
            recommend_resp = await ttb_client.recommend_gmvmax_bid(
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                shopping_ads_type="PRODUCT",
                optimization_goal="VALUE",
                item_group_ids=list(payload.item_group_ids or payload.product_item_group_ids or []),
                identity_id=payload.identity_id,
            )
            recommended_roas_bid = recommend_resp.get("roas_bid")
            recommended_budget = recommend_resp.get("budget")
        except Exception:
            recommended_roas_bid = None
            recommended_budget = None

        return GMVMaxPrecheckResponse(
            is_gmv_max_available=is_gmv_max_available,
            needs_exclusive_auth=needs_exclusive_auth,
            current_authorized_advertiser_id=current_authorized_advertiser_id,
            promote_all_products_allowed=promote_all_products_allowed,
            has_running_custom_shop_ads=has_running_custom_shop_ads,
            occupied_custom_shop_ads=occupied_ads,
            unoccupied_item_group_ids=unoccupied_item_group_ids,
            occupied_item_group_ids=occupied_item_group_ids,
            available_identities=identities,
            available_videos=videos,
            available_custom_anchor_videos=custom_anchor_videos,
            recommended_roas_bid=recommended_roas_bid,
            recommended_budget=recommended_budget,
            store_usage=store_usage,
            identities=getattr(identity_data, "identity_list", []) if identity_data else [],
            occupancy=occupancy_data,
            request_ids={
                "store_usage": usage_resp.request_id,
                "identities": getattr(identity_resp, "request_id", None),
                "occupancy": getattr(occupancy_resp, "request_id", None) if occupancy_resp else None,
            },
        )
    finally:
        if not client_provided:
            try:
                await client.aclose()
            except Exception:
                pass
        if not ttb_client_provided:
            try:
                await ttb_client.aclose()
            except Exception:
                pass


async def sync_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    campaign_ids: Optional[Iterable[str]] = None,
) -> int:
    """Sync campaigns for a binding using ``ttb_gmvmax.sync_gmvmax_campaigns`` and TikTok client."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    try:
        result = await svc_sync_campaigns(
            db,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(resolved_advertiser),
            status=status_filter,
            campaign_ids=[str(cid) for cid in campaign_ids] if campaign_ids else None,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()

    return int(result.get("synced", 0))


async def list_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    store_id: Optional[str] = None,
    business_center_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    sync: bool = False,
) -> dict[str, Any]:
    """Return cached GMV Max campaigns with optional pre-sync; relies on repository filters."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    synced: Optional[int] = None
    if sync:
        synced = await sync_campaigns(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=resolved_advertiser,
            status_filter=status_filter,
        )

    if not store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )

    items, total = list_gmvmax_campaigns(
        db,
        workspace_id=workspace_id,
        advertiser_id=str(resolved_advertiser),
        store_id=str(store_id),
        status_filter=status_filter,
        search=search,
        page=page,
        page_size=page_size,
    )

    payload: dict[str, Any] = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if synced is not None:
        payload["synced"] = synced
    return payload


async def get_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
    refresh: bool = False,
) -> TTBGmvMaxCampaign:
    """Load a single campaign, optionally triggering a targeted sync when missing."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    try:
        instance = _ensure_campaign(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
            advertiser_id=resolved_advertiser,
        )
        return instance
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND or not refresh:
            raise

    await sync_campaigns(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=resolved_advertiser,
        campaign_ids=[campaign_id],
    )

    instance = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=resolved_advertiser,
    )
    return instance


async def sync_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str,
    granularity: str,
    start_date: date,
    end_date: date,
) -> int:
    """Sync hourly/daily metrics for a campaign by calling TikTok report API through the client."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
    )

    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    try:
        if granularity.upper() == "HOUR":
            result = await svc_sync_metrics_hourly(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            result = await svc_sync_metrics_daily(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                start_date=start_date,
                end_date=end_date,
            )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()

    return int(result.get("synced_rows", 0))


def _query_metrics_hourly(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsHourly]:
    query = select(TTBGmvMaxMetricsHourly).where(
        TTBGmvMaxMetricsHourly.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour < end)
    query = (
        query.order_by(TTBGmvMaxMetricsHourly.stat_time_hour.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def _query_metrics_daily(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[date],
    end: Optional[date],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsDaily]:
    query = select(TTBGmvMaxMetricsDaily).where(
        TTBGmvMaxMetricsDaily.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day < end)
    query = (
        query.order_by(TTBGmvMaxMetricsDaily.stat_time_day.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def query_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    granularity: str,
    start: Optional[datetime | date],
    end: Optional[datetime | date],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Query stored hourly or daily metrics for a campaign (DB only)."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    gran = granularity.upper()
    if gran == "HOUR":
        rows = _query_metrics_hourly(
            db,
            campaign=campaign,
            start=start if isinstance(start, datetime) else None,
            end=end if isinstance(end, datetime) else None,
            limit=limit,
            offset=offset,
        )
        return {
            "granularity": "HOUR",
            "items": rows,
            "count": len(rows),
        }

    rows = _query_metrics_daily(
        db,
        campaign=campaign,
        start=start if isinstance(start, date) else None,
        end=end if isinstance(end, date) else None,
        limit=limit,
        offset=offset,
    )
    return {
        "granularity": "DAY",
        "items": rows,
        "count": len(rows),
    }


async def apply_campaign_action(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    action: str,
    payload: dict[str, Any],
    reason: Optional[str],
    performed_by: str,
    audit_hook: Any | None = None,
) -> tuple[TTBGmvMaxCampaign, TTBGmvMaxActionLog]:
    """Apply a campaign action via TikTok and persist an action log snapshot."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    normalized_action = _normalize_action(action)
    advertiser_id = get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
    ttb_client: TTBApiClient | None = None
    try:
        promotion_type = getattr(campaign, "promotion_type", None)
        promotion_type_value = str(
            promotion_type.value if hasattr(promotion_type, "value") else promotion_type or ""
        ).upper()
        shopping_ads_type = getattr(campaign, "shopping_ads_type", None)
        is_product_campaign = promotion_type_value == PromotionTypeEnum.PRODUCT.value or str(
            shopping_ads_type or ""
        ).upper() == "PRODUCT"
        # NOTE:
        # - We only enforce occupancy checks for *Product* GMV Max campaigns here.
        # - If/when Live GMV Max is supported, revisit this branching and define
        #   whether Live series need distinct conflict rules.
        if normalized_action == "START" and is_product_campaign:
            product_specific_type = _resolve_product_specific_type(campaign)
            if product_specific_type == "ALL":
                usage_resp = await client.gmv_max_store_shop_ad_usage_check(
                    GMVMaxStoreAdUsageCheckRequest(
                        advertiser_id=str(advertiser_id),
                        store_id=str(campaign.store_id or ""),
                    )
                )
                promote_all_products_allowed = bool(
                    getattr(getattr(usage_resp, "data", None), "promote_all_products_allowed", False)
                )
                if not promote_all_products_allowed:
                    raise TTBBusinessError(
                        "Product GMV Max ALL campaign conflicts with another running campaign",
                        code="GMVMAX_PROMOTE_ALL_CONFLICT",
                        payload={
                            "campaign_id": campaign.campaign_id,
                            "store_id": campaign.store_id,
                        },
                    )
            else:
                item_group_ids = get_item_group_ids_for_campaign(db, campaign=campaign)
                ttb_client = build_ttb_client(db, auth_id=auth_id)
                products = await _list_products_for_gmvmax(
                    ttb_client,
                    advertiser_id=str(advertiser_id),
                    store_id=str(campaign.store_id or ""),
                )
                status_map = {
                    str(prod.get("item_group_id")): prod.get("gmv_max_ads_status")
                    for prod in products
                    if prod.get("item_group_id")
                    and prod.get("status") == "AVAILABLE"
                }
                conflicting = [
                    item_id
                    for item_id in item_group_ids
                    if status_map.get(item_id) != "UNOCCUPIED"
                ]
                if conflicting:
                    raise TTBBusinessError(
                        "Some SPUs are already occupied by another Product GMV Max campaign",
                        code="GMVMAX_PRODUCT_OCCUPIED",
                        payload={"item_group_ids": conflicting},
                    )

        log_entry = await svc_apply_campaign_action(
            db,
            ttb_client=client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            action=action,
            payload=payload,
            reason=reason,
            performed_by=performed_by,
            audit_hook=audit_hook,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()
        if ttb_client is not None:
            await ttb_client.aclose()

    db.refresh(campaign)
    return campaign, log_entry


def list_action_logs(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    limit: int,
    offset: int,
    sort: str = "-timestamp",
) -> tuple[TTBGmvMaxCampaign, Sequence[TTBGmvMaxActionLog], int]:
    """Return campaign with paginated action logs stored locally."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    sort_key = str(sort or "").lower()
    sort_desc = sort_key.startswith("-")
    order_clause = (
        TTBGmvMaxActionLog.created_at.desc()
        if sort_desc
        else TTBGmvMaxActionLog.created_at.asc()
    )

    query = (
        select(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
        .order_by(order_clause, TTBGmvMaxActionLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(query).scalars().all()

    count_query = (
        select(func.count())
        .select_from(TTBGmvMaxActionLog)
        .where(TTBGmvMaxActionLog.campaign_id == campaign.id)
    )
    total = db.scalar(count_query) or 0

    return campaign, rows, int(total)


def get_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
) -> TTBGmvMaxStrategyConfig:
    """Load or create the local strategy configuration for a GMV Max campaign."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )
    db.commit()
    db.refresh(cfg)
    return cfg


def _parse_decimal(value: Optional[str | float | int | Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):  # pragma: no cover
        raise HTTPException(status_code=422, detail="invalid decimal value")


def update_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: dict[str, Any],
) -> Optional[TTBGmvMaxStrategyConfig]:
    """Persist strategy config adjustments used by automated decision making."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )

    if not payload:
        return None

    if "enabled" in payload:
        cfg.enabled = bool(payload["enabled"])

    if "target_roi" in payload:
        cfg.target_roi = _parse_decimal(payload["target_roi"])

    if "min_roi" in payload:
        cfg.min_roi = _parse_decimal(payload["min_roi"])

    if "max_roi" in payload:
        cfg.max_roi = _parse_decimal(payload["max_roi"])

    if "min_impressions" in payload:
        cfg.min_impressions = payload["min_impressions"]

    if "min_clicks" in payload:
        cfg.min_clicks = payload["min_clicks"]

    if "max_budget_raise_pct_per_day" in payload:
        cfg.max_budget_raise_pct_per_day = _parse_decimal(
            payload["max_budget_raise_pct_per_day"]
        )

    if "max_budget_cut_pct_per_day" in payload:
        cfg.max_budget_cut_pct_per_day = _parse_decimal(
            payload["max_budget_cut_pct_per_day"]
        )

    if "max_roas_step_per_adjust" in payload:
        cfg.max_roas_step_per_adjust = _parse_decimal(
            payload["max_roas_step_per_adjust"]
        )

    if "cooldown_minutes" in payload:
        cfg.cooldown_minutes = payload["cooldown_minutes"]

    if "min_runtime_minutes_before_first_change" in payload:
        cfg.min_runtime_minutes_before_first_change = payload[
            "min_runtime_minutes_before_first_change"
        ]

    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def preview_strategy(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
) -> dict[str, Any]:
    """Compute a dry-run decision using cached metrics and strategy thresholds."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    cfg = get_or_create_strategy_config(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign=campaign,
    )
    if not cfg.enabled:
        return {"enabled": False, "reason": "strategy.disabled"}

    metrics_raw = aggregate_recent_metrics(db, campaign=campaign)
    metrics = dict(metrics_raw)
    roi = metrics.get("roi")
    metrics["roi"] = str(roi) if roi is not None else None

    decision = decide_campaign_action(
        campaign=campaign,
        strategy=cfg,
        metrics=metrics_raw,
    )
    return {
        "enabled": True,
        "metrics": metrics,
        "decision": dict(decision) if decision else None,
    }

