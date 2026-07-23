from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.core.security import client_ip
from app.data.db import get_db
from app.data.models.commerce import CommerceProductCostVersion
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopProduct,
    TikTokShopSku,
    TikTokShopSyncRun,
)
from app.services.audit import log_event
from app.services.commerce_analytics import (
    commerce_context,
    commerce_overview,
    cost_history,
    resolve_scope,
)
from app.services.commerce_orders import CommerceOrderError, order_summary


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/commerce",
    tags=["Tenant / Commerce"],
)


class CommerceSyncRequest(BaseModel):
    shop_id: int = Field(gt=0)
    start_date: date | None = None
    end_date: date | None = None
    include_finance: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> "CommerceSyncRequest":
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValueError("start_date must not exceed end_date")
            if (self.end_date - self.start_date).days > 364:
                raise ValueError("sync range cannot exceed 365 calendar days")
        return self


class ProductCostRequest(BaseModel):
    shop_id: int = Field(gt=0)
    sku_id: str = Field(default="", max_length=128)
    effective_from: datetime | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    unit_cost: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000)
    packaging_cost: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000)
    fulfillment_cost: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000)
    seller_shipping_cost: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1_000_000,
    )
    other_variable_cost: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1_000_000,
    )
    platform_fee_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    payment_fee_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    affiliate_commission_rate: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
    )
    expected_refund_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    target_margin_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> str:
        return str(value or "").strip().upper()

    @model_validator(mode="after")
    def validate_rates(self) -> "ProductCostRequest":
        variable_rates = (
            self.platform_fee_rate
            + self.payment_fee_rate
            + self.affiliate_commission_rate
            + self.expected_refund_rate
        )
        if variable_rates >= Decimal("1"):
            raise ValueError("combined variable rates must be less than 100%")
        return self


class FlashSalePolicyRequest(BaseModel):
    shop_id: int = Field(gt=0)
    activity_price_amount: Decimal = Field(gt=0, le=1_000_000, decimal_places=2)


def _shop(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int,
) -> OAuthTikTokShopShop:
    row = db.scalar(
        select(OAuthTikTokShopShop).where(
            OAuthTikTokShopShop.id == int(shop_id),
            OAuthTikTokShopShop.workspace_id == int(workspace_id),
            OAuthTikTokShopShop.is_active.is_(True),
        )
    )
    if not row:
        raise APIError("COMMERCE_SHOP_NOT_FOUND", "Active TikTok Shop not found.", 404)
    return row


def _product(
    db: Session,
    *,
    workspace_id: int,
    shop_id: int,
    product_id: str,
) -> TikTokShopProduct:
    row = db.scalar(
        select(TikTokShopProduct).where(
            TikTokShopProduct.workspace_id == int(workspace_id),
            TikTokShopProduct.shop_row_id == int(shop_id),
            TikTokShopProduct.product_id == str(product_id),
        )
    )
    if not row:
        raise APIError("COMMERCE_PRODUCT_NOT_FOUND", "TikTok Shop product not found.", 404)
    return row


def _api_error(exc: CommerceOrderError) -> APIError:
    return APIError("INVALID_COMMERCE_SCOPE", str(exc), 400)


def _effective_utc(value: datetime | None, shop: OAuthTikTokShopShop) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(str(shop.timezone_name)))
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _flash_sale_policy_item(
    row: TikTokShopFlashSalePolicy,
    shop: OAuthTikTokShopShop,
) -> dict[str, Any]:
    zone = ZoneInfo(str(shop.timezone_name or settings.TT_SHOP_DEFAULT_TIMEZONE))

    def timestamp(value: datetime | None) -> dict[str, str] | None:
        if value is None:
            return None
        aware = value.replace(tzinfo=timezone.utc)
        return {
            "utc": aware.isoformat(),
            "shop_local": aware.astimezone(zone).isoformat(),
        }

    return {
        "id": int(row.id),
        "shop_id": int(row.shop_row_id),
        "product_id": str(row.product_id),
        "enabled": bool(row.enabled),
        "activity_price_amount": str(row.activity_price_amount),
        "currency": str(row.currency),
        "status": str(row.status),
        "policy_revision": int(row.policy_revision),
        "applied_revision": int(row.applied_revision),
        "current_activity_id": row.current_activity_id,
        "current_activity_status": row.current_activity_status,
        "current_begin_at": timestamp(row.current_begin_at),
        "current_end_at": timestamp(row.current_end_at),
        "next_renewal_at": timestamp(row.next_renewal_at),
        "last_checked_at": timestamp(row.last_checked_at),
        "last_applied_at": timestamp(row.last_applied_at),
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "shop_timezone": str(shop.timezone_name),
    }


@router.get("/context")
def context(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return commerce_context(db, workspace_id=int(workspace_id))


@router.get("/overview")
def overview(
    workspace_id: int,
    shop_id: int | None = Query(default=None, gt=0),
    advertiser_id: str | None = Query(default=None, max_length=64),
    start_date: date | None = None,
    end_date: date | None = None,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return commerce_overview(
            db,
            workspace_id=int(workspace_id),
            shop_id=shop_id,
            advertiser_id=advertiser_id,
            start_date=start_date,
            end_date=end_date,
        )
    except CommerceOrderError as exc:
        raise _api_error(exc) from exc


@router.get("/orders/summary")
def orders_summary(
    workspace_id: int,
    shop_id: int | None = Query(default=None, gt=0),
    store_id: str | None = Query(default=None, max_length=128),
    advertiser_id: str | None = Query(default=None, max_length=64),
    start_date: date | None = None,
    end_date: date | None = None,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        effective_shop_id = shop_id
        if effective_shop_id is None and store_id:
            effective_shop_id = db.scalar(
                select(OAuthTikTokShopShop.id)
                .where(
                    OAuthTikTokShopShop.workspace_id == int(workspace_id),
                    OAuthTikTokShopShop.shop_id == str(store_id),
                    OAuthTikTokShopShop.is_active.is_(True),
                )
                .order_by(OAuthTikTokShopShop.last_seen_at.desc())
                .limit(1)
            )
        scope = resolve_scope(
            db,
            workspace_id=int(workspace_id),
            shop_id=effective_shop_id,
            advertiser_id=advertiser_id,
        )
        today = datetime.now(ZoneInfo(scope.reporting_timezone)).date()
        effective_end = end_date or today
        effective_start = start_date or (effective_end - timedelta(days=29))
        return order_summary(
            db,
            workspace_id=int(workspace_id),
            store_id=str(scope.shop.shop_id),
            start_date=effective_start,
            end_date=effective_end,
            advertiser_timezone=scope.reporting_timezone,
        )
    except CommerceOrderError as exc:
        raise _api_error(exc) from exc


@router.get("/products/{product_id}/costs")
def product_cost_history(
    workspace_id: int,
    product_id: str,
    shop_id: int = Query(..., gt=0),
    sku_id: str | None = Query(default=None, max_length=128),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _shop(db, workspace_id=int(workspace_id), shop_id=int(shop_id))
    _product(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        product_id=str(product_id),
    )
    items = cost_history(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        product_id=str(product_id),
        sku_id=sku_id,
    )
    return {"items": items, "total": len(items)}


@router.get("/sync/status")
def sync_status(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
    )
    rows = list(
        db.scalars(
            select(TikTokShopSyncRun)
            .where(
                TikTokShopSyncRun.workspace_id == int(workspace_id),
                TikTokShopSyncRun.shop_row_id == int(shop.id),
            )
            .order_by(TikTokShopSyncRun.id.desc())
            .limit(100)
        )
    )
    latest: dict[str, TikTokShopSyncRun] = {}
    for row in rows:
        latest.setdefault(str(row.domain), row)
    return {
        "shop_id": int(shop.id),
        "domains": {
            domain: {
                "run_id": int(row.id),
                "status": str(row.status),
                "trigger": str(row.trigger),
                "range_start": (
                    row.range_start.isoformat() if row.range_start else None
                ),
                "range_end_exclusive": (
                    row.range_end_exclusive.isoformat()
                    if row.range_end_exclusive
                    else None
                ),
                "rows_seen": int(row.rows_seen or 0),
                "rows_upserted": int(row.rows_upserted or 0),
                "started_at": (
                    row.started_at.isoformat() if row.started_at else None
                ),
                "completed_at": (
                    row.completed_at.isoformat()
                    if row.completed_at
                    else None
                ),
                "error": str(row.error_message or "") or None,
            }
            for domain, row in latest.items()
        },
    }


@router.put("/products/{product_id}/costs", status_code=201)
def save_product_cost(
    workspace_id: int,
    product_id: str,
    payload: ProductCostRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
    )
    product = _product(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
        product_id=str(product_id),
    )
    sku_id = str(payload.sku_id or "").strip()
    if sku_id:
        sku_exists = db.scalar(
            select(TikTokShopSku.id).where(
                TikTokShopSku.workspace_id == int(workspace_id),
                TikTokShopSku.shop_row_id == int(payload.shop_id),
                TikTokShopSku.product_id == str(product_id),
                TikTokShopSku.sku_id == sku_id,
            )
        )
        if not sku_exists:
            raise APIError(
                "COMMERCE_SKU_NOT_FOUND",
                "SKU does not belong to this TikTok Shop product.",
                404,
            )
    effective_from = _effective_utc(payload.effective_from, shop)
    row = CommerceProductCostVersion(
        workspace_id=int(workspace_id),
        shop_row_id=int(payload.shop_id),
        product_id=str(product_id),
        sku_id=sku_id,
        effective_from=effective_from,
        currency=str(payload.currency).strip().upper(),
        unit_cost=payload.unit_cost,
        packaging_cost=payload.packaging_cost,
        fulfillment_cost=payload.fulfillment_cost,
        seller_shipping_cost=payload.seller_shipping_cost,
        other_variable_cost=payload.other_variable_cost,
        platform_fee_rate=payload.platform_fee_rate,
        payment_fee_rate=payload.payment_fee_rate,
        affiliate_commission_rate=payload.affiliate_commission_rate,
        expected_refund_rate=payload.expected_refund_rate,
        target_margin_rate=payload.target_margin_rate,
        notes=(payload.notes or "").strip() or None,
        created_by_user_id=int(me.id),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise APIError(
            "COMMERCE_COST_VERSION_CONFLICT",
            "A cost version already exists for this effective time.",
            409,
        ) from exc
    log_event(
        db,
        action="commerce.product_cost_version_created",
        resource_type="tiktok_shop_product",
        resource_id=int(product.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={
            "shop_id": int(payload.shop_id),
            "product_id": str(product_id),
            "sku_id": sku_id,
            "effective_from": effective_from.isoformat(),
            "currency": row.currency,
        },
    )
    db.commit()
    return {
        "id": int(row.id),
        "product_id": str(product_id),
        "sku_id": sku_id,
        "effective_from": effective_from.isoformat(),
        "status": "created",
    }


@router.get("/flash-sales")
def flash_sale_policies(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(db, workspace_id=int(workspace_id), shop_id=int(shop_id))
    rows = list(
        db.scalars(
            select(TikTokShopFlashSalePolicy)
            .where(
                TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
                TikTokShopFlashSalePolicy.shop_row_id == int(shop.id),
            )
            .order_by(TikTokShopFlashSalePolicy.product_id.asc())
        )
    )
    return {
        "items": [_flash_sale_policy_item(row, shop) for row in rows],
        "total": len(rows),
        "shop_id": int(shop.id),
        "shop_timezone": str(shop.timezone_name),
        "automation": {
            "check_interval_seconds": int(
                settings.TT_SHOP_FLASH_SALE_AUTOMATION_INTERVAL_SECONDS
            ),
            "activity_duration_seconds": int(
                settings.TT_SHOP_FLASH_SALE_DURATION_SECONDS
            ),
            "minimum_coverage_seconds": int(
                settings.TT_SHOP_FLASH_SALE_MIN_COVERAGE_SECONDS
            ),
        },
    }


@router.put("/products/{product_id}/flash-sale", status_code=202)
def save_flash_sale_policy(
    workspace_id: int,
    product_id: str,
    payload: FlashSalePolicyRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(db, workspace_id=int(workspace_id), shop_id=int(payload.shop_id))
    product = _product(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
        product_id=str(product_id),
    )
    price = payload.activity_price_amount.quantize(Decimal("0.01"))
    currency = str(product.currency or "USD").strip().upper()
    row = db.scalar(
        select(TikTokShopFlashSalePolicy).where(
            TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
            TikTokShopFlashSalePolicy.shop_row_id == int(shop.id),
            TikTokShopFlashSalePolicy.product_id == str(product_id),
        )
    )
    changed = (
        row is None
        or not bool(row.enabled)
        or Decimal(row.activity_price_amount) != price
        or str(row.currency) != currency
    )
    if row is None:
        row = TikTokShopFlashSalePolicy(
            workspace_id=int(workspace_id),
            account_id=int(shop.account_id),
            shop_row_id=int(shop.id),
            product_id=str(product_id),
            activity_price_amount=price,
            currency=currency,
            enabled=True,
            status="active",
            policy_revision=1,
            applied_revision=0,
            created_by_user_id=int(me.id),
        )
        db.add(row)
    else:
        row.account_id = int(shop.account_id)
        row.activity_price_amount = price
        row.currency = currency
        row.enabled = True
        row.status = "active"
        if changed:
            row.policy_revision = int(row.policy_revision) + 1
        row.last_error_code = None
        row.last_error_message = None
    db.flush()
    log_event(
        db,
        action="commerce.flash_sale_policy_saved",
        resource_type="tiktok_shop_product",
        resource_id=int(product.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={
            "shop_id": int(shop.id),
            "product_id": str(product_id),
            "activity_price_amount": str(price),
            "currency": currency,
            "policy_revision": int(row.policy_revision),
            "force_replace": changed,
        },
    )
    db.commit()
    task = celery_app.send_task(
        "tiktok_shop.reconcile_flash_sale_shop",
        kwargs={
            "workspace_id": int(workspace_id),
            "account_id": int(shop.account_id),
            "shop_row_id": int(shop.id),
            "trigger": "user_price_update",
            "force_replace": bool(changed),
        },
        queue="tiktok_shop",
    )
    return {
        "status": "queued",
        "task_id": str(task.id),
        "policy": _flash_sale_policy_item(row, shop),
    }


@router.delete("/products/{product_id}/flash-sale", status_code=202)
def disable_flash_sale_policy(
    workspace_id: int,
    product_id: str,
    request: Request,
    shop_id: int = Query(..., gt=0),
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(db, workspace_id=int(workspace_id), shop_id=int(shop_id))
    product = _product(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(shop_id),
        product_id=str(product_id),
    )
    row = db.scalar(
        select(TikTokShopFlashSalePolicy).where(
            TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
            TikTokShopFlashSalePolicy.shop_row_id == int(shop.id),
            TikTokShopFlashSalePolicy.product_id == str(product_id),
        )
    )
    if not row:
        raise APIError(
            "FLASH_SALE_POLICY_NOT_FOUND",
            "Flash-sale automation is not configured for this product.",
            404,
        )
    row.enabled = False
    row.status = "paused"
    row.policy_revision = int(row.policy_revision) + 1
    log_event(
        db,
        action="commerce.flash_sale_policy_disabled",
        resource_type="tiktok_shop_product",
        resource_id=int(product.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"shop_id": int(shop.id), "product_id": str(product_id)},
    )
    db.commit()
    return {
        "status": "paused",
        "policy": _flash_sale_policy_item(row, shop),
        "note": "Existing TikTok Shop activity is left unchanged; no further renewal will be created.",
    }


@router.post("/flash-sales/reconcile", status_code=202)
def reconcile_flash_sale_policies(
    workspace_id: int,
    shop_id: int = Query(..., gt=0),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(db, workspace_id=int(workspace_id), shop_id=int(shop_id))
    task = celery_app.send_task(
        "tiktok_shop.reconcile_flash_sale_shop",
        kwargs={
            "workspace_id": int(workspace_id),
            "account_id": int(shop.account_id),
            "shop_row_id": int(shop.id),
            "trigger": "manual_reconcile",
            "force_replace": False,
        },
        queue="tiktok_shop",
    )
    return {"status": "queued", "task_id": str(task.id), "shop_id": int(shop.id)}


@router.post("/sync", status_code=202)
def sync_commerce_data(
    workspace_id: int,
    payload: CommerceSyncRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shop = _shop(
        db,
        workspace_id=int(workspace_id),
        shop_id=int(payload.shop_id),
    )
    domains = ["orders", "catalog", "analytics"]
    if payload.include_finance:
        domains.append("finance")
    end_exclusive = (
        payload.end_date + timedelta(days=1) if payload.end_date else None
    )
    tasks: dict[str, str] = {}
    for index, domain in enumerate(domains):
        domain_start = payload.start_date
        domain_end_exclusive = end_exclusive
        if domain == "catalog":
            domain_start = None
            domain_end_exclusive = None
        elif (
            domain == "analytics"
            and domain_start is not None
            and domain_end_exclusive is not None
            and (domain_end_exclusive - domain_start).days > 31
        ):
            domain_start = domain_end_exclusive - timedelta(days=31)
        result = celery_app.send_task(
            "tiktok_shop.sync_domain",
            kwargs={
                "workspace_id": int(workspace_id),
                "account_id": int(shop.account_id),
                "shop_row_id": int(shop.id),
                "domain": domain,
                "trigger": "commerce_manual",
                "start_date": (
                    domain_start.isoformat() if domain_start else None
                ),
                "end_date_exclusive": (
                    domain_end_exclusive.isoformat()
                    if domain_end_exclusive
                    else None
                ),
            },
            queue="tiktok_shop",
            countdown=index * 2,
        )
        tasks[domain] = str(result.id)
    log_event(
        db,
        action="commerce.data_sync_queued",
        resource_type="oauth_tiktok_shop_shop",
        resource_id=int(shop.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={
            "domains": domains,
            "tasks": tasks,
            "start_date": (
                payload.start_date.isoformat() if payload.start_date else None
            ),
            "end_date": (
                payload.end_date.isoformat() if payload.end_date else None
            ),
        },
    )
    db.commit()
    return {
        "status": "queued",
        "shop_id": int(shop.id),
        "tasks": tasks,
    }
