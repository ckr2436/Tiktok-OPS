from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.data.models.gmv_restructured import (
    GmvCreativeMetrics10Min,
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
)
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
)
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.gmvmax.services import campaign_report_sync
from app.gmvmax.services.campaign_report_sync import (
    SyncIdentifiers,
    sync_campaign_metrics,
)
from app.gmvmax.services.creative_report_sync import (
    sync_product_creative_metrics,
)
from app.gmvmax.services.fact_freshness import settlement_metadata
from app.services.gmvmax_creative_metrics import (
    _merge_creative_entries_by_day,
    latest_creative_metrics_snapshots,
)
from app.services import ttb_gmvmax
from app.services.ttb_gmvmax import (
    fetch_gmvmax_report_by_level,
    sync_gmvmax_product_metrics_daily,
    sync_gmvmax_product_metrics_hourly,
)
from app.services.ttb_api import TTBBusinessError


class _CampaignClient:
    def __init__(self, metrics: dict[str, object]):
        self.metrics = metrics

    async def gmv_max_report_get(self, request):
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[
                    {
                        "metrics": dict(self.metrics),
                        "dimensions": {
                            "campaign_id": "campaign-1",
                            "stat_time_day": "2024-01-01",
                        },
                    }
                ],
                page_info=SimpleNamespace(
                    has_more=False,
                    has_next=False,
                    total_page=1,
                    page_size=50,
                ),
            )
        )


def test_campaign_fact_accepts_official_downward_correction_and_updates_clock(
    db_session,
    monkeypatch,
):
    source_1 = datetime(2024, 1, 2, 1, 0, 0)
    ingest_1 = datetime(2024, 1, 2, 1, 0, 1)
    source_2 = datetime(2024, 1, 2, 2, 0, 0)
    ingest_2 = datetime(2024, 1, 2, 2, 0, 1)
    clock = iter([source_1, ingest_1, source_2, ingest_2])
    monkeypatch.setattr(campaign_report_sync, "utc_now_naive", lambda: next(clock))

    identifiers = SyncIdentifiers(1, 2, "advertiser-1", "store-1")
    client = _CampaignClient({"cost": "100.00", "net_cost": "80.00"})
    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=identifiers,
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )
    first = db_session.query(GmvmaxProductCampaignMetricsDaily).one()
    created_at = first.created_at

    client.metrics = {"cost": "90.00", "net_cost": "70.00"}
    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=identifiers,
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )
    db_session.expire_all()
    corrected = db_session.query(GmvmaxProductCampaignMetricsDaily).one()

    assert corrected.cost_cents == 9000
    assert corrected.net_cost_cents == 7000
    assert corrected.created_at == created_at
    assert corrected.source_observed_at == source_2
    assert corrected.ingested_at == ingest_2
    assert corrected.updated_at == ingest_2


def test_settlement_boundary_uses_advertiser_timezone():
    report_day = date(2024, 1, 1)
    before_deadline = datetime(2024, 1, 2, 15, 59, 59)  # 10:59:59 New York
    at_deadline = datetime(2024, 1, 2, 16, 0, 0)  # 11:00:00 New York

    assert settlement_metadata(
        report_day,
        source_observed_at=before_deadline,
        advertiser_timezone="America/New_York",
    ) == (False, None)
    assert settlement_metadata(
        report_day,
        source_observed_at=at_deadline,
        advertiser_timezone="America/New_York",
    ) == (True, at_deadline)
    assert settlement_metadata(
        report_day,
        source_observed_at=at_deadline,
        advertiser_timezone=None,
    ) == (False, None)


class _PagedCreativeClient:
    def __init__(self):
        self.calls: list[tuple[int, int, bool, list[str]]] = []

    async def gmv_max_report_get(self, request, *, inject_promotion_types=True):
        self.calls.append(
            (
                int(request.page or 0),
                int(request.page_size or 0),
                bool(inject_promotion_types),
                list(request.metrics),
            )
        )
        page = int(request.page or 1)
        row = {
            "metrics": {"cost": str(page), "net_cost": str(page)},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "item_id": f"creative-{page}",
                "stat_time_day": "2024-01-01",
            },
        }
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[row],
                page_info=SimpleNamespace(
                    has_more=page == 1,
                    has_next=False,
                    total_page=2,
                    page_size=50,
                ),
            )
        )


def test_creative_report_fetches_all_official_pages_without_unsupported_net_cost():
    client = _PagedCreativeClient()
    rows = asyncio.run(
        fetch_gmvmax_report_by_level(
            client,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-1",
            campaign_ids=["campaign-1"],
            level="creative",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            item_group_ids=["item-1"],
        )
    )

    assert [call[0] for call in client.calls] == [1, 2]
    assert all(call[1] == 1000 for call in client.calls)
    assert all(call[2] is False for call in client.calls)
    assert all("net_cost" not in call[3] for call in client.calls)
    assert [row["dimensions"]["shop_content_id"] for row in rows] == [
        "creative-1",
        "creative-2",
    ]


class _WindowPagedCreativeClient:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def gmv_max_report_get(self, request, *, inject_promotion_types=True):
        page = int(request.page or 1)
        self.calls.append(
            (str(request.start_date), str(request.end_date), page)
        )
        row = {
            "metrics": {"cost": str(page)},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "item_id": f"{request.start_date}-creative-{page}",
                "stat_time_day": str(request.start_date),
            },
        }
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[row],
                page_info=SimpleNamespace(
                    has_more=page == 1,
                    has_next=False,
                    total_page=2,
                ),
            )
        )


def test_creative_report_resets_pagination_for_each_30_day_window():
    client = _WindowPagedCreativeClient()
    rows = asyncio.run(
        fetch_gmvmax_report_by_level(
            client,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-1",
            campaign_ids=["campaign-1"],
            level="creative",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
            item_group_ids=["item-1"],
        )
    )

    assert client.calls == [
        ("2024-01-01", "2024-01-30", 1),
        ("2024-01-01", "2024-01-30", 2),
        ("2024-01-31", "2024-02-01", 1),
        ("2024-01-31", "2024-02-01", 2),
    ]
    assert [row["dimensions"]["stat_time_day"] for row in rows] == [
        "2024-01-01",
        "2024-01-01",
        "2024-01-31",
        "2024-01-31",
    ]


@pytest.mark.parametrize(
    "stat_time_day",
    [None, "", "not-a-date", "2023-12-31", "2024-02-02"],
)
def test_creative_report_rejects_missing_invalid_or_out_of_window_day(
    stat_time_day,
):
    class Client:
        async def gmv_max_report_get(self, request, *, inject_promotion_types=True):
            dimensions = {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "item_id": "creative-1",
            }
            if stat_time_day is not None:
                dimensions["stat_time_day"] = stat_time_day
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[{"metrics": {"cost": "1"}, "dimensions": dimensions}],
                    page_info=SimpleNamespace(
                        has_more=False,
                        has_next=False,
                        total_page=1,
                    ),
                )
            )

    with pytest.raises(TTBBusinessError) as exc_info:
        asyncio.run(
            fetch_gmvmax_report_by_level(
                Client(),
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                campaign_ids=["campaign-1"],
                level="creative",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 2, 1),
                item_group_ids=["item-1"],
            )
        )

    assert exc_info.value.code == "GMVMAX_REPORT_DATE_INCOMPLETE"


class _EmptyWindowClient:
    def __init__(self):
        self.calls: list[tuple[str, str, int, tuple[str, ...]]] = []
        self.requests: list[object] = []
        self.inject_promotion_types: list[bool] = []

    async def gmv_max_report_get(self, request, *, inject_promotion_types=True):
        self.requests.append(request)
        self.inject_promotion_types.append(bool(inject_promotion_types))
        self.calls.append(
            (
                str(request.start_date),
                str(request.end_date),
                int(request.page or 0),
                tuple(request.dimensions or ()),
            )
        )
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[],
                page_info=SimpleNamespace(
                    has_more=False,
                    has_next=False,
                    total_page=1,
                ),
            )
        )


def test_campaign_sync_does_not_finalize_rows_missing_from_official_response(
    db_session,
):
    legacy = GmvmaxProductCampaignMetricsDaily(
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-legacy",
        stat_time_day=date(2023, 12, 1),
        cost_cents=14,
        is_final=False,
    )
    db_session.add(legacy)
    db_session.flush()

    asyncio.run(
        sync_campaign_metrics(
            db_session,
            _EmptyWindowClient(),
            identifiers=SyncIdentifiers(
                1,
                2,
                "advertiser-1",
                "store-1",
                advertiser_timezone="UTC",
            ),
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    db_session.refresh(legacy)
    assert legacy.source_observed_at is None
    assert legacy.is_final is False
    assert legacy.settled_at is None


def test_product_level_report_does_not_send_officially_rejected_filters():
    client = _EmptyWindowClient()
    rows = asyncio.run(
        fetch_gmvmax_report_by_level(
            client,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-1",
            campaign_ids=["campaign-1"],
            level="product",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            # A caller may already have this scope. It must not leak into the
            # product-level API request: TikTok returns 40002 when it does.
            item_group_ids=["item-1"],
        )
    )

    assert rows == []
    request = client.requests[0]
    assert list(request.store_ids) == ["store-1"]
    assert list(request.campaign_ids) == ["campaign-1"]
    assert list(request.dimensions) == ["item_group_id", "stat_time_day"]
    assert request.item_group_ids is None
    assert request.gmv_max_promotion_types is None
    assert request.filtering.campaign_ids == ["campaign-1"]
    assert request.filtering.store_ids is None
    assert request.filtering.item_group_ids is None
    assert client.inject_promotion_types == [False]


def test_campaign_and_creative_writers_enforce_official_daily_windows(
    db_session,
):
    identifiers = SyncIdentifiers(
        1,
        2,
        "advertiser-1",
        "store-1",
        advertiser_timezone="UTC",
    )

    campaign_client = _EmptyWindowClient()
    asyncio.run(
        sync_campaign_metrics(
            db_session,
            campaign_client,
            identifiers=identifiers,
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )
    )
    assert [
        (start, end, page)
        for start, end, page, _ in campaign_client.calls
    ] == [
        ("2024-01-01", "2024-01-30", 1),
        ("2024-01-31", "2024-02-01", 1),
    ]

    creative_client = _EmptyWindowClient()
    asyncio.run(
        sync_product_creative_metrics(
            db_session,
            creative_client,
            identifiers=identifiers,
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )
    )
    assert [
        (start, end, page)
        for start, end, page, _ in creative_client.calls
    ] == [
        ("2024-01-01", "2024-01-30", 1),
        ("2024-01-31", "2024-02-01", 1),
    ]
    assert all(
        request.gmv_max_promotion_types is None
        for request in creative_client.requests
    )
    assert all(
        list(request.item_group_ids or ()) == ["item-1"]
        for request in creative_client.requests
    )
    assert all(
        request.filtering.store_ids is None
        for request in creative_client.requests
    )


def test_product_writers_enforce_hourly_and_daily_report_windows(db_session):
    campaign = SimpleNamespace(
        campaign_id="campaign-1",
        store_id="store-1",
    )

    hourly_client = _EmptyWindowClient()
    asyncio.run(
        sync_gmvmax_product_metrics_hourly(
            db_session,
            hourly_client,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            campaign=campaign,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
        )
    )
    assert [
        (start, end, page)
        for start, end, page, _ in hourly_client.calls
    ] == [
        ("2024-01-01", "2024-01-01", 1),
        ("2024-01-02", "2024-01-02", 1),
    ]
    assert all(
        list(request.dimensions) == ["item_group_id", "stat_time_hour"]
        for request in hourly_client.requests
    )
    assert all("net_cost" not in request.metrics for request in hourly_client.requests)
    assert all(request.filtering.store_ids is None for request in hourly_client.requests)

    daily_client = _EmptyWindowClient()
    asyncio.run(
        sync_gmvmax_product_metrics_daily(
            db_session,
            daily_client,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            campaign=campaign,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )
    )
    assert [
        (start, end, page)
        for start, end, page, _ in daily_client.calls
    ] == [
        ("2024-01-01", "2024-01-30", 1),
        ("2024-01-31", "2024-02-01", 1),
    ]
    assert all(
        list(request.dimensions) == ["item_group_id", "stat_time_day"]
        for request in daily_client.requests
    )
    assert all("net_cost" not in request.metrics for request in daily_client.requests)
    assert all(request.filtering.store_ids is None for request in daily_client.requests)


class _ProductMetricClient:
    def __init__(self, metrics: dict[str, object]):
        self.metrics = metrics
        self.requested_metrics: list[list[str]] = []

    async def gmv_max_report_get(self, request, *, inject_promotion_types=True):
        self.requested_metrics.append(list(request.metrics))
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[
                    {
                        "metrics": dict(self.metrics),
                        "dimensions": {
                            "campaign_id": "campaign-1",
                            "item_group_id": "item-1",
                            "stat_time_day": "2024-01-01",
                        },
                    }
                ],
                page_info=SimpleNamespace(
                    has_more=False,
                    has_next=False,
                    total_page=1,
                    page_size=50,
                ),
            )
        )


def test_product_daily_fact_accepts_downward_official_revision_and_finality(
    db_session,
    monkeypatch,
):
    clock = iter(
        [
            datetime(2024, 1, 3, 12, 0, 0),
            datetime(2024, 1, 3, 12, 0, 1),
            datetime(2024, 1, 3, 13, 0, 0),
            datetime(2024, 1, 3, 13, 0, 1),
        ]
    )
    monkeypatch.setattr(ttb_gmvmax, "utc_now_naive", lambda: next(clock))
    campaign = SimpleNamespace(
        campaign_id="campaign-1",
        store_id="store-1",
    )
    client = _ProductMetricClient({"cost": "111.41"})
    kwargs = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "advertiser-1",
        "campaign": campaign,
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 1),
        "advertiser_timezone": "UTC",
    }
    asyncio.run(
        sync_gmvmax_product_metrics_daily(
            db_session,
            client,
            **kwargs,
        )
    )
    stale = db_session.query(GmvProductMetricsDaily).one()
    stale.net_cost_cents = 12345
    db_session.flush()
    client.metrics = {"cost": "111.40"}
    asyncio.run(
        sync_gmvmax_product_metrics_daily(
            db_session,
            client,
            **kwargs,
        )
    )

    row = db_session.query(GmvProductMetricsDaily).one()
    assert row.cost_cents == 11140
    assert row.net_cost_cents is None
    assert row.source_observed_at == datetime(2024, 1, 3, 13, 0, 0)
    assert row.ingested_at == datetime(2024, 1, 3, 13, 0, 1)
    assert row.is_final is True
    assert row.settled_at == datetime(2024, 1, 3, 13, 0, 0)
    assert all("net_cost" not in metrics for metrics in client.requested_metrics)


def test_true_ten_minute_merge_keeps_each_report_day():
    performance = [
        {
            "metrics": {"cost": "10.00"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-1",
                "stat_time_day": "2024-01-01",
            },
        },
        {
            "metrics": {"cost": "20.00"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-1",
                "stat_time_day": "2024-01-02",
            },
        },
    ]
    statuses = [
        {
            "metrics": {"creative_delivery_status": "DELIVERING"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-1",
                "stat_time_day": "2024-01-02",
            },
        }
    ]

    merged = _merge_creative_entries_by_day(
        performance,
        statuses,
        campaign_id="campaign-1",
        item_group_ids=["item-1"],
        status_day=date(2024, 1, 2),
    )
    by_day = {row["dimensions"]["stat_time_day"]: row for row in merged}

    assert set(by_day) == {date(2024, 1, 1), date(2024, 1, 2)}
    assert by_day[date(2024, 1, 1)]["metrics"] == {"cost": "10.00"}
    assert by_day[date(2024, 1, 2)]["metrics"] == {
        "cost": "20.00",
        "creative_delivery_status": "DELIVERING",
    }


def test_true_ten_minute_merge_never_assigns_request_day_to_undated_performance():
    performance = [
        {
            "metrics": {"cost": "10.00"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-undated",
            },
        }
    ]
    statuses = [
        {
            "metrics": {"creative_delivery_status": "DELIVERING"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-status",
            },
        }
    ]

    merged = _merge_creative_entries_by_day(
        performance,
        statuses,
        campaign_id="campaign-1",
        item_group_ids=["item-1"],
        status_day=date(2024, 1, 2),
    )

    assert merged == [
        {
            "metrics": {"creative_delivery_status": "DELIVERING"},
            "dimensions": {
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-status",
                "stat_time_day": date(2024, 1, 2),
            },
        }
    ]


def test_latest_ten_minute_snapshot_is_fully_tenant_and_product_scoped(db_session):
    snapshot_at = datetime(2024, 1, 1, 12, 0)
    rows = [
        GmvCreativeMetrics10Min(
            workspace_id=1,
            auth_id=10,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="shared-campaign",
            item_group_id="item-1",
            creative_id="shared-creative",
            stat_time_day=date(2024, 1, 1),
            snapshot_at=snapshot_at,
            cost_cents=100,
        ),
        GmvCreativeMetrics10Min(
            workspace_id=2,
            auth_id=20,
            advertiser_id="advertiser-2",
            store_id="store-2",
            campaign_id="shared-campaign",
            item_group_id="item-1",
            creative_id="shared-creative",
            stat_time_day=date(2024, 1, 1),
            snapshot_at=snapshot_at,
            cost_cents=999,
        ),
        GmvCreativeMetrics10Min(
            workspace_id=1,
            auth_id=10,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="shared-campaign",
            item_group_id="item-2",
            creative_id="shared-creative",
            stat_time_day=date(2024, 1, 1),
            snapshot_at=snapshot_at,
            cost_cents=200,
        ),
    ]
    db_session.add_all(
        [
            *rows,
            GmvCreative10MinBatchManifest(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="shared-campaign",
                stat_time_day=date(2024, 1, 1),
                snapshot_at=snapshot_at,
                complete=True,
                row_count=2,
                source_observed_at=snapshot_at,
            ),
            GmvCreative10MinBatchManifest(
                workspace_id=2,
                auth_id=20,
                advertiser_id="advertiser-2",
                store_id="store-2",
                campaign_id="shared-campaign",
                stat_time_day=date(2024, 1, 1),
                snapshot_at=snapshot_at,
                complete=True,
                row_count=1,
                source_observed_at=snapshot_at,
            ),
        ]
    )
    db_session.flush()

    result = latest_creative_metrics_snapshots(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=10,
        advertiser_id="advertiser-1",
        campaign_ids=["shared-campaign"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
        store_ids=["store-1"],
        product_ids=["item-1"],
    )

    assert len(result) == 1
    assert result[0].cost_cents == 100
    assert result[0].item_group_id == "item-1"


def test_true_ten_minute_unique_key_includes_full_scope_and_item_group():
    constraint = next(
        item
        for item in GmvCreativeMetrics10Min.__table__.constraints
        if item.name == "uk_creative_10min_scope_item"
    )
    assert [column.name for column in constraint.columns] == [
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "campaign_id",
        "item_group_id",
        "creative_id",
        "stat_time_day",
        "snapshot_at",
    ]
    # MySQL UNIQUE permits repeated NULL values. Every identity column must
    # therefore be NOT NULL after migration; legacy incomplete rows are
    # quarantined rather than left in this active fact.
    assert all(column.nullable is False for column in constraint.columns)
    assert all(
        item.name != "idx_creative_10min_scope_item"
        for item in GmvCreativeMetrics10Min.__table__.indexes
    )


def test_0095_quarantines_legacy_null_identity_before_not_null_upgrade(
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0095_gmv_data_accuracy"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "gmv_creative_metrics_10min",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.BigInteger, nullable=True),
        sa.Column("auth_id", sa.BigInteger, nullable=True),
        sa.Column("advertiser_id", sa.String(64), nullable=True),
        sa.Column("store_id", sa.String(64), nullable=True),
        sa.Column("campaign_id", sa.String(64), nullable=False),
        sa.Column("item_group_id", sa.String(64), nullable=True),
        sa.Column("creative_id", sa.String(64), nullable=False),
        sa.Column("stat_time_day", sa.Date, nullable=False),
        sa.Column("snapshot_at", sa.DateTime, nullable=False),
        sa.Column("cost_cents", sa.BigInteger, nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            {
                "id": 1,
                "campaign_id": "legacy-campaign",
                "creative_id": "legacy-creative",
                "stat_time_day": date(2024, 1, 1),
                "snapshot_at": datetime(2024, 1, 1, 12, 0),
            },
        )
        connection.execute(
            table.insert(),
            {
                "id": 2,
                "workspace_id": 1,
                "auth_id": 2,
                "advertiser_id": "advertiser-1",
                "store_id": "store-1",
                "campaign_id": "campaign-1",
                "item_group_id": "item-1",
                "creative_id": "creative-1",
                "stat_time_day": date(2024, 1, 1),
                "snapshot_at": datetime(2024, 1, 1, 12, 0),
            },
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        # MySQL applies DDL non-transactionally, so the repair must also be
        # safe when Alembic is re-run after an interrupted attempt.
        migration.upgrade()

        active_ids = connection.execute(
            sa.text(
                "select id from gmv_creative_metrics_10min order by id"
            )
        ).scalars().all()
        quarantined = connection.execute(
            sa.text(
                "select id, quarantine_reason "
                "from gmv_creative_metrics_10min_quarantine"
            )
        ).all()

    assert active_ids == [2]
    assert quarantined == [(1, "INCOMPLETE_SCOPE")]
    inspector = sa.inspect(engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("gmv_creative_metrics_10min")
    }
    assert all(
        columns[name]["nullable"] is False
        for name in migration.TEN_MINUTE_REQUIRED_IDENTITY_COLUMNS
    )
    unique = next(
        item
        for item in inspector.get_unique_constraints(
            "gmv_creative_metrics_10min"
        )
        if item["name"] == migration.TEN_MINUTE_UNIQUE_NAME
    )
    assert unique["column_names"] == migration.TEN_MINUTE_UNIQUE_COLUMNS


def test_card_consumed_product_and_overview_facts_expose_finality():
    for model in (
        GmvProductMetricsDaily,
        GmvProductMetricsHourly,
        GmvOverviewMetricsDaily,
        GmvOverviewMetricsHourly,
    ):
        columns = model.__table__.c
        assert columns.source_observed_at.nullable is True
        assert columns.ingested_at.nullable is True
        assert columns.is_final.nullable is False
        assert columns.settled_at.nullable is True
