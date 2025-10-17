# app/features/tenants/oauth_ttb/router_sync_all.py
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from app.celery_app import celery_app

router = APIRouter(tags=["tenant.tiktok-business.bootstrap"])

BASE_PREFIX = "/api/v1/tenants/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync"


def _norm_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("tiktok-business", "tiktok_business"):
        return "tiktok-business"
    raise HTTPException(status_code=400, detail="unsupported provider")


@router.post(f"{BASE_PREFIX}/bootstrap")
def trigger_bootstrap(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _norm_provider(provider)
    if not idempotency_key or len(idempotency_key) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    task = celery_app.send_task(
        "tenant.ttb.sync.bootstrap_orchestrator",
        kwargs={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "idempotency_key": idempotency_key,
        },
        queue="gmv.tasks.default",
    )
    return {"job_id": task.id, "accepted": True, "action": "bootstrap", "steps": ["bc", "advertisers", "shops", "products"]}

