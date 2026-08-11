from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_creative_metrics import (
    GmvmaxProductCreativeMetricsDaily,
)
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBAdvertiser
from app.data.models.workspaces import Workspace
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    get_recent_creative_metrics,
)


_PROVIDER = "tiktok-business"
_CAMPAIGN_ID = "cmp-100"
_CREATIVE_ID = "cr-1"
_ADVERTISER_ID = "adv-1"
_STORE_ID = "store-1"
_ITEM_GROUP_ID = "spu-1"


def _setup_scope(db_session, *, timezone_name: str = "UTC") -> tuple[int, int]:
    workspace = Workspace(id=1, name="Tenant", company_code="0001")
    db_session.add(workspace)
    provider_app = OAuthProviderApp(
        id=1,
        provider=_PROVIDER,
        name="Provider",
        client_id="client",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add(provider_app)
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
    db_session.add(
        TTBAdvertiser(
            workspace_id=workspace.id,
            auth_id=account.id,
            advertiser_id=_ADVERTISER_ID,
            timezone=timezone_name,
            display_timezone=timezone_name,
        )
    )
    db_session.flush()
    return workspace.id, account.id


def _daily(
    *,
    workspace_id: int,
    auth_id: int,
    stat_day: date,
    item_group_id: str = _ITEM_GROUP_ID,
    creative_id: str = _CREATIVE_ID,
    advertiser_id: str = _ADVERTISER_ID,
    store_id: str = _STORE_ID,
    source_observed_at: datetime | None,
    clicks: int,
    impressions: int,
    cost_cents: int,
    revenue_cents: int,
    orders: int,
) -> GmvmaxProductCreativeMetricsDaily:
    return GmvmaxProductCreativeMetricsDaily(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=_CAMPAIGN_ID,
        item_group_id=item_group_id,
        creative_id=creative_id,
        stat_time_day=stat_day,
        creative_delivery_status="DELIVERING",
        clicks=clicks,
        impressions=impressions,
        cost_cents=cost_cents,
        gross_revenue_cents=revenue_cents,
        orders=orders,
        source_observed_at=source_observed_at,
        ingested_at=source_observed_at,
        is_final=False,
    )


def _snapshot(
    *,
    workspace_id: int,
    auth_id: int,
    stat_day: date,
    snapshot_at: datetime,
    item_group_id: str = _ITEM_GROUP_ID,
    creative_id: str = _CREATIVE_ID,
    advertiser_id: str = _ADVERTISER_ID,
    store_id: str = _STORE_ID,
    clicks: int,
    impressions: int,
    cost_cents: int,
    revenue_cents: int,
    orders: int,
) -> GmvCreativeMetrics10Min:
    return GmvCreativeMetrics10Min(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=_CAMPAIGN_ID,
        item_group_id=item_group_id,
        creative_id=creative_id,
        stat_time_day=stat_day,
        snapshot_at=snapshot_at,
        creative_status="DELIVERING",
        clicks=clicks,
        impressions=impressions,
        cost_cents=cost_cents,
        gross_revenue_cents=revenue_cents,
        orders=orders,
        source_observed_at=snapshot_at,
        ingested_at=snapshot_at,
        is_final=False,
    )


def _snapshot_manifest(
    *,
    workspace_id: int,
    auth_id: int,
    stat_day: date,
    snapshot_at: datetime,
    row_count: int,
) -> GmvCreative10MinBatchManifest:
    return GmvCreative10MinBatchManifest(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=_ADVERTISER_ID,
        store_id=_STORE_ID,
        campaign_id=_CAMPAIGN_ID,
        stat_time_day=stat_day,
        snapshot_at=snapshot_at,
        complete=True,
        row_count=row_count,
        source_observed_at=snapshot_at,
    )


def test_recent_metrics_uses_only_official_historical_daily_and_exact_item_scope(
    db_session,
):
    workspace_id, auth_id = _setup_scope(db_session)
    now = datetime(2026, 7, 17, 16, tzinfo=timezone.utc)
    observed = datetime(2026, 7, 17, 12)
    db_session.add_all(
        [
            _daily(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 15),
                source_observed_at=observed,
                clicks=10,
                impressions=100,
                cost_cents=1000,
                revenue_cents=2500,
                orders=2,
            ),
            _daily(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 16),
                source_observed_at=observed,
                clicks=20,
                impressions=200,
                cost_cents=2000,
                revenue_cents=5000,
                orders=3,
            ),
            # A row without source provenance is a legacy/derived fact and is
            # never authoritative for heating.
            _daily(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 16),
                creative_id="legacy",
                source_observed_at=None,
                clicks=999,
                impressions=999,
                cost_cents=99999,
                revenue_cents=0,
                orders=0,
            ),
            # Same creative in another product must not leak into this result.
            _daily(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 16),
                item_group_id="spu-other",
                source_observed_at=observed,
                clicks=777,
                impressions=777,
                cost_cents=77777,
                revenue_cents=0,
                orders=0,
            ),
            _snapshot(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                clicks=5,
                impressions=50,
                cost_cents=500,
                revenue_cents=1000,
                orders=1,
            ),
            _snapshot_manifest(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                row_count=1,
            ),
        ]
    )
    db_session.flush()

    result = asyncio.run(
        get_recent_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            advertiser_id=_ADVERTISER_ID,
            store_id=_STORE_ID,
            campaign_id=_CAMPAIGN_ID,
            item_group_id=_ITEM_GROUP_ID,
            window_minutes=3 * 24 * 60,
            creative_ids=[_CREATIVE_ID, "legacy"],
            now=now,
        )
    )

    assert set(result) == {_CREATIVE_ID}
    metric = result[_CREATIVE_ID]
    assert metric.item_group_id == _ITEM_GROUP_ID
    assert metric.clicks == 35
    assert metric.cost == 35
    assert metric.gross_revenue == 85
    assert metric.orders == 6
    assert metric.roi == pytest.approx(85 / 35)
    assert metric.ad_click_rate == pytest.approx(35 / 350)


def test_intraday_metrics_are_window_delta_not_daily_cumulative(db_session):
    workspace_id, auth_id = _setup_scope(db_session)
    now = datetime(2026, 7, 17, 16, tzinfo=timezone.utc)
    db_session.add_all(
        [
            _snapshot(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 14, 50),
                clicks=100,
                impressions=1000,
                cost_cents=10000,
                revenue_cents=20000,
                orders=10,
            ),
            _snapshot(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                clicks=115,
                impressions=1120,
                cost_cents=11200,
                revenue_cents=23600,
                orders=12,
            ),
            _snapshot(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                item_group_id="spu-other",
                clicks=999,
                impressions=999,
                cost_cents=99999,
                revenue_cents=0,
                orders=0,
            ),
            _snapshot_manifest(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 14, 50),
                row_count=1,
            ),
            _snapshot_manifest(
                workspace_id=workspace_id,
                auth_id=auth_id,
                stat_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                row_count=2,
            ),
        ]
    )
    db_session.flush()

    metric = asyncio.run(
        get_recent_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            advertiser_id=_ADVERTISER_ID,
            store_id=_STORE_ID,
            campaign_id=_CAMPAIGN_ID,
            item_group_id=_ITEM_GROUP_ID,
            window_minutes=60,
            creative_ids=[_CREATIVE_ID],
            now=now,
        )
    )[_CREATIVE_ID]

    assert metric.clicks == 15
    assert metric.cost == 12
    assert metric.gross_revenue == 36
    assert metric.orders == 2
    assert metric.roi == 3
    assert metric.ad_click_rate == pytest.approx(15 / 120)


def test_single_intraday_snapshot_is_not_misreported_as_window_performance(
    db_session,
):
    workspace_id, auth_id = _setup_scope(db_session)
    now = datetime(2026, 7, 17, 16, tzinfo=timezone.utc)
    db_session.add(
        _snapshot(
            workspace_id=workspace_id,
            auth_id=auth_id,
            stat_day=date(2026, 7, 17),
            snapshot_at=datetime(2026, 7, 17, 15, 50),
            clicks=100,
            impressions=1000,
            cost_cents=10000,
            revenue_cents=20000,
            orders=10,
        )
    )
    db_session.add(
        _snapshot_manifest(
            workspace_id=workspace_id,
            auth_id=auth_id,
            stat_day=date(2026, 7, 17),
            snapshot_at=datetime(2026, 7, 17, 15, 50),
            row_count=1,
        )
    )
    db_session.flush()

    result = asyncio.run(
        get_recent_creative_metrics(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            advertiser_id=_ADVERTISER_ID,
            store_id=_STORE_ID,
            campaign_id=_CAMPAIGN_ID,
            item_group_id=_ITEM_GROUP_ID,
            window_minutes=60,
            creative_ids=[_CREATIVE_ID],
            now=now,
        )
    )
    assert result == {}


def test_creative_metrics_require_complete_supported_scope(db_session):
    workspace_id, auth_id = _setup_scope(db_session)
    common = {
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "advertiser_id": _ADVERTISER_ID,
        "store_id": _STORE_ID,
        "campaign_id": _CAMPAIGN_ID,
        "item_group_id": _ITEM_GROUP_ID,
        "window_minutes": 60,
    }
    with pytest.raises(ValueError, match="unsupported"):
        asyncio.run(
            get_recent_creative_metrics(
                db_session,
                provider="other",
                **common,
            )
        )
    with pytest.raises(ValueError, match="complete"):
        asyncio.run(
            get_recent_creative_metrics(
                db_session,
                provider=_PROVIDER,
                **{**common, "item_group_id": ""},
            )
        )
