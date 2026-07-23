from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.celery_app import HERMES_AGENT_TASK_QUEUE, beat_schedule, celery_app
from app.tasks.hermes_agent import content_factory_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    release_content_factory_stage_retry,
    self_heal_content_factory_projects,
)


def test_content_factory_tasks_are_isolated_on_hermes_queue() -> None:
    assert HERMES_AGENT_TASK_QUEUE == "gmv.tasks.hermes_agent"
    assert celery_app.conf.task_routes["hermes_content_factory.*"]["queue"] == (
        HERMES_AGENT_TASK_QUEUE
    )
    assert self_heal_content_factory_projects.queue == HERMES_AGENT_TASK_QUEUE
    assert release_content_factory_stage_retry.queue == HERMES_AGENT_TASK_QUEUE


def test_content_factory_self_heal_beat_entry_uses_hermes_queue() -> None:
    entry = beat_schedule["hermes_content_factory_self_heal"]
    assert entry["task"] == "hermes_content_factory.self_heal"
    assert entry["options"]["queue"] == HERMES_AGENT_TASK_QUEUE


def test_self_heal_never_launches_browser_probe_for_api_stage(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 13, 0, 0)
    stage = SimpleNamespace(
        status="running",
        stage="DIRECTOR",
        started_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
        created_at=now - timedelta(minutes=30),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: "toapis:text",
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_page_state",
        lambda: (_ for _ in ()).throw(
            AssertionError("API self-heal must not start agent-browser"),
        ),
    )

    assert content_factory_tasks._running_stage_browser_idle(
        object(),
        SimpleNamespace(),
        stage,
        {"execution_backend": "api"},
        now=now,
    ) == (False, None)


def test_self_heal_browser_probe_closes_local_daemon(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 13, 0, 0)
    stage = SimpleNamespace(
        status="running",
        stage="VISUAL_PREVIEW",
        started_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
        created_at=now - timedelta(minutes=30),
    )
    closed = []
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_browser_bridge_fresh_for_stage",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_browser_cdp_reachable_for_stage",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_bridge_cdp_url_for_stage",
        lambda *_args: "http://127.0.0.1:9326",
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_page_state",
        lambda: {"url": "https://chatgpt.com/", "busy": False},
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_attachment_upload_state",
        lambda: {},
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_composer_text_length",
        lambda: 0,
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "close_agent_browser_session_best_effort",
        lambda session: closed.append(session) or True,
    )

    idle, reason = content_factory_tasks._running_stage_browser_idle(
        object(),
        SimpleNamespace(),
        stage,
        {"execution_backend": "browser"},
        now=now,
    )

    assert idle is True
    assert reason and "browser idle while stage running" in reason
    assert closed == ["hermes-cdp-http---127-0-0-1-9326"]


def test_video_wait_heartbeat_uses_stage_database_clock(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 20, 13, 40, 0)
    project = SimpleNamespace(id=38, state_json={})
    commits = []
    published = []
    monkeypatch.setattr(content_factory_tasks, "_stage_now", lambda: fixed_now)
    monkeypatch.setattr(
        content_factory_tasks.wait_for_content_factory_videos,
        "apply_async",
        lambda **kwargs: published.append(kwargs) or SimpleNamespace(id="wait-123"),
    )

    task_id = content_factory_tasks._schedule_video_wait(
        SimpleNamespace(commit=lambda: commits.append(True)),
        project,
        countdown=20,
        reason="test local-clock heartbeat",
    )

    assert task_id == "wait-123"
    assert project.state_json["ai_video_wait_heartbeat_at"] == fixed_now.isoformat()
    assert project.state_json["ai_video_wait_task_id"] == "wait-123"
    assert commits == [True]
    assert published[0]["queue"] == HERMES_AGENT_TASK_QUEUE


def test_terminal_failed_project_does_not_recreate_global_video_waiter() -> None:
    now = datetime(2026, 7, 20, 14, 0, 0)
    stale = now - timedelta(minutes=10)

    assert content_factory_tasks._should_recover_global_video_waiter(
        SimpleNamespace(status="failed"),
        {"ai_video_terminal_failure": "provider rejected segment"},
        [2353, 2354],
        stale,
        now=now,
    ) is False
    assert content_factory_tasks._should_recover_global_video_waiter(
        SimpleNamespace(status="running"),
        {},
        [2614, 2615],
        stale,
        now=now,
    ) is True


def test_completed_historical_video_ids_do_not_recreate_waiter() -> None:
    class FakeQuery:
        def filter(self, *_args):
            return self

        def all(self):
            return [(2618, "success"), (2619, "failed"), (2620, "in_progress")]

    fake_db = SimpleNamespace(query=lambda *_args: FakeQuery())
    project = SimpleNamespace(workspace_id=3, user_id=6)

    assert content_factory_tasks._active_video_task_ids_for_waiter(
        fake_db,
        project,
        [2618, 2619, 2620, 2621],
    ) == [2620, 2621]
