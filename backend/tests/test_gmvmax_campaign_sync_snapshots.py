import asyncio

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignSyncSnapshot,
    PromotionTypeEnum,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import sync_gmvmax_campaigns


def _ensure_account(db_session) -> tuple[int, int]:
    workspace = db_session.query(Workspace).first()
    if workspace is None:
        workspace = Workspace(id=1, name="Demo", company_code="0001")
        db_session.add(workspace)
        db_session.flush()
    provider = db_session.query(OAuthProviderApp).first()
    if provider is None:
        provider = OAuthProviderApp(
            id=1,
            provider="tiktok-business",
            name="Provider",
            client_id="client-id",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.com/callback",
        )
        db_session.add(provider)
        db_session.flush()
    account = db_session.query(OAuthAccountTTB).first()
    if account is None:
        account = OAuthAccountTTB(
            id=1,
            workspace_id=workspace.id,
            provider_app_id=provider.id,
            alias="Account",
            access_token_cipher=b"cipher",
            token_fingerprint=b"f" * 32,
        )
        db_session.add(account)
        db_session.flush()
    return workspace.id, account.id


class _DummyTTBClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads

    async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
        for payload in self.payloads:
            yield payload, {"page_info": {"page": 1, "total_page": 1}, "request_id": "req-1"}

    async def aclose(self) -> None:  # pragma: no cover - compatibility shim
        return None


def _campaign_payload(campaign_id: str, *, store_id: str, name: str = "Demo") -> dict:
    return {
        "campaign_id": campaign_id,
        "campaign_name": name,
        "store_id": store_id,
        "promotion_type": PromotionTypeEnum.PRODUCT.value,
        "status": "ENABLE",
    }


def _sync(db_session, client, workspace_id: int, auth_id: int):
    return asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
        )
    )


def test_snapshot_and_campaign_upsert(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    client = _DummyTTBClient(
        [
            _campaign_payload("cmp-1", store_id="store-1", name="One"),
            _campaign_payload("cmp-2", store_id="store-1", name="Two"),
        ]
    )

    result = _sync(db_session, client, workspace_id, auth_id)

    campaigns = (
        db_session.query(GmvCampaign)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id, advertiser_id="adv-1")
        .order_by(GmvCampaign.campaign_id)
        .all()
    )
    snapshots = db_session.query(GmvCampaignSyncSnapshot).all()

    assert result["synced"] == 2
    assert len(campaigns) == 2
    assert all(not row.is_deleted for row in campaigns)
    assert len(snapshots) == 2
    assert {snap.snapshot_type for snap in snapshots} == {"CAMPAIGN"}


def test_sync_marks_missing_campaigns_deleted(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    client = _DummyTTBClient(
        [
            _campaign_payload("cmp-a", store_id="store-1", name="Alpha"),
            _campaign_payload("cmp-b", store_id="store-1", name="Beta"),
        ]
    )

    _sync(db_session, client, workspace_id, auth_id)

    client.payloads = [_campaign_payload("cmp-a", store_id="store-1", name="Alpha")]
    result = _sync(db_session, client, workspace_id, auth_id)

    active = db_session.query(GmvCampaign).filter_by(campaign_id="cmp-a").one()
    deleted = db_session.query(GmvCampaign).filter_by(campaign_id="cmp-b").one()

    assert not active.is_deleted
    assert deleted.is_deleted and deleted.deleted_at is not None
    assert result["removed"] == 1


def test_sync_is_idempotent(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    client = _DummyTTBClient(
        [
            _campaign_payload("cmp-x", store_id="store-1", name="First"),
            _campaign_payload("cmp-y", store_id="store-1", name="Second"),
        ]
    )

    first = _sync(db_session, client, workspace_id, auth_id)
    second = _sync(db_session, client, workspace_id, auth_id)

    campaigns = db_session.query(GmvCampaign).all()
    assert len(campaigns) == 2
    assert all(not c.is_deleted for c in campaigns)
    assert first["synced"] == 2
    assert second["synced"] == 2
