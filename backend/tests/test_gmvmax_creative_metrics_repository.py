import asyncio
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import event

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeMetric
from app.data.models.workspaces import Workspace
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    get_latest_metrics_for_creative,
    get_recent_creative_metrics,
    list_creative_metrics,
    upsert_creative_metrics,
)


_PROVIDER = "tiktok-business"
_CAMPAIGN_ID = "cmp-100"
_CREATIVE_ID = "cr-1"
_CREATIVE_METRIC_ID_SEQ = 1


@event.listens_for(TTBGmvMaxCreativeMetric, "before_insert")
def _assign_creative_metric_id(mapper, connection, target) -> None:  # pragma: no cover - sqlite helper
    global _CREATIVE_METRIC_ID_SEQ
    if target.id is not None:
        return
    target.id = _CREATIVE_METRIC_ID_SEQ
    _CREATIVE_METRIC_ID_SEQ += 1


def _setup_workspace_and_account(db_session):
    global _CREATIVE_METRIC_ID_SEQ
    _CREATIVE_METRIC_ID_SEQ = 1
    workspace = Workspace(id=1, name="Tenant", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=1,
        provider=_PROVIDER,
        name="Provider",
        client_id="client",
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

    return workspace.id, account.id


def test_upsert_creative_metrics_insert_and_update(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)
    stat_day = datetime(2024, 1, 15, 0, 0, 0)

    first_metrics = {
        "creative_name": "Alpha",
        "impressions": 1200,
        "clicks": 45,
        "cost": 123.45,
        "ad_click_rate": 0.0375,
    }
    row = asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
            stat_time_day=stat_day,
            metrics=first_metrics,
        )
    )
    assert row.impressions == 1200
    assert row.creative_name == "Alpha"
    assert row.raw_metrics == first_metrics

    updated_metrics = {
        "creative_name": "Alpha",
        "impressions": 1300,
        "clicks": 60,
        "gross_revenue": 456.78,
        "ad_conversion_rate": 0.023,
    }
    row_updated = asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
            stat_time_day=stat_day,
            metrics=updated_metrics,
        )
    )
    assert row_updated is row
    assert row_updated.impressions == 1300
    assert row_updated.clicks == 60
    assert row_updated.gross_revenue == 456.78
    assert row_updated.ad_conversion_rate == 0.023
    assert row_updated.raw_metrics == updated_metrics


def test_list_and_latest_creative_metrics_filters(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)
    base_day = datetime(2024, 1, 1)

    metrics = {
        "impressions": 100,
        "clicks": 10,
    }
    other_metrics = {"impressions": 200, "clicks": 20}

    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
            stat_time_day=base_day,
            metrics=metrics,
        )
    )
    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
            stat_time_day=base_day + timedelta(days=1),
            metrics=other_metrics,
        )
    )
    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id="cmp-200",
            creative_id="cr-2",
            stat_time_day=base_day,
            metrics=metrics,
        )
    )

    result = asyncio.run(
        list_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_ids=["cr-1"],
            date_from=base_day,
            date_to=base_day + timedelta(days=1),
        )
    )
    assert [row.stat_time_day for row in result] == [
        base_day + timedelta(days=1),
        base_day,
    ]

    latest = asyncio.run(
        get_latest_metrics_for_creative(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
        )
    )
    assert latest is not None
    assert latest.stat_time_day == base_day + timedelta(days=1)
    assert latest.clicks == 20


def test_get_recent_creative_metrics_window(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
            stat_time_day=today,
            metrics={"clicks": 15, "ad_click_rate": 0.03, "gross_revenue": 120.5},
        )
    )
    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
            stat_time_day=today - timedelta(days=2),
            metrics={"clicks": 25, "ad_click_rate": 0.05, "gross_revenue": 80.0},
        )
    )
    asyncio.run(
        upsert_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-2",
            stat_time_day=today,
            metrics={"clicks": 5, "ad_click_rate": 0.04},
        )
    )

    recent_short = asyncio.run(
        get_recent_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            window_minutes=60,
            creative_ids=["cr-1", "cr-2"],
        )
    )
    assert "cr-1" in recent_short and "cr-2" in recent_short
    assert recent_short["cr-1"].clicks == 15
    assert recent_short["cr-2"].clicks == 5

    recent_long = asyncio.run(
        get_recent_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            window_minutes=60 * 72,
            creative_ids=["cr-1"],
        )
    )
    assert recent_long["cr-1"].clicks == 40
