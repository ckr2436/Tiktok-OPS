from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, union
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.data.models.hermes_agent import HermesContentProduct
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
    WebsiteAdsDailyReport,
    WebsiteAdsLandingPage,
    WebsiteAdsMagentoConnection,
    WebsiteAdsMediaPlan,
    WebsiteAdsMetricHourly,
    WebsiteAdsUploadFingerprint,
)
from app.data.models.ttb_entities import TTBAdvertiser
from app.features.tenants.ttb.gmv_max._helpers import get_ttb_client_for_account, resolve_account_binding
from app.features.tenants.ttb.website_ads.schemas import (
    AdGroupDeliveryUpdateRequest,
    LocationSearchRequest,
    MagentoConnectionCreate,
    MagentoConnectionUpdate,
    ManualLandingPageCreate,
    CreativeAssetUpdate,
    MediaPlanGenerateRequest,
    ProductProfileUpdate,
    StatusUpdateRequest,
    TargetingSearchRequest,
    WebsiteAdLaunchRequest,
    VideoUploadByUrlRequest,
)
from app.features.tenants.ttb.website_ads.scope import resolve_bound_advertiser_id
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.website_ads_launch import launch_website_ad
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_hermes_planner import (
    analyze_product_profile,
    media_plan_dict,
    resolve_website_ads_advertiser_id,
    sync_creative_assets,
    sync_spark_creative_assets,
)
from app.services.website_ads_asset_pipeline import resolve_asset_contact_sheet
from app.services.website_ads_magento import create_connection, sync_landing_pages, update_connection
from app.services.website_ads_media_cache import (
    archive_remote_url,
    archive_stream,
    public_asset_media_url,
    resolve_asset_media,
    unique_tiktok_file_name,
)
from app.services.website_ads_uploads import (
    complete_upload_fingerprint as _complete_upload_fingerprint,
    extract_uploaded_video_id as _extract_uploaded_video_id,
    fail_upload_fingerprint as _fail_upload_fingerprint,
    queue_upload_fingerprint as _queue_upload_fingerprint,
    reserve_upload_fingerprint as _reserve_upload_fingerprint,
    upload_result as _upload_result,
    upsert_uploaded_asset as _upsert_uploaded_asset,
)
from app.services.website_ads_monitor import run_website_ads_monitor_cycle
from app.services.website_ads_conversion_guard import apply_manual_campaign_override
from app.services.website_ads_execution_lock import (
    WebsiteAdsExecutionLockLost,
    assert_website_ads_execution_lock,
    website_ads_execution_lease,
)
from app.services.website_ads_products import bind_content_product, content_product_summary, effective_product_profile
from app.services.website_ads_tracking import build_tracking_url
router = APIRouter(prefix="/website-ads", tags=["Tenant / Website Ads"])

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mpeg", ".avi"}
_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/mpeg", "video/x-msvideo"}
_MAX_VIDEO_BYTES = 500 * 1024 * 1024


def _website_ads_task(name: str):
    # Import only after FastAPI has finished loading the TTB router tree. The
    # Celery module imports TTB helpers, so resolving it at module load creates
    # a circular dependency during a cold Gunicorn start.
    from app.tasks import website_ads_tasks

    return getattr(website_ads_tasks, name)


def _metric_summary(db: Session, filters: list, *, join_ads: bool) -> tuple[dict, datetime | None]:
    statement = select(
        func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.conversion_value), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_play_actions), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_2s), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_watched_6s), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p25), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p50), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p75), 0),
        func.coalesce(func.sum(WebsiteAdsMetricHourly.video_views_p100), 0),
        func.coalesce(
            func.sum(func.coalesce(WebsiteAdsMetricHourly.average_video_play, 0) * WebsiteAdsMetricHourly.video_play_actions),
            0,
        ),
        func.max(WebsiteAdsMetricHourly.synced_at),
    )
    if join_ads:
        statement = statement.join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
    values = db.execute(statement.where(*filters)).one()
    spend = Decimal(str(values[0] or 0))
    conversion_value = Decimal(str(values[1] or 0))
    conversions = Decimal(str(values[2] or 0))
    impressions = int(values[3] or 0)
    clicks = int(values[4] or 0)
    video_plays = int(values[5] or 0)
    watched_2s = int(values[6] or 0)
    watched_6s = int(values[7] or 0)
    views_p25 = int(values[8] or 0)
    views_p50 = int(values[9] or 0)
    views_p75 = int(values[10] or 0)
    views_p100 = int(values[11] or 0)
    weighted_play_seconds = Decimal(str(values[12] or 0))
    metrics = {
        "spend": float(spend),
        "conversion_value": float(conversion_value),
        "conversions": float(conversions),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": clicks / impressions if impressions else 0.0,
        "cpc": float(spend / clicks) if clicks else 0.0,
        "cpm": float(spend * Decimal("1000") / impressions) if impressions else 0.0,
        "cpa": float(spend / conversions) if conversions else 0.0,
        "roas": float(conversion_value / spend) if spend else 0.0,
        "video_play_actions": video_plays,
        "video_watched_2s": watched_2s,
        "video_watched_6s": watched_6s,
        "video_views_p25": views_p25,
        "video_views_p50": views_p50,
        "video_views_p75": views_p75,
        "video_views_p100": views_p100,
        "video_2s_rate": watched_2s / video_plays if video_plays else 0.0,
        "video_6s_rate": watched_6s / video_plays if video_plays else 0.0,
        "video_p25_rate": views_p25 / video_plays if video_plays else 0.0,
        "video_p50_rate": views_p50 / video_plays if video_plays else 0.0,
        "video_p75_rate": views_p75 / video_plays if video_plays else 0.0,
        "video_completion_rate": views_p100 / video_plays if video_plays else 0.0,
        "average_video_play": float(weighted_play_seconds / video_plays) if video_plays else 0.0,
    }
    return metrics, values[13]


def _scoped_action_query(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
):
    action_scope = (
        WebsiteAdsActionLog.workspace_id == int(workspace_id),
        WebsiteAdsActionLog.auth_id == int(auth_id),
    )
    campaign_scope = (
        WebsiteAdsCampaign.workspace_id == int(workspace_id),
        WebsiteAdsCampaign.auth_id == int(auth_id),
        WebsiteAdsCampaign.advertiser_id == str(advertiser_id),
    )
    asset_scope = (
        WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
        WebsiteAdsCreativeAsset.auth_id == int(auth_id),
        WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
    )
    plan_scope = (
        WebsiteAdsMediaPlan.workspace_id == int(workspace_id),
        WebsiteAdsMediaPlan.auth_id == int(auth_id),
        WebsiteAdsMediaPlan.advertiser_id == str(advertiser_id),
    )
    campaign_request_id = WebsiteAdsActionLog.request_json["campaign_local_id"].as_integer()
    media_plan_request_id = WebsiteAdsActionLog.request_json["media_plan_id"].as_integer()
    asset_request_id = WebsiteAdsActionLog.request_json["asset_id"].as_integer()
    attributed_ids = union(
        select(WebsiteAdsActionLog.id.label("action_id"))
        .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsActionLog.ad_local_id)
        .join(
            WebsiteAdsCampaign,
            WebsiteAdsCampaign.id == WebsiteAdsAd.campaign_local_id,
        )
        .where(*action_scope, *campaign_scope),
        select(WebsiteAdsActionLog.id.label("action_id"))
        .join(
            WebsiteAdsCampaign,
            WebsiteAdsCampaign.id == campaign_request_id,
        )
        .where(*action_scope, *campaign_scope),
        select(WebsiteAdsActionLog.id.label("action_id"))
        .join(
            WebsiteAdsMediaPlan,
            WebsiteAdsMediaPlan.id == media_plan_request_id,
        )
        .where(*action_scope, *plan_scope),
        select(WebsiteAdsActionLog.id.label("action_id"))
        .join(
            WebsiteAdsCreativeAsset,
            WebsiteAdsCreativeAsset.id == asset_request_id,
        )
        .where(*action_scope, *asset_scope),
    ).subquery()
    return select(WebsiteAdsActionLog).where(
        WebsiteAdsActionLog.id.in_(select(attributed_ids.c.action_id))
    )


def _connection_dict(row: WebsiteAdsMagentoConnection) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "base_url": row.base_url,
        "is_enabled": row.is_enabled,
        "last_sync_at": row.last_sync_at,
        "last_error": row.last_error,
        "has_access_token": bool(row.access_token_cipher),
    }


def _landing_dict(db: Session, row: WebsiteAdsLandingPage) -> dict:
    effective = effective_product_profile(db, row)
    tracking_url, tracking_params = build_tracking_url(row.landing_url)
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    return {
        "id": row.id,
        "connection_id": row.connection_id,
        "content_product_id": row.content_product_id,
        "content_product": effective["content_product"],
        "external_id": row.external_id,
        "title": row.title,
        "identifier": row.identifier,
        "landing_url": row.landing_url,
        "product_id": row.product_id,
        "content_name": row.content_name,
        "content_category": row.content_category,
        "brand": row.brand,
        "reference_price": float(row.reference_price) if row.reference_price is not None else None,
        "currency": row.currency,
        "image_url": row.image_url,
        "seller_profile": row.seller_profile,
        "promotion_text": row.promotion_text,
        "product_details": row.product_details,
        "effective_brand": effective["brand"],
        "effective_content_name": effective["content_name"],
        "effective_description": effective["description"],
        "effective_product_details": effective["product_details"],
        "tiktok_shop_url": raw.get("pc_fallback_url") or raw.get("mobile_fallback_url") or raw.get("raw_pdp_url"),
        "tracking_url_preview": tracking_url,
        "tracking_params": tracking_params,
        "hermes_analysis": row.hermes_analysis_json,
        "analysis_status": row.analysis_status,
        "analysis_error": row.analysis_error,
        "analyzed_at": row.analyzed_at,
        "is_active": row.is_active,
        "last_synced_at": row.last_synced_at,
    }


def _asset_dict(row: WebsiteAdsCreativeAsset) -> dict:
    analysis = dict(row.hermes_analysis_json or {})
    policy = assess_website_ads_creative_policy(analysis)
    product_match = analysis.get("product_match") if isinstance(analysis.get("product_match"), dict) else {}
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    user_info = raw.get("user_info") if isinstance(raw.get("user_info"), dict) else {}
    auth_info = raw.get("auth_info") if isinstance(raw.get("auth_info"), dict) else {}
    item_info = raw.get("item_info") if isinstance(raw.get("item_info"), dict) else {}
    is_spark = str(row.source or "").upper() == "SPARK_AUTHORIZED_POST"
    return {
        "id": row.id,
        "landing_page_id": row.landing_page_id,
        "video_id": row.video_id,
        "title": row.title,
        "file_name": row.file_name,
        "preview_url": public_asset_media_url(row, "video") if resolve_asset_media(row, "video") else row.preview_url,
        "cover_url": public_asset_media_url(row, "cover") if resolve_asset_media(row, "cover") else row.cover_url,
        "duration_seconds": float(row.duration_seconds) if row.duration_seconds is not None else None,
        "width": row.width,
        "height": row.height,
        "source": row.source,
        "source_label": "达人授权 Spark" if is_spark else "广告主素材库",
        "tiktok_item_id": item_info.get("item_id") if is_spark else None,
        "creator_name": user_info.get("tiktok_name") if is_spark else None,
        "source_identity_id": user_info.get("identity_id") if is_spark else None,
        "source_identity_type": user_info.get("identity_type") if is_spark else None,
        "authorization_status": auth_info.get("ad_auth_status") if is_spark else None,
        "authorization_end_time": auth_info.get("auth_end_time") if is_spark else None,
        "user_notes": row.user_notes,
        "tags": list(row.tags_json or []),
        "hermes_analysis": row.hermes_analysis_json,
        "creative_type": analysis.get("creative_type"),
        "talent_type": analysis.get("talent_type"),
        "production_origin": analysis.get("production_origin"),
        "is_real_creator": bool(analysis.get("is_real_creator")) or is_spark,
        "video_description": analysis.get("video_description") or analysis.get("visual_summary"),
        "creator_style": analysis.get("creator_style"),
        "hook_type": analysis.get("hook_type"),
        "funnel_stage": analysis.get("funnel_stage"),
        "product_match": product_match,
        "policy_readiness": policy["readiness"],
        "policy_flags": policy["flags"],
        "policy_risk_only": policy["risk_only"],
        "policy_submission_mode": policy["submission_mode"],
        "analysis_status": row.analysis_status,
        "analysis_error": row.analysis_error,
        "analysis_version": row.analysis_version,
        "analysis_attempts": row.analysis_attempts,
        "analysis_next_retry_at": row.analysis_next_retry_at,
        "analysis_inputs": row.analysis_inputs_json,
        "transcript_excerpt": str(row.transcript_text or "")[:600] or None,
        "transcript_language": row.transcript_language,
        "contact_sheet_url": row.contact_sheet_url,
        "analyzed_at": row.analyzed_at,
        "auto_launch_status": row.auto_launch_status,
        "auto_launch_attempts": row.auto_launch_attempts,
        "auto_launch_next_retry_at": row.auto_launch_next_retry_at,
        "auto_launch_decision": row.auto_launch_decision_json,
        "auto_launch_error": row.auto_launch_error,
        "auto_launched_at": row.auto_launched_at,
        "last_synced_at": row.last_synced_at,
        "is_active": row.is_active,
    }


@router.get("/connections")
def list_connections(
    workspace_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    rows = db.scalars(
        select(WebsiteAdsMagentoConnection)
        .where(WebsiteAdsMagentoConnection.workspace_id == workspace_id)
        .order_by(WebsiteAdsMagentoConnection.id)
    ).all()
    return {"items": [_connection_dict(row) for row in rows]}


@router.post("/connections", status_code=201)
def add_connection(
    workspace_id: int,
    body: MagentoConnectionCreate,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    try:
        row = create_connection(
            db,
            workspace_id=workspace_id,
            name=body.name,
            base_url=str(body.base_url),
            access_token=body.access_token,
            is_enabled=body.is_enabled,
        )
        return _connection_dict(row)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/connections/{connection_id}")
def patch_connection(
    workspace_id: int,
    connection_id: int,
    body: MagentoConnectionUpdate,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    row = db.get(WebsiteAdsMagentoConnection, connection_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Magento connection not found")
    try:
        return _connection_dict(update_connection(db, row, **body.model_dump(exclude_unset=True)))
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/connections/{connection_id}/sync")
async def sync_connection(
    workspace_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    row = db.get(WebsiteAdsMagentoConnection, connection_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Magento connection not found")
    try:
        # This custom Magento endpoint is an unpaginated full export. Keep the
        # completeness assertion explicit so future paginated callers default
        # to upsert-only behavior.
        return await sync_landing_pages(db, row, complete_snapshot=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/landing-pages")
def list_landing_pages(
    workspace_id: int,
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    query = select(WebsiteAdsLandingPage).where(WebsiteAdsLandingPage.workspace_id == workspace_id)
    if not include_inactive:
        query = query.where(WebsiteAdsLandingPage.is_active.is_(True))
    rows = db.scalars(query.order_by(WebsiteAdsLandingPage.updated_at.desc())).all()
    return {"items": [_landing_dict(db, row) for row in rows]}


@router.get("/content-products")
def list_content_factory_products(
    workspace_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    rows = db.scalars(
        select(HermesContentProduct)
        .where(HermesContentProduct.workspace_id == workspace_id, HermesContentProduct.status == "active")
        .order_by(HermesContentProduct.updated_at.desc(), HermesContentProduct.id.desc())
    ).all()
    return {"items": [content_product_summary(row) for row in rows]}


@router.post("/landing-pages/manual", status_code=201)
def add_manual_landing_page(
    workspace_id: int,
    body: ManualLandingPageCreate,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    url = str(body.landing_url)
    row = WebsiteAdsLandingPage(
        workspace_id=workspace_id,
        connection_id=None,
        external_id=f"manual-{uuid4()}",
        identifier=f"manual-{uuid4().hex[:12]}",
        title=body.title,
        landing_url=url,
        product_id=body.product_id,
        content_name=body.title,
        reference_price=Decimal(str(body.reference_price)) if body.reference_price is not None else None,
        currency=body.currency.upper(),
        image_url=str(body.image_url) if body.image_url else None,
        is_active=True,
        raw_json={"source": "manual"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _landing_dict(db, row)


@router.patch("/products/{product_id}")
def update_product_profile(
    workspace_id: int,
    product_id: int,
    body: ProductProfileUpdate,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    row = db.get(WebsiteAdsLandingPage, product_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Product not found")
    values = body.model_dump(exclude_unset=True)
    if "content_product_id" in values:
        try:
            bind_content_product(db, row, values.pop("content_product_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key, value in values.items():
        if key in {"landing_url", "image_url"} and value is not None:
            value = str(value)
        if key == "reference_price" and value is not None:
            value = Decimal(str(value))
        if key == "currency" and value is not None:
            value = str(value).upper()
        setattr(row, key, value)
    row.analysis_status = "NOT_ANALYZED"
    row.analysis_error = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return _landing_dict(db, row)


@router.post("/products/{product_id}/analyze")
async def analyze_product(
    workspace_id: int,
    product_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    row = db.get(WebsiteAdsLandingPage, product_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        return _landing_dict(db, await analyze_product_profile(db, row))
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _with_api(db: Session, workspace_id: int, provider: str, auth_id: int, callback):
    client = TikTokWebsiteAdsClient(get_ttb_client_for_account(db, workspace_id, provider, auth_id))
    try:
        return await callback(client)
    finally:
        await client.aclose()


async def _run_locked_manual_mutation(
    db: Session,
    *,
    workspace_id: int,
    operation: str,
    mutation,
):
    """Serialize console writes with monitor/report actions."""

    with website_ads_execution_lease(
        db,
        operation=operation,
        workspace_id=workspace_id,
    ) as lease:
        if lease is None:
            db.rollback()
            raise HTTPException(
                status_code=423,
                detail="Website Ads is busy; retry after the active automation cycle.",
            )
        # Lock acquisition may have waited behind a monitor/manual writer.
        # End any pre-lock REPEATABLE READ transaction and discard identity-map
        # state before the mutation callback re-queries its complete scope.
        db.rollback()
        db.expire_all()
        try:
            result = await mutation()
            lease.assert_active()
            return result
        except WebsiteAdsExecutionLockLost as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Website Ads execution ownership was lost; "
                    "no further remote changes were attempted."
                ),
            ) from exc
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=502, detail=str(exc)) from exc


def _request_advertiser_id(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    requested_advertiser_id: object = None,
) -> str:
    binding = resolve_account_binding(db, workspace_id, provider, auth_id)
    return resolve_bound_advertiser_id(
        binding.advertiser_id,
        requested_advertiser_id,
    )


def _legacy_compatible_page_size(
    *,
    page_size: int | None,
    limit: int | None,
    default: int,
) -> int:
    """Resolve the numbered-page size while preserving legacy ``limit`` callers.

    List responses always expose the effective page size and the untruncated
    total, so a legacy ``limit`` can no longer masquerade as a complete list.
    """

    return int(page_size if page_size is not None else limit if limit is not None else default)


@router.get("/metadata")
async def metadata(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str | None = None,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        requested_advertiser_id=advertiser_id,
    )

    async def load(client):
        pixels = await client.list_all_pixels(advertiser)
        identities = await client.list_all_identities(advertiser)
        videos = await client.list_all_videos(advertiser)
        advertiser_row = db.scalar(
            select(TTBAdvertiser).where(
                TTBAdvertiser.workspace_id == workspace_id,
                TTBAdvertiser.auth_id == auth_id,
                TTBAdvertiser.advertiser_id == advertiser,
            )
        )
        advertiser_timezone = (
            str(advertiser_row.display_timezone or advertiser_row.timezone or "UTC")
            if advertiser_row
            else "UTC"
        )
        return {
            "advertiser_id": advertiser,
            "advertiser_timezone": advertiser_timezone,
            "pixels": pixels.get("data", {}),
            "identities": identities.get("data", {}),
            "videos": videos.get("data", {}),
        }

    try:
        return await _with_api(db, workspace_id, provider, auth_id, load)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/creative-assets/sync")
async def sync_asset_library(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    try:
        videos = await _with_api(
            db,
            workspace_id,
            provider,
            auth_id,
            lambda client: client.list_all_videos(advertiser),
        )
        spark_payloads = await _with_api(
            db,
            workspace_id,
            provider,
            auth_id,
            lambda client: client.list_all_spark_videos(advertiser),
        )
        sync_creative_assets(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser,
            videos_payload=videos.get("data", {}),
            complete_snapshot=True,
        )
        sync_spark_creative_assets(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser,
            spark_payloads=[item.get("data", {}) for item in spark_payloads],
            complete_snapshot=True,
        )
        dispatch = _website_ads_task("dispatch_asset_analysis")(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser,
            limit=30,
        )
        media_cache_dispatch = _website_ads_task("dispatch_asset_media_cache")(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser,
            limit=30,
        )
        refreshed = list(
            db.scalars(
                select(WebsiteAdsCreativeAsset).where(
                    WebsiteAdsCreativeAsset.workspace_id == workspace_id,
                    WebsiteAdsCreativeAsset.auth_id == auth_id,
                    WebsiteAdsCreativeAsset.advertiser_id == advertiser,
                    WebsiteAdsCreativeAsset.is_active.is_(True),
                ).order_by(WebsiteAdsCreativeAsset.updated_at.desc())
            ).all()
        )
        return {
            "items": [_asset_dict(row) for row in refreshed],
            "total": len(refreshed),
            "analysis_dispatch": dispatch,
            "media_cache_dispatch": media_cache_dispatch,
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/creative-assets")
def list_asset_library(
    workspace_id: int,
    provider: str,
    auth_id: int,
    landing_page_id: int | None = Query(None),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    query = select(WebsiteAdsCreativeAsset).where(
        WebsiteAdsCreativeAsset.workspace_id == workspace_id,
        WebsiteAdsCreativeAsset.auth_id == auth_id,
        WebsiteAdsCreativeAsset.advertiser_id == advertiser,
        WebsiteAdsCreativeAsset.is_active.is_(True),
    )
    if landing_page_id is not None:
        query = query.where(
            (WebsiteAdsCreativeAsset.landing_page_id == landing_page_id)
            | (WebsiteAdsCreativeAsset.landing_page_id.is_(None))
        )
    rows = db.scalars(query.order_by(WebsiteAdsCreativeAsset.updated_at.desc())).all()
    return {"items": [_asset_dict(row) for row in rows], "total": len(rows)}


@router.post("/creative-assets/analysis/dispatch")
def dispatch_asset_library_analysis(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    return _website_ads_task("dispatch_asset_analysis")(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser,
        limit=20,
    )


@router.get("/creative-assets/{asset_id}/contact-sheet")
def get_asset_contact_sheet(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsCreativeAsset, asset_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Creative asset not found")
    path = resolve_asset_contact_sheet(row)
    if not path:
        raise HTTPException(status_code=404, detail="Creative contact sheet is unavailable")
    return FileResponse(path, media_type="image/jpeg", filename=f"creative-{asset_id}-contact-sheet.jpg")


@router.get("/creative-assets/{asset_id}/video")
def get_asset_cached_video(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsCreativeAsset, asset_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Creative asset not found")
    media = resolve_asset_media(row, "video")
    if not media:
        raise HTTPException(status_code=404, detail="Creative video is not cached yet")
    path, content_type = media
    return FileResponse(
        path,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/creative-assets/{asset_id}/cover")
def get_asset_cached_cover(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsCreativeAsset, asset_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Creative asset not found")
    media = resolve_asset_media(row, "cover")
    if not media:
        raise HTTPException(status_code=404, detail="Creative cover is not cached yet")
    path, content_type = media
    return FileResponse(
        path,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.patch("/creative-assets/{asset_id}")
def update_asset_library_item(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int,
    body: CreativeAssetUpdate,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsCreativeAsset, asset_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Creative asset not found")
    values = body.model_dump(exclude_unset=True)
    if "landing_page_id" in values and values["landing_page_id"] is not None:
        product = db.get(WebsiteAdsLandingPage, int(values["landing_page_id"]))
        if not product or product.workspace_id != workspace_id:
            raise HTTPException(status_code=400, detail="Product is unavailable")
    if "tags" in values:
        row.tags_json = values.pop("tags")
    for key, value in values.items():
        setattr(row, key, value)
    row.analysis_status = "NOT_ANALYZED"
    row.analysis_version = None
    row.analysis_error = None
    row.auto_launch_status = "PENDING"
    row.auto_launch_next_retry_at = None
    row.auto_launch_error = None
    row.auto_launch_decision_json = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return _asset_dict(row)


@router.post("/creative-assets/{asset_id}/analyze")
def analyze_asset_library_item(
    workspace_id: int,
    provider: str,
    auth_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsCreativeAsset, asset_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Creative asset not found")
    row.auto_launch_status = "PENDING"
    row.auto_launch_next_retry_at = None
    row.auto_launch_error = None
    db.add(row)
    db.commit()
    _website_ads_task("dispatch_asset_analysis")(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser,
        asset_ids=[asset_id],
        force=True,
        limit=1,
    )
    db.refresh(row)
    return _asset_dict(row)


@router.get("/videos/uploads")
def list_video_uploads(
    workspace_id: int,
    provider: str,
    auth_id: int,
    upload_ids: str | None = Query(default=None, max_length=2000),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    limit: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    binding = resolve_account_binding(db, workspace_id, provider, auth_id)
    advertiser = str(binding.advertiser_id or "")
    effective_page_size = _legacy_compatible_page_size(
        page_size=page_size,
        limit=limit,
        default=50,
    )
    query = select(WebsiteAdsUploadFingerprint).where(
        WebsiteAdsUploadFingerprint.workspace_id == int(workspace_id),
        WebsiteAdsUploadFingerprint.auth_id == int(auth_id),
        WebsiteAdsUploadFingerprint.advertiser_id == advertiser,
    )
    if upload_ids:
        try:
            parsed_ids = list(dict.fromkeys(int(value) for value in upload_ids.split(",") if value.strip()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="upload_ids must contain comma-separated integers") from exc
        if not parsed_ids:
            return {
                "items": [],
                "page": int(page),
                "page_size": effective_page_size,
                "total": 0,
            }
        query = query.where(WebsiteAdsUploadFingerprint.id.in_(parsed_ids))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        db.scalars(
            query.order_by(WebsiteAdsUploadFingerprint.id.desc())
            .offset((int(page) - 1) * effective_page_size)
            .limit(effective_page_size)
        ).all()
    )
    return {
        "items": [_upload_result(row, deduplicated=False) for row in rows],
        "page": int(page),
        "page_size": effective_page_size,
        "total": int(total),
    }


@router.post("/videos/upload-url", status_code=202)
async def upload_video_by_url(
    workspace_id: int,
    provider: str,
    auth_id: int,
    body: VideoUploadByUrlRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    binding = resolve_account_binding(db, workspace_id, provider, auth_id)
    advertiser = str(binding.advertiser_id or "")
    if not advertiser:
        raise HTTPException(status_code=400, detail="Advertiser is not configured")
    source_url = str(body.video_url).strip()
    original_name = str(body.file_name or Path(urlsplit(source_url).path).name or "video.mp4").strip()
    try:
        archived = await archive_remote_url(source_url, file_name=original_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    upload_name = unique_tiktok_file_name(original_name, archived.sha256)
    fingerprint, should_upload = _reserve_upload_fingerprint(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser,
        content_sha256=archived.sha256,
        fingerprint_type="URL_CONTENT_SHA256",
        file_name=upload_name,
        file_size_bytes=archived.size_bytes,
        initial_status="QUEUED",
    )
    if not should_upload:
        result = _upload_result(fingerprint, deduplicated=True)
        if fingerprint.video_id:
            asset = _upsert_uploaded_asset(
                db,
                fingerprint=fingerprint,
                payload=dict(fingerprint.response_json or {}),
                archived=archived,
                original_name=original_name,
                source_url=source_url,
            )
            db.commit()
            db.refresh(asset)
            result["asset_id"] = int(asset.id)
            result["preview_url"] = public_asset_media_url(asset, "video")
        return result
    fingerprint = _queue_upload_fingerprint(
        db,
        fingerprint,
        archived=archived,
        provider=provider,
        original_name=original_name,
        upload_name=upload_name,
        source_url=source_url,
        flaw_detect=body.flaw_detect,
        auto_fix_enabled=body.auto_fix_enabled,
    )
    dispatched = True
    try:
        _website_ads_task("upload_video_task").apply_async(
            kwargs={"upload_id": int(fingerprint.id), "provider": provider},
            queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
        )
    except Exception as exc:
        dispatched = False
        fingerprint.error_message = f"QueueDispatchPending: {type(exc).__name__}: {exc}"[:4000]
        db.add(fingerprint)
        db.commit()
        db.refresh(fingerprint)
    result = _upload_result(fingerprint, deduplicated=False)
    result["task_accepted"] = dispatched
    return result


@router.post("/videos/upload-file", status_code=202)
async def upload_video_file(
    workspace_id: int,
    provider: str,
    auth_id: int,
    video_file: UploadFile = File(...),
    file_name: str | None = Form(None),
    flaw_detect: bool = Form(False),
    auto_fix_enabled: bool = Form(False),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    binding = resolve_account_binding(db, workspace_id, provider, auth_id)
    advertiser = str(binding.advertiser_id or "")
    if not advertiser:
        raise HTTPException(status_code=400, detail="Advertiser is not configured")
    original_name = str(video_file.filename or "video.mp4").strip()
    requested_name = str(file_name or original_name).strip()
    extension = Path(original_name).suffix.lower()
    content_type = str(video_file.content_type or "application/octet-stream").lower()
    if extension not in _VIDEO_EXTENSIONS or (
        content_type not in _VIDEO_CONTENT_TYPES
        and content_type not in {"application/octet-stream", "binary/octet-stream"}
    ):
        raise HTTPException(status_code=400, detail="Only MP4, MOV, MPEG, and AVI video files are supported")
    try:
        archived = await asyncio.to_thread(
            archive_stream,
            video_file.file,
            file_name=original_name,
            content_type=content_type,
            max_bytes=_MAX_VIDEO_BYTES,
        )
    except ValueError as exc:
        status_code = 413 if "500 MB" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to archive video: {type(exc).__name__}: {exc}") from exc
    upload_name = unique_tiktok_file_name(requested_name, archived.sha256)
    fingerprint, should_upload = _reserve_upload_fingerprint(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser,
        content_sha256=archived.sha256,
        fingerprint_type="FILE_SHA256",
        file_name=upload_name,
        file_size_bytes=archived.size_bytes,
        initial_status="QUEUED",
    )
    if not should_upload:
        result = _upload_result(fingerprint, deduplicated=True)
        if fingerprint.video_id:
            asset = _upsert_uploaded_asset(
                db,
                fingerprint=fingerprint,
                payload=dict(fingerprint.response_json or {}),
                archived=archived,
                original_name=original_name,
            )
            db.commit()
            db.refresh(asset)
            result["asset_id"] = int(asset.id)
            result["preview_url"] = public_asset_media_url(asset, "video")
        return result
    fingerprint = _queue_upload_fingerprint(
        db,
        fingerprint,
        archived=archived,
        provider=provider,
        original_name=original_name,
        upload_name=upload_name,
        flaw_detect=flaw_detect,
        auto_fix_enabled=auto_fix_enabled,
    )
    dispatched = True
    try:
        _website_ads_task("upload_video_task").apply_async(
            kwargs={"upload_id": int(fingerprint.id), "provider": provider},
            queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
        )
    except Exception as exc:
        dispatched = False
        fingerprint.error_message = f"QueueDispatchPending: {type(exc).__name__}: {exc}"[:4000]
        db.add(fingerprint)
        db.commit()
        db.refresh(fingerprint)
    result = _upload_result(fingerprint, deduplicated=False)
    result["task_accepted"] = dispatched
    return result


@router.post("/targeting/interests")
async def search_interests(
    workspace_id: int,
    provider: str,
    auth_id: int,
    body: TargetingSearchRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        requested_advertiser_id=body.advertiser_id,
    )
    payload = await _with_api(
        db,
        workspace_id,
        provider,
        auth_id,
        lambda client: client.search_targeting(advertiser, body.targeting_type, body.search_keywords),
    )
    data = payload.get("data") if isinstance(payload, dict) else {}
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(data, dict):
        for category, category_data in data.items():
            search_result = category_data.get("search_result") if isinstance(category_data, dict) else None
            if not isinstance(search_result, dict):
                continue
            for values in search_result.values():
                if not isinstance(values, list):
                    continue
                for item in values:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    key = (str(item["id"]), str(item.get("sub_targeting_type") or category))
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({
                        "interest_category_id": str(item["id"]),
                        "name": item.get("name") or str(item["id"]),
                        "category": category,
                        "sub_targeting_type": item.get("sub_targeting_type"),
                    })
    return {"items": items}


@router.post("/targeting/locations")
async def search_locations(
    workspace_id: int,
    provider: str,
    auth_id: int,
    body: LocationSearchRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        requested_advertiser_id=body.advertiser_id,
    )
    payload = {
        "advertiser_id": advertiser,
        "objective_type": body.objective_type,
        "promotion_type": body.promotion_type,
        "placements": ["PLACEMENT_TIKTOK"],
        "search_type": "FUZZY_SEARCH",
        "keywords": [body.search_keyword],
    }
    if body.region_codes:
        payload["region_codes"] = body.region_codes
    if body.geo_types:
        payload["geo_types"] = body.geo_types
    response = await _with_api(db, workspace_id, provider, auth_id, lambda client: client.search_locations(payload))
    data = response.get("data") if isinstance(response, dict) else {}
    tags = data.get("targeting_tag_list") if isinstance(data, dict) else []
    items = []
    for tag in tags if isinstance(tags, list) else []:
        geo = tag.get("geo") if isinstance(tag, dict) else None
        if not isinstance(geo, dict) or not geo.get("geo_id"):
            continue
        items.append({
            "location_id": str(geo["geo_id"]),
            "name": tag.get("name") or tag.get("keyword") or str(geo["geo_id"]),
            "geo_type": geo.get("geo_type"),
            "region_code": geo.get("region_code"),
            "parent_id": geo.get("parent_id"),
        })
    return {"items": items}


_MEDIA_PLAN_GENERATION_RECOVERY_LIMIT = 2
_MEDIA_PLAN_GENERATION_STALE_SECONDS = 22 * 60


def _media_plan_task_state(task_id: str | None) -> str:
    if not task_id:
        return "MISSING"
    try:
        from app.celery_app import celery_app

        return str(celery_app.AsyncResult(str(task_id)).state or "PENDING").upper()
    except Exception:
        return "UNKNOWN"


def _media_plan_generation_age_seconds(
    plan: WebsiteAdsMediaPlan,
    *,
    now: datetime | None = None,
) -> int:
    current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    reference = plan.updated_at or plan.created_at or current
    return max(0, int((current - reference).total_seconds()))


def _media_plan_generation_recovery_reason(
    *,
    status: str | None,
    task_state: str | None,
    age_seconds: int,
    has_task_id: bool,
) -> str | None:
    normalized_status = str(status or "").upper()
    normalized_task_state = str(task_state or "UNKNOWN").upper()
    if normalized_status not in {"GENERATING", "PROCESSING"}:
        return None
    if not has_task_id and age_seconds >= 30:
        return "MISSING_TASK_ID"
    if normalized_task_state in {"FAILURE", "REVOKED"}:
        return f"TASK_{normalized_task_state}"
    if normalized_task_state == "SUCCESS":
        return "TASK_FINISHED_WITHOUT_PLAN"
    if age_seconds >= _MEDIA_PLAN_GENERATION_STALE_SECONDS and normalized_task_state in {
        "MISSING", "PENDING", "UNKNOWN"
    }:
        return "STALE_WITHOUT_ACTIVE_TASK"
    if age_seconds >= (23 * 60):
        return "GENERATION_HEARTBEAT_TIMEOUT"
    return None


def _media_plan_generation_request(
    plan: WebsiteAdsMediaPlan,
    *,
    provider: str,
) -> dict:
    metadata = dict(plan.hermes_response_json or {})
    request = metadata.get("generation_request")
    request = dict(request) if isinstance(request, dict) else {}
    selected_ids = request.get("creative_asset_ids")
    if not isinstance(selected_ids, list):
        selected_ids = list(plan.selected_asset_ids_json or []) or None
    return {
        "plan_id": int(plan.id),
        "workspace_id": int(plan.workspace_id),
        "auth_id": int(plan.auth_id),
        "provider": str(request.get("provider") or provider),
        "landing_page_id": int(plan.landing_page_id),
        "creative_asset_ids": selected_ids,
        "daily_budget": float(plan.daily_budget),
        "activate_after_create": bool(plan.activate_after_create),
        "request_notes": request.get("request_notes"),
    }


def _dispatch_media_plan_generation(plan: WebsiteAdsMediaPlan, *, provider: str):
    metadata = dict(plan.hermes_response_json or {})
    task_id = str(metadata.get("generation_task_id") or "")
    if not task_id:
        raise RuntimeError("Hermes generation task id is missing")
    return _website_ads_task("generate_media_plan_task").apply_async(
        kwargs=_media_plan_generation_request(plan, provider=provider),
        queue=settings.WEBSITE_ADS_TASK_QUEUE,
        task_id=task_id,
    )


def _recover_media_plan_generation(
    db: Session,
    *,
    plan_id: int,
    provider: str,
) -> tuple[WebsiteAdsMediaPlan, str]:
    plan = db.scalar(
        select(WebsiteAdsMediaPlan)
        .where(WebsiteAdsMediaPlan.id == int(plan_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if plan is None:
        raise RuntimeError("Media plan no longer exists")
    metadata = dict(plan.hermes_response_json or {})
    task_id = str(metadata.get("generation_task_id") or "")
    task_state = _media_plan_task_state(task_id)
    reason = _media_plan_generation_recovery_reason(
        status=plan.status,
        task_state=task_state,
        age_seconds=_media_plan_generation_age_seconds(plan),
        has_task_id=bool(task_id),
    )
    if reason is None:
        db.commit()
        db.refresh(plan)
        return plan, task_state
    recovery_count = int(metadata.get("generation_recovery_count") or 0)
    if recovery_count >= _MEDIA_PLAN_GENERATION_RECOVERY_LIMIT:
        plan.status = "FAILED"
        plan.error_message = (
            "Hermes media plan generation could not recover after "
            f"{recovery_count} attempts ({reason})"
        )[:8000]
        metadata["generation_stage"] = "FAILED"
        metadata["generation_last_recovery_reason"] = reason
        plan.hermes_response_json = metadata
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan, task_state

    now = datetime.now(timezone.utc)
    new_task_id = uuid4().hex
    metadata["generation_task_id"] = new_task_id
    metadata["generation_stage"] = "RECOVERY_QUEUED"
    metadata["generation_recovery_count"] = recovery_count + 1
    metadata["generation_last_recovery_reason"] = reason
    metadata["generation_recovered_at"] = now.isoformat()
    metadata.setdefault("generation_request", {
        "provider": provider,
        "creative_asset_ids": list(plan.selected_asset_ids_json or []) or None,
        "request_notes": None,
    })
    plan.hermes_response_json = metadata
    plan.status = "GENERATING"
    plan.strategy_summary = "检测到后台任务中断，Hermes 已自动恢复方案生成"
    plan.error_message = None
    plan.updated_at = now.replace(tzinfo=None)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    try:
        task = _dispatch_media_plan_generation(plan, provider=provider)
    except Exception as exc:
        plan.status = "FAILED"
        plan.error_message = f"RecoveryQueueError: {exc}"[:8000]
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan, "FAILURE"
    if str(task.id) != new_task_id:
        metadata = dict(plan.hermes_response_json or {})
        metadata["generation_task_id"] = str(task.id)
        plan.hermes_response_json = metadata
        db.add(plan)
        db.commit()
        db.refresh(plan)
    return plan, "PENDING"


def _media_plan_generation_message(plan: WebsiteAdsMediaPlan, task_state: str, age_seconds: int) -> str:
    metadata = dict(plan.hermes_response_json or {})
    recovery_count = int(metadata.get("generation_recovery_count") or 0)
    minutes, seconds = divmod(max(0, int(age_seconds)), 60)
    elapsed = f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"
    if str(plan.status).upper() == "GENERATING":
        if recovery_count:
            return f"后台 worker 中断后已自动恢复（第 {recovery_count} 次），任务正在重新排队。"
        return "任务已进入 Hermes 队列，正在等待规划 worker 接管。"
    if str(plan.status).upper() == "PROCESSING":
        return f"Hermes 正在分析商品、素材和受众，当前阶段已运行 {elapsed}。"
    if str(plan.status).upper() == "READY":
        return "Hermes 投放方案已生成。"
    if str(plan.status).upper() == "FAILED":
        return "Hermes 投放方案生成失败，请查看错误信息。"
    return f"方案当前状态：{plan.status}（任务状态：{task_state}）。"


def _media_plan_generation_response(
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan: WebsiteAdsMediaPlan,
    *,
    task_state: str | None = None,
) -> dict:
    task_metadata = plan.hermes_response_json if isinstance(plan.hermes_response_json, dict) else {}
    resolved_task_state = task_state or _media_plan_task_state(task_metadata.get("generation_task_id"))
    age_seconds = _media_plan_generation_age_seconds(plan)
    return {
        "task_id": task_metadata.get("generation_task_id"),
        "plan_id": int(plan.id),
        "state": str(plan.status),
        "task_state": resolved_task_state,
        "stage": task_metadata.get("generation_stage") or str(plan.status),
        "message": _media_plan_generation_message(plan, resolved_task_state, age_seconds),
        "elapsed_seconds": age_seconds,
        "recovery_count": int(task_metadata.get("generation_recovery_count") or 0),
        "updated_at": plan.updated_at,
        "status_url": (
            f"/api/v1/tenants/{int(workspace_id)}/providers/{provider}/accounts/{int(auth_id)}"
            f"/website-ads/media-plans/{int(plan.id)}/generation"
        ),
    }


def _media_plan_execution_response(
    db: Session,
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan: WebsiteAdsMediaPlan,
) -> dict:
    context = plan.execution_context_json if isinstance(plan.execution_context_json, dict) else {}
    campaign = db.get(WebsiteAdsCampaign, plan.campaign_local_id) if plan.campaign_local_id else None
    adgroup_count = 0
    ad_count = 0
    if campaign is not None:
        adgroup_count = int(db.scalar(
            select(func.count(WebsiteAdsAdGroup.id)).where(
                WebsiteAdsAdGroup.campaign_local_id == campaign.id,
                WebsiteAdsAdGroup.adgroup_id.is_not(None),
            )
        ) or 0)
        ad_count = int(db.scalar(
            select(func.count(WebsiteAdsAd.id)).where(
                WebsiteAdsAd.campaign_local_id == campaign.id,
                WebsiteAdsAd.ad_id.is_not(None),
            )
        ) or 0)
    return {
        "task_id": context.get("execution_task_id"),
        "plan_id": int(plan.id),
        "state": str(plan.status),
        "error": plan.error_message,
        "status_url": (
            f"/api/v1/tenants/{int(workspace_id)}/providers/{provider}/accounts/{int(auth_id)}"
            f"/website-ads/media-plans/{int(plan.id)}/execution"
        ),
        "result": {
            "id": int(plan.id),
            "status": str(plan.status),
            "campaign_local_id": int(campaign.id) if campaign else None,
            "campaign_id": campaign.campaign_id if campaign else None,
            "adgroup_count": adgroup_count,
            "ad_count": ad_count,
        } if str(plan.status) in {"CREATED", "ACTIVE"} else None,
    }


@router.post("/media-plans/generate", status_code=202)
async def create_media_plan(
    workspace_id: int,
    provider: str,
    auth_id: int,
    body: MediaPlanGenerateRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    try:
        product = db.get(WebsiteAdsLandingPage, int(body.landing_page_id))
        if not product or int(product.workspace_id) != int(workspace_id) or not product.is_active:
            raise ValueError("Product is unavailable")
        advertiser_id = resolve_website_ads_advertiser_id(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            provider=provider,
        )
        existing = db.scalar(
            select(WebsiteAdsMediaPlan)
            .where(
                WebsiteAdsMediaPlan.workspace_id == int(workspace_id),
                WebsiteAdsMediaPlan.auth_id == int(auth_id),
                WebsiteAdsMediaPlan.advertiser_id == advertiser_id,
                WebsiteAdsMediaPlan.landing_page_id == int(body.landing_page_id),
                WebsiteAdsMediaPlan.status.in_(("GENERATING", "PROCESSING")),
            )
            .order_by(WebsiteAdsMediaPlan.created_at.desc())
        )
        if existing:
            return _media_plan_generation_response(workspace_id, provider, auth_id, existing)
        task_id = uuid4().hex
        plan = WebsiteAdsMediaPlan(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=advertiser_id,
            landing_page_id=int(body.landing_page_id),
            name=f"{product.title} Hermes 方案生成中"[:512],
            status="GENERATING",
            daily_budget=Decimal(str(body.daily_budget)).quantize(Decimal("0.01")),
            activate_after_create=bool(body.activate_after_create),
            strategy_source="PENDING",
            strategy_summary="Hermes 正在分析商品、素材、受众与实验结构",
            hermes_response_json={
                "generation_task_id": task_id,
                "generation_stage": "QUEUED",
                "generation_recovery_count": 0,
                "generation_requested_at": datetime.now(timezone.utc).isoformat(),
                "generation_request": {
                    "provider": provider,
                    "creative_asset_ids": list(body.creative_asset_ids or []) or None,
                    "request_notes": body.request_notes,
                },
            },
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        try:
            task = _dispatch_media_plan_generation(plan, provider=provider)
        except Exception as exc:
            plan.status = "FAILED"
            plan.error_message = f"QueueError: {exc}"[:8000]
            db.add(plan)
            db.commit()
            raise RuntimeError("Hermes planning queue is unavailable") from exc
        if str(task.id) != task_id:
            task_metadata = dict(plan.hermes_response_json or {})
            task_metadata["generation_task_id"] = str(task.id)
            plan.hermes_response_json = task_metadata
            db.add(plan)
            db.commit()
            db.refresh(plan)
        return _media_plan_generation_response(workspace_id, provider, auth_id, plan)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/media-plans")
def list_media_plans(
    workspace_id: int,
    provider: str,
    auth_id: int,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=100),
    limit: int | None = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    query = select(WebsiteAdsMediaPlan).where(
        WebsiteAdsMediaPlan.workspace_id == workspace_id,
        WebsiteAdsMediaPlan.auth_id == auth_id,
        WebsiteAdsMediaPlan.advertiser_id == advertiser,
    )
    effective_page_size = _legacy_compatible_page_size(
        page_size=page_size,
        limit=limit,
        default=20,
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query
        .order_by(WebsiteAdsMediaPlan.created_at.desc(), WebsiteAdsMediaPlan.id.desc())
        .offset((int(page) - 1) * effective_page_size)
        .limit(effective_page_size)
    ).all()
    return {
        "items": [media_plan_dict(db, row) for row in rows],
        "page": int(page),
        "page_size": effective_page_size,
        "total": int(total),
    }


@router.get("/media-plans/{plan_id}/generation")
def get_media_plan_generation(
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsMediaPlan, int(plan_id))
    if (
        not row
        or int(row.workspace_id) != int(workspace_id)
        or int(row.auth_id) != int(auth_id)
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Media plan not found")
    metadata = dict(row.hermes_response_json or {})
    task_state = _media_plan_task_state(metadata.get("generation_task_id"))
    recovery_reason = _media_plan_generation_recovery_reason(
        status=row.status,
        task_state=task_state,
        age_seconds=_media_plan_generation_age_seconds(row),
        has_task_id=bool(metadata.get("generation_task_id")),
    )
    if recovery_reason:
        row, task_state = _recover_media_plan_generation(
            db,
            plan_id=int(row.id),
            provider=provider,
        )
    payload = _media_plan_generation_response(
        workspace_id,
        provider,
        auth_id,
        row,
        task_state=task_state,
    )
    payload["error"] = row.error_message
    payload["plan"] = media_plan_dict(db, row) if row.status == "READY" else None
    return payload


@router.get("/media-plans/{plan_id}")
def get_media_plan(
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsMediaPlan, plan_id)
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Media plan not found")
    return media_plan_dict(db, row)


@router.post("/media-plans/{plan_id}/execute", status_code=202)
def run_media_plan(
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.scalar(
        select(WebsiteAdsMediaPlan)
        .where(WebsiteAdsMediaPlan.id == int(plan_id))
        .with_for_update()
    )
    if (
        not row
        or row.workspace_id != workspace_id
        or row.auth_id != auth_id
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Media plan not found")
    if str(row.status) in {"EXECUTION_QUEUED", "EXECUTING", "CREATED", "ACTIVE"}:
        return _media_plan_execution_response(db, workspace_id, provider, auth_id, row)
    if str(row.status) != "READY":
        raise HTTPException(status_code=409, detail=f"Media plan cannot be executed from status {row.status}")
    task_id = uuid4().hex
    context = dict(row.execution_context_json or {})
    context["execution_task_id"] = task_id
    context["execution_queued_at"] = datetime.now(timezone.utc).isoformat()
    row.execution_context_json = context
    row.status = "EXECUTION_QUEUED"
    row.error_message = None
    db.add(row)
    db.commit()
    db.refresh(row)
    try:
        task = _website_ads_task("execute_media_plan_task").apply_async(
            kwargs={"plan_id": int(row.id)},
            queue=settings.WEBSITE_ADS_TASK_QUEUE,
            task_id=task_id,
        )
    except Exception as exc:
        row.status = "READY"
        row.error_message = f"QueueError: {exc}"[:8000]
        db.add(row)
        db.commit()
        raise HTTPException(status_code=503, detail="TikTok creation queue is unavailable") from exc
    if str(task.id) != task_id:
        context["execution_task_id"] = str(task.id)
        row.execution_context_json = context
        db.add(row)
        db.commit()
        db.refresh(row)
    return _media_plan_execution_response(db, workspace_id, provider, auth_id, row)


@router.get("/media-plans/{plan_id}/execution")
def get_media_plan_execution(
    workspace_id: int,
    provider: str,
    auth_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    row = db.get(WebsiteAdsMediaPlan, int(plan_id))
    if (
        not row
        or int(row.workspace_id) != int(workspace_id)
        or int(row.auth_id) != int(auth_id)
        or str(row.advertiser_id) != advertiser
    ):
        raise HTTPException(status_code=404, detail="Media plan not found")
    stale_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=25)
    if str(row.status) in {"EXECUTION_QUEUED", "EXECUTING"} and row.updated_at < stale_cutoff:
        row.status = "FAILED"
        row.error_message = "TikTok media plan execution exceeded 25 minutes"
        db.add(row)
        db.commit()
        db.refresh(row)
    return _media_plan_execution_response(db, workspace_id, provider, auth_id, row)


@router.post("/launch", status_code=201)
async def launch(
    workspace_id: int,
    provider: str,
    auth_id: int,
    body: WebsiteAdLaunchRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    async def execute_mutation():
        return await launch_website_ad(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            provider=provider,
            request=body,
            require_execution_lease=True,
        )

    return await _run_locked_manual_mutation(
        db,
        workspace_id=workspace_id,
        operation="manual_launch",
        mutation=execute_mutation,
    )


@router.get("/campaigns")
def list_campaigns(
    workspace_id: int,
    provider: str,
    auth_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    base = select(WebsiteAdsCampaign).where(
        WebsiteAdsCampaign.workspace_id == workspace_id,
        WebsiteAdsCampaign.auth_id == auth_id,
        WebsiteAdsCampaign.advertiser_id == advertiser,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    campaigns = db.scalars(
        base.order_by(
            WebsiteAdsCampaign.created_at.desc(),
            WebsiteAdsCampaign.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for campaign in campaigns:
        landing = db.get(WebsiteAdsLandingPage, campaign.landing_page_id)
        adgroups = db.scalars(select(WebsiteAdsAdGroup).where(WebsiteAdsAdGroup.campaign_local_id == campaign.id)).all()
        ads = db.scalars(select(WebsiteAdsAd).where(WebsiteAdsAd.campaign_local_id == campaign.id)).all()
        metric_filter = [WebsiteAdsAd.campaign_local_id == campaign.id]
        if start_date:
            metric_filter.append(WebsiteAdsMetricHourly.stat_hour >= datetime.combine(start_date, time.min))
        if end_date:
            metric_filter.append(WebsiteAdsMetricHourly.stat_hour <= datetime.combine(end_date, time.max))
        campaign_metrics, last_synced = _metric_summary(db, metric_filter, join_ads=True)
        ad_items = []
        for row in ads:
            row_filter = [WebsiteAdsMetricHourly.ad_local_id == row.id]
            if start_date:
                row_filter.append(WebsiteAdsMetricHourly.stat_hour >= datetime.combine(start_date, time.min))
            if end_date:
                row_filter.append(WebsiteAdsMetricHourly.stat_hour <= datetime.combine(end_date, time.max))
            ad_metrics, _ = _metric_summary(db, row_filter, join_ads=False)
            ad_items.append({
                "id": row.id,
                "ad_id": row.ad_id,
                "adgroup_local_id": row.adgroup_local_id,
                "name": row.name,
                "status": row.operation_status,
                "video_id": row.video_id,
                "guard_enabled": row.guard_enabled,
                "last_checked_at": row.last_checked_at,
                "metrics": ad_metrics,
            })
        adgroup_items = []
        for row in adgroups:
            group_filter = [WebsiteAdsAd.adgroup_local_id == row.id]
            if start_date:
                group_filter.append(WebsiteAdsMetricHourly.stat_hour >= datetime.combine(start_date, time.min))
            if end_date:
                group_filter.append(WebsiteAdsMetricHourly.stat_hour <= datetime.combine(end_date, time.max))
            group_metrics, _ = _metric_summary(db, group_filter, join_ads=True)
            targeting = dict(row.targeting_json or {})
            adgroup_items.append({
                "id": row.id,
                "adgroup_id": row.adgroup_id,
                "name": row.name,
                "status": row.operation_status,
                "budget": float(row.budget),
                "bid_type": row.bid_type,
                "conversion_bid_price": float(row.conversion_bid_price) if row.conversion_bid_price is not None else None,
                "schedule_start_time": row.schedule_start_time,
                "targeting": targeting,
                "audience_segment": targeting.get("audience_segment"),
                "metrics": group_metrics,
            })
        items.append(
            {
                "id": campaign.id,
                "campaign_id": campaign.campaign_id,
                "name": campaign.name,
                "status": campaign.local_status,
                "operation_status": campaign.operation_status,
                "advertiser_id": campaign.advertiser_id,
                "landing_page": _landing_dict(db, landing) if landing else None,
                "adgroups": adgroup_items,
                "ads": ad_items,
                "metrics": campaign_metrics,
                "last_metrics_sync_at": last_synced,
                "error_message": campaign.error_message,
                "created_at": campaign.created_at,
            }
        )
    return {"items": items, "page": page, "page_size": page_size, "total": int(total)}


@router.post("/campaigns/{campaign_local_id}/status")
async def update_campaign_status(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_local_id: int,
    body: StatusUpdateRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    async def execute_mutation():
        advertiser = _request_advertiser_id(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
        )
        campaign = db.scalar(
            select(WebsiteAdsCampaign).where(
                WebsiteAdsCampaign.id == int(campaign_local_id),
                WebsiteAdsCampaign.workspace_id == int(workspace_id),
                WebsiteAdsCampaign.auth_id == int(auth_id),
                WebsiteAdsCampaign.advertiser_id == str(advertiser),
                WebsiteAdsCampaign.campaign_id.is_not(None),
            )
        )
        if campaign is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        adgroups = list(
            db.scalars(
                select(WebsiteAdsAdGroup).where(
                    WebsiteAdsAdGroup.campaign_local_id == int(campaign.id)
                )
            ).all()
        )
        ads = list(
            db.scalars(
                select(WebsiteAdsAd).where(
                    WebsiteAdsAd.campaign_local_id == int(campaign.id)
                )
            ).all()
        )

        async def mutate(client):
            assert_website_ads_execution_lock(db, required=True)
            response = await client.update_campaign_status(
                campaign.advertiser_id, [str(campaign.campaign_id)], body.operation_status
            )
            if body.operation_status == "ENABLE":
                group_ids = [str(row.adgroup_id) for row in adgroups if row.adgroup_id]
                ad_ids = [str(row.ad_id) for row in ads if row.ad_id]
                if group_ids:
                    assert_website_ads_execution_lock(db, required=True)
                    await client.update_adgroup_status(campaign.advertiser_id, group_ids, "ENABLE")
                if ad_ids:
                    assert_website_ads_execution_lock(db, required=True)
                    await client.update_ad_status(campaign.advertiser_id, ad_ids, "ENABLE")
            return response

        response = await _with_api(db, workspace_id, provider, auth_id, mutate)
        assert_website_ads_execution_lock(db, required=True)
        campaign.operation_status = body.operation_status
        campaign.local_status = "ACTIVE" if body.operation_status == "ENABLE" else "PAUSED"
        for row in adgroups:
            row.operation_status = body.operation_status
            db.add(row)
        for row in ads:
            row.operation_status = body.operation_status
            db.add(row)
        apply_manual_campaign_override(
            db,
            campaign=campaign,
            operation_status=body.operation_status,
        )
        db.add(campaign)
        db.add(WebsiteAdsActionLog(
            workspace_id=workspace_id,
            auth_id=auth_id,
            actor_type="USER",
            action="ENABLE_CAMPAIGN" if body.operation_status == "ENABLE" else "PAUSE_CAMPAIGN",
            reason=body.reason,
            result="SUCCESS",
            request_json={
                "campaign_local_id": int(campaign.id),
                "operation_status": body.operation_status,
                "manual_override": True,
            },
            response_json=response,
        ))
        assert_website_ads_execution_lock(db, required=True)
        db.commit()
        return {"id": campaign.id, "operation_status": campaign.operation_status, "status": campaign.local_status}

    return await _run_locked_manual_mutation(
        db,
        workspace_id=workspace_id,
        operation="manual_campaign_status",
        mutation=execute_mutation,
    )


@router.patch("/adgroups/{adgroup_local_id}/delivery")
async def update_adgroup_delivery(
    workspace_id: int,
    provider: str,
    auth_id: int,
    adgroup_local_id: int,
    body: AdGroupDeliveryUpdateRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    async def execute_mutation():
        advertiser = _request_advertiser_id(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
        )
        scoped_row = db.execute(
            select(WebsiteAdsAdGroup, WebsiteAdsCampaign)
            .join(
                WebsiteAdsCampaign,
                WebsiteAdsCampaign.id
                == WebsiteAdsAdGroup.campaign_local_id,
            )
            .where(
                WebsiteAdsAdGroup.id == int(adgroup_local_id),
                WebsiteAdsAdGroup.adgroup_id.is_not(None),
                WebsiteAdsCampaign.workspace_id == int(workspace_id),
                WebsiteAdsCampaign.auth_id == int(auth_id),
                WebsiteAdsCampaign.advertiser_id == str(advertiser),
            )
        ).first()
        if scoped_row is None:
            raise HTTPException(status_code=404, detail="Ad group not found")
        adgroup, campaign = scoped_row

        async def mutate(client):
            responses = {}
            if body.daily_budget is not None:
                assert_website_ads_execution_lock(db, required=True)
                responses["budget"] = await client.update_adgroup_budget(
                    campaign.advertiser_id, str(adgroup.adgroup_id), body.daily_budget
                )
            if body.conversion_bid_price is not None:
                assert_website_ads_execution_lock(db, required=True)
                responses["bid"] = await client.update_adgroup({
                    "advertiser_id": campaign.advertiser_id,
                    "adgroup_id": str(adgroup.adgroup_id),
                    "conversion_bid_price": body.conversion_bid_price,
                })
            return responses

        response = await _with_api(db, workspace_id, provider, auth_id, mutate)
        assert_website_ads_execution_lock(db, required=True)
        if body.daily_budget is not None:
            adgroup.budget = Decimal(str(body.daily_budget))
        if body.conversion_bid_price is not None:
            adgroup.conversion_bid_price = Decimal(str(body.conversion_bid_price))
        db.add(adgroup)
        db.add(WebsiteAdsActionLog(
            workspace_id=workspace_id,
            auth_id=auth_id,
            actor_type="USER",
            action="UPDATE_ADGROUP_DELIVERY",
            reason="Updated from Website Ads console",
            result="SUCCESS",
            request_json={
                **body.model_dump(exclude_none=True),
                "campaign_local_id": int(campaign.id),
                "adgroup_local_id": int(adgroup.id),
            },
            response_json=response,
        ))
        assert_website_ads_execution_lock(db, required=True)
        db.commit()
        return {
            "id": adgroup.id,
            "daily_budget": float(adgroup.budget),
            "conversion_bid_price": float(adgroup.conversion_bid_price) if adgroup.conversion_bid_price is not None else None,
        }

    return await _run_locked_manual_mutation(
        db,
        workspace_id=workspace_id,
        operation="manual_adgroup_delivery",
        mutation=execute_mutation,
    )


@router.get("/actions")
def list_actions(
    workspace_id: int,
    provider: str,
    auth_id: int,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=200),
    limit: int | None = Query(None, ge=1, le=200),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    effective_page_size = _legacy_compatible_page_size(
        page_size=page_size,
        limit=limit,
        default=30,
    )
    scoped_actions = _scoped_action_query(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser,
    )
    total = db.scalar(
        select(func.count()).select_from(scoped_actions.subquery())
    ) or 0
    rows = db.scalars(
        scoped_actions
        .order_by(WebsiteAdsActionLog.created_at.desc(), WebsiteAdsActionLog.id.desc())
        .offset((page - 1) * effective_page_size)
        .limit(effective_page_size)
    ).all()
    return {
        "items": [{
            "id": row.id,
            "ad_local_id": row.ad_local_id,
            "actor_type": row.actor_type,
            "action": row.action,
            "reason": row.reason,
            "result": row.result,
            "metrics": row.metrics_json,
            "created_at": row.created_at,
        } for row in rows],
        "page": page,
        "page_size": effective_page_size,
        "total": int(total),
    }


@router.get("/reports/daily")
def list_daily_reports(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_local_id: int | None = Query(None, ge=1),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=200),
    limit: int | None = Query(None, ge=1, le=200),
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_member),
):
    advertiser = _request_advertiser_id(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )
    query = select(WebsiteAdsDailyReport).where(
        WebsiteAdsDailyReport.workspace_id == int(workspace_id),
        WebsiteAdsDailyReport.auth_id == int(auth_id),
        WebsiteAdsDailyReport.advertiser_id == advertiser,
    )
    if campaign_local_id is not None:
        query = query.where(WebsiteAdsDailyReport.campaign_local_id == int(campaign_local_id))
    if start_date is not None:
        query = query.where(WebsiteAdsDailyReport.report_date >= start_date)
    if end_date is not None:
        query = query.where(WebsiteAdsDailyReport.report_date <= end_date)
    effective_page_size = _legacy_compatible_page_size(
        page_size=page_size,
        limit=limit,
        default=30,
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        db.scalars(
            query
            .order_by(WebsiteAdsDailyReport.report_date.desc(), WebsiteAdsDailyReport.id.desc())
            .offset((int(page) - 1) * effective_page_size)
            .limit(effective_page_size)
        ).all()
    )
    campaigns = {
        int(row.id): row
        for row in db.scalars(
            select(WebsiteAdsCampaign).where(
                WebsiteAdsCampaign.id.in_({int(item.campaign_local_id) for item in rows} or {-1}),
                WebsiteAdsCampaign.workspace_id == int(workspace_id),
                WebsiteAdsCampaign.auth_id == int(auth_id),
                WebsiteAdsCampaign.advertiser_id == advertiser,
            )
        ).all()
    }
    return {
        "items": [
            {
                "id": int(row.id),
                "campaign_local_id": int(row.campaign_local_id),
                "campaign_name": (
                    campaigns[int(row.campaign_local_id)].name
                    if int(row.campaign_local_id) in campaigns
                    else None
                ),
                "report_date": row.report_date,
                "advertiser_timezone": row.advertiser_timezone,
                "status": row.status,
                "metrics": row.metrics_json,
                "audience_performance": row.audience_performance_json,
                "gmv_signal": row.gmv_signal_json,
                "actions": row.action_summary_json,
                "hermes_report": row.hermes_report_json,
                "report_text": row.report_text,
                "source_freshness": row.source_freshness_json,
                "generated_at": row.generated_at,
            }
            for row in rows
        ],
        "page": int(page),
        "page_size": effective_page_size,
        "total": int(total),
    }


@router.post("/ads/{ad_local_id}/status")
async def update_ad_status(
    workspace_id: int,
    provider: str,
    auth_id: int,
    ad_local_id: int,
    body: StatusUpdateRequest,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    async def execute_mutation():
        advertiser = _request_advertiser_id(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
        )
        scoped_row = db.execute(
            select(WebsiteAdsAd, WebsiteAdsCampaign)
            .join(
                WebsiteAdsCampaign,
                WebsiteAdsCampaign.id == WebsiteAdsAd.campaign_local_id,
            )
            .where(
                WebsiteAdsAd.id == int(ad_local_id),
                WebsiteAdsAd.ad_id.is_not(None),
                WebsiteAdsCampaign.workspace_id == int(workspace_id),
                WebsiteAdsCampaign.auth_id == int(auth_id),
                WebsiteAdsCampaign.advertiser_id == str(advertiser),
            )
        ).first()
        if scoped_row is None:
            raise HTTPException(status_code=404, detail="Ad not found")
        ad, campaign = scoped_row
        adgroup = db.scalar(
            select(WebsiteAdsAdGroup).where(
                WebsiteAdsAdGroup.id == int(ad.adgroup_local_id),
                WebsiteAdsAdGroup.campaign_local_id == int(campaign.id),
            )
        )

        async def mutate(client):
            assert_website_ads_execution_lock(db, required=True)
            responses = {
                "ad": await client.update_ad_status(
                    campaign.advertiser_id,
                    [str(ad.ad_id)],
                    body.operation_status,
                )
            }
            if body.operation_status == "ENABLE":
                if adgroup and adgroup.adgroup_id:
                    assert_website_ads_execution_lock(db, required=True)
                    responses["adgroup"] = await client.update_adgroup_status(
                        campaign.advertiser_id, [str(adgroup.adgroup_id)], "ENABLE"
                    )
                if campaign.campaign_id:
                    assert_website_ads_execution_lock(db, required=True)
                    responses["campaign"] = await client.update_campaign_status(
                        campaign.advertiser_id, [str(campaign.campaign_id)], "ENABLE"
                    )
            return responses

        response = await _with_api(db, workspace_id, provider, auth_id, mutate)
        assert_website_ads_execution_lock(db, required=True)
        ad.operation_status = body.operation_status
        if body.operation_status == "ENABLE":
            campaign.local_status = "ACTIVE"
            campaign.operation_status = "ENABLE"
            if adgroup:
                adgroup.operation_status = "ENABLE"
                db.add(adgroup)
        else:
            enabled_siblings = db.scalar(
                select(func.count()).select_from(WebsiteAdsAd).where(
                    WebsiteAdsAd.campaign_local_id == campaign.id,
                    WebsiteAdsAd.id != ad.id,
                    WebsiteAdsAd.operation_status == "ENABLE",
                )
            ) or 0
            if enabled_siblings == 0:
                campaign.local_status = "PAUSED"
        db.add(campaign)
        db.add(ad)
        db.add(
            WebsiteAdsActionLog(
                workspace_id=workspace_id,
                auth_id=auth_id,
                ad_local_id=ad.id,
                actor_type="USER",
                action="ENABLE_AD" if body.operation_status == "ENABLE" else "PAUSE_AD",
                reason=body.reason,
                result="SUCCESS",
                response_json=response,
            )
        )
        assert_website_ads_execution_lock(db, required=True)
        db.commit()
        return {"id": ad.id, "operation_status": ad.operation_status}

    return await _run_locked_manual_mutation(
        db,
        workspace_id=workspace_id,
        operation="manual_ad_status",
        mutation=execute_mutation,
    )


@router.post("/monitor/run")
async def run_monitor(
    workspace_id: int,
    db: Session = Depends(get_db),
    _: SessionUser = Depends(require_tenant_admin),
):
    result = await run_website_ads_monitor_cycle(db, workspace_id=workspace_id)
    result["requested_workspace_id"] = workspace_id
    return result
