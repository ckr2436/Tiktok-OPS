from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .router_provider import (
    GMVMaxRouteContext,
    get_gmvmax_strategy_provider,
    get_route_context as get_provider_context,
    preview_gmvmax_strategy_provider,
    update_gmvmax_strategy_provider,
)
from .schemas import (
    StrategyPreviewRequest,
    StrategyPreviewResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    StrategyUpdateResponse,
)

PROVIDER_SLUG = "tiktok-business"

router = APIRouter(prefix="/gmvmax")


def get_deprecated_route_context(
    workspace_id: int,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return get_provider_context(workspace_id, PROVIDER_SLUG, auth_id, db)


@router.get(
    "/{campaign_id}/strategy",
    response_model=StrategyResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/strategy instead.
async def get_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str | None = Query(None),
    include_recommendation: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> StrategyResponse:
    return await get_gmvmax_strategy_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
        include_recommendation=include_recommendation,
        context=context,
    )


@router.put(
    "/{campaign_id}/strategy",
    response_model=StrategyUpdateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/strategy instead.
async def update_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: StrategyUpdateRequest,
    advertiser_id: str | None = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> StrategyUpdateResponse:
    return await update_gmvmax_strategy_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=payload,
        advertiser_id=advertiser_id,
        context=context,
    )


@router.post(
    "/{campaign_id}/strategies/preview",
    response_model=StrategyPreviewResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/strategies/preview instead.
async def preview_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: StrategyPreviewRequest,
    advertiser_id: str | None = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> StrategyPreviewResponse:
    return await preview_gmvmax_strategy_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=payload,
        advertiser_id=advertiser_id,
        context=context,
    )
