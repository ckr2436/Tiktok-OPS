from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.services.ttb_gmvmax import sync_gmvmax_campaigns

from ._helpers import (
    ensure_account,
    get_advertiser_id_for_account,
    get_ttb_client_for_account,
)
from .schemas import (
    GmvMaxCampaignListResponse,
    GmvMaxCampaignOut,
    GmvMaxSyncResponse,
)

router = APIRouter()


@router.post(
    "/campaigns/sync",
    response_model=GmvMaxSyncResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_campaigns_handler(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> GmvMaxSyncResponse:
    if advertiser_id is None:
        advertiser_id = get_advertiser_id_for_account(db, workspace_id, provider, auth_id)

    client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
    try:
        result = await sync_gmvmax_campaigns(
            db,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            status=status_filter,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        await client.aclose()

    return GmvMaxSyncResponse(synced=result["synced"])


@router.get(
    "/campaigns",
    response_model=GmvMaxCampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_campaigns_handler(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> GmvMaxCampaignListResponse:
    ensure_account(db, workspace_id, provider, auth_id)

    query = (
        db.query(TTBGmvMaxCampaign)
        .filter(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .filter(TTBGmvMaxCampaign.auth_id == auth_id)
    )
    if advertiser_id:
        query = query.filter(TTBGmvMaxCampaign.advertiser_id == advertiser_id)
    if status_filter:
        query = query.filter(TTBGmvMaxCampaign.status == status_filter)
    if q:
        pattern = f"%{q}%"
        query = query.filter(TTBGmvMaxCampaign.name.ilike(pattern))

    total = query.count()
    items = (
        query.order_by(TTBGmvMaxCampaign.ext_created_time.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return GmvMaxCampaignListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[GmvMaxCampaignOut.from_orm(it) for it in items],
    )
