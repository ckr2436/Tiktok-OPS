from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .schemas import (
    GmvMaxCampaignDetailResponse,
    GmvMaxCampaignListQuery,
    GmvMaxCampaignListResponse,
    GmvMaxCampaignOut,
    GmvMaxSyncResponse,
)
from .service import get_campaign, list_campaigns, sync_campaigns

PROVIDER_ALIAS = "tiktok_business"

router = APIRouter()


@router.post(
    "/campaigns/sync",
    response_model=GmvMaxSyncResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_campaigns_handler(
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> GmvMaxSyncResponse:
    synced = await sync_campaigns(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        status_filter=status_filter,
    )
    return GmvMaxSyncResponse(synced=synced)


@router.get(
    "/",
    response_model=GmvMaxCampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_campaigns_handler(
    workspace_id: int,
    auth_id: int,
    filters: GmvMaxCampaignListQuery = Depends(GmvMaxCampaignListQuery),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sync: bool = Query(False),
    db: Session = Depends(get_db),
) -> GmvMaxCampaignListResponse:
    payload = await list_campaigns(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        advertiser_id=filters.advertiser_id,
        store_id=filters.store_id,
        business_center_id=filters.business_center_id,
        status_filter=filters.status,
        search=filters.search,
        page=page,
        page_size=page_size,
        sync=sync,
    )
    return GmvMaxCampaignListResponse(
        total=payload["total"],
        page=payload["page"],
        page_size=payload["page_size"],
        items=[GmvMaxCampaignOut.from_orm(item) for item in payload["items"]],
        synced=payload.get("synced"),
    )


@router.get(
    "/{campaign_id}",
    response_model=GmvMaxCampaignDetailResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_campaign_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: str | None = Query(None),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
) -> GmvMaxCampaignDetailResponse:
    campaign = await get_campaign(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
        refresh=refresh,
    )
    return GmvMaxCampaignDetailResponse(campaign=GmvMaxCampaignOut.from_orm(campaign))
