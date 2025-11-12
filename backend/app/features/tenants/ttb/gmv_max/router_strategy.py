from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .schemas import (
    GmvMaxStrategyConfigIn,
    GmvMaxStrategyConfigOut,
    GmvMaxStrategyPreviewResponse,
)
from .service import get_strategy, preview_strategy, update_strategy

PROVIDER_ALIAS = "tiktok_business"

router = APIRouter(prefix="/gmvmax")


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def _decimal_to_float(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _serialize_strategy(cfg) -> GmvMaxStrategyConfigOut:
    return GmvMaxStrategyConfigOut(
        workspace_id=cfg.workspace_id,
        auth_id=cfg.auth_id,
        campaign_id=cfg.campaign_id,
        enabled=bool(cfg.enabled),
        target_roi=_decimal_to_str(cfg.target_roi),
        min_roi=_decimal_to_str(cfg.min_roi),
        max_roi=_decimal_to_str(cfg.max_roi),
        min_impressions=cfg.min_impressions,
        min_clicks=cfg.min_clicks,
        max_budget_raise_pct_per_day=_decimal_to_float(cfg.max_budget_raise_pct_per_day),
        max_budget_cut_pct_per_day=_decimal_to_float(cfg.max_budget_cut_pct_per_day),
        max_roas_step_per_adjust=_decimal_to_str(cfg.max_roas_step_per_adjust),
        cooldown_minutes=cfg.cooldown_minutes,
        min_runtime_minutes_before_first_change=cfg.min_runtime_minutes_before_first_change,
    )


@router.get(
    "/{campaign_id}/strategy",
    response_model=GmvMaxStrategyConfigOut,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    db: Session = Depends(get_db),
) -> GmvMaxStrategyConfigOut:
    cfg = get_strategy(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )
    return _serialize_strategy(cfg)


@router.put(
    "/{campaign_id}/strategy",
    response_model=GmvMaxStrategyConfigOut,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxStrategyConfigIn,
    db: Session = Depends(get_db),
) -> Response | GmvMaxStrategyConfigOut:
    data = payload.model_dump(exclude_unset=True)
    cfg = update_strategy(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=data,
    )
    if cfg is None:
        return Response(status_code=204)
    return _serialize_strategy(cfg)


@router.post(
    "/{campaign_id}/strategies/preview",
    response_model=GmvMaxStrategyPreviewResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def preview_gmvmax_strategy_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: dict[str, Any] | None = None,
    db: Session = Depends(get_db),
) -> GmvMaxStrategyPreviewResponse:
    result = preview_strategy(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )
    return GmvMaxStrategyPreviewResponse(**result)
