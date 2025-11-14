from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .router_provider import (
    GMVMaxRouteContext,
    get_route_context as get_provider_context,
    query_gmvmax_metrics_provider,
    sync_gmvmax_metrics_provider,
)
from .schemas import MetricsRequest, MetricsResponse

PROVIDER_SLUG = "tiktok-business"

router = APIRouter(prefix="/gmvmax")


def get_deprecated_route_context(
    workspace_id: int,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return get_provider_context(workspace_id, PROVIDER_SLUG, auth_id, db)


@router.post(
    "/{campaign_id}/metrics/sync",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_admin)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/metrics/sync instead.
async def sync_gmvmax_metrics_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: MetricsRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> MetricsResponse:
    return await sync_gmvmax_metrics_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=payload,
        advertiser_id=advertiser_id,
        context=context,
    )


@router.get(
    "/{campaign_id}/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/metrics instead.
async def query_gmvmax_metrics_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> MetricsResponse:
    return await query_gmvmax_metrics_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        start_date=start_date,
        end_date=end_date,
        advertiser_id=advertiser_id,
        context=context,
    )
