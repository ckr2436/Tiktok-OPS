from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxActionLog, TTBGmvMaxCampaign
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import apply_campaign_action


class StubTTBClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def update_gmvmax_campaign(self, advertiser_id: str, body: dict[str, Any]) -> None:
        self.calls.append((advertiser_id, body))


def _setup_campaign(db_session) -> TTBGmvMaxCampaign:
    workspace = Workspace(id=1, name="Test", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=1,
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    campaign = TTBGmvMaxCampaign(
        id=1,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        name="GMV Campaign",
        status="PAUSED",
        daily_budget_cents=1000,
        roas_bid=Decimal("1.00"),
        currency="USD",
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


@event.listens_for(TTBGmvMaxActionLog, "before_insert")
def _assign_action_log_id(mapper, connection, target) -> None:  # pragma: no cover - helper for SQLite
    if target.id is not None:
        return
    result = connection.execute(sa.text("SELECT COALESCE(MAX(id), 0) + 1 FROM ttb_gmvmax_action_logs"))
    target.id = result.scalar_one()


def test_apply_campaign_action_updates_and_logs(db_session):
    campaign = _setup_campaign(db_session)
    client = StubTTBClient()
    audit_calls: list[dict[str, Any]] = []

    def audit_hook(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    log_start = asyncio.run(
        apply_campaign_action(
            db_session,
            client,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            action="START",
            performed_by="tester",
            audit_hook=audit_hook,
        )
    )
    assert log_start.action == "START"
    assert campaign.status == "ACTIVE"
    assert client.calls[-1][1] == {"campaign_id": campaign.campaign_id, "is_enabled": True}

    log_pause = asyncio.run(
        apply_campaign_action(
            db_session,
            client,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            action="PAUSE",
            performed_by="tester",
            audit_hook=audit_hook,
        )
    )
    assert log_pause.action == "PAUSE"
    assert campaign.status == "PAUSED"
    assert client.calls[-1][1] == {"campaign_id": campaign.campaign_id, "is_enabled": False}

    new_budget = 5000
    log_budget = asyncio.run(
        apply_campaign_action(
            db_session,
            client,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            action="SET_BUDGET",
            payload={"daily_budget_cents": new_budget},
            performed_by="tester",
            audit_hook=audit_hook,
        )
    )
    assert log_budget.action == "SET_BUDGET"
    assert campaign.daily_budget_cents == new_budget
    assert client.calls[-1][1] == {
        "campaign_id": campaign.campaign_id,
        "budget": "50.00",
    }

    new_roas = Decimal("1.50")
    log_roas = asyncio.run(
        apply_campaign_action(
            db_session,
            client,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            action="SET_ROAS",
            payload={"roas_bid": new_roas},
            performed_by="tester",
            audit_hook=audit_hook,
        )
    )
    assert log_roas.action == "SET_ROAS"
    assert Decimal(str(campaign.roas_bid)) == new_roas
    assert client.calls[-1][1] == {
        "campaign_id": campaign.campaign_id,
        "roas_bid": "1.5000",
    }

    logs = (
        db_session.query(TTBGmvMaxActionLog)
        .filter(TTBGmvMaxActionLog.campaign_id == campaign.id)
        .order_by(TTBGmvMaxActionLog.id.asc())
        .all()
    )
    assert len(logs) == 4
    assert {log.result for log in logs} == {"SUCCESS"}
    assert len(audit_calls) == 4
