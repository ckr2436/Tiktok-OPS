from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reap_agent_browser_daemons.py"
SPEC = importlib.util.spec_from_file_location("reap_agent_browser_daemons", SCRIPT)
assert SPEC and SPEC.loader
reaper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reaper)


def test_reaper_dry_run_enumerates_every_stale_pid(monkeypatch, tmp_path):
    groups = {
        (969, "hermes-cdp-slot"): [
            {"pid": 111, "age_seconds": 4000, "home": "/opt/gmv"},
            {"pid": 222, "age_seconds": 3000, "home": "/opt/gmv"},
        ],
    }
    monkeypatch.setattr(reaper, "_daemon_groups", lambda _uid: groups)
    monkeypatch.setattr(reaper, "_activity_age", lambda *_args: 2000)

    result = reaper.reap(
        idle_seconds=900,
        all_users=True,
        dry_run=True,
        runtime_dir=tmp_path,
    )

    assert result["examined_processes"] == 2
    assert result["stopped_sessions"] == 1
    assert result["stopped_pids"] == [111, 222]


def test_reaper_preserves_recent_session(monkeypatch, tmp_path):
    groups = {
        (969, "hermes-cdp-active"): [
            {"pid": 333, "age_seconds": 4000, "home": "/opt/gmv"},
        ],
    }
    monkeypatch.setattr(reaper, "_daemon_groups", lambda _uid: groups)
    monkeypatch.setattr(reaper, "_activity_age", lambda *_args: 30)

    result = reaper.reap(
        idle_seconds=900,
        all_users=True,
        dry_run=True,
        runtime_dir=tmp_path,
    )

    assert result["stopped_pids"] == []
    assert result["recent_sessions"] == [{"uid": 969, "session": "hermes-cdp-active"}]


def test_reaper_preserves_stale_daemon_while_stage_lease_is_held(
    monkeypatch,
    tmp_path,
):
    groups = {
        (969, "hermes-cdp-generating"): [
            {"pid": 444, "age_seconds": 4000, "home": "/opt/gmv"},
        ],
    }
    monkeypatch.setattr(reaper, "_daemon_groups", lambda _uid: groups)
    monkeypatch.setattr(reaper, "_activity_age", lambda *_args: 2000)
    monkeypatch.setattr(reaper, "_try_lock", lambda *_args: None)

    result = reaper.reap(
        idle_seconds=60,
        all_users=True,
        dry_run=True,
        runtime_dir=tmp_path,
    )

    assert result["stopped_pids"] == []
    assert result["busy_sessions"] == [
        {"uid": 969, "session": "hermes-cdp-generating"},
    ]
