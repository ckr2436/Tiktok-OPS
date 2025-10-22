# app/features/tenants/oauth_ttb/router_jobs.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from celery.result import AsyncResult

from app.core.deps import SessionUser, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.services.platform_tasks import get_last_sync_job

router = APIRouter(tags=["tenant.tiktok-business.jobs"])

BASE_PREFIX = "/api/v1/tenants/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync"


def _norm_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("tiktok-business", "tiktok_business"):
        return "tiktok-business"
    raise HTTPException(status_code=400, detail="unsupported provider")


def _binding_recent_jobs_key(workspace_id: int, auth_id: int) -> str:
    return f"jobs:ttb:{workspace_id}:{auth_id}"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SyncLastResponse(BaseModel):
    status: str
    triggered_at: str | None = None
    finished_at: str | None = None
    duration_sec: int | None = None
    summary: str | None = None
    next_allowed_at: str | None = Field(default=None, alias="next_allowed_at")


@router.get(f"{BASE_PREFIX}/jobs/{{job_id}}")
def get_job(
    workspace_id: int,
    provider: str,
    auth_id: int,
    job_id: str,
):
    _norm_provider(provider)
    res = AsyncResult(job_id, app=celery_app)
    data = {
        "job_id": job_id,
        "action": getattr(res, "name", None),
        "state": res.state,
        "result": None,
        "error": None,
        "progress": None,
    }
    info = res.info
    if isinstance(info, dict):
        data["result"] = info.get("result")
        data["error"] = info.get("error")
        data["progress"] = info.get("progress")
    return data


@router.get(f"{BASE_PREFIX}/jobs")
def list_jobs(
    workspace_id: int,
    provider: str,
    auth_id: int,
    limit: int = Query(default=20, ge=1, le=100),
):
    _norm_provider(provider)
    backend = getattr(celery_app, "backend", None)
    client = getattr(backend, "client", None)
    items: List[dict] = []
    if client:
        try:
            key = _binding_recent_jobs_key(workspace_id, auth_id)
            job_ids = client.lrange(key, 0, limit - 1)  # newest first
            for raw in job_ids or []:
                jid = raw.decode("utf-8")
                res = AsyncResult(jid, app=celery_app)
                items.append({"job_id": jid, "state": res.state, "action": getattr(res, "name", None)})
        except Exception:
            pass
    return {"items": items}


@router.get(f"{BASE_PREFIX}/last", response_model=SyncLastResponse)
def get_last_sync(
    workspace_id: int,
    provider: str,
    auth_id: int,
    response: Response,
    *,
    kind: str = Query(default="products"),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    p = _norm_provider(provider)
    job = get_last_sync_job(
        db,
        workspace_id=workspace_id,
        provider=p,
        auth_id=auth_id,
        kind=kind,
    )
    if not job:
        raise APIError("JOB_NOT_FOUND", "No sync history", 404)

    next_allowed_iso = _iso(job.next_allowed_at)
    if response is not None and next_allowed_iso:
        response.headers["X-Next-Allowed-At"] = next_allowed_iso
        if job.next_allowed_at:
            retry_after = int((job.next_allowed_at - datetime.now(timezone.utc)).total_seconds())
            if retry_after > 0:
                response.headers["Retry-After"] = str(retry_after)

    return SyncLastResponse(
        status=job.status,
        triggered_at=_iso(job.triggered_at),
        finished_at=_iso(job.finished_at),
        duration_sec=job.duration_sec,
        summary=job.summary,
        next_allowed_at=_iso(job.next_allowed_at),
    )

