import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.hermes_agent import content_factory
from app.data.models.hermes_agent import HermesBrowserBridge
from app.data.models.hermes_agent import (
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.data.models.kie_api import KieApiKey, KieTask
from app.core.errors import APIError
from app.services.hermes_agent.content_factory import (
    _authorize_bridge_agent_key,
    _agent_target_slot_count,
    _bridge_is_api_video_dormant,
    _bridge_base_device_id,
    _bridge_device_bound,
    _recover_degraded_bridge_if_reachable,
    _effective_browser_device_id,
    _new_agent_slot,
    _recent_project_slot_request,
    _request_locked_agent_slot_restart,
    _retire_dead_agent_rows,
    hibernate_project_browser_slot_for_api_video,
    _lock_project_for_operator_control,
    reconcile_bridge_agent,
    reconcile_bridge_project_leases,
    bridge_agent_inbox_manifest,
    build_bridge_agent_executable,
    ensure_bridge_agent_file_access,
    queue_stage,
    restart_project,
    resume_project,
    verify_bridge_agent_token,
)
from app.features.tenants.hermes_agent.schemas import ContentFactoryBridgeAgentSlotStatus


def test_bridge_slot_is_grouped_under_agent_device():
    bridge = HermesBrowserBridge(
        device_id="device-a::slot:2",
        meta_json={"agent_device_id": "device-a", "account_device_bound": True},
    )

    assert _bridge_base_device_id(bridge) == "device-a"
    assert _bridge_device_bound(bridge) is True


def test_operator_control_reloads_and_locks_project_row():
    project = SimpleNamespace(id=168)
    locked_project = SimpleNamespace(id=168)
    events: list[str] = []

    class Query:
        def filter(self, *_args):
            events.append("filter")
            return self

        def populate_existing(self):
            events.append("populate_existing")
            return self

        def with_for_update(self):
            events.append("with_for_update")
            return self

        def one_or_none(self):
            events.append("one_or_none")
            return locked_project

    db = SimpleNamespace(query=lambda _entity: Query())

    result = _lock_project_for_operator_control(db, project)

    assert result is locked_project
    assert events == [
        "filter",
        "populate_existing",
        "with_for_update",
        "one_or_none",
    ]


def test_bridge_authorized_key_uses_parent_owned_session_guard(monkeypatch, tmp_path):
    authorized_keys = tmp_path / ".ssh" / "authorized_keys"
    monkeypatch.setattr(content_factory, "BRIDGE_AGENT_AUTHORIZED_KEYS", authorized_keys)
    monkeypatch.setattr(
        content_factory,
        "BRIDGE_AGENT_SESSION_GUARD",
        content_factory.Path("/opt/gmv/bin/hermes-bridge-session-guard"),
    )

    _authorize_bridge_agent_key(
        public_key="ssh-ed25519 " + "A" * 44,
        device_id="device-a",
        user_id=101,
    )

    text = authorized_keys.read_text(encoding="utf-8")
    assert 'command="exec /opt/gmv/bin/hermes-bridge-session-guard"' in text
    assert 'restrict,port-forwarding,permitlisten="127.0.0.1:*"' in text
    assert "sleep infinity" not in text
    assert authorized_keys.stat().st_mode & 0o777 == 0o600


def test_bridge_authorized_key_atomically_upgrades_legacy_rule(monkeypatch, tmp_path):
    authorized_keys = tmp_path / ".ssh" / "authorized_keys"
    authorized_keys.parent.mkdir()
    normalized = "ssh-ed25519 " + "B" * 44
    unrelated = "ssh-ed25519 " + "C" * 44 + " unrelated"
    authorized_keys.write_text(
        f'command="/usr/bin/sleep infinity",restrict,port-forwarding {normalized} old\n'
        f"{unrelated}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(content_factory, "BRIDGE_AGENT_AUTHORIZED_KEYS", authorized_keys)
    monkeypatch.setattr(
        content_factory,
        "BRIDGE_AGENT_SESSION_GUARD",
        content_factory.Path("/opt/gmv/bin/hermes-bridge-session-guard"),
    )

    for _ in range(2):
        _authorize_bridge_agent_key(
            public_key=normalized,
            device_id="device-b",
            user_id=102,
        )

    text = authorized_keys.read_text(encoding="utf-8")
    assert text.count(normalized) == 1
    assert text.count('command="exec /opt/gmv/bin/hermes-bridge-session-guard"') == 1
    assert "sleep infinity" not in text
    assert unrelated in text


def test_sticky_slot_recovers_after_heartbeat_and_cdp_return(monkeypatch):
    now = datetime.now()
    bridge = HermesBrowserBridge(
        status="active",
        last_seen_at=now,
        browser="Chrome/old",
        cdp_url="http://127.0.0.1:9373",
        meta_json={
            "last_degraded_at": (now - timedelta(minutes=2)).isoformat(),
            "last_degraded_reason": "temporary tunnel failure",
            "last_degraded_project_id": 160,
        },
    )
    monkeypatch.setattr(
        content_factory,
        "_probe_bridge",
        lambda _bridge: (True, "Chrome/recovered", None),
    )

    assert _recover_degraded_bridge_if_reachable(bridge, now=now) is True
    assert bridge.browser == "Chrome/recovered"
    assert "last_degraded_at" not in bridge.meta_json
    assert bridge.meta_json["last_recovered_at"]


def test_sticky_slot_does_not_probe_before_degraded_backoff(monkeypatch):
    now = datetime.now()
    bridge = HermesBrowserBridge(
        status="active",
        last_seen_at=now,
        cdp_url="http://127.0.0.1:9373",
        meta_json={"last_degraded_at": now.isoformat()},
    )
    probes = []
    monkeypatch.setattr(
        content_factory,
        "_probe_bridge",
        lambda _bridge: probes.append(True) or (True, "Chrome/recovered", None),
    )

    assert _recover_degraded_bridge_if_reachable(bridge, now=now) is False
    assert probes == []


def test_one_online_bound_device_is_selected_automatically():
    devices = [
        {"device_id": "offline", "bound": True, "online": False, "selected": True},
        {"device_id": "online", "bound": True, "online": True, "selected": False},
        {"device_id": "unbound", "bound": False, "online": True, "selected": False},
    ]

    assert _effective_browser_device_id(devices) == ("online", False)


def test_multiple_online_devices_require_an_explicit_selection():
    devices = [
        {"device_id": "a", "bound": True, "online": True, "selected": False},
        {"device_id": "b", "bound": True, "online": True, "selected": False},
    ]

    assert _effective_browser_device_id(devices) == (None, True)
    devices[1]["selected"] = True
    assert _effective_browser_device_id(devices) == ("b", False)


def test_agent_slots_scale_with_real_project_demand_only():
    assert _agent_target_slot_count(
        capacity=8,
        active_project_ids={101},
        requested_project_ids=set(),
    ) == 1
    assert _agent_target_slot_count(
        capacity=8,
        active_project_ids={101},
        requested_project_ids={102},
    ) == 2
    assert _agent_target_slot_count(
        capacity=8,
        active_project_ids=set(),
        requested_project_ids=set(),
    ) == 0


def test_complete_stage_is_terminal_even_when_status_is_waiting_bridge():
    project = _content_project(key="cf_complete_waiting_bridge", user_id=101)
    project.status = "waiting_bridge"
    project.current_stage = "COMPLETE"

    assert content_factory._project_bridge_lock_terminal(project) is True


def test_waiting_bridge_without_active_stage_does_not_hold_chrome():
    project = _content_project(key="cf_idle_waiting_bridge", user_id=101)
    project.status = "waiting_bridge"
    project.current_stage = "CREATIVE"

    assert content_factory._project_bridge_lock_terminal(project, active_stage=None) is True


def test_agent_slot_creation_is_idempotent_for_the_same_device_slot(db_session):
    first = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-idempotent",
        device_name="Device",
        inbox_root="C:\\HermesInbox",
        slot_index=0,
    )
    second = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-idempotent",
        device_name="Device",
        inbox_root="C:\\HermesInbox",
        slot_index=0,
    )

    assert second.id == first.id
    assert db_session.query(HermesBrowserBridge).filter(
        HermesBrowserBridge.workspace_id == 3,
        HermesBrowserBridge.user_id == 101,
        HermesBrowserBridge.device_id == "device-idempotent::slot:0",
    ).count() == 1


def test_agent_slot_adopts_legacy_nonretired_row_in_place(db_session):
    legacy = HermesBrowserBridge(
        bridge_id="br_legacy_slot",
        workspace_id=3,
        user_id=101,
        device_id="device-legacy::slot:0",
        device_name="Legacy",
        cdp_url="http://127.0.0.1:9322",
        server_port=9322,
        inbox_root="C:\\OldInbox",
        browser="Chrome",
        status="pending",
        load_json={},
        meta_json={"connect_command": "legacy"},
    )
    db_session.add(legacy)
    db_session.flush()

    adopted = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-legacy",
        device_name="Device",
        inbox_root="C:\\HermesInbox",
        slot_index=0,
    )

    assert adopted.id == legacy.id
    assert adopted.bridge_id == "br_legacy_slot"
    assert adopted.meta_json["agent_managed"] is True
    assert adopted.meta_json["agent_device_id"] == "device-legacy"
    assert adopted.meta_json["slot_index"] == 0
    assert adopted.meta_json["local_port"] == 9222
    assert adopted.meta_json["agent_last_heartbeat_at"]
    assert adopted.last_seen_at is not None


def test_pending_agent_slot_with_fresh_heartbeat_is_not_retired(db_session):
    now = datetime.now()
    bridge = HermesBrowserBridge(
        bridge_id="br_booting_slot",
        workspace_id=3,
        user_id=101,
        device_id="device-booting::slot:0",
        device_name="Device",
        cdp_url="http://127.0.0.1:9322",
        server_port=9322,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="pending",
        last_seen_at=None,
        load_json={},
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-booting",
            "account_device_bound": True,
            "slot_index": 0,
            "agent_last_heartbeat_at": now.isoformat(),
        },
    )
    db_session.add(bridge)
    db_session.flush()

    kept = _retire_dead_agent_rows(
        db_session,
        [bridge],
        reports={bridge.bridge_id: {"bridge_id": bridge.bridge_id, "connected": False}},
        now=now,
    )

    assert kept == [bridge]
    assert bridge.status == "pending"


def test_agent_slot_reuses_retired_identity_and_profile(db_session):
    row = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-revive",
        device_name="Device",
        inbox_root="C:\\HermesInbox",
        slot_index=0,
    )
    original_id = row.id
    original_bridge_id = row.bridge_id
    row.status = "retired"
    db_session.flush()

    revived = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-revive",
        device_name="Device renamed",
        inbox_root="C:\\HermesInbox",
        slot_index=0,
    )

    assert revived.id == original_id
    assert revived.bridge_id == original_bridge_id
    assert revived.status == "pending"
    assert revived.device_name == "Device renamed"


def test_only_recent_explicit_project_requests_create_slots():
    now = datetime.now()
    project = HermesContentFactoryProject(
        status="ready",
        current_stage="CREATIVE",
        config_json={"manual_paused": False},
        state_json={
            "preferred_browser_device_id": "device-a",
            "browser_slot_requested_at": now.isoformat(),
        },
    )
    assert _recent_project_slot_request(project, device_id="device-a", now=now) is True

    project.state_json = {
        "preferred_browser_device_id": "device-a",
        "browser_slot_requested_at": (now - timedelta(minutes=16)).isoformat(),
    }
    assert _recent_project_slot_request(project, device_id="device-a", now=now) is False

    project.status = "complete"
    project.current_stage = "COMPLETE"
    project.state_json = {
        "preferred_browser_device_id": "device-a",
        "browser_slot_requested_at": now.isoformat(),
    }
    assert _recent_project_slot_request(project, device_id="device-a", now=now) is False


def test_agent_confirmed_stopped_slot_is_retired_immediately():
    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, row):
            self.added.append(row)

    db = FakeDB()
    bridge = HermesBrowserBridge(
        bridge_id="bridge-stopped",
        status="stopping",
        active_project_id=None,
        meta_json={"agent_managed": True},
    )

    kept = _retire_dead_agent_rows(db, [bridge], reports={}, now=datetime.now())

    assert kept == []
    assert bridge.status == "retired"
    assert bridge.meta_json["retired_reason"] == "agent_confirmed_slot_stopped"
    assert db.added == [bridge]


def test_requested_same_slot_restart_survives_missing_agent_report(db_session):
    now = datetime.now()
    project = _content_project(key="cf_restart_report_gap", user_id=101)
    project.status = "paused"
    project.config_json = {"manual_paused": True}
    db_session.add(project)
    db_session.flush()
    bridge = HermesBrowserBridge(
        bridge_id="br_restart_report_gap",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:2",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9324",
        server_port=9324,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="stopping",
        active_project_id=None,
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "agent_slot_mode": "active",
            "same_slot_restart_requested_at": now.isoformat(),
            "same_slot_restart_project_id": project.id,
        },
        load_json={"agent_error": "server requested same-slot restart"},
    )
    db_session.add(bridge)
    db_session.flush()
    project.state_json = {"browser_bridge_id": bridge.bridge_id}

    kept = _retire_dead_agent_rows(
        db_session,
        [bridge],
        reports={},
        now=now,
    )

    assert kept == [bridge]
    assert bridge.status == "pending"
    assert bridge.active_project_id == project.id
    assert bridge.load_json["restart_required"] is True


def test_agent_heartbeat_preserves_paused_project_same_slot_restart(
    monkeypatch,
    db_session,
):
    now = datetime.now()
    project = _content_project(key="cf_restart_heartbeat_gap", user_id=101)
    project.status = "waiting_bridge"
    project.config_json = {"manual_paused": True}
    db_session.add(project)
    db_session.flush()
    bridge = HermesBrowserBridge(
        bridge_id="br_restart_heartbeat_gap",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:2",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9324",
        server_port=9324,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="pending",
        active_project_id=project.id,
        lease_expires_at=now + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "slot_index": 2,
            "local_port": 9224,
            "agent_slot_mode": "active",
            "same_slot_restart_requested_at": now.isoformat(),
            "same_slot_restart_project_id": project.id,
        },
        load_json={"agent_error": "server requested same-slot restart", "restart_required": True},
    )
    db_session.add(bridge)
    db_session.flush()
    project.state_json = {"browser_bridge_id": bridge.bridge_id}
    monkeypatch.setattr(content_factory, "_authorize_bridge_agent_key", lambda **_kwargs: None)

    response = reconcile_bridge_agent(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-a",
        device_name="Device A",
        agent_version="2026.07.18.2",
        public_key="ssh-ed25519 " + "A" * 44,
        inbox_root="C:\\HermesInbox",
        local_capacity=3,
        reported_slots=[{"bridge_id": bridge.bridge_id, "connected": False, "mode": "active"}],
    )

    assert bridge.status == "pending"
    assert bridge.active_project_id == project.id
    assert len(response["slots"]) == 1
    assert response["slots"][0]["bridge_id"] == bridge.bridge_id
    assert response["slots"][0]["mode"] == "active"
    assert response["slots"][0]["restart_required"] is True


def test_agent_heartbeat_clears_same_slot_restart_after_cdp_returns(
    monkeypatch,
    db_session,
):
    now = datetime.now()
    project = _content_project(key="cf_restart_recovered", user_id=101)
    project.status = "waiting_bridge"
    project.config_json = {"manual_paused": True}
    db_session.add(project)
    db_session.flush()
    bridge = HermesBrowserBridge(
        bridge_id="br_restart_recovered",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:2",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9324",
        server_port=9324,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="pending",
        active_project_id=project.id,
        lease_expires_at=now + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "slot_index": 2,
            "local_port": 9224,
            "agent_slot_mode": "active",
            "same_slot_restart_requested_at": now.isoformat(),
            "same_slot_restart_project_id": project.id,
            "same_slot_restart_reason": "browser fallback required",
        },
        load_json={"agent_error": "server requested same-slot restart", "restart_required": True},
    )
    db_session.add(bridge)
    db_session.flush()
    project.state_json = {"browser_bridge_id": bridge.bridge_id}
    monkeypatch.setattr(content_factory, "_authorize_bridge_agent_key", lambda **_kwargs: None)
    monkeypatch.setattr(content_factory, "_probe_bridge", lambda _bridge: (True, "Chrome/new", None))

    response = reconcile_bridge_agent(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-a",
        device_name="Device A",
        agent_version="2026.07.18.2",
        public_key="ssh-ed25519 " + "A" * 44,
        inbox_root="C:\\HermesInbox",
        local_capacity=3,
        reported_slots=[{"bridge_id": bridge.bridge_id, "connected": True, "mode": "active"}],
    )

    assert bridge.status == "active"
    assert bridge.active_project_id == project.id
    assert "same_slot_restart_requested_at" not in bridge.meta_json
    assert bridge.meta_json["same_slot_restart_completed_at"]
    assert response["slots"][0]["restart_required"] is False


def test_failed_project_revives_its_exact_retired_slot_on_online_device(db_session):
    now = datetime.now()
    project = _content_project(key="cf_same_slot_restart", user_id=101)
    project.status = "failed"
    project.current_stage = "VIDEO_PROMPTS"
    db_session.add(project)
    db_session.flush()
    target = HermesBrowserBridge(
        bridge_id="br_target_slot",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:14",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9373",
        server_port=9373,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="retired",
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "slot_index": 14,
            "retired_reason": "agent_confirmed_slot_stopped",
        },
    )
    heartbeat = HermesBrowserBridge(
        bridge_id="br_device_heartbeat",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:16",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9375",
        server_port=9375,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        last_seen_at=now,
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "agent_last_heartbeat_at": now.isoformat(),
            "slot_index": 16,
        },
    )
    db_session.add_all([target, heartbeat])
    db_session.flush()
    project.state_json = {
        "browser_bridge_id": target.bridge_id,
        "preferred_browser_device_id": "device-a",
    }

    assert _request_locked_agent_slot_restart(
        db_session,
        project=project,
        bridge=target,
        now=now,
        reason="truncated ChatGPT response",
    ) is True
    assert target.status == "pending"
    assert target.active_project_id == project.id
    assert target.bridge_id == "br_target_slot"
    assert project.status == "waiting_bridge"
    assert project.state_json["browser_bridge_id"] == "br_target_slot"


def test_agent_download_is_scoped_to_workspace_user_and_device(tmp_path, monkeypatch):
    binary = tmp_path / "MYUPONA-HermesBridge.exe"
    binary.write_bytes(b"MZ" + b"agent-binary")
    monkeypatch.setattr(content_factory, "BRIDGE_AGENT_BINARY", binary)

    _, payload = build_bridge_agent_executable(
        workspace_id=31,
        user_id=42,
        device_id="device-one",
        device_name="Workstation",
        api_base_url="https://gmv.example.test",
    )
    config = json.loads(payload.rsplit(content_factory.BRIDGE_AGENT_CONFIG_MARKER, 1)[1])
    identity = verify_bridge_agent_token(config["token"])

    assert config["workspace_id"] == 31
    assert config["user_id"] == 42
    assert config["device_id"] == "device-one"
    assert identity["workspace_id"] == 31
    assert identity["user_id"] == 42
    assert identity["device_id"] == "device-one"


def _content_project(*, key: str, user_id: int) -> HermesContentFactoryProject:
    return HermesContentFactoryProject(
        project_key=key,
        workspace_id=3,
        user_id=user_id,
        title=key,
        product_name="Product",
        market="US",
        status="running",
        current_stage="CREATIVE",
        config_json={},
        state_json={},
    )


def test_manual_resume_resets_current_stage_self_heal_budget(db_session):
    project = _content_project(key="cf_resume", user_id=101)
    project.status = "failed"
    project.current_stage = "DIRECTOR"
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="DIRECTOR",
        attempt=1,
        status="failed",
        input_json={"self_heal_count": 5, "retry_after": "2099-01-01T00:00:00"},
    )
    db_session.add(stage)
    db_session.commit()

    resume_project(db_session, project)

    assert project.status == "ready"
    assert project.state_json["resume_generation"] == 1
    assert stage.input_json["self_heal_count"] == 0
    assert stage.input_json["manual_resume_generation"] == 1
    assert "retry_after" not in stage.input_json


def test_manual_resume_invalidates_stale_global_video_waiter(db_session):
    project = _content_project(key="cf_resume_parallel_video", user_id=101)
    project.status = "paused"
    project.current_stage = "CREATIVE"
    project.config_json = {"manual_paused": True, "video_count": 50}
    project.state_json = {
        "ai_video_task_ids": [2586, 2587, 2588, 2589],
        "ai_video_pending_task_ids": [2586, 2587, 2588, 2589],
        "ai_video_wait_task_id": "paused-waiter",
        "ai_video_wait_heartbeat_at": "2026-07-20T07:26:11",
        "video_variant_pipeline": {
            "active_index": 32,
            "submitted_indices": list(range(1, 32)),
        },
    }
    db_session.add(project)
    db_session.commit()

    resume_project(db_session, project)

    state = dict(project.state_json or {})
    assert state["ai_video_task_ids"] == [2586, 2587, 2588, 2589]
    assert state["ai_video_pending_task_ids"] == [2586, 2587, 2588, 2589]
    assert state["ai_video_wait_takeover_from"] == "paused-waiter"
    assert "ai_video_wait_task_id" not in state
    assert "ai_video_wait_heartbeat_at" not in state
    assert state["ai_video_wait_resume_requested_at"]
    assert project.status == "ready"


def test_manual_resume_does_not_wait_on_terminal_video_history(
    db_session,
    monkeypatch,
):
    project = _content_project(key="cf_resume_terminal_video_history", user_id=101)
    project.status = "paused"
    project.current_stage = "VIDEO_PROMPTS"
    project.config_json = {"manual_paused": True, "video_count": 50}
    project.state_json = {
        "ai_video_task_ids": [2586, 2587, 2588, 2589],
        "ai_video_pending_task_ids": [],
        "ai_video_wait_task_id": "stale-terminal-waiter",
        "ai_video_wait_heartbeat_at": "2026-07-20T07:26:11",
        "video_variant_pipeline": {
            "active_index": 36,
            "submitted_indices": list(range(1, 36)),
        },
    }
    db_session.add(project)
    db_session.commit()

    monkeypatch.setattr(
        content_factory,
        "_resume_production_plan_is_authoritative",
        lambda *_args, **_kwargs: True,
    )

    resume_project(db_session, project)

    state = dict(project.state_json or {})
    assert state["ai_video_task_ids"] == [2586, 2587, 2588, 2589]
    assert state["ai_video_pending_task_ids"] == []
    assert "ai_video_wait_task_id" not in state
    assert "ai_video_wait_heartbeat_at" not in state
    assert "ai_video_wait_resume_requested_at" not in state
    assert project.current_stage == "VIDEO_PROMPTS"
    assert project.status == "ready"


def test_manual_resume_requeues_waiter_for_declared_failed_segment(db_session):
    project = _content_project(key="cf_resume_failed_segment", user_id=101)
    project.status = "paused"
    project.current_stage = "CREATIVE"
    project.config_json = {"manual_paused": True, "video_count": 50}
    db_session.add(project)
    key = KieApiKey(
        name="resume-failed-segment-key",
        provider_key="bandianwa",
        api_key_ciphertext="test",
        is_active=True,
        is_default=True,
    )
    db_session.add(key)
    db_session.flush()
    task = KieTask(
        workspace_id=project.workspace_id,
        created_by_user_id=project.user_id,
        key_id=key.id,
        model="omni_flash",
        task_id="resume-failed-segment-task",
        state="timeout",
        input_json={"content_factory_project_key": project.project_key},
    )
    db_session.add(task)
    db_session.flush()
    project.state_json = {
        "ai_video_task_ids": [int(task.id)],
        "ai_video_pending_task_ids": [int(task.id)],
        "ai_video_wait_task_id": "paused-failed-waiter",
    }
    db_session.commit()

    resume_project(db_session, project)

    state = dict(project.state_json or {})
    assert state["ai_video_pending_task_ids"] == []
    assert state["ai_video_resume_failed_task_ids"] == [int(task.id)]
    assert state["ai_video_wait_takeover_from"] == "paused-failed-waiter"
    assert state["ai_video_wait_resume_requested_at"]






def test_manual_resume_reuses_downloaded_visual_api_checkpoint(
    db_session,
    tmp_path,
    monkeypatch,
):
    from app.tasks.hermes_agent import content_factory_tasks

    paid_reference = tmp_path / "visual-reference-01.png"
    paid_reference.write_bytes(b"paid-provider-result")
    project = _content_project(key="cf_visual_resume", user_id=101)
    project.status = "paused"
    project.current_stage = "VISUAL_PREVIEW"
    project.config_json = {"manual_paused": True, "video_count": 50}
    project.state_json = {
        "active_variant_index": 25,
        "video_variant_pipeline": {"active_index": 25, "target_count": 50},
    }
    db_session.add(project)
    db_session.flush()
    paused_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VISUAL_PREVIEW",
        attempt=153,
        status="paused",
        input_json={
            "variant_index": 25,
            "variant_total": 50,
            "api_route": "toapis:gpt-image-2",
            "execution_backend": "api",
            "visual_api": {
                "provider": "toapis",
                "status": "completed",
                "boards": {
                    "1": {
                        "status": "completed",
                        "task_id": "paid-task-1",
                        "output_path": str(paid_reference),
                    },
                },
            },
        },
    )
    db_session.add(paused_stage)
    db_session.commit()

    monkeypatch.setattr(
        content_factory,
        "_resume_production_plan_is_authoritative",
        lambda *_args, **_kwargs: True,
    )
    resume_project(db_session, project)
    assert project.state_json["pending_visual_api_resume"]["source_stage_id"] == (
        paused_stage.id
    )

    monkeypatch.setattr(content_factory, "has_active_key", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        content_factory_tasks.run_content_factory_stage,
        "apply_async",
        lambda **_kwargs: SimpleNamespace(id="resume-task"),
    )
    resumed_stage = queue_stage(
        db_session,
        project=project,
        user_id=project.user_id,
        instruction="Resume paid visual checkpoint.",
        queue_priority=9,
    )

    resumed_input = dict(resumed_stage.input_json or {})
    assert resumed_stage.id != paused_stage.id
    assert resumed_input["api_route"] == "toapis:gpt-image-2"
    assert resumed_input["resumed_visual_checkpoint_stage_id"] == paused_stage.id
    assert resumed_input["visual_api"]["boards"]["1"]["task_id"] == "paid-task-1"
    assert resumed_input["visual_api"]["boards"]["1"]["output_path"] == str(
        paid_reference
    )
    assert "pending_visual_api_resume" not in dict(project.state_json or {})


def test_manual_resume_recovers_paid_visuals_from_failed_api_predecessor(
    db_session,
    tmp_path,
    monkeypatch,
):
    from app.tasks.hermes_agent import content_factory_tasks

    paid_reference = tmp_path / "paid-reference-01.png"
    paid_reference.write_bytes(b"paid-provider-result")
    project = _content_project(key="cf_visual_fallback_resume", user_id=101)
    project.status = "paused"
    project.current_stage = "VISUAL_PREVIEW"
    project.config_json = {"manual_paused": True, "video_count": 50}
    project.state_json = {
        "active_variant_index": 9,
        "video_variant_pipeline": {"active_index": 9, "target_count": 50},
    }
    db_session.add(project)
    db_session.flush()
    paid_api_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VISUAL_PREVIEW",
        attempt=54,
        status="failed",
        input_json={
            "variant_index": 9,
            "api_route": "bandianwa:auto-image",
            "execution_backend": "api",
            "visual_api": {
                "provider": "bandianwa",
                "status": "failed",
                "provider_retry_generation": 4,
                "account_quota_exhausted": True,
                "account_quota_exhausted_at": "2026-07-21T11:00:00",
                "provider_failures": ["stale provider failure"],
                "boards": {
                    "1": {
                        "status": "completed",
                        "task_id": "paid-task-1",
                        "prompt_digest": "exact-prompt-digest",
                        "output_path": str(paid_reference),
                    },
                },
            },
        },
    )
    db_session.add(paid_api_stage)
    db_session.flush()
    browser_fallback_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VISUAL_PREVIEW",
        attempt=55,
        status="paused",
        input_json={
            "variant_index": 9,
            "execution_backend": "browser",
            "visual_api_force_browser_fallback": True,
        },
    )
    db_session.add(browser_fallback_stage)
    db_session.commit()

    monkeypatch.setattr(
        content_factory,
        "_resume_production_plan_is_authoritative",
        lambda *_args, **_kwargs: True,
    )
    resume_project(db_session, project)
    checkpoint = dict(project.state_json["pending_visual_api_resume"])
    assert checkpoint["source_stage_id"] == paid_api_stage.id
    assert checkpoint["recovered_across_fallback"] is True
    assert checkpoint["fallback_stage_id"] == browser_fallback_stage.id
    assert checkpoint["visual_api"]["provider_retry_generation"] == 0
    assert checkpoint["visual_api"]["account_quota_exhausted"] is False
    assert "provider_failures" not in checkpoint["visual_api"]
    assert content_factory.resume_stage_force_browser(
        dict(browser_fallback_stage.input_json or {}),
        dict(project.state_json or {}),
    ) is False

    monkeypatch.setattr(content_factory, "has_active_key", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        content_factory_tasks.run_content_factory_stage,
        "apply_async",
        lambda **_kwargs: SimpleNamespace(id="resume-task"),
    )
    resumed_stage = queue_stage(
        db_session,
        project=project,
        user_id=project.user_id,
        instruction="Retry only product placement and verification.",
        queue_priority=9,
    )

    resumed_input = dict(resumed_stage.input_json or {})
    assert resumed_input["resumed_visual_checkpoint_stage_id"] == paid_api_stage.id
    assert resumed_input["visual_api"]["boards"]["1"]["output_path"] == str(
        paid_reference
    )
    assert resumed_input["visual_api"]["provider_retry_generation"] == 0
    assert resumed_input["visual_api"]["account_quota_exhausted"] is False
    assert "visual_api_force_browser_fallback" not in resumed_input


def test_resume_failed_product_replan_returns_to_paid_visual_scene(
    db_session,
    tmp_path,
    monkeypatch,
):
    raw_scene = tmp_path / "paid-product-free-scene.png"
    raw_scene.write_bytes(b"paid-provider-result")
    project = _content_project(key="cf_product_surface_resume", user_id=101)
    project.status = "paused"
    project.current_stage = "PRODUCTION_PLAN"
    project.config_json = {"manual_paused": False, "video_count": 50}
    project.state_json = {
        "active_variant_index": 9,
        "video_variant_pipeline": {"active_index": 9, "target_count": 50},
    }
    db_session.add(project)
    db_session.flush()
    visual_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VISUAL_PREVIEW",
        attempt=287,
        status="failed",
        input_json={
            "variant_index": 9,
            "execution_backend": "api",
            "visual_api": {
                "status": "quality_replan_required",
                "boards": {
                    "5": {
                        "status": "failed",
                        "failure_class": "product_scene_unplaceable",
                        "generated_scene_source_path": str(raw_scene),
                    },
                },
            },
        },
    )
    db_session.add(visual_stage)
    db_session.flush()
    failed_plan = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="PRODUCTION_PLAN",
        attempt=288,
        status="failed",
        input_json={"variant_index": 9},
    )
    db_session.add(failed_plan)
    db_session.commit()
    monkeypatch.setattr(
        content_factory,
        "_resume_production_plan_is_authoritative",
        lambda *_args, **_kwargs: True,
    )
    pre_resume_checkpoint = (
        content_factory._latest_resumable_visual_api_checkpoint(
            db_session,
            project,
            None,
        )
    )
    assert pre_resume_checkpoint["source_stage_id"] == visual_stage.id

    resume_project(db_session, project)

    assert project.current_stage == "VISUAL_PREVIEW"
    assert project.status == "ready"
    checkpoint = project.state_json["pending_visual_api_resume"]
    assert checkpoint["source_stage_id"] == visual_stage.id
    assert checkpoint["visual_api"]["boards"]["5"][
        "generated_scene_source_path"
    ] == str(raw_scene)
    assert project.state_json["resume_control_reset"]["reason"] == (
        "recover_paid_product_scene_after_failed_replan"
    )




def test_browser_inbox_access_rejects_another_member_project(db_session, tmp_path, monkeypatch):
    owner_project = _content_project(key="cf_owner", user_id=101)
    db_session.add(owner_project)
    db_session.commit()
    browser_root = tmp_path / "browser_inbox"
    target = browser_root / "workspace_3" / "cf_owner" / "product.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"image")
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_root)

    assert ensure_bridge_agent_file_access(
        db_session,
        workspace_id=3,
        user_id=101,
        relative_path="cf_owner/product.png",
    ) == target
    with pytest.raises(APIError):
        ensure_bridge_agent_file_access(
            db_session,
            workspace_id=3,
            user_id=202,
            relative_path="cf_owner/product.png",
        )


def test_agent_manifest_contains_only_its_users_active_projects(db_session, tmp_path, monkeypatch):
    owner_project = _content_project(key="cf_owner", user_id=101)
    other_project = _content_project(key="cf_other", user_id=202)
    db_session.add_all([owner_project, other_project])
    db_session.commit()
    browser_root = tmp_path / "browser_inbox"
    for key in ("cf_owner", "cf_other"):
        target = browser_root / "workspace_3" / key / "product.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(key.encode())
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_root)

    manifest = bridge_agent_inbox_manifest(db_session, workspace_id=3, user_id=101)

    assert [item["path"] for item in manifest] == ["cf_owner/product.png"]


def test_agent_manifest_keeps_ready_project_waiting_for_scheduler(db_session, tmp_path, monkeypatch):
    project = _content_project(key="cf_ready", user_id=101)
    project.status = "ready"
    db_session.add(project)
    db_session.commit()
    browser_root = tmp_path / "browser_inbox"
    target = browser_root / "workspace_3" / project.project_key / "product.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"image")
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_root)

    manifest = bridge_agent_inbox_manifest(db_session, workspace_id=3, user_id=101)

    assert [item["path"] for item in manifest] == ["cf_ready/product.png"]


def test_agent_slot_heartbeat_preserves_file_sync_acknowledgements():
    payload = ContentFactoryBridgeAgentSlotStatus.model_validate({
        "bridge_id": "br_sync",
        "connected": True,
        "synced_files": [{"path": "cf_owner/product.png", "size": 5, "mtime": 123}],
        "last_sync_at": "2026-07-13T04:00:00Z",
        "sync_error": "",
    }).model_dump()

    assert payload["synced_files"] == [{"path": "cf_owner/product.png", "size": 5, "mtime": 123}]
    assert payload["last_sync_at"] == "2026-07-13T04:00:00Z"


def test_api_video_wait_hibernates_the_sticky_slot_without_releasing_it(db_session):
    project = _content_project(key="cf_api_dormant", user_id=101)
    project.current_stage = "WAITING_VIDEO_INPUT"
    project.status = "generating_video"
    db_session.add(project)
    db_session.flush()
    bridge = HermesBrowserBridge(
        bridge_id="br_api_dormant",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:3",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9325",
        server_port=9325,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        active_project_id=project.id,
        lease_expires_at=datetime.now() + timedelta(hours=1),
        meta_json={"agent_managed": True, "agent_device_id": "device-a"},
    )
    db_session.add(bridge)
    db_session.flush()

    assert hibernate_project_browser_slot_for_api_video(db_session, project=project) is True
    assert hibernate_project_browser_slot_for_api_video(db_session, project=project) is False
    assert bridge.active_project_id == project.id
    assert bridge.meta_json["agent_slot_mode"] == "dormant"
    assert _bridge_is_api_video_dormant(bridge, project) is True

    reconcile_bridge_project_leases(db_session, workspace_id=3, user_id=101)
    assert bridge.active_project_id == project.id
    assert bridge.lease_expires_at is not None


def test_parallel_api_stage_keeps_dormant_slot_asleep_during_video_generation(db_session):
    project = _content_project(key="cf_parallel_api_dormant", user_id=101)
    project.current_stage = "CREATIVE"
    project.status = "running"
    project.state_json = {"ai_video_task_ids": [2504, 2505, 2506, 2507]}
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="CREATIVE",
        attempt=1,
        status="running",
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:text",
            "variant_index": 21,
        },
    )
    bridge = HermesBrowserBridge(
        bridge_id="br_parallel_api_dormant",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:5",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9327",
        server_port=9327,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="dormant",
        active_project_id=project.id,
        lease_expires_at=datetime.now() + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "agent_slot_mode": "dormant",
        },
    )
    db_session.add_all([stage, bridge])
    db_session.commit()

    assert _bridge_is_api_video_dormant(bridge, project, active_stage=stage) is True

    reconcile_bridge_project_leases(db_session, workspace_id=3, user_id=101)

    assert project.status == "running"
    assert bridge.active_project_id == project.id
    assert bridge.meta_json["agent_slot_mode"] == "dormant"


def test_parallel_api_stage_can_rehibernate_a_browser_fallback_slot(db_session):
    project = _content_project(key="cf_parallel_api_rehibernate", user_id=101)
    project.current_stage = "CREATIVE_REVIEW"
    project.status = "queued"
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="CREATIVE_REVIEW",
        attempt=2,
        status="retrying",
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:text",
            "api_fallback_to_browser": False,
        },
    )
    bridge = HermesBrowserBridge(
        bridge_id="br_parallel_api_rehibernate",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:6",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9328",
        server_port=9328,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        active_project_id=project.id,
        lease_expires_at=datetime.now() + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
        },
    )
    db_session.add_all([stage, bridge])
    db_session.flush()

    assert hibernate_project_browser_slot_for_api_video(
        db_session,
        project=project,
        active_stage=stage,
    ) is True
    assert bridge.active_project_id == project.id
    assert bridge.meta_json["agent_slot_mode"] == "dormant"
    assert project.state_json["browser_slot_mode"] == "dormant"


def test_agent_desired_slots_keep_parallel_api_stage_dormant(monkeypatch, db_session):
    project = _content_project(key="cf_parallel_api_agent_dormant", user_id=101)
    project.current_stage = "CREATIVE"
    project.status = "running"
    project.state_json = {"ai_video_task_ids": [2504, 2505, 2506, 2507]}
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="CREATIVE",
        attempt=1,
        status="running",
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:text",
            "variant_index": 21,
        },
    )
    bridge = HermesBrowserBridge(
        bridge_id="br_parallel_api_agent_dormant",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:5",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9327",
        server_port=9327,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="dormant",
        active_project_id=project.id,
        lease_expires_at=datetime.now() + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "slot_index": 5,
            "local_port": 9227,
            "agent_slot_mode": "dormant",
        },
    )
    db_session.add_all([stage, bridge])
    db_session.commit()
    monkeypatch.setattr(content_factory, "_authorize_bridge_agent_key", lambda **_kwargs: None)

    response = reconcile_bridge_agent(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-a",
        device_name="Device A",
        agent_version="2026.07.18.2",
        public_key="ssh-ed25519 " + "A" * 44,
        inbox_root="C:\\HermesInbox",
        local_capacity=3,
        reported_slots=[
            {
                "bridge_id": bridge.bridge_id,
                "connected": False,
                "mode": "dormant",
            }
        ],
    )

    assert response["slots"][0]["bridge_id"] == bridge.bridge_id
    assert response["slots"][0]["mode"] == "dormant"
    assert bridge.active_project_id == project.id


def test_agent_heartbeat_does_not_create_chrome_for_unleased_api_stage(
    monkeypatch,
    db_session,
):
    project = _content_project(key="cf_api_stage_without_browser", user_id=101)
    project.current_stage = "CREATIVE"
    project.status = "running"
    project.state_json = {"preferred_browser_device_id": "device-a"}
    db_session.add(project)
    db_session.flush()
    db_session.add(HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="CREATIVE",
        attempt=1,
        status="running",
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:text",
            "api_fallback_to_browser": False,
        },
    ))
    db_session.commit()
    monkeypatch.setattr(content_factory, "_authorize_bridge_agent_key", lambda **_kwargs: None)

    response = reconcile_bridge_agent(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-a",
        device_name="Device A",
        agent_version="2026.07.18.2",
        public_key="ssh-ed25519 " + "A" * 44,
        inbox_root="C:\\HermesInbox",
        local_capacity=3,
        reported_slots=[],
    )

    assert response["slots"] == []
    registration = db_session.query(HermesBrowserBridge).one()
    assert registration.status == "standby"
    assert registration.active_project_id is None


def test_agent_heartbeat_acknowledges_dormant_slot_without_recovery(monkeypatch, db_session):
    project = _content_project(key="cf_dormant_heartbeat", user_id=101)
    project.current_stage = "WAITING_VIDEO_INPUT"
    project.status = "generating_video"
    db_session.add(project)
    db_session.flush()
    bridge = HermesBrowserBridge(
        bridge_id="br_dormant_heartbeat",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:4",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9326",
        server_port=9326,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        active_project_id=project.id,
        lease_expires_at=datetime.now() + timedelta(hours=1),
        meta_json={
            "agent_managed": True,
            "agent_device_id": "device-a",
            "account_device_bound": True,
            "slot_index": 4,
            "local_port": 9226,
            "agent_slot_mode": "dormant",
        },
    )
    db_session.add(bridge)
    db_session.flush()
    monkeypatch.setattr(content_factory, "_authorize_bridge_agent_key", lambda **_kwargs: None)

    response = reconcile_bridge_agent(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="device-a",
        device_name="Device A",
        agent_version="2026.07.18.1",
        public_key="ssh-ed25519 " + "A" * 44,
        inbox_root="C:\\HermesInbox",
        local_capacity=3,
        reported_slots=[{"bridge_id": bridge.bridge_id, "connected": False, "mode": "dormant"}],
    )

    assert bridge.status == "dormant"
    assert bridge.active_project_id == project.id
    assert len(response["slots"]) == 1
    assert response["slots"][0]["bridge_id"] == bridge.bridge_id
    assert response["slots"][0]["mode"] == "dormant"


def test_agent_manifest_keeps_failed_project_owned_by_sticky_slot(db_session, tmp_path, monkeypatch):
    project = _content_project(key="cf_failed_retry", user_id=101)
    project.status = "failed"
    db_session.add(project)
    db_session.flush()
    db_session.add(HermesBrowserBridge(
        bridge_id="br_failed_retry",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:1",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9323",
        server_port=9323,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        active_project_id=project.id,
        meta_json={"agent_managed": True, "agent_device_id": "device-a"},
    ))
    db_session.commit()
    browser_root = tmp_path / "browser_inbox"
    target = browser_root / "workspace_3" / project.project_key / "product.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"image")
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_root)

    manifest = bridge_agent_inbox_manifest(db_session, workspace_id=3, user_id=101)

    assert [item["path"] for item in manifest] == ["cf_failed_retry/product.png"]


def test_agent_manifest_keeps_failed_project_after_retry_releases_lease(db_session, tmp_path, monkeypatch):
    project = _content_project(key="cf_failed_sticky", user_id=101)
    project.status = "failed"
    db_session.add(project)
    db_session.flush()
    project.state_json = {"browser_bridge_id": "br_failed_sticky"}
    db_session.add(HermesBrowserBridge(
        bridge_id="br_failed_sticky",
        workspace_id=3,
        user_id=101,
        device_id="device-a::slot:2",
        device_name="Device A",
        cdp_url="http://127.0.0.1:9324",
        server_port=9324,
        inbox_root="C:\\HermesInbox",
        browser="Chrome",
        status="active",
        active_project_id=None,
        meta_json={"agent_managed": True, "agent_device_id": "device-a"},
    ))
    db_session.commit()
    browser_root = tmp_path / "browser_inbox"
    target = browser_root / "workspace_3" / project.project_key / "product.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"image")
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_root)

    manifest = bridge_agent_inbox_manifest(db_session, workspace_id=3, user_id=101)

    assert [item["path"] for item in manifest] == ["cf_failed_sticky/product.png"]
