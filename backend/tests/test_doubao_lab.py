from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import APIError
from app.services import doubao_lab as doubao_lab_service
from app.data.models.hermes_agent import HermesBrowserBridge
from app.data.models.kie_api import KieTask
from app.services.doubao_lab import (
    cancel_doubao_lab_onboarding,
    complete_doubao_manual_verification,
    apply_doubao_manual_video_challenge_result,
    decrypt_doubao_session_context,
    doubao_slot_should_wake,
    doubao_slot_spec,
    ingest_doubao_browser_capture,
    is_doubao_lab_slot,
    list_doubao_lab_sessions,
    queue_doubao_lab_test,
    queue_doubao_capability_probe,
    reconcile_doubao_account_pool,
    rebind_doubao_account_proxy,
    record_doubao_browser_report,
    retire_doubao_account,
    start_doubao_manual_verification,
    start_doubao_lab_onboarding,
    verify_doubao_lab_session,
)
from app.services.flow_proxy_pool import create_flow_proxy
from app.services.hermes_agent.content_factory import _new_agent_slot, browser_devices
from app.services.jimeng_lab import is_external_account_slot
from app.services.doubao_provider.pool import (
    DoubaoPoolBusyError,
    _browser_lane_is_busy,
    account_dispatch_score,
    account_is_ready,
    account_is_retry_candidate,
    auth_probe_eligible,
    claim_account,
    due_auth_probe_accounts,
    due_capability_probe_accounts,
    leased_account,
    record_submit_observation,
    release_account,
    set_account_membership,
)
from app.services.doubao_provider.health import (
    AUTHENTICATED,
    AUTH_REQUIRED,
    AUTH_UNKNOWN,
    NETWORK_UNREACHABLE,
    mark_authenticated,
)
from app.services.doubao_provider import tasks as doubao_tasks
from app.services.doubao_provider.client import DoubaoProviderError
from app.services.doubao_provider.capability import (
    apply_seedance_capability_result,
    seedance_capability_state,
)
from app.tasks import doubao_lab_tasks


def test_manual_browser_generation_requires_exact_ready_bridge_report():
    lease_id = "manual-capture:mvc_exact"
    meta = {
        "doubao_pool_lease_task_id": lease_id,
        "doubao_provider_browser_task_id": lease_id,
        "doubao_browser_capture_id": "old-generation",
        "doubao_browser_status": "ready",
        "doubao_page_url": "https://www.doubao.com/chat/create-image",
    }

    assert not doubao_lab_tasks._manual_browser_generation_ready(
        meta, lease_id=lease_id
    )
    meta["doubao_browser_capture_id"] = lease_id
    assert doubao_lab_tasks._manual_browser_generation_ready(
        meta, lease_id=lease_id
    )


def test_production_browser_generation_requires_exact_ready_bridge_report():
    meta = {
        "doubao_pool_lease_task_id": 901,
        "doubao_provider_browser_task_id": 901,
        "doubao_browser_capture_id": "old-generation",
        "doubao_browser_status": "ready",
        "doubao_page_url": "https://www.doubao.com/chat/create-image",
    }

    assert not doubao_tasks._provider_browser_generation_ready(meta, task_id=901)
    meta["doubao_browser_capture_id"] = "901"
    assert doubao_tasks._provider_browser_generation_ready(meta, task_id=901)


def test_production_browser_generation_login_required_is_account_scoped():
    meta = {
        "doubao_pool_lease_task_id": 901,
        "doubao_provider_browser_task_id": 901,
        "doubao_browser_capture_id": "901",
        "doubao_browser_status": "login_required",
        "doubao_page_url": "https://www.doubao.com/chat/create-image",
    }

    error = doubao_tasks._provider_browser_generation_error(meta, task_id=901)

    assert error is not None
    assert error.code == "doubao_auth_required"


def test_production_browser_generation_ignores_stale_login_required_report():
    meta = {
        "doubao_pool_lease_task_id": 902,
        "doubao_provider_browser_task_id": 902,
        "doubao_browser_capture_id": "old-generation",
        "doubao_browser_status": "login_required",
        "doubao_page_url": "https://www.doubao.com/chat/create-image",
    }

    assert (
        doubao_tasks._provider_browser_generation_error(meta, task_id=902) is None
    )


def test_production_browser_generation_region_restriction_is_account_scoped():
    meta = {
        "doubao_pool_lease_task_id": 903,
        "doubao_provider_browser_task_id": 903,
        "doubao_browser_capture_id": "903",
        "doubao_browser_status": "login_required",
        "doubao_page_url": "https://www.doubao.com/security/doubao-region-ban",
    }

    error = doubao_tasks._provider_browser_generation_error(meta, task_id=903)

    assert error is not None
    assert error.code == "doubao_region_restricted"


def test_production_browser_generation_restores_saved_session_once(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    row.cdp_url = "http://127.0.0.1:9230"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_pool_lease_task_id": 904,
        "doubao_provider_browser_task_id": 904,
        "doubao_browser_capture_id": "904",
        "doubao_browser_status": "login_required",
        "doubao_page_url": "https://www.doubao.com/chat/create-image",
    }
    db_session.add(row)
    db_session.commit()
    payloads = []

    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda _db, _row: {
            "cookies": [{"name": "sessionid", "value": "encrypted-source"}]
        },
    )

    def invoke(payload, *, timeout_seconds):
        payloads.append((dict(payload), timeout_seconds))
        current = db_session.get(HermesBrowserBridge, int(row.id))
        meta = dict(current.meta_json or {})
        meta["doubao_browser_status"] = "ready"
        current.meta_json = meta
        db_session.add(current)
        db_session.flush()
        return {"status": "restored"}

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", invoke)
    monkeypatch.setattr(doubao_tasks.time, "sleep", lambda _seconds: None)

    recovered = doubao_tasks._wait_for_provider_browser_generation(
        db_session,
        account_id=int(row.id),
        task_id=904,
        timeout_seconds=5,
    )

    assert int(recovered.id) == int(row.id)
    assert len(payloads) == 1
    assert payloads[0][0]["action"] == "restore_session"
    assert payloads[0][0]["browser_cdp_url"] == "http://127.0.0.1:9230"


def test_auth_probe_can_recover_cookie_backed_profile_but_not_captcha():
    recoverable = {
        "doubao_pool_enabled": False,
        "doubao_capture_state": "failed",
        "doubao_auth_state": AUTH_REQUIRED,
        "doubao_pool_last_error": "doubao_auth_required",
    }
    captcha = {
        **recoverable,
        "doubao_capture_state": "captcha_required",
        "doubao_pool_last_error": "doubao_captcha_required",
    }

    assert auth_probe_eligible(recoverable) is True
    assert auth_probe_eligible(captcha) is False


def test_provider_browser_hold_expires_without_releasing_remote_task_lease():
    now = datetime.now().astimezone().replace(tzinfo=None)
    meta = {
        "doubao_pool_lease_task_id": 902,
        "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
        "doubao_provider_browser_task_id": 902,
        "doubao_provider_browser_hold_until": (now + timedelta(seconds=30)).isoformat(),
    }

    assert doubao_lab_service._provider_request_pending(meta, now=now)
    assert not doubao_lab_service._provider_request_pending(
        meta,
        now=now + timedelta(seconds=31),
    )
    assert meta["doubao_pool_lease_task_id"] == 902


def test_due_browser_hold_releases_only_browser_marker():
    now = datetime.now().astimezone().replace(tzinfo=None)
    row = HermesBrowserBridge(
        id=77,
        meta_json={
            "doubao_pool_lease_task_id": 903,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 903,
            "doubao_provider_browser_hold_until": (now - timedelta(seconds=1)).isoformat(),
        },
    )

    class Db:
        def add(self, _row):
            return None

    assert doubao_tasks._release_provider_browser_hold_if_due(
        Db(),
        row,
        task_id=903,
        now=now,
    )
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert row.meta_json["doubao_provider_browser_hold_until"] is None
    assert row.meta_json["doubao_pool_lease_task_id"] == 903


def test_completed_remote_result_replaces_stale_progress_with_complete(db_session):
    task = KieTask(
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:complete-progress",
        state="queued",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "download_name_base": "904",
                "doubao_remote_progress": {"state": "silent_conversation"},
            }
        },
    )
    db_session.add(task)
    db_session.flush()

    result = doubao_tasks._ensure_result(
        db_session,
        task,
        result={
            "video_url": "https://example.test/complete-progress.mp4",
            "width": 720,
            "height": 1280,
            "duration": 4.0,
        },
    )

    assert result.state == "downloading"
    assert result.result_json["__local"]["doubao_remote_progress"] == {
        "state": "complete",
        "width": 720,
        "height": 1280,
        "duration": 4.0,
    }
    assert result.result_json["__local"]["doubao_remote_completed_at"]


def test_inconclusive_auth_probe_preserves_still_fresh_authentication():
    checked_at = datetime.now() - timedelta(minutes=5)
    meta = mark_authenticated({}, now=checked_at)

    result = doubao_lab_tasks._apply_auth_probe_observation(
        meta,
        auth_state=AUTH_UNKNOWN,
        network_state=NETWORK_UNREACHABLE,
        error_code="doubao_timeout",
        now=datetime.now(),
    )

    assert result["doubao_auth_state"] == AUTHENTICATED
    assert result["doubao_auth_checked_at"] == checked_at.isoformat()
    assert result["doubao_network_state"] == NETWORK_UNREACHABLE
    assert result["doubao_auth_error"] == "doubao_timeout"


def test_inconclusive_auth_probe_does_not_preserve_expired_authentication():
    checked_at = datetime.now() - timedelta(hours=9)
    meta = mark_authenticated({}, now=checked_at)

    result = doubao_lab_tasks._apply_auth_probe_observation(
        meta,
        auth_state=AUTH_UNKNOWN,
        network_state=NETWORK_UNREACHABLE,
        error_code="doubao_timeout",
        now=datetime.now(),
    )

    assert result["doubao_auth_state"] == AUTH_UNKNOWN


def _online_device(db_session):
    row = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        device_name="Windows A",
        inbox_root=r"C:\HermesInbox",
        slot_index=0,
    )
    row.meta_json = {
        **dict(row.meta_json or {}),
        "account_device_bound": True,
        "agent_last_heartbeat_at": datetime.now().isoformat(),
    }
    db_session.add(row)
    db_session.flush()
    return row


def _proxy(db_session):
    return create_flow_proxy(
        db_session,
        name="doubao lab proxy",
        proxy_url="socks5h://192.168.1.21:7893",
        actor_user_id=101,
    )


def _start(db_session):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item for item in db_session.query(HermesBrowserBridge).all() if is_doubao_lab_slot(item)
    )
    return session, row


def _future_lease() -> str:
    return (datetime.now() + timedelta(minutes=20)).isoformat()


def test_doubao_onboarding_owns_dedicated_profile_without_plain_proxy(db_session):
    session, row = _start(db_session)

    assert session["state"] == "awaiting_login"
    assert row.meta_json["slot_index"] == 1
    assert is_external_account_slot(row)
    assert "192.168.1.21" not in str(row.meta_json)
    spec = doubao_slot_spec(db_session, row)
    assert spec["purpose"] == "doubao_lab"
    assert spec["login_only"] is True
    assert spec["proxy_url"] == "socks5h://192.168.1.21:7893"
    assert session["membership"]["tier"] == "free"
    assert session["membership"]["allowed_durations_seconds"] == list(range(4, 11))


def test_doubao_desktop_runtime_is_opt_in_and_reported_to_agent(db_session):
    _, row = _start(db_session)
    assert doubao_slot_spec(db_session, row)["runtime"] == "chrome"

    meta = dict(row.meta_json or {})
    meta["doubao_runtime"] = "doubao_desktop"
    row.meta_json = meta
    db_session.add(row)
    db_session.flush()

    spec = doubao_slot_spec(db_session, row)
    assert spec["runtime"] == "doubao_desktop"
    assert spec["login_only"] is False
    assert spec["capture_required"] is True
    assert spec["interactive"] is True


def test_doubao_onboarding_supports_device_local_direct_network(db_session):
    _online_device(db_session)
    session = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=None,
    )
    row = next(
        item for item in db_session.query(HermesBrowserBridge).all() if is_doubao_lab_slot(item)
    )

    assert session["network_mode"] == "direct"
    assert session["proxy_id"] is None
    assert row.meta_json["doubao_network_mode"] == "direct"
    assert doubao_slot_spec(db_session, row)["proxy_url"] == ""


def test_doubao_proxy_rebind_invalidates_session_and_reopens_exact_profile(db_session):
    session, row = _start(db_session)
    old_profile_port = row.meta_json["local_port"]
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:old-session",
            "doubao_profile_id": f"windows-a/slot-{old_profile_port}",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        }
    )
    row.meta_json = meta
    replacement = create_flow_proxy(
        db_session,
        name="replacement proxy",
        proxy_url="socks5h://192.168.1.22:7893",
        actor_user_id=101,
    )
    db_session.flush()

    updated = rebind_doubao_account_proxy(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
        proxy_id=int(replacement.id),
    )

    assert updated["session_id"] != session["session_id"]
    assert updated["state"] == "awaiting_login"
    assert updated["pool"]["enabled"] is False
    assert row.meta_json["local_port"] == old_profile_port
    assert row.meta_json["doubao_session_context_ciphertext"] is None
    assert doubao_slot_spec(db_session, row)["proxy_url"] == "socks5h://192.168.1.22:7893"


def test_doubao_account_delete_tombstones_profile_and_removes_from_pool(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:session",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        }
    )
    row.meta_json = meta

    result = retire_doubao_account(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert result["deleted"] is True
    assert row.status == "retired"
    assert row.meta_json["doubao_profile_retired"] is True
    assert row.meta_json["doubao_session_context_ciphertext"] is None
    assert list_doubao_lab_sessions(db_session, workspace_id=3, user_id=101) == []


def test_doubao_account_delete_and_proxy_rebind_reject_active_lease(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:session",
            "doubao_pool_lease_task_id": 9001,
        }
    )
    row.meta_json = meta
    replacement = create_flow_proxy(
        db_session,
        name="busy replacement proxy",
        proxy_url="socks5h://192.168.1.22:7893",
        actor_user_id=101,
    )
    db_session.flush()

    with pytest.raises(APIError, match="正在生成视频") as rebind_error:
        rebind_doubao_account_proxy(
            db_session,
            workspace_id=3,
            user_id=101,
            capture_id=session["session_id"],
            proxy_id=int(replacement.id),
        )
    assert rebind_error.value.code == "DOUBAO_POOL_ACCOUNT_BUSY"
    with pytest.raises(APIError, match="正在生成视频") as delete_error:
        retire_doubao_account(
            db_session,
            workspace_id=3,
            user_id=101,
            capture_id=session["session_id"],
        )
    assert delete_error.value.code == "DOUBAO_POOL_ACCOUNT_BUSY"


def test_login_window_close_advances_exact_doubao_profile_to_capture(db_session):
    session, row = _start(db_session)

    record_doubao_browser_report(
        row,
        {
            "flow_status": "login_complete",
            "page_url": "https://www.doubao.com/chat/",
            "capture_id": "runtime-generation-1",
        },
        now=datetime.now(),
    )

    assert row.meta_json["doubao_capture_state"] == "capture_pending"
    assert row.meta_json["doubao_capture_id"] == session["session_id"]
    assert row.meta_json["doubao_browser_capture_id"] == "runtime-generation-1"
    spec = doubao_slot_spec(db_session, row)
    assert spec["login_only"] is False
    assert spec["capture_required"] is True


def test_doubao_capture_ignores_initial_login_probe_until_page_restores(db_session):
    _session, row = _start(db_session)
    started_at = datetime.now()
    record_doubao_browser_report(
        row,
        {"flow_status": "login_complete", "page_url": "https://www.doubao.com/chat/"},
        now=started_at,
    )

    record_doubao_browser_report(
        row,
        {"flow_status": "login_required", "page_url": "https://www.doubao.com/chat/"},
        now=started_at + timedelta(seconds=2),
    )

    assert row.meta_json["doubao_capture_state"] == "capture_pending"
    assert row.meta_json["doubao_capture_login_required_reports"] == 1
    assert doubao_slot_spec(db_session, row)["capture_required"] is True

    record_doubao_browser_report(
        row,
        {"flow_status": "login_required", "page_url": "https://www.doubao.com/chat/"},
        now=started_at + timedelta(seconds=21),
    )

    assert row.meta_json["doubao_capture_state"] == "awaiting_login"
    assert doubao_slot_spec(db_session, row)["login_only"] is True


def test_provider_request_wakes_only_the_account_leased_to_that_task(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_pool_lease_task_id": 9182,
            "doubao_pool_lease_expires_at": _future_lease(),
            "doubao_provider_browser_task_id": 9182,
        }
    )
    row.meta_json = meta

    assert doubao_slot_should_wake(row, now=datetime.now()) is True
    assert doubao_slot_spec(db_session, row)["provider_request"] is True
    assert doubao_slot_spec(db_session, row)["interactive"] is False

    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_provider_browser_task_id": 9183,
    }
    assert doubao_slot_should_wake(row, now=datetime.now()) is False
    assert doubao_slot_spec(db_session, row)["provider_request"] is False


def test_manual_capture_provider_request_is_interactive(db_session):
    _session, row = _start(db_session)
    lease_id = "manual-capture:9230:test"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "ready",
        "doubao_pool_lease_task_id": lease_id,
        "doubao_pool_lease_expires_at": _future_lease(),
        "doubao_provider_browser_task_id": lease_id,
    }

    spec = doubao_slot_spec(db_session, row)

    assert spec["provider_request"] is True
    assert spec["interactive"] is True
    assert spec["capture_id"] == lease_id


def test_new_provider_request_uses_its_lease_as_agent_retry_generation(
    db_session,
):
    session, row = _start(db_session)
    stable_capture_id = str(row.meta_json["doubao_capture_id"])
    first_request_id = "doubao-task:first"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "ready",
        "doubao_pool_lease_task_id": first_request_id,
        "doubao_pool_lease_expires_at": _future_lease(),
        "doubao_provider_browser_task_id": first_request_id,
    }

    first_spec = doubao_slot_spec(db_session, row)

    second_request_id = "doubao-task:second"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_pool_lease_task_id": second_request_id,
        "doubao_pool_lease_expires_at": _future_lease(),
        "doubao_provider_browser_task_id": second_request_id,
    }
    second_spec = doubao_slot_spec(db_session, row)

    assert first_spec["capture_id"] == first_request_id
    assert second_spec["capture_id"] == second_request_id
    assert row.meta_json["doubao_capture_id"] == stable_capture_id


def test_manual_verification_reuses_exact_profile_without_erasing_login(db_session):
    session, row = _start(db_session)
    ciphertext = "enc:v1:preserve-this-login"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_session_context_ciphertext": ciphertext,
        "doubao_page_url": "https://www.doubao.com/chat/38436133030532610",
    }

    result, dispatch_required = start_doubao_manual_verification(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    lease_id = str(row.meta_json["doubao_pool_lease_task_id"])
    assert lease_id.startswith("manual-capture:")
    assert row.meta_json["doubao_provider_browser_task_id"] == lease_id
    assert row.meta_json["doubao_session_context_ciphertext"] == ciphertext
    assert row.meta_json["doubao_target_url"] == "https://www.doubao.com/chat/create-image"
    assert result["manual_verification"]["state"] == "preparing"
    assert dispatch_required is True
    assert doubao_slot_spec(db_session, row)["interactive"] is True


def test_manual_verification_prepares_seedance_without_auto_submitting(db_session):
    session, row = _start(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_session_context_ciphertext": "enc:v1:test",
    }
    _result, dispatch_required = start_doubao_manual_verification(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )
    assert dispatch_required is True
    challenge_id = str(row.meta_json["doubao_manual_verification_challenge_id"])

    assert apply_doubao_manual_video_challenge_result(
        row,
        challenge_id=challenge_id,
        status="ready_to_submit",
    ) is True
    assert row.meta_json["doubao_manual_verification_state"] == "awaiting_human"
    assert row.meta_json["doubao_capture_state"] == "captcha_required"
    assert row.meta_json["doubao_pool_enabled"] is False
    assert str(row.meta_json["doubao_pool_lease_task_id"]).startswith(
        "manual-capture:"
    )
    assert "Seedance 2.0 Mini" in row.meta_json[
        "doubao_manual_verification_message"
    ]


def test_cancel_manual_verification_releases_lease_and_stops_browser(db_session):
    session, row = _start(db_session)
    lease_id = "manual-capture:test-cancel"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_pool_lease_task_id": lease_id,
        "doubao_pool_lease_expires_at": _future_lease(),
        "doubao_provider_browser_task_id": lease_id,
        "doubao_manual_verification_state": "awaiting_human",
    }

    result = cancel_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert result["manual_verification"]["state"] == "cancelled"
    assert row.meta_json["doubao_capture_state"] == "cancelled"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_pool_lease_expires_at"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert doubao_slot_should_wake(row, now=datetime.now()) is False


@pytest.mark.asyncio
async def test_manual_verification_restores_pool_after_seedance_chat_redirect(db_session):
    session, row = _start(db_session)
    lease_id = "manual-capture:test-recovery"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_session_context_ciphertext": "enc:v1:test",
        "doubao_pool_lease_task_id": lease_id,
        "doubao_provider_browser_task_id": lease_id,
        "doubao_page_url": "https://www.doubao.com/chat/38436133030532610",
        "doubao_seedance_capability_state": "captcha_required",
        "doubao_pool_last_error": "doubao_captcha_required",
        "doubao_manual_verification_challenge_id": "test-recovery",
        "doubao_manual_verification_challenge_sent_at": datetime.now().isoformat(),
    }

    result = await complete_doubao_manual_verification(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert result["manual_verification"]["state"] == "complete"
    assert row.meta_json["doubao_capture_state"] == "ready"
    assert row.meta_json["doubao_pool_enabled"] is True
    assert row.meta_json["doubao_seedance_capability_state"] == "ready"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert int(row.meta_json.get("doubao_pool_success_count") or 0) == 0


def test_region_restriction_stops_manual_browser_without_login_loop(db_session):
    _session, row = _start(db_session)
    lease_id = "manual-capture:legacy"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_pool_lease_task_id": lease_id,
        "doubao_provider_browser_task_id": lease_id,
        "doubao_manual_verification_state": "awaiting_human",
    }

    record_doubao_browser_report(
        row,
        {
            "flow_status": "login_required",
            "page_url": "https://www.doubao.com/security/doubao-region-ban?source=1",
        },
        now=datetime.now(),
    )

    assert row.meta_json["doubao_capture_state"] == "ready"
    assert row.meta_json["doubao_pool_enabled"] is False
    assert row.meta_json["doubao_seedance_capability_state"] == "region_restricted"
    assert row.meta_json["doubao_manual_verification_state"] == "region_restricted"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert doubao_slot_should_wake(row, now=datetime.now()) is False


def test_pool_reconcile_cancels_legacy_manual_lease(db_session):
    _session, row = _start(db_session)
    lease_id = "manual-capture:legacy"
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_pool_lease_task_id": lease_id,
        "doubao_provider_browser_task_id": lease_id,
        "doubao_manual_verification_state": "awaiting_human",
    }

    result = reconcile_doubao_account_pool(
        db_session, workspace_id=3, user_id=101
    )

    assert result["legacy_manual_cancelled"] == 1
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert row.meta_json["doubao_manual_verification_state"] == "cancelled"


def test_pool_reconcile_clears_orphaned_manual_browser_marker(db_session):
    _session, row = _start(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "ready",
        "doubao_pool_enabled": False,
        "doubao_pool_lease_task_id": None,
        "doubao_provider_browser_task_id": "manual-capture:orphaned",
        "doubao_seedance_capability_state": "region_restricted",
        "doubao_pool_last_error": "doubao_region_restricted",
    }

    result = reconcile_doubao_account_pool(
        db_session, workspace_id=3, user_id=101
    )

    assert result["region_restricted"] == 1
    assert row.meta_json["doubao_provider_browser_task_id"] is None


@pytest.mark.asyncio
async def test_structural_verify_does_not_clear_captcha_required(db_session, monkeypatch):
    session, row = _start(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "captcha_required",
        "doubao_pool_enabled": False,
        "doubao_session_context_ciphertext": "enc:v1:test",
    }
    monkeypatch.setattr(
        "app.services.doubao_lab.decrypt_doubao_session_context",
        lambda _meta: {"cookies": [{"name": "sessionid", "value": "redacted"}]},
    )

    result = await verify_doubao_lab_session(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert result["state"] == "captcha_required"
    assert result["pool"]["enabled"] is False


@pytest.mark.asyncio
async def test_doubao_capture_encrypts_bounded_cookie_context(db_session):
    session, row = _start(db_session)
    record_doubao_browser_report(
        row, {"flow_status": "login_complete"}, now=datetime.now()
    )
    secret = "doubao-session-secret-value-1234567890"

    result = await ingest_doubao_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_cookies=[
            {
                "name": "sessionid",
                "value": secret,
                "domain": ".doubao.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            },
            {
                "name": "sessionid",
                "value": "must-be-rejected",
                "domain": ".attacker.test",
                "path": "/",
            },
        ],
        session_diagnostics={
            "device_params": {
                "fp": "verify_browser_profile",
                "device_id": "7123456789012345",
                "web_id": "7612345678901234567",
            }
        },
        profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
        fingerprint={
            "user_agent": "Mozilla/5.0",
            "accept_language": "zh-CN",
            "sec_ch_ua": '"Chromium";v="150"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "timezone": "Asia/Shanghai",
        },
    )

    assert result == {"success": True}
    assert row.meta_json["doubao_capture_state"] == "ready"
    assert row.meta_json["doubao_session_context_ciphertext"].startswith("enc:v1:")
    assert secret not in str(row.meta_json)
    context = decrypt_doubao_session_context(dict(row.meta_json or {}))
    assert context is not None
    assert len(context["cookies"]) == 1
    assert context["device_params"]["fp"] == "verify_browser_profile"
    assert account_is_ready(row) is False
    assert row.meta_json["doubao_seedance_capability_state"] == "unknown"


def test_seedance_probe_owns_exact_profile_before_account_becomes_routable(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
        }
    )
    row.meta_json = meta
    db_session.flush()

    current, dispatch_required = queue_doubao_capability_probe(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert dispatch_required is True
    probe_id = current["pool"]["capability"]["probe_id"]
    assert current["pool"]["capability"]["state"] == "probing"
    assert row.meta_json["doubao_pool_lease_task_id"] == f"probe:{probe_id}"
    assert row.meta_json["doubao_provider_browser_task_id"] == f"probe:{probe_id}"
    assert doubao_slot_should_wake(row, now=datetime.now()) is True
    assert account_is_ready(row) is False


def test_historical_generation_without_fresh_auth_is_not_routable(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": None,
            "doubao_pool_success_count": 1,
        }
    )
    row.meta_json = meta

    assert account_is_ready(row) is False


def test_fresh_auth_and_verified_capability_make_account_routable(db_session):
    _session, row = _start(db_session)
    meta = mark_authenticated(
        {
            **dict(row.meta_json or {}),
                "doubao_capture_state": "ready",
                "doubao_session_context_ciphertext": "enc:v1:test",
                "doubao_pool_enabled": True,
                "doubao_seedance_capability_state": "ready",
                "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    row.meta_json = meta

    assert account_is_ready(row) is True


def test_stale_agent_heartbeat_removes_cached_account_from_routing(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = mark_authenticated(
        {
            **dict(row.meta_json or {}),
            "agent_last_heartbeat_at": (now - timedelta(minutes=5)).isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        },
        now=now,
    )
    row.meta_json = meta
    db_session.flush()

    assert account_is_ready(row, now=now) is False
    assert account_is_retry_candidate(row, now=now) is False
    with pytest.raises(RuntimeError, match="没有可用账号"):
        claim_account(db_session, task_id=450)
    listed = list_doubao_lab_sessions(db_session, workspace_id=3, user_id=101)
    assert listed[0]["pool"]["ready"] is False
    assert listed[0]["pool"]["status"] == "device_offline"


def test_device_fault_opens_one_circuit_without_poisoning_sibling_accounts(
    db_session,
):
    _session, first = _start(db_session)
    now = datetime.now()
    first_meta = mark_authenticated(
        {
            **dict(first.meta_json or {}),
            "agent_last_heartbeat_at": now.isoformat(),
                "doubao_capture_state": "ready",
                "doubao_session_context_ciphertext": "enc:v1:first",
                "doubao_pool_enabled": True,
                "doubao_seedance_capability_state": "ready",
                "agent_last_heartbeat_at": datetime.now().isoformat(),
            "doubao_pool_lease_task_id": 451,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 451,
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_last_error": None,
        },
        now=now,
    )
    first.meta_json = first_meta
    start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first_meta["doubao_proxy_id"]),
    )
    sibling = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if row.id != first.id and is_doubao_lab_slot(row)
    )
    sibling.meta_json = mark_authenticated(
        {
            **dict(sibling.meta_json or {}),
            "agent_last_heartbeat_at": now.isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:sibling",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_last_error": None,
        },
        now=now,
    )
    db_session.flush()

    release_account(
        db_session,
        first,
        task_id=451,
        success=False,
        error_code="doubao_browser_unstable",
    )
    db_session.flush()

    assert first.meta_json["doubao_pool_consecutive_errors"] == 0
    assert first.meta_json["doubao_pool_last_error"] is None
    assert first.meta_json["doubao_seedance_capability_state"] == "ready"
    assert first.meta_json["doubao_device_circuit_until"]
    assert sibling.meta_json["doubao_device_circuit_until"]
    assert account_is_retry_candidate(sibling, now=now) is False
    with pytest.raises(RuntimeError, match="没有可用账号"):
        claim_account(
            db_session,
            task_id=452,
            excluded_bridge_ids={str(first.bridge_id)},
        )


def test_auth_probe_scheduler_claims_each_due_account_once(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_next_auth_probe_at": None,
        }
    )
    row.meta_json = meta
    db_session.flush()

    assert due_auth_probe_accounts(db_session, limit=10) == [int(row.id)]
    assert row.meta_json["doubao_auth_probe_claimed_at"]
    assert row.meta_json["doubao_next_auth_probe_at"]
    assert due_auth_probe_accounts(db_session, limit=10) == []


def test_auth_probe_scheduler_claims_disabled_auth_recovery_account(db_session):
    _session, row = _start(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "doubao_capture_state": "failed",
        "doubao_session_context_ciphertext": "enc:v1:test",
        "doubao_pool_enabled": False,
        "doubao_auth_state": AUTH_REQUIRED,
        "doubao_pool_last_error": "doubao_auth_required",
        "doubao_next_auth_probe_at": None,
    }
    db_session.add(row)
    db_session.flush()

    assert due_auth_probe_accounts(db_session, limit=10) == [int(row.id)]


def test_capability_probe_scheduler_claims_unknown_account_once(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
            "doubao_pool_last_error": "doubao_submit_unconfirmed",
            "doubao_next_capability_probe_at": None,
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    db_session.flush()

    claims = due_capability_probe_accounts(
        db_session, limit=2, retry_after_seconds=900
    )

    assert len(claims) == 1
    assert claims[0]["account_id"] == int(row.id)
    assert claims[0]["bridge_id"] == str(row.bridge_id)
    assert row.meta_json["doubao_seedance_capability_state"] == "probing"
    assert str(row.meta_json["doubao_pool_lease_task_id"]).startswith("probe:dp_")
    assert due_capability_probe_accounts(db_session, limit=2) == []


def test_capability_probe_scheduler_does_not_share_active_network_lane(db_session):
    _session, first = _start(db_session)
    now = datetime.now()
    first_meta = dict(first.meta_json or {})
    first_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:first",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_lease_task_id": 9001,
            "doubao_provider_browser_task_id": 9001,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
        }
    )
    first.meta_json = mark_authenticated(first_meta, now=now)
    db_session.flush()
    start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first.meta_json["doubao_proxy_id"]),
    )
    second = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if row.id != first.id and is_doubao_lab_slot(row)
    )
    second_meta = dict(second.meta_json or {})
    second_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:second",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
        }
    )
    second.meta_json = mark_authenticated(second_meta, now=now)
    db_session.flush()

    assert due_capability_probe_accounts(db_session, limit=2) == []


def test_rate_limited_account_reenters_pool_after_cooldown_without_manual_probe(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "agent_last_heartbeat_at": (now + timedelta(minutes=2)).isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "rate_limited",
            "doubao_pool_last_error": "doubao_risk_rate_limited",
            "doubao_pool_cooldown_until": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)

    assert account_is_ready(row, now=now) is False
    assert account_is_ready(row, now=now + timedelta(minutes=2)) is False
    assert account_is_retry_candidate(row, now=now + timedelta(minutes=2)) is True


def test_transient_unknown_account_reenters_pool_after_cooldown(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "agent_last_heartbeat_at": (now + timedelta(minutes=2)).isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
            "doubao_pool_last_error": "doubao_failed",
            "doubao_pool_cooldown_until": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)

    assert account_is_ready(row, now=now) is False
    assert account_is_ready(row, now=now + timedelta(minutes=2)) is False
    assert account_is_retry_candidate(row, now=now + timedelta(minutes=2)) is True


def test_text_only_response_account_reenters_pool_after_cooldown(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "agent_last_heartbeat_at": (now + timedelta(minutes=2)).isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
            "doubao_pool_last_error": "doubao_text_only_response",
            "doubao_pool_cooldown_until": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)

    assert account_is_retry_candidate(row, now=now) is False
    assert account_is_retry_candidate(row, now=now + timedelta(minutes=2)) is True


def test_legacy_superseded_release_reenters_pool_without_manual_probe(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "agent_last_heartbeat_at": (now + timedelta(minutes=2)).isoformat(),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
            "doubao_seedance_capability_error": "cf_variant_superseded",
            "doubao_pool_last_error": "cf_variant_superseded",
            "doubao_pool_cooldown_until": (now + timedelta(minutes=1)).isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)

    assert account_is_retry_candidate(row, now=now) is False
    assert account_is_retry_candidate(row, now=now + timedelta(minutes=2)) is True


def test_capability_probe_retries_one_transient_composer_failure(monkeypatch):
    calls = []

    def invoke(payload, *, timeout_seconds):
        calls.append((payload, timeout_seconds))
        if len(calls) == 1:
            raise DoubaoProviderError("loading", code="doubao_composer_unavailable")
        return {"status": "capable"}

    monkeypatch.setattr(doubao_lab_tasks, "invoke_doubao_helper", invoke)
    monkeypatch.setattr(doubao_lab_tasks.time, "sleep", lambda _seconds: None)

    assert doubao_lab_tasks._invoke_capability_probe({"action": "probe"}) == {
        "status": "capable"
    }
    assert len(calls) == 2


def test_inconclusive_capability_recheck_preserves_last_confirmed_ability():
    recovered = apply_seedance_capability_result(
        {
            "doubao_seedance_capability_state": "probing",
            "doubao_seedance_capability_previous_state": "ready",
            "doubao_pool_last_success_at": "2026-07-29T14:01:29",
            "doubao_pool_success_count": 4,
        },
        success=False,
        error_code="doubao_browser_unstable",
    )

    assert recovered["doubao_seedance_capability_state"] == "ready"
    assert recovered["doubao_seedance_capability_error"] == "doubao_browser_unstable"
    assert "doubao_seedance_capability_previous_state" not in recovered


def test_legacy_success_evidence_recovers_transient_unknown_but_not_new_account():
    assert seedance_capability_state({
        "doubao_seedance_capability_state": "unknown",
        "doubao_seedance_capability_error": "doubao_capability_probe_failed",
        "doubao_pool_success_count": 3,
    }) == "ready"
    assert seedance_capability_state({
        "doubao_seedance_capability_state": "unknown",
        "doubao_seedance_capability_error": "doubao_capability_probe_failed",
        "doubao_pool_success_count": 0,
    }) == "unknown"


def test_hard_capability_recheck_still_revokes_confirmed_ability():
    restricted = apply_seedance_capability_result(
        {
            "doubao_seedance_capability_state": "probing",
            "doubao_seedance_capability_previous_state": "ready",
            "doubao_pool_success_count": 4,
        },
        success=False,
        error_code="doubao_region_restricted",
    )

    assert restricted["doubao_seedance_capability_state"] == "region_restricted"


def test_successful_capability_probe_clears_only_transient_pool_cooldown():
    recovered = apply_seedance_capability_result(
        {
            "doubao_pool_enabled": True,
            "doubao_pool_last_error": "doubao_composer_unavailable",
            "doubao_pool_cooldown_until": "2026-07-29T12:00:00",
            "doubao_pool_consecutive_errors": 3,
        },
        success=True,
    )
    assert recovered["doubao_seedance_capability_state"] == "ready"
    assert recovered["doubao_pool_last_error"] is None
    assert recovered["doubao_pool_cooldown_until"] is None
    assert recovered["doubao_pool_consecutive_errors"] == 0

    legacy_request_outcome = apply_seedance_capability_result(
        {
            "doubao_pool_enabled": True,
            "doubao_pool_last_error": "doubao_submit_unconfirmed",
            "doubao_pool_cooldown_until": "2026-08-09T12:00:00",
            "doubao_pool_consecutive_errors": 9,
        },
        success=True,
    )
    assert legacy_request_outcome["doubao_pool_last_error"] is None
    assert legacy_request_outcome["doubao_pool_cooldown_until"] is None
    assert legacy_request_outcome["doubao_pool_consecutive_errors"] == 0

    quota = apply_seedance_capability_result(
        {
            "doubao_pool_last_error": "doubao_quota_exhausted",
            "doubao_pool_cooldown_until": "2026-07-30T00:00:00",
        },
        success=True,
    )
    assert quota["doubao_pool_last_error"] == "doubao_quota_exhausted"
    assert quota["doubao_pool_cooldown_until"] == "2026-07-30T00:00:00"


def test_successful_capability_probe_clears_stale_failure_message():
    recovered = apply_seedance_capability_result(
        {
            "doubao_seedance_capability_state": "unavailable",
            "doubao_seedance_capability_error": "doubao_composer_unavailable",
            "doubao_seedance_capability_message": "旧的失败提示不应继续展示",
        },
        success=True,
    )

    assert recovered["doubao_seedance_capability_state"] == "ready"
    assert recovered["doubao_seedance_capability_error"] is None
    assert recovered["doubao_seedance_capability_message"] is None


def test_ready_session_hides_legacy_capability_failure_message(db_session):
    _, row = _start(db_session)
    row.meta_json = mark_authenticated({
        **dict(row.meta_json or {}),
        "doubao_capture_state": "ready",
        "doubao_pool_enabled": True,
        "doubao_session_context_ciphertext": "encrypted-context",
        "doubao_seedance_capability_state": "ready",
        "doubao_seedance_capability_error": None,
        "doubao_seedance_capability_message": "过期失败提示",
    })
    db_session.add(row)
    db_session.flush()

    [session] = list_doubao_lab_sessions(
        db_session, workspace_id=3, user_id=101
    )

    assert session["pool"]["ready"] is True
    assert session["pool"]["capability"]["message"] is None


def test_doubao_profile_is_hidden_from_content_factory_slot_pool(db_session):
    _start(db_session)

    devices = browser_devices(db_session, workspace_id=3, user_id=101)

    assert len(devices) == 1
    assert devices[0]["slot_count"] == 1


def test_running_doubao_test_is_idempotent(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_test_id": "dt_existing",
            "doubao_test_state": "running",
        }
    )
    row.meta_json = meta
    db_session.add(row)
    db_session.flush()

    current, dispatch_required = queue_doubao_lab_test(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
        prompt="must not submit twice",
        duration=4,
        ratio="9:16",
    )

    assert dispatch_required is False
    assert current["test"]["id"] == "dt_existing"
    assert current["test"]["state"] == "running"


def test_doubao_lab_generation_uses_account_managed_browser_endpoint(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_test_prompt": "A vertical city park at night.",
            "doubao_test_duration": 4,
            "doubao_test_ratio": "9:16",
            "doubao_proxy_id": 41,
        }
    )
    monkeypatch.setattr(
        doubao_lab_tasks,
        "decrypt_doubao_session_context",
        lambda _meta: {"cookies": [{"name": "sessionid", "value": "redacted"}]},
    )
    monkeypatch.setattr(
        doubao_lab_tasks,
        "resolve_flow_proxy_url",
        lambda _db, proxy_id, require_active: "socks5h://proxy.invalid:7893",
    )

    payload = doubao_lab_tasks._generation_request_payload(
        db_session, row, meta=meta
    )

    assert payload["browser_cdp_url"] == row.cdp_url
    assert payload["prompt"] == "A vertical city park at night."
    assert payload["duration"] == 4
    assert payload["ratio"] == "9:16"


def test_doubao_lab_test_owns_account_and_wakes_exact_profile(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    row.meta_json = meta
    db_session.flush()

    current, dispatch_required = queue_doubao_lab_test(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
        prompt="A vertical city park at night.",
        duration=4,
        ratio="9:16",
    )

    lease_id = f"lab:{current['test']['id']}"
    assert dispatch_required is True
    assert row.meta_json["doubao_pool_lease_task_id"] == lease_id
    assert row.meta_json["doubao_provider_browser_task_id"] == lease_id
    assert doubao_slot_should_wake(row, now=datetime.now()) is True


def test_terminal_doubao_lab_test_releases_account_and_browser(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_test_id": "dt_terminal",
            "doubao_test_state": "running",
            "doubao_pool_lease_task_id": "lab:dt_terminal",
            "doubao_pool_lease_expires_at": (datetime.now() + timedelta(minutes=15)).isoformat(),
            "doubao_provider_browser_task_id": "lab:dt_terminal",
        }
    )
    row.meta_json = meta
    db_session.flush()

    assert doubao_lab_tasks._mark(
        db_session,
        row,
        test_id="dt_terminal",
        state="failed",
        message="bounded failure",
        error="test",
    ) is True

    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_pool_lease_expires_at"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None


def test_captcha_lab_result_removes_account_from_production_pool(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_test_id": "dt_captcha",
            "doubao_test_state": "running",
            "doubao_capture_state": "ready",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_lease_task_id": "lab:dt_captcha",
            "doubao_provider_browser_task_id": "lab:dt_captcha",
        }
    )
    row.meta_json = meta
    db_session.flush()

    assert doubao_lab_tasks._mark(
        db_session,
        row,
        test_id="dt_captcha",
        state="captcha_required",
        message="human verification required",
        error="captcha_required code=710022004",
    ) is True

    assert row.meta_json["doubao_capture_state"] == "captcha_required"
    assert row.meta_json["doubao_pool_enabled"] is False
    assert row.meta_json["doubao_pool_last_error"] == "doubao_captcha_required"
    assert row.meta_json["doubao_pool_lease_task_id"] is None


def test_each_added_doubao_account_gets_a_distinct_browser_profile(db_session):
    session, first = _start(db_session)
    first_meta = dict(first.meta_json or {})
    first_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
        }
    )
    first.meta_json = mark_authenticated(first_meta)
    db_session.add(first)
    db_session.flush()
    # Avoid depending on the helper's table class: the original proxy id is
    # already stored safely on the first profile.
    second = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first.meta_json["doubao_proxy_id"]),
    )
    rows = [row for row in db_session.query(HermesBrowserBridge).all() if is_doubao_lab_slot(row)]
    assert len(rows) == 2
    assert rows[0].bridge_id != rows[1].bridge_id
    assert rows[0].meta_json["local_port"] != rows[1].meta_json["local_port"]
    assert second["session_id"] != session["session_id"]


def test_pool_claim_is_single_account_and_rotates_after_release(db_session):
    _session, first = _start(db_session)
    meta = dict(first.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        }
    )
    first.meta_json = mark_authenticated(meta)
    db_session.add(first)
    db_session.flush()

    claimed = claim_account(db_session, task_id=501)
    assert claimed.id == first.id
    assert claimed.meta_json["doubao_provider_browser_task_id"] == 501
    with pytest.raises(DoubaoPoolBusyError, match="通道正忙"):
        claim_account(db_session, task_id=502)

    release_account(db_session, claimed, task_id=501, success=True)
    db_session.flush()
    claimed_again = claim_account(db_session, task_id=502)
    assert claimed_again.id == first.id
    assert claimed_again.meta_json["doubao_pool_lease_task_id"] == 502


def test_pool_prefers_recent_proven_account_over_pure_lru(db_session):
    _session, older = _start(db_session)
    now = datetime.now()
    older_meta = mark_authenticated(
        {
            **dict(older.meta_json or {}),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:older",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_seedance_capability_checked_at": now.isoformat(),
            "doubao_pool_last_used_at": (now - timedelta(days=2)).isoformat(),
            "doubao_pool_last_success_at": (now - timedelta(days=20)).isoformat(),
            "doubao_pool_success_count": 1,
        },
        now=now,
    )
    older.meta_json = older_meta
    newer_session = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(older_meta["doubao_proxy_id"]),
    )
    newer = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if row.id != older.id and is_doubao_lab_slot(row)
    )
    newer_meta = mark_authenticated(
        {
            **dict(newer.meta_json or {}),
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:newer",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_seedance_capability_checked_at": now.isoformat(),
            "doubao_pool_last_used_at": now.isoformat(),
            "doubao_pool_last_success_at": (now - timedelta(hours=1)).isoformat(),
            "doubao_pool_success_count": 5,
        },
        now=now,
    )
    newer.meta_json = newer_meta
    db_session.add_all([older, newer])
    db_session.flush()

    assert account_dispatch_score(newer, now=now) > account_dispatch_score(
        older, now=now
    )
    claimed = claim_account(db_session, task_id=503)

    assert claimed.id == newer.id
    assert claimed.meta_json["doubao_capture_id"] == newer_session["session_id"]


def test_submit_observation_tracks_latency_without_secrets(db_session):
    _session, row = _start(db_session)

    record_submit_observation(row, duration_ms=100_000, success=True)
    record_submit_observation(row, duration_ms=50_000, success=True)
    record_submit_observation(
        row,
        duration_ms=12_000,
        success=False,
        error_code="doubao_composer_unavailable",
    )

    meta = dict(row.meta_json or {})
    assert meta["doubao_pool_submit_latency_ewma_ms"] == 85_000
    assert meta["doubao_pool_last_submit_outcome"] == "failed"
    assert meta["doubao_pool_last_submit_error"] == "doubao_composer_unavailable"
    assert "cookie" not in str(meta).lower()


def test_expired_provider_lease_cannot_reopen_browser_and_is_reclaimed(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_lease_task_id": 520,
            "doubao_pool_lease_expires_at": (
                datetime.now() - timedelta(minutes=1)
            ).isoformat(),
            "doubao_provider_browser_task_id": 520,
        }
    )
    row.meta_json = mark_authenticated(meta)
    db_session.add(row)
    db_session.flush()

    assert doubao_slot_should_wake(row, now=datetime.now()) is False
    assert doubao_slot_spec(db_session, row)["provider_request"] is False

    claimed = claim_account(db_session, task_id=521)

    assert claimed.id == row.id
    assert claimed.meta_json["doubao_pool_lease_task_id"] == 521
    assert claimed.meta_json["doubao_provider_browser_task_id"] == 521


def test_accounts_sharing_proxy_serialize_only_browser_submission(db_session):
    _session, first = _start(db_session)
    first_meta = dict(first.meta_json or {})
    first_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:first",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    first.meta_json = mark_authenticated(first_meta)
    second_session = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first.meta_json["doubao_proxy_id"]),
    )
    second = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if item.id != first.id and is_doubao_lab_slot(item)
    )
    second_meta = dict(second.meta_json or {})
    second_meta.update(
        {
                "doubao_capture_state": "ready",
                "doubao_session_context_ciphertext": "enc:v1:second",
                "doubao_pool_enabled": True,
                "doubao_seedance_capability_state": "ready",
                "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    second.meta_json = mark_authenticated(second_meta)
    db_session.add_all([first, second])
    db_session.flush()

    claimed_first = claim_account(db_session, task_id=530)
    assert claimed_first.id == first.id
    with pytest.raises(DoubaoPoolBusyError, match="通道正忙"):
        claim_account(db_session, task_id=531)

    first_meta = dict(claimed_first.meta_json or {})
    first_meta["doubao_provider_browser_task_id"] = None
    claimed_first.meta_json = first_meta
    db_session.add(claimed_first)
    db_session.flush()

    claimed_second = claim_account(db_session, task_id=531)
    assert claimed_second.id == second.id
    assert claimed_second.meta_json["doubao_capture_id"] == second_session["session_id"]


def test_free_account_rejects_paid_duration_and_enhanced_account_accepts_it(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        }
    )
    row.meta_json = mark_authenticated(meta)
    db_session.add(row)
    db_session.flush()

    with pytest.raises(RuntimeError, match="加强套餐账号"):
        claim_account(db_session, task_id=510, requested_duration=12)

    set_account_membership(row, tier="enhanced")
    db_session.flush()
    claimed = claim_account(db_session, task_id=510, requested_duration=12)

    assert claimed.id == row.id
    assert claimed.meta_json["doubao_membership_tier"] == "enhanced"


def test_free_account_lab_test_rejects_more_than_ten_seconds(db_session):
    session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
        }
    )
    row.meta_json = meta
    db_session.flush()

    with pytest.raises(APIError) as exc_info:
        queue_doubao_lab_test(
            db_session,
            workspace_id=3,
            user_id=101,
            capture_id=session["session_id"],
            prompt="A vertical city park at night.",
            duration=12,
            ratio="9:16",
        )

    assert exc_info.value.code == "DOUBAO_MEMBERSHIP_REQUIRED"


def test_remote_task_recovers_an_expired_idle_account_lease(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": None,
            "doubao_pool_lease_expires_at": None,
        }
    )
    row.meta_json = meta
    db_session.add(row)
    db_session.flush()

    recovered = leased_account(
        db_session,
        bridge_id=str(row.bridge_id),
        task_id=777,
    )

    assert recovered.meta_json["doubao_pool_lease_task_id"] == 777
    assert recovered.meta_json["doubao_pool_lease_recovered_at"]
    assert recovered.meta_json["doubao_pool_lease_expires_at"]


@pytest.mark.asyncio
async def test_transient_poll_failure_keeps_remote_account_lease(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 778,
        }
    )
    row.meta_json = meta
    task = KieTask(
        id=778,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-778",
        state="queued",
        input_json={"duration": 8, "aspect_ratio": "9:16"},
        result_json={"__local": {"doubao_account_bridge_id": row.bridge_id}},
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DoubaoProviderError("temporary network failure", code="doubao_timeout")
        ),
    )

    with pytest.raises(DoubaoProviderError) as exc_info:
        await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert exc_info.value.code == "doubao_timeout"
    assert row.meta_json["doubao_pool_lease_task_id"] == 778


@pytest.mark.asyncio
async def test_silent_remote_conversation_rotates_after_bounded_grace(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 780,
        }
    )
    row.meta_json = meta
    task = KieTask(
        id=780,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-780",
        state="queued",
        input_json={"duration": 7, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=11)
                ).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-780",
            "progress": {
                "state": "silent_conversation",
                "message_count": 2,
                "bot_message_count": 1,
                "bot_content_count": 0,
                "video_model_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        doubao_tasks.settings,
        "DOUBAO_SILENT_CONVERSATION_TIMEOUT_SECONDS",
        600,
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "failed"
    assert result.fail_code == "doubao_silent_timeout"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert task.result_json["__local"]["doubao_remote_progress"]["message_count"] == 2
    assert task.result_json["__local"]["doubao_failed_account_bridge_ids"] == [
        row.bridge_id
    ]


@pytest.mark.asyncio
async def test_text_only_remote_response_rotates_after_bounded_grace(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 781,
        }
    )
    row.meta_json = meta
    task = KieTask(
        id=781,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-781",
        state="queued",
        input_json={"duration": 7, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=11)
                ).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-781",
            "progress": {
                "state": "assistant_progress",
                "message_count": 4,
                "bot_message_count": 2,
                "bot_content_count": 1,
                "video_model_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        doubao_tasks.settings,
        "DOUBAO_TEXT_ONLY_RESPONSE_TIMEOUT_SECONDS",
        600,
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "failed"
    assert result.fail_code == "doubao_text_only_response"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert task.result_json["__local"]["doubao_failed_account_bridge_ids"] == [
        row.bridge_id
    ]
    assert (
        task.result_json["__local"]["doubao_remote_progress"]
        ["bot_content_count"]
        == 1
    )


@pytest.mark.asyncio
async def test_empty_assistant_shell_without_video_keeps_polling_after_grace(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 782,
        }
    )
    row.meta_json = meta
    task = KieTask(
        id=782,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-782",
        state="queued",
        input_json={"duration": 7, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=11)
                ).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-782",
            "progress": {
                "state": "assistant_progress",
                "message_count": 3,
                "bot_message_count": 1,
                "bot_content_count": 0,
                "video_model_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        doubao_tasks.settings,
        "DOUBAO_TEXT_ONLY_RESPONSE_TIMEOUT_SECONDS",
        600,
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "queued"
    assert result.fail_code is None
    assert row.meta_json["doubao_pool_lease_task_id"] == 782
    assert (
        task.result_json["__local"]["doubao_remote_progress"]
        ["bot_content_count"]
        == 0
    )


@pytest.mark.asyncio
async def test_completed_wrong_aspect_becomes_retryable_not_repolled(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 779,
        }
    )
    row.meta_json = meta
    task = KieTask(
        id=779,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-779",
        state="queued",
        input_json={"duration": 8, "aspect_ratio": "9:16"},
        result_json={"__local": {"doubao_account_bridge_id": row.bridge_id}},
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "complete",
            "conversation_id": "conversation-779",
            "video_url": "https://example.test/square.mp4",
            "width": 960,
            "height": 960,
            "duration": 8,
        },
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "failed"
    assert result.fail_code == "doubao_output_aspect_mismatch"
    rejected = result.result_json["__local"]["doubao_rejected_result"]
    assert rejected["width"] == 960
    assert rejected["height"] == 960
    assert row.meta_json["doubao_pool_lease_task_id"] is None


def test_quota_exhausted_account_is_cooled_and_next_account_is_selected(db_session):
    _session, first = _start(db_session)
    first_meta = dict(first.meta_json or {})
    first_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:first",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
        }
    )
    first.meta_json = mark_authenticated(first_meta)
    db_session.add(first)
    db_session.flush()

    second_session = start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first.meta_json["doubao_proxy_id"]),
    )
    second = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if row.id != first.id and is_doubao_lab_slot(row)
    )
    second_meta = dict(second.meta_json or {})
    second_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:second",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    second.meta_json = mark_authenticated(second_meta)
    db_session.add(second)
    db_session.flush()

    claimed = claim_account(db_session, task_id=601)
    assert claimed.id == first.id
    release_account(
        db_session,
        claimed,
        task_id=601,
        success=False,
        error_code="doubao_quota_exhausted",
    )
    db_session.flush()

    cooled = db_session.get(HermesBrowserBridge, int(first.id))
    assert cooled.meta_json["doubao_pool_capacity_state"] == "exhausted"
    assert cooled.meta_json["doubao_pool_capacity_retry_at"]
    assert cooled.meta_json["doubao_pool_enabled"] is True

    replacement = claim_account(
        db_session,
        task_id=601,
        excluded_bridge_ids={str(first.bridge_id)},
    )
    assert replacement.id == second.id
    assert replacement.meta_json["doubao_capture_id"] == second_session["session_id"]


def test_successful_generation_clears_observed_capacity_cooldown(db_session):
    _session, row = _start(db_session)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_capacity_state": "exhausted",
            "doubao_pool_capacity_retry_at": "2026-07-27T12:00:00",
            "doubao_pool_cooldown_until": "2026-07-27T12:00:00",
            "doubao_pool_lease_task_id": 701,
        }
    )
    row.meta_json = meta
    db_session.add(row)
    db_session.flush()

    release_account(db_session, row, task_id=701, success=True)
    db_session.flush()

    assert row.meta_json["doubao_pool_capacity_state"] == "available"
    assert row.meta_json["doubao_pool_capacity_retry_at"] is None
    assert row.meta_json["doubao_pool_success_count"] == 1


def test_superseded_task_releases_lease_without_poisoning_account_health(db_session):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_lease_task_id": 702,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 702,
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_last_error": None,
            "doubao_pool_cooldown_until": None,
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    db_session.add(row)
    db_session.flush()

    release_account(
        db_session,
        row,
        task_id=702,
        success=False,
        error_code="cf_variant_superseded",
    )
    db_session.flush()

    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert row.meta_json["doubao_seedance_capability_state"] == "ready"
    assert int(row.meta_json.get("doubao_pool_consecutive_errors") or 0) == 0
    assert row.meta_json["doubao_pool_last_error"] is None
    assert row.meta_json["doubao_pool_cooldown_until"] is None
    assert row.meta_json["doubao_pool_last_neutral_release"] == "cf_variant_superseded"
    assert account_is_retry_candidate(row, now=now) is True


@pytest.mark.parametrize(
    "error_code",
    [
        "doubao_submit_unconfirmed",
        "doubao_silent_timeout",
        "doubao_text_only_response",
    ],
)
def test_request_level_release_does_not_poison_account_health(
    db_session, error_code
):
    _session, row = _start(db_session)
    now = datetime.now()
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "doubao_pool_lease_task_id": 703,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 703,
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_last_error": None,
            "doubao_pool_cooldown_until": None,
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    db_session.flush()

    release_account(
        db_session,
        row,
        task_id=703,
        success=False,
        error_code=error_code,
    )
    db_session.flush()

    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert row.meta_json["doubao_seedance_capability_state"] == "ready"
    assert row.meta_json["doubao_pool_consecutive_errors"] == 0
    assert row.meta_json["doubao_pool_last_error"] is None
    assert row.meta_json["doubao_pool_cooldown_until"] is None
    assert row.meta_json["doubao_pool_last_neutral_release"] == error_code
    assert account_is_retry_candidate(row, now=now) is True


@pytest.mark.asyncio
async def test_pending_submit_hands_off_quickly_and_keeps_bounded_browser_hold(
    db_session, monkeypatch
):
    _session, account = _start(db_session)
    meta = dict(account.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    account.meta_json = mark_authenticated(meta)
    task = KieTask(
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="local-doubao-fast-handoff",
        state="queued_local",
        prompt="A fast vertical camera move through a quiet city park.",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={},
    )
    db_session.add_all([account, task])
    db_session.flush()
    helper_payloads = []

    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda _db, row: {"account_bridge_id": str(row.bridge_id)},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "_wait_for_provider_browser_generation",
        lambda db, *, account_id, **_kwargs: db.get(HermesBrowserBridge, account_id),
    )
    monkeypatch.setattr(
        doubao_tasks, "_ensure_live_video_composer", lambda *_args, **_kwargs: None
    )

    def invoke(payload, *, timeout_seconds):
        helper_payloads.append((dict(payload), timeout_seconds))
        return {
            "status": "submitted",
            "conversation_id": "38436222074597378",
            "submission_contract": {
                "surface": "ai_creation",
                "ability_type": 17,
                "model": "seedance_v2.0_mini",
                "ratio": "9:16",
                "duration": 4,
                "reference_count": 0,
            },
        }

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", invoke)
    result = await doubao_tasks.submit_doubao_task(db_session, task=task)
    db_session.flush()
    account = db_session.get(HermesBrowserBridge, int(account.id))

    assert result.state == "queued"
    assert result.task_id == "doubao:38436222074597378"
    assert helper_payloads[0][0]["post_submit_observe_seconds"] == 5
    assert account.meta_json["doubao_provider_browser_task_id"] == int(task.id)
    assert account.meta_json["doubao_provider_browser_hold_until"]
    assert account.meta_json["doubao_provider_submission_accepted_at"]
    assert account.meta_json["doubao_pool_lease_task_id"] == int(task.id)
    assert result.result_json["__local"]["doubao_browser_hold_until"]
    hold_until = datetime.fromisoformat(
        account.meta_json["doubao_provider_browser_hold_until"]
    )
    assert hold_until - datetime.now().astimezone().replace(tzinfo=None) > timedelta(
        minutes=5
    )
    assert not _browser_lane_is_busy(
        account.meta_json,
        now=datetime.now().astimezone().replace(tzinfo=None),
    )


@pytest.mark.asyncio
async def test_silent_remote_keeps_exact_browser_profile_alive(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    now = datetime.now().astimezone().replace(tzinfo=None)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 779,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 779,
            "doubao_provider_browser_hold_until": (now + timedelta(minutes=6)).isoformat(),
            "doubao_provider_submission_accepted_at": now.isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    task = KieTask(
        id=779,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-779",
        state="queued",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-779",
            "progress": {
                "state": "silent_conversation",
                "message_count": 2,
                "bot_message_count": 1,
                "bot_content_count": 0,
                "video_model_count": 0,
            },
        },
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "queued"
    assert row.meta_json["doubao_provider_browser_task_id"] == 779
    assert doubao_slot_should_wake(row, now=now) is True


@pytest.mark.asyncio
async def test_video_model_progress_releases_browser_but_keeps_remote_lease(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    now = datetime.now().astimezone().replace(tzinfo=None)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 778,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 778,
            "doubao_provider_browser_hold_until": (now + timedelta(minutes=6)).isoformat(),
            "doubao_provider_submission_accepted_at": now.isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    task = KieTask(
        id=778,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-778",
        state="queued",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-778",
            "progress": {
                "state": "video_ready",
                "message_count": 3,
                "bot_message_count": 1,
                "bot_content_count": 0,
                "video_model_count": 1,
            },
        },
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "queued"
    assert row.meta_json["doubao_provider_browser_task_id"] is None
    assert row.meta_json["doubao_pool_lease_task_id"] == 778


@pytest.mark.asyncio
async def test_explicit_content_rejection_fails_fast_without_poisoning_account(
    db_session, monkeypatch
):
    _session, row = _start(db_session)
    now = datetime.now().astimezone().replace(tzinfo=None)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:test",
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": 777,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "doubao_provider_browser_task_id": 777,
            "doubao_provider_browser_hold_until": (now + timedelta(minutes=6)).isoformat(),
            "doubao_provider_submission_accepted_at": now.isoformat(),
        }
    )
    row.meta_json = mark_authenticated(meta, now=now)
    task = KieTask(
        id=777,
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:conversation-777",
        state="queued",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={
            "__local": {
                "doubao_account_bridge_id": row.bridge_id,
                "doubao_remote_accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add_all([row, task])
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda *_args, **_kwargs: {"account": "safe-test"},
    )
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: {
            "status": "pending",
            "conversation_id": "conversation-777",
            "progress": {
                "state": "content_rejected",
                "message_count": 2,
                "bot_message_count": 1,
                "bot_content_count": 0,
                "video_model_count": 0,
                "content_rejected": True,
            },
        },
    )

    result = await doubao_tasks.refresh_doubao_task(db_session, task=task)

    assert result.state == "failed"
    assert result.fail_code == "doubao_content_rejected"
    assert row.meta_json["doubao_pool_lease_task_id"] is None
    assert int(row.meta_json.get("doubao_pool_consecutive_errors") or 0) == 0


@pytest.mark.asyncio
async def test_submit_rotates_to_next_account_before_provider_failover(
    db_session, monkeypatch
):
    _session, first = _start(db_session)
    first_meta = dict(first.meta_json or {})
    first_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:first",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    first.meta_json = mark_authenticated(first_meta)
    db_session.add(first)
    db_session.flush()
    start_doubao_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(first.meta_json["doubao_proxy_id"]),
    )
    second = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if row.id != first.id and is_doubao_lab_slot(row)
    )
    second_meta = dict(second.meta_json or {})
    second_meta.update(
        {
            "doubao_capture_state": "ready",
            "doubao_session_context_ciphertext": "enc:v1:second",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "ready",
            "agent_last_heartbeat_at": datetime.now().isoformat(),
        }
    )
    second.meta_json = mark_authenticated(second_meta)
    task = KieTask(
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="local-doubao-rotation-test",
        state="queued_local",
        prompt="A vertical camera move through a quiet city street.",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={},
    )
    db_session.add_all([second, task])
    db_session.flush()

    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda _db, row: {"account_bridge_id": str(row.bridge_id)},
    )
    calls: list[str] = []

    def _invoke(payload, *, timeout_seconds):
        del timeout_seconds
        calls.append(str(payload["account_bridge_id"]))
        if len(calls) == 1:
            raise DoubaoProviderError(
                "Doubao account quota is temporarily exhausted",
                code="doubao_quota_exhausted",
            )
        return {
            "status": "complete",
            "conversation_id": "conversation-second-account",
            "video_url": "https://example.test/second-account.mp4",
            "duration": 4,
            "submission_contract": {
                "surface": "ai_creation",
                "ability_type": 17,
                "model": "seedance_v2.0_mini",
                "ratio": "9:16",
                "duration": 4,
                "reference_count": 0,
            },
        }

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", _invoke)
    monkeypatch.setattr(
        doubao_tasks,
        "_wait_for_provider_browser_generation",
        lambda db, *, account_id, **_kwargs: db.get(HermesBrowserBridge, account_id),
    )
    monkeypatch.setattr(
        doubao_tasks, "_ensure_live_video_composer", lambda *_args, **_kwargs: None
    )
    result = await doubao_tasks.submit_doubao_task(db_session, task=task)

    assert calls == [str(first.bridge_id), str(second.bridge_id)]
    assert result.state == "downloading"
    assert result.task_id == "doubao:conversation-second-account"
    assert first.meta_json["doubao_pool_capacity_state"] == "exhausted"
    assert second.meta_json["doubao_pool_capacity_state"] == "available"


@pytest.mark.asyncio
async def test_device_browser_fault_short_circuits_sibling_accounts_before_failover(
    db_session, monkeypatch
):
    _session, first = _start(db_session)
    proxy_id = int(first.meta_json["doubao_proxy_id"])
    rows = []
    for index in range(3):
        if index == 0:
            row = first
        else:
            start_doubao_lab_onboarding(
                db_session,
                workspace_id=3,
                user_id=101,
                device_id="windows-a",
                proxy_id=proxy_id,
            )
            row = max(
                (
                    item
                    for item in db_session.query(HermesBrowserBridge).all()
                    if is_doubao_lab_slot(item)
                ),
                key=lambda item: int(item.id),
            )
        meta = dict(row.meta_json or {})
        meta.update(
            {
                "doubao_capture_state": "ready",
                "doubao_session_context_ciphertext": f"enc:v1:{index}",
                "doubao_pool_enabled": True,
                "doubao_seedance_capability_state": "ready",
            }
        )
        row.meta_json = mark_authenticated(meta)
        rows.append(row)
        db_session.add(row)
        db_session.flush()
    task = KieTask(
        workspace_id=3,
        created_by_user_id=101,
        key_id=1,
        model="seedance_2_0_mini",
        task_id="local-doubao-bounded-rotation-test",
        state="queued_local",
        prompt="A vertical camera move through a quiet city street.",
        input_json={"duration": 4, "aspect_ratio": "9:16"},
        result_json={},
    )
    db_session.add(task)
    db_session.flush()

    monkeypatch.setattr(
        doubao_tasks,
        "account_request_payload",
        lambda _db, row: {"account_bridge_id": str(row.bridge_id)},
    )
    monkeypatch.setattr(
        doubao_tasks, "_ensure_live_video_composer", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        doubao_tasks,
        "_wait_for_provider_browser_generation",
        lambda db, *, account_id, **_kwargs: db.get(HermesBrowserBridge, account_id),
    )
    calls = []

    def _invoke(payload, *, timeout_seconds):
        del timeout_seconds
        calls.append(str(payload["account_bridge_id"]))
        raise DoubaoProviderError(
            "Browser is temporarily unstable",
            code="doubao_browser_unstable",
        )

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", _invoke)

    with pytest.raises(DoubaoProviderError) as exc_info:
        await doubao_tasks.submit_doubao_task(db_session, task=task)

    assert exc_info.value.code == "doubao_pool_unavailable"
    assert len(calls) == 1
    assert task.result_json["__local"]["provider_submit_accounts_exhausted"] == 1
    for row in rows:
        assert row.meta_json["doubao_device_circuit_until"]
        assert int(row.meta_json.get("doubao_pool_consecutive_errors") or 0) == 0
        assert row.meta_json.get("doubao_pool_last_error") is None
    assert task.state == "queued_local"
    assert task.task_id.startswith("local-ai-video-")
