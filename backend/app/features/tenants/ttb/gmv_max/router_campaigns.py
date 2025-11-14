from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .schemas import (
    CampaignDetailResponse,
    CampaignListResponse,
    SyncRequest,
    SyncResponse,
)
from .router_provider import (
    GMVMaxRouteContext,
    get_gmvmax_campaign_provider,
    get_route_context as get_provider_context,
    list_gmvmax_campaigns_provider,
    sync_gmvmax_campaigns_provider,
)

PROVIDER_SLUG = "tiktok-business"

router = APIRouter(prefix="/gmvmax")


def get_deprecated_route_context(
    workspace_id: int,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return get_provider_context(workspace_id, PROVIDER_SLUG, auth_id, db)


@router.post(
    "/sync",
    response_model=SyncResponse,
    dependencies=[Depends(require_tenant_admin)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/sync instead.
async def sync_gmvmax_campaigns_handler(
    workspace_id: int,
    auth_id: int,
    payload: SyncRequest,
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> SyncResponse:
    return await sync_gmvmax_campaigns_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        payload=payload,
        context=context,
    )


@router.get(
    "",
    response_model=CampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax instead.
async def list_gmvmax_campaigns_handler(
    workspace_id: int,
    auth_id: int,
    gmv_max_promotion_types: list[str] | None = Query(None),
    store_ids: list[str] | None = Query(None),
    campaign_ids: list[str] | None = Query(None),
    campaign_name: str | None = Query(None),
    primary_status: str | None = Query(None),
    creation_filter_start_time: str | None = Query(None),
    creation_filter_end_time: str | None = Query(None),
    fields: list[str] | None = Query(None),
    page: int | None = Query(None, ge=1),
    page_size: int | None = Query(None, ge=1, le=50),
    advertiser_id: str | None = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> CampaignListResponse:
    return await list_gmvmax_campaigns_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        gmv_max_promotion_types=gmv_max_promotion_types,
        store_ids=store_ids,
        campaign_ids=campaign_ids,
        campaign_name=campaign_name,
        primary_status=primary_status,
        creation_filter_start_time=creation_filter_start_time,
        creation_filter_end_time=creation_filter_end_time,
        fields=fields,
        page=page,
        page_size=page_size,
        advertiser_id=advertiser_id,
        context=context,
    )


@router.get(
    "/{campaign_id}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id} instead.
async def get_gmvmax_campaign_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: str | None = Query(None),
    include_sessions: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> CampaignDetailResponse:
    return await get_gmvmax_campaign_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
        include_sessions=include_sessions,
        context=context,
    )
