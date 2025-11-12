from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_session, require_tenant_admin
from app.data.db import get_db
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.services import audit as audit_svc
from app.services.ttb_gmvmax import apply_campaign_action

from ._helpers import get_advertiser_id_for_account, get_ttb_client_for_account
from .schemas import (
    GmvMaxCampaignActionIn,
    GmvMaxCampaignActionOut,
    GmvMaxCampaignActionType,
    GmvMaxCampaignOut,
)

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
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxCampaignActionIn,
    db: Session = Depends(get_db),
    current_user: SessionUser = Depends(require_session),
) -> GmvMaxCampaignActionOut:
    campaign = (
        db.query(TTBGmvMaxCampaign)
        .filter(
            TTBGmvMaxCampaign.workspace_id == workspace_id,
            TTBGmvMaxCampaign.auth_id == auth_id,
            TTBGmvMaxCampaign.campaign_id == campaign_id,
        )
        .one_or_none()
    )
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

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

    advertiser_id = get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)

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

    try:
        log_entry = await apply_campaign_action(
            db,
            ttb_client=client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            campaign=campaign,
            action=payload.action.value,
            payload=payload_dict,
            reason=payload.reason,
            performed_by=performed_by,
            audit_hook=audit_hook,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        await client.aclose()

    db.refresh(campaign)

    return GmvMaxCampaignActionOut(
        action=payload.action,
        result=log_entry.result,
        campaign=GmvMaxCampaignOut.from_orm(campaign),
    )
