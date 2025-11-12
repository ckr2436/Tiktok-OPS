from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_session, require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.services import audit as audit_svc

from .schemas import (
    GmvMaxActionLogListResponse,
    GmvMaxActionLogOut,
    GmvMaxCampaignActionIn,
    GmvMaxCampaignActionOut,
    GmvMaxCampaignActionType,
    GmvMaxCampaignOut,
)
from .service import apply_campaign_action, list_action_logs

PROVIDER_ALIAS = "tiktok_business"

router = APIRouter()


def _audit_adapter(actor_user: SessionUser | None) -> Callable[..., Any] | None:
    log_event = getattr(audit_svc, "log_event", None)
    if log_event is None:
        return None

    actor_user_id = int(actor_user.id) if actor_user else None

    def _hook(
        *,
        db: Session,
        workspace_id: int,
        actor: str,
        domain: str,
        event: str,
        target: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        result: str,
        error: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "domain": domain,
            "event": event,
            "actor": actor,
            "target": target or {},
            "before": before or {},
            "after": after or {},
            "result": result,
        }
        if error:
            details["error"] = error
        log_event(
            db,
            action=event,
            resource_type="gmv_max.campaign",
            resource_id=None,
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            details=details,
        )

    return _hook


@router.post(
    "/campaigns/{campaign_id}/actions",
    response_model=GmvMaxCampaignActionOut,
    dependencies=[Depends(require_tenant_admin)],
)
async def apply_gmvmax_campaign_action_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxCampaignActionIn,
    db: Session = Depends(get_db),
    current_user: SessionUser = Depends(require_session),
) -> GmvMaxCampaignActionOut:
    if payload.action == GmvMaxCampaignActionType.SET_BUDGET and payload.daily_budget_cents is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="daily_budget_cents is required for SET_BUDGET",
        )
    if payload.action == GmvMaxCampaignActionType.SET_ROAS and payload.roas_bid is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="roas_bid is required for SET_ROAS",
        )

    performed_by = current_user.email or str(current_user.id)
    payload_dict = {
        key: value
        for key, value in {
            "daily_budget_cents": payload.daily_budget_cents,
            "roas_bid": payload.roas_bid,
        }.items()
        if value is not None
    }

    audit_hook = _audit_adapter(current_user)

    campaign, log_entry = await apply_campaign_action(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
        action=payload.action.value,
        payload=payload_dict,
        reason=payload.reason,
        performed_by=performed_by,
        audit_hook=audit_hook,
    )

    return GmvMaxCampaignActionOut(
        action=payload.action,
        result=log_entry.result or "",
        campaign=GmvMaxCampaignOut.from_orm(campaign),
    )


@router.get(
    "/{campaign_id}/actions",
    response_model=GmvMaxActionLogListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_action_logs_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> GmvMaxActionLogListResponse:
    campaign, rows = list_action_logs(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
        auth_id=auth_id,
        campaign_id=campaign_id,
        limit=limit,
        offset=offset,
    )
    items = [
        GmvMaxActionLogOut(
            id=row.id,
            action=row.action,
            reason=row.reason,
            performed_by=row.performed_by,
            result=row.result,
            error_message=row.error_message,
            before=row.before_json,
            after=row.after_json,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return GmvMaxActionLogListResponse(
        campaign_id=campaign.campaign_id,
        count=len(items),
        items=items,
    )
