from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin
from app.core.errors import APIError
from app.core.security import client_ip
from app.celery_app import celery_app
from app.data.db import get_db
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount
from app.data.models.tiktok_shop_content_posting import TikTokShopContentPost
from app.services.audit import log_event
from app.services.tiktok_shop_content_posting import (
    normalize_optional_identifier,
    persist_uploaded_video,
    request_fingerprint,
    serialize_content_post,
    utcnow_naive,
    validate_idempotency_key,
    validate_product_link_title,
    validate_video_title,
)
from app.services.tiktok_shop_creator_api import CREATOR_VIDEO_SCOPE, TikTokShopCreatorAPIClient


QUEUE = "tiktok_shop"


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/tiktok-shop/content-posting",
    tags=["Tenant / TikTok Shop Content Posting"],
)


class ProviderDataOut(BaseModel):
    data: Any
    request_id: str | None = None


class CreatorAccountOut(BaseModel):
    id: int
    alias: str | None = None
    seller_name: str | None = None
    status: str
    user_type: int | None = None
    granted_scopes: list[str]
    content_posting_ready: bool
    expires_at: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class CreatorAccountListOut(BaseModel):
    items: list[CreatorAccountOut]


class ShowcaseAddIn(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=20)


class ContentPostOut(BaseModel):
    id: int
    account_id: int
    created_by_user_id: int
    idempotency_key: str
    original_filename: str
    file_size: int
    local_file_available: bool
    product_id: str
    product_link_title: str
    video_title: str | None = None
    cover_timestamp_ms: int | None = None
    music_id: str | None = None
    official_file_id: str | None = None
    precheck_task_id: str | None = None
    precheck_status: str | None = None
    precheck_issues: list[Any] = Field(default_factory=list)
    video_id: str | None = None
    post_status: str | None = None
    post_time: str | None = None
    workflow_status: str
    publish_requested: bool
    poll_attempts: int
    next_poll_at: str | None = None
    provider_request_ids: dict[str, Any] = Field(default_factory=dict)
    api_versions: dict[str, Any] = Field(default_factory=dict)
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_error_request_id: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ContentPostCreateOut(BaseModel):
    item: ContentPostOut
    reused: bool = False


class ContentPostListOut(BaseModel):
    items: list[ContentPostOut]
    total: int
    page: int
    page_size: int


def _creator_account(db: Session, *, workspace_id: int, account_id: int) -> OAuthTikTokShopAccount:
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("CREATOR_AUTHORIZATION_NOT_FOUND", "Creator authorization not found.", 404)
    scopes = {str(value) for value in (account.granted_scopes_json or [])}
    if account.user_type != 1:
        raise APIError(
            "CREATOR_TOKEN_REQUIRED",
            "Content Posting requires a creator token (user_type=1), not a seller token.",
            409,
        )
    if account.status != "active" or CREATOR_VIDEO_SCOPE not in scopes:
        raise APIError(
            "CREATOR_REAUTHORIZATION_REQUIRED",
            "Re-authorize the creator account with creator.video.write.",
            409,
            data={"required_scope": CREATOR_VIDEO_SCOPE},
        )
    return account


def _post_row(db: Session, *, workspace_id: int, post_id: int) -> TikTokShopContentPost:
    row = db.get(TikTokShopContentPost, int(post_id))
    if not row or int(row.workspace_id) != int(workspace_id):
        raise APIError("CONTENT_POST_NOT_FOUND", "Content posting workflow not found.", 404)
    return row


def _queue_post(post_id: int, *, countdown: int = 0) -> None:
    celery_app.send_task(
        "tiktok_shop.process_content_post",
        kwargs={"post_id": int(post_id)},
        queue=QUEUE,
        countdown=max(0, int(countdown)),
    )


@router.get("/accounts", response_model=CreatorAccountListOut)
def list_creator_accounts(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    accounts = db.execute(
        select(OAuthTikTokShopAccount)
        .where(
            OAuthTikTokShopAccount.workspace_id == int(workspace_id),
            OAuthTikTokShopAccount.user_type == 1,
        )
        .order_by(OAuthTikTokShopAccount.created_at.desc())
    ).scalars().all()
    return CreatorAccountListOut(
        items=[
            CreatorAccountOut(
                id=int(account.id),
                alias=account.alias,
                seller_name=account.seller_name,
                status=account.status,
                user_type=account.user_type,
                granted_scopes=list(account.granted_scopes_json or []),
                content_posting_ready=(
                    account.status == "active"
                    and CREATOR_VIDEO_SCOPE in set(account.granted_scopes_json or [])
                ),
                expires_at=account.expires_at.isoformat() if account.expires_at else None,
                last_error_code=account.last_error_code,
                last_error_message=account.last_error_message,
            )
            for account in accounts
        ]
    )


@router.get("/accounts/{account_id}/profile", response_model=ProviderDataOut)
async def get_creator_profile(
    workspace_id: int,
    account_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    _creator_account(db, workspace_id=workspace_id, account_id=account_id)
    async with await TikTokShopCreatorAPIClient.create(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
    ) as client:
        result = await client.profile()
    return ProviderDataOut(data=result.data, request_id=result.request_id)


@router.get("/accounts/{account_id}/shop-products", response_model=ProviderDataOut)
async def get_creator_shop_products(
    workspace_id: int,
    account_id: int,
    title_keyword: str | None = Query(default=None, max_length=255),
    sort_field: Literal["PRODUCT_ID", "PRICE", "SALE"] = "PRODUCT_ID",
    sort_order: Literal["ASC", "DESC"] = "DESC",
    page_size: int = Query(default=20, ge=1, le=100),
    page_token: str | None = Query(default=None, max_length=1024),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    _creator_account(db, workspace_id=workspace_id, account_id=account_id)
    async with await TikTokShopCreatorAPIClient.create(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
    ) as client:
        result = await client.shop_products(
            title_keyword=title_keyword,
            sort_field=sort_field,
            sort_order=sort_order,
            page_size=page_size,
            page_token=page_token,
        )
    return ProviderDataOut(data=result.data, request_id=result.request_id)


@router.get("/accounts/{account_id}/showcase-products", response_model=ProviderDataOut)
async def get_creator_showcase_products(
    workspace_id: int,
    account_id: int,
    origin: Literal["LIVE", "SHOWCASE"] = "SHOWCASE",
    page_size: int = Query(default=20, ge=1, le=20),
    page_token: str | None = Query(default=None, max_length=1024),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    _creator_account(db, workspace_id=workspace_id, account_id=account_id)
    async with await TikTokShopCreatorAPIClient.create(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
    ) as client:
        result = await client.showcase_products(
            origin=origin,
            page_size=page_size,
            page_token=page_token,
        )
    return ProviderDataOut(data=result.data, request_id=result.request_id)


@router.post("/accounts/{account_id}/showcase-products", response_model=ProviderDataOut)
async def add_creator_showcase_products(
    workspace_id: int,
    account_id: int,
    payload: ShowcaseAddIn,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    _creator_account(db, workspace_id=workspace_id, account_id=account_id)
    product_ids = list(dict.fromkeys(str(value or "").strip() for value in payload.product_ids))
    if any(not value or len(value) > 128 for value in product_ids):
        raise APIError("INVALID_PRODUCT_ID", "Every product ID must be a non-empty official TikTok product ID.", 422)
    async with await TikTokShopCreatorAPIClient.create(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
    ) as client:
        result = await client.add_showcase_products(product_ids)
    log_event(
        db,
        action="tiktok_shop.creator_showcase_products_added",
        resource_type="oauth_tiktok_shop_account",
        resource_id=int(account_id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"product_ids": product_ids, "request_id": result.request_id},
    )
    return ProviderDataOut(data=result.data, request_id=result.request_id)


@router.post("/posts", response_model=ContentPostCreateOut, status_code=202)
async def create_content_post(
    workspace_id: int,
    request: Request,
    account_id: int = Form(..., gt=0),
    product_id: str = Form(..., min_length=1, max_length=128),
    product_link_title: str = Form(..., min_length=1, max_length=64),
    video: UploadFile = File(...),
    video_title: str | None = Form(default=None),
    cover_uri: str | None = Form(default=None, max_length=2048),
    cover_timestamp_ms: int | None = Form(default=None, ge=0),
    music_id: str | None = Form(default=None, max_length=128),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    normalized_key = validate_idempotency_key(idempotency_key)
    account = _creator_account(db, workspace_id=workspace_id, account_id=account_id)
    existing = db.scalar(
        select(TikTokShopContentPost).where(
            TikTokShopContentPost.account_id == int(account.id),
            TikTokShopContentPost.idempotency_key == normalized_key,
        )
    )
    if existing is not None:
        await video.close()
        return ContentPostCreateOut(item=ContentPostOut(**serialize_content_post(existing)), reused=True)

    normalized_product_id = str(product_id).strip()
    normalized_anchor_title = validate_product_link_title(product_link_title)
    normalized_video_title = validate_video_title(video_title)
    normalized_cover_uri = normalize_optional_identifier(cover_uri, field="cover_uri", max_length=2048)
    normalized_music_id = normalize_optional_identifier(music_id, field="music_id")
    path, original_name, file_size, sha256_digest = await persist_uploaded_video(
        video,
        workspace_id=workspace_id,
        account_id=account_id,
    )
    fingerprint = request_fingerprint(
        sha256_digest=sha256_digest,
        product_id=normalized_product_id,
        product_link_title=normalized_anchor_title,
        video_title=normalized_video_title,
        cover_uri=normalized_cover_uri,
        cover_timestamp_ms=cover_timestamp_ms,
        music_id=normalized_music_id,
    )
    row = TikTokShopContentPost(
        workspace_id=int(workspace_id),
        account_id=int(account.id),
        created_by_user_id=int(me.id),
        idempotency_key=normalized_key,
        request_fingerprint=fingerprint,
        original_filename=original_name,
        local_file_path=str(path),
        media_type=str(video.content_type or "")[:128] or None,
        file_size=file_size,
        sha256_digest=sha256_digest,
        product_id=normalized_product_id,
        product_link_title=normalized_anchor_title,
        video_title=normalized_video_title,
        cover_uri=normalized_cover_uri,
        cover_timestamp_ms=cover_timestamp_ms,
        music_id=normalized_music_id,
        workflow_status="QUEUED",
        publish_requested=False,
        api_versions_json={
            "upload": "202505",
            "precheck_create": "202511",
            "precheck_status": "202511",
            "publish": "202603",
            "publish_status": "202509",
        },
    )
    db.add(row)
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        Path(path).unlink(missing_ok=True)
        existing = db.scalar(
            select(TikTokShopContentPost).where(
                TikTokShopContentPost.account_id == int(account.id),
                TikTokShopContentPost.idempotency_key == normalized_key,
            )
        )
        if existing is None:
            raise
        return ContentPostCreateOut(item=ContentPostOut(**serialize_content_post(existing)), reused=True)
    try:
        _queue_post(int(row.id))
    except Exception as exc:
        row.workflow_status = "QUEUE_FAILED"
        row.last_error_code = "CONTENT_POST_QUEUE_FAILED"
        row.last_error_message = "The workflow was saved but could not be queued. Retry from the workflow page."
        db.add(row)
        db.commit()
        raise APIError("CONTENT_POST_QUEUE_FAILED", row.last_error_message, 503) from exc
    log_event(
        db,
        action="tiktok_shop.content_post_created",
        resource_type="tiktok_shop_content_post",
        resource_id=int(row.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"account_id": int(account.id), "product_id": normalized_product_id},
    )
    return ContentPostCreateOut(item=ContentPostOut(**serialize_content_post(row)), reused=False)


@router.get("/posts", response_model=ContentPostListOut)
def list_content_posts(
    workspace_id: int,
    account_id: int | None = Query(default=None, gt=0),
    workflow_status: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=30, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    filters = [TikTokShopContentPost.workspace_id == int(workspace_id)]
    if account_id is not None:
        filters.append(TikTokShopContentPost.account_id == int(account_id))
    if workflow_status:
        filters.append(TikTokShopContentPost.workflow_status == str(workflow_status).strip().upper())
    total = int(db.scalar(select(func.count()).select_from(TikTokShopContentPost).where(*filters)) or 0)
    rows = db.execute(
        select(TikTokShopContentPost)
        .where(*filters)
        .order_by(TikTokShopContentPost.created_at.desc(), TikTokShopContentPost.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return ContentPostListOut(
        items=[ContentPostOut(**serialize_content_post(row)) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/posts/{post_id}", response_model=ContentPostOut)
def get_content_post(
    workspace_id: int,
    post_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    return ContentPostOut(**serialize_content_post(_post_row(db, workspace_id=workspace_id, post_id=post_id)))


@router.post("/posts/{post_id}/publish", response_model=ContentPostOut, status_code=202)
def publish_content_post(
    workspace_id: int,
    post_id: int,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _post_row(db, workspace_id=workspace_id, post_id=post_id)
    _creator_account(db, workspace_id=workspace_id, account_id=int(row.account_id))
    if row.workflow_status == "SUCCESS" or row.publish_requested:
        return ContentPostOut(**serialize_content_post(row))
    if row.precheck_status != "SUCCESS" or row.workflow_status != "READY_TO_PUBLISH":
        raise APIError(
            "PRECHECK_REQUIRED",
            "The official precheck must pass before publishing.",
            409,
            data={"precheck_status": row.precheck_status, "workflow_status": row.workflow_status},
        )
    row.publish_requested = True
    row.completed_at = None
    row.last_error_code = None
    row.last_error_message = None
    row.last_error_request_id = None
    db.add(row)
    db.commit()
    _queue_post(int(row.id))
    log_event(
        db,
        action="tiktok_shop.content_post_publish_requested",
        resource_type="tiktok_shop_content_post",
        resource_id=int(row.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"product_id": row.product_id},
    )
    return ContentPostOut(**serialize_content_post(row))


@router.post("/posts/{post_id}/refresh", response_model=ContentPostOut, status_code=202)
def refresh_content_post(
    workspace_id: int,
    post_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _post_row(db, workspace_id=workspace_id, post_id=post_id)
    _creator_account(db, workspace_id=workspace_id, account_id=int(row.account_id))
    if row.workflow_status == "PUBLISH_UNCERTAIN":
        raise APIError(
            "PUBLISH_RESULT_UNCERTAIN",
            "Do not retry publishing automatically. Reconcile the creator account before any new post.",
            409,
        )
    if row.workflow_status in {"SUCCESS", "PRECHECK_FAILED"}:
        return ContentPostOut(**serialize_content_post(row))
    row.completed_at = None
    row.last_error_code = None
    row.last_error_message = None
    row.last_error_request_id = None
    row.poll_attempts = 0
    if row.video_id:
        row.workflow_status = "PROCESSING"
    elif row.precheck_status == "SUCCESS":
        row.workflow_status = "READY_TO_PUBLISH"
    elif row.precheck_task_id:
        row.workflow_status = "PRECHECKING"
    else:
        row.workflow_status = "QUEUED"
    db.add(row)
    db.commit()
    _queue_post(int(row.id))
    return ContentPostOut(**serialize_content_post(row))
