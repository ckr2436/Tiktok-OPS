"""Tenant-level GMV Max helper endpoints.

These routes provide lightweight entry points for triggering GMV Max sync
tasks and polling Celery task status without requiring callers to construct
the full provider/account path. They delegate to the same Celery workers
used by the provider-scoped routes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.core.deps import require_tenant_admin, require_tenant_member
from app.celery_app import celery_app
from app.data.db import get_db
from app.services.ttb_binding_config import get_binding_config

from .schemas import SyncRequest, SyncTaskResponse, SyncTaskStateResponse


router = APIRouter()


@router.post(
    "/{workspace_id}/gmvmax/sync",
    response_model=SyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def start_gmvmax_sync(
    workspace_id: int,
    payload: SyncRequest,
    provider: str = Query("tiktok-business", description="Provider identifier"),
    auth_id: int = Query(..., description="OAuth account id for the workspace"),
    advertiser_id_query: Optional[str] = Query(None, alias="advertiser_id"),
    store_id_query: Optional[str] = Query(None, alias="store_id"),
    db=Depends(get_db),
) -> SyncTaskResponse:
    """Enqueue a GMV Max sync job for the given workspace scope.

    The task leverages the same Celery worker used by provider-scoped routes
    but accepts parameters via the request body and query string to simplify
    frontend integration.
    """

    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    advertiser_id = payload.advertiser_id or advertiser_id_query
    store_id = payload.store_id or store_id_query
    if not advertiser_id:
        advertiser_id = binding.advertiser_id if binding else None
    if not store_id:
        store_id = binding.store_id if binding else None

    if not advertiser_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="advertiser_id is required to start GMV Max sync",
        )

    task_kwargs: Dict[str, Any] = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "advertiser_id": str(advertiser_id),
    }

    filters: Dict[str, Any] = {}
    if store_id:
        filters["store_ids"] = [str(store_id)]
    if payload.campaign_filter:
        filters.update(payload.campaign_filter.model_dump(exclude_none=True, by_alias=True))
    if payload.campaign_options:
        filters.update(payload.campaign_options.model_dump(exclude_none=True, by_alias=True))
    if filters:
        task_kwargs["filters"] = filters

    params_payload = payload.model_dump(exclude_none=True, by_alias=True)
    if params_payload:
        task_kwargs["params"] = params_payload

    async_res = celery_app.send_task(
        "gmvmax.sync_campaigns",
        kwargs=task_kwargs,
        queue="gmvmax",
    )
    task_id = async_res.id or ""
    status_url = (
        f"/tenants/{workspace_id}/gmvmax/tasks/{task_id}?provider={provider}&auth_id={auth_id}"
        if task_id
        else None
    )

    return SyncTaskResponse(task_id=task_id, state=str(async_res.state), status_url=status_url)


@router.get(
    "/{workspace_id}/gmvmax/tasks/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_member)],
)
def get_gmvmax_task_state(
    workspace_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    provider: str = Query("tiktok-business", description="Provider identifier"),
    auth_id: int = Query(..., description="OAuth account id for the workspace"),
) -> SyncTaskStateResponse:
    """Return Celery task status for GMV Max sync or async jobs."""

    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    state = str(res.state)
    info = res.info if isinstance(res.info, dict) else {}
    error = info.get("error") if state in {"FAILURE", "RETRY"} else None
    result = None
    if state == "SUCCESS":
        result = info.get("result") if info else res.result

    return SyncTaskStateResponse(task_id=task_id, state=state, result=result, error=error)


@router.get(
    "/{workspace_id}/tasks/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_member)],
)
def get_tenant_task_state(
    workspace_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    provider: Optional[str] = Query(None, description="Optional provider identifier"),
    auth_id: Optional[int] = Query(None, description="Optional OAuth account id for the workspace"),
) -> SyncTaskStateResponse:
    """Generic task status endpoint for GMV Max Celery tasks."""

    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    state = str(res.state)
    info = res.info if isinstance(res.info, dict) else {}
    error = info.get("error") if state in {"FAILURE", "RETRY"} else None
    result = None
    if state == "SUCCESS":
        result = info.get("result") if info else res.result

    return SyncTaskStateResponse(task_id=task_id, state=state, result=result, error=error)

