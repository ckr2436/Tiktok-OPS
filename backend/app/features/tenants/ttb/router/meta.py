"""
Endpoints for retrieving meta data (business centers, advertisers, stores,
and products) associated with a TikTok Business tenant.  These routes
provide paginated and filtered listings and handle automatic backfilling
when data is missing.
"""

from __future__ import annotations

from typing import Dict, Any, List, Optional, Tuple, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, func, select
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBAdvertiserStoreLink,
    TTBBCAdvertiserLink,
    TTBProduct,
)
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxCampaignProduct

from . import common

# Import identifier normalization utility from the original sync module
from app.services.ttb_sync import _normalize_identifier


# Subrouter for meta‑related endpoints.  Paths are defined relative to the
# tenant prefix configured in __init__.py.
router = APIRouter()


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/business-centers",
    response_model=common.BusinessCenterList,
)
def list_account_business_centers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.BusinessCenterList:
    """Return all business centers for the given account."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBBusinessCenter)
        .filter(TTBBusinessCenter.workspace_id == int(workspace_id))
        .filter(TTBBusinessCenter.auth_id == int(auth_id))
    )
    rows = query.order_by(TTBBusinessCenter.name.asc(), TTBBusinessCenter.bc_id.asc()).all()
    if not rows:
        common._backfill_meta_if_needed(db, workspace_id=workspace_id, auth_id=auth_id)
        db.expire_all()
        rows = query.order_by(TTBBusinessCenter.name.asc(), TTBBusinessCenter.bc_id.asc()).all()
    items = [common.BusinessCenterItem(**common._serialize_bc(r)) for r in rows]
    return common.BusinessCenterList(items=items)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers",
    response_model=common.AdvertiserList,
)
def list_account_advertisers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.AdvertiserList:
    """Return advertisers linked to the given account, optionally filtered by BC."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    # Legacy bc_id parameter is no longer supported
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    query = (
        db.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
    )
    normalized_owner = _normalize_identifier(owner_bc_id)
    if normalized_owner:
        link_subquery = (
            db.query(TTBBCAdvertiserLink.advertiser_id)
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.bc_id == normalized_owner)
        )
        query = query.filter(
            or_(TTBAdvertiser.advertiser_id.in_(link_subquery), TTBAdvertiser.bc_id == normalized_owner)
        )
    rows = query.order_by(TTBAdvertiser.display_name.asc(), TTBAdvertiser.advertiser_id.asc()).all()
    if not rows:
        common._backfill_meta_if_needed(db, workspace_id=workspace_id, auth_id=auth_id)
        db.expire_all()
        rows = query.order_by(TTBAdvertiser.display_name.asc(), TTBAdvertiser.advertiser_id.asc()).all()
    advertiser_ids = [str(row.advertiser_id) for row in rows if row and row.advertiser_id]
    bc_hints: Dict[str, Tuple[int, str]] = {}
    if advertiser_ids:
        link_rows = (
            db.query(
                TTBBCAdvertiserLink.advertiser_id,
                TTBBCAdvertiserLink.bc_id,
                TTBBCAdvertiserLink.relation_type,
            )
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.advertiser_id.in_(advertiser_ids))
            .all()
        )
        for adv_id, bc_id, relation_type in link_rows:
            if not adv_id or not bc_id:
                continue
            key = str(adv_id)
            rank = common._relation_rank(relation_type)
            existing = bc_hints.get(key)
            if existing is None or rank < existing[0]:
                bc_hints[key] = (rank, str(bc_id))
    items: List[common.AdvertiserItem] = []
    for row in rows:
        payload = common._serialize_adv(row)
        adv_id = payload.get("advertiser_id")
        if adv_id:
            hint = bc_hints.get(str(adv_id))
            if hint and not payload.get("bc_id"):
                payload["bc_id"] = hint[1]
        items.append(common.AdvertiserItem(**payload))
    return common.AdvertiserList(items=items)


def _build_account_store_list(
    db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str
) -> common.StoreList:
    """Build a list of stores linked to the given advertiser for the account."""
    normalized_adv = _normalize_identifier(advertiser_id)
    if not normalized_adv:
        return common.StoreList(items=[])
    link_rows = (
        db.query(
            TTBAdvertiserStoreLink.store_id,
            TTBAdvertiserStoreLink.relation_type,
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
        )
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)
        .all()
    )
    linked_store_ids = {str(store_id) for store_id, *_ in link_rows if store_id}
    if not linked_store_ids:
        return common.StoreList(items=[])
    store_rows: List[TTBStore] = (
        db.query(TTBStore)
        .filter(TTBStore.workspace_id == int(workspace_id))
        .filter(TTBStore.auth_id == int(auth_id))
        .filter(TTBStore.store_id.in_(linked_store_ids))
        .all()
    )
    best_link_by_store: Dict[str, Dict[str, Any]] = {}
    for store_id, relation_type, store_authorized_bc_id, bc_id_hint in link_rows:
        sid = str(store_id)
        current = best_link_by_store.get(sid)
        if (
            current is None
            or common._relation_rank(relation_type) < common._relation_rank(current.get("relation_type"))
        ):
            best_link_by_store[sid] = {
                "relation_type": relation_type,
                "store_authorized_bc_id": store_authorized_bc_id,
                "bc_id_hint": bc_id_hint,
            }
    items: List[Dict[str, Any]] = []
    for row in store_rows:
        payload = common._serialize_store(row)
        payload["advertiser_id"] = normalized_adv
        link_info = best_link_by_store.get(str(row.store_id)) or {}
        if not payload.get("store_authorized_bc_id") and link_info.get("store_authorized_bc_id"):
            payload["store_authorized_bc_id"] = link_info["store_authorized_bc_id"]
        if not payload.get("bc_id") and link_info.get("bc_id_hint"):
            payload["bc_id"] = link_info["bc_id_hint"]
        items.append(payload)
    return common.StoreList(items=items)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/stores",
    response_model=common.StoreList,
)
def list_account_stores_query(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    advertiser_id: str = Query(..., max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.StoreList:
    """List stores for an advertiser using a query parameter."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    stores = _build_account_store_list(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    if not stores.items:
        common._backfill_meta_if_needed(db, workspace_id=workspace_id, auth_id=auth_id)
        db.expire_all()
        stores = _build_account_store_list(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
        )
    return stores


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers/{advertiser_id}/stores",
    response_model=common.StoreList,
)
def list_account_stores(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.StoreList:
    """List stores for a specific advertiser using a path parameter."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    stores = _build_account_store_list(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    if not stores.items:
        common._backfill_meta_if_needed(db, workspace_id=workspace_id, auth_id=auth_id)
        db.expire_all()
        stores = _build_account_store_list(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
        )
    return stores


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/products",
    response_model=common.ProductList,
)
def list_account_products(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    store_id: str = Query(..., max_length=64),
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(200, ge=1, le=500),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.ProductList:
    """List products for a given store, optionally filtering by advertiser and BC."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    normalized_store = _normalize_identifier(store_id)
    if not normalized_store:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="store_id is required",
        )
    normalized_adv = _normalize_identifier(advertiser_id)
    normalized_owner = _normalize_identifier(owner_bc_id)
    store = common._get_store(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=normalized_store,
    )
    advertiser = None
    if normalized_adv:
        advertiser = common._get_advertiser(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_adv,
        )
    # If an owner BC is provided without an advertiser filter, verify it aligns with the store
    if normalized_owner and not advertiser:
        store_candidates, _ = common._resolve_store_bc_candidates(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            store_id=normalized_store,
        )
        store_candidates = common._collect_bc_candidates(
            store.bc_id,
            store.store_authorized_bc_id,
            *store_candidates,
        )
        if store_candidates and normalized_owner not in store_candidates:
            raise APIError(
                "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
                "Store belongs to a different business center.",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
    if advertiser:
        expected_bc = normalized_owner or advertiser.bc_id or store.bc_id
        common._validate_bc_alignment(
            db=db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            expected_bc_id=expected_bc,
            advertiser=advertiser,
            store=store,
        )
        link_exists = (
            db.query(TTBAdvertiserStoreLink.id)
            .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
            .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
            .filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)
            .filter(TTBAdvertiserStoreLink.store_id == normalized_store)
            .first()
        )
        if link_exists is None:
            raise APIError(
                "ADVERTISER_STORE_LINK_NOT_FOUND",
                "Store is not linked to the advertiser.",
                status.HTTP_404_NOT_FOUND,
            )
    offset = (page - 1) * page_size
    def _load_products() -> Tuple[int, List[TTBProduct], Set[str]]:
        base_query = (
            db.query(TTBProduct)
            .filter(TTBProduct.workspace_id == int(workspace_id))
            .filter(TTBProduct.auth_id == int(auth_id))
            .filter(TTBProduct.store_id == normalized_store)
        )
        assignment_stmt = (
            select(TTBGmvMaxCampaignProduct.item_group_id)
            .join(
                TTBGmvMaxCampaign,
                TTBGmvMaxCampaign.id == TTBGmvMaxCampaignProduct.campaign_pk,
            )
            .where(TTBGmvMaxCampaignProduct.workspace_id == int(workspace_id))
            .where(TTBGmvMaxCampaignProduct.auth_id == int(auth_id))
            .where(TTBGmvMaxCampaignProduct.store_id == str(normalized_store))
            .where(func.lower(TTBGmvMaxCampaign.operation_status) == "enable")
        )
        assigned_ids = {
            str(item)
            for item in db.execute(assignment_stmt).scalars().all()
            if item is not None
        }
        total_rows = base_query.count()
        rows = (
            base_query.order_by(TTBProduct.title.asc(), TTBProduct.product_id.asc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        return total_rows, rows, assigned_ids
    total, rows, assigned_ids = _load_products()
    if total == 0:
        common._backfill_products_if_needed(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            store_id=normalized_store,
            advertiser_id=normalized_adv,
        )
        db.expire_all()
        total, rows, assigned_ids = _load_products()
    return common.ProductList(
        items=[common.ProductItem(**common._serialize_product(r, assigned_ids=assigned_ids)) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = [
    "router",
    "list_account_business_centers",
    "list_account_advertisers",
    "list_account_stores_query",
    "list_account_stores",
    "list_account_products",
]
