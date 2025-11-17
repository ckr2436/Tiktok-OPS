from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import sync_gmvmax_campaigns, upsert_campaign_from_api


def _next_id(db_session, model) -> int:
    value = db_session.execute(select(func.coalesce(func.max(model.id), 0))).scalar_one()
    return int(value) + 1


def _ensure_account(db_session) -> tuple[int, int]:
    workspace = db_session.query(Workspace).first()
    if workspace is None:
        workspace = Workspace(id=_next_id(db_session, Workspace), name="Demo", company_code="0001")
        db_session.add(workspace)
        db_session.flush()
    provider = db_session.query(OAuthProviderApp).first()
    if provider is None:
        provider = OAuthProviderApp(
            id=_next_id(db_session, OAuthProviderApp),
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
            id=_next_id(db_session, OAuthAccountTTB),
            workspace_id=workspace.id,
            provider_app_id=provider.id,
            alias="Account",
            access_token_cipher=b"cipher",
            token_fingerprint=b"f" * 32,
        )
        db_session.add(account)
        db_session.flush()
    return workspace.id, account.id


def _create_store_link(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str | None = None,
    bc_id_hint: str | None = None,
    relation_type: str = "BOUND",
) -> TTBAdvertiserStoreLink:
    link = TTBAdvertiserStoreLink(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        relation_type=relation_type,
        store_authorized_bc_id=store_authorized_bc_id,
        bc_id_hint=bc_id_hint,
    )
    db_session.add(link)
    db_session.flush()
    return link


def _create_campaign_stub(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
) -> TTBGmvMaxCampaign:
    campaign = TTBGmvMaxCampaign(
        id=_next_id(db_session, TTBGmvMaxCampaign),
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id="",
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def test_upsert_campaign_uses_store_links_when_payload_missing_store(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_store_link(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
    )

    campaign = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-1"},
    )

    assert campaign.store_id == "store-1"


def test_upsert_campaign_prefers_store_link_matching_bc(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_store_link(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
    )
    _create_store_link(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-2",
        store_authorized_bc_id="bc-2",
    )
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-2",
    )

    payload = {"campaign_id": "cmp-2", "authorized_bc_id": "bc-2"}
    campaign = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload=payload,
    )

    assert campaign.store_id == "store-2"


class _DummyTTBClient:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, str]] = []

    async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
        yield {
            "campaign_id": "cmp-1",
            "campaign_name": "Demo",
            "advertiser_id": advertiser_id,
        }, {}

    async def get_gmvmax_campaign_info(self, advertiser_id: str, campaign_id: str):
        self.info_calls.append((advertiser_id, campaign_id))
        return {
            "campaign_id": campaign_id,
            "store_id": "store-from-info",
        }


def test_sync_campaigns_fetches_store_id_from_detail_api(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    client = _DummyTTBClient()
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
    )

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
        )
    )

    campaign = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id, campaign_id="cmp-1")
        .one()
    )
    assert campaign.store_id == "store-from-info"
    assert client.info_calls == [("adv-1", "cmp-1")]
