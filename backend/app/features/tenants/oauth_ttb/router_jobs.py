"""Deprecated TikTok Business job status routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import settings

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business OAuth (deprecated)"]
)

_DEPRECATION_DETAIL = (
    "Job inspection moved to /api/v1/tenants/providers/tiktok-business/sync-runs. "
    "Legacy route will be deleted after 2024-12-31."
)


@router.api_route(
    "/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync/jobs/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def deprecated_jobs_route(**_: dict) -> None:
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_DEPRECATION_DETAIL)
