from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from pydantic import BaseModel

from .schemas import ActionLogEntry, CampaignActionRequest, CampaignActionResponse
from .router_provider import (
    GMVMaxRouteContext,
    apply_gmvmax_campaign_action_provider,
    get_route_context as get_provider_context,
    list_gmvmax_action_logs_provider,
)

PROVIDER_SLUG = "tiktok-business"

router = APIRouter(prefix="/gmvmax")


class LegacyCampaignActionType(str, Enum):
    """Legacy action types supported by the deprecated /ttb routes."""

    START = "START"
    PAUSE = "PAUSE"
    SET_BUDGET = "SET_BUDGET"
    SET_ROAS = "SET_ROAS"


class LegacyCampaignActionIn(BaseModel):
    """Legacy request payload accepted by the deprecated campaign action route."""

    action: LegacyCampaignActionType
    daily_budget_cents: Optional[int] = None
    roas_bid: Optional[Decimal] = None
    reason: Optional[str] = None


class LegacyCampaignOut(BaseModel):
    """Legacy campaign representation returned by deprecated routes."""

    id: int = 0
    campaign_id: str
    name: Optional[str] = None
    status: Optional[str] = None
    operation_status: Optional[str] = None
    advertiser_id: str
    store_id: Optional[str] = None
    shopping_ads_type: Optional[str] = None
    optimization_goal: Optional[str] = None
    roas_bid: Optional[Decimal] = None
    daily_budget_cents: Optional[int] = None
    currency: Optional[str] = None
    ext_created_time: Optional[datetime] = None
    ext_updated_time: Optional[datetime] = None


class LegacyCampaignActionOut(BaseModel):
    """Legacy response payload returned by the deprecated campaign action route."""

    action: LegacyCampaignActionType
    result: str
    campaign: LegacyCampaignOut


class LegacyActionLogOut(BaseModel):
    """Legacy action log entry representation."""

    id: int = 0
    action: str
    reason: Optional[str] = None
    performed_by: Optional[str] = None
    result: Optional[str] = None
    error_message: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None


class LegacyActionLogListResponse(BaseModel):
    """Legacy wrapper returned by the deprecated action log route."""

    campaign_id: str
    count: int
    items: List[LegacyActionLogOut]


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _convert_budget_to_cents(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        quantized = (Decimal(str(value)) * Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(quantized)
    except (ArithmeticError, ValueError):  # pragma: no cover - defensive
        return None


def _legacy_to_provider_request(
    payload: LegacyCampaignActionIn,
) -> CampaignActionRequest:
    """Translate a legacy campaign action request into the provider schema."""

    if payload.action == LegacyCampaignActionType.START:
        return CampaignActionRequest(type="enable", payload={})
    if payload.action == LegacyCampaignActionType.PAUSE:
        return CampaignActionRequest(type="pause", payload={})
    if payload.action == LegacyCampaignActionType.SET_BUDGET:
        if payload.daily_budget_cents is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="daily_budget_cents is required for SET_BUDGET",
            )
        budget_dollars = Decimal(payload.daily_budget_cents) / Decimal(100)
        return CampaignActionRequest(
            type="update_budget",
            payload={"budget": float(budget_dollars)},
        )
    if payload.action == LegacyCampaignActionType.SET_ROAS:
        if payload.roas_bid is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="roas_bid is required for SET_ROAS",
            )
        return CampaignActionRequest(
            type="update_strategy",
            payload={"roas_bid": float(payload.roas_bid)},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported action")


def _provider_response_to_legacy_campaign(
    *,
    campaign_id: str,
    advertiser_id: str,
    store_id: Optional[str],
    provider_response: Optional[Dict[str, Any]],
) -> LegacyCampaignOut:
    payload = provider_response or {}
    roas_value = payload.get("roas_bid")
    return LegacyCampaignOut(
        campaign_id=str(payload.get("campaign_id") or campaign_id),
        name=payload.get("campaign_name"),
        status=payload.get("secondary_status"),
        operation_status=payload.get("operation_status"),
        advertiser_id=str(payload.get("advertiser_id") or advertiser_id),
        store_id=str(payload.get("store_id") or store_id) if payload.get("store_id") or store_id else None,
        shopping_ads_type=payload.get("shopping_ads_type"),
        optimization_goal=payload.get("optimization_goal"),
        roas_bid=Decimal(str(roas_value)) if roas_value is not None else None,
        daily_budget_cents=_convert_budget_to_cents(payload.get("budget")),
        ext_created_time=_parse_datetime(payload.get("create_time")),
        ext_updated_time=_parse_datetime(payload.get("modify_time")),
    )


def _provider_logs_to_legacy(entries: List[Dict[str, Any]]) -> List[LegacyActionLogOut]:
    legacy_entries: List[LegacyActionLogOut] = []
    for entry in entries:
        legacy_entries.append(
            LegacyActionLogOut(
                id=int(entry.get("id") or 0),
                action=str(entry.get("action") or ""),
                reason=entry.get("reason"),
                performed_by=entry.get("performed_by"),
                result=entry.get("result"),
                error_message=entry.get("error_message"),
                before=entry.get("before"),
                after=entry.get("after"),
                created_at=_parse_datetime(entry.get("created_at")),
            )
        )
    return legacy_entries


def get_deprecated_route_context(
    workspace_id: int,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return get_provider_context(workspace_id, PROVIDER_SLUG, auth_id, db)


@router.post(
    "/{campaign_id}/actions",
    response_model=LegacyCampaignActionOut,
    dependencies=[Depends(require_tenant_admin)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/actions instead.
async def apply_gmvmax_campaign_action_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: LegacyCampaignActionIn,
    advertiser_id: str | None = Query(None),
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> LegacyCampaignActionOut:
    provider_request = _legacy_to_provider_request(payload)
    provider_response = await apply_gmvmax_campaign_action_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=provider_request,
        advertiser_id=advertiser_id,
        context=context,
    )
    campaign = _provider_response_to_legacy_campaign(
        campaign_id=campaign_id,
        advertiser_id=advertiser_id or context.advertiser_id,
        store_id=context.store_id,
        provider_response=provider_response.response,
    )
    result = provider_response.status or ""
    return LegacyCampaignActionOut(action=payload.action, result=result, campaign=campaign)


@router.get(
    "/{campaign_id}/actions",
    response_model=LegacyActionLogListResponse,
    dependencies=[Depends(require_tenant_member)],
)
# DEPRECATED: use /providers/{provider}/accounts/{auth_id}/gmvmax/{campaign_id}/actions instead.
async def list_gmvmax_action_logs_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    context: GMVMaxRouteContext = Depends(get_deprecated_route_context),
) -> LegacyActionLogListResponse:
    logs = await list_gmvmax_action_logs_provider(
        workspace_id=workspace_id,
        provider=PROVIDER_SLUG,
        auth_id=auth_id,
        campaign_id=campaign_id,
        context=context,
    )
    items = _provider_logs_to_legacy(logs.entries)
    return LegacyActionLogListResponse(campaign_id=str(campaign_id), count=len(items), items=items)
