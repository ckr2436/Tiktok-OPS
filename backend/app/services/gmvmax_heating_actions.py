from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_heating import (
    update_heating_action_result,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignActionApplyBody,
    GMVMaxCampaignActionApplyRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_gmvmax import log_campaign_action


def _snapshot_campaign_state(campaign: TTBGmvMaxCampaign | None) -> dict[str, Any]:
    if campaign is None:
        return {}
    return {
        "status": getattr(campaign, "status", None),
        "daily_budget_cents": getattr(campaign, "daily_budget_cents", None),
        "roas_bid": getattr(campaign, "roas_bid", None),
    }


def _extract_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
        return str(detail)
    return str(detail)


async def apply_boost_creative_action(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    campaign: TTBGmvMaxCampaign,
    heating: TTBGmvMaxCreativeHeating,
    mode: str | None = None,
    target_daily_budget: float | None = None,
    budget_delta: float | None = None,
    currency: str | None = None,
    max_duration_minutes: int | None = None,
    note: str | None = None,
    performed_by: str = "system_auto_heating",
    before_state: Mapping[str, Any] | None = None,
    after_state: Mapping[str, Any] | None = None,
):
    action_body: dict[str, Any] = {
        "campaign_id": str(campaign.campaign_id),
        "action_type": "BOOST_CREATIVE",
        "creative_id": str(heating.creative_id),
    }
    if mode:
        action_body["mode"] = mode
    if target_daily_budget is not None:
        action_body["target_daily_budget"] = target_daily_budget
    if budget_delta is not None:
        action_body["budget_delta"] = budget_delta
    if currency:
        action_body["currency"] = currency
    if max_duration_minutes is not None:
        action_body["max_duration_minutes"] = max_duration_minutes
    if note:
        action_body["note"] = note

    api_request = GMVMaxCampaignActionApplyRequest(
        advertiser_id=str(campaign.advertiser_id),
        body=GMVMaxCampaignActionApplyBody(**action_body),
    )

    action_time = datetime.now(timezone.utc)
    before_snapshot = dict(before_state or _snapshot_campaign_state(campaign))
    try:
        response = await client.gmv_max_campaign_action_apply(api_request)
    except Exception as exc:  # noqa: BLE001
        error_detail = getattr(exc, "detail", None)
        response_payload = error_detail if isinstance(error_detail, dict) else None
        error_message = _extract_error_message(error_detail) if error_detail else str(exc)
        await update_heating_action_result(
            db,
            heating_id=heating.id,
            status="FAILED",
            action_type="APPLY_BOOST",
            action_time=action_time,
            request_payload=action_body,
            response_payload=response_payload,
            error_message=error_message,
        )
        db.flush()
        try:
            log_campaign_action(
                db,
                workspace_id=heating.workspace_id,
                auth_id=heating.auth_id,
                campaign=campaign,
                action="BOOST_CREATIVE",
                reason=note,
                before=before_snapshot,
                after=before_snapshot,
                performed_by=performed_by,
                result="FAILED",
                error_message=error_message,
            )
        finally:
            db.flush()
        raise

    payload = response.data.model_dump(exclude_none=True)
    updated_row = await update_heating_action_result(
        db,
        heating_id=heating.id,
        status="APPLIED",
        action_type="APPLY_BOOST",
        action_time=action_time,
        request_payload=action_body,
        response_payload=payload,
        error_message=None,
    )
    db.flush()

    after_snapshot = dict(after_state or _snapshot_campaign_state(campaign))
    try:
        log_campaign_action(
            db,
            workspace_id=heating.workspace_id,
            auth_id=heating.auth_id,
            campaign=campaign,
            action="BOOST_CREATIVE",
            reason=note,
            before=before_snapshot,
            after=after_snapshot,
            performed_by=performed_by,
            result="SUCCESS",
        )
    finally:
        db.flush()

    return updated_row, response
