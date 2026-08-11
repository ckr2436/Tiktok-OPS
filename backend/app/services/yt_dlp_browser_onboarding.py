from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services import video_site_cookies


SITE_CONFIG = {
    "tiktok": {
        "url": "https://www.tiktok.com/login",
        "hosts": ["www.tiktok.com", "tiktok.com"],
        "domains": ["tiktok.com"],
        "required": ["sessionid", "sessionid_ss", "sid_tt"],
    },
    "douyin": {
        "url": "https://www.douyin.com/",
        "hosts": ["www.douyin.com", "douyin.com"],
        "domains": ["douyin.com"],
        "required": ["sessionid", "sessionid_ss"],
    },
    "youtube": {
        "url": "https://www.youtube.com/",
        "hosts": ["www.youtube.com", "youtube.com"],
        "domains": ["youtube.com", "google.com"],
        "required": ["SAPISID", "SID", "__Secure-1PSID", "__Secure-3PSID"],
    },
    "kuaishou": {
        "url": "https://www.kuaishou.com/",
        "hosts": ["kuaishou.com", "gifshow.com", "kwai.com"],
        "domains": ["kuaishou.com", "gifshow.com", "kwai.com"],
        "required": ["kuaishou.server.web_st", "kuaishou.server.web_ph", "did"],
    },
    "facebook": {
        "url": "https://www.facebook.com/login/",
        "hosts": ["facebook.com"], "domains": ["facebook.com"],
        "required": ["c_user", "xs"],
    },
    "instagram": {
        "url": "https://www.instagram.com/accounts/login/",
        "hosts": ["instagram.com"], "domains": ["instagram.com"],
        "required": ["sessionid"],
    },
    "twitter": {
        "url": "https://x.com/i/flow/login",
        "hosts": ["x.com", "twitter.com"], "domains": ["x.com", "twitter.com"],
        "required": ["auth_token"],
    },
    "bilibili": {
        "url": "https://passport.bilibili.com/login",
        "hosts": ["bilibili.com"], "domains": ["bilibili.com"],
        "required": ["SESSDATA"],
    },
    "xiaohongshu": {
        "url": "https://www.xiaohongshu.com/explore",
        "hosts": ["xiaohongshu.com"], "domains": ["xiaohongshu.com"],
        "required": ["web_session"],
    },
    "weibo": {
        "url": "https://weibo.com/",
        "hosts": ["weibo.com", "weibo.cn", "sina.com.cn"],
        "domains": ["weibo.com", "weibo.cn", "sina.com.cn"],
        "required": ["SUB"],
    },
    "vimeo": {
        "url": "https://vimeo.com/log_in",
        "hosts": ["vimeo.com"], "domains": ["vimeo.com"],
        "required": ["vimeo"],
    },
    "reddit": {
        "url": "https://www.reddit.com/login/",
        "hosts": ["reddit.com"], "domains": ["reddit.com"],
        "required": ["reddit_session"],
    },
    "twitch": {
        "url": "https://www.twitch.tv/login",
        "hosts": ["twitch.tv"], "domains": ["twitch.tv"],
        "required": ["auth-token"],
    },
    "dailymotion": {
        "url": "https://www.dailymotion.com/signin",
        "hosts": ["dailymotion.com"], "domains": ["dailymotion.com"],
        "required": ["access_token", "client_token"],
    },
    "pinterest": {
        "url": "https://www.pinterest.com/login/",
        "hosts": ["pinterest.com"], "domains": ["pinterest.com"],
        "required": ["_auth", "_pinterest_sess"],
    },
    "linkedin": {
        "url": "https://www.linkedin.com/login",
        "hosts": ["linkedin.com"], "domains": ["linkedin.com"],
        "required": ["li_at"],
    },
    "nicovideo": {
        "url": "https://account.nicovideo.jp/login",
        "hosts": ["nicovideo.jp"], "domains": ["nicovideo.jp"],
        "required": ["user_session"],
    },
    "youku": {
        "url": "https://www.youku.com/",
        "hosts": ["youku.com"], "domains": ["youku.com"],
        "required": ["P_sck", "P_pck"],
    },
    "iqiyi": {
        "url": "https://www.iqiyi.com/",
        "hosts": ["iqiyi.com", "iq.com"], "domains": ["iqiyi.com", "iq.com"],
        "required": ["P00001"],
    },
}
KEEPALIVE_HOURS = {
    "tiktok": 12, "douyin": 12, "xiaohongshu": 12, "kuaishou": 12,
    "youtube": 24, "facebook": 24, "instagram": 24, "twitter": 24,
    "bilibili": 48, "weibo": 48, "vimeo": 72, "reddit": 48,
    "twitch": 48, "dailymotion": 72, "pinterest": 48, "linkedin": 72,
    "nicovideo": 72, "youku": 48, "iqiyi": 48,
}
KEEPALIVE_CAPTURE_TIMEOUT = timedelta(minutes=8)
_ACTIVE_STATES = {"awaiting_login", "capture_pending"}
_COOKIE_NAME_RE = re.compile(r"^[^\s;=]{1,256}$")


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _next_keepalive_at(site: str, now: datetime, expires_at: datetime | None = None) -> datetime:
    scheduled = now + timedelta(hours=int(KEEPALIVE_HOURS.get(site, 48)))
    if expires_at is not None:
        scheduled = min(scheduled, max(now + timedelta(minutes=15), expires_at - timedelta(hours=2)))
    return scheduled


def is_yt_dlp_account_slot(row: HermesBrowserBridge) -> bool:
    return bool(dict(row.meta_json or {}).get("yt_dlp_account_slot"))


def _safe_session(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    return {
        "session_id": meta.get("yt_dlp_capture_id"),
        "bridge_id": str(row.bridge_id),
        "device_id": meta.get("agent_device_id"),
        "device_name": row.device_name,
        "site": meta.get("yt_dlp_site"),
        "label": meta.get("yt_dlp_label"),
        "state": meta.get("yt_dlp_capture_state") or "unknown",
        "message": meta.get("yt_dlp_capture_message"),
        "error": meta.get("yt_dlp_capture_error"),
        "browser_status": meta.get("yt_dlp_browser_status"),
        "profile_id": meta.get("yt_dlp_profile_id"),
        "cookie_id": meta.get("yt_dlp_cookie_id"),
        "cookie_count": int(meta.get("yt_dlp_cookie_count") or 0),
        "created_at": meta.get("yt_dlp_capture_created_at"),
        "updated_at": meta.get("yt_dlp_capture_updated_at"),
    }


def list_yt_dlp_browser_sessions(db: Session, *, workspace_id: int, user_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        .order_by(HermesBrowserBridge.id.desc())
        .all()
    )
    return [_safe_session(row) for row in rows if is_yt_dlp_account_slot(row)]


def _row_for_capture(db: Session, *, workspace_id: int, user_id: int, capture_id: str) -> HermesBrowserBridge:
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        .order_by(HermesBrowserBridge.id.desc())
        .all()
    )
    row = next(
        (
            item for item in rows
            if is_yt_dlp_account_slot(item)
            and str(dict(item.meta_json or {}).get("yt_dlp_capture_id") or "") == str(capture_id)
        ),
        None,
    )
    if row is None:
        raise APIError("YT_DLP_BROWSER_SESSION_NOT_FOUND", "Cookies 浏览器登录会话不存在。", 404)
    return row


def start_yt_dlp_browser_session(
    db: Session, *, workspace_id: int, user_id: int, device_id: str, site: str, label: str
) -> dict[str, Any]:
    from app.services.hermes_agent.content_factory import (
        BRIDGE_AGENT_VERSION,
        _agent_rows,
        _bridge_agent_recent,
        _bridge_device_bound,
        _next_agent_profile_slot_index,
        _new_agent_slot,
    )

    site = str(site or "").strip().lower()
    label = str(label or "").strip()
    if site not in SITE_CONFIG:
        raise APIError("YT_DLP_SITE_INVALID", "不支持该视频站点。", 422)
    device = str(device_id or "").strip()
    rows = _agent_rows(db, workspace_id=int(workspace_id), user_id=int(user_id), device_id=device)
    if not rows or not any(_bridge_device_bound(row) and _bridge_agent_recent(row) for row in rows):
        raise APIError("YT_DLP_DEVICE_OFFLINE", "请选择当前在线且已绑定的 Windows 浏览器桥设备。", 409)
    if not any(
        _bridge_device_bound(row)
        and _bridge_agent_recent(row)
        and str(dict(row.meta_json or {}).get("agent_version") or "") == BRIDGE_AGENT_VERSION
        for row in rows
    ):
        raise APIError(
            "YT_DLP_AGENT_UPDATE_REQUIRED",
            "该设备的 Windows 浏览器桥版本过旧，请更新或重新下载运行后再抓取 Cookies。",
            409,
        )
    active = next(
        (
            row for row in rows
            if is_yt_dlp_account_slot(row)
            and str(dict(row.meta_json or {}).get("yt_dlp_capture_state") or "") in _ACTIVE_STATES
        ),
        None,
    )
    if active is not None:
        return _safe_session(active)

    row = next(
        (
            item for item in reversed(rows)
            if is_yt_dlp_account_slot(item)
            and str(dict(item.meta_json or {}).get("yt_dlp_site") or "") == site
            and str(dict(item.meta_json or {}).get("yt_dlp_label") or "") == label
        ),
        None,
    )
    if row is None:
        slot_index = _next_agent_profile_slot_index(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            device_id=device,
            error_code="YT_DLP_PROFILE_CAPACITY_FULL",
        )
        sample = rows[-1]
        row = _new_agent_slot(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            device_id=device,
            device_name=str(sample.device_name or "Windows device"),
            inbox_root=str(sample.inbox_root or ""),
            slot_index=slot_index,
        )
    now = _now()
    capture_id = "ytdlp_" + secrets.token_hex(20)
    meta = dict(row.meta_json or {})
    meta.update({
        "yt_dlp_account_slot": True,
        "yt_dlp_capture_id": capture_id,
        "yt_dlp_capture_state": "awaiting_login",
        "yt_dlp_capture_created_at": now.isoformat(),
        "yt_dlp_capture_updated_at": now.isoformat(),
        "yt_dlp_capture_message": "请在打开的 Chrome 中完成登录，确认站点主页已登录后关闭整个窗口。",
        "yt_dlp_capture_error": None,
        "yt_dlp_browser_status": "starting",
        "yt_dlp_site": site,
        "yt_dlp_label": label,
        "yt_dlp_target_url": SITE_CONFIG[site]["url"],
    })
    row.meta_json = meta
    row.status = "pending"
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    row.last_seen_at = now
    db.add(row)
    db.flush()
    return _safe_session(row)


def cancel_yt_dlp_browser_session(db: Session, *, workspace_id: int, user_id: int, capture_id: str) -> dict[str, Any]:
    row = _row_for_capture(db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id)
    meta = dict(row.meta_json or {})
    if str(meta.get("yt_dlp_capture_state") or "") in _ACTIVE_STATES:
        now = _now()
        meta.update({
            "yt_dlp_capture_state": "cancelled",
            "yt_dlp_capture_updated_at": now.isoformat(),
            "yt_dlp_capture_message": "Cookies 浏览器登录已取消。",
        })
        row.meta_json = meta
        row.status = "standby"
        row.load_json = {}
        db.add(row)
    return _safe_session(row)


def get_yt_dlp_browser_session(db: Session, *, workspace_id: int, user_id: int, capture_id: str) -> dict[str, Any]:
    return _safe_session(_row_for_capture(db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id))


def yt_dlp_slot_should_wake(row: HermesBrowserBridge, *, now: datetime) -> bool:
    del now
    meta = dict(row.meta_json or {})
    return is_yt_dlp_account_slot(row) and str(meta.get("yt_dlp_capture_state") or "") in _ACTIVE_STATES


def yt_dlp_slot_spec(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    state = str(meta.get("yt_dlp_capture_state") or "")
    site = str(meta.get("yt_dlp_site") or "")
    config = SITE_CONFIG.get(site) or SITE_CONFIG["tiktok"]
    return {
        "purpose": "yt_dlp_account",
        "target_url": str(meta.get("yt_dlp_target_url") or config["url"]),
        "capture_id": str(meta.get("yt_dlp_capture_id") or ""),
        "capture_required": state == "capture_pending",
        "login_only": state == "awaiting_login",
        "flow_token_id": None,
        "proxy_url": "",
        "cookie_page_hosts": list(config["hosts"]),
        "cookie_domains": list(config["domains"]),
        "cookie_names": list(config["required"]),
    }


def record_yt_dlp_browser_report(row: HermesBrowserBridge, report: dict[str, Any], *, now: datetime) -> bool:
    meta = dict(row.meta_json or {})
    expected_capture_id = str(meta.get("yt_dlp_capture_id") or "")
    reported_capture_id = str(report.get("capture_id") or "")
    if reported_capture_id and expected_capture_id and reported_capture_id != expected_capture_id:
        # The Agent may report the final heartbeat of the previous browser
        # cycle after a replacement capture has already been issued.  Never
        # let that stale report advance or fail the current capture.
        return False
    status = str(report.get("flow_status") or "checking").strip().lower()
    meta["yt_dlp_browser_status"] = status
    if status == "login_complete" and str(meta.get("yt_dlp_capture_state") or "") == "awaiting_login":
        meta.update({
            "yt_dlp_capture_state": "capture_pending",
            "yt_dlp_capture_updated_at": now.isoformat(),
            "yt_dlp_capture_message": "登录窗口已关闭，正在从同一固定 Profile 采集站点 Cookies。",
        })
    elif status == "login_required" and str(meta.get("yt_dlp_capture_state") or "") == "capture_pending":
        # Automatic keepalive probes are one-shot and invisible.  A logged-out
        # fixed Profile is a terminal human-action state, not a reason to keep
        # opening Chrome on every maintenance tick.
        keepalive = bool(meta.get("yt_dlp_keepalive"))
        meta.update({
            "yt_dlp_capture_state": "reauth_required",
            "yt_dlp_capture_updated_at": now.isoformat(),
            "yt_dlp_capture_message": (
                "自动保活检测到账号已退出登录，需要人工重新登录。"
                if keepalive else "未检测到有效登录，需要重新登录后再采集 Cookies。"
            ),
            "yt_dlp_capture_error": "browser_session_not_authenticated",
            "yt_dlp_keepalive": False,
        })
    row.meta_json = meta
    return True


def _normalize_cookies(site: str, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = SITE_CONFIG[site]["domains"]
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    total_bytes = 0
    for item in values[:200]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip().lower().lstrip(".")
        path = str(item.get("path") or "/")[:255]
        if not name or not value or not _COOKIE_NAME_RE.fullmatch(name):
            continue
        if not any(domain == root or domain.endswith("." + root) for root in allowed):
            continue
        key = (name, domain, path)
        if key in seen:
            continue
        total_bytes += len(value.encode("utf-8"))
        if total_bytes > 256_000:
            raise APIError("YT_DLP_COOKIES_TOO_LARGE", "采集到的 Cookies 超过安全大小限制。", 400)
        seen.add(key)
        result.append({
            "name": name,
            "value": value,
            "domain": "." + domain,
            "path": path,
            "secure": bool(item.get("secure")),
            "httpOnly": bool(item.get("http_only") or item.get("httpOnly")),
            "expirationDate": float(item.get("expires") or 0),
        })
    return result


def ingest_yt_dlp_browser_capture(
    db: Session, *, workspace_id: int, user_id: int, device_id: str, bridge_id: str,
    capture_id: str, session_cookies: list[dict[str, Any]], profile_id: str
) -> dict[str, Any]:
    row = _row_for_capture(db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id)
    meta = dict(row.meta_json or {})
    expected_profile = f"{device_id}/slot-{int(meta.get('local_port') or 0)}"
    if str(row.bridge_id) != str(bridge_id) or str(meta.get("agent_device_id") or "") != str(device_id):
        raise APIError("YT_DLP_CAPTURE_SCOPE_MISMATCH", "Cookies 抓取不属于该设备或 Slot。", 403)
    if str(profile_id) != expected_profile:
        raise APIError("YT_DLP_CAPTURE_PROFILE_MISMATCH", "Cookies 抓取 Profile 不匹配。", 409)
    if str(meta.get("yt_dlp_capture_state") or "") == "ready":
        return {"success": True}
    if str(meta.get("yt_dlp_capture_state") or "") != "capture_pending":
        raise APIError("YT_DLP_CAPTURE_STATE_INVALID", "Cookies 抓取状态已失效。", 409)
    site = str(meta.get("yt_dlp_site") or "")
    cookies = _normalize_cookies(site, session_cookies)
    names = {str(item["name"]).lower() for item in cookies}
    if not cookies or not any(name.lower() in names for name in SITE_CONFIG[site]["required"]):
        raise APIError("YT_DLP_LOGIN_NOT_CONFIRMED", "未检测到有效登录 Cookies，请确认登录成功后再关闭窗口。", 400)
    required_names = {str(name).lower() for name in SITE_CONFIG[site]["required"]}
    expiries = [
        float(item.get("expirationDate") or 0)
        for item in cookies
        if str(item.get("name") or "").lower() in required_names
        and float(item.get("expirationDate") or 0) > 0
    ]
    expires_at = datetime.utcfromtimestamp(min(expiries)) if expiries else None
    now = _now()
    existing = video_site_cookies.get_cookie_by_site_label(
        db, site=site, label=str(meta.get("yt_dlp_label") or "Browser account")
    )
    extra = dict(existing.extra or {}) if existing is not None else {}
    extra.update({
        "source": "hermes_bridge_cdp",
        "profile_id": expected_profile,
        "device_id": str(device_id),
        "bridge_id": str(bridge_id),
        "health_status": "healthy",
        "last_verified_at": now.isoformat(),
        "next_keepalive_at": _next_keepalive_at(site, now, expires_at).isoformat(),
        "keepalive_failure_count": 0,
        "keepalive_error": None,
        "reauth_required": False,
    })
    record = video_site_cookies.upsert_video_site_cookies(
        db,
        site=site,
        label=str(meta.get("yt_dlp_label") or "Browser account"),
        cookies_json=json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
        is_active=True,
        last_login_at=now,
        expires_at=expires_at,
        extra=extra,
    )
    db.flush()
    meta.update({
        "yt_dlp_capture_state": "ready",
        "yt_dlp_capture_updated_at": now.isoformat(),
        "yt_dlp_capture_message": f"已安全采集并保存 {len(cookies)} 项 Cookies。",
        "yt_dlp_capture_error": None,
        "yt_dlp_profile_id": expected_profile,
        "yt_dlp_cookie_id": str(record.id),
        "yt_dlp_cookie_count": len(cookies),
        "yt_dlp_keepalive": False,
        "yt_dlp_keepalive_started_at": None,
    })
    row.meta_json = meta
    row.status = "standby"
    row.load_json = {}
    db.add(row)
    return {"success": True}


def reconcile_cookie_keepalives(
    db: Session, *, now: datetime | None = None, limit: int = 4, force_cookie_id: str | None = None
) -> dict[str, int]:
    """Wake fixed browser Profiles only when due; never automate credentials or MFA."""
    from app.services.hermes_agent.content_factory import _bridge_agent_recent

    now = now or _now()
    stats = {"scheduled": 0, "timed_out": 0, "reauth_required": 0, "skipped": 0}
    rows = db.query(HermesBrowserBridge).filter(HermesBrowserBridge.status != "retired").order_by(HermesBrowserBridge.id.asc()).all()
    slots_by_cookie = {
        str(dict(row.meta_json or {}).get("yt_dlp_cookie_id") or ""): row
        for row in rows if is_yt_dlp_account_slot(row)
    }
    used_devices: set[str] = set()
    records = list(video_site_cookies.list_cookies(db))
    for record in records:
        if force_cookie_id and str(record.id) != str(force_cookie_id):
            continue
        if not record.is_active:
            continue
        extra = dict(record.extra or {})
        if extra.get("source") != "hermes_bridge_cdp":
            continue
        slot = slots_by_cookie.get(str(record.id))
        if slot is None:
            extra.update({"health_status": "reauth_required", "reauth_required": True, "keepalive_error": "fixed_profile_missing"})
            record.extra = extra
            db.add(record)
            stats["reauth_required"] += 1
            continue
        meta = dict(slot.meta_json or {})
        state = str(meta.get("yt_dlp_capture_state") or "")
        if state == "reauth_required" or bool(extra.get("reauth_required")):
            # Human-action states are terminal for automatic maintenance.  The
            # explicit login endpoint creates a new capture cycle after the
            # operator chooses to reauthenticate this exact account.
            extra.update({
                "health_status": "reauth_required",
                "reauth_required": True,
                "keepalive_error": str(
                    meta.get("yt_dlp_capture_error")
                    or extra.get("keepalive_error")
                    or "browser_session_not_authenticated"
                ),
            })
            record.extra = extra
            slot.status = "standby"
            slot.load_json = {}
            db.add_all([record, slot])
            stats["reauth_required"] += 1
            continue
        started = _parse_time(meta.get("yt_dlp_keepalive_started_at"))
        if state == "capture_pending" and bool(meta.get("yt_dlp_keepalive")) and started and now - started >= KEEPALIVE_CAPTURE_TIMEOUT:
            failures = int(extra.get("keepalive_failure_count") or 0) + 1
            extra.update({
                "health_status": "reauth_required", "reauth_required": True,
                "keepalive_failure_count": failures, "keepalive_error": "browser_session_not_authenticated",
                "last_keepalive_failed_at": now.isoformat(),
            })
            meta.update({
                "yt_dlp_capture_state": "reauth_required", "yt_dlp_capture_updated_at": now.isoformat(),
                "yt_dlp_capture_message": "自动保活未检测到有效登录，需要人工重新登录。",
                "yt_dlp_capture_error": "browser_session_not_authenticated", "yt_dlp_keepalive": False,
            })
            record.extra = extra
            slot.meta_json = meta
            slot.status = "standby"
            slot.load_json = {}
            db.add_all([record, slot])
            stats["timed_out"] += 1
            continue
        if state in _ACTIVE_STATES:
            stats["skipped"] += 1
            continue
        due = _parse_time(extra.get("next_keepalive_at"))
        if due is None:
            base = record.last_login_at or record.updated_at or now
            due = _next_keepalive_at(record.site, base, record.expires_at)
            extra["next_keepalive_at"] = due.isoformat()
            extra.setdefault("health_status", "healthy")
            record.extra = extra
            db.add(record)
        forced = bool(force_cookie_id and str(record.id) == str(force_cookie_id))
        if not forced and (due > now or stats["scheduled"] >= max(1, int(limit))):
            continue
        device_id = str(meta.get("agent_device_id") or "")
        if device_id in used_devices or not _bridge_agent_recent(slot):
            stats["skipped"] += 1
            continue
        capture_id = "ytdlp_keepalive_" + secrets.token_hex(16)
        meta.update({
            "yt_dlp_capture_id": capture_id, "yt_dlp_capture_state": "capture_pending",
            "yt_dlp_capture_created_at": now.isoformat(), "yt_dlp_capture_updated_at": now.isoformat(),
            "yt_dlp_capture_message": "正在使用原固定 Profile 自动验证并续签 Cookies。",
            "yt_dlp_capture_error": None, "yt_dlp_browser_status": "starting",
            "yt_dlp_keepalive": True, "yt_dlp_keepalive_started_at": now.isoformat(),
        })
        extra.update({"health_status": "refreshing", "last_keepalive_started_at": now.isoformat(), "keepalive_error": None})
        slot.meta_json = meta
        slot.status = "pending"
        slot.load_json = {}
        record.extra = extra
        db.add_all([slot, record])
        used_devices.add(device_id)
        stats["scheduled"] += 1
    return stats


__all__ = [
    "cancel_yt_dlp_browser_session", "get_yt_dlp_browser_session",
    "ingest_yt_dlp_browser_capture", "is_yt_dlp_account_slot",
    "list_yt_dlp_browser_sessions", "record_yt_dlp_browser_report",
    "start_yt_dlp_browser_session", "yt_dlp_slot_should_wake", "yt_dlp_slot_spec",
    "reconcile_cookie_keepalives",
]
