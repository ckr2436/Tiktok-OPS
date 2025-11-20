"""
Endpoints for retrieving and updating GMV Max binding configuration and
options.  These routes expose the current binding state and allow clients
to refresh and update the configuration for an account.
"""

from __future__ import annotations

from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.services.ttb_meta import (
    build_gmvmax_options,
    compute_meta_etag,
    enqueue_meta_sync,
    get_meta_cursor_state,
)
from app.services.ttb_binding_config import (
    BindingConfigStorageNotReady,
    get_binding_config,
    upsert_binding_config,
)
from app.services.ttb_sync import _normalize_identifier

from app.data.models.ttb_entities import TTBBusinessCenter

from . import common


# Subrouter for GMV Max binding configuration endpoints.
router = APIRouter()


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/options",
)
async def get_gmv_max_options(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    refresh: int = Query(default=0, ge=0, le=1),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> Response:
    """Return GMV Max binding options with optional refresh and ETag support."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    refresh_requested = bool(refresh)
    cursor_state = get_meta_cursor_state(db, workspace_id=workspace_id, auth_id=auth_id)
    etag = compute_meta_etag(cursor_state.revisions)
    request_etag = common._normalize_if_none_match(request.headers.get("if-none-match"))
    if not refresh_requested and request_etag and request_etag == etag:
        response = Response(status_code=status.HTTP_304_NOT_MODIFIED)
        response.headers["ETag"] = f'"{etag}"'
        return response
    refresh_status: Optional[str] = None
    idempotency_key: Optional[str] = None
    task_name: Optional[str] = None
    if refresh_requested:
        try:
            result = enqueue_meta_sync(workspace_id=int(workspace_id), auth_id=int(auth_id))
            idempotency_key = result.idempotency_key
            task_name = result.task_name
            common.logger.info(
                "gmv max options refresh enqueued",
                extra={
                    "provider": "tiktok-business",
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "idempotency_key": idempotency_key,
                    "task_name": task_name,
                },
            )
        except Exception:
            common.logger.exception(
                "failed to enqueue meta refresh",
                extra={
                    "provider": "tiktok-business",
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "idempotency_key": idempotency_key,
                    "task_name": task_name,
                },
            )
        cursor_state, changed = await common._poll_for_meta_refresh(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            initial_state=cursor_state,
            initial_etag=etag,
        )
        new_etag = compute_meta_etag(cursor_state.revisions)
        if new_etag != etag:
            etag = new_etag
        if not changed:
            refresh_status = "timeout"
        common.logger.info(
            "gmv max options refresh polled",
            extra={
                "provider": "tiktok-business",
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "idempotency_key": idempotency_key,
                "task_name": task_name,
                "refresh_changed": changed,
            },
        )
    db.expire_all()
    payload: Dict[str, Any] = build_gmvmax_options(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        fallback_synced_at=cursor_state.updated_at,
    )
    if refresh_status:
        payload["refresh"] = refresh_status
        if refresh_status == "timeout" and idempotency_key:
            payload["idempotency_key"] = idempotency_key
    response = JSONResponse(payload)
    response.headers["ETag"] = f'"{etag}"'
    return response


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/config",
    response_model=common.GMVMaxBindingConfig,
)
def get_gmv_max_config(
    workspace_id: int,
    provider: str,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.GMVMaxBindingConfig:
    """Retrieve the persisted GMV Max binding configuration for this account."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    try:
        row = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    except BindingConfigStorageNotReady as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
        ) from exc
    return common._serialize_binding_config(row)


@router.put(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/config",
    response_model=common.GMVMaxBindingConfig,
)
def update_gmv_max_config(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: common.GMVMaxBindingUpdateRequest,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> common.GMVMaxBindingConfig:
    """Update the GMV Max binding configuration for this advertiser account."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    normalized_bc_id = _normalize_identifier(payload.bc_id)
    if not normalized_bc_id:
        raise APIError("BUSINESS_CENTER_NOT_FOUND", "Business center not found.", status.HTTP_404_NOT_FOUND)
    bc: Optional[TTBBusinessCenter] | None = None
    try:
        bc = common._get_business_center(
            db, workspace_id=workspace_id, auth_id=auth_id, bc_id=normalized_bc_id
        )
    except APIError as exc:
        if exc.code != "BUSINESS_CENTER_NOT_FOUND":
            raise
    expected_bc_id = bc.bc_id if bc else normalized_bc_id
    advertiser = common._get_advertiser(
        db, workspace_id=workspace_id, auth_id=auth_id, advertiser_id=payload.advertiser_id
    )
    store = common._get_store(db, workspace_id=workspace_id, auth_id=auth_id, store_id=payload.store_id)
    common._validate_bc_alignment(
        db=db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        expected_bc_id=expected_bc_id,
        advertiser=advertiser,
        store=store,
    )
    try:
        row = upsert_binding_config(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            bc_id=expected_bc_id,
            advertiser_id=payload.advertiser_id,
            store_id=payload.store_id,
            auto_sync_products=payload.auto_sync_products,
            actor_user_id=int(me.id),
        )
        db.commit()
    except BindingConfigStorageNotReady as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
        ) from exc
    except Exception:
        db.rollback()
        raise
    return common._serialize_binding_config(row)


__all__ = [
    "router",
    "get_gmv_max_options",
    "get_gmv_max_config",
    "update_gmv_max_config",
]
