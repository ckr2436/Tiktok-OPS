from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.services import gmvmax_hermes_daily_report as daily_report


class _Db:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_decision_retry_policy_is_bounded_and_terminal():
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    assert daily_report._decision_retry_due(
        status=None,
        attempts=0,
        last_attempt_at=None,
        now=now,
    )
    assert not daily_report._decision_retry_due(
        status="APPROVED",
        attempts=1,
        last_attempt_at=now - timedelta(days=1),
        now=now,
    )
    assert not daily_report._decision_retry_due(
        status="RETRY_PENDING",
        attempts=1,
        last_attempt_at=now - timedelta(minutes=14),
        now=now,
    )
    assert daily_report._decision_retry_due(
        status="RETRY_PENDING",
        attempts=1,
        last_attempt_at=now - timedelta(minutes=15),
        now=now,
    )
    assert not daily_report._decision_retry_due(
        status="RETRY_PENDING",
        attempts=4,
        last_attempt_at=now - timedelta(days=1),
        now=now,
    )


def test_tracked_decision_records_success(monkeypatch):
    db = _Db()
    updates = []
    monkeypatch.setattr(
        daily_report,
        "_load_report_decision_state",
        lambda *_args: {"decision_attempts": 0},
    )
    monkeypatch.setattr(
        daily_report,
        "_update_report_decision_state",
        lambda _db, **kwargs: updates.append(kwargs),
    )

    async def approve(_db, *, report_id):
        return {"report_id": report_id, "status": "approved", "approved": 2}

    monkeypatch.setattr(daily_report, "run_hermes_report_decision", approve)

    result = asyncio.run(
        daily_report._run_tracked_report_decision(db, report_id=10)
    )

    assert result["decision_status"] == "APPROVED"
    assert [row["status"] for row in updates] == ["RUNNING", "APPROVED"]
    assert db.commits == 2


def test_tracked_decision_schedules_retry_after_timeout(monkeypatch):
    db = _Db()
    updates = []
    monkeypatch.setattr(
        daily_report,
        "_load_report_decision_state",
        lambda *_args: {"decision_attempts": 0},
    )
    monkeypatch.setattr(
        daily_report,
        "_update_report_decision_state",
        lambda _db, **kwargs: updates.append(kwargs),
    )
    monkeypatch.setattr(
        daily_report,
        "ensure_hermes_daily_report_table",
        lambda *_args: None,
    )

    async def timeout(_db, *, report_id):
        raise TimeoutError(f"report {report_id} timed out")

    monkeypatch.setattr(daily_report, "run_hermes_report_decision", timeout)

    result = asyncio.run(
        daily_report._run_tracked_report_decision(db, report_id=10)
    )

    assert result["status"] == "retry_pending"
    assert [row["status"] for row in updates] == ["RUNNING", "RETRY_PENDING"]
    assert db.rollbacks == 1
