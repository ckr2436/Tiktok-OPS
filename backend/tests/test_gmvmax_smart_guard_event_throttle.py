from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import gmvmax_smart_guard
from app.services.gmvmax_smart_guard import (
    CatalogCampaign,
    RealtimeMetrics,
    _insert_event,
)


class _MappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _EventDb:
    def __init__(self) -> None:
        self.latest = None
        self.insert_count = 0

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).lower().split())
        params = dict(params or {})
        if sql.startswith("select") and "from gmv_campaign_guard_events" in sql:
            row = self.latest
            if row is not None:
                same_key = all(
                    str(row.get(key)) == str(params.get(key))
                    for key in ("action", "reason", "result")
                )
                if (
                    not same_key
                    or row["created_at"] < params["heartbeat_cutoff"]
                ):
                    row = None
            return _MappingResult(row)
        if sql.startswith("insert into gmv_campaign_guard_events"):
            self.insert_count += 1
            self.latest = {
                "action": params["action"],
                "reason": params["reason"],
                "result": params["result"],
                "cost_cents": params["cost_cents"],
                "gross_revenue_cents": params["gross_revenue_cents"],
                "orders": params["orders"],
                "roi": params["roi"],
                "request_json": json.loads(params["request_json"]),
                "response_json": json.loads(params["response_json"]),
                "error_message": params["error_message"],
                "created_at": params["created_at"],
            }
            return SimpleNamespace()
        raise AssertionError(sql)


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


def test_unchanged_hold_event_is_throttled_but_gets_ten_minute_heartbeat(
    monkeypatch,
):
    db = _EventDb()
    clock = {"now": datetime(2026, 7, 17, 4, 34, tzinfo=timezone.utc)}
    monkeypatch.setattr(gmvmax_smart_guard, "_utcnow", lambda: clock["now"])
    metrics = RealtimeMetrics(
        cost_cents=1234,
        gross_revenue_cents=5678,
        orders=2,
        roi=None,
    )

    def write() -> bool:
        return _insert_event(
            db,
            strategy=SimpleNamespace(id=9),
            campaign=_campaign(),
            metrics=metrics,
            action="HOLD",
            reason="data_quality:source_conflict",
            result="SKIPPED",
            response_json={
                "data_quality": {
                    "state": "conflict",
                    "regressed_fields": ["cost_cents"],
                    "fetched_at": clock["now"].isoformat(),
                }
            },
            write_learning_sample=False,
        )

    assert write() is True
    clock["now"] += timedelta(minutes=1)
    assert write() is False
    assert db.insert_count == 1
    clock["now"] += timedelta(minutes=10)
    assert write() is True
    assert db.insert_count == 2


def test_invalid_data_is_still_evaluated_every_minute(monkeypatch):
    strategy = SimpleNamespace(
        id=9,
        workspace_id=7,
        auth_id=11,
        campaign_id="campaign-1",
        config_json={},
    )
    campaign = _campaign()
    db = MagicMock()
    fetch = AsyncMock(
        return_value=RealtimeMetrics(
            row_count=1,
            raw={},
            fetched_at=datetime(2026, 7, 17, 4, 34, tzinfo=timezone.utc),
        )
    )
    insert = MagicMock(return_value=False)
    state_updates: list[dict] = []

    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_enabled_strategies",
        lambda *_: [strategy],
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_runtime_state",
        lambda *_, **__: {},
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_strategy_due", lambda *_: True)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_catalog_campaign",
        lambda *_: campaign,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "is_manual_pause_override_active",
        lambda *_, **__: False,
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_fetch_today_metrics", fetch)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_assess_realtime_metrics_quality",
        lambda *_, **__: {
            "valid": False,
            "state": "conflict",
            "reason": "source_conflict",
        },
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_insert_event", insert)
    monkeypatch.setattr(gmvmax_smart_guard, "_smart_guard_state", lambda *_: {})
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_update_strategy_state",
        lambda *_, **kwargs: state_updates.append(kwargs),
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_persist_runtime_state", lambda *_, **__: None)
    monkeypatch.setattr(gmvmax_smart_guard, "_clear_legacy_runtime_config", lambda *_: None)

    first = asyncio.run(
        gmvmax_smart_guard.run_smart_guard_cycle(
            db,
            now=datetime(2026, 7, 17, 4, 34, tzinfo=timezone.utc),
        )
    )
    second = asyncio.run(
        gmvmax_smart_guard.run_smart_guard_cycle(
            db,
            now=datetime(2026, 7, 17, 4, 35, tzinfo=timezone.utc),
        )
    )

    assert fetch.await_count == 2
    assert insert.call_count == 2
    assert first["checked"] == second["checked"] == 1
    assert all(update["monitor_interval_minutes"] == 1 for update in state_updates)


def test_manual_pause_override_keeps_one_minute_guard_cadence(monkeypatch):
    strategy = SimpleNamespace(
        id=9,
        workspace_id=7,
        auth_id=11,
        campaign_id="campaign-1",
        config_json={},
    )
    db = MagicMock()
    state_updates: list[dict] = []
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_enabled_strategies",
        lambda *_: [strategy],
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_runtime_state",
        lambda *_, **__: {},
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_strategy_due", lambda *_: True)
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_load_catalog_campaign",
        lambda *_: _campaign(),
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "is_manual_pause_override_active",
        lambda *_, **__: True,
    )
    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_update_strategy_state",
        lambda *_, **kwargs: state_updates.append(kwargs),
    )
    monkeypatch.setattr(gmvmax_smart_guard, "_persist_runtime_state", lambda *_, **__: None)
    monkeypatch.setattr(gmvmax_smart_guard, "_clear_legacy_runtime_config", lambda *_: None)

    result = asyncio.run(
        gmvmax_smart_guard.run_smart_guard_cycle(
            db,
            now=datetime(2026, 7, 17, 4, 34, tzinfo=timezone.utc),
        )
    )

    assert result["checked"] == 1
    assert result["manual_override_holds"] == 1
    assert state_updates[0]["monitor_interval_minutes"] == 1
    assert state_updates[0]["decision"]["monitor_interval_minutes"] == 1
