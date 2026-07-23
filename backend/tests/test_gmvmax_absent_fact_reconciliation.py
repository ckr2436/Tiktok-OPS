from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.data.models.gmv_restructured import (
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
)
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
)
from app.data.models.gmvmax_creative_metrics import (
    GmvmaxProductCreativeMetricsDaily,
)
from app.gmvmax.services import creative_report_sync
from app.gmvmax.services.campaign_report_sync import (
    SyncIdentifiers,
    _ensure_catalog_stub,
    sync_campaign_metrics,
)
from app.gmvmax.services.creative_report_sync import (
    sync_product_creative_metrics,
)
from app.gmvmax.services.report_pagination import (
    NumberedPaginationStalledError,
    report_page_has_more,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCreativeReportRequest,
    GMVMaxMetricsLevel,
    GMVMaxReportData,
    GMVMaxReportFiltering,
    GMVMaxResponse,
    TikTokBusinessGMVMaxClient,
)
from app.services.gmvmax_creative_metrics import _item_group_ids_for_campaign
from app.services.ttb_gmvmax import (
    fetch_gmvmax_current_creative_statuses,
    fetch_gmvmax_report_by_level,
    sync_gmvmax_overview_metrics,
    sync_gmvmax_product_metrics_daily,
    sync_gmvmax_product_metrics_hourly,
)


def _page(rows, *, has_more=False, total_page=1):
    return SimpleNamespace(
        data=SimpleNamespace(
            list=list(rows),
            page_info=SimpleNamespace(
                has_more=has_more,
                has_next=False,
                total_page=total_page,
                page_size=50,
            ),
        )
    )


def test_positive_continuation_wins_over_conflicting_terminal_signal():
    data = SimpleNamespace(
        page_info=SimpleNamespace(
            has_more=True,
            has_next=False,
            total_page=1,
            page_size=50,
        )
    )
    assert report_page_has_more(data, current_page=1, rows=[object()]) is True


def test_legacy_creative_wrapper_serializes_store_only_at_top_level():
    captured: dict[str, object] = {}

    async def _run():
        client = TikTokBusinessGMVMaxClient(access_token="test-token")

        async def _capture(params):
            captured.update(params)
            return GMVMaxResponse(
                code=0,
                message="OK",
                data=GMVMaxReportData(),
            )

        client._gmv_max_report_get = _capture
        try:
            await client.gmv_max_creative_report(
                GMVMaxCreativeReportRequest(
                    advertiser_id="adv-1",
                    metrics=["cost"],
                    dimensions=["campaign_id", "item_group_id", "item_id"],
                    start_time="2024-01-01",
                    end_time="2024-01-01",
                    campaign_ids=["campaign-1"],
                    filtering=GMVMaxReportFiltering(
                        store_ids=["store-1"],
                        campaign_ids=["campaign-1"],
                        item_group_ids=["item-1"],
                    ),
                )
            )
        finally:
            await client.aclose()

    asyncio.run(_run())

    assert json.loads(str(captured["store_ids"])) == ["store-1"]
    filtering = json.loads(str(captured["filtering"]))
    assert filtering == {
        "campaign_ids": ["campaign-1"],
        "item_group_ids": ["item-1"],
    }


def test_product_client_helper_does_not_send_unsupported_item_filter():
    captured: dict[str, object] = {}

    async def _run():
        client = TikTokBusinessGMVMaxClient(access_token="test-token")

        async def _capture(request, **_kwargs):
            captured["request"] = request
            return GMVMaxResponse(
                code=0,
                message="OK",
                data=GMVMaxReportData(),
            )

        client.gmv_max_report_get = _capture
        try:
            await client.fetch_gmvmax_report(
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                campaign_ids=["campaign-1"],
                level=GMVMaxMetricsLevel.PRODUCT,
                start_date="2024-01-01",
                end_date="2024-01-01",
                item_group_ids=["item-1"],
            )
        finally:
            await client.aclose()

    asyncio.run(_run())

    request = captured["request"]
    assert request.item_group_ids is None
    assert request.filtering.item_group_ids is None
    assert request.filtering.campaign_ids == ["campaign-1"]
    assert request.dimensions == ["item_group_id", "stat_time_day"]


class _TerminalClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[tuple[int, int]] = []
        self.requests: list[object] = []

    async def gmv_max_report_get(self, request, **_kwargs):
        self.calls.append((int(request.page or 1), int(request.page_size or 0)))
        self.requests.append(request.model_copy(deep=True))
        return _page(self.rows)


class _CreativeChunkClient:
    def __init__(self, *, fail_from_call: int | None = None):
        self.fail_from_call = fail_from_call
        self.requests: list[object] = []

    async def gmv_max_report_get(self, request, **_kwargs):
        self.requests.append(request)
        if (
            self.fail_from_call is not None
            and len(self.requests) >= self.fail_from_call
        ):
            raise RuntimeError("creative chunk failed")
        return _page([])


def test_creative_campaign_and_item_filters_are_chunked_at_100(db_session):
    client = _CreativeChunkClient()
    campaign_ids = [f"campaign-{index}" for index in range(101)]
    item_group_ids = [f"item-{index}" for index in range(101)]

    asyncio.run(
        sync_product_creative_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    assert len(client.requests) == 4
    assert {
        (
            len(request.filtering.campaign_ids),
            len(request.filtering.item_group_ids),
        )
        for request in client.requests
    } == {(100, 100), (100, 1), (1, 100), (1, 1)}
    assert all(request.page_size == 1000 for request in client.requests)


def test_creative_chunk_failure_never_deletes_later_chunk_fact(
    db_session,
    monkeypatch,
):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(creative_report_sync.asyncio, "sleep", _no_sleep)
    db_session.add(
        _creative_row(
            creative_id="must-survive",
            campaign_id="campaign-100",
            item_group_id="item-1",
        )
    )
    db_session.flush()
    client = _CreativeChunkClient(fail_from_call=2)

    with pytest.raises(RuntimeError, match="creative chunk failed"):
        asyncio.run(
            sync_product_creative_metrics(
                db_session,
                client,
                identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
                campaign_ids=[
                    f"campaign-{index}" for index in range(101)
                ],
                item_group_ids=["item-1"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 1),
            )
        )

    survivor = (
        db_session.query(GmvmaxProductCreativeMetricsDaily)
        .filter_by(creative_id="must-survive")
        .one()
    )
    assert survivor.cost_cents == 999


def test_creative_status_inventory_chunks_filters_and_uses_1000_page_size():
    client = _CreativeChunkClient()
    rows = asyncio.run(
        fetch_gmvmax_current_creative_statuses(
            client,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_ids=[
                f"campaign-{index}" for index in range(101)
            ],
            item_group_ids=[f"item-{index}" for index in range(101)],
            report_date=date(2024, 1, 1),
        )
    )

    assert rows == []
    assert len(client.requests) == 4
    assert all(
        len(request.filtering.campaign_ids) <= 100
        and len(request.filtering.item_group_ids) <= 100
        and request.page_size == 1000
        for request in client.requests
    )


def test_ten_minute_creative_fetch_chunks_item_filters_at_100():
    client = _CreativeChunkClient()
    rows = asyncio.run(
        fetch_gmvmax_report_by_level(
            client,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            campaign_ids=["campaign-1"],
            level="creative",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            item_group_ids=[f"item-{index}" for index in range(101)],
        )
    )

    assert rows == []
    assert len(client.requests) == 2
    assert all(
        request.filtering.campaign_ids == ["campaign-1"]
        and len(request.filtering.item_group_ids) <= 100
        and request.page_size == 1000
        for request in client.requests
    )


def test_ten_minute_product_resolver_merges_all_sources_without_limit_50():
    class _Result:
        def __init__(self, *, row=None, values=None):
            self.row = row
            self.values = list(values or [])

        def mappings(self):
            return self

        def scalars(self):
            return self

        def first(self):
            return self.row

        def all(self):
            return self.values

    class _Session:
        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement, _params):
            self.statements.append(str(statement))
            if len(self.statements) == 1:
                return _Result(
                    row={
                        "detail_raw_json": {
                            "item_group_ids": ["catalog-detail"]
                        },
                        "list_raw_json": {
                            "item_group_ids": ["catalog-list"]
                        },
                    }
                )
            return _Result(
                values=[
                    *(f"database-{index}" for index in range(120)),
                    "campaign-raw",
                ]
            )

    session = _Session()
    campaign = SimpleNamespace(
        campaign_id="campaign-1",
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        raw_json={"item_group_ids": ["campaign-raw"]},
        detail_raw_json={"item_group_ids": ["campaign-detail"]},
        list_raw_json={"item_group_ids": ["campaign-list"]},
    )

    resolved = _item_group_ids_for_campaign(session, campaign)

    assert resolved[:5] == [
        "campaign-raw",
        "campaign-detail",
        "campaign-list",
        "catalog-detail",
        "catalog-list",
    ]
    assert len(resolved) == 125
    assert "database-119" in resolved
    assert "limit 50" not in " ".join(session.statements).lower()


def test_catalog_lock_error_propagates_without_global_rollback_or_seen_pollution():
    class _Nested:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _LockedMysqlSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        def __init__(self):
            self.rollback_called = False

        def begin_nested(self):
            return _Nested()

        def execute(self, _statement):
            raise OperationalError(
                "insert catalog",
                {},
                Exception(1205, "lock wait timeout"),
            )

        def rollback(self):
            self.rollback_called = True
            raise AssertionError("catalog helper must not rollback the outer transaction")

    session = _LockedMysqlSession()
    seen: set[str] = set()
    with pytest.raises(OperationalError):
        _ensure_catalog_stub(
            session,
            SyncIdentifiers(1, 2, "adv-1", "store-1"),
            "campaign-1",
            "PRODUCT",
            seen,
        )

    assert session.rollback_called is False
    assert seen == set()


@pytest.mark.parametrize(
    ("model", "granularity", "time_column", "time_value", "dimension_name"),
    [
        (
            GmvmaxProductCampaignMetricsDaily,
            "DAILY",
            "stat_time_day",
            date(2024, 1, 1),
            "stat_time_day",
        ),
        (
            GmvmaxProductCampaignMetricsHourly,
            "HOURLY",
            "stat_time_hour",
            datetime(2024, 1, 1, 12),
            "stat_time_hour",
        ),
    ],
)
def test_campaign_complete_window_deletes_only_absent_nonfinal_scope(
    db_session,
    model,
    granularity,
    time_column,
    time_value,
    dimension_name,
):
    common = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        time_column: time_value,
    }
    db_session.add_all(
        [
            model(**common, campaign_id="omitted", cost_cents=999, is_final=False),
            model(**common, campaign_id="final", cost_cents=888, is_final=True),
            model(
                **{**common, "store_id": "store-2"},
                campaign_id="other-store",
                cost_cents=777,
                is_final=False,
            ),
        ]
    )
    db_session.flush()

    dimension_value = (
        time_value.isoformat()
        if isinstance(time_value, date)
        else str(time_value)
    )
    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "seen",
                    dimension_name: dimension_value,
                },
            }
        ]
    )
    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            promotion_type="PRODUCT",
            granularity=granularity,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    remaining = {
        (row.store_id, row.campaign_id): row
        for row in db_session.query(model).all()
    }
    assert ("store-1", "omitted") not in remaining
    assert remaining[("store-1", "final")].cost_cents == 888
    assert remaining[("store-1", "final")].is_final is True
    assert remaining[("store-2", "other-store")].cost_cents == 777
    assert remaining[("store-1", "seen")].cost_cents == 100


def test_campaign_filtered_sync_preserves_other_campaigns_and_filters_official_request(
    db_session,
):
    common = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        "stat_time_day": date(2024, 1, 1),
        "is_final": False,
    }
    db_session.add_all(
        [
            GmvmaxProductCampaignMetricsDaily(
                **common,
                campaign_id="current-campaign",
                cost_cents=999,
            ),
            GmvmaxProductCampaignMetricsDaily(
                **common,
                campaign_id="other-campaign",
                cost_cents=888,
            ),
        ]
    )
    db_session.flush()
    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "current-campaign",
                    "stat_time_day": "2024-01-01",
                },
            },
        ]
    )

    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            campaign_ids=["current-campaign"],
        )
    )

    rows = {
        row.campaign_id: row
        for row in db_session.query(GmvmaxProductCampaignMetricsDaily).all()
    }
    assert rows["current-campaign"].cost_cents == 100
    assert rows["other-campaign"].cost_cents == 888
    request = client.requests[0]
    assert list(request.campaign_ids or ()) == ["current-campaign"]
    assert list(request.filtering.campaign_ids or ()) == ["current-campaign"]


def test_campaign_filters_are_chunked_at_official_hundred_id_limit(db_session):
    client = _CreativeChunkClient()
    campaign_ids = [f"campaign-{index}" for index in range(101)]

    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            campaign_ids=campaign_ids,
        )
    )

    assert [len(request.campaign_ids or ()) for request in client.requests] == [
        100,
        1,
    ]
    assert all(
        list(request.campaign_ids or ())
        == list(request.filtering.campaign_ids or ())
        for request in client.requests
    )


@pytest.mark.parametrize(
    ("model", "sync_function", "time_column", "time_value", "dimension_name"),
    [
        (
            GmvProductMetricsDaily,
            sync_gmvmax_product_metrics_daily,
            "stat_time_day",
            date(2024, 1, 1),
            "stat_time_day",
        ),
        (
            GmvProductMetricsHourly,
            sync_gmvmax_product_metrics_hourly,
            "stat_time_hour",
            datetime(2024, 1, 1, 12),
            "stat_time_hour",
        ),
    ],
)
def test_product_complete_window_is_campaign_item_and_store_scoped(
    db_session,
    model,
    sync_function,
    time_column,
    time_value,
    dimension_name,
):
    common = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        "campaign_id": "campaign-1",
        time_column: time_value,
    }
    db_session.add_all(
        [
            model(**common, item_group_id="omitted", cost_cents=999, is_final=False),
            model(**common, item_group_id="final", cost_cents=888, is_final=True),
            model(
                **{**common, "campaign_id": "campaign-2"},
                item_group_id="other-campaign",
                cost_cents=777,
                is_final=False,
            ),
            model(
                **{**common, "store_id": "store-2"},
                item_group_id="other-store",
                cost_cents=666,
                is_final=False,
            ),
        ]
    )
    db_session.flush()

    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "item_group_id": "seen",
                    dimension_name: time_value.isoformat(),
                },
            }
        ]
    )
    asyncio.run(
        sync_function(
            db_session,
            client,
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            campaign=SimpleNamespace(
                campaign_id="campaign-1",
                store_id="store-1",
            ),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    remaining = {
        (row.store_id, row.campaign_id, row.item_group_id): row
        for row in db_session.query(model).all()
    }
    assert ("store-1", "campaign-1", "omitted") not in remaining
    assert remaining[("store-1", "campaign-1", "final")].is_final is True
    assert remaining[("store-1", "campaign-2", "other-campaign")].cost_cents == 777
    assert remaining[("store-2", "campaign-1", "other-store")].cost_cents == 666
    assert remaining[("store-1", "campaign-1", "seen")].cost_cents == 100


def _creative_row(
    *,
    creative_id: str,
    campaign_id: str = "campaign-1",
    item_group_id: str = "item-1",
    store_id: str = "store-1",
    is_final: bool = False,
    cost_cents: int = 999,
    net_cost_cents: int | None = None,
):
    return GmvmaxProductCreativeMetricsDaily(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id=store_id,
        campaign_id=campaign_id,
        item_group_id=item_group_id,
        creative_id=creative_id,
        stat_time_day=date(2024, 1, 1),
        cost_cents=cost_cents,
        net_cost_cents=net_cost_cents,
        is_final=is_final,
    )


def test_creative_multiday_row_without_stat_day_is_skipped_and_keeps_existing_facts(
    db_session,
):
    db_session.add(_creative_row(creative_id="must-survive"))
    db_session.flush()
    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "campaign-1",
                    "item_group_id": "item-1",
                    "item_id": "missing-day",
                },
            }
        ]
    )

    rows_synced = asyncio.run(
        sync_product_creative_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
        )
    )

    rows = {
        row.creative_id: row
        for row in db_session.query(GmvmaxProductCreativeMetricsDaily).all()
    }
    assert rows_synced == 0
    assert "missing-day" not in rows
    assert rows["must-survive"].cost_cents == 999


def test_creative_single_day_row_without_stat_day_uses_requested_day(db_session):
    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "campaign-1",
                    "item_group_id": "item-1",
                    "item_id": "single-day",
                },
            }
        ]
    )

    rows_synced = asyncio.run(
        sync_product_creative_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    row = (
        db_session.query(GmvmaxProductCreativeMetricsDaily)
        .filter_by(creative_id="single-day")
        .one()
    )
    assert rows_synced == 1
    assert row.stat_time_day == date(2024, 1, 1)
    assert row.cost_cents == 100


def test_creative_complete_window_deletes_only_requested_absent_nonfinal(
    db_session,
):
    db_session.add_all(
        [
            _creative_row(creative_id="omitted"),
            _creative_row(
                creative_id="seen",
                cost_cents=500,
                net_cost_cents=450,
            ),
            _creative_row(creative_id="final", is_final=True, cost_cents=888),
            _creative_row(creative_id="other-item", item_group_id="item-2"),
            _creative_row(creative_id="other-campaign", campaign_id="campaign-2"),
            _creative_row(creative_id="other-store", store_id="store-2"),
        ]
    )
    db_session.flush()
    client = _TerminalClient(
        [
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "campaign-1",
                    "item_group_id": "item-1",
                    "item_id": "seen",
                    "stat_time_day": "2024-01-01",
                },
            },
            {
                "metrics": {"cost": "1.00"},
                "dimensions": {
                    "campaign_id": "campaign-1",
                    "item_group_id": "item-1",
                    "item_id": "final",
                    "stat_time_day": "2024-01-01",
                },
            },
        ]
    )

    asyncio.run(
        sync_product_creative_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    remaining = {
        (row.store_id, row.campaign_id, row.item_group_id, row.creative_id): row
        for row in db_session.query(GmvmaxProductCreativeMetricsDaily).all()
    }
    assert ("store-1", "campaign-1", "item-1", "omitted") not in remaining
    assert remaining[("store-1", "campaign-1", "item-1", "final")].is_final is True
    assert remaining[("store-1", "campaign-1", "item-1", "final")].cost_cents == 888
    assert ("store-1", "campaign-1", "item-2", "other-item") in remaining
    assert ("store-1", "campaign-2", "item-1", "other-campaign") in remaining
    assert ("store-2", "campaign-1", "item-1", "other-store") in remaining
    assert remaining[("store-1", "campaign-1", "item-1", "seen")].cost_cents == 100
    assert (
        remaining[("store-1", "campaign-1", "item-1", "seen")].net_cost_cents
        is None
    )


def test_creative_status_only_row_clears_omitted_stale_metrics(
    db_session,
    monkeypatch,
):
    db_session.add(_creative_row(creative_id="status-only", cost_cents=500))
    db_session.flush()

    async def _statuses(*_args, **_kwargs):
        return [
            {
                "metrics": {"creative_delivery_status": "DELIVERING"},
                "dimensions": {
                    "campaign_id": "campaign-1",
                    "item_group_id": "item-1",
                    "item_id": "status-only",
                    "stat_time_day": "2024-01-01",
                },
            }
        ]

    monkeypatch.setattr(
        creative_report_sync,
        "fetch_gmvmax_current_creative_statuses",
        _statuses,
    )
    asyncio.run(
        sync_product_creative_metrics(
            db_session,
            _TerminalClient([]),
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            include_current_statuses=True,
        )
    )

    row = db_session.query(GmvmaxProductCreativeMetricsDaily).one()
    assert row.creative_id == "status-only"
    assert row.cost_cents is None
    assert row.creative_delivery_status == "DELIVERING"


def test_creative_report_status_only_row_does_not_preserve_stale_metrics(
    db_session,
):
    db_session.add(_creative_row(creative_id="status-only", cost_cents=500))
    db_session.flush()

    asyncio.run(
        sync_product_creative_metrics(
            db_session,
            _TerminalClient(
                [
                    {
                        "metrics": {
                            "creative_delivery_status": "NOT_DELIVERING",
                        },
                        "dimensions": {
                            "campaign_id": "campaign-1",
                            "item_group_id": "item-1",
                            "item_id": "status-only",
                            "stat_time_day": "2024-01-01",
                        },
                    }
                ]
            ),
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            campaign_ids=["campaign-1"],
            item_group_ids=["item-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    row = db_session.query(GmvmaxProductCreativeMetricsDaily).one()
    assert row.cost_cents is None
    assert row.creative_delivery_status == "NOT_DELIVERING"


class _FailingSecondPageClient:
    def __init__(self):
        self.calls: list[int] = []

    async def gmv_max_report_get(self, request, **_kwargs):
        page = int(request.page or 1)
        self.calls.append(page)
        if page == 2:
            raise RuntimeError("page 2 failed")
        return _page(
            [
                {
                    "metrics": {"cost": "1.00"},
                    "dimensions": {
                        "campaign_id": "campaign-1",
                        "item_group_id": "seen",
                        "stat_time_day": "2024-01-01",
                    },
                }
            ],
            has_more=True,
            total_page=2,
        )


class _RepeatedDimensionPageClient:
    async def gmv_max_report_get(self, request, **_kwargs):
        page = int(request.page or 1)
        return _page(
            [
                {
                    "metrics": {"cost": "1.00" if page == 1 else "2.00"},
                    "dimensions": {
                        "campaign_id": "campaign-1",
                        "item_group_id": "seen",
                        "stat_time_day": "2024-01-01",
                    },
                }
            ],
            has_more=page == 1,
            total_page=2,
        )


def test_partial_product_pagination_never_deletes_absent_fact(db_session):
    db_session.add(
        GmvProductMetricsDaily(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            item_group_id="must-survive",
            stat_time_day=date(2024, 1, 1),
            cost_cents=999,
            is_final=False,
        )
    )
    db_session.flush()

    with pytest.raises(RuntimeError, match="page 2 failed"):
        asyncio.run(
            sync_gmvmax_product_metrics_daily(
                db_session,
                _FailingSecondPageClient(),
                workspace_id=1,
                auth_id=2,
                advertiser_id="adv-1",
                campaign=SimpleNamespace(
                    campaign_id="campaign-1",
                    store_id="store-1",
                ),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 1),
            )
        )

    survivor = (
        db_session.query(GmvProductMetricsDaily)
        .filter_by(item_group_id="must-survive")
        .one()
    )
    assert survivor.cost_cents == 999


def test_repeated_dimension_page_never_reconciles_absent_fact(db_session):
    db_session.add(
        GmvProductMetricsDaily(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            item_group_id="must-survive",
            stat_time_day=date(2024, 1, 1),
            cost_cents=999,
            is_final=False,
        )
    )
    db_session.flush()

    with pytest.raises(
        NumberedPaginationStalledError,
        match="repeated a dimension key",
    ):
        asyncio.run(
            sync_gmvmax_product_metrics_daily(
                db_session,
                _RepeatedDimensionPageClient(),
                workspace_id=1,
                auth_id=2,
                advertiser_id="adv-1",
                campaign=SimpleNamespace(
                    campaign_id="campaign-1",
                    store_id="store-1",
                ),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 1),
            )
        )

    survivor = (
        db_session.query(GmvProductMetricsDaily)
        .filter_by(item_group_id="must-survive")
        .one()
    )
    assert survivor.cost_cents == 999


def test_out_of_window_campaign_row_disables_absence_deletion(db_session):
    db_session.add(
        GmvmaxProductCampaignMetricsDaily(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="must-survive",
            stat_time_day=date(2024, 1, 1),
            cost_cents=999,
            is_final=False,
        )
    )
    db_session.flush()

    asyncio.run(
        sync_campaign_metrics(
            db_session,
            _TerminalClient(
                [
                    {
                        "metrics": {"cost": "1.00"},
                        "dimensions": {
                            "campaign_id": "outside",
                            "stat_time_day": "2024-01-02",
                        },
                    }
                ]
            ),
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    assert (
        db_session.query(GmvmaxProductCampaignMetricsDaily)
        .filter_by(campaign_id="must-survive")
        .one()
        .cost_cents
        == 999
    )
    assert (
        db_session.query(GmvmaxProductCampaignMetricsDaily)
        .filter_by(campaign_id="outside")
        .count()
        == 0
    )


def test_out_of_window_product_row_disables_absence_deletion(db_session):
    db_session.add(
        GmvProductMetricsDaily(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            item_group_id="must-survive",
            stat_time_day=date(2024, 1, 1),
            cost_cents=999,
            is_final=False,
        )
    )
    db_session.flush()

    asyncio.run(
        sync_gmvmax_product_metrics_daily(
            db_session,
            _TerminalClient(
                [
                    {
                        "metrics": {"cost": "1.00"},
                        "dimensions": {
                            "item_group_id": "outside",
                            "stat_time_day": "2024-01-02",
                        },
                    }
                ]
            ),
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            campaign=SimpleNamespace(
                campaign_id="campaign-1",
                store_id="store-1",
            ),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    assert (
        db_session.query(GmvProductMetricsDaily)
        .filter_by(item_group_id="must-survive")
        .one()
        .cost_cents
        == 999
    )
    assert (
        db_session.query(GmvProductMetricsDaily)
        .filter_by(item_group_id="outside")
        .count()
        == 0
    )


class _NoPageInfoClampedClient:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    async def gmv_max_report_get(self, request, **_kwargs):
        page = int(request.page or 1)
        self.calls.append((page, int(request.page_size or 0)))
        if page == 1:
            rows = [
                {
                    "metrics": {"cost": "1.00"},
                    "dimensions": {
                        "campaign_id": f"campaign-{index}",
                        "stat_time_day": "2024-01-01",
                    },
                }
                for index in range(50)
            ]
        elif page == 2:
            rows = [
                {
                    "metrics": {"cost": "2.00"},
                    "dimensions": {
                        "campaign_id": "late-campaign",
                        "stat_time_day": "2024-01-01",
                    },
                }
            ]
        else:
            rows = []
        return SimpleNamespace(
            data=SimpleNamespace(list=rows, page_info=None)
        )


def test_missing_page_info_fetches_until_empty_before_reconciliation(db_session):
    db_session.add(
        GmvmaxProductCampaignMetricsDaily(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="late-campaign",
            stat_time_day=date(2024, 1, 1),
            cost_cents=999,
            is_final=False,
        )
    )
    db_session.flush()
    client = _NoPageInfoClampedClient()

    asyncio.run(
        sync_campaign_metrics(
            db_session,
            client,
            identifiers=SyncIdentifiers(1, 2, "adv-1", "store-1"),
            promotion_type="PRODUCT",
            granularity="DAILY",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    # The request uses the official maximum, while the simulated endpoint
    # clamps the actual response to 50 and omits all pagination metadata.
    assert client.calls == [(1, 1000), (2, 1000), (3, 1000)]
    late = (
        db_session.query(GmvmaxProductCampaignMetricsDaily)
        .filter_by(campaign_id="late-campaign")
        .one()
    )
    assert late.cost_cents == 200


@pytest.mark.parametrize(
    ("model", "granularity", "time_column", "time_value"),
    [
        (
            GmvOverviewMetricsDaily,
            "DAILY",
            "stat_time_day",
            date(2024, 1, 1),
        ),
        (
            GmvOverviewMetricsHourly,
            "HOUR",
            "stat_time_hour",
            datetime(2024, 1, 1, 12),
        ),
    ],
)
def test_overview_complete_window_reconciles_exact_store_only(
    db_session,
    model,
    granularity,
    time_column,
    time_value,
):
    common = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        time_column: time_value,
    }
    db_session.add_all(
        [
            model(**common, cost_cents=999, is_final=False),
            model(
                **{**common, "store_id": "store-2"},
                cost_cents=777,
                is_final=False,
            ),
        ]
    )
    db_session.flush()

    asyncio.run(
        sync_gmvmax_overview_metrics(
            db_session,
            _TerminalClient([]),
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_ids=["store-1"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            granularity=granularity,
        )
    )

    remaining = {
        row.store_id: row
        for row in db_session.query(model).all()
    }
    assert "store-1" not in remaining
    assert remaining["store-2"].cost_cents == 777
