import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.data.db import SessionLocal
from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
)
from app.data.models.gmvmax_campaign_snapshots import (
    GmvmaxProductCampaignSnapshotBatch,
    GmvmaxProductCampaignSnapshotRow,
)
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.gmvmax.services.campaign_report_sync import (
    SyncIdentifiers,
    sync_campaign_metrics,
    sync_campaign_snapshot,
)
from app.gmvmax.services.campaign_cleanup import cleanup_campaign_tables
from app.gmvmax.services.gmvmax_value_parser import (
    money_to_cents,
    parse_stat_time_hour,
    to_decimal,
)
from app.providers.tiktok_business.gmvmax_client import GMVMaxReportData, PageInfo


class DummyReportClient:
    def __init__(self, rows):
        self.rows = rows

    async def gmv_max_report_get(self, request):  # noqa: D401
        return SimpleNamespace(
            data=GMVMaxReportData(
                list=self.rows,
                page_info=PageInfo(
                    page=int(request.page or 1),
                    page_size=50,
                    total_page=1,
                    has_more=False,
                ),
            )
        )


def test_fact_upsert_idempotent(db_session):
    identifiers = SyncIdentifiers(1, 2, "adv", "store")
    client = DummyReportClient(
        [
            {
                "metrics": {"cost": "57.75", "orders": 1},
                "dimensions": {"campaign_id": "c1", "stat_time_day": "2024-01-01"},
            }
        ]
    )

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
    client.rows[0]["metrics"]["cost"] = "100.00"
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

    rows = db_session.query(GmvmaxProductCampaignMetricsDaily).all()
    assert len(rows) == 1
    assert rows[0].cost_cents == 10000


def test_fact_created_at_preserved(db_session):
    identifiers = SyncIdentifiers(1, 2, "adv", "store")
    client = DummyReportClient(
        [
            {
                "metrics": {"cost": "10.00"},
                "dimensions": {"campaign_id": "c1", "stat_time_day": "2024-01-01"},
            }
        ]
    )

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
    first_row = db_session.query(GmvmaxProductCampaignMetricsDaily).one()
    first_created_at = first_row.created_at
    client.rows[0]["metrics"]["cost"] = "12.00"
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
    updated_row = db_session.query(GmvmaxProductCampaignMetricsDaily).one()
    assert updated_row.cost_cents == 1200
    assert updated_row.created_at == first_created_at


def test_snapshot_replace_idempotent(db_session):
    identifiers = SyncIdentifiers(1, 2, "adv", "store")
    client = DummyReportClient(
        [
            {
                "metrics": {"cost": "10.00"},
                "dimensions": {"campaign_id": "c1"},
            }
        ]
    )

    asyncio.run(
        sync_campaign_snapshot(
            db_session,
            client,
            identifiers=identifiers,
            promotion_type="PRODUCT",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
        )
    )
    client.rows[0]["metrics"]["cost"] = "12.00"
    asyncio.run(
        sync_campaign_snapshot(
            db_session,
            client,
            identifiers=identifiers,
            promotion_type="PRODUCT",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
        )
    )

    batch_count = db_session.query(GmvmaxProductCampaignSnapshotBatch).count()
    row = db_session.query(GmvmaxProductCampaignSnapshotRow).one()
    assert batch_count == 1
    assert row.cost_cents == 1200


def test_snapshot_concurrent_workers():
    identifiers = SyncIdentifiers(1, 2, "adv", "store")
    rows = [
        {
            "metrics": {"cost": "5.00"},
            "dimensions": {"campaign_id": "c1"},
        }
    ]

    def _run_once():
        session = SessionLocal()
        client = DummyReportClient(rows)
        try:
            asyncio.run(
                sync_campaign_snapshot(
                    session,
                    client,
                    identifiers=identifiers,
                    promotion_type="PRODUCT",
                    start_date=date(2024, 1, 1),
                    end_date=date(2024, 1, 2),
                )
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: _run_once(), range(5)))

    session = SessionLocal()
    try:
        batch_count = session.query(GmvmaxProductCampaignSnapshotBatch).count()
        row_count = session.query(GmvmaxProductCampaignSnapshotRow).count()
        assert batch_count == 1
        assert row_count == 1
    finally:
        session.close()


def test_metrics_concurrent_workers():
    identifiers = SyncIdentifiers(1, 2, "adv", "store")

    def _run_once(cost_value: str):
        session = SessionLocal()
        client = DummyReportClient(
            [
                {
                    "metrics": {"cost": cost_value},
                    "dimensions": {"campaign_id": "c1", "stat_time_day": "2024-01-01"},
                }
            ]
        )
        try:
            asyncio.run(
                sync_campaign_metrics(
                    session,
                    client,
                    identifiers=identifiers,
                    promotion_type="PRODUCT",
                    granularity="DAILY",
                    start_date=date(2024, 1, 1),
                    end_date=date(2024, 1, 1),
                )
            )
            session.commit()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        pool.map(lambda value: _run_once(value), ["5.00"] * 5)

    session = SessionLocal()
    try:
        rows = session.query(GmvmaxProductCampaignMetricsDaily).all()
        assert len(rows) == 1
        assert rows[0].cost_cents == 500
    finally:
        session.close()


def test_money_to_cents_parsing():
    assert money_to_cents("57.75") == 5775
    assert money_to_cents(12.34) == 1234
    assert to_decimal(1.2345) is not None
    assert money_to_cents(float("nan")) is None
    assert money_to_cents(float("inf")) is None
    assert money_to_cents(True) is None
    assert to_decimal(True) is None


def test_parse_stat_time_hour_timezone_normalization():
    assert parse_stat_time_hour("2024-01-01T10:00:00+08:00") == datetime(2024, 1, 1, 2, 0, 0)
    assert parse_stat_time_hour("2024-01-01T10:00:00Z") == datetime(2024, 1, 1, 10, 0, 0)
    assert parse_stat_time_hour("2024-01-01T10:30:00+08:00") == datetime(2024, 1, 1, 2, 0, 0)
    assert parse_stat_time_hour("2024-01-01T10:45:00Z") == datetime(2024, 1, 1, 10, 0, 0)


def test_cleanup_campaign_tables(db_session):
    identifiers = SyncIdentifiers(1, 2, "adv", "store")
    old_hour = datetime.utcnow() - timedelta(days=200)
    recent_hour = datetime.utcnow() - timedelta(days=10)

    db_session.add(
        GmvmaxProductCampaignMetricsHourly(
            id=1,
            workspace_id=identifiers.workspace_id,
            auth_id=identifiers.auth_id,
            advertiser_id=identifiers.advertiser_id,
            store_id=identifiers.store_id,
            campaign_id="c1",
            stat_time_hour=old_hour,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    db_session.add(
        GmvmaxProductCampaignMetricsHourly(
            id=2,
            workspace_id=identifiers.workspace_id,
            auth_id=identifiers.auth_id,
            advertiser_id=identifiers.advertiser_id,
            store_id=identifiers.store_id,
            campaign_id="c1",
            stat_time_hour=recent_hour,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    old_batch_time = datetime.utcnow() - timedelta(days=200)
    recent_batch_time = datetime.utcnow() - timedelta(days=5)
    old_batch = GmvmaxProductCampaignSnapshotBatch(
        id=1,
        workspace_id=identifiers.workspace_id,
        auth_id=identifiers.auth_id,
        advertiser_id=identifiers.advertiser_id,
        store_id=identifiers.store_id,
        start_date=date(2023, 1, 1),
        end_date=date(2023, 1, 2),
        snapshot_type="MANUAL",
        snapshot_at=old_batch_time,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    recent_batch = GmvmaxProductCampaignSnapshotBatch(
        id=2,
        workspace_id=identifiers.workspace_id,
        auth_id=identifiers.auth_id,
        advertiser_id=identifiers.advertiser_id,
        store_id=identifiers.store_id,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        snapshot_type="MANUAL",
        snapshot_at=recent_batch_time,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    old_creative_metric = GmvCreativeMetrics10Min(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        store_id="store",
        campaign_id="c1",
        item_group_id="product-1",
        creative_id="creative-old",
        stat_time_day=old_batch_time.date(),
        snapshot_at=old_batch_time,
    )
    recent_creative_metric = GmvCreativeMetrics10Min(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        store_id="store",
        campaign_id="c1",
        item_group_id="product-1",
        creative_id="creative-recent",
        stat_time_day=recent_batch_time.date(),
        snapshot_at=recent_batch_time,
    )
    old_creative_manifest = GmvCreative10MinBatchManifest(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        store_id="store",
        campaign_id="c1",
        stat_time_day=old_batch_time.date(),
        snapshot_at=old_batch_time,
        complete=True,
        row_count=1,
        source_observed_at=old_batch_time,
    )
    recent_creative_manifest = GmvCreative10MinBatchManifest(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        store_id="store",
        campaign_id="c1",
        stat_time_day=recent_batch_time.date(),
        snapshot_at=recent_batch_time,
        complete=True,
        row_count=1,
        source_observed_at=recent_batch_time,
    )
    db_session.add_all(
        [
            old_batch,
            recent_batch,
            old_creative_metric,
            recent_creative_metric,
            old_creative_manifest,
            recent_creative_manifest,
        ]
    )
    db_session.commit()

    summary = cleanup_campaign_tables(
        db_session,
        now=datetime.utcnow(),
        hourly_retention_days=90,
        daily_retention_days=730,
        snapshot_retention_days=90,
        creative_10min_retention_days=90,
    )

    remaining_hours = db_session.query(GmvmaxProductCampaignMetricsHourly).all()
    remaining_batches = db_session.query(GmvmaxProductCampaignSnapshotBatch).all()
    remaining_creative_metrics = db_session.query(GmvCreativeMetrics10Min).all()
    remaining_creative_manifests = db_session.query(
        GmvCreative10MinBatchManifest
    ).all()
    assert len(remaining_hours) == 1
    assert remaining_hours[0].stat_time_hour == recent_hour
    assert len(remaining_batches) == 1
    assert remaining_batches[0].snapshot_at == recent_batch_time
    assert [row.creative_id for row in remaining_creative_metrics] == [
        "creative-recent"
    ]
    assert [row.snapshot_at for row in remaining_creative_manifests] == [
        recent_batch_time
    ]
    assert summary["creative_10min_metrics"] == 1
    assert summary["creative_10min_manifests"] == 1
