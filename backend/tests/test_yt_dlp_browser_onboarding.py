from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.data.models import VideoSiteCookies
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.hermes_agent.content_factory import _new_agent_slot
from app.services.hermes_agent.content_factory import BRIDGE_AGENT_VERSION
from app.services.jimeng_lab import is_external_account_slot
from app.services.yt_dlp_browser_onboarding import (
    SITE_CONFIG,
    ingest_yt_dlp_browser_capture,
    is_yt_dlp_account_slot,
    record_yt_dlp_browser_report,
    reconcile_cookie_keepalives,
    start_yt_dlp_browser_session,
    yt_dlp_slot_spec,
)
from app.services.video_site_cookies import SUPPORTED_SITES
from app.features.tenants.openai_whisper.tasks import _detect_site_from_url


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
        "agent_version": BRIDGE_AGENT_VERSION,
    }
    db_session.add(row)
    db_session.flush()
    return row


def _start(db_session, site="tiktok", label="运营号 A"):
    _online_device(db_session)
    session = start_yt_dlp_browser_session(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        site=site,
        label=label,
    )
    row = next(item for item in db_session.query(HermesBrowserBridge).all() if is_yt_dlp_account_slot(item))
    return session, row


def test_yt_dlp_onboarding_uses_dedicated_profile(db_session):
    session, row = _start(db_session)
    assert session["state"] == "awaiting_login"
    assert row.meta_json["slot_index"] == 1
    assert is_external_account_slot(row)
    spec = yt_dlp_slot_spec(row)
    assert spec["purpose"] == "yt_dlp_account"
    assert spec["login_only"] is True
    assert spec["capture_required"] is False


def test_login_close_advances_exact_profile_to_capture(db_session):
    session, row = _start(db_session, site="youtube", label="YouTube A")
    record_yt_dlp_browser_report(row, {"flow_status": "login_complete"}, now=datetime.now())
    assert row.meta_json["yt_dlp_capture_id"] == session["session_id"]
    assert row.meta_json["yt_dlp_capture_state"] == "capture_pending"
    spec = yt_dlp_slot_spec(row)
    assert spec["login_only"] is False
    assert spec["capture_required"] is True


def test_capture_filters_domains_and_persists_without_secret_metadata(db_session):
    session, row = _start(db_session)
    record_yt_dlp_browser_report(row, {"flow_status": "login_complete"}, now=datetime.now())
    result = ingest_yt_dlp_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
        session_cookies=[
            {"name": "sessionid", "value": "secret-session", "domain": ".tiktok.com", "path": "/", "secure": True},
            {"name": "csrftoken", "value": "secret-csrf", "domain": "www.tiktok.com", "path": "/"},
            {"name": "SID", "value": "must-not-leak", "domain": ".google.com", "path": "/"},
        ],
    )
    assert result == {"success": True}
    record = db_session.query(VideoSiteCookies).one()
    assert record.site == "tiktok"
    assert "secret-session" in record.cookies_json
    assert "secret-csrf" in record.cookies_json
    assert "must-not-leak" not in record.cookies_json
    assert "secret-session" not in str(row.meta_json)
    assert row.meta_json["yt_dlp_cookie_count"] == 2
    assert record.extra["health_status"] == "healthy"
    assert record.extra["next_keepalive_at"]


def test_capture_rejects_wrong_profile_and_missing_login_cookie(db_session):
    session, row = _start(db_session)
    record_yt_dlp_browser_report(row, {"flow_status": "login_complete"}, now=datetime.now())
    with pytest.raises(Exception) as wrong_profile:
        ingest_yt_dlp_browser_capture(
            db_session,
            workspace_id=3,
            user_id=101,
            device_id="windows-a",
            bridge_id=row.bridge_id,
            capture_id=session["session_id"],
            profile_id="windows-a/slot-9999",
            session_cookies=[],
        )
    assert "Profile" in str(wrong_profile.value)

    with pytest.raises(Exception) as missing_login:
        ingest_yt_dlp_browser_capture(
            db_session,
            workspace_id=3,
            user_id=101,
            device_id="windows-a",
            bridge_id=row.bridge_id,
            capture_id=session["session_id"],
            profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
            session_cookies=[{"name": "csrftoken", "value": "x", "domain": ".tiktok.com", "path": "/"}],
        )
    assert "登录 Cookies" in str(missing_login.value)


def test_agent_watchdog_retries_yt_dlp_probe_instead_of_chatgpt_fallback():
    source = (Path(__file__).resolve().parents[2] / "hermes-bridge-agent" / "main.go").read_text(encoding="utf-8")
    watchdog = source[source.index("case <-watchdog.C:") : source.index("if waitCDP(cdpPort", source.index("case <-watchdog.C:"))]
    assert 'spec.Purpose == "yt_dlp_account"' in watchdog
    assert "a.refreshYtDlpSession(slot, cdpPort)" in watchdog


def test_all_cookie_platforms_have_bounded_capture_rules_and_download_detection():
    expected = {
        "tiktok", "douyin", "youtube", "kuaishou", "facebook", "instagram",
        "twitter", "bilibili", "xiaohongshu", "weibo", "vimeo", "reddit",
        "twitch", "dailymotion", "pinterest", "linkedin", "nicovideo", "youku", "iqiyi",
    }
    assert set(SITE_CONFIG) == expected == SUPPORTED_SITES
    for site, config in SITE_CONFIG.items():
        assert str(config["url"]).startswith("https://")
        assert config["hosts"]
        assert config["domains"]
        assert config["required"]
        assert _detect_site_from_url(config["url"]) == site


def test_keepalive_reuses_fixed_profile_then_times_out_to_manual_reauth(db_session):
    session, row = _start(db_session)
    record_yt_dlp_browser_report(row, {"flow_status": "login_complete"}, now=datetime.now())
    ingest_yt_dlp_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
        session_cookies=[{"name": "sessionid", "value": "secret", "domain": ".tiktok.com", "path": "/"}],
    )
    record = db_session.query(VideoSiteCookies).one()
    first_profile = row.meta_json["yt_dlp_profile_id"]
    now = datetime.now()
    stats = reconcile_cookie_keepalives(db_session, now=now, force_cookie_id=record.id)
    assert stats["scheduled"] == 1
    assert row.meta_json["yt_dlp_capture_state"] == "capture_pending"
    assert row.meta_json["yt_dlp_keepalive"] is True
    assert row.meta_json["yt_dlp_profile_id"] == first_profile
    assert record.extra["health_status"] == "refreshing"

    stats = reconcile_cookie_keepalives(db_session, now=now.replace(microsecond=0) + timedelta(minutes=9))
    assert stats["timed_out"] == 1
    assert row.meta_json["yt_dlp_capture_state"] == "reauth_required"
    assert record.extra["reauth_required"] is True
    assert record.extra["health_status"] == "reauth_required"


def test_keepalive_login_required_is_terminal_and_not_rescheduled(db_session):
    session, row = _start(db_session)
    record_yt_dlp_browser_report(row, {"flow_status": "login_complete"}, now=datetime.now())
    ingest_yt_dlp_browser_capture(
        db_session,
        workspace_id=3,
        user_id=101,
        device_id="windows-a",
        bridge_id=row.bridge_id,
        capture_id=session["session_id"],
        profile_id=f"windows-a/slot-{row.meta_json['local_port']}",
        session_cookies=[{"name": "sessionid", "value": "secret", "domain": ".tiktok.com", "path": "/"}],
    )
    record = db_session.query(VideoSiteCookies).one()
    now = datetime.now()
    reconcile_cookie_keepalives(db_session, now=now, force_cookie_id=record.id)
    capture_id = row.meta_json["yt_dlp_capture_id"]

    accepted = record_yt_dlp_browser_report(
        row,
        {"capture_id": capture_id, "flow_status": "login_required"},
        now=now + timedelta(seconds=10),
    )
    assert accepted is True
    assert row.meta_json["yt_dlp_capture_state"] == "reauth_required"
    assert row.meta_json["yt_dlp_keepalive"] is False

    stats = reconcile_cookie_keepalives(
        db_session,
        now=now + timedelta(hours=24),
        force_cookie_id=record.id,
    )
    assert stats["scheduled"] == 0
    assert stats["reauth_required"] == 1
    assert row.status == "standby"
    assert record.extra["reauth_required"] is True


def test_stale_yt_dlp_report_cannot_fail_replacement_capture(db_session):
    session, row = _start(db_session)
    current_id = session["session_id"]
    accepted = record_yt_dlp_browser_report(
        row,
        {"capture_id": "older-capture", "flow_status": "login_required"},
        now=datetime.now(),
    )
    assert accepted is False
    assert row.meta_json["yt_dlp_capture_id"] == current_id
    assert row.meta_json["yt_dlp_capture_state"] == "awaiting_login"
