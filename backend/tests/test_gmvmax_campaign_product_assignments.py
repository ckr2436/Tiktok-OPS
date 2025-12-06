from datetime import datetime

from sqlalchemy import func, select

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignProduct,
    PromotionTypeEnum,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import _sync_campaign_product_assignments


def _next_id(db_session, model) -> int:
    value = db_session.execute(select(func.coalesce(func.max(model.id), 0))).scalar_one()
    return int(value) + 1


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
    operation_status: str = "ENABLE",
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
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def test_sync_campaign_product_assignments_creates_links(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        store_id="store-1",
        name="Campaign 1",
    )

    _sync_campaign_product_assignments(
        db_session,
        campaign=campaign,
        product_ids=["prod-1", "prod-2"],
        store_id_hint=None,
        operation_status="ENABLE",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.flush()

    rows = (
        db_session.query(GmvCampaignProduct)
        .order_by(GmvCampaignProduct.item_group_id)
        .all()
    )

    assert len(rows) == 2
    assert {row.item_group_id for row in rows} == {"prod-1", "prod-2"}
    for row in rows:
        assert row.campaign_id == "cmp-1"
        assert row.campaign_pk == campaign.id
        assert row.store_id == "store-1"
        assert row.operation_status == "ENABLE"
        assert row.promotion_type == PromotionTypeEnum.PRODUCT


def test_sync_campaign_product_assignments_updates_existing_rows(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-2",
        store_id="store-2",
        name="Campaign 2",
    )

    _sync_campaign_product_assignments(
        db_session,
        campaign=campaign,
        product_ids=["prod-1", "prod-2"],
        store_id_hint=None,
        operation_status="ENABLE",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.flush()

    _sync_campaign_product_assignments(
        db_session,
        campaign=campaign,
        product_ids=["prod-1"],
        store_id_hint=None,
        operation_status="DELETE",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.flush()

    rows = (
        db_session.query(GmvCampaignProduct)
        .order_by(GmvCampaignProduct.item_group_id)
        .all()
    )

    assert len(rows) == 2
    statuses = {row.item_group_id: row.operation_status for row in rows}
    assert statuses["prod-1"] == "DELETE"
    assert statuses["prod-2"] == "ENABLE"


def test_sync_campaign_product_assignments_reassigns_campaign(db_session):
    workspace_id, auth_id = _ensure_workspace_and_auth(db_session)
    campaign_a = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-3",
        store_id="store-3",
        name="Campaign 3",
    )
    campaign_b = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="cmp-4",
        store_id="store-3",
        name="Campaign 4",
    )

    _sync_campaign_product_assignments(
        db_session,
        campaign=campaign_a,
        product_ids=["prod-3"],
        store_id_hint=None,
        operation_status="ENABLE",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.flush()

    _sync_campaign_product_assignments(
        db_session,
        campaign=campaign_b,
        product_ids=["prod-3"],
        store_id_hint=None,
        operation_status="DISABLE",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.flush()

    rows = db_session.query(GmvCampaignProduct).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.campaign_id == "cmp-4"
    assert row.campaign_pk == campaign_b.id
    assert row.operation_status == "DISABLE"
    assert row.store_id == "store-3"
    assert row.promotion_type == PromotionTypeEnum.PRODUCT
