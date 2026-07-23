from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import FileResponse

from app.celery_app import WHISPER_TASK_QUEUE, celery_app
from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.core.security import client_ip
from app.data.db import get_db
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount, OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopCategory,
    TikTokShopCoupon,
    TikTokShopFinanceStatement,
    TikTokShopFinanceTransaction,
    TikTokShopGlobalProduct,
    TikTokShopLiveDailyMetric,
    TikTokShopOrder,
    TikTokShopOrderFinanceSummary,
    TikTokShopOrderLine,
    TikTokShopPayment,
    TikTokShopProduct,
    TikTokShopProductChannelDailyMetric,
    TikTokShopProductDailyMetric,
    TikTokShopPromotionActivity,
    TikTokShopShopHourlyMetric,
    TikTokShopSku,
    TikTokShopSkuDailyMetric,
    TikTokShopSyncRun,
    TikTokShopUnsettledTransaction,
    TikTokShopVideoDailyMetric,
    TikTokShopVideoContentAnalysis,
    TikTokShopVideoOverviewDailyMetric,
    TikTokShopWithdrawal,
)
from app.services.audit import log_event
from app.services.gmvmax_creative_media_cache import resolve_creative_media
from app.services.tiktok_shop_api import TikTokShopAPIClient
from app.services.tiktok_shop_sync import SUPPORTED_DOMAINS, serialize_model, shop_today
from app.services.tiktok_shop_video_analysis import (
    PROMPT_VERSION as VIDEO_ANALYSIS_PROMPT_VERSION,
    PROVIDER_MODEL as VIDEO_ANALYSIS_PROVIDER_MODEL,
    analysis_cache_key,
    build_metric_packet,
    find_media_identity,
    serialize_analysis,
    utcnow,
)


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/tiktok-shop",
    tags=["Tenant / TikTok Shop Data"],
)


class SyncRequest(BaseModel):
    shop_id: int = Field(gt=0)
    domains: list[str] = Field(default_factory=lambda: sorted(SUPPORTED_DOMAINS), min_length=1)
    start_date: date | None = None
    end_date_exclusive: date | None = None


class VideoMediaLookupRequest(BaseModel):
    shop_id: int = Field(gt=0)
    video_ids: list[str] = Field(min_length=1, max_length=500)


class VideoAnalysisLookupRequest(BaseModel):
    shop_id: int = Field(gt=0)
    video_ids: list[str] = Field(min_length=1, max_length=500)
    start_date: date
    end_date_exclusive: date


class VideoAnalysisRequest(BaseModel):
    shop_id: int = Field(gt=0)
    video_id: str = Field(min_length=1, max_length=128)
    start_date: date
    end_date_exclusive: date
    retry_failed: bool = False


class PromotionActivityUpdateRequest(BaseModel):
    confirm: bool = False
    changes: dict[str, Any] = Field(min_length=1)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _get_shop(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int,
    active_only: bool = False,
) -> OAuthTikTokShopShop:
    query = select(OAuthTikTokShopShop).where(
        OAuthTikTokShopShop.id == int(shop_id),
        OAuthTikTokShopShop.workspace_id == int(workspace_id),
    )
    if active_only:
        query = query.where(OAuthTikTokShopShop.is_active.is_(True))
    shop = db.scalar(query)
    if not shop:
        raise APIError("TIKTOK_SHOP_NOT_FOUND", "TikTok Shop not found.", 404)
    return shop


def _page(
    statement: Any,
    db: Session,
    *,
    page: int,
    page_size: int,
    serializer: Any = serialize_model,
) -> dict[str, Any]:
    total = int(db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)
    rows = db.execute(
        statement.offset((int(page) - 1) * int(page_size)).limit(int(page_size))
    ).scalars().all()
    return {
        "items": [serializer(row) for row in rows],
        "page": int(page),
        "page_size": int(page_size),
        "total": total,
    }


def _local_bounds(
    shop: OAuthTikTokShopShop,
    start_date: date | None,
    end_date_exclusive: date | None,
    *,
    default_days: int,
) -> tuple[datetime, datetime]:
    end = end_date_exclusive or (shop_today(shop) + timedelta(days=1))
    start = start_date or (end - timedelta(days=default_days))
    if start >= end or (end - start).days > 365:
        raise APIError("INVALID_DATE_RANGE", "Date range must be between 1 and 365 days.", 400)
    try:
        zone = ZoneInfo(shop.timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Etc/GMT+8")
    start_utc = datetime.combine(start, dt_time.min, zone).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(end, dt_time.min, zone).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


@router.get("/capabilities")
def capabilities(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    accounts = db.execute(
        select(OAuthTikTokShopAccount).where(
            OAuthTikTokShopAccount.workspace_id == int(workspace_id),
            OAuthTikTokShopAccount.status == "active",
        )
    ).scalars().all()
    granted = sorted(
        {
            str(scope)
            for account in accounts
            for scope in (account.granted_scopes_json or [])
        }
    )
    required = {
        "catalog": ["seller.product.basic"],
        "orders": ["seller.order.info"],
        "finance": ["seller.finance.info"],
        "promotions": ["seller.promotion.info"],
        "promotion_writes": ["seller.promotion.write"],
        "analytics": ["data.shop_analytics.public.read"],
        "global_products": ["seller.global_product.info"],
        "shop_metadata": ["seller.authorization.info", "seller.shop.info"],
    }
    return {
        "granted_scopes": granted,
        "domains": {
            name: {
                "available": all(scope in granted for scope in scopes),
                "required_scopes": scopes,
            }
            for name, scopes in required.items()
        },
        "promotion_writes_enabled": bool(
            getattr(settings, "TT_SHOP_PROMOTION_WRITES_ENABLED", False)
        ),
        "video_content_analysis": {
            "enabled": bool(settings.HERMES_VIDEO_ANALYST_AGENT_ENABLED),
            "model": VIDEO_ANALYSIS_PROVIDER_MODEL,
            "visual_detail": "low",
            "max_frames": max(1, min(8, int(settings.HERMES_VIDEO_ANALYSIS_MAX_FRAMES))),
            "prompt_version": VIDEO_ANALYSIS_PROMPT_VERSION,
        },
    }


@router.get("/shops")
def shops(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.execute(
        select(OAuthTikTokShopShop)
        .where(OAuthTikTokShopShop.workspace_id == int(workspace_id))
        .order_by(
            OAuthTikTokShopShop.is_active.desc(),
            OAuthTikTokShopShop.shop_name.asc(),
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": int(row.id),
                "account_id": int(row.account_id),
                "shop_id": row.shop_id,
                "shop_code": row.shop_code,
                "shop_name": row.shop_name,
                "region": row.region,
                "timezone_name": row.timezone_name,
                "seller_type": row.seller_type,
                "status": row.status,
                "is_active": bool(row.is_active),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            }
            for row in rows
        ]
    }


@router.get("/categories")
def categories(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    leaf_only: bool = False,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = select(TikTokShopCategory).where(
        TikTokShopCategory.workspace_id == int(workspace_id),
        TikTokShopCategory.shop_row_id == int(shop_id),
    )
    if leaf_only:
        query = query.where(TikTokShopCategory.is_leaf.is_(True))
    rows = db.execute(query.order_by(TikTokShopCategory.local_name.asc())).scalars().all()
    return {"items": [serialize_model(row) for row in rows], "total": len(rows)}


@router.get("/products")
def products(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    search: str | None = Query(default=None, max_length=255),
    status: str | None = Query(default=None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = select(TikTokShopProduct).where(
        TikTokShopProduct.workspace_id == int(workspace_id),
        TikTokShopProduct.shop_row_id == int(shop_id),
    )
    if status:
        query = query.where(TikTokShopProduct.status == status)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                TikTokShopProduct.product_id.like(pattern),
                TikTokShopProduct.title.like(pattern),
            )
        )
    return _page(
        query.order_by(TikTokShopProduct.provider_updated_at.desc(), TikTokShopProduct.id.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.get("/products/{product_id}")
def product_detail(
    workspace_id: int,
    product_id: str,
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    product = db.scalar(
        select(TikTokShopProduct).where(
            TikTokShopProduct.workspace_id == int(workspace_id),
            TikTokShopProduct.shop_row_id == int(shop_id),
            TikTokShopProduct.product_id == str(product_id),
        )
    )
    if not product:
        raise APIError("TIKTOK_SHOP_PRODUCT_NOT_FOUND", "Product not found.", 404)
    skus = db.execute(
        select(TikTokShopSku)
        .where(
            TikTokShopSku.workspace_id == int(workspace_id),
            TikTokShopSku.shop_row_id == int(shop_id),
            TikTokShopSku.product_id == str(product_id),
        )
        .order_by(TikTokShopSku.sku_id.asc())
    ).scalars().all()
    return {
        "product": serialize_model(product),
        "skus": [serialize_model(row) for row in skus],
    }


@router.get("/orders")
def orders(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    status: str | None = Query(default=None, max_length=64),
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    start, end = _local_bounds(shop, start_date, end_date_exclusive, default_days=30)
    query = select(TikTokShopOrder).where(
        TikTokShopOrder.workspace_id == int(workspace_id),
        TikTokShopOrder.shop_row_id == int(shop_id),
        TikTokShopOrder.provider_created_at >= start,
        TikTokShopOrder.provider_created_at < end,
    )
    if status:
        query = query.where(TikTokShopOrder.status == status)
    return _page(
        query.order_by(TikTokShopOrder.provider_created_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.get("/orders/{order_id}")
def order_detail(
    workspace_id: int,
    order_id: str,
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    order = db.scalar(
        select(TikTokShopOrder).where(
            TikTokShopOrder.workspace_id == int(workspace_id),
            TikTokShopOrder.shop_row_id == int(shop_id),
            TikTokShopOrder.order_id == str(order_id),
        )
    )
    if not order:
        raise APIError("TIKTOK_SHOP_ORDER_NOT_FOUND", "Order not found.", 404)
    lines = db.execute(
        select(TikTokShopOrderLine)
        .where(
            TikTokShopOrderLine.workspace_id == int(workspace_id),
            TikTokShopOrderLine.shop_row_id == int(shop_id),
            TikTokShopOrderLine.order_id == str(order_id),
        )
        .order_by(TikTokShopOrderLine.id.asc())
    ).scalars().all()
    return {
        "order": serialize_model(order),
        "line_items": [serialize_model(row) for row in lines],
    }


@router.get("/finance/statements")
def finance_statements(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    start, end = _local_bounds(shop, start_date, end_date_exclusive, default_days=90)
    query = (
        select(TikTokShopFinanceStatement)
        .where(
            TikTokShopFinanceStatement.workspace_id == int(workspace_id),
            TikTokShopFinanceStatement.shop_row_id == int(shop_id),
            TikTokShopFinanceStatement.statement_time >= start,
            TikTokShopFinanceStatement.statement_time < end,
        )
        .order_by(TikTokShopFinanceStatement.statement_time.desc())
    )
    return _page(query, db, page=page, page_size=page_size)


@router.get("/finance/transactions")
def finance_transactions(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    statement_id: str | None = Query(default=None, max_length=128),
    order_id: str | None = Query(default=None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = select(TikTokShopFinanceTransaction).where(
        TikTokShopFinanceTransaction.workspace_id == int(workspace_id),
        TikTokShopFinanceTransaction.shop_row_id == int(shop_id),
    )
    if statement_id:
        query = query.where(TikTokShopFinanceTransaction.statement_id == statement_id)
    if order_id:
        query = query.where(TikTokShopFinanceTransaction.order_id == order_id)
    return _page(
        query.order_by(TikTokShopFinanceTransaction.order_created_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.get("/finance/order-summaries")
def finance_order_summaries(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = (
        select(TikTokShopOrderFinanceSummary)
        .where(
            TikTokShopOrderFinanceSummary.workspace_id == int(workspace_id),
            TikTokShopOrderFinanceSummary.shop_row_id == int(shop_id),
        )
        .order_by(TikTokShopOrderFinanceSummary.order_created_at.desc())
    )
    return _page(query, db, page=page, page_size=page_size)


@router.get("/finance/withdrawals")
def withdrawals(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = (
        select(TikTokShopWithdrawal)
        .where(
            TikTokShopWithdrawal.workspace_id == int(workspace_id),
            TikTokShopWithdrawal.shop_row_id == int(shop_id),
        )
        .order_by(TikTokShopWithdrawal.provider_created_at.desc())
    )
    return _page(query, db, page=page, page_size=page_size)


@router.get("/finance/payments")
def payments(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    start, end = _local_bounds(shop, start_date, end_date_exclusive, default_days=90)
    query = (
        select(TikTokShopPayment)
        .where(
            TikTokShopPayment.workspace_id == int(workspace_id),
            TikTokShopPayment.shop_row_id == int(shop_id),
            TikTokShopPayment.provider_created_at >= start,
            TikTokShopPayment.provider_created_at < end,
        )
        .order_by(TikTokShopPayment.provider_created_at.desc())
    )
    return _page(query, db, page=page, page_size=page_size)


@router.get("/finance/unsettled-transactions")
def unsettled_transactions(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    order_id: str | None = Query(default=None, max_length=128),
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    start, end = _local_bounds(shop, start_date, end_date_exclusive, default_days=90)
    query = select(TikTokShopUnsettledTransaction).where(
        TikTokShopUnsettledTransaction.workspace_id == int(workspace_id),
        TikTokShopUnsettledTransaction.shop_row_id == int(shop_id),
        TikTokShopUnsettledTransaction.order_created_at >= start,
        TikTokShopUnsettledTransaction.order_created_at < end,
    )
    if order_id:
        query = query.where(TikTokShopUnsettledTransaction.order_id == str(order_id))
    return _page(
        query.order_by(TikTokShopUnsettledTransaction.order_created_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.get("/promotions/activities")
def activities(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    status: str | None = Query(default=None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = select(TikTokShopPromotionActivity).where(
        TikTokShopPromotionActivity.workspace_id == int(workspace_id),
        TikTokShopPromotionActivity.shop_row_id == int(shop_id),
    )
    if status:
        query = query.where(TikTokShopPromotionActivity.status == status)
    return _page(
        query.order_by(TikTokShopPromotionActivity.provider_updated_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.put("/promotions/activities/{activity_id}")
async def update_activity(
    workspace_id: int,
    activity_id: str,
    payload: PromotionActivityUpdateRequest,
    request: Request,
    shop_id: int = Query(..., gt=0),
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(
        db,
        workspace_id=workspace_id,
        shop_id=shop_id,
        active_only=True,
    )
    if not payload.confirm:
        raise APIError(
            "TIKTOK_SHOP_CONFIRMATION_REQUIRED",
            "Explicit confirmation is required for promotion changes.",
            409,
        )
    if not bool(getattr(settings, "TT_SHOP_PROMOTION_WRITES_ENABLED", False)):
        raise APIError(
            "TIKTOK_SHOP_WRITES_DISABLED",
            "TikTok Shop promotion writes are disabled by platform policy.",
            409,
        )
    client = await TikTokShopAPIClient.create(
        db,
        workspace_id=int(workspace_id),
        account_id=int(shop.account_id),
        shop_row_id=int(shop.id),
    )
    async with client:
        result = await client.update_activity(str(activity_id), payload.changes)
    activity = db.scalar(
        select(TikTokShopPromotionActivity).where(
            TikTokShopPromotionActivity.workspace_id == int(workspace_id),
            TikTokShopPromotionActivity.shop_row_id == int(shop.id),
            TikTokShopPromotionActivity.activity_id == str(activity_id),
        )
    )
    log_event(
        db,
        action="tiktok_shop.promotion_activity_updated",
        resource_type="tiktok_shop_promotion_activity",
        resource_id=int(activity.id) if activity else None,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={
            "provider_activity_id": str(activity_id),
            "changed_fields": sorted(payload.changes),
            "provider_request_id": result.request_id,
        },
    )
    db.commit()
    sync_task = celery_app.send_task(
        "tiktok_shop.sync_domain",
        kwargs={
            "workspace_id": int(workspace_id),
            "account_id": int(shop.account_id),
            "shop_row_id": int(shop.id),
            "domain": "promotions",
            "trigger": "write_followup",
        },
        queue="tiktok_shop",
        countdown=2,
    )
    return {
        "status": "updated",
        "activity_id": str(activity_id),
        "provider_request_id": result.request_id,
        "sync_task_id": str(sync_task.id),
    }


@router.get("/promotions/coupons")
def coupons(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    status: str | None = Query(default=None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    query = select(TikTokShopCoupon).where(
        TikTokShopCoupon.workspace_id == int(workspace_id),
        TikTokShopCoupon.shop_row_id == int(shop_id),
    )
    if status:
        query = query.where(TikTokShopCoupon.status == status)
    return _page(
        query.order_by(TikTokShopCoupon.provider_updated_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


_ANALYTICS_MODELS = {
    "shop-hourly": (TikTokShopShopHourlyMetric, "hour_index"),
    "video-overview": (TikTokShopVideoOverviewDailyMetric, "gmv"),
    "videos": (TikTokShopVideoDailyMetric, "gmv"),
    "products": (TikTokShopProductDailyMetric, "gmv"),
    "product-channels": (TikTokShopProductChannelDailyMetric, "gmv"),
    "skus": (TikTokShopSkuDailyMetric, "gmv"),
    "lives": (TikTokShopLiveDailyMetric, "gmv"),
}


@router.get("/analytics/{dataset}")
def analytics(
    workspace_id: int,
    dataset: str,
    shop_id: int = Query(..., gt=0),
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    product_id: str | None = Query(default=None, max_length=128),
    channel: str | None = Query(default=None, max_length=48),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
    if dataset not in _ANALYTICS_MODELS:
        raise APIError("INVALID_ANALYTICS_DATASET", "Unknown analytics dataset.", 404)
    model, order_column = _ANALYTICS_MODELS[dataset]
    end = end_date_exclusive or (shop_today(shop) + timedelta(days=1))
    start = start_date or (end - timedelta(days=7))
    if start >= end or (end - start).days > 366:
        raise APIError("INVALID_DATE_RANGE", "Invalid analytics date range.", 400)
    query = (
        select(model)
        .where(
            model.workspace_id == int(workspace_id),
            model.shop_row_id == int(shop_id),
            model.report_date >= start,
            model.report_date < end,
        )
    )
    if product_id:
        if not hasattr(model, "product_id"):
            raise APIError(
                "INVALID_ANALYTICS_FILTER",
                "product_id is not supported for this analytics dataset.",
                400,
            )
        query = query.where(model.product_id == str(product_id))
    if channel:
        if model is not TikTokShopProductChannelDailyMetric:
            raise APIError(
                "INVALID_ANALYTICS_FILTER",
                "channel is only supported by the product-channels dataset.",
                400,
            )
        query = query.where(TikTokShopProductChannelDailyMetric.channel == str(channel))
    # Offset pagination must have a total order. Analytics rows commonly tie on
    # both date and metric (especially zero-GMV videos); without the primary-key
    # tiebreaker MySQL may move tied rows across page boundaries between SELECTs.
    query = query.order_by(
        model.report_date.desc(),
        getattr(model, order_column).desc(),
        model.id.desc(),
    )
    current_shop_date = shop_today(shop)

    def analytics_serializer(row: Any) -> dict[str, Any]:
        item = serialize_model(row)
        if model is not TikTokShopVideoOverviewDailyMetric:
            return item
        raw = _json_mapping(getattr(row, "raw_json", None))
        meta = _json_mapping(raw.get("_gmv_ops_meta"))
        item.update(
            {
                "data_source": meta.get("source") or "shop_video_overview",
                "is_provisional": bool(
                    meta.get("provisional")
                    if "provisional" in meta
                    else row.report_date == current_shop_date
                ),
                "latest_available_date": meta.get("latest_available_date"),
                "provider_request_id": meta.get("provider_request_id"),
                "fallback_reason": meta.get("fallback_error"),
                "ctr_definition": meta.get("ctr_definition")
                or "product_clicks_divided_by_video_views",
            }
        )
        return item

    result = _page(
        query,
        db,
        page=page,
        page_size=page_size,
        serializer=analytics_serializer,
    )
    result["start_date"] = start.isoformat()
    result["end_date_exclusive"] = end.isoformat()
    result["timezone_name"] = shop.timezone_name
    result["data_meta"] = {
        "shop_today": current_shop_date.isoformat(),
        "mutable_date": current_shop_date.isoformat(),
        "stable_through_date": (current_shop_date - timedelta(days=1)).isoformat(),
        "includes_mutable_date": start <= current_shop_date < end,
        "dataset_freshness": (
            "realtime_aggregate"
            if dataset == "video-overview"
            else "t_plus_one_detail"
            if dataset == "videos"
            else "mixed"
        ),
        "official_metric_definitions": (
            {
                "gmv": "Overall GMV for shoppable videos.",
                "sku_orders": "Paid SKU orders placed directly from shoppable videos.",
                "product_impressions": "Impressions of products in videos.",
                "product_clicks": "Product clicks from videos.",
                "click_through_rate": "Product clicks divided by video views.",
            }
            if dataset == "video-overview"
            else None
        ),
    }
    return result


@router.get("/operations/guard-feed")
def guard_feed(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=40, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return a bounded, store-scoped Smart Guard execution feed.

    The Shop screen uses this read-only projection to explain GMV Max decisions.
    It intentionally exposes neither arbitrary request payloads nor a write path.
    """

    shop = _get_shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        active_only=True,
    )
    params = {
        "workspace_id": int(workspace_id),
        "store_id": str(shop.shop_id),
        "before_id": int(before_id) if before_id is not None else None,
        "row_limit": int(limit) + 1,
    }
    event_rows = db.execute(
        text(
            """
            select id, advertiser_id, campaign_id, event_type, action, reason,
                   result, cost_cents, gross_revenue_cents, orders, roi,
                   request_json, response_json, error_message, created_at
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and store_id=:store_id
              and (:before_id is null or id < :before_id)
            order by id desc
            limit :row_limit
            """
        ),
        params,
    ).mappings().all()
    has_more = len(event_rows) > int(limit)
    visible_rows = event_rows[: int(limit)]

    state_rows = db.execute(
        text(
            """
            select id, advertiser_id, campaign_id, campaign_name,
                   operation_status, secondary_status, guard_status,
                   last_action, last_reason, latest_cost_cents,
                   latest_gross_revenue_cents, latest_orders, latest_roi,
                   source, last_report_at, last_checked_at, updated_at
            from gmv_campaign_realtime_state
            where workspace_id=:workspace_id and store_id=:store_id
            order by updated_at desc, id desc
            limit 500
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "store_id": str(shop.shop_id),
        },
    ).mappings().all()

    events: list[dict[str, Any]] = []
    for raw in visible_rows:
        row = dict(raw)
        request_payload = _json_mapping(row.get("request_json"))
        response_payload = _json_mapping(row.get("response_json"))
        creative_id = request_payload.get("creative_id") or request_payload.get("item_id")
        operator = request_payload.get("performed_by") or request_payload.get("actor")
        events.append(
            {
                "id": int(row["id"]),
                "advertiser_id": str(row.get("advertiser_id") or ""),
                "campaign_id": str(row.get("campaign_id") or ""),
                "creative_id": str(creative_id) if creative_id is not None else None,
                "event_type": str(row.get("event_type") or ""),
                "action": str(row.get("action") or ""),
                "reason": row.get("reason"),
                "result": str(row.get("result") or ""),
                "operator": str(operator or "系统"),
                "cost_cents": row.get("cost_cents"),
                "gross_revenue_cents": row.get("gross_revenue_cents"),
                "orders": row.get("orders"),
                "roi": row.get("roi"),
                "official_request_id": (
                    response_payload.get("request_id")
                    or response_payload.get("log_id")
                ),
                "error_message": row.get("error_message"),
                "created_at": row.get("created_at"),
            }
        )

    states = [
        {
            "id": int(row["id"]),
            "advertiser_id": str(row.get("advertiser_id") or ""),
            "campaign_id": str(row.get("campaign_id") or ""),
            "campaign_name": row.get("campaign_name"),
            "operation_status": row.get("operation_status"),
            "secondary_status": row.get("secondary_status"),
            "guard_status": row.get("guard_status"),
            "last_action": row.get("last_action"),
            "last_reason": row.get("last_reason"),
            "cost_cents": row.get("latest_cost_cents"),
            "gross_revenue_cents": row.get("latest_gross_revenue_cents"),
            "orders": row.get("latest_orders"),
            "roi": row.get("latest_roi"),
            "source": row.get("source"),
            "last_report_at": row.get("last_report_at"),
            "last_checked_at": row.get("last_checked_at"),
            "updated_at": row.get("updated_at"),
        }
        for row in map(dict, state_rows)
    ]
    observed_candidates = [
        value
        for value in [
            *(row.get("created_at") for row in visible_rows),
            *(row.get("last_checked_at") for row in state_rows),
        ]
        if isinstance(value, datetime)
    ]
    return {
        "items": events,
        "states": states,
        "has_more": has_more,
        "next_before_id": int(visible_rows[-1]["id"]) if has_more and visible_rows else None,
        "data_meta": {
            "source": "TIKTOK_BUSINESS_AND_SYSTEM_EXECUTION",
            "grain": "CAMPAIGN_ACTION",
            "refresh_interval_seconds": 60,
            "observed_at": max(observed_candidates).isoformat() if observed_candidates else None,
            "complete": not has_more,
            "coverage": "LATEST_BOUNDED_WINDOW",
            "returned": len(events),
        },
    }


def _shop_video_media_url(
    workspace_id: int,
    shop_id: int,
    asset_id: int,
    kind: str,
) -> str:
    normalized = "cover" if kind == "cover" else "video"
    return (
        f"/api/v1/tenants/{int(workspace_id)}/tiktok-shop/"
        f"video-media/{int(asset_id)}/{normalized}?shop_id={int(shop_id)}"
    )


def _shop_video_media_row(
    db: Session,
    *,
    workspace_id: int,
    asset_id: int,
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            select id, workspace_id, auth_id, advertiser_id, store_id, item_id,
                   local_preview_path, local_cover_path,
                   preview_content_type, cover_content_type,
                   media_cache_status, media_cached_at, updated_at
            from gmvmax_creative_asset_cache
            where id=:asset_id and workspace_id=:workspace_id
            limit 1
            """
        ),
        {"asset_id": int(asset_id), "workspace_id": int(workspace_id)},
    ).mappings().first()
    return dict(row) if row else None


def _shop_video_media_status(
    video_id: str,
    row: dict[str, Any] | None,
    *,
    has_video: bool,
    has_cover: bool,
) -> str:
    if video_id in {"0", "-1"}:
        return "INVALID_VIDEO_ID"
    if has_video and has_cover:
        return "READY"
    if has_video or has_cover:
        return "PARTIAL"
    if row:
        upstream = str(row.get("media_cache_status") or "").strip().upper()
        return upstream or "MEDIA_UNAVAILABLE"
    return "NOT_IN_GMVMAX_LIBRARY"


@router.post("/video-media/lookup")
def lookup_shop_video_media(
    workspace_id: int,
    payload: VideoMediaLookupRequest,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
        active_only=True,
    )
    video_ids = list(
        dict.fromkeys(
            value
            for raw in payload.video_ids
            if (value := str(raw or "").strip()) and len(value) <= 128
        )
    )
    if not video_ids:
        raise APIError("INVALID_VIDEO_IDS", "No valid TikTok video ids were supplied.", 400)

    statement = text(
        """
        select id, workspace_id, auth_id, advertiser_id, store_id, item_id,
               local_preview_path, local_cover_path,
               preview_content_type, cover_content_type,
               media_cache_status, media_cached_at, updated_at
        from gmvmax_creative_asset_cache
        where workspace_id=:workspace_id
          and item_id in :video_ids
        order by item_id asc,
                 case when media_cache_status='READY' then 0 else 1 end,
                 updated_at desc,
                 id desc
        """
    ).bindparams(bindparam("video_ids", expanding=True))
    rows = db.execute(
        statement,
        {"workspace_id": int(workspace_id), "video_ids": video_ids},
    ).mappings().all()

    best: dict[str, tuple[int, dict[str, Any], bool, bool]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        item_id = str(row.get("item_id") or "").strip()
        if not item_id or str(row.get("store_id") or "") != str(shop.shop_id):
            continue
        video = resolve_creative_media(row, "video")
        cover = resolve_creative_media(row, "cover")
        score = int(video is not None) + int(cover is not None)
        current = best.get(item_id)
        if current is None or score > current[0]:
            best[item_id] = (
                score,
                row,
                video is not None,
                cover is not None,
            )

    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    cached = 0
    for video_id in video_ids:
        selected = best.get(video_id)
        row = selected[1] if selected else None
        has_video = bool(selected and selected[2])
        has_cover = bool(selected and selected[3])
        media_status = _shop_video_media_status(
            video_id,
            row,
            has_video=has_video,
            has_cover=has_cover,
        )
        status_counts[media_status] = status_counts.get(media_status, 0) + 1
        if has_video or has_cover:
            cached += 1
        asset_id = int(row["id"]) if row else None
        items.append(
            {
                "video_id": video_id,
                "asset_id": asset_id,
                "cover_url": (
                    _shop_video_media_url(
                        workspace_id,
                        int(shop.id),
                        int(asset_id),
                        "cover",
                    )
                    if asset_id is not None and has_cover
                    else None
                ),
                "preview_url": (
                    _shop_video_media_url(
                        workspace_id,
                        int(shop.id),
                        int(asset_id),
                        "video",
                    )
                    if asset_id is not None and has_video
                    else None
                ),
                "media_status": media_status,
                "cache_status": str(row.get("media_cache_status") or "") if row else None,
                "cached_at": (
                    row["media_cached_at"].isoformat()
                    if row and isinstance(row.get("media_cached_at"), datetime)
                    else row.get("media_cached_at") if row else None
                ),
            }
        )
    return {
        "items": items,
        "requested": len(video_ids),
        "matched": cached,
        "status_counts": status_counts,
    }


def _validate_video_analysis_range(start: date, end_exclusive: date) -> None:
    if start >= end_exclusive or (end_exclusive - start).days > 366:
        raise APIError(
            "INVALID_DATE_RANGE",
            "Video analysis range must be between 1 and 366 days.",
            400,
        )


@router.post("/video-content-analyses/lookup")
def lookup_video_content_analyses(
    workspace_id: int,
    payload: VideoAnalysisLookupRequest,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
        active_only=True,
    )
    _validate_video_analysis_range(payload.start_date, payload.end_date_exclusive)
    video_ids = list(dict.fromkeys(
        value
        for raw in payload.video_ids
        if (value := str(raw or "").strip()) and len(value) <= 128
    ))
    rows = db.scalars(
        select(TikTokShopVideoContentAnalysis)
        .where(
            TikTokShopVideoContentAnalysis.workspace_id == int(workspace_id),
            TikTokShopVideoContentAnalysis.shop_row_id == int(payload.shop_id),
            TikTokShopVideoContentAnalysis.video_id.in_(video_ids),
            TikTokShopVideoContentAnalysis.metric_start_date == payload.start_date,
            TikTokShopVideoContentAnalysis.metric_end_date_exclusive == payload.end_date_exclusive,
        )
        .order_by(
            TikTokShopVideoContentAnalysis.video_id.asc(),
            TikTokShopVideoContentAnalysis.created_at.desc(),
            TikTokShopVideoContentAnalysis.id.desc(),
        )
    ).all()
    latest: dict[str, TikTokShopVideoContentAnalysis] = {}
    for row in rows:
        latest.setdefault(str(row.video_id), row)
    return {
        "items": [serialize_analysis(latest[video_id]) for video_id in video_ids if video_id in latest],
        "requested": len(video_ids),
        "matched": len(latest),
        "data_meta": {
            "source": "HERMES_VIDEO_ANALYST_CACHE",
            "complete": True,
            "prompt_version": VIDEO_ANALYSIS_PROMPT_VERSION,
            "model": VIDEO_ANALYSIS_PROVIDER_MODEL,
        },
    }


@router.post("/video-content-analyses")
def request_video_content_analysis(
    workspace_id: int,
    payload: VideoAnalysisRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not bool(settings.HERMES_VIDEO_ANALYST_AGENT_ENABLED):
        raise APIError(
            "HERMES_VIDEO_ANALYST_DISABLED",
            "The isolated Hermes video analyst is not enabled.",
            503,
        )
    shop = _get_shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
        active_only=True,
    )
    _validate_video_analysis_range(payload.start_date, payload.end_date_exclusive)
    video_id = str(payload.video_id).strip()
    if video_id in {"0", "-1"}:
        raise APIError(
            "INVALID_VIDEO_ID",
            "Product cards do not have video content to analyze.",
            400,
        )
    packet = build_metric_packet(
        db,
        workspace_id=int(workspace_id),
        shop=shop,
        video_id=video_id,
        start_date=payload.start_date,
        end_date_exclusive=payload.end_date_exclusive,
    )
    media_row, asset_id, media_fingerprint = find_media_identity(
        db,
        workspace_id=int(workspace_id),
        shop=shop,
        video_id=video_id,
    )
    cache_key = analysis_cache_key(packet=packet, media_fingerprint=media_fingerprint)
    row = db.scalar(
        select(TikTokShopVideoContentAnalysis).where(
            TikTokShopVideoContentAnalysis.cache_key == cache_key
        )
    )
    now = utcnow()
    if row and row.status == "SUCCEEDED":
        return {"item": serialize_analysis(row), "queued": False, "cache_hit": True}
    if row and row.status == "UNAVAILABLE" and not media_fingerprint:
        return {"item": serialize_analysis(row), "queued": False, "cache_hit": True}
    if row and row.status in {"QUEUED", "RUNNING"}:
        lease_live = row.lease_expires_at is None or row.lease_expires_at > now
        if lease_live or not payload.retry_failed:
            return {"item": serialize_analysis(row), "queued": False, "cache_hit": True}
    if row and row.status == "FAILED" and not payload.retry_failed:
        return {"item": serialize_analysis(row), "queued": False, "cache_hit": True}

    if row:
        row.status = "QUEUED"
        row.attempts = 0
        row.error_code = None
        row.error_message = None
        row.started_at = None
        row.completed_at = None
        row.lease_expires_at = None
        row.requested_by_user_id = int(me.id)
        row.input_summary_json = packet
        if row.transcript_status not in {"READY", "NO_SPEECH"}:
            row.transcript_status = "PENDING"
            row.transcript_source = None
            row.transcript_language = None
            row.transcript_text = None
            row.transcript_segments_json = None
            row.transcript_reason = None
            row.transcript_error_message = None
            row.transcript_attempts = 0
            row.transcript_started_at = None
            row.transcript_completed_at = None
    else:
        reusable_transcript = None
        if media_fingerprint:
            reusable_transcript = db.scalar(
                select(TikTokShopVideoContentAnalysis)
                .where(
                    TikTokShopVideoContentAnalysis.workspace_id == int(workspace_id),
                    TikTokShopVideoContentAnalysis.shop_row_id == int(shop.id),
                    TikTokShopVideoContentAnalysis.video_id == video_id,
                    TikTokShopVideoContentAnalysis.source_media_fingerprint == media_fingerprint,
                    TikTokShopVideoContentAnalysis.transcript_status.in_({"READY", "NO_SPEECH"}),
                )
                .order_by(
                    TikTokShopVideoContentAnalysis.transcript_completed_at.desc(),
                    TikTokShopVideoContentAnalysis.id.desc(),
                )
            )
        row = TikTokShopVideoContentAnalysis(
            workspace_id=int(workspace_id),
            account_id=int(shop.account_id),
            shop_row_id=int(shop.id),
            video_id=video_id,
            cache_key=cache_key,
            status="QUEUED",
            model_alias=str(settings.HERMES_VIDEO_ANALYST_AGENT_MODEL),
            provider_model=VIDEO_ANALYSIS_PROVIDER_MODEL,
            prompt_version=VIDEO_ANALYSIS_PROMPT_VERSION,
            metric_start_date=payload.start_date,
            metric_end_date_exclusive=payload.end_date_exclusive,
            source_asset_id=asset_id,
            source_media_fingerprint=media_fingerprint,
            transcript_status=(reusable_transcript.transcript_status if reusable_transcript else "PENDING"),
            transcript_source=("CACHE" if reusable_transcript else None),
            transcript_language=(reusable_transcript.transcript_language if reusable_transcript else None),
            transcript_text=(reusable_transcript.transcript_text if reusable_transcript else None),
            transcript_segments_json=(reusable_transcript.transcript_segments_json if reusable_transcript else None),
            transcript_reason=(reusable_transcript.transcript_reason if reusable_transcript else None),
            transcript_completed_at=(utcnow() if reusable_transcript else None),
            input_summary_json=packet,
            requested_by_user_id=int(me.id),
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = db.scalar(
                select(TikTokShopVideoContentAnalysis).where(
                    TikTokShopVideoContentAnalysis.cache_key == cache_key
                )
            )
            if not row:
                raise
            return {"item": serialize_analysis(row), "queued": False, "cache_hit": True}

    log_event(
        db,
        action="tiktok_shop.video_content_analysis_requested",
        resource_type="tiktok_shop_video_content_analysis",
        resource_id=int(row.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={
            "shop_row_id": int(shop.id),
            "video_id": video_id,
            "start_date": payload.start_date.isoformat(),
            "end_date_exclusive": payload.end_date_exclusive.isoformat(),
            "model": VIDEO_ANALYSIS_PROVIDER_MODEL,
            "has_local_media": bool(media_fingerprint),
        },
    )
    # Commit the idempotency row before publishing, otherwise a fast worker can
    # receive the message before the row is visible.
    db.commit()
    db.refresh(row)
    has_local_video = bool(resolve_creative_media(media_row, "video")) if media_row else False
    if row.transcript_status not in {"READY", "NO_SPEECH"} and not has_local_video:
        row.transcript_status = "UNAVAILABLE"
        row.transcript_reason = "LOCAL_VIDEO_UNAVAILABLE"
        row.transcript_completed_at = utcnow()
        db.add(row)
        db.commit()
    transcript_needed = row.transcript_status == "PENDING" and has_local_video
    task_name = (
        "tiktok_shop_video_transcript.prepare"
        if transcript_needed
        else "tiktok_shop_video_analysis.run"
    )
    task_queue = (
        str(WHISPER_TASK_QUEUE)
        if transcript_needed
        else str(settings.HERMES_VIDEO_ANALYSIS_TASK_QUEUE)
    )
    try:
        task = celery_app.send_task(
            task_name,
            kwargs={"analysis_id": int(row.id)},
            queue=task_queue,
        )
    except Exception as exc:
        row.status = "FAILED"
        row.error_code = "QUEUE_PUBLISH_FAILED"
        row.error_message = str(exc)[:1500]
        row.completed_at = utcnow()
        db.add(row)
        db.commit()
        raise APIError(
            "VIDEO_ANALYSIS_QUEUE_UNAVAILABLE",
            "The analysis request could not be queued.",
            503,
        ) from exc
    return {
        "item": serialize_analysis(row),
        "queued": True,
        "cache_hit": False,
        "task_id": str(task.id),
    }


def _serve_shop_video_media(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int,
    asset_id: int,
    kind: str,
) -> FileResponse:
    shop = _get_shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        active_only=True,
    )
    row = _shop_video_media_row(
        db,
        workspace_id=int(workspace_id),
        asset_id=int(asset_id),
    )
    if row and str(row.get("store_id") or "") != str(shop.shop_id):
        row = None
    media = resolve_creative_media(row, kind) if row else None
    if media is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video media is not cached locally.",
        )
    path, content_type = media
    return FileResponse(
        path=str(path),
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600, must-revalidate",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/video-media/{asset_id}/video")
def get_shop_video_media(
    workspace_id: int,
    asset_id: int = Path(..., ge=1),
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> FileResponse:
    return _serve_shop_video_media(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        asset_id=int(asset_id),
        kind="video",
    )


@router.get("/video-media/{asset_id}/cover")
def get_shop_video_cover(
    workspace_id: int,
    asset_id: int = Path(..., ge=1),
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> FileResponse:
    return _serve_shop_video_media(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        asset_id=int(asset_id),
        kind="cover",
    )


@router.get("/global-products")
def global_products(
    workspace_id: int,
    account_id: int = Query(..., gt=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("TIKTOK_SHOP_ACCOUNT_NOT_FOUND", "Authorization not found.", 404)
    query = (
        select(TikTokShopGlobalProduct)
        .where(
            TikTokShopGlobalProduct.workspace_id == int(workspace_id),
            TikTokShopGlobalProduct.account_id == int(account_id),
        )
        .order_by(TikTokShopGlobalProduct.synced_at.desc())
    )
    return _page(query, db, page=page, page_size=page_size)


@router.get("/sync-runs")
def sync_runs(
    workspace_id: int,
    shop_id: int | None = Query(default=None, gt=0),
    domain: str | None = Query(default=None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(TikTokShopSyncRun).where(
        TikTokShopSyncRun.workspace_id == int(workspace_id)
    )
    if shop_id:
        _get_shop(db, workspace_id=workspace_id, shop_id=shop_id)
        query = query.where(TikTokShopSyncRun.shop_row_id == int(shop_id))
    if domain:
        query = query.where(TikTokShopSyncRun.domain == domain)
    return _page(
        query.order_by(TikTokShopSyncRun.started_at.desc()),
        db,
        page=page,
        page_size=page_size,
    )


@router.post("/sync", status_code=202)
def start_sync(
    workspace_id: int,
    payload: SyncRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _get_shop(
        db,
        workspace_id=workspace_id,
        shop_id=payload.shop_id,
        active_only=True,
    )
    domains = list(dict.fromkeys(str(value).strip().lower() for value in payload.domains))
    invalid = sorted(set(domains) - SUPPORTED_DOMAINS)
    if invalid:
        raise APIError(
            "INVALID_SYNC_DOMAIN",
            f"Unsupported sync domains: {', '.join(invalid)}.",
            400,
        )
    if (
        payload.start_date
        and payload.end_date_exclusive
        and payload.start_date >= payload.end_date_exclusive
    ):
        raise APIError("INVALID_DATE_RANGE", "start_date must be before end_date_exclusive.", 400)
    task_ids: dict[str, str] = {}
    for index, domain in enumerate(domains):
        result = celery_app.send_task(
            "tiktok_shop.sync_domain",
            kwargs={
                "workspace_id": int(workspace_id),
                "account_id": int(shop.account_id),
                "shop_row_id": int(shop.id),
                "domain": domain,
                "trigger": "manual",
                "start_date": payload.start_date.isoformat() if payload.start_date else None,
                "end_date_exclusive": (
                    payload.end_date_exclusive.isoformat()
                    if payload.end_date_exclusive
                    else None
                ),
            },
            queue="tiktok_shop",
            countdown=index * 2,
        )
        task_ids[domain] = str(result.id)
    log_event(
        db,
        action="tiktok_shop.data_sync_queued",
        resource_type="oauth_tiktok_shop_shop",
        resource_id=int(shop.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"domains": domains, "task_ids": task_ids},
    )
    return {"status": "queued", "shop_id": int(shop.id), "tasks": task_ids}
