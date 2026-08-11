from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

import app.celery_app  # noqa: F401 - initialize the application's task import order
from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.services import gmvmax_creative_guard as guard
from app.services import gmvmax_creative_metrics as creative_metrics
from app.services.gmvmax_creative_metrics import (
    _assert_complete_creative_rows,
    _merge_creative_entries_by_day,
    _register_complete_batch_manifests,
    latest_creative_metrics_snapshots,
    sync_creative_metrics_10min_for_campaign,
)


STAT_DAY = date(2026, 7, 17)


def _metric(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    creative_id: str,
    snapshot_at: datetime,
    cost_cents: int,
) -> GmvCreativeMetrics10Min:
    return GmvCreativeMetrics10Min(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
        item_group_id="product-1",
        creative_id=creative_id,
        stat_time_day=STAT_DAY,
        snapshot_at=snapshot_at,
        source_observed_at=snapshot_at,
        ingested_at=snapshot_at,
        cost_cents=cost_cents,
    )


def _manifest(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    snapshot_at: datetime,
    row_count: int,
) -> GmvCreative10MinBatchManifest:
    return GmvCreative10MinBatchManifest(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
        stat_time_day=STAT_DAY,
        snapshot_at=snapshot_at,
        complete=True,
        row_count=row_count,
        source_observed_at=snapshot_at,
    )


def _latest(db_session, *, workspace_id: int, auth_id: int, advertiser_id: str):
    return latest_creative_metrics_snapshots(
        db_session,
        workspace_id=workspace_id,
        provider="tiktok-business",
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_ids=["campaign-1"],
        start_date=STAT_DAY,
        end_date=STAT_DAY,
        store_ids=["store-1"],
    )


def test_latest_reader_uses_one_complete_batch_and_isolates_scope(db_session):
    first = datetime(2026, 7, 17, 10, 0)
    second = datetime(2026, 7, 17, 10, 10)
    unmanifested = datetime(2026, 7, 17, 10, 20)
    db_session.add_all(
        [
            _manifest(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                snapshot_at=first,
                row_count=2,
            ),
            _metric(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-current",
                snapshot_at=first,
                cost_cents=100,
            ),
            _metric(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-absent-next",
                snapshot_at=first,
                cost_cents=200,
            ),
            _manifest(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                snapshot_at=second,
                row_count=1,
            ),
            _metric(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-current",
                snapshot_at=second,
                cost_cents=150,
            ),
            # A newer legacy detail row without a manifest must be invisible.
            _metric(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-unmanifested",
                snapshot_at=unmanifested,
                cost_cents=999,
            ),
            _manifest(
                workspace_id=2,
                auth_id=20,
                advertiser_id="advertiser-2",
                store_id="store-1",
                campaign_id="campaign-1",
                snapshot_at=unmanifested,
                row_count=1,
            ),
            _metric(
                workspace_id=2,
                auth_id=20,
                advertiser_id="advertiser-2",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-other-scope",
                snapshot_at=unmanifested,
                cost_cents=500,
            ),
        ]
    )
    db_session.flush()

    scope_one = _latest(
        db_session,
        workspace_id=1,
        auth_id=10,
        advertiser_id="advertiser-1",
    )
    scope_two = _latest(
        db_session,
        workspace_id=2,
        auth_id=20,
        advertiser_id="advertiser-2",
    )

    assert [(row.creative_id, row.cost_cents) for row in scope_one] == [
        ("creative-current", 150)
    ]
    assert [row.creative_id for row in scope_two] == ["creative-other-scope"]


def test_latest_zero_row_complete_batch_hides_previous_inventory(db_session):
    first = datetime(2026, 7, 17, 10, 0)
    empty = datetime(2026, 7, 17, 10, 10)
    db_session.add_all(
        [
            _manifest(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                snapshot_at=first,
                row_count=1,
            ),
            _metric(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                creative_id="creative-old",
                snapshot_at=first,
                cost_cents=100,
            ),
            _manifest(
                workspace_id=1,
                auth_id=10,
                advertiser_id="advertiser-1",
                store_id="store-1",
                campaign_id="campaign-1",
                snapshot_at=empty,
                row_count=0,
            ),
        ]
    )
    db_session.flush()

    assert (
        _latest(
            db_session,
            workspace_id=1,
            auth_id=10,
            advertiser_id="advertiser-1",
        )
        == []
    )


def test_zero_row_manifest_is_registered_as_complete(db_session):
    observed_at = datetime(2026, 7, 17, 10, 10)
    _register_complete_batch_manifests(
        db_session,
        workspace_id=1,
        auth_id=10,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        snapshot_at=observed_at,
        source_observed_at=observed_at,
        row_counts={STAT_DAY: 0},
    )
    db_session.flush()
    retry_observed_at = datetime(2026, 7, 17, 10, 11)
    _register_complete_batch_manifests(
        db_session,
        workspace_id=1,
        auth_id=10,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        snapshot_at=observed_at,
        source_observed_at=retry_observed_at,
        row_counts={STAT_DAY: 3},
    )
    db_session.flush()

    manifest = db_session.query(GmvCreative10MinBatchManifest).one()
    assert manifest.complete is True
    assert manifest.row_count == 3
    assert manifest.source_observed_at == retry_observed_at


def test_zero_row_official_snapshot_still_writes_batch_watermark(monkeypatch):
    snapshot_at = datetime(2026, 7, 17, 10, 10)
    captured: dict[str, object] = {}

    class _Session:
        statements: list[object] = []
        flush_count = 0

        def execute(self, statement, _params=None):
            self.statements.append(statement)
            return None

        def flush(self):
            self.flush_count += 1

    async def _empty_report(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        creative_metrics,
        "_item_group_ids_for_campaign",
        lambda *_args: ["product-1"],
    )
    monkeypatch.setattr(
        creative_metrics,
        "fetch_gmvmax_report_by_level",
        _empty_report,
    )
    monkeypatch.setattr(
        creative_metrics,
        "fetch_gmvmax_current_creative_statuses",
        _empty_report,
    )
    monkeypatch.setattr(
        creative_metrics,
        "_floor_snapshot",
        lambda *_args: snapshot_at,
    )

    def _capture_manifest(_session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        creative_metrics,
        "_register_complete_batch_manifests",
        _capture_manifest,
    )
    session = _Session()
    result = asyncio.run(
        sync_creative_metrics_10min_for_campaign(
            session,
            object(),
            workspace_id=1,
            provider="tiktok-business",
            auth_id=10,
            advertiser_id="advertiser-1",
            campaign={
                "campaign_id": "campaign-1",
                "store_id": "store-1",
                "advertiser_timezone": "UTC",
            },
            start_date=STAT_DAY,
            end_date=STAT_DAY,
        )
    )

    assert result["rows"] == 0
    assert result["complete_batches"] == 1
    assert captured["row_counts"] == {STAT_DAY: 0}
    assert captured["snapshot_at"] == snapshot_at
    assert session.flush_count == 2


def test_duplicate_rows_fail_closed_before_manifest_registration():
    row = {
        "metrics": {"cost": "1.00"},
        "dimensions": {
            "campaign_id": "campaign-1",
            "product_id": "product-1",
            "shop_content_id": "creative-1",
            "stat_time_day": STAT_DAY,
        },
    }

    with pytest.raises(ValueError, match="duplicate"):
        _assert_complete_creative_rows(
            [row, row],
            campaign_id="campaign-1",
            item_group_ids=["product-1"],
            start_date=STAT_DAY,
            end_date=STAT_DAY,
        )


def test_same_key_across_performance_and_status_is_a_valid_merge():
    performance = {
        "metrics": {
            "cost": "1.00",
            "orders": 2,
            "creative_delivery_status": "DELIVERING",
        },
        "dimensions": {
            "campaign_id": "campaign-1",
            "product_id": "product-1",
            "shop_content_id": "creative-1",
            "stat_time_day": STAT_DAY,
        },
    }
    status = {
        "metrics": {"creative_delivery_status": "IN_QUEUE"},
        "dimensions": {
            "campaign_id": "campaign-1",
            "item_group_id": "product-1",
            "item_id": "creative-1",
            "stat_time_day": STAT_DAY,
        },
    }

    merged = _merge_creative_entries_by_day(
        [performance],
        [status],
        campaign_id="campaign-1",
        item_group_ids=["product-1"],
        status_day=STAT_DAY,
    )

    assert len(merged) == 1
    assert merged[0]["metrics"]["cost"] == "1.00"
    assert merged[0]["metrics"]["orders"] == 2
    assert merged[0]["metrics"]["creative_delivery_status"] == "IN_QUEUE"


def test_incomplete_row_fails_closed_before_manifest_registration():
    with pytest.raises(ValueError, match="incomplete"):
        _assert_complete_creative_rows(
            [
                {
                    "metrics": {"cost": "1.00"},
                    "dimensions": {
                        "campaign_id": "campaign-1",
                        "product_id": "product-1",
                        "shop_content_id": "creative-1",
                    },
                }
            ],
            campaign_id="campaign-1",
            item_group_ids=["product-1"],
            start_date=STAT_DAY,
            end_date=STAT_DAY,
        )


def test_guard_holds_on_latest_complete_zero_row_batch(monkeypatch):
    now = datetime(2026, 7, 17, 10, 11, tzinfo=timezone.utc)

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {
                "campaign_report_at": now.replace(tzinfo=None),
                "campaign_age_local": 0,
                "campaign_age_utc": 0,
                "creative_snapshot_at": now.replace(tzinfo=None),
                "creative_observed_at": now.replace(tzinfo=None),
                "creative_snapshot_rows": 0,
                "creative_daily_rows": 99,
            }

    class _Db:
        statement = ""

        def execute(self, statement, _params):
            self.statement = " ".join(str(statement).split())
            return _Result()

    db = _Db()
    monkeypatch.setattr(guard, "_advertiser_today", lambda *_args: STAT_DAY)

    quality = guard._creative_guard_data_quality(
        db,
        guard.CampaignScope(
            strategy_id=1,
            workspace_id=1,
            auth_id=10,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-1",
            campaign_name="Campaign",
            operation_status="ENABLE",
            secondary_status="CAMPAIGN_STATUS_ENABLE",
            budget_cents=1000,
            roas_bid=None,
            config={},
            monitor_state={},
            smart_guard_state={},
        ),
        now=now,
    )

    assert quality["state"] == "hold"
    assert quality["campaign_valid"] is True
    assert quality["creative_valid"] is False
    assert quality["creative_rows"] == 0
    assert "gmv_creative_10min_batch_manifests" in db.statement
