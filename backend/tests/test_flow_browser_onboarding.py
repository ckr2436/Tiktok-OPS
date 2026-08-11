from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow_browser_onboarding import (
    _flow_admin_error_retryable,
    cancel_flow_browser_onboarding,
    flow_slot_spec,
    flow_slot_should_wake,
    ingest_flow_browser_capture,
    is_flow_account_slot,
    record_flow_browser_report,
    reconcile_flow_browser_bindings_from_upstream,
    start_flow_browser_onboarding,
)
from app.services.flow2api_admin import Flow2ApiAdminError
from app.services.hermes_agent.content_factory import _new_agent_slot
from app.services.hermes_agent.content_factory import _next_agent_profile_slot_index
from app.core.errors import APIError


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


def test_browser_onboarding_reserves_dedicated_stable_profile(db_session):
    _online_device(db_session)

    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=2,
        proxy_url="socks5h://192.168.1.21:7893",
    )

    assert session["state"] == "awaiting_login"
    assert session["profile_id"] is None
    assert "session_token" not in session
    flow_rows = [row for row in db_session.query(HermesBrowserBridge).all() if is_flow_account_slot(row)]
    assert len(flow_rows) == 1
    assert flow_rows[0].meta_json["slot_index"] == 1
    spec = flow_slot_spec(flow_rows[0])
    assert spec["login_only"] is True
    assert spec["capture_required"] is False
    assert spec["automatic_visit"] is False
    assert spec["proxy_url"] == "socks5h://192.168.1.21:7893"


def test_device_reported_profile_capacity_allows_slot_above_legacy_32(db_session):
    device = _online_device(db_session)
    device.meta_json = {**dict(device.meta_json or {}), "agent_profile_capacity": 64}
    for index in range(1, 32):
        _new_agent_slot(
            db_session,
            workspace_id=3,
            user_id=101,
            device_id="windows-a",
            device_name="Windows A",
            inbox_root=r"C:\HermesInbox",
            slot_index=index,
        )
    db_session.flush()

    slot_index = _next_agent_profile_slot_index(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        error_code="FLOW_BROWSER_PROFILE_CAPACITY_FULL",
    )

    assert slot_index == 32


def test_device_reported_profile_capacity_remains_authoritative(db_session):
    device = _online_device(db_session)
    device.meta_json = {**dict(device.meta_json or {}), "agent_profile_capacity": 4}
    for index in range(1, 4):
        _new_agent_slot(
            db_session,
            workspace_id=3,
            user_id=101,
            device_id="windows-a",
            device_name="Windows A",
            inbox_root=r"C:\HermesInbox",
            slot_index=index,
        )
    db_session.flush()

    with pytest.raises(APIError) as raised:
        _next_agent_profile_slot_index(
            db_session,
            workspace_id=3,
            user_id=101,
            device_id="windows-a",
            error_code="FLOW_BROWSER_PROFILE_CAPACITY_FULL",
        )

    assert raised.value.code == "FLOW_BROWSER_PROFILE_CAPACITY_FULL"
    assert "4 个持久化浏览器 Profile" in raised.value.message


def test_verified_unbound_flow_orphan_is_wiped_before_profile_reuse(db_session):
    device = _online_device(db_session)
    device.meta_json = {**dict(device.meta_json or {}), "agent_profile_capacity": 2}
    orphan = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        device_name="Windows A",
        inbox_root=r"C:\HermesInbox",
        slot_index=1,
    )
    orphan.meta_json = {
        **dict(orphan.meta_json or {}),
        "flow_account_slot": True,
        "flow_capture_state": "failed",
        "flow_capture_updated_at": (datetime.now() - timedelta(hours=2)).isoformat(),
        "flow_token_id": None,
    }
    orphan.status = "standby"
    orphan_id = orphan.id
    db_session.flush()

    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="replacement",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )

    db_session.flush()
    replacement = db_session.get(HermesBrowserBridge, orphan_id)
    assert session["state"] == "awaiting_login"
    assert replacement is not None
    assert replacement.meta_json["slot_index"] == 1
    assert replacement.meta_json["reset_profile_required"] is True
    assert replacement.meta_json["flow_token_id"] is None


def test_normal_login_close_advances_same_profile_to_capture(db_session):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )

    record_flow_browser_report(
        row,
        {
            "capture_id": session["session_id"],
            "flow_status": "login_complete",
            "page_url": "https://labs.google/fx/tools/flow",
        },
        now=datetime.now(),
    )

    assert row.meta_json["flow_capture_state"] == "capture_pending"
    assert row.meta_json["flow_capture_id"] == session["session_id"]
    assert row.meta_json["flow_browser_status"] == "login_complete"
    spec = flow_slot_spec(row)
    assert spec["login_only"] is False
    assert spec["capture_required"] is True

    record_flow_browser_report(
        row,
        {
            "capture_id": session["session_id"],
            "flow_status": "login_required",
            "page_url": "https://labs.google/fx/tools/flow",
        },
        now=datetime.now(),
    )

    assert row.meta_json["flow_capture_state"] == "awaiting_login"
    retry_spec = flow_slot_spec(row)
    assert retry_spec["login_only"] is True
    assert retry_spec["capture_required"] is False


def test_cancel_unfinished_onboarding_retires_profile_and_disables_slot(db_session):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )
    # The Agent may acknowledge the closed Chrome before the UI cancellation
    # reaches the API. Cancellation must remain idempotent across that race.
    row.status = "retired"

    cancelled = cancel_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert cancelled["state"] == "cancelled"
    assert row.status == "retired"
    assert row.meta_json["flow_profile_retired"] is True
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert flow_slot_spec(row)["login_only"] is False
    assert flow_slot_spec(row)["capture_required"] is False

    record_flow_browser_report(
        row,
        {"flow_status": "login_complete"},
        now=datetime.now(),
    )
    assert row.meta_json["flow_capture_state"] == "cancelled"


def test_cancel_reauth_restores_bound_profile_to_ready(db_session):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )
    row.meta_json = {**dict(row.meta_json or {}), "flow_token_id": 44}

    cancelled = cancel_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
    )

    assert cancelled["state"] == "ready"
    assert row.status == "standby"
    assert row.meta_json["flow_token_id"] == 44
    assert row.meta_json.get("flow_profile_retired") is not True


def test_ready_profile_never_wakes_for_background_keepalive(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_auto_reauth_policy_ready": True,
        "flow_next_keepalive_at": (datetime.now() + timedelta(hours=1)).isoformat(),
    }
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_next_keepalive_at": (datetime.now() - timedelta(seconds=1)).isoformat(),
    }
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert row.meta_json["flow_capture_state"] == "ready"


def test_stale_keepalive_is_normalized_without_reopening_browser(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "keepalive_pending",
        "flow_capture_updated_at": (now - timedelta(minutes=9)).isoformat(),
    }

    assert flow_slot_should_wake(row, now=now) is False
    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_capture_error"] is None
    assert row.meta_json["flow_next_retry_at"] is None
    assert row.meta_json["flow_next_keepalive_at"] is None


def test_expired_interactive_login_stops_browser_until_manual_retry(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_capture_state": "awaiting_login",
        "flow_capture_updated_at": (now - timedelta(minutes=16)).isoformat(),
    }

    assert flow_slot_should_wake(row, now=now) is False
    assert row.meta_json["flow_capture_state"] == "failed"
    assert row.meta_json["flow_capture_error"] == "interactive_login_timeout"
    assert row.meta_json["flow_next_retry_at"] is None


def test_transient_validation_errors_retry_but_identity_mismatch_stops():
    assert _flow_admin_error_retryable(
        Flow2ApiAdminError("credits validation failed: Exception", status_code=400)
    ) is True
    assert _flow_admin_error_retryable(
        Flow2ApiAdminError(
            "credits validation failed [grant_expired]: Exception",
            status_code=400,
        )
    ) is False
    assert _flow_admin_error_retryable(
        Flow2ApiAdminError("account identity mismatch", status_code=400)
    ) is False


def test_reconcile_syncs_proxy_for_existing_account_binding(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "captcha_proxy_url": "socks5h://192.168.1.23:7893",
            }
        ],
    )

    assert changed == 1
    assert flow_slot_spec(row)["proxy_url"] == "socks5h://192.168.1.23:7893"


def test_reconcile_mirrors_expiry_without_scheduling_browser_keepalive(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    expires = now + timedelta(minutes=55)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_next_keepalive_at": (now + timedelta(hours=20)).isoformat(),
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": True,
                "at_expires": expires.isoformat(),
            }
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_next_keepalive_at"] is None
    assert row.meta_json["flow_upstream_at_expires"] == expires.isoformat()
    assert flow_slot_should_wake(row, now=now) is False


def test_reconcile_expired_account_schedules_bounded_headless_reauth(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_next_keepalive_at": (now + timedelta(hours=20)).isoformat(),
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": False,
                "ban_reason": "consecutive_errors",
                "at_expires": (now - timedelta(minutes=5)).isoformat(),
            }
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_next_keepalive_at"] is None
    assert row.meta_json["flow_upstream_ban_reason"] == "consecutive_errors"
    assert flow_slot_should_wake(row, now=now) is False


def test_reconcile_grant_expired_wakes_one_renderer_bootstrap_per_device(db_session):
    first = _online_device(db_session)
    first.meta_json = {
        **dict(first.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_auto_reauth_policy_ready": True,
    }
    second = _new_agent_slot(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        device_name="Windows A",
        inbox_root=r"C:\HermesInbox",
        slot_index=1,
    )
    second.meta_json = {
        **dict(second.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 8,
        "flow_capture_state": "ready",
        "flow_auto_reauth_policy_ready": True,
    }
    db_session.flush()
    now = datetime.now()

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": True,
                "ban_reason": "GRANT_EXPIRED",
                "current_project_id": "project-seven",
            },
            {"id": 8, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 2
    scheduled = [
        row
        for row in (first, second)
        if row.meta_json["flow_capture_state"] == "awaiting_login"
    ]
    assert len(scheduled) == 1
    selected = scheduled[0]
    waiting = second if selected is first else first
    assert selected.meta_json["flow_capture_purpose"] == "auto_reauth"
    assert selected.meta_json["flow_auto_reauth_attempts"] == 1
    assert flow_slot_should_wake(selected, now=now) is True
    assert flow_slot_spec(selected)["capture_required"] is False
    assert flow_slot_spec(selected)["login_only"] is True
    assert flow_slot_spec(selected)["automatic_visit"] is True
    if selected is first:
        assert flow_slot_spec(selected)["target_url"] == "https://labs.google/fx/tools/flow"
        assert selected.meta_json["flow_upstream_project_id"] == "project-seven"
    assert waiting.meta_json["flow_capture_state"] == "ready"
    assert flow_slot_should_wake(waiting, now=now) is False


def test_automatic_reauth_login_page_stops_without_visible_browser_loop(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_id": "flow_current",
        "flow_capture_state": "capture_pending",
        "flow_capture_purpose": "auto_reauth",
        "flow_auto_reauth_attempts": 1,
    }

    record_flow_browser_report(
        row,
        {
            "capture_id": "flow_current",
            "flow_status": "login_required",
            "page_url": "https://accounts.google.com/",
        },
        now=datetime.now(),
    )

    assert row.meta_json["flow_capture_state"] == "human_required"
    assert row.meta_json["flow_capture_error"] == "interactive_login_required"
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert flow_slot_spec(row)["login_only"] is False
    assert flow_slot_spec(row)["capture_required"] is False


def test_stale_flow_report_cannot_terminate_new_automatic_reauth(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_id": "flow_current",
        "flow_capture_state": "capture_pending",
        "flow_capture_purpose": "auto_reauth",
        "flow_auto_reauth_attempts": 1,
    }

    accepted = record_flow_browser_report(
        row,
        {
            "capture_id": "flow_previous",
            "flow_status": "login_required",
            "page_url": "https://accounts.google.com/",
        },
        now=datetime.now(),
    )

    assert accepted is False
    assert row.meta_json["flow_capture_id"] == "flow_current"
    assert row.meta_json["flow_capture_state"] == "capture_pending"
    assert row.meta_json.get("flow_capture_error") is None
    assert flow_slot_spec(row)["capture_required"] is True


def test_reconcile_strategy_change_never_reopens_failed_profile(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "human_required",
        "flow_capture_purpose": "auto_reauth",
        "flow_capture_error": "interactive_login_required",
        "flow_auto_reauth_attempts": 1,
        "flow_auto_reauth_strategy": "renderer_bootstrap_then_capture_v3",
        "flow_auto_reauth_next_at": None,
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "human_required"
    assert row.meta_json["flow_capture_purpose"] == "auto_reauth"
    assert row.meta_json["flow_capture_error"] == "interactive_login_required"
    assert row.meta_json["flow_auto_reauth_strategy"] == "renderer_bootstrap_then_capture_v3"
    assert row.meta_json["flow_auto_reauth_attempts"] == 1
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert flow_slot_spec(row)["automatic_visit"] is False


def test_reconcile_current_renderer_strategy_does_not_repeat_failed_auto_reauth(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "human_required",
        "flow_capture_purpose": "auto_reauth",
        "flow_capture_error": "interactive_login_required",
        "flow_auto_reauth_attempts": 1,
        "flow_auto_reauth_strategy": "project_renderer_diagnostics_then_capture_v6",
        "flow_auto_reauth_next_at": None,
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "human_required"
    assert row.meta_json["flow_capture_error"] == "interactive_login_required"
    assert row.meta_json["flow_auto_reauth_attempts"] == 1
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert flow_slot_spec(row)["automatic_visit"] is False


def test_fixed_bridge_rearms_affected_empty_profile_failure_once(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "agent_version": "2026.08.11.4",
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "human_required",
        "flow_capture_purpose": "auto_reauth",
        "flow_capture_error": "interactive_login_required",
        "flow_auto_reauth_attempts": 1,
        "flow_auto_reauth_strategy": "project_renderer_diagnostics_then_capture_v6",
        "flow_auto_reauth_policy_ready": True,
        "flow_auto_reauth_next_at": None,
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "awaiting_login"
    assert row.meta_json["flow_capture_purpose"] == "auto_reauth"
    assert row.meta_json["flow_auto_reauth_attempts"] == 1
    assert row.meta_json["flow_profile_layout_recovery_version"] == "2026.08.11.4"
    assert flow_slot_spec(row)["automatic_visit"] is True

    # A second reconciliation sees the active capture and must not create a
    # duplicate attempt or another browser window.
    repeated = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )
    assert repeated == 0
    assert row.meta_json["flow_auto_reauth_attempts"] == 1


def test_fixed_bridge_repairs_false_ready_state_from_empty_profile(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "agent_version": "2026.08.11.4",
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_capture_purpose": "keepalive",
        "flow_capture_error": None,
        "flow_browser_status": "login_required",
        "flow_auto_reauth_attempts": 1,
        "flow_auto_reauth_strategy": "project_renderer_diagnostics_then_capture_v6",
        "flow_auto_reauth_policy_ready": True,
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "awaiting_login"
    assert row.meta_json["flow_profile_layout_recovery_version"] == "2026.08.11.4"
    assert flow_slot_spec(row)["automatic_visit"] is True


def test_reconcile_migrated_grant_marker_is_imported_without_browser_wake(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "human_required",
        "flow_capture_purpose": "reauth_required",
        "flow_capture_error": "grant_expired",
        "flow_auto_reauth_attempts": 0,
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "human_required"
    assert row.meta_json["flow_capture_purpose"] == "reauth_required"
    assert row.meta_json["flow_auto_reauth_attempts"] == 1
    assert row.meta_json["flow_auto_reauth_policy_ready"] is True
    assert flow_slot_should_wake(row, now=datetime.now()) is False
    assert flow_slot_spec(row)["capture_required"] is False
    assert flow_slot_spec(row)["login_only"] is False
    assert flow_slot_spec(row)["automatic_visit"] is False


def test_healthy_observation_arms_next_new_grant_for_automatic_repair(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
    }

    first = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": None},
        ],
    )
    second = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {"id": 7, "has_st": True, "is_active": True, "ban_reason": "GRANT_EXPIRED"},
        ],
    )

    assert first == 1
    assert second == 1
    assert row.meta_json["flow_capture_state"] == "awaiting_login"
    assert row.meta_json["flow_capture_purpose"] == "auto_reauth"
    assert row.meta_json["flow_auto_reauth_attempts"] == 1


def test_automatic_renderer_visit_advances_same_capture_to_headless_capture(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_id": "flow_current",
        "flow_capture_state": "awaiting_login",
        "flow_capture_purpose": "auto_reauth",
        "flow_auto_reauth_attempts": 1,
        "flow_auto_reauth_strategy": "project_renderer_diagnostics_then_capture_v6",
    }

    accepted = record_flow_browser_report(
        row,
        {
            "capture_id": "flow_current",
            "flow_status": "login_complete",
            "page_url": "https://labs.google/fx/tools/flow",
        },
        now=datetime.now(),
    )

    assert accepted is True
    assert row.meta_json["flow_capture_id"] == "flow_current"
    assert row.meta_json["flow_capture_state"] == "capture_pending"
    spec = flow_slot_spec(row)
    assert spec["login_only"] is False
    assert spec["automatic_visit"] is False
    assert spec["capture_required"] is True


def test_flow_report_persists_only_safe_session_diagnostics(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_capture_id": "flow_current",
        "flow_capture_state": "capture_pending",
    }

    accepted = record_flow_browser_report(
        row,
        {
            "capture_id": "flow_current",
            "flow_status": "login_required",
            "page_url": "https://labs.google/fx/tools/flow",
            "session_diagnostics": {
                "candidate_count": 0,
                "window_login_state": True,
                "local_storage_keys": ["theme"],
                "document_cookie_names": ["consent"],
                "applicable_cookies": [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "domain": "labs.google",
                        "path": "/",
                        "expired": False,
                        "value": "must-not-be-persisted",
                    }
                ],
                "secret": "must-not-be-persisted",
            },
        },
        now=datetime.now(),
    )

    assert accepted is True
    assert row.meta_json["flow_session_diagnostics"] == {
        "candidate_count": 0,
        "window_login_state": True,
        "local_storage_keys": ["theme"],
        "document_cookie_names": ["consent"],
        "applicable_cookies": [
            {
                "name": "__Secure-next-auth.session-token",
                "domain": "labs.google",
                "path": "/",
                "expired": False,
            }
        ],
    }


def test_reconcile_does_not_wake_manually_disabled_account(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "ready",
        "flow_next_keepalive_at": (datetime.now() - timedelta(minutes=1)).isoformat(),
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": False,
                "ban_reason": "manual_disabled",
                "at_expires": (datetime.now() - timedelta(minutes=5)).isoformat(),
            }
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_next_keepalive_at"] is None
    assert flow_slot_should_wake(row, now=datetime.now()) is False


def test_reconcile_releases_pending_lane_when_upstream_is_already_healthy(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "keepalive_pending",
        "flow_capture_error": "bridge_capture_timeout",
        "flow_next_keepalive_at": now.isoformat(),
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": True,
                "ban_reason": None,
                "at_expires": (now + timedelta(hours=1)).isoformat(),
            }
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_capture_error"] is None
    assert row.meta_json["flow_next_keepalive_at"] is None
    assert flow_slot_should_wake(row, now=now) is False


def test_reconcile_releases_manual_reauth_marker_when_upstream_recovers(db_session):
    row = _online_device(db_session)
    now = datetime.now()
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_token_id": 7,
        "flow_capture_state": "human_required",
        "flow_capture_purpose": "reauth_required",
        "flow_capture_error": "grant_expired",
        "flow_browser_status": "reauth_required",
    }

    changed = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 7,
                "has_st": True,
                "is_active": True,
                "ban_reason": None,
                "at_expires": (now + timedelta(hours=1)).isoformat(),
            }
        ],
    )

    assert changed == 1
    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_capture_purpose"] == "keepalive"
    assert row.meta_json["flow_capture_error"] is None
    assert flow_slot_should_wake(row, now=now) is False


@pytest.mark.asyncio
async def test_capture_forwards_secret_once_and_persists_only_safe_state(db_session, monkeypatch):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    captured = {}
    requests = []
    row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )
    profile_id = f"windows-a/slot-{row.meta_json['local_port']}"

    async def fake_request(_self, method, path, *, payload=None):
        requests.append((method, path))
        if method == "GET" and path == "/api/tokens":
            return (
                []
                if len(requests) == 1
                else [
                    {
                        "id": 91,
                        "email": "flow@example.com",
                        "browser_profile_id": profile_id,
                        "is_active": True,
                        "at_expires": (datetime.now() + timedelta(hours=1)).isoformat(),
                    }
                ]
            )
        captured.update({"method": method, "path": path, "payload": payload})
        return {"token": {"id": 91, "email": "flow@example.com"}}

    monkeypatch.setattr(
        "app.services.flow_browser_onboarding.Flow2ApiAdminClient.request",
        fake_request,
    )
    result = await ingest_flow_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="s" * 80,
        profile_id=profile_id,
        fingerprint={
            "user_agent": "Mozilla/5.0 Chrome/150",
            "accept_language": "en-US, en",
            "sec_ch_ua": '"Chromium";v="150"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "timezone": "America/New_York",
        },
    )

    assert result == {"success": True, "token_id": 91}
    assert requests == [
        ("GET", "/api/tokens"),
        ("POST", "/api/tokens"),
        ("GET", "/api/tokens"),
    ]
    assert captured["payload"]["st"] == "s" * 80
    assert captured["payload"]["captcha_proxy_url"] == "socks5h://192.168.1.21:7893"
    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_upstream_active"] is True
    assert row.meta_json["flow_upstream_at_expires"] is not None
    assert "session_token" not in repr(row.meta_json)
    assert row.load_json == {}


@pytest.mark.asyncio
async def test_capture_tries_each_flow_cookie_candidate_before_failing(
    db_session, monkeypatch
):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(item)
    )
    row.meta_json = {**dict(row.meta_json or {}), "flow_token_id": 5}
    profile_id = f"windows-a/slot-{row.meta_json['local_port']}"
    attempted: list[str] = []

    async def fake_request(_self, method, path, *, payload=None):
        if method == "PUT":
            attempted.append(payload["st"])
            if payload["st"] == "o" * 80:
                raise Flow2ApiAdminError(
                    "credits validation failed [grant_expired]: Exception",
                    status_code=400,
                )
            return {"success": True}
        assert method == "GET" and path == "/api/tokens"
        return [
            {
                "id": 5,
                "email": "flow@example.com",
                "browser_profile_id": profile_id,
                "is_active": True,
                "ban_reason": None,
                "at_expires": (datetime.now() + timedelta(hours=1)).isoformat(),
            }
        ]

    monkeypatch.setattr(
        "app.services.flow_browser_onboarding.Flow2ApiAdminClient.request",
        fake_request,
    )
    result = await ingest_flow_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="o" * 80,
        session_tokens=["o" * 80, "f" * 80],
        profile_id=profile_id,
        fingerprint={
            "user_agent": "Mozilla/5.0 Chrome/150",
            "sec_ch_ua_platform": '"Windows"',
        },
    )

    assert result == {"success": True, "token_id": 5}
    assert attempted == ["o" * 80, "f" * 80]
    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_upstream_ban_reason"] is None
    assert "o" * 20 not in repr(row.meta_json)
    assert "f" * 20 not in repr(row.meta_json)


@pytest.mark.asyncio
async def test_capture_reports_google_verification_when_all_candidates_are_expired(
    db_session, monkeypatch
):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(item)
    )
    row.meta_json = {**dict(row.meta_json or {}), "flow_token_id": 5}

    async def fake_request(_self, method, path, *, payload=None):
        raise Flow2ApiAdminError(
            "credits validation failed [grant_expired]: Exception",
            status_code=400,
        )

    monkeypatch.setattr(
        "app.services.flow_browser_onboarding.Flow2ApiAdminClient.request",
        fake_request,
    )
    result = await ingest_flow_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="o" * 80,
        session_tokens=["f" * 80],
        profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
        fingerprint={
            "user_agent": "Mozilla/5.0 Chrome/150",
            "sec_ch_ua_platform": '"Windows"',
        },
    )

    assert result["success"] is False
    assert result["retry"] is False
    assert "Google API" in result["message"]
    assert "账号验证" in result["message"]
    assert row.meta_json["flow_capture_state"] == "failed"


@pytest.mark.asyncio
async def test_capture_recovers_upstream_profile_after_lost_local_pointer(
    db_session, monkeypatch
):
    _online_device(db_session)
    session = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=2,
        proxy_url="socks5h://192.168.1.21:7893",
    )
    row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )
    profile_id = f"windows-a/slot-{row.meta_json['local_port']}"
    requests = []

    async def fake_request(_self, method, path, *, payload=None):
        requests.append((method, path, payload))
        if method == "GET":
            return [
                {
                    "id": 44,
                    "browser_profile_id": profile_id,
                    "is_active": True,
                    "at_expires": (datetime.now() + timedelta(hours=1)).isoformat(),
                }
            ]
        return {"success": True}

    monkeypatch.setattr(
        "app.services.flow_browser_onboarding.Flow2ApiAdminClient.request",
        fake_request,
    )
    result = await ingest_flow_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="r" * 80,
        profile_id=profile_id,
        fingerprint={
            "user_agent": "Mozilla/5.0 Chrome/150",
            "sec_ch_ua_platform": '"Windows"',
        },
    )

    assert result == {"success": True, "token_id": 44}
    assert [(method, path) for method, path, _ in requests] == [
        ("GET", "/api/tokens"),
        ("PUT", "/api/tokens/44"),
        ("GET", "/api/tokens"),
    ]
    assert requests[-2][2]["video_concurrency"] == 2
    assert row.meta_json["flow_token_id"] == 44
    assert row.meta_json["flow_capture_state"] == "ready"


def test_ready_flow_binding_is_monotonic_under_late_browser_report(db_session):
    row = _online_device(db_session)
    row.meta_json = {
        **dict(row.meta_json or {}),
        "flow_account_slot": True,
        "flow_capture_state": "ready",
        "flow_token_id": 44,
    }

    record_flow_browser_report(
        row,
        {"flow_status": "login_complete"},
        now=datetime.now(),
    )

    assert row.meta_json["flow_capture_state"] == "ready"
    assert row.meta_json["flow_token_id"] == 44


def test_safe_upstream_profile_reconciliation_releases_slot_for_next_account(
    db_session,
):
    _online_device(db_session)
    first = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow A",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="http://192.168.1.22:7893",
    )
    first_row = next(
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    )
    profile_id = f"windows-a/slot-{first_row.meta_json['local_port']}"

    repaired = reconcile_flow_browser_bindings_from_upstream(
        db_session,
        workspace_id=3,
        user_id=101,
        upstream_tokens=[
            {
                "id": 55,
                "email": "flow@example.com",
                "has_st": True,
                "browser_profile_id": profile_id,
                "browser_fingerprint_state": "captured",
            }
        ],
    )
    second = start_flow_browser_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        remark="Flow B",
        image_enabled=False,
        video_enabled=True,
        image_concurrency=1,
        video_concurrency=1,
        proxy_url="http://192.168.1.22:7893",
    )

    assert repaired == 1
    assert first_row.meta_json["flow_capture_state"] == "ready"
    assert first_row.meta_json["flow_token_id"] == 55
    assert second["session_id"] != first["session_id"]
    flow_rows = [
        row
        for row in db_session.query(HermesBrowserBridge).all()
        if is_flow_account_slot(row)
    ]
    assert len(flow_rows) == 2
    assert sorted(int(row.meta_json["slot_index"]) for row in flow_rows) == [1, 2]
