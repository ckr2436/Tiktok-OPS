"""Aggregate router for the TikTok Business tenant API."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings

from ..gmv_max.router_provider import router as gmv_max_provider_router
from ..gmv_max.router_tenant import router as gmv_max_tenant_router
from ..website_ads.router import router as website_ads_router
from . import accounts, binding, meta, sync


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business"],
)

router.include_router(accounts.router)
router.include_router(sync.router)
router.include_router(meta.router)
router.include_router(binding.router)

router.include_router(
    gmv_max_provider_router,
    prefix="/{workspace_id}/providers/{provider}/accounts/{auth_id}",
)
router.include_router(gmv_max_tenant_router)

router.include_router(
    website_ads_router,
    prefix="/{workspace_id}/providers/{provider}/accounts/{auth_id}",
)

__all__ = ["router"]
