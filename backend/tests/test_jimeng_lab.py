from __future__ import annotations

from datetime import datetime

import pytest

from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow_proxy_pool import create_flow_proxy
from app.services.hermes_agent.content_factory import _new_agent_slot, browser_devices
from app.services.jimeng_lab import (
    ingest_jimeng_browser_capture,
    is_external_account_slot,
    is_jimeng_lab_slot,
    jimeng_slot_spec,
    queue_jimeng_lab_test,
    record_jimeng_browser_report,
    start_jimeng_lab_onboarding,
)


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
        name="lab proxy",
        proxy_url="socks5h://192.168.1.21:7893",
        actor_user_id=101,
    )


def test_jimeng_onboarding_uses_dedicated_profile_and_encrypted_proxy_lookup(db_session):
    _online_device(db_session)
    proxy = _proxy(db_session)

    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )

    assert session["state"] == "awaiting_login"
    assert session["credential_state"] == "missing"
    assert session["test"]["state"] == "idle"
    assert session["test"]["id"] is None
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    assert is_external_account_slot(row)
    assert row.meta_json["slot_index"] == 1
    assert "192.168.1.21" not in str(row.meta_json)
    spec = jimeng_slot_spec(db_session, row)
    assert spec["purpose"] == "jimeng_lab"
    assert spec["login_only"] is True
    assert spec["proxy_url"] == "socks5h://192.168.1.21:7893"


def test_login_close_advances_same_jimeng_profile_to_capture(db_session):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )

    record_jimeng_browser_report(
        row,
        {
            "flow_status": "login_complete",
            "page_url": "https://jimeng.jianying.com/ai-tool/generate?type=video",
        },
        now=datetime.now(),
    )

    assert row.meta_json["jimeng_capture_state"] == "capture_pending"
    assert row.meta_json["jimeng_capture_id"] == session["session_id"]
    spec = jimeng_slot_spec(db_session, row)
    assert spec["login_only"] is False
    assert spec["capture_required"] is True


@pytest.mark.asyncio
async def test_capture_verifies_and_stores_only_encrypted_session(db_session, monkeypatch):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    record_jimeng_browser_report(
        row, {"flow_status": "login_complete"}, now=datetime.now()
    )

    async def _live(token, proxy_url):
        assert token == "session-secret-value-1234567890"
        assert proxy_url == "socks5h://192.168.1.21:7893"
        return True

    monkeypatch.setattr("app.services.jimeng_lab._verify_token", _live)
    result = await ingest_jimeng_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="session-secret-value-1234567890",
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
    assert row.meta_json["jimeng_capture_state"] == "ready"
    assert row.meta_json["jimeng_session_ciphertext"].startswith("enc:v1:")
    assert "session-secret-value" not in str(row.meta_json)
    assert "session_token" not in str(result)


@pytest.mark.asyncio
async def test_capture_tries_page_applicable_session_candidates(db_session, monkeypatch):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    record_jimeng_browser_report(
        row, {"flow_status": "login_complete"}, now=datetime.now()
    )
    attempted: list[str] = []

    async def _live(token, proxy_url):
        attempted.append(token)
        return token == "current-session-secret-value-1234567890"

    monkeypatch.setattr("app.services.jimeng_lab._verify_token", _live)
    result = await ingest_jimeng_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="expired-session-secret-value-1234567890",
        session_tokens=[
            "expired-session-secret-value-1234567890",
            "current-session-secret-value-1234567890",
        ],
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
    assert attempted == [
        "expired-session-secret-value-1234567890",
        "current-session-secret-value-1234567890",
    ]
    assert row.meta_json["jimeng_capture_state"] == "ready"
    assert "current-session-secret-value" not in str(row.meta_json)


@pytest.mark.asyncio
async def test_capture_distinguishes_logged_in_web_session_from_reverse_api_failure(
    db_session, monkeypatch
):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    record_jimeng_browser_report(
        row, {"flow_status": "login_complete"}, now=datetime.now()
    )

    async def _not_live(token, proxy_url):
        return False

    monkeypatch.setattr("app.services.jimeng_lab._verify_token", _not_live)
    result = await ingest_jimeng_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="expired-session-secret-value-1234567890",
        session_diagnostics={
            "window_login_state": True,
            "candidate_count": 1,
            "document_cookie_names": ["sessionid", "safe_name"],
            "applicable_cookies": [
                {
                    "name": "sessionid",
                    "domain": ".jianying.com",
                    "path": "/",
                    "secure": True,
                    "http_only": True,
                    "expired": False,
                    "value": "must-not-be-persisted",
                }
            ],
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

    assert result["success"] is False
    assert row.meta_json["jimeng_capture_state"] == "failed"
    assert "逆向接口不兼容" in row.meta_json["jimeng_capture_message"]
    assert "must-not-be-persisted" not in str(row.meta_json)


@pytest.mark.asyncio
async def test_capture_verifies_and_encrypts_full_browser_session_context(
    db_session, monkeypatch
):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    record_jimeng_browser_report(
        row, {"flow_status": "login_complete"}, now=datetime.now()
    )
    checked: dict = {}

    async def _context_live(cookies, fingerprint, proxy_url):
        checked["cookies"] = cookies
        checked["fingerprint"] = fingerprint
        checked["proxy_url"] = proxy_url
        return True

    async def _legacy_must_not_run(token, proxy_url):
        raise AssertionError("verified browser context must not fall back to legacy token")

    monkeypatch.setattr(
        "app.services.jimeng_lab._verify_session_context", _context_live
    )
    monkeypatch.setattr("app.services.jimeng_lab._verify_token", _legacy_must_not_run)
    result = await ingest_jimeng_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        session_token="browser-session-secret-value-1234567890",
        session_cookies=[
            {
                "name": "sessionid",
                "value": "browser-session-secret-value-1234567890",
                "domain": ".jianying.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            },
            {
                "name": "_tea_web_id",
                "value": "7337600885095139000",
                "domain": "jimeng.jianying.com",
                "path": "/",
            },
        ],
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
    assert checked["proxy_url"] == "socks5h://192.168.1.21:7893"
    assert len(checked["cookies"]) == 2
    assert checked["cookies"][0]["domain"] == ".jianying.com"
    assert row.meta_json["jimeng_session_context_ciphertext"].startswith("enc:v1:")
    assert "browser-session-secret-value" not in str(row.meta_json)

    from app.services.jimeng_lab import decrypt_jimeng_session_context

    decrypted = decrypt_jimeng_session_context(
        dict(row.meta_json or {}), "socks5h://192.168.1.21:7893"
    )
    assert decrypted is not None
    assert decrypted["proxy_url"] == "socks5h://192.168.1.21:7893"
    assert decrypted["web_id"] == "7337600885095139000"
    assert len(decrypted["cookies"]) == 2


def test_jimeng_profile_is_not_exposed_as_content_factory_slot(db_session):
    _online_device(db_session)
    proxy = _proxy(db_session)
    start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )

    devices = browser_devices(db_session, workspace_id=3, user_id=101)
    assert len(devices) == 1
    assert devices[0]["slot_count"] == 1


def test_jimeng_waiting_upstream_cannot_submit_duplicate(db_session):
    _online_device(db_session)
    proxy = _proxy(db_session)
    session = start_jimeng_lab_onboarding(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        proxy_id=int(proxy.id),
    )
    row = next(
        item
        for item in db_session.query(HermesBrowserBridge).all()
        if is_jimeng_lab_slot(item)
    )
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "jimeng_capture_state": "ready",
            "jimeng_session_ciphertext": "enc:v1:test",
            "jimeng_test_id": "jt_existing",
            "jimeng_test_state": "waiting_upstream",
            "jimeng_test_result": {"upstream_history_id": "38295459566860"},
        }
    )
    row.meta_json = meta
    db_session.add(row)
    db_session.flush()

    current, dispatch_required = queue_jimeng_lab_test(
        db_session,
        workspace_id=3,
        user_id=101,
        capture_id=session["session_id"],
        prompt="must not submit again",
        model="jimeng-video-seedance-2.0-fast",
    )

    assert dispatch_required is False
    assert current["test"]["id"] == "jt_existing"
    assert current["test"]["state"] == "waiting_upstream"
    assert current["test"]["upstream_history_id"] == "38295459566860"
