"""Deprecated TikTok Business sync trigger routes under oauth_ttb."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.config import settings

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business OAuth (deprecated)"]
)

_DEPRECATION_DETAIL = (
    "This endpoint was removed. Use /api/v1/tenants/providers/tiktok-business/* instead. "
    "Legacy route will be deleted after 2024-12-31."
)


@router.api_route(
    "/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def deprecated_sync_route(**_: dict) -> None:
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_DEPRECATION_DETAIL)
