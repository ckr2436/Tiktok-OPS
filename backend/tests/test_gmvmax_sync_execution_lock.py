from __future__ import annotations

from types import SimpleNamespace

from celery.exceptions import Retry
import pytest

from app.features.tenants.ttb.gmv_max.control import GMVMaxGuardActionLease
from app.gmvmax import tasks_sync
from app.tasks import ttb_gmvmax_tasks
from app.gmvmax.services.sync_execution_lock import (
    GmvMaxAccountSyncFence,
    GmvMaxAccountSyncFenceLost,
    account_sync_lock_key,
    creative_10min_sync_fence_name,
    release_account_sync_fence,
)


def test_account_sync_lock_key_is_shared_per_workspace_auth():
    assert account_sync_lock_key(workspace_id=3, auth_id=7) == (
        "gmvmax:account-fact-sync:3:7"
    )
    assert account_sync_lock_key(workspace_id=3, auth_id=8) != (
        account_sync_lock_key(workspace_id=3, auth_id=7)
    )


def test_strategy_tick_is_coalesced_when_account_sync_is_inflight(monkeypatch):
    events: list[object] = []
    strategy = SimpleNamespace(
        id=12,
        enabled=True,
        workspace_id=3,
        auth_id=7,
        advertiser_id=None,
        store_id=None,
        level="CAMPAIGN_DAILY",
    )

    class _Repo:
        def get_by_id(self, strategy_id):
            assert strategy_id == 12
            return strategy

        def mark_success(self, *_args):
            raise AssertionError("a deferred strategy was not completed")

        def mark_error(self, *_args):
            raise AssertionError("a coalesced tick is not an error")

    class _Service:
        def sync_strategy(self, *_args):
            raise AssertionError("overlapping strategy must not execute")

    class _Lock:
        def acquire(self, **_kwargs):
            events.append("acquire")
            return False

        def release(self):
            raise AssertionError("an unacquired lock must not be released")

    monkeypatch.setattr(
        tasks_sync,
        "GmvMaxMonitoringStrategyRepository",
        _Repo,
    )
    monkeypatch.setattr(tasks_sync, "GmvMaxSyncService", _Service)
    monkeypatch.setattr(
        tasks_sync,
        "build_account_sync_lock",
        lambda **_kwargs: _Lock(),
    )
    monkeypatch.setattr(
        tasks_sync.run_gmvmax_sync_for_strategy,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(
            Retry(exc=kwargs.get("exc"), when=kwargs.get("countdown"))
        ),
    )

    with pytest.raises(Retry):
        tasks_sync.run_gmvmax_sync_for_strategy.run(12)

    assert events == ["acquire"]


def test_redelivered_strategy_uses_a_fresh_lock_owner_each_execution(monkeypatch):
    strategy = SimpleNamespace(
        id=12,
        enabled=True,
        workspace_id=3,
        auth_id=7,
        advertiser_id=None,
        store_id=None,
        level="CAMPAIGN_DAILY",
    )
    owner_tokens: list[str] = []
    nonces = iter(("delivery-a", "delivery-b"))

    class _Repo:
        def get_by_id(self, _strategy_id):
            return strategy

        def mark_success(self, *_args):
            raise AssertionError("a deferred strategy was not completed")

        def mark_error(self, *_args):
            raise AssertionError("a coalesced tick is not an error")

    class _Lock:
        def acquire(self, **_kwargs):
            return False

        def release(self):
            raise AssertionError("an unacquired lock must not be released")

    monkeypatch.setattr(tasks_sync, "GmvMaxMonitoringStrategyRepository", _Repo)
    monkeypatch.setattr(tasks_sync, "uuid4", lambda: next(nonces))
    monkeypatch.setattr(
        tasks_sync,
        "build_account_sync_lock",
        lambda **kwargs: owner_tokens.append(kwargs["owner_token"]) or _Lock(),
    )
    monkeypatch.setattr(
        tasks_sync.run_gmvmax_sync_for_strategy,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(
            Retry(exc=kwargs.get("exc"), when=kwargs.get("countdown"))
        ),
    )

    for _ in range(2):
        with pytest.raises(Retry):
            tasks_sync.run_gmvmax_sync_for_strategy.run(12)

    assert owner_tokens == [
        "strategy:12:delivery-a",
        "strategy:12:delivery-b",
    ]


def test_stale_fence_release_cannot_clear_newer_generation(db_session):
    lease = GMVMaxGuardActionLease(
        lease_name="gmvmax:account-fact-sync:3:7",
        owner_token="same-task-id",
        fencing_token=9,
    )
    db_session.add(lease)
    db_session.commit()

    stale_fence = GmvMaxAccountSyncFence(
        lease_name=lease.lease_name,
        owner_token="same-task-id",
        fencing_token=8,
        redis_lock=SimpleNamespace(),
        ttl_seconds=300,
    )

    assert release_account_sync_fence(db_session, fence=stale_fence) is False
    db_session.commit()
    db_session.refresh(lease)
    assert lease.owner_token == "same-task-id"
    assert lease.fencing_token == 9


def test_creative_10min_fence_name_is_bounded_and_exactly_scoped():
    first = creative_10min_sync_fence_name(
        workspace_id=3,
        auth_id=7,
        advertiser_id="a" * 64,
        campaign_id="c" * 64,
    )
    second = creative_10min_sync_fence_name(
        workspace_id=3,
        auth_id=7,
        advertiser_id="a" * 64,
        campaign_id=("c" * 63) + "d",
    )

    assert len(first) <= 128
    assert first != second


def test_redelivered_creative_10min_task_uses_fresh_owner_nonce(monkeypatch):
    owner_tokens: list[str] = []
    nonces = iter(("delivery-a", "delivery-b"))

    class _Lock:
        def acquire(self, **_kwargs):
            return False

        def release(self):
            raise AssertionError("an unacquired lock must not be released")

    monkeypatch.setattr(ttb_gmvmax_tasks, "uuid4", lambda: next(nonces))
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "build_creative_10min_sync_lock",
        lambda **kwargs: owner_tokens.append(kwargs["owner_token"]) or _Lock(),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks.task_gmvmax_sync_creative_metrics_10min_for_campaign,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(
            Retry(exc=kwargs.get("exc"), when=kwargs.get("countdown"))
        ),
    )

    task = ttb_gmvmax_tasks.task_gmvmax_sync_creative_metrics_10min_for_campaign
    task.push_request(id="same-task-id", retries=0)
    try:
        for _ in range(2):
            with pytest.raises(Retry):
                task.run(
                    workspace_id=3,
                    provider="tiktok-business",
                    auth_id=7,
                    advertiser_id="adv",
                    campaign_id="campaign",
                )
    finally:
        task.pop_request()

    assert owner_tokens == [
        "same-task-id:delivery-a",
        "same-task-id:delivery-b",
    ]


def test_creative_10min_lost_fence_rolls_back_without_fact_commit(monkeypatch):
    events: list[str] = []

    class _Db:
        def __init__(self, name):
            self.name = name

        def commit(self):
            events.append(f"{self.name}:commit")

        def rollback(self):
            events.append(f"{self.name}:rollback")

    class _Lock:
        owner_token = "same-task-id:delivery-a"

        def acquire(self, **_kwargs):
            events.append("lock:acquire")
            return True

        def release(self):
            events.append("lock:release")
            return True

    class _Fence:
        def assert_current(self, _db):
            events.append("fence:assert")
            raise GmvMaxAccountSyncFenceLost("superseded")

    sessions = iter((_Db("fence_db"), _Db("facts_db"), _Db("release_db")))
    lock = _Lock()
    fence = _Fence()
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "build_creative_10min_sync_lock",
        lambda **_kwargs: lock,
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "acquire_creative_10min_sync_fence",
        lambda *_args, **_kwargs: fence,
    )
    monkeypatch.setattr(ttb_gmvmax_tasks, "_db_session", lambda: next(sessions))
    monkeypatch.setattr(ttb_gmvmax_tasks, "_close_session", lambda *_args: None)
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_find_catalog_campaign_scope",
        lambda *_args, **_kwargs: SimpleNamespace(campaign_id="campaign"),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_run_with_client",
        lambda *_args, **_kwargs: events.append("facts:staged") or {"rows": 1},
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_record_creative_10min_result",
        lambda *_args, **_kwargs: events.append("result:staged") or True,
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "release_sync_fence",
        lambda *_args, **_kwargs: events.append("fence:release") or True,
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks.task_gmvmax_sync_creative_metrics_10min_for_campaign,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(
            Retry(exc=kwargs.get("exc"), when=kwargs.get("countdown"))
        ),
    )

    task = ttb_gmvmax_tasks.task_gmvmax_sync_creative_metrics_10min_for_campaign
    task.push_request(id="same-task-id", retries=0)
    try:
        with pytest.raises(Retry):
            task.run(
                workspace_id=3,
                provider="tiktok-business",
                auth_id=7,
                advertiser_id="adv",
                campaign_id="campaign",
                sync_attempt_token="claim-a",
            )
    finally:
        task.pop_request()

    assert "facts:staged" in events
    assert "result:staged" in events
    assert "fence:assert" in events
    assert "facts_db:rollback" in events
    assert "facts_db:commit" not in events
    assert events[-2:] == ["release_db:commit", "lock:release"]
