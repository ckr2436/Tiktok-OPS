from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services import gmvmax_smart_guard as smart_guard
from app.services.gmvmax_hermes_context import summarize_guard_events


class _MappingsResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _PreviousMetricsDb:
    def __init__(self, row):
        self._row = row

    def execute(self, *_args, **_kwargs):
        return _MappingsResult(self._row)


def test_minor_cost_correction_does_not_block_guard(monkeypatch):
    monkeypatch.setattr(
        smart_guard,
        "_campaign_report_date_range",
        lambda _db, _campaign: (date(2026, 7, 12), date(2026, 7, 12)),
    )
    db = _PreviousMetricsDb(
        {
            "report_start_date": date(2026, 7, 12),
            "report_end_date": date(2026, 7, 12),
            "latest_cost_cents": 1597,
            "latest_gross_revenue_cents": 0,
            "latest_orders": 0,
            "last_report_at": "2026-07-12 11:47:56",
        }
    )
    campaign = SimpleNamespace(
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
    )
    metrics = smart_guard.RealtimeMetrics(
        cost_cents=1593,
        gross_revenue_cents=0,
        orders=0,
        row_count=1,
    )

    quality = smart_guard._assess_realtime_metrics_quality(
        db,
        strategy=SimpleNamespace(),
        campaign=campaign,
        metrics=metrics,
    )

    assert quality["valid"] is True
    assert quality["reason"] == "minor_cost_correction_accepted"
    assert quality["correction_cents"] == 4


def test_recent_cost_lead_is_warning_but_attribution_lead_is_conflict():
    metrics = smart_guard.RealtimeMetrics(
        cost_cents=1000,
        gross_revenue_cents=0,
        orders=0,
    )
    product_stats = {
        "source_age_seconds": 30,
        "current_campaign": {"gross_revenue_cents": 0, "orders": 0},
    }
    recent_stats = {
        "source_age_seconds": 30,
        "reliable_group_count": 1,
        "active_campaign": {"cost_cents": 1300, "gross_revenue_cents": 0, "orders": 0},
    }

    cost_only = smart_guard._decision_consistency_snapshot(
        guard={},
        metrics=metrics,
        product_stats=product_stats,
        recent_stats=recent_stats,
    )
    assert cost_only["valid"] is True
    assert cost_only["state"] == "degraded"
    assert "recent_cost_ahead_of_campaign_report" in cost_only["warnings"]

    recent_stats["active_campaign"]["orders"] = 1
    attribution_ahead = smart_guard._decision_consistency_snapshot(
        guard={},
        metrics=metrics,
        product_stats=product_stats,
        recent_stats=recent_stats,
    )
    assert attribution_ahead["valid"] is False
    assert "recent_orders_ahead_of_campaign_report" in attribution_ahead["conflicts"]


def test_guard_rollup_keeps_latest_snapshot_instead_of_adding_cumulative_metrics():
    summary = summarize_guard_events(
        [
            {
                "event_type": "SMART_GUARD",
                "action": "HOLD",
                "result": "SKIPPED",
                "reason": "data_quality",
                "cost": 10.0,
                "gmv": 5.0,
                "orders": 1,
                "created_at": "2026-07-12 10:00:00",
            },
            {
                "event_type": "SMART_GUARD",
                "action": "HOLD",
                "result": "SKIPPED",
                "reason": "data_quality",
                "cost": 12.0,
                "gmv": 5.0,
                "orders": 1,
                "created_at": "2026-07-12 10:03:00",
            },
        ]
    )

    group = summary["groups"][0]
    assert group["count"] == 2
    assert group["latest_snapshot"] == {"cost": 12.0, "gmv": 5.0, "orders": 1}
    assert "cost" not in group


def test_active_campaign_discards_stale_pause_deadline():
    stale_deadline = "2026-07-12T05:59:58+00:00"

    assert (
        smart_guard._effective_paused_until(
            active=True,
            paused_until=stale_deadline,
        )
        is None
    )
    assert (
        smart_guard._effective_paused_until(
            active=False,
            paused_until=stale_deadline,
        )
        == stale_deadline
    )
