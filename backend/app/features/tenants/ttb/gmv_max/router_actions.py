from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .schemas import ActionLogEntry, CampaignActionRequest, CampaignActionResponse
from .router_provider import (
    GMVMaxRouteContext,
    apply_gmvmax_campaign_action_provider,
    get_route_context as get_provider_context,
    list_gmvmax_action_logs_provider,
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
    "/{campaign_id}/actions",
    response_model=CampaignActionResponse,
    dependencies=[Depends(require_tenant_admin)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/actions instead.
async def apply_gmvmax_campaign_action_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: CampaignActionRequest,
    advertiser_id: str | None = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> CampaignActionResponse:
    return await apply_gmvmax_campaign_action_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=payload,
        advertiser_id=advertiser_id,
        context=context,
    )


@router.get(
    "/{campaign_id}/actions",
    response_model=ActionLogEntry,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/actions instead.
async def list_gmvmax_action_logs_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> ActionLogEntry:
    return await list_gmvmax_action_logs_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        context=context,
    )
