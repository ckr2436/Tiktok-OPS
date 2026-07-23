from __future__ import annotations

import asyncio
import ast
import inspect
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.gmvmax.services.mutation_execution_lock import GmvMaxMutationFenceLost
from app.services import gmvmax_creative_guard, gmvmax_smart_guard
from app.services.gmvmax_smart_guard import (
    CatalogCampaign,
    RealtimeMetrics,
    _decide,
    _fetch_today_metrics,
    _product_day_stats,
)
from app.services.ttb_api import TTBBusinessError


class _Client:
    def __init__(self, response) -> None:
        self.responses = (
            list(response) if isinstance(response, (list, tuple)) else [response]
        )
        self.closed = False
        self.requested_pages: list[int] = []
        self.requests: list[dict] = []

    async def gmv_max_report_get(self, request, **_):
        page = int(request.page or 1)
        self.requested_pages.append(page)
        self.requests.append(request.model_dump(mode="json", exclude_none=True))
        if page <= len(self.responses):
            return self.responses[page - 1]
        return SimpleNamespace(
            request_id=f"empty-{page}",
            data=SimpleNamespace(list=[], page_info=None),
        )

    async def aclose(self) -> None:
        self.closed = True


class _MappingResult:
    def __init__(self, row) -> None:
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _StatsDb:
    def __init__(self, row) -> None:
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _MappingResult(self.row)


def _campaign() -> CatalogCampaign:
    return CatalogCampaign(
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        promotion_type="PRODUCT",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_value=10000,
        roas_bid=None,
    )


def _campaign_day_dimensions(
    *,
    campaign_id: str = "campaign-1",
    stat_time_day: str = "2026-07-17",
) -> dict[str, str]:
    return {
        "campaign_id": campaign_id,
        "stat_time_day": stat_time_day,
    }


def test_external_disabled_campaign_never_gets_automatic_recovery(monkeypatch):
    now = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    campaign = _campaign()
    campaign.operation_status = "DISABLE"
    campaign.secondary_status = "CAMPAIGN_STATUS_DISABLE"
    strategy = SimpleNamespace(
        id=9,
        workspace_id=7,
        auth_id=11,
        campaign_id=campaign.campaign_id,
        config_json={"smart_guard": {"enabled": True}},
        cooldown_minutes=30,
        min_roi=None,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "current_order_timing_signal",
        lambda *_, **__: {"available": False, "confidence": 0, "delivery_multiplier": 1},
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_dynamic_no_order_spend_cents",
        lambda *_, **__: (300, {}),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_product_day_stats",
        lambda *_, **__: {
            "item_group_ids": [],
            "cost_cents": 0,
            "gross_revenue_cents": 0,
            "orders": 0,
            "roi": None,
            "campaign_count": 0,
            "source": "none",
        },
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_recent_product_failure_stats",
        lambda *_, **__: {},
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_product_recent_momentum_stats",
        lambda *_, **__: {"roi": None, "active_campaign": {}},
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_decision_consistency_snapshot",
        lambda *_, **__: {
            "valid": True,
            "product_usable": True,
            "recent_momentum_usable": False,
            "attribution_pending": False,
            "conflicts": [],
        },
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_dynamic_bad_performance_cap_cents",
        lambda *_, **__: (None, {}),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_controlled_test_budget_bounds",
        lambda *_, **__: {},
    )

    decision = _decide(
        SimpleNamespace(),
        strategy=strategy,
        campaign=campaign,
        metrics=RealtimeMetrics(),
        now=now,
    )

    assert decision["action"] == "HOLD"
    assert decision["paused_until"] is None
    assert decision["decision_phase"] == "EXPLICIT_ENABLE_REQUIRED"
    assert decision["monitor_interval_minutes"] == 1


def test_smart_guard_never_writes_canonical_campaign_daily_facts():
    source = inspect.getsource(gmvmax_smart_guard._upsert_realtime_state)

    assert "gmv_campaign_realtime_state" in source
    assert "gmvmax_product_campaign_metrics_daily" not in source
    assert "gmvmax_live_campaign_metrics_daily" not in source


def test_fetch_today_metrics_preserves_official_zero_net_cost(monkeypatch):
    response = SimpleNamespace(
        request_id="request-1",
        data=SimpleNamespace(
            list=[
                SimpleNamespace(
                    metrics={
                        "cost": "12.34",
                        "net_cost": "0",
                        "gross_revenue": "20.00",
                        "orders": "1",
                    },
                    dimensions=_campaign_day_dimensions(
                        stat_time_day="2026-07-17 00:00:00"
                    ),
                )
            ]
        ),
    )
    client = _Client(response)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_, **__: client,
    )

    metrics = asyncio.run(_fetch_today_metrics(SimpleNamespace(), _campaign()))

    assert metrics.cost_cents == 1234
    assert metrics.net_cost_cents == 0
    assert metrics.gross_revenue_cents == 2000
    assert metrics.roi == gmvmax_smart_guard.Decimal("1.6207")
    assert metrics.raw["roi_audit"]["selected_source"] == "aggregate_gross_over_total_cost"
    assert metrics.raw["pagination_complete"] is True
    assert metrics.raw["pages_fetched"] == 2
    assert client.requested_pages == [1, 2]
    assert client.requests[0]["page_size"] == 1000
    assert client.requests[0]["campaign_ids"] == ["campaign-1"]
    assert client.requests[0]["gmv_max_promotion_types"] == ["PRODUCT"]
    assert client.closed is True


def test_fetch_today_metrics_prefers_single_official_roi_and_audits_difference(monkeypatch):
    response = SimpleNamespace(
        request_id="request-2",
        data=SimpleNamespace(
            list=[
                SimpleNamespace(
                    metrics={
                        "cost": "100.00",
                        "net_cost": "20.00",
                        "gross_revenue": "200.00",
                        "orders": "2",
                        "roi": "1.8",
                    },
                    dimensions=_campaign_day_dimensions(),
                )
            ]
        ),
    )
    client = _Client(response)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    monkeypatch.setattr(gmvmax_smart_guard, "build_ttb_gmvmax_client", lambda *_, **__: client)

    metrics = asyncio.run(_fetch_today_metrics(SimpleNamespace(), _campaign()))

    assert metrics.roi == gmvmax_smart_guard.Decimal("1.8000")
    assert metrics.net_cost_cents == 2000
    assert metrics.raw["roi_audit"] == {
        "selected_source": "official_single_row",
        "selected_roi": "1.8000",
        "official_single_row_roi": "1.8000",
        "reported_row_rois": ["1.8000"],
        "calculated_gross_over_total_cost": "2.0000",
        "official_minus_calculated": "-0.2000",
        "total_cost_cents": 10000,
        "net_cost_cents": 2000,
    }


def test_fetch_today_metrics_rejects_duplicate_campaign_day_rows(monkeypatch):
    response = SimpleNamespace(
        request_id="request-3",
        data=SimpleNamespace(
            list=[
                SimpleNamespace(
                    metrics={
                        "cost": "100.00",
                        "net_cost": "50.00",
                        "gross_revenue": "150.00",
                        "orders": "1",
                        "roi": "9",
                    },
                    dimensions=_campaign_day_dimensions(),
                ),
                SimpleNamespace(
                    metrics={
                        "cost": "100.00",
                        "net_cost": "50.00",
                        "gross_revenue": "50.00",
                        "orders": "1",
                        "roi": "9",
                    },
                    dimensions=_campaign_day_dimensions(),
                ),
            ]
        ),
    )
    client = _Client(response)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    monkeypatch.setattr(gmvmax_smart_guard, "build_ttb_gmvmax_client", lambda *_, **__: client)

    with pytest.raises(TTBBusinessError) as exc_info:
        asyncio.run(_fetch_today_metrics(SimpleNamespace(), _campaign()))

    assert exc_info.value.code == "GMVMAX_SMART_GUARD_REPORT_INCOMPLETE"
    assert client.closed is True


@pytest.mark.parametrize(
    ("dimensions", "expected_code"),
    [
        ({}, "GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID"),
        (
            _campaign_day_dimensions(campaign_id="campaign-wrong"),
            "GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID",
        ),
        (
            _campaign_day_dimensions(stat_time_day="2026-07-16"),
            "GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID",
        ),
    ],
)
def test_fetch_today_metrics_rejects_rows_outside_exact_scope(
    monkeypatch,
    dimensions,
    expected_code,
):
    response = SimpleNamespace(
        request_id="request-scope",
        data=SimpleNamespace(
            list=[
                SimpleNamespace(
                    metrics={"cost": "1.00", "gross_revenue": "0", "orders": "0"},
                    dimensions=dimensions,
                )
            ],
            page_info=SimpleNamespace(
                page=1,
                page_size=1000,
                total_page=1,
                total_number=1,
                has_more=False,
            ),
        ),
    )
    client = _Client(response)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_, **__: client,
    )

    with pytest.raises(TTBBusinessError) as exc_info:
        asyncio.run(_fetch_today_metrics(SimpleNamespace(), _campaign()))

    assert exc_info.value.code == expected_code
    assert client.closed is True


def test_fetch_today_metrics_rejects_incomplete_terminal_page(monkeypatch):
    first_page = SimpleNamespace(
        request_id="request-page-1",
        data=SimpleNamespace(
            list=[
                SimpleNamespace(
                    metrics={"cost": "1.00", "gross_revenue": "0", "orders": "0"},
                    dimensions=_campaign_day_dimensions(),
                )
            ],
            page_info=SimpleNamespace(
                page=1,
                page_size=1,
                total_page=2,
                total_number=2,
                has_more=True,
            ),
        ),
    )
    empty_second_page = SimpleNamespace(
        request_id="request-page-2",
        data=SimpleNamespace(
            list=[],
            page_info=SimpleNamespace(
                page=2,
                page_size=1,
                total_page=2,
                total_number=2,
                has_more=False,
            ),
        ),
    )
    client = _Client([first_page, empty_second_page])
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_, **__: client,
    )

    with pytest.raises(TTBBusinessError) as exc_info:
        asyncio.run(_fetch_today_metrics(SimpleNamespace(), _campaign()))

    assert exc_info.value.code == "GMVMAX_SMART_GUARD_REPORT_INCOMPLETE"
    assert client.requested_pages == [1, 2]
    assert client.closed is True


def test_product_day_uses_one_official_observation_not_per_field_max(monkeypatch):
    observed_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    row = {
        "total_cost_cents": 15000,
        "total_gmv_cents": 30000,
        "total_orders": 5,
        "campaign_count": 2,
        "current_cost_cents": 10000,
        "current_gmv_cents": 20000,
        "current_orders": 3,
        "source_updated_at": observed_at.replace(tzinfo=None),
    }
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_item_group_ids",
        lambda *_: ["product-1"],
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_advertiser_today",
        lambda *_, **__: date(2026, 7, 17),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_source_age_seconds",
        lambda *_: 0,
    )
    official = RealtimeMetrics(
        cost_cents=7000,
        net_cost_cents=7000,
        gross_revenue_cents=15000,
        orders=2,
        raw={"rows": [{}]},
        row_count=1,
        fetched_at=observed_at,
    )

    result = _product_day_stats(
        _StatsDb(row),
        campaign=_campaign(),
        guard={},
        metrics=official,
    )

    assert result["canonical_current_source"] == "official_campaign_report"
    assert result["cost_cents"] == 12000
    assert result["gross_revenue_cents"] == 25000
    assert result["orders"] == 4
    assert result["current_campaign"]["creative_daily_observation"]["cost_cents"] == 10000
    assert result["current_campaign"]["official_observation"]["cost_cents"] == 7000


def test_product_day_falls_back_as_one_complete_creative_observation(monkeypatch):
    observed_at = datetime(2026, 7, 17, 5, 0)
    row = {
        "total_cost_cents": 15000,
        "total_gmv_cents": 30000,
        "total_orders": 5,
        "campaign_count": 2,
        "current_cost_cents": 10000,
        "current_gmv_cents": 20000,
        "current_orders": 3,
        "source_updated_at": observed_at,
    }
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_item_group_ids",
        lambda *_: ["product-1"],
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_advertiser_today",
        lambda *_, **__: date(2026, 7, 17),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_source_age_seconds",
        lambda *_: 0,
    )

    result = _product_day_stats(
        _StatsDb(row),
        campaign=_campaign(),
        guard={},
        metrics=RealtimeMetrics(raw=None, row_count=0),
    )

    assert result["canonical_current_source"] == "creative_daily_fallback"
    assert result["cost_cents"] == 15000
    assert result["gross_revenue_cents"] == 30000
    assert result["orders"] == 5


def test_recent_product_momentum_isolated_by_item_group(monkeypatch):
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    early = now.replace(tzinfo=None) - timedelta(minutes=70)
    late = now.replace(tzinfo=None) - timedelta(minutes=10)
    snapshots = [
        {
            "campaign_id": "campaign-1",
            "item_group_id": "product-1",
            "creative_id": "shared-creative",
            "snapshot_at": early,
            "cost_cents": 100,
            "gross_revenue_cents": 0,
            "orders": 0,
            "impressions": 10,
            "clicks": 1,
        },
        {
            "campaign_id": "campaign-1",
            "item_group_id": "product-1",
            "creative_id": "shared-creative",
            "snapshot_at": late,
            "cost_cents": 150,
            "gross_revenue_cents": 100,
            "orders": 1,
            "impressions": 20,
            "clicks": 2,
        },
        {
            "campaign_id": "campaign-1",
            "item_group_id": "product-2",
            "creative_id": "shared-creative",
            "snapshot_at": early,
            "cost_cents": 0,
            "gross_revenue_cents": 0,
            "orders": 0,
            "impressions": 0,
            "clicks": 0,
        },
        {
            "campaign_id": "campaign-1",
            "item_group_id": "product-2",
            "creative_id": "shared-creative",
            "snapshot_at": late,
            "cost_cents": 1000,
            "gross_revenue_cents": 5000,
            "orders": 10,
            "impressions": 1000,
            "clicks": 100,
        },
    ]
    for snapshot in snapshots:
        snapshot["latest_complete_snapshot_at"] = late

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class _Db:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return _Rows(["campaign-1"] if len(self.calls) == 1 else snapshots)

    db = _Db()
    monkeypatch.setattr(gmvmax_smart_guard, "_source_age_seconds", lambda *_: 0)

    result = gmvmax_smart_guard._product_recent_momentum_stats(
        db,
        campaign=_campaign(),
        guard={"recent_momentum_window_minutes": 60},
        item_group_ids=["product-1"],
        now=now,
    )

    assert result["cost_cents"] == 50
    assert result["gross_revenue_cents"] == 100
    assert result["orders"] == 1
    assert result["active_campaign"]["orders"] == 1
    query, params = db.calls[1]
    normalized = " ".join(query.split())
    assert "item_group_id in" in normalized
    assert (
        "order by m.campaign_id, m.item_group_id, m.creative_id, "
        "m.snapshot_at"
    ) in normalized
    assert "join gmv_creative_10min_batch_manifests b" in normalized
    assert params["item_group_ids"] == ["product-1"]


def test_every_guard_10min_query_contains_complete_tenant_scope():
    for module in (gmvmax_smart_guard, gmvmax_creative_guard):
        tree = ast.parse(inspect.getsource(module))
        sql_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "gmv_creative_metrics_10min" in node.value
            and "from " in node.value.lower()
        ]
        assert sql_literals
        for sql in sql_literals:
            normalized = " ".join(sql.lower().split())
            assert ":workspace_id" in normalized
            assert ":auth_id" in normalized
            assert ":advertiser_id" in normalized
            assert ":store_id" in normalized


@pytest.mark.parametrize(
    ("lose_on_assert", "expected_remote_calls"),
    [(1, 0), (2, 1)],
)
def test_status_mutation_loss_never_commits_local_success(
    monkeypatch,
    lose_on_assert: int,
    expected_remote_calls: int,
) -> None:
    campaign = _campaign()
    remote_calls: list[object] = []

    class _Mutation:
        def __init__(self):
            self.assertions = 0
            self.commits = 0

        def assert_current(self, _db):
            self.assertions += 1
            if self.assertions == lose_on_assert:
                raise GmvMaxMutationFenceLost("injected ownership loss")

        def commit(self, _db):
            self.commits += 1

    class _Client:
        async def campaign_status_update(self, request):
            remote_calls.append(request)
            return SimpleNamespace(request_id="request-1")

        async def aclose(self):
            return None

    class _Db:
        def __init__(self):
            self.writes: list[object] = []

        def execute(self, statement, _params):
            self.writes.append(statement)

    mutation = _Mutation()
    db = _Db()
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_reload_catalog_campaign_for_mutation",
        lambda *_args: campaign,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_assert_smart_guard_mutation_allowed",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: _Client(),
    )

    with pytest.raises(GmvMaxMutationFenceLost, match="injected"):
        asyncio.run(
            gmvmax_smart_guard._apply_status_action_unlocked(
                db,
                campaign=campaign,
                action="PAUSE",
                mutation=mutation,
            )
        )

    assert len(remote_calls) == expected_remote_calls
    assert db.writes == []
    assert mutation.commits == 0


def test_budget_adjustment_stamps_authority_before_fenced_commit(
    monkeypatch,
) -> None:
    campaign = _campaign()
    observed_at = datetime(2026, 7, 17, 8, 30)

    class _Mutation:
        def __init__(self):
            self.assertions = 0
            self.commits = 0

        def assert_current(self, _db):
            self.assertions += 1

        def commit(self, _db):
            self.commits += 1

    class _Client:
        async def gmv_max_campaign_update(self, _request):
            return SimpleNamespace(request_id="request-1")

        async def aclose(self):
            return None

    class _Db:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def execute(self, statement, params):
            self.calls.append((" ".join(str(statement).split()), dict(params)))

    mutation = _Mutation()
    db = _Db()
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_reload_catalog_campaign_for_mutation",
        lambda *_args: campaign,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_assert_smart_guard_mutation_allowed",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: _Client(),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "catalog_observation_now",
        lambda: observed_at,
    )

    asyncio.run(
        gmvmax_smart_guard._apply_campaign_adjustment_unlocked(
            db,
            campaign=campaign,
            adjustment={
                "budget": 55.0,
                "budget_cents": 5500,
                "roas_bid": 1.4,
            },
            mutation=mutation,
        )
    )

    sql, params = db.calls[-1]
    assert "list_synced_at=:observed_at" in sql
    assert "detail_synced_at=:observed_at" in sql
    assert "modify_time_utc=:observed_at" in sql
    assert params["observed_at"] == observed_at
    assert params["budget_cents"] == 5500
    assert params["roas_bid"] == "1.4"
    assert mutation.assertions == 2
    assert mutation.commits == 1


@pytest.mark.parametrize(
    "control_state",
    ["strategy_disabled", "creation_quarantine", "intent_finalizing"],
)
@pytest.mark.parametrize("requested_action", ["PAUSE", "ADJUST"])
def test_smart_guard_stale_enabled_scope_holds_before_official_mutation(
    monkeypatch,
    control_state: str,
    requested_action: str,
) -> None:
    """A strategy selected as enabled may become fenced before its official write."""

    campaign = _campaign()
    strategy = SimpleNamespace(
        id=9,
        workspace_id=campaign.workspace_id,
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        enabled=True,
        config_json={"smart_guard": {"enabled": True}},
        cooldown_minutes=30,
        monitor_interval_minutes=3,
        min_roi=Decimal("0.8"),
    )
    metrics = RealtimeMetrics(
        cost_cents=1_000,
        net_cost_cents=1_000,
        gross_revenue_cents=0,
        orders=0,
        raw={},
        row_count=1,
    )

    class _Result:
        def __init__(self, *, rows=None, row=None):
            self.rows = list(rows or [])
            self.row = row

        def mappings(self):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.row

    class _Db:
        def __init__(self):
            self.rollbacks = 0

        def execute(self, statement, _parameters):
            sql = " ".join(str(statement).lower().split())
            if "from gmv_strategy_configs" in sql:
                config = (
                    {"creation_quarantine": {"enabled": True}}
                    if control_state == "creation_quarantine"
                    else {}
                )
                return _Result(
                    rows=[
                        {
                            "enabled": control_state != "strategy_disabled",
                            "config_json": config,
                        }
                    ]
                )
            if "from gmvmax_campaign_create_intents" in sql:
                return _Result(
                    row=(
                        {"state": "FINALIZING"}
                        if control_state == "intent_finalizing"
                        else None
                    )
                )
            raise AssertionError(f"unexpected SQL during mutation fence: {sql}")

        def rollback(self):
            self.rollbacks += 1

    class _Mutation:
        def assert_current(self, _db):
            return None

    @contextmanager
    def _mutation_lease(*_args, **_kwargs):
        yield _Mutation()

    class _OfficialClient:
        def __init__(self):
            self.status_calls = 0
            self.budget_calls = 0

        async def campaign_status_update(self, _request):
            self.status_calls += 1

        async def gmv_max_campaign_update(self, _request):
            self.budget_calls += 1

        async def aclose(self):
            return None

    official_client = _OfficialClient()
    client_builds = []
    db = _Db()

    async def _fetch_metrics(*_args, **_kwargs):
        return metrics

    async def _review_decision(*_args, decision, **_kwargs):
        return decision

    decision = {
        "action": requested_action,
        "reason": "test stale enabled scope",
        "threshold_context": {},
    }
    if requested_action == "ADJUST":
        decision["adjustment"] = {
            "budget": 55.0,
            "budget_cents": 5_500,
        }

    monkeypatch.setattr(gmvmax_smart_guard, "_load_enabled_strategies", lambda *_: [strategy])
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_catalog_campaign",
        lambda *_args: campaign,
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_load_runtime_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gmvmax_smart_guard, "_strategy_due", lambda *_args: True)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "is_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_fetch_today_metrics", _fetch_metrics)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_assess_realtime_metrics_quality",
        lambda *_args, **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_decide",
        lambda *_args, **_kwargs: dict(decision),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_prepare_two_stage_decision",
        lambda **kwargs: kwargs["decision"],
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_enqueue_conflict_sync", lambda **_kwargs: None)
    monkeypatch.setattr(gmvmax_smart_guard, "_review_two_stage_decision", _review_decision)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_dynamic_monitor_interval_minutes",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_guard_config", lambda *_args: {})
    monkeypatch.setattr(gmvmax_smart_guard, "_smart_guard_state", lambda *_args: {})
    monkeypatch.setattr(gmvmax_smart_guard, "_insert_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_reload_catalog_campaign_for_mutation",
        lambda *_args: campaign,
    )
    monkeypatch.setattr(gmvmax_smart_guard, "gmvmax_mutation_lease", _mutation_lease)

    def _build_client(*_args, **_kwargs):
        client_builds.append(True)
        return official_client

    monkeypatch.setattr(gmvmax_smart_guard, "build_ttb_gmvmax_client", _build_client)

    summary = asyncio.run(
        gmvmax_smart_guard.run_smart_guard_cycle(
            db,
            now=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
        )
    )

    assert summary["held"] == 1
    assert summary["checked"] == 1
    assert summary["errors"] == 0
    assert official_client.status_calls == 0
    assert official_client.budget_calls == 0
    assert client_builds == []
    assert db.rollbacks == 1
