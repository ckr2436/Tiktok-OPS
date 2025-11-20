"""
Endpoints related to provider and account listings for the TikTok Business
tenant API.  These routes handle listing of OAuth bindings at both the
provider level and the workspace level.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_member
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAccountTTB
from sqlalchemy import func

from . import common


# Subrouter for account‑related endpoints.  No prefix is specified here
# because the parent router defined in __init__.py sets the tenant prefix.
router = APIRouter()


@router.get(
    "/{workspace_id}/providers",
    response_model=common.ProviderAccountsResponse,
)
def list_providers(
    workspace_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.ProviderAccountsResponse:
    """List all provider OAuth accounts for a workspace.

    This returns a paginated list of all TikTok Business bindings for the
    specified workspace.  The provider field on each item is hard‑coded to
    ``tiktok‑business`` since that is the only supported provider.
    """
    query = db.query(OAuthAccountTTB).filter(OAuthAccountTTB.workspace_id == int(workspace_id))
    total = int(query.with_entities(func.count()).scalar() or 0)
    rows = (
        query.order_by(OAuthAccountTTB.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [common._serialize_binding(row) for row in rows]
    return common.ProviderAccountsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts",
    response_model=common.ProviderAccountListResponse,
)
def list_provider_accounts(
    workspace_id: int,
    provider: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.ProviderAccountListResponse:
    """List the account bindings for a specific provider.

    This endpoint validates the provider name, loads all OAuth accounts for
    the workspace and triggers an automatic meta backfill if the account
    appears to be missing data.
    """
    # Validate that the provider is supported.  The returned value is
    # normalized to the canonical provider key but is not used further here.
    common._normalize_provider(provider)
    query = db.query(OAuthAccountTTB).filter(OAuthAccountTTB.workspace_id == int(workspace_id))
    total = int(query.with_entities(func.count()).scalar() or 0)
    rows = (
        query.order_by(OAuthAccountTTB.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    # Ensure meta data has been seeded for each account; this may enqueue
    # background sync jobs for newly authorized accounts.
    for row in rows:
        common._ensure_account_meta_seeded(db, workspace_id=workspace_id, account=row)
    items = [common._serialize_account_summary(row) for row in rows]
    return common.ProviderAccountListResponse(items=items, page=page, page_size=page_size, total=total)


__all__ = ["router", "list_providers", "list_provider_accounts"]
