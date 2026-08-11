"""
Endpoints for triggering and inspecting synchronization runs for the TikTok
Business tenant API.  These routes allow users to start meta or product
synchronization jobs and query the status of individual runs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.services.ttb_sync_dispatch import DispatchResult, dispatch_sync

# Import identifier normalization from the original sync module.  This utility
# is used to coerce optional BC IDs into a normalized form.
from app.services.ttb_sync import _normalize_identifier

from . import common


# Subrouter for sync‑related endpoints.  As with other subrouters, no
# prefix is specified here; the parent router handles the tenant prefix.
router = APIRouter()


def _run_matches_account(run: ScheduleRun, provider: str, auth_id: int) -> bool:
    """Verify that a ScheduleRun belongs to the expected provider and account."""
    stats = run.stats_json or {}
    requested = stats.get("requested") or {}
    if int(requested.get("auth_id") or 0) != int(auth_id):
        return False
    if (requested.get("provider") or "").strip() != provider:
        return False
    return True


@router.post(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/sync",
    response_model=common.SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_sync(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    body: common.SyncRequest,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> common.SyncResponse:
    """Trigger a meta or product synchronization for a specific account.

    The request body determines the scope (meta or products), mode (full or
    incremental), and other parameters.  This endpoint performs a number of
    validations and may wait synchronously for the run to complete if
    requested.
    """
    # Validate provider and ensure the account exists
    normalized_provider = common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)

    requested_idempotency = body.idempotency_key
    raw_mode = body.mode or "full"
    normalized_mode = str(raw_mode).strip().lower() if raw_mode else "full"
    if normalized_mode not in {"incremental", "full"}:
        raise APIError("INVALID_MODE", "mode must be incremental or full.", status.HTTP_400_BAD_REQUEST)

    wait_for_completion = bool(body.wait_for_completion)
    wait_timeout = body.wait_timeout_seconds or settings.TTB_SYNC_WAIT_TIMEOUT_SECONDS

    # Handle meta syncs separately from product syncs
    if body.scope == "meta":
        try:
            result: DispatchResult = dispatch_sync(
                db,
                workspace_id=int(workspace_id),
                provider=normalized_provider,
                auth_id=int(auth_id),
                scope="meta",
                params={"mode": normalized_mode, "page_size": 200},
                actor_user_id=int(me.id),
                actor_workspace_id=int(me.workspace_id),
                actor_ip=request.client.host if request.client else None,
                idempotency_key=requested_idempotency,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        summary: Optional[common.MetaSummary] = None
        status_value = result.status
        run = result.run
        if wait_for_completion and run:
            completed = common._wait_for_run_completion(
                db,
                run_id=int(run.id),
                timeout_seconds=wait_timeout,
                interval_seconds=settings.TTB_SYNC_WAIT_INTERVAL_SECONDS,
            )
            run = completed or run
            if completed:
                status_value = completed.status or status_value
        if run:
            summary = common._extract_sync_summary(run)
        return common.SyncResponse(
            run_id=int(run.id) if run else None,
            schedule_id=int(run.schedule_id) if run and run.schedule_id else None,
            task_name=common.SYNC_TASKS["meta"],
            task_id=result.task_id,
            status=status_value,
            idempotent=result.idempotent,
            idempotency_key=result.run.idempotency_key if result.run else None,
            summary=summary,
        )
    # Only "products" scope is supported beyond meta
    if body.scope != "products":
        raise APIError("UNSUPPORTED_SCOPE", f"Scope {body.scope} is not supported.", status.HTTP_400_BAD_REQUEST)

    advertiser_id = body.advertiser_id
    store_id = body.store_id
    if not advertiser_id:
        raise APIError(
            "ADVERTISER_REQUIRED_FOR_GMV_MAX",
            "advertiser_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )
    if not store_id:
        raise APIError(
            "STORE_ID_REQUIRED_FOR_GMV_MAX",
            "store_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )
    advertiser = common._get_advertiser(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
    )
    store = common._get_store(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=str(store_id),
    )
    # Ensure advertiser and store are linked
    link_exists = (
        db.query(TTBAdvertiserStoreLink.id)
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id))
        .filter(TTBAdvertiserStoreLink.store_id == str(store_id))
        .first()
    )
    if not link_exists:
        raise APIError(
            "ADVERTISER_NOT_LINKED_TO_STORE",
            "The advertiser is not linked to the specified store.",
            status.HTTP_400_BAD_REQUEST,
        )
    # Normalize the optional business center hint and resolve the effective BC
    bc_hint = body.bc_id
    bc_id = _normalize_identifier(bc_hint) or store.bc_id or advertiser.bc_id
    common._validate_bc_alignment(
        db=db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        expected_bc_id=bc_id,
        advertiser=advertiser,
        store=store,
    )
    common._enforce_products_limits(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
    )
    raw_eligibility = body.product_eligibility or "gmv_max"
    product_eligibility = str(raw_eligibility).strip().lower()
    if product_eligibility not in {"gmv_max", "ads", "all"}:
        raise APIError(
            "INVALID_PRODUCT_ELIGIBILITY",
            "product_eligibility must be one of gmv_max, ads, all.",
            status.HTTP_400_BAD_REQUEST,
        )
    params = {
        "mode": normalized_mode,
        "advertiser_id": str(advertiser_id),
        "store_id": str(store_id),
        "product_eligibility": product_eligibility,
    }
    if bc_id:
        params["bc_id"] = bc_id
    try:
        result = dispatch_sync(
            db,
            workspace_id=int(workspace_id),
            provider=normalized_provider,
            auth_id=int(auth_id),
            scope="products",
            params=params,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
            actor_ip=request.client.host if request.client else None,
            idempotency_key=requested_idempotency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    status_value = result.status
    run = result.run
    if wait_for_completion and run:
        completed = common._wait_for_run_completion(
            db,
            run_id=int(run.id),
            timeout_seconds=wait_timeout,
            interval_seconds=settings.TTB_SYNC_WAIT_INTERVAL_SECONDS,
        )
        run = completed or run
        if completed:
            status_value = completed.status or status_value
    return common.SyncResponse(
        run_id=int(run.id) if run else None,
        schedule_id=int(run.schedule_id) if run and run.schedule_id else None,
        task_name=common.SYNC_TASKS["products"],
        task_id=result.task_id,
        status=status_value,
        idempotent=result.idempotent,
        idempotency_key=result.run.idempotency_key if result.run else None,
    )


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/sync-runs/{run_id}",
    response_model=common.SyncRunResponse,
)
def get_sync_run(
    workspace_id: int,
    provider: str,
    auth_id: int,
    run_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.SyncRunResponse:
    """Retrieve detailed information about a specific sync run."""
    normalized_provider = common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    run = db.get(ScheduleRun, int(run_id))
    if not run or run.workspace_id != int(workspace_id):
        raise HTTPException(status_code=404, detail="sync run not found")
    if not _run_matches_account(run, normalized_provider, auth_id):
        raise HTTPException(status_code=404, detail="sync run not found")
    schedule = db.get(Schedule, int(run.schedule_id)) if run.schedule_id else None
    return common.SyncRunResponse(
        id=int(run.id),
        schedule_id=int(run.schedule_id),
        task_name=schedule.task_name if schedule else "",
        status=run.status,
        scheduled_for=run.scheduled_for.isoformat() if run.scheduled_for else "",
        enqueued_at=run.enqueued_at.isoformat() if run.enqueued_at else None,
        duration_ms=run.duration_ms,
        error_code=run.error_code,
        error_message=run.error_message,
        stats=run.stats_json,
    )


__all__ = ["router", "trigger_sync", "get_sync_run"]
