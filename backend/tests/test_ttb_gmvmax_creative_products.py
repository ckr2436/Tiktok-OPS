from __future__ import annotations

from datetime import datetime

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignProduct,
    PromotionTypeEnum,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import get_item_group_ids_for_campaign


def _next_id(db_session, model) -> int:
    value = db_session.execute(
        db_session.query(model.id).order_by(model.id.desc()).limit(1).statement
    ).scalar()
    return int(value or 0) + 1


def _ensure_workspace_and_auth(db_session) -> tuple[int, int]:
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
    item_group_ids: list[str],
    operation_status: str | None = "ENABLE",
) -> GmvCampaign:
    campaign = GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
        name=name,
        status="ACTIVE",
        operation_status=operation_status,
        promotion_type=PromotionTypeEnum.PRODUCT,
        ext_created_time=datetime.now(),
        raw_json={"item_group_ids": item_group_ids},
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def test_get_item_group_ids_updates_existing_mapping(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign_a = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-old",
        store_id="store-1",
        name="Campaign Old",
        item_group_ids=["item-1"],
    )

    existing = GmvCampaignProduct(
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_pk=campaign_a.id,
        campaign_id=campaign_a.campaign_id,
        store_id="store-1",
        item_group_id="item-1",
        promotion_type=PromotionTypeEnum.PRODUCT,
        operation_status="ENABLE",
    )
    db_session.add(existing)
    db_session.flush()

    campaign_b = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-new",
        store_id="store-1",
        name="Campaign New",
        item_group_ids=["item-1"],
    )

    original_row_id = existing.id
    item_group_ids = get_item_group_ids_for_campaign(db_session, campaign=campaign_b)

    rows = db_session.query(GmvCampaignProduct).all()
    assert item_group_ids == ["item-1"]
    assert len(rows) == 1
    row = rows[0]
    assert row.id == original_row_id
    assert row.campaign_pk == campaign_b.id
    assert row.campaign_id == "cmp-new"
    assert row.operation_status == "ENABLE"
    assert row.store_id == "store-1"
    assert row.item_group_id == "item-1"
    assert row.promotion_type == PromotionTypeEnum.PRODUCT


def test_get_item_group_ids_idempotent_for_same_campaign(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-2",
        campaign_id="cmp-idem",
        store_id="store-2",
        name="Campaign Idem",
        item_group_ids=["item-2"],
    )

    first = get_item_group_ids_for_campaign(db_session, campaign=campaign)
    second = get_item_group_ids_for_campaign(db_session, campaign=campaign)

    rows = db_session.query(GmvCampaignProduct).all()
    assert first == ["item-2"]
    assert second == ["item-2"]
    assert len(rows) == 1
    row = rows[0]
    assert row.campaign_pk == campaign.id
    assert row.campaign_id == "cmp-idem"
    assert row.operation_status == "ENABLE"
    assert row.item_group_id == "item-2"


def test_get_item_group_ids_inserts_when_missing(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-3",
        campaign_id="cmp-insert",
        store_id="store-3",
        name="Campaign Insert",
        item_group_ids=["item-3"],
    )

    item_group_ids = get_item_group_ids_for_campaign(db_session, campaign=campaign)

    rows = db_session.query(GmvCampaignProduct).all()
    assert item_group_ids == ["item-3"]
    assert len(rows) == 1
    row = rows[0]
    assert row.campaign_pk == campaign.id
    assert row.campaign_id == "cmp-insert"
    assert row.operation_status == "ENABLE"
    assert row.item_group_id == "item-3"
    assert row.store_id == "store-3"
    assert row.promotion_type == PromotionTypeEnum.PRODUCT


def test_get_item_group_ids_inserts_disabled_status(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-4",
        campaign_id="cmp-disable",
        store_id="store-4",
        name="Campaign Disable",
        item_group_ids=["item-4", "item-5"],
        operation_status="DISABLE",
    )

    item_group_ids = get_item_group_ids_for_campaign(db_session, campaign=campaign)

    rows = db_session.query(GmvCampaignProduct).order_by(GmvCampaignProduct.item_group_id).all()
    assert item_group_ids == ["item-4", "item-5"]
    assert len(rows) == 2
    assert rows[0].operation_status == "DISABLE"
    assert rows[1].operation_status == "DISABLE"
    assert {row.campaign_id for row in rows} == {"cmp-disable"}
    assert {row.campaign_pk for row in rows} == {campaign.id}


def test_get_item_group_ids_updates_status_to_disable(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign_old = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-5",
        campaign_id="cmp-old-status",
        store_id="store-5",
        name="Campaign Old Status",
        item_group_ids=["item-6"],
    )

    existing = GmvCampaignProduct(
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_pk=campaign_old.id,
        campaign_id=campaign_old.campaign_id,
        store_id="store-5",
        item_group_id="item-6",
        promotion_type=PromotionTypeEnum.PRODUCT,
        operation_status="ENABLE",
    )
    db_session.add(existing)
    db_session.flush()

    campaign_new = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-5",
        campaign_id="cmp-new-status",
        store_id="store-5",
        name="Campaign New Status",
        item_group_ids=["item-6"],
        operation_status="DISABLE",
    )

    original_row_id = existing.id
    item_group_ids = get_item_group_ids_for_campaign(db_session, campaign=campaign_new)

    rows = db_session.query(GmvCampaignProduct).all()
    assert item_group_ids == ["item-6"]
    assert len(rows) == 1
    row = rows[0]
    assert row.id == original_row_id
    assert row.campaign_pk == campaign_new.id
    assert row.campaign_id == "cmp-new-status"
    assert row.operation_status == "DISABLE"
    assert row.item_group_id == "item-6"


def test_get_item_group_ids_defaults_operation_status_to_enable(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-6",
        campaign_id="cmp-none-status",
        store_id="store-6",
        name="Campaign None Status",
        item_group_ids=["item-7"],
        operation_status=None,
    )

    item_group_ids = get_item_group_ids_for_campaign(db_session, campaign=campaign)

    rows = db_session.query(GmvCampaignProduct).all()
    assert item_group_ids == ["item-7"]
    assert len(rows) == 1
    row = rows[0]
    assert row.operation_status == "ENABLE"
    assert row.campaign_id == "cmp-none-status"
