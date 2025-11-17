from datetime import date

from sqlalchemy import func, select

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxMetricsDaily
from app.data.models.workspaces import Workspace
from app.data.repositories.tiktok_business.gmvmax_metrics import (
    GMVMaxMetricDTO,
    query_gmvmax_metrics,
)


def _next_id(db_session, model) -> int:
    value = db_session.execute(select(func.coalesce(func.max(model.id), 0))).scalar_one()
    return int(value) + 1


def _setup_campaign(
    db_session, *, store_id: str = "store-1", campaign_key: str = "cmp-1"
) -> TTBGmvMaxCampaign:
    workspace = db_session.query(Workspace).first()
    if workspace is None:
        workspace = Workspace(id=_next_id(db_session, Workspace), name="Demo", company_code="dmo")
        db_session.add(workspace)
        db_session.flush()

    provider_app = db_session.query(OAuthProviderApp).first()
    if provider_app is None:
        provider_app = OAuthProviderApp(
            id=_next_id(db_session, OAuthProviderApp),
            provider="tiktok-business",
            name="Provider",
            client_id="client-id",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.com/callback",
        )
        db_session.add(provider_app)
        db_session.flush()

    account = db_session.query(OAuthAccountTTB).first()
    if account is None:
        account = OAuthAccountTTB(
            id=_next_id(db_session, OAuthAccountTTB),
            workspace_id=workspace.id,
            provider_app_id=provider_app.id,
            alias="Account",
            access_token_cipher=b"cipher",
            token_fingerprint=b"f" * 32,
        )
        db_session.add(account)
        db_session.flush()

    campaign = TTBGmvMaxCampaign(
        id=_next_id(db_session, TTBGmvMaxCampaign),
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        campaign_id=campaign_key,
        store_id=store_id,
        name="Primary",
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def _insert_metric(db_session, campaign: TTBGmvMaxCampaign, *, stat_date: date, cost_cents: int, orders: int) -> None:
    metric = TTBGmvMaxMetricsDaily(
        id=_next_id(db_session, TTBGmvMaxMetricsDaily),
        campaign_id=campaign.id,
        date=stat_date,
        cost_cents=cost_cents,
        net_cost_cents=cost_cents,
        orders=orders,
        gross_revenue_cents=cost_cents * 2,
    )
    db_session.add(metric)
    db_session.flush()


def test_query_metrics_returns_rows(db_session):
    campaign = _setup_campaign(db_session)
    _insert_metric(db_session, campaign, stat_date=date(2024, 1, 1), cost_cents=1000, orders=2)
    _insert_metric(db_session, campaign, stat_date=date(2024, 1, 2), cost_cents=2000, orders=4)

    items, total = query_gmvmax_metrics(
        db_session,
        workspace_id=campaign.workspace_id,
        provider="tiktok-business",
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        limit=10,
        offset=0,
    )

    assert total == 2
    assert isinstance(items[0], GMVMaxMetricDTO)
    assert items[0].stat_time_day == date(2024, 1, 1)
    assert items[0].cost == 10.0
    assert items[1].cost_per_order == 5.0


def test_query_metrics_filters_by_store(db_session):
    campaign = _setup_campaign(db_session, store_id="store-1", campaign_key="cmp-1")
    _setup_campaign(db_session, store_id="store-2", campaign_key="cmp-2")

    _insert_metric(db_session, campaign, stat_date=date(2024, 1, 1), cost_cents=1000, orders=2)
    other_campaign = db_session.query(TTBGmvMaxCampaign).filter_by(campaign_id="cmp-2").first()
    assert other_campaign is not None
    _insert_metric(
        db_session,
        other_campaign,
        stat_date=date(2024, 1, 1),
        cost_cents=500,
        orders=1,
    )

    items, total = query_gmvmax_metrics(
        db_session,
        workspace_id=campaign.workspace_id,
        provider="tiktok-business",
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        advertiser_id=campaign.advertiser_id,
        store_id="store-1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
        limit=10,
        offset=0,
    )

    assert total == 1
    assert len(items) == 1
    assert items[0].store_id == "store-1"
