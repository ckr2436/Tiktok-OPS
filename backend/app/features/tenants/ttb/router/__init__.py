"""
Aggregate router for the TikTok Business tenant API.

This module defines the top‑level FastAPI router used by the
``tenants`` feature.  It mounts the subrouters implemented in this
package (accounts, sync, meta, binding, and deprecated).  The
resulting router has the same prefix and top‑level tag as the
original monolithic router, but it removes the redundant ``gmvmax``
tag from the GMV Max provider routes.  If the optional GMV Max
provider router is available, it is mounted under the expected
account path without any additional tags.  If the import fails
because the underlying package is missing, the router is simply not
included.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings

from . import accounts, binding, deprecated, meta, sync


# Create the aggregate router with the same prefix and tag as the
# original monolithic router.  The prefix anchors the tenant routes
# under ``/api/v1/tenants`` (or whatever ``API_PREFIX`` is set to) and
# the tag groups these routes together in the generated OpenAPI schema.
router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business"],
)

# Mount the feature‑specific subrouters.  Each subrouter defines
# its own paths beginning with ``/{workspace_id}/...``, so we do not
# specify an additional prefix here.  The tags of the subrouters
# propagate automatically.
router.include_router(accounts.router)
router.include_router(sync.router)
router.include_router(meta.router)
router.include_router(binding.router)
router.include_router(deprecated.router)


# Attempt to include the GMV Max provider routes.  These routes live
# in the ``gmv_max.router_provider`` module in the original codebase.
# We intentionally omit the ``tags`` argument here to avoid the
# duplicate ``gmvmax`` tag that existed in the monolithic router.  If
# the import fails (e.g., because the package is not present in this
# environment), we simply do not mount the routes.
try:
    from ..gmv_max.router_provider import router as gmv_max_provider_router  # type: ignore

    # Mount under the same prefix used in the original router.  This
    # prefix nests the GMV Max provider routes under the account path.
    router.include_router(
        gmv_max_provider_router,
        prefix="/{workspace_id}/providers/{provider}/accounts/{auth_id}",
    )
except Exception:
    # No GMV Max provider module available; skip inclusion.
    pass

__all__ = ["router"]
