import asyncio
from decimal import Decimal
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import event

from app.core.deps import SessionUser
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxActionLog, TTBGmvMaxCampaign
from app.data.models.workspaces import Workspace
from app.features.tenants.ttb.gmv_max import router_provider


class _DummyResponseData:
    def __init__(self, payload: dict, sessions: list | None = None) -> None:
        self._payload = dict(payload)
        self.list = sessions or []

    def model_dump(self, exclude_none: bool = True) -> dict:
        if not exclude_none:
            return dict(self._payload)
        return {k: v for k, v in self._payload.items() if v is not None}


def _response(payload: dict, *, sessions: list | None = None, request_id: str = "req-1"):
    return SimpleNamespace(request_id=request_id, data=_DummyResponseData(payload, sessions=sessions))


def _setup_entities(db_session):
    workspace = Workspace(id=101, name="Tenant", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=202,
        provider="tiktok-business",
        name="Provider",
        client_id="id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/cb",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=303,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    campaign = TTBGmvMaxCampaign(
        id=404,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        store_id="store-1",
        name="GMV",
        status="ACTIVE",
        daily_budget_cents=1000,
        roas_bid=Decimal("1.20"),
        currency="USD",
    )
    db_session.add(campaign)
    db_session.flush()
    return workspace, account, campaign


def test_apply_action_writes_action_log(monkeypatch, db_session):
    workspace, account, campaign = _setup_entities(db_session)

    actor = SessionUser(
        id=99,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )

    async def fake_status_update(request):  # noqa: ANN001
        return _response({"operation_status": request.operation_status})

    async def fake_campaign_info(request):  # noqa: ANN001
        return _response(
            {
                "campaign_id": request.campaign_id,
                "status": "PAUSED",
                "budget": 12.34,
                "roas_bid": 1.5,
            }
        )

    async def fake_campaign_update(request):  # noqa: ANN001
        return _response({"ok": True})

    async def fake_session_update(request):  # noqa: ANN001
        return _response({"sessions": []}, sessions=[])

    client = SimpleNamespace(
        campaign_status_update=fake_status_update,
        gmv_max_campaign_update=fake_campaign_update,
        gmv_max_session_update=fake_session_update,
        gmv_max_campaign_info=fake_campaign_info,
    )

    binding = router_provider.GMVMaxAccountBinding(
        account=account,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
    )
    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=binding,
        client=client,
        db=db_session,
    )

    def fake_upsert(db, **kwargs):  # noqa: ANN001
        row = db.get(TTBGmvMaxCampaign, campaign.id)
        payload = kwargs.get("payload", {})
        status = payload.get("status")
        if status:
            row.status = status
        budget = payload.get("budget")
        if budget is not None:
            row.daily_budget_cents = int(Decimal(str(budget)) * 100)
        roas = payload.get("roas_bid")
        if roas is not None:
            row.roas_bid = Decimal(str(roas))
        db.flush()
        return row

    monkeypatch.setattr(router_provider, "upsert_campaign_from_api", fake_upsert)

    asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            payload={"type": "pause"},
            advertiser_id=None,
            me=actor,
            context=context,
        )
    )

    logs = db_session.query(TTBGmvMaxActionLog).all()
    assert len(logs) == 1
    log_row = logs[0]
    assert log_row.action == "PAUSE"
    assert log_row.result == "SUCCESS"
    assert log_row.performed_by == actor.email
    assert log_row.before_json.get("status") == "ACTIVE"
    assert log_row.after_json.get("status") == "PAUSED"
@event.listens_for(TTBGmvMaxActionLog, "before_insert")
def _assign_action_log_id(mapper, connection, target) -> None:  # pragma: no cover - sqlite helper
    if target.id is not None:
        return
    result = connection.execute(
        sa.text("SELECT COALESCE(MAX(id), 0) + 1 FROM ttb_gmvmax_action_logs")
    )
    target.id = result.scalar_one()


