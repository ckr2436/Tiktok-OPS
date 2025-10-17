# app/features/tenants/oauth_ttb/router_jobs.py
from __future__ import annotations

from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query

from app.celery_app import celery_app
from celery.result import AsyncResult

router = APIRouter(tags=["tenant.tiktok-business.jobs"])

BASE_PREFIX = "/api/v1/tenants/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync"


def _norm_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("tiktok-business", "tiktok_business"):
        return "tiktok-business"
    raise HTTPException(status_code=400, detail="unsupported provider")


def _binding_recent_jobs_key(workspace_id: int, auth_id: int) -> str:
    return f"jobs:ttb:{workspace_id}:{auth_id}"


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

