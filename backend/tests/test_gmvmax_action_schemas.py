from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.tenants.ttb.gmv_max.schemas import CampaignActionRequest


def test_campaign_action_request_accepts_aliases() -> None:
    request = CampaignActionRequest(type="resume", payload={})
    assert request.type == "enable"

    request = CampaignActionRequest(action_type="disable", payload={})
    assert request.type == "pause"

    request = CampaignActionRequest(type="remove", payload={})
    assert request.type == "delete"


def test_campaign_action_request_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        CampaignActionRequest(type="launch", payload={})


def test_campaign_action_response_allows_durable_queued_pause() -> None:
    from app.features.tenants.ttb.gmv_max.schemas import CampaignActionResponse

    response = CampaignActionResponse(type="pause", status="queued")
    assert response.status == "queued"


def test_campaign_action_request_accepts_atomic_strategy_shutdown() -> None:
    request = CampaignActionRequest(
        type="pause",
        disable_strategy=True,
    )

    assert request.type == "pause"
    assert request.disable_strategy is True
