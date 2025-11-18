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
