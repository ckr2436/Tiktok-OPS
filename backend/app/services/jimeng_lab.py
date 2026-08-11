from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow_proxy_pool import resolve_flow_proxy_url
from app.services.ai_video.accounts import decrypt_api_key, encrypt_api_key


JIMENG_LAB_URL = "https://jimeng.jianying.com/ai-tool/generate?type=video"
_ACTIVE_STATES = {"awaiting_login", "capture_pending"}
_TERMINAL_STATES = {"ready", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def is_jimeng_lab_slot(row: HermesBrowserBridge) -> bool:
    return bool(dict(row.meta_json or {}).get("jimeng_lab_slot"))


def is_external_account_slot(row: HermesBrowserBridge) -> bool:
    meta = dict(row.meta_json or {})
    return bool(
        meta.get("flow_account_slot")
        or meta.get("jimeng_lab_slot")
        or meta.get("doubao_lab_slot")
        or meta.get("yt_dlp_account_slot")
    )


def _safe_session(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    result = dict(meta.get("jimeng_test_result") or {})
    return {
        "session_id": meta.get("jimeng_capture_id"),
        "bridge_id": str(row.bridge_id),
        "device_id": meta.get("agent_device_id"),
        "device_name": row.device_name,
        "state": meta.get("jimeng_capture_state") or "unknown",
        "message": meta.get("jimeng_capture_message"),
        "error": meta.get("jimeng_capture_error"),
        "browser_status": meta.get("jimeng_browser_status"),
        "profile_id": meta.get("jimeng_profile_id"),
        "fingerprint_state": meta.get("jimeng_fingerprint_state") or "missing",
        "proxy_id": meta.get("jimeng_proxy_id"),
        "credential_state": "encrypted"
        if meta.get("jimeng_session_context_ciphertext")
        or meta.get("jimeng_session_ciphertext")
        else "missing",
        "last_verified_at": meta.get("jimeng_last_verified_at"),
        "created_at": meta.get("jimeng_capture_created_at"),
        "updated_at": meta.get("jimeng_capture_updated_at"),
        "test": {
            "id": meta.get("jimeng_test_id"),
            "state": meta.get("jimeng_test_state") or "idle",
            "message": meta.get("jimeng_test_message"),
            "error": meta.get("jimeng_test_error"),
            "prompt": meta.get("jimeng_test_prompt"),
            "model": meta.get("jimeng_test_model"),
            "duration": meta.get("jimeng_test_duration"),
            "video_url": result.get("video_url"),
            "upstream_history_id": result.get("upstream_history_id"),
            "started_at": meta.get("jimeng_test_started_at"),
            "completed_at": meta.get("jimeng_test_completed_at"),
        },
    }


def list_jimeng_lab_sessions(
    db: Session, *, workspace_id: int, user_id: int
) -> list[dict[str, Any]]:
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
    return [_safe_session(row) for row in rows if is_jimeng_lab_slot(row)]


def _row_for_capture(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    include_retired: bool = False,
) -> HermesBrowserBridge:
    query = db.query(HermesBrowserBridge).filter(
        HermesBrowserBridge.workspace_id == int(workspace_id),
        HermesBrowserBridge.user_id == int(user_id),
    )
    if not include_retired:
        query = query.filter(HermesBrowserBridge.status != "retired")
    row = next(
        (
            item
            for item in query.order_by(HermesBrowserBridge.id.desc()).all()
            if is_jimeng_lab_slot(item)
            and str(dict(item.meta_json or {}).get("jimeng_capture_id") or "")
            == str(capture_id)
        ),
        None,
    )
    if row is None:
        raise APIError("JIMENG_LAB_SESSION_NOT_FOUND", "即梦实验登录会话不存在。", 404)
    return row


def start_jimeng_lab_onboarding(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    proxy_id: int,
) -> dict[str, Any]:
    from app.services.hermes_agent.content_factory import (
        _agent_rows,
        _bridge_agent_recent,
        _bridge_device_bound,
        _next_agent_profile_slot_index,
        _new_agent_slot,
    )

    # Resolve once before reserving a profile. This validates that the proxy is
    # enabled, while the plaintext itself is never persisted in bridge JSON.
    try:
        resolve_flow_proxy_url(db, int(proxy_id), require_active=True)
    except ValueError as exc:
        raise APIError("JIMENG_LAB_PROXY_INVALID", str(exc), 409) from exc

    device = str(device_id or "").strip()
    rows = _agent_rows(
        db, workspace_id=int(workspace_id), user_id=int(user_id), device_id=device
    )
    if not rows or not any(
        _bridge_device_bound(row) and _bridge_agent_recent(row) for row in rows
    ):
        raise APIError(
            "JIMENG_LAB_DEVICE_OFFLINE",
            "请选择当前在线且已绑定的 Windows 浏览器桥设备。",
            409,
        )
    active = next(
        (
            row
            for row in rows
            if is_jimeng_lab_slot(row)
            and str(dict(row.meta_json or {}).get("jimeng_capture_state") or "")
            in _ACTIVE_STATES
        ),
        None,
    )
    if active is not None:
        return _safe_session(active)

    # The lab intentionally owns one reusable account profile per device. A
    # retry or re-login must reopen the same profile rather than slowly
    # allocating orphaned profiles or mixing several accounts in one test UI.
    row = next((item for item in reversed(rows) if is_jimeng_lab_slot(item)), None)
    if row is None:
        slot_index = _next_agent_profile_slot_index(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            device_id=device,
            error_code="JIMENG_LAB_PROFILE_CAPACITY_FULL",
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
    capture_id = "jimeng_" + secrets.token_hex(20)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "jimeng_lab_slot": True,
            "jimeng_capture_id": capture_id,
            "jimeng_capture_state": "awaiting_login",
            "jimeng_capture_created_at": now.isoformat(),
            "jimeng_capture_updated_at": now.isoformat(),
            "jimeng_capture_message": (
                "请在已打开的 Chrome 中登录即梦，并确认视频生成页面可访问后关闭整个窗口。"
            ),
            "jimeng_capture_error": None,
            "jimeng_browser_status": "starting",
            "jimeng_target_url": JIMENG_LAB_URL,
            "jimeng_proxy_id": int(proxy_id),
            "jimeng_session_ciphertext": None,
            "jimeng_test_state": "idle",
        }
    )
    row.meta_json = meta
    row.status = "pending"
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    row.last_seen_at = now
    db.add(row)
    db.flush()
    return _safe_session(row)


def cancel_jimeng_lab_onboarding(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> dict[str, Any]:
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        include_retired=True,
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("jimeng_capture_state") or "") not in _TERMINAL_STATES:
        now = _now()
        meta["jimeng_capture_state"] = "cancelled"
        meta["jimeng_capture_updated_at"] = now.isoformat()
        meta["jimeng_capture_message"] = "即梦实验登录已取消。"
        meta["jimeng_profile_retired"] = True
        row.meta_json = meta
        row.status = "retired"
        row.load_json = {}
        db.add(row)
    return _safe_session(row)


def get_jimeng_lab_session(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> dict[str, Any]:
    return _safe_session(
        _row_for_capture(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            capture_id=capture_id,
        )
    )


def jimeng_slot_should_wake(
    db: Session, row: HermesBrowserBridge, *, now: datetime
) -> bool:
    if not is_jimeng_lab_slot(row):
        return False
    meta = dict(row.meta_json or {})
    should_wake = (
        str(row.status or "").lower() != "retired"
        and not bool(meta.get("jimeng_profile_retired"))
        and str(meta.get("jimeng_capture_state") or "") in _ACTIVE_STATES
    )
    if not should_wake:
        return False
    try:
        resolve_flow_proxy_url(
            db, int(meta.get("jimeng_proxy_id") or 0), require_active=True
        )
    except ValueError as exc:
        meta["jimeng_capture_state"] = "failed"
        meta["jimeng_capture_updated_at"] = now.isoformat()
        meta["jimeng_capture_error"] = str(exc)[:500]
        meta["jimeng_capture_message"] = "固定代理不可用，未启动即梦浏览器。"
        row.meta_json = meta
        return False
    return True


def jimeng_slot_spec(db: Session, row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    state = str(meta.get("jimeng_capture_state") or "")
    proxy_id = int(meta.get("jimeng_proxy_id") or 0)
    try:
        proxy_url = resolve_flow_proxy_url(db, proxy_id, require_active=True)
    except ValueError as exc:
        raise APIError("JIMENG_LAB_PROXY_INVALID", str(exc), 409) from exc
    return {
        "purpose": "jimeng_lab",
        "target_url": str(meta.get("jimeng_target_url") or JIMENG_LAB_URL),
        "capture_id": str(meta.get("jimeng_capture_id") or ""),
        "capture_required": state == "capture_pending",
        "login_only": state == "awaiting_login",
        "flow_token_id": None,
        "proxy_url": proxy_url,
    }


def record_jimeng_browser_report(
    row: HermesBrowserBridge, report: dict[str, Any], *, now: datetime
) -> None:
    meta = dict(row.meta_json or {})
    if (
        str(row.status or "").lower() == "retired"
        or bool(meta.get("jimeng_profile_retired"))
        or str(meta.get("jimeng_capture_state") or "") == "cancelled"
    ):
        return
    status = str(report.get("flow_status") or "checking").strip().lower()
    if status not in {
        "starting",
        "checking",
        "login_required",
        "login_complete",
        "capturing",
        "submitted",
        "ready",
    }:
        status = "checking"
    meta["jimeng_browser_status"] = status
    meta["jimeng_browser_checked_at"] = now.isoformat()
    meta["jimeng_page_url"] = str(report.get("page_url") or "")[:1000]
    if (
        status == "login_complete"
        and str(meta.get("jimeng_capture_state") or "") == "awaiting_login"
    ):
        meta["jimeng_capture_state"] = "capture_pending"
        meta["jimeng_capture_updated_at"] = now.isoformat()
        meta["jimeng_capture_message"] = (
            "登录窗口已关闭，正在从同一固定 Profile 安全采集即梦登录态。"
        )
    elif (
        status == "login_required"
        and str(meta.get("jimeng_capture_state") or "") == "capture_pending"
    ):
        meta["jimeng_capture_state"] = "awaiting_login"
        meta["jimeng_capture_updated_at"] = now.isoformat()
        meta["jimeng_capture_message"] = (
            "尚未检测到有效即梦登录。已切回普通 Chrome，请登录后关闭窗口。"
        )
    row.meta_json = meta


def _normalize_fingerprint(value: dict[str, Any]) -> dict[str, str]:
    limits = {
        "user_agent": 512,
        "accept_language": 256,
        "sec_ch_ua": 512,
        "sec_ch_ua_mobile": 16,
        "sec_ch_ua_platform": 64,
        "timezone": 128,
    }
    result: dict[str, str] = {}
    for key, limit in limits.items():
        item = value.get(key) if isinstance(value, dict) else None
        if isinstance(item, str) and item.strip():
            result[key] = item.strip()[:limit]
    if "user_agent" not in result or "sec_ch_ua_platform" not in result:
        raise APIError("JIMENG_LAB_FINGERPRINT_INVALID", "浏览器指纹采集不完整。", 400)
    return result


def _normalize_session_diagnostics(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    login_state = source.get("window_login_state")
    if not isinstance(login_state, bool):
        login_state = None
    result: dict[str, Any] = {
        "window_login_state": login_state,
        "candidate_count": min(max(int(source.get("candidate_count") or 0), 0), 8),
    }
    for key in ("local_storage_keys", "document_cookie_names"):
        raw = source.get(key)
        if isinstance(raw, list):
            result[key] = [
                str(item).strip()[:128]
                for item in raw[:80]
                if isinstance(item, str) and str(item).strip()
            ]
    raw_cookies = source.get("applicable_cookies")
    cookies: list[dict[str, Any]] = []
    if isinstance(raw_cookies, list):
        for item in raw_cookies[:80]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:128]
            domain = str(item.get("domain") or "").strip().lower()[:255]
            if not name or not domain or not (
                domain == "jianying.com" or domain.endswith(".jianying.com")
                or domain == "capcut.com" or domain.endswith(".capcut.com")
            ):
                continue
            cookies.append(
                {
                    "name": name,
                    "domain": domain,
                    "path": str(item.get("path") or "")[:255],
                    "secure": bool(item.get("secure")),
                    "http_only": bool(item.get("http_only")),
                    "expired": bool(item.get("expired")),
                }
            )
    result["applicable_cookies"] = cookies
    return result


_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _normalize_session_cookies(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    total_value_bytes = 0
    for item in value or []:
        if not isinstance(item, dict) or len(cookies) >= 80:
            continue
        name = str(item.get("name") or "").strip()
        raw_value = str(item.get("value") or "").strip()
        raw_domain = str(item.get("domain") or "").strip().lower()
        domain = raw_domain.lstrip(".")
        path = str(item.get("path") or "/").strip()[:255] or "/"
        if (
            not _COOKIE_NAME_RE.fullmatch(name)
            or not raw_value
            or len(raw_value) > 8192
            or not (
                domain == "jianying.com"
                or domain.endswith(".jianying.com")
                or domain == "capcut.com"
                or domain.endswith(".capcut.com")
            )
        ):
            continue
        value_bytes = len(raw_value.encode("utf-8"))
        if total_value_bytes + value_bytes > 48 * 1024:
            break
        total_value_bytes += value_bytes
        cookies.append(
            {
                "name": name,
                "value": raw_value,
                "domain": f".{domain}" if raw_domain.startswith(".") else domain,
                "path": path,
                "secure": bool(item.get("secure")),
                "http_only": bool(item.get("http_only")),
                "expires": float(item.get("expires") or 0),
            }
        )
    return cookies


def _session_context_payload(
    cookies: list[dict[str, Any]], fingerprint: dict[str, str], proxy_url: str
) -> dict[str, Any]:
    cookie_header = "; ".join(
        f"{item['name']}={item['value']}" for item in cookies
    )
    values = {str(item["name"]): str(item["value"]) for item in cookies}
    return {
        "proxy_url": proxy_url,
        "session_id": values.get("sessionid") or values.get("sessionid_ss") or "browser-session",
        "cookie_header": cookie_header,
        "web_id": values.get("_tea_web_id") or "",
        "fingerprint": fingerprint,
        "cookies": cookies,
    }


def decrypt_jimeng_session_context(
    meta: dict[str, Any], proxy_url: str
) -> dict[str, Any] | None:
    """Return the bounded browser context for the loopback reverse service.

    The decrypted payload must only be kept in memory and sent to the local
    JiMeng service. Callers must never persist or log the returned mapping.
    """

    ciphertext = str(meta.get("jimeng_session_context_ciphertext") or "")
    if not ciphertext:
        return None
    context = json.loads(decrypt_api_key(ciphertext))
    cookies = _normalize_session_cookies(context.get("cookies"))
    fingerprint = _normalize_fingerprint(context.get("fingerprint") or {})
    if not cookies:
        raise ValueError("JiMeng browser session contains no usable cookies")
    return _session_context_payload(cookies, fingerprint, proxy_url)


async def _verify_token(token: str, proxy_url: str) -> bool:
    auth_token = f"{proxy_url}@{token}" if proxy_url else token
    base_url = str(settings.JIMENG_LAB_API_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
        response = await client.post(
            f"{base_url}/token/check", json={"token": auth_token}
        )
    response.raise_for_status()
    payload = response.json()
    return bool(payload.get("live")) if isinstance(payload, dict) else False


async def _verify_session_context(
    cookies: list[dict[str, Any]], fingerprint: dict[str, str], proxy_url: str
) -> bool:
    base_url = str(settings.JIMENG_LAB_API_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
        response = await client.post(
            f"{base_url}/token/check-context",
            json=_session_context_payload(cookies, fingerprint, proxy_url),
        )
    response.raise_for_status()
    payload = response.json()
    return bool(payload.get("live")) if isinstance(payload, dict) else False


async def ingest_jimeng_browser_capture(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    bridge_id: str,
    capture_id: str,
    session_token: str,
    session_tokens: list[str] | None = None,
    session_diagnostics: dict[str, Any] | None = None,
    session_cookies: list[dict[str, Any]] | None = None,
    profile_id: str,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    row = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.bridge_id == str(bridge_id),
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None or not is_jimeng_lab_slot(row):
        raise APIError("JIMENG_LAB_SLOT_FORBIDDEN", "即梦实验 Slot 不属于当前设备。", 403)
    meta = dict(row.meta_json or {})
    if str(meta.get("agent_device_id") or "") != str(device_id):
        raise APIError("JIMENG_LAB_DEVICE_MISMATCH", "即梦实验浏览器设备不匹配。", 403)
    if str(meta.get("jimeng_capture_id") or "") != str(capture_id):
        raise APIError("JIMENG_LAB_CAPTURE_STALE", "该即梦登录采集已过期。", 409)
    if str(meta.get("jimeng_capture_state") or "") == "ready":
        return {"success": True}
    candidates: list[str] = []
    for value in [session_token, *(session_tokens or [])]:
        token_candidate = str(value or "").strip()
        if (
            20 <= len(token_candidate) <= 20_000
            and token_candidate not in candidates
            and len(candidates) < 8
        ):
            candidates.append(token_candidate)
    if not candidates:
        raise APIError("JIMENG_LAB_SESSION_INVALID", "浏览器没有返回有效的即梦登录会话。", 400)
    normalized_fingerprint = _normalize_fingerprint(fingerprint)
    normalized_diagnostics = _normalize_session_diagnostics(session_diagnostics)
    normalized_cookies = _normalize_session_cookies(session_cookies)
    expected_profile = f"{device_id}/slot-{int(meta.get('local_port') or 0)}"
    if str(profile_id or "") != expected_profile:
        raise APIError("JIMENG_LAB_PROFILE_MISMATCH", "即梦浏览器 Profile 身份不匹配。", 403)
    try:
        proxy_url = resolve_flow_proxy_url(
            db, int(meta.get("jimeng_proxy_id") or 0), require_active=True
        )
        token = candidates[0]
        live = False
        if normalized_cookies:
            live = await _verify_session_context(
                normalized_cookies, normalized_fingerprint, proxy_url
            )
        if not live:
            for token_candidate in candidates:
                if await _verify_token(token_candidate, proxy_url):
                    token = token_candidate
                    live = True
                    break
    except (ValueError, httpx.HTTPError) as exc:
        live = False
        verify_error = str(exc)[:500]
    else:
        verify_error = None
    now = _now()
    if not live:
        meta["jimeng_capture_state"] = "failed"
        meta["jimeng_capture_updated_at"] = now.isoformat()
        meta["jimeng_capture_error"] = verify_error or "即梦登录态校验失败。"
        meta["jimeng_session_diagnostics"] = normalized_diagnostics
        if normalized_diagnostics.get("window_login_state") is True:
            meta["jimeng_capture_message"] = (
                "网页已登录，但当前即梦逆向接口不兼容这份网页会话。"
            )
        elif normalized_diagnostics.get("window_login_state") is False:
            meta["jimeng_capture_message"] = "即梦网页本身仍是未登录状态，请完成登录后再关闭窗口。"
        else:
            meta["jimeng_capture_message"] = "登录态未通过验证，已记录安全诊断信息。"
        row.meta_json = meta
        row.status = "standby"
        db.add(row)
        return {"success": False, "message": meta["jimeng_capture_message"]}
    meta.update(
        {
            "jimeng_session_ciphertext": encrypt_api_key(token),
            "jimeng_session_context_ciphertext": encrypt_api_key(
                json.dumps(
                    {
                        "cookies": normalized_cookies,
                        "fingerprint": normalized_fingerprint,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if normalized_cookies
            else None,
            "jimeng_profile_id": expected_profile,
            "jimeng_capture_state": "ready",
            "jimeng_capture_updated_at": now.isoformat(),
            "jimeng_capture_message": "即梦登录态已验证，可生成 4 秒实验视频。",
            "jimeng_capture_error": None,
            "jimeng_fingerprint_state": "captured",
            "jimeng_fingerprint_digest": hashlib.sha256(
                repr(sorted(normalized_fingerprint.items())).encode("utf-8")
            ).hexdigest(),
            "jimeng_last_verified_at": now.isoformat(),
            "jimeng_session_diagnostics": normalized_diagnostics,
        }
    )
    row.meta_json = meta
    row.status = "standby"
    row.load_json = {}
    db.add(row)
    return {"success": True}


async def verify_jimeng_lab_session(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> dict[str, Any]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    ciphertext = str(meta.get("jimeng_session_ciphertext") or "")
    context_ciphertext = str(meta.get("jimeng_session_context_ciphertext") or "")
    if not ciphertext and not context_ciphertext:
        raise APIError("JIMENG_LAB_CREDENTIAL_MISSING", "请先完成即梦浏览器登录。", 409)
    try:
        proxy_url = resolve_flow_proxy_url(
            db, int(meta.get("jimeng_proxy_id") or 0), require_active=True
        )
        if context_ciphertext:
            context_payload = decrypt_jimeng_session_context(meta, proxy_url)
            if context_payload is None:
                raise ValueError("JiMeng browser session context is missing")
            live = await _verify_session_context(
                list(context_payload["cookies"]),
                dict(context_payload["fingerprint"]),
                proxy_url,
            )
        else:
            token = decrypt_api_key(ciphertext)
            live = await _verify_token(token, proxy_url)
    except (ValueError, httpx.HTTPError) as exc:
        raise APIError("JIMENG_LAB_VERIFY_FAILED", str(exc)[:500], 502) from exc
    now = _now()
    meta["jimeng_last_verified_at"] = now.isoformat()
    meta["jimeng_capture_updated_at"] = now.isoformat()
    if live:
        meta["jimeng_capture_state"] = "ready"
        meta["jimeng_capture_message"] = "即梦登录态有效。"
        meta["jimeng_capture_error"] = None
    else:
        meta["jimeng_capture_state"] = "failed"
        meta["jimeng_capture_message"] = "即梦登录态已失效，请重新登录。"
        meta["jimeng_capture_error"] = "session_invalid"
    row.meta_json = meta
    db.add(row)
    return _safe_session(row)


def queue_jimeng_lab_test(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    prompt: str,
    model: str,
) -> tuple[dict[str, Any], bool]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("jimeng_capture_state") or "") != "ready" or not meta.get(
        "jimeng_session_ciphertext"
    ):
        raise APIError("JIMENG_LAB_NOT_READY", "请先完成并验证即梦浏览器登录。", 409)
    if str(meta.get("jimeng_test_state") or "") in {
        "queued",
        "running",
        "waiting_upstream",
        "upstream_timeout",
    }:
        return _safe_session(row), False
    test_id = "jt_" + secrets.token_hex(16)
    meta.update(
        {
            "jimeng_test_id": test_id,
            "jimeng_test_state": "queued",
            "jimeng_test_message": "4 秒 Seedance 实验任务已进入后台队列。",
            "jimeng_test_error": None,
            "jimeng_test_prompt": str(prompt).strip()[:2000],
            "jimeng_test_model": str(model).strip()[:128],
            "jimeng_test_duration": 4,
            "jimeng_test_result": None,
            "jimeng_test_started_at": _now().isoformat(),
            "jimeng_test_completed_at": None,
        }
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return _safe_session(row), True


def fail_jimeng_lab_test_dispatch(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    test_id: str,
    error: str,
) -> None:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("jimeng_test_id") or "") != str(test_id):
        return
    meta["jimeng_test_state"] = "failed"
    meta["jimeng_test_message"] = "后台任务派发失败，请稍后重试。"
    meta["jimeng_test_error"] = str(error)[:500]
    meta["jimeng_test_completed_at"] = _now().isoformat()
    row.meta_json = meta
    db.add(row)


__all__ = [
    "JIMENG_LAB_URL",
    "cancel_jimeng_lab_onboarding",
    "get_jimeng_lab_session",
    "fail_jimeng_lab_test_dispatch",
    "ingest_jimeng_browser_capture",
    "is_external_account_slot",
    "is_jimeng_lab_slot",
    "jimeng_slot_should_wake",
    "jimeng_slot_spec",
    "list_jimeng_lab_sessions",
    "queue_jimeng_lab_test",
    "record_jimeng_browser_report",
    "start_jimeng_lab_onboarding",
    "verify_jimeng_lab_session",
]
