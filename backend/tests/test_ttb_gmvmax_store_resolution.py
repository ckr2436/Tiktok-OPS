from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxCampaign,
    TTBGmvMaxCampaignSyncSnapshot,
)
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
    store_id: str = "",
) -> TTBGmvMaxCampaign:
    campaign = TTBGmvMaxCampaign(
        id=_next_id(db_session, TTBGmvMaxCampaign),
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def _create_snapshot(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str,
    synced_at: datetime,
) -> TTBGmvMaxCampaignSyncSnapshot:
    snapshot = TTBGmvMaxCampaignSyncSnapshot(
        id=_next_id(db_session, TTBGmvMaxCampaignSyncSnapshot),
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
        synced_at=synced_at,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


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


def test_upsert_campaign_does_not_guess_store_when_multiple_links(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_store_link(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
    )
    _create_store_link(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-2",
    )

    campaign = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-ambiguous",
    )
    campaign.store_id = ""
    db_session.flush()

    updated = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-ambiguous"},
    )

    assert updated.store_id == ""


class _DummyTTBClient:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, str]] = []

    async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
        yield {
            "campaign_id": "cmp-1",
            "campaign_name": "Demo",
            "advertiser_id": advertiser_id,
            "store_id": "store-from-page",
        }, {}

    async def get_gmvmax_campaign_info(self, advertiser_id: str, campaign_id: str):
        self.info_calls.append((advertiser_id, campaign_id))
        return {
            "campaign_id": campaign_id,
            "store_id": "store-from-info",
            "status": "ENABLE",
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


def test_sync_campaigns_removes_missing_rows(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-keep",
    )
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-stale",
    )

    class _Client(_DummyTTBClient):
        async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
            yield {
                "campaign_id": "cmp-keep",
                "campaign_name": "Demo",
                "advertiser_id": advertiser_id,
                "store_id": "store-from-page",
            }, {"page_info": {"page": 1, "total_page": 1, "total_number": 1}}

    client = _Client()

    result = asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
        )
    )

    ids = {
        row.campaign_id
        for row in db_session.query(TTBGmvMaxCampaign)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id)
        .all()
    }
    assert ids == {"cmp-keep", "cmp-stale"}
    stale_row = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-stale",
        )
        .one()
    )
    assert stale_row.operation_status == "DELETE"
    assert stale_row.secondary_status == "CAMPAIGN_STATUS_DELETE"
    assert result["removed"] == 1


def test_sync_campaigns_marks_all_missing_when_response_empty(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-a",
    )
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-b",
    )

    old_synced_at = datetime(2023, 12, 31, 0, 0, 0)
    _create_snapshot(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-a",
        store_id="",
        synced_at=old_synced_at,
    )
    _create_snapshot(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-b",
        store_id="",
        synced_at=old_synced_at,
    )

    class _EmptyClient(_DummyTTBClient):
        async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
            if False:
                yield None

    result = asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            _EmptyClient(),
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
        )
    )

    campaigns = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id)
        .order_by(TTBGmvMaxCampaign.campaign_id)
        .all()
    )
    assert {c.campaign_id for c in campaigns} == {"cmp-a", "cmp-b"}
    assert all(c.is_deleted for c in campaigns)
    assert all(c.status == "DELETE" for c in campaigns)
    assert all(c.secondary_status == "CAMPAIGN_STATUS_DELETE" for c in campaigns)
    assert result["removed"] == 2

    snapshot_count = (
        db_session.query(TTBGmvMaxCampaignSyncSnapshot)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id, advertiser_id="adv-1")
        .count()
    )
    assert snapshot_count == 0


def test_sync_campaigns_soft_delete_scoped_by_store(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    old_synced_at = datetime(2024, 1, 1)

    s1_shared = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-shared",
        store_id="store-1",
    )
    s1_shared.operation_status = "ENABLE"

    s2_shared = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-shared",
        store_id="store-2",
    )
    s2_shared.operation_status = "ENABLE"

    s1_keep = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-keep",
        store_id="store-1",
    )
    s1_keep.operation_status = "ENABLE"

    _create_snapshot(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-shared",
        store_id="store-1",
        synced_at=old_synced_at,
    )
    _create_snapshot(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-shared",
        store_id="store-2",
        synced_at=old_synced_at,
    )
    _create_snapshot(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-keep",
        store_id="store-1",
        synced_at=old_synced_at,
    )

    class _ScopedClient(_DummyTTBClient):
        async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
            yield {
                "campaign_id": "cmp-keep",
                "campaign_name": "Keep",
                "advertiser_id": advertiser_id,
                "store_id": "store-1",
            }, {}

        async def get_gmvmax_campaign_info(self, advertiser_id: str, campaign_id: str):
            return {
                "campaign_id": campaign_id,
                "store_id": "store-1",
                "status": "ENABLE",
            }

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            _ScopedClient(),
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            store_ids=["store-1"],
        )
    )

    s1_shared = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-shared",
            store_id="store-1",
        )
        .one()
    )
    assert s1_shared.operation_status == "DELETE"
    assert s1_shared.secondary_status == "CAMPAIGN_STATUS_DELETE"
    assert s1_shared.status == "DELETE"

    s2_shared = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-shared",
            store_id="store-2",
        )
        .one()
    )
    assert s2_shared.operation_status == "ENABLE"
    assert s2_shared.secondary_status is None
    assert s2_shared.status is None

    s1_snapshot_count = (
        db_session.query(TTBGmvMaxCampaignSyncSnapshot)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-shared",
            store_id="store-1",
        )
        .count()
    )
    s2_snapshot_count = (
        db_session.query(TTBGmvMaxCampaignSyncSnapshot)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-shared",
            store_id="store-2",
        )
        .count()
    )

    assert s1_snapshot_count == 0
    assert s2_snapshot_count == 1


def test_sync_campaigns_does_not_remove_missing_rows_on_filtered_run(
    db_session,
):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-keep",
    )
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-stale",
    )

    class _Client(_DummyTTBClient):
        async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
            yield {
                "campaign_id": "cmp-keep",
                "campaign_name": "Demo",
                "advertiser_id": advertiser_id,
                "store_id": "store-from-page",
            }, {"page_info": {"page": 1, "total_page": 1, "total_number": 1}}

    client = _Client()

    result = asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_ids=["cmp-keep"],
        )
    )

    ids = {
        row.campaign_id
        for row in db_session.query(TTBGmvMaxCampaign)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id)
        .all()
    }
    assert ids == {"cmp-keep", "cmp-stale"}
    stale_row = (
        db_session.query(TTBGmvMaxCampaign)
        .filter_by(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-stale",
        )
        .one()
    )
    assert stale_row.operation_status == "DELETE"
    assert stale_row.secondary_status == "CAMPAIGN_STATUS_DELETE"
    assert result["removed"] == 0


def test_sync_campaigns_replaces_previous_snapshots(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        store_id="old-store",
    )

    db_session.add(
        TTBGmvMaxCampaignSyncSnapshot(
            id=_next_id(db_session, TTBGmvMaxCampaignSyncSnapshot),
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id="cmp-1",
            store_id="old-store",
            synced_at=datetime(2023, 12, 31, 0, 0, 0),
        )
    )
    db_session.flush()

    class _Client(_DummyTTBClient):
        async def iter_gmvmax_campaigns(self, advertiser_id: str, **_filters):
            yield {
                "campaign_id": "cmp-1",
                "campaign_name": "Demo",
                "advertiser_id": advertiser_id,
                "store_id": "new-store",
            }, {"page_info": {"page": 1, "total_page": 1, "total_number": 1}}

    client = _Client()

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
        )
    )

    snapshots = (
        db_session.query(TTBGmvMaxCampaignSyncSnapshot)
        .filter_by(workspace_id=workspace_id, auth_id=auth_id, advertiser_id="adv-1")
        .all()
    )
    assert len(snapshots) == 1
    assert snapshots[0].store_id == "store-from-info"


def test_upsert_campaign_prefers_detail_store_id_over_payload(db_session):
    workspace_id, auth_id = _ensure_account(db_session)

    campaign = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-99", "store_id": "store-from-payload"},
        campaign_details={"campaign_id": "cmp-99", "store_id": "store-from-detail"},
    )

    assert campaign.store_id == "store-from-detail"


def test_upsert_campaign_does_not_override_existing_store_with_hint(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    campaign = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-100",
    )
    campaign.store_id = "store-authoritative"
    db_session.flush()

    campaign = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-100"},
        store_id_hint="store-hint",
    )

    assert campaign.store_id == "store-authoritative"


def test_upsert_campaign_updates_store_when_details_available(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    campaign = _create_campaign_stub(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-101",
    )
    campaign.store_id = "store-stale"
    db_session.flush()

    updated = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-101"},
        campaign_details={"campaign_id": "cmp-101", "store_id": "store-official"},
    )

    assert updated.store_id == "store-official"


def test_upsert_campaign_uses_detail_payload_to_fill_fields(db_session):
    workspace_id, auth_id = _ensure_account(db_session)

    detail_payload = {
        "campaign_id": "cmp-77",
        "store_id": "store-77",
        "operation_status": "enable",
        "status": "enable",
        "secondary_status": "paused",
        "shopping_ads_type": "PRODUCT",
        "optimization_goal": "ROI",
        "roas_bid": "1.2345",
        "daily_budget": "123.45",
        "currency": "USD",
        "create_time": "2024-01-02T03:04:05Z",
        "update_time": "2024-01-03T04:05:06Z",
    }

    campaign = upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        payload={"campaign_id": "cmp-77"},
        campaign_details=detail_payload,
    )

    assert campaign.store_id == "store-77"
    assert campaign.status == "ENABLE"
    assert campaign.secondary_status == "PAUSED"
    assert campaign.shopping_ads_type == "PRODUCT"
    assert campaign.optimization_goal == "ROI"
    assert campaign.roas_bid == Decimal("1.2345")
    assert campaign.daily_budget_cents == 12345
    assert campaign.currency == "USD"
    assert campaign.ext_created_time is not None
    assert campaign.ext_updated_time is not None
    assert campaign.raw_json["_campaign_info"]["store_id"] == "store-77"
