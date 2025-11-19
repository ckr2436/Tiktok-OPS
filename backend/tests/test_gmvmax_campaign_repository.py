from datetime import datetime

from sqlalchemy import func, select

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.data.models.workspaces import Workspace
from app.data.repositories.tiktok_business.gmvmax import list_gmvmax_campaigns


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


def _create_campaign(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str,
    name: str,
    operation_status: str = "ENABLE",
    secondary_status: str | None = None,
    created_at: datetime | None = None,
) -> TTBGmvMaxCampaign:
    campaign = TTBGmvMaxCampaign(
        id=_next_id(db_session, TTBGmvMaxCampaign),
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
        name=name,
        operation_status=operation_status,
        secondary_status=secondary_status,
        ext_created_time=created_at,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def test_list_campaigns_filters_by_store(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-a",
        store_id="store-1",
        name="Primary",
        created_at=datetime(2024, 1, 2),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-b",
        store_id="store-2",
        name="Secondary",
        created_at=datetime(2024, 1, 3),
    )

    items, total = list_gmvmax_campaigns(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )

    assert total == 1
    assert [item.campaign_id for item in items] == ["cmp-a"]


def test_list_campaigns_excludes_blocked_secondary_status(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-active",
        store_id="store-1",
        name="Active",
        secondary_status="CAMPAIGN_STATUS_LIVE_GMV_MAX_AUTHORIZATION_CANCEL",
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-deleted",
        store_id="store-1",
        name="Deleted",
        secondary_status="CAMPAIGN_STATUS_DELETE",
    )

    items, total = list_gmvmax_campaigns(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )

    assert total == 1
    assert items[0].campaign_id == "cmp-active"


def test_list_campaigns_filters_by_advertiser(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-a",
        store_id="store-1",
        name="Scoped",
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-2",
        campaign_id="cmp-b",
        store_id="store-1",
        name="Other",
    )

    items, total = list_gmvmax_campaigns(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )

    assert total == 1
    assert items[0].advertiser_id == "adv-1"


def test_list_campaigns_respects_operation_status(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-enabled",
        store_id="store-1",
        name="Enabled",
        operation_status="ENABLE",
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-disabled",
        store_id="store-1",
        name="Disabled",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
    )

    items, total = list_gmvmax_campaigns(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )

    assert total == 2
    assert {item.campaign_id for item in items} == {"cmp-enabled", "cmp-disabled"}


def test_list_campaigns_does_not_require_matching_auth(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    provider = db_session.query(OAuthProviderApp).first()
    other_auth = OAuthAccountTTB(
        id=_next_id(db_session, OAuthAccountTTB),
        workspace_id=workspace_id,
        provider_app_id=provider.id,
        alias="Extra",
        access_token_cipher=b"cipher-2",
        token_fingerprint=b"g" * 32,
    )
    db_session.add(other_auth)
    db_session.flush()

    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=other_auth.id,
        advertiser_id="adv-1",
        campaign_id="cmp-shared",
        store_id="store-1",
        name="Shared",
    )

    items, total = list_gmvmax_campaigns(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )

    assert total == 1
    assert [item.campaign_id for item in items] == ["cmp-shared"]
