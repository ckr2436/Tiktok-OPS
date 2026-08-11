from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.tasks import ttb_gmvmax_tasks


def test_creative_realtime_window_is_advertiser_today_plus_yesterday(monkeypatch):
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_advertiser_report_day",
        lambda *_args, **_kwargs: date(2026, 7, 21),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_campaign_start_at_utc",
        lambda _campaign: None,
    )

    start_day, end_day = ttb_gmvmax_tasks._campaign_sync_window(
        SimpleNamespace(),
        SimpleNamespace(campaign_id="campaign-1"),
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
    )

    assert start_day == date(2026, 7, 20)
    assert end_day == date(2026, 7, 21)
