"""
Deprecated catch‑all route for legacy tenants/ttb endpoints.  Requests to
these paths will return a 404 along with a message pointing to the new
tenant API base path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status


# Message returned when legacy routes are accessed.  The path in this message
# references the current API prefix to guide clients to the correct base.
_DEPRECATION_DETAIL = (
    "This endpoint was replaced by /api/v1/tenants/providers/tiktok-business/*. "
    "Legacy tenants/ttb routes have been removed."
)


# Subrouter for deprecated endpoints.  These routes are hidden from the
# generated OpenAPI schema.
router = APIRouter()


@router.api_route(
    "/{workspace_id}/ttb/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def deprecated_ttb_routes(**_: dict) -> None:
    """Return a 404 for any legacy ttb route."""
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DEPRECATION_DETAIL)


__all__ = ["router", "deprecated_ttb_routes"]
