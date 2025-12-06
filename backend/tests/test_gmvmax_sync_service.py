from __future__ import annotations

import contextlib
import logging
from datetime import datetime

from sqlalchemy import func, select

from app.data.models.gmv_restructured import GmvCampaign, PromotionTypeEnum
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.gmvmax.services.sync_service import GmvMaxSyncService


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
    status: str,
    is_deleted: bool,
    ext_updated_time: datetime,
) -> GmvCampaign:
    campaign = GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
        promotion_type=PromotionTypeEnum.PRODUCT,
        status=status,
        is_deleted=is_deleted,
        ext_updated_time=ext_updated_time,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def _build_strategy(workspace_id: int, auth_id: int | None = None) -> MonitoringStrategy:
    return MonitoringStrategy(
        id=1,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=None,
        store_id=None,
        level="CREATIVE_10MIN",
        interval_minutes=10,
        enabled=True,
        promotion_type=None,
        max_campaigns_per_run=None,
    )


def test_select_active_campaigns_filters_by_status(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    active = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="active-1",
        store_id="store-1",
        status="ACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 3),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="inactive-1",
        store_id="store-1",
        status="INACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 2),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="deleted-1",
        store_id="store-1",
        status="DELETED",
        is_deleted=True,
        ext_updated_time=datetime(2024, 1, 1),
    )

    strategy = _build_strategy(workspace_id, auth_id)
    service = GmvMaxSyncService()

    results = service._select_active_campaigns(db_session, strategy)

    assert [campaign.campaign_id for campaign in results] == [active.campaign_id]


def test_sync_creative_logs_workspace_when_no_active(db_session, caplog):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="inactive-2",
        store_id="store-1",
        status="INACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 5),
    )

    strategy = _build_strategy(workspace_id, auth_id)
    service = GmvMaxSyncService(session_factory=lambda: contextlib.nullcontext(db_session))

    caplog.set_level(logging.INFO, logger="gmv.services.gmvmax.sync")
    service._sync_creative_10min(strategy, datetime(2024, 1, 6))

    assert any("no active campaigns" in record.message for record in caplog.records)
    last = caplog.records[-1]
    assert getattr(last, "workspace_id", None) == workspace_id
