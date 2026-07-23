from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.celery_app  # noqa: F401 - initialize the application's task import order
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignCreateBody,
    GMVMaxCreativeStatusUpdateBody,
    GMVMaxCreativeStatusUpdateItem,
)
from app.services import gmvmax_creative_guard as guard


class _FakeMutation:
    global_fencing_token = 1

    def assert_current(self, _db) -> None:
        return None

    def commit(self, db) -> None:
        commit = getattr(db, "commit", None)
        if callable(commit):
            commit()
        else:
            flush = getattr(db, "flush", None)
            if callable(flush):
                flush()


@contextmanager
def _fake_mutation_lease(*_args, **_kwargs):
    yield _FakeMutation()


def _scope(*, config: dict | None = None) -> guard.CampaignScope:
    return guard.CampaignScope(
        strategy_id=9,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=10_000,
        roas_bid=Decimal("1.5"),
        config=config or {},
        monitor_state={},
        smart_guard_state={},
    )


def test_official_gmvmax_batch_limits_are_enforced_by_request_models() -> None:
    common = {
        "store_id": "store-1",
        "shopping_ads_type": "PRODUCT",
        "optimization_goal": "VALUE",
        "campaign_name": "Campaign",
    }
    with pytest.raises(ValidationError):
        GMVMaxCampaignCreateBody(
            **common,
            item_group_ids=[f"product-{index}" for index in range(51)],
        )
    with pytest.raises(ValidationError):
        GMVMaxCreativeStatusUpdateBody(
            campaign_id="campaign-1",
            action="REMOVE",
            item_list=[
                GMVMaxCreativeStatusUpdateItem(item_id=f"creative-{index}")
                for index in range(401)
            ],
        )


def test_campaign_activity_uses_time_cutoff_beyond_48_snapshots(monkeypatch) -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    latest_at = now.replace(tzinfo=None)
    baseline_at = latest_at - timedelta(minutes=600)

    class _Result:
        def __init__(self, value):
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.value

    class _Db:
        def __init__(self):
            self.values = [
                {"created_at": latest_at - timedelta(days=1)},
                {
                    "hourly_rows": 1,
                    "hourly_cost_cents": 1000,
                    "official_daily_rows": 0,
                    "creative_rows": 0,
                    "latest_metric_at": latest_at,
                    "recent_snapshot_count": 60,
                },
                {"snapshot_at": latest_at, "cost_cents": 1000, "orders": 4},
                {"snapshot_at": baseline_at, "cost_cents": 200, "orders": 1},
            ]
            self.statements: list[str] = []
            self.parameters: list[dict] = []

        def execute(self, statement, parameters):
            self.statements.append(" ".join(str(statement).split()))
            self.parameters.append(dict(parameters))
            return _Result(self.values.pop(0))

    db = _Db()
    monkeypatch.setattr(guard, "_advertiser_today", lambda *_args: now.date())

    activity = guard._load_campaign_activity(
        db,
        _scope(config={"no_spend_reset": {"low_spend_window_minutes": 600}}),
        now=now,
    )

    assert activity.low_spend_delta_cents == 800
    assert activity.low_spend_order_delta == 3
    assert "snapshot_at <= :cutoff" in db.statements[3]
    assert db.parameters[3]["cutoff"] == baseline_at
    assert all(
        "stat_time_day=:today" in statement
        for statement in db.statements[1:]
    )
    assert all(
        parameters.get("today") == now.date()
        for parameters in db.parameters[1:]
    )
    assert all("limit 48" not in statement.lower() for statement in db.statements)


def test_clone_refuses_partial_rebuild_above_official_product_limit() -> None:
    class _Result:
        def __init__(self, *, row=None, values=None):
            self.row = row
            self.values = values or []

        def mappings(self):
            return self

        def first(self):
            return self.row

        def scalars(self):
            return self

        def all(self):
            return self.values

    class _Db:
        def __init__(self):
            self.calls = 0
            self.statements: list[str] = []

        def execute(self, statement, _parameters):
            self.calls += 1
            self.statements.append(str(statement))
            if self.calls == 1:
                return _Result(
                    row={
                        "detail_raw_json": {},
                        "campaign_name": "Campaign",
                        "store_id": "store-1",
                        "shopping_ads_type": "PRODUCT",
                        "optimization_goal": "VALUE",
                        "deep_bid_type": None,
                        "product_specific_type": "CUSTOMIZED_PRODUCTS",
                        "budget_cents": 10_000,
                        "roas_bid": Decimal("1.5"),
                        "schedule_type": "SCHEDULE_FROM_NOW",
                    }
                )
            return _Result(values=[f"product-{index}" for index in range(51)])

    with pytest.raises(guard.CreativeGuardAutomationHold, match="50-SPU"):
        guard._clone_campaign_body(_Db(), _scope())


def test_clone_prefers_normalized_current_products_over_raw_detail() -> None:
    class _Result:
        def __init__(self, *, row=None, values=None):
            self.row = row
            self.values = values or []

        def mappings(self):
            return self

        def first(self):
            return self.row

        def scalars(self):
            return self

        def all(self):
            return self.values

    class _Db:
        def __init__(self):
            self.calls = 0
            self.statements: list[str] = []

        def execute(self, statement, _parameters):
            self.calls += 1
            self.statements.append(str(statement))
            if self.calls == 1:
                return _Result(
                    row={
                        "detail_raw_json": {
                            "item_group_ids": ["stale-product"],
                            "product_specific_type": "CUSTOMIZED_PRODUCTS",
                        },
                        "campaign_name": "Campaign",
                        "store_id": "store-1",
                        "shopping_ads_type": "PRODUCT",
                        "optimization_goal": "VALUE",
                        "deep_bid_type": None,
                        "product_specific_type": "CUSTOMIZED_PRODUCTS",
                        "budget_cents": 10_000,
                        "roas_bid": Decimal("1.5"),
                        "schedule_type": "SCHEDULE_FROM_NOW",
                    }
                )
            return _Result(values=["current-product"])

    body = guard._clone_campaign_body(_Db(), _scope())

    assert body.item_group_ids == ["current-product"]


def test_clone_never_rebuilds_custom_products_from_historical_metrics() -> None:
    class _Result:
        def __init__(self, *, row=None):
            self.row = row

        def mappings(self):
            return self

        def first(self):
            return self.row

        def scalars(self):
            return self

        def all(self):
            return []

    class _Db:
        def __init__(self):
            self.calls = 0
            self.statements: list[str] = []

        def execute(self, statement, _parameters):
            self.calls += 1
            self.statements.append(str(statement))
            if self.calls == 1:
                return _Result(
                    row={
                        "detail_raw_json": {},
                        "campaign_name": "Campaign",
                        "store_id": "store-1",
                        "shopping_ads_type": "PRODUCT",
                        "optimization_goal": "VALUE",
                        "deep_bid_type": None,
                        "product_specific_type": "CUSTOMIZED_PRODUCTS",
                        "budget_cents": 10_000,
                        "roas_bid": Decimal("1.5"),
                        "schedule_type": "SCHEDULE_FROM_NOW",
                    }
                )
            return _Result()

    db = _Db()
    with pytest.raises(guard.CreativeGuardAutomationHold, match="historical metrics"):
        guard._clone_campaign_body(db, _scope())

    assert all(
        "gmvmax_product_creative_metrics_daily" not in statement
        for statement in db.statements
    )


def test_rebuild_preflight_failure_happens_before_old_campaign_pause(monkeypatch) -> None:
    class _Client:
        def __init__(self):
            self.status_requests = []
            self.closed = False

        async def campaign_status_update(self, request):
            self.status_requests.append(request)

        async def aclose(self):
            self.closed = True

    client = _Client()
    monkeypatch.setattr(
        guard,
        "_assert_creative_guard_mutation_allowed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        guard,
        "_load_rebuild_intent",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)

    async def _fail_preflight(*_args, **_kwargs):
        raise guard.CreativeGuardAutomationHold("50-SPU preflight")

    monkeypatch.setattr(guard, "_prepare_recreated_campaign_body", _fail_preflight)
    metric = guard.CreativeMetric(
        creative_id="-1",
        item_group_id="product-1",
        status="DELIVERING",
        cost_cents=0,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=0,
        product_clicks=0,
        ad_click_rate=None,
        product_click_rate=None,
        ad_conversion_rate=None,
        roi=None,
    )

    with pytest.raises(guard.CreativeGuardAutomationHold, match="50-SPU"):
        asyncio.run(
            guard._reset_campaign_for_product_card(
                object(),
                _scope(
                    config={
                        "product_card_reset": {
                            "enabled": True,
                            "recreate": True,
                        }
                    }
                ),
                metric,
                {"action": "RESET_CAMPAIGN", "reason": "test"},
            )
        )

    assert client.status_requests == []
    assert client.closed is True


def test_historical_exclusions_are_sent_in_all_official_batches(monkeypatch) -> None:
    creatives = [
        (f"creative-{index}", f"product-{index % 3}")
        for index in range(801)
    ] + [("creative-0", "product-extra")]

    class _Response:
        def __init__(self, batch: int):
            self.batch = batch

        def model_dump(self, **_kwargs):
            return {"batch": self.batch}

    class _Client:
        def __init__(self):
            self.batch_sizes: list[int] = []
            self.batches: list[list[GMVMaxCreativeStatusUpdateItem]] = []
            self.closed = False

        async def gmv_max_creative_status_update(self, request):
            self.batch_sizes.append(len(request.body.item_list))
            self.batches.append(list(request.body.item_list))
            return _Response(len(self.batch_sizes))

        async def aclose(self):
            self.closed = True

    class _Db:
        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement, _parameters):
            self.statements.append(str(statement))
            return None

    client = _Client()
    db = _Db()
    monkeypatch.setattr(
        guard,
        "_historical_removed_creatives_for_scope",
        lambda *_args: creatives,
    )
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: client,
    )

    result = asyncio.run(
        guard._exclude_historical_removed_creatives(
            db,
            _scope(),
            new_campaign_id="campaign-2",
        )
    )

    assert result["excluded"] == 801
    assert client.batch_sizes == [400, 400, 1]
    assert client.batches[0][0].spu_id_list == ["product-0", "product-extra"]
    assert client.closed is True


def test_historical_exclusion_batch_failure_is_explicit_and_later_batches_continue(
    monkeypatch,
) -> None:
    creatives = [(f"creative-{index}", "product-1") for index in range(801)]

    class _Response:
        def model_dump(self, **_kwargs):
            return {"ok": True}

    class _Client:
        def __init__(self):
            self.calls = 0

        async def gmv_max_creative_status_update(self, _request):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("batch two failed")
            return _Response()

        async def aclose(self):
            return None

    class _Db:
        def execute(self, _statement, _parameters):
            return None

    client = _Client()
    monkeypatch.setattr(
        guard,
        "_historical_removed_creatives_for_scope",
        lambda *_args: creatives,
    )
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)

    result = asyncio.run(
        guard._exclude_historical_removed_creatives(
            _Db(),
            _scope(),
            new_campaign_id="campaign-2",
        )
    )

    assert client.calls == 3
    assert result["requested"] == 801
    assert result["excluded"] == 401
    assert "failed" in result["error"]


def test_duplicate_pause_failure_does_not_starve_later_campaigns(monkeypatch) -> None:
    class _Mappings:
        def all(self):
            return [
                {"campaign_id": "campaign-a"},
                {"campaign_id": "campaign-b"},
            ]

    class _Result:
        def mappings(self):
            return _Mappings()

    class _Db:
        def __init__(self):
            self.statements: list[str] = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, statement, _parameters):
            self.statements.append(" ".join(str(statement).split()))
            return _Result()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def flush(self):
            return None

    class _Response:
        def model_dump(self, **_kwargs):
            return {"ok": True}

    class _Client:
        def __init__(self):
            self.calls: list[str] = []

        async def campaign_status_update(self, request):
            campaign_id = request.campaign_ids[0]
            self.calls.append(campaign_id)
            if campaign_id == "campaign-a":
                raise RuntimeError("poison campaign")
            return _Response()

        async def aclose(self):
            return None

    client = _Client()
    db = _Db()
    monkeypatch.setattr(guard, "_campaign_item_group_ids", lambda *_args: ["product-1"])
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        guard,
        "_assert_creative_guard_mutation_allowed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)

    paused = asyncio.run(guard._pause_unmanaged_duplicate_campaigns(db, _scope()))

    assert paused == 1
    assert client.calls == ["campaign-a", "campaign-b"]
    assert "limit 20" not in db.statements[0].lower()


@pytest.mark.parametrize(
    "control_state",
    ["strategy_disabled", "creation_quarantine", "intent_finalizing"],
)
def test_creative_guard_stale_enabled_scope_holds_before_official_mutation(
    monkeypatch,
    control_state: str,
) -> None:
    """A scope selected while enabled must honor newer control state at write time."""

    scope = _scope(config={"max_remove_per_campaign_per_cycle": 1})
    metric = guard.CreativeMetric(
        creative_id="creative-1",
        item_group_id="product-1",
        status="DELIVERING",
        cost_cents=1_000,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=100,
        product_clicks=10,
        ad_click_rate=Decimal("0.1"),
        product_click_rate=Decimal("0.1"),
        ad_conversion_rate=Decimal("0"),
        roi=Decimal("0"),
    )

    class _Result:
        def __init__(self, row=None):
            self.row = row

        def mappings(self):
            return self

        def first(self):
            return self.row

    class _Db:
        def __init__(self):
            self.commits = 0
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
                    {
                        "enabled": control_state != "strategy_disabled",
                        "config_json": config,
                    }
                )
            if "from gmvmax_campaign_create_intents" in sql:
                return _Result(
                    {
                        "state": "FINALIZING",
                    }
                    if control_state == "intent_finalizing"
                    else None
                )
            raise AssertionError(f"unexpected SQL during mutation fence: {sql}")

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    class _OfficialClient:
        def __init__(self):
            self.creative_calls = 0

        async def gmv_max_creative_status_update(self, _request):
            self.creative_calls += 1

        async def aclose(self):
            return None

    official_client = _OfficialClient()
    client_builds = []
    active_mutation = _FakeMutation()
    db = _Db()

    async def _no_duplicate_pause(*_args, **_kwargs):
        return 0

    async def _no_retests(*_args, **_kwargs):
        return {"added": 0, "creative_ids": [], "errors": 0}

    monkeypatch.setattr(guard, "_load_scopes", lambda *_args: [scope])
    monkeypatch.setattr(
        guard,
        "_rebuild_recovery_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(guard, "_scope_due", lambda *_args: True)
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        guard,
        "_creative_guard_data_quality",
        lambda *_args, **_kwargs: {
            "campaign_valid": True,
            "creative_valid": True,
        },
    )
    monkeypatch.setattr(guard, "_pause_unmanaged_duplicate_campaigns", _no_duplicate_pause)
    monkeypatch.setattr(guard, "_decide_no_spend_reset", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(guard, "_load_creatives", lambda *_args: [metric])
    monkeypatch.setattr(
        guard,
        "_creative_monitor_interval_minutes",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(guard, "_retest_removed_creatives", _no_retests)
    monkeypatch.setattr(
        guard,
        "_metric_for_current_retest_window",
        lambda *_args: (metric, {}),
    )
    monkeypatch.setattr(
        guard,
        "_apply_learning_protection",
        lambda _db, _scope, _metric, decision, **_kwargs: dict(decision),
    )
    monkeypatch.setattr(
        guard,
        "_decide_creative",
        lambda *_args: {
            "action": "REMOVE",
            "reason": "test stale enabled scope",
        },
    )
    monkeypatch.setattr(
        guard,
        "_campaign_is_currently_enabled",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(guard, "_already_removed", lambda *_args: False)
    monkeypatch.setattr(guard, "_update_creative_guard_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(guard, "assert_gmvmax_mutation_current", lambda *_args: None)
    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _fake_mutation_lease)
    monkeypatch.setattr(
        guard,
        "active_gmvmax_mutation_lease",
        lambda *_args: active_mutation,
    )

    def _build_client(*_args, **_kwargs):
        client_builds.append(True)
        return official_client

    monkeypatch.setattr(guard, "build_ttb_gmvmax_client", _build_client)

    summary = asyncio.run(
        guard.run_creative_guard_cycle(
            db,
            now=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
        )
    )

    assert summary["held"] == 1
    assert summary["removed"] == 0
    assert summary["errors"] == 0
    assert official_client.creative_calls == 0
    assert client_builds == []
    assert db.rollbacks == 1
