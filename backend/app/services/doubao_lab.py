from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow_proxy_pool import resolve_flow_proxy_url
from app.services.ai_video.accounts import decrypt_api_key, encrypt_api_key
from app.services.doubao_provider.membership import (
    membership_payload,
    supports_duration,
)
from app.services.doubao_provider.capability import (
    apply_seedance_capability_result,
    seedance_capability_ready,
    seedance_capability_state,
)
from app.services.doubao_provider.health import (
    AUTH_REQUIRED,
    AUTH_UNKNOWN,
    NETWORK_REACHABLE,
    NETWORK_REGION_RESTRICTED,
    NETWORK_UNKNOWN,
    authentication_is_fresh,
    authentication_state,
    mark_auth_probe_result,
    mark_authenticated,
)
from app.services.doubao_provider.device_health import (
    agent_heartbeat_at,
    agent_is_online,
    device_circuit_is_open,
)


DOUBAO_LAB_URL = "https://www.doubao.com/chat/create-image"
_ACTIVE_STATES = {"awaiting_login", "capture_pending"}
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_CAPTURE_LOGIN_GRACE = timedelta(seconds=20)
_DOUBAO_CHAT_URL_RE = re.compile(r"^https://www\.doubao\.com/chat/(\d{8,128})(?:[/?#].*)?$")
_DOUBAO_REGION_BLOCK_MARKER = "/security/doubao-region-ban"
_MANUAL_VERIFICATION_LEASE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def _future(value: Any, *, now: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed > now


def _parsed_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _provider_request_pending(meta: dict[str, Any], *, now: datetime) -> bool:
    provider_task_id = str(meta.get("doubao_provider_browser_task_id") or "")
    leased_task_id = str(meta.get("doubao_pool_lease_task_id") or "")
    return bool(
        provider_task_id
        and provider_task_id == leased_task_id
        and _future(meta.get("doubao_pool_lease_expires_at"), now=now)
        and (
            not meta.get("doubao_provider_browser_hold_until")
            or _future(meta.get("doubao_provider_browser_hold_until"), now=now)
        )
    )


def is_doubao_lab_slot(row: HermesBrowserBridge) -> bool:
    return bool(dict(row.meta_json or {}).get("doubao_lab_slot"))


def _safe_session(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    result = dict(meta.get("doubao_test_result") or {})
    now = _now()
    auth_state = authentication_state(meta)
    auth_fresh = authentication_is_fresh(meta, now=now)
    pool_enabled = bool(meta.get("doubao_pool_enabled", True))
    leased = bool(meta.get("doubao_pool_lease_task_id"))
    cooling_down = _future(meta.get("doubao_pool_cooldown_until"), now=now)
    capacity_wait = _future(meta.get("doubao_pool_capacity_retry_at"), now=now)
    device_online = agent_is_online(row, now=now)
    device_circuit_open = device_circuit_is_open(row, now=now)
    generation_ready = bool(
        str(meta.get("doubao_capture_state") or "") == "ready"
        and meta.get("doubao_session_context_ciphertext")
        and pool_enabled
        and device_online
        and not device_circuit_open
        and auth_fresh
        and seedance_capability_ready(meta)
        and not leased
        and not cooling_down
        and not capacity_wait
    )
    if str(meta.get("doubao_capture_state") or "") == "captcha_required":
        pool_status = "captcha_required"
    elif auth_state == AUTH_REQUIRED:
        pool_status = "auth_required"
    elif str(meta.get("doubao_network_state") or NETWORK_UNKNOWN) == "region_restricted":
        pool_status = "region_restricted"
    elif not pool_enabled:
        pool_status = "disabled"
    elif not device_online:
        pool_status = "device_offline"
    elif device_circuit_open:
        pool_status = "device_recovering"
    elif leased:
        pool_status = "busy"
    elif not auth_fresh:
        pool_status = "auth_check_due"
    elif capacity_wait:
        pool_status = "capacity_exhausted"
    elif cooling_down:
        pool_status = "cooling_down"
    elif not seedance_capability_ready(meta):
        pool_status = "capability_check_due"
    else:
        pool_status = "production_ready"
    return {
        "session_id": meta.get("doubao_capture_id"),
        "bridge_id": str(row.bridge_id),
        "device_id": meta.get("agent_device_id"),
        "device_name": row.device_name,
        "state": meta.get("doubao_capture_state") or "unknown",
        "message": meta.get("doubao_capture_message"),
        "error": meta.get("doubao_capture_error"),
        "browser_status": meta.get("doubao_browser_status"),
        "profile_id": meta.get("doubao_profile_id"),
        "fingerprint_state": meta.get("doubao_fingerprint_state") or "missing",
        "network_mode": str(meta.get("doubao_network_mode") or "proxy"),
        "proxy_id": (
            None
            if str(meta.get("doubao_network_mode") or "proxy") == "direct"
            else meta.get("doubao_proxy_id")
        ),
        "credential_state": (
            "encrypted" if meta.get("doubao_session_context_ciphertext") else "missing"
        ),
        "membership": membership_payload(meta),
        "last_verified_at": meta.get("doubao_last_verified_at"),
        "manual_verification": {
            "state": meta.get("doubao_manual_verification_state") or "idle",
            "started_at": meta.get("doubao_manual_verification_started_at"),
            "completed_at": meta.get("doubao_manual_verification_completed_at"),
            "conversation_id": meta.get("doubao_manual_verification_conversation_id"),
            "challenge_id": meta.get("doubao_manual_verification_challenge_id"),
            "message": meta.get("doubao_manual_verification_message"),
        },
        "pool": {
            "enabled": pool_enabled,
            "ready": generation_ready,
            "status": pool_status,
            "device": {
                "online": device_online,
                "heartbeat_at": (
                    agent_heartbeat_at(row).isoformat()
                    if agent_heartbeat_at(row) is not None
                    else None
                ),
                "circuit_open": device_circuit_open,
                "circuit_until": meta.get("doubao_device_circuit_until"),
                "last_error": meta.get("doubao_device_last_error"),
            },
            "lease_task_id": meta.get("doubao_pool_lease_task_id"),
            "lease_expires_at": meta.get("doubao_pool_lease_expires_at"),
            "last_used_at": meta.get("doubao_pool_last_used_at"),
            "last_success_at": meta.get("doubao_pool_last_success_at"),
            "consecutive_errors": int(meta.get("doubao_pool_consecutive_errors") or 0),
            "last_error": meta.get("doubao_pool_last_error"),
            "cooldown_until": meta.get("doubao_pool_cooldown_until"),
            "authentication": {
                "state": auth_state,
                "fresh": auth_fresh,
                "checked_at": meta.get("doubao_auth_checked_at"),
                "next_check_at": meta.get("doubao_next_auth_probe_at"),
                "error": meta.get("doubao_auth_error"),
            },
            "network": {
                "state": meta.get("doubao_network_state") or NETWORK_UNKNOWN,
                "checked_at": meta.get("doubao_network_checked_at"),
            },
            "capability": {
                "state": seedance_capability_state(meta),
                "checked_at": meta.get("doubao_seedance_capability_checked_at"),
                "error": meta.get("doubao_seedance_capability_error"),
                "probe_id": meta.get("doubao_seedance_probe_id"),
                "message": (
                    None
                    if seedance_capability_ready(meta)
                    else meta.get("doubao_seedance_capability_message")
                ),
            },
            "capacity": {
                "source": "provider_not_exposed",
                "reported_credits": None,
                "state": meta.get("doubao_pool_capacity_state")
                or ("available" if meta.get("doubao_pool_last_success_at") else "unknown"),
                "exhausted_at": meta.get("doubao_pool_capacity_exhausted_at"),
                "retry_at": meta.get("doubao_pool_capacity_retry_at"),
                "success_count": int(meta.get("doubao_pool_success_count") or 0),
            },
        },
        "created_at": meta.get("doubao_capture_created_at"),
        "updated_at": meta.get("doubao_capture_updated_at"),
        "test": {
            "id": meta.get("doubao_test_id"),
            "state": meta.get("doubao_test_state") or "idle",
            "message": meta.get("doubao_test_message"),
            "error": meta.get("doubao_test_error"),
            "prompt": meta.get("doubao_test_prompt"),
            "model": meta.get("doubao_test_model"),
            "duration": meta.get("doubao_test_duration"),
            "ratio": meta.get("doubao_test_ratio"),
            "video_url": result.get("video_url"),
            "width": result.get("width"),
            "height": result.get("height"),
            "started_at": meta.get("doubao_test_started_at"),
            "completed_at": meta.get("doubao_test_completed_at"),
        },
    }


def _row_for_capture(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    for_update: bool = False,
) -> HermesBrowserBridge:
    query = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        .order_by(HermesBrowserBridge.id.desc())
    )
    if for_update:
        query = query.with_for_update()
    row = next(
        (
            item
            for item in query.all()
            if is_doubao_lab_slot(item)
            and str(dict(item.meta_json or {}).get("doubao_capture_id") or "")
            == str(capture_id)
        ),
        None,
    )
    if row is None:
        raise APIError("DOUBAO_LAB_SESSION_NOT_FOUND", "豆包实验登录会话不存在。", 404)
    return row


def list_doubao_lab_sessions(
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
    return [_safe_session(row) for row in rows if is_doubao_lab_slot(row)]


def start_doubao_lab_onboarding(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    proxy_id: int | None,
) -> dict[str, Any]:
    from app.services.hermes_agent.content_factory import (
        _agent_rows,
        _bridge_agent_recent,
        _bridge_device_bound,
        _next_agent_profile_slot_index,
        _new_agent_slot,
    )

    if proxy_id is not None:
        try:
            resolve_flow_proxy_url(db, int(proxy_id), require_active=True)
        except ValueError as exc:
            raise APIError("DOUBAO_LAB_PROXY_INVALID", str(exc), 409) from exc
    device = str(device_id or "").strip()
    rows = _agent_rows(
        db, workspace_id=int(workspace_id), user_id=int(user_id), device_id=device
    )
    if not rows or not any(
        _bridge_device_bound(row) and _bridge_agent_recent(row) for row in rows
    ):
        raise APIError(
            "DOUBAO_LAB_DEVICE_OFFLINE",
            "请选择当前在线且已绑定的 Windows 浏览器桥设备。",
            409,
        )
    active = next(
        (
            row
            for row in rows
            if is_doubao_lab_slot(row)
            and str(dict(row.meta_json or {}).get("doubao_capture_state") or "")
            in _ACTIVE_STATES
        ),
        None,
    )
    if active is not None:
        return _safe_session(active)
    # Every account owns one immutable browser Profile. Reusing a previous
    # ready slot here silently replaced its cookies and defeated account-pool
    # isolation when an operator added a second account.
    slot_index = _next_agent_profile_slot_index(
        db,
        workspace_id=int(workspace_id),
        user_id=int(user_id),
        device_id=device,
        error_code="DOUBAO_LAB_PROFILE_CAPACITY_FULL",
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
    capture_id = "doubao_" + secrets.token_hex(20)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_lab_slot": True,
            "doubao_capture_id": capture_id,
            "doubao_capture_state": "awaiting_login",
            "doubao_capture_created_at": now.isoformat(),
            "doubao_capture_updated_at": now.isoformat(),
            "doubao_capture_message": (
                "请在独立 Chrome 中完成豆包网页登录，确认聊天页已登录后关闭整个窗口。"
            ),
            "doubao_capture_error": None,
            "doubao_browser_status": "starting",
            "doubao_target_url": DOUBAO_LAB_URL,
            "doubao_network_mode": "proxy" if proxy_id is not None else "direct",
            "doubao_proxy_id": int(proxy_id) if proxy_id is not None else None,
            "doubao_session_context_ciphertext": None,
            "doubao_profile_retired": False,
            "doubao_test_state": "idle",
            "doubao_pool_enabled": True,
            "doubao_seedance_capability_state": "unknown",
            "doubao_seedance_capability_checked_at": None,
            "doubao_seedance_capability_error": None,
            "doubao_auth_state": AUTH_UNKNOWN,
            "doubao_auth_checked_at": None,
            "doubao_auth_error": None,
            "doubao_network_state": NETWORK_UNKNOWN,
            "doubao_network_checked_at": None,
            "doubao_next_auth_probe_at": None,
            "doubao_pool_capacity_state": "unknown",
            "doubao_pool_capacity_exhausted_at": None,
            "doubao_pool_capacity_retry_at": None,
            "doubao_pool_success_count": 0,
            # Subscription level is not reliably exposed by the captured web
            # session. New and legacy accounts fail closed to the free tier
            # until a platform administrator explicitly marks otherwise.
            "doubao_membership_tier": "free",
            "doubao_membership_source": "default_free",
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


def cancel_doubao_lab_onboarding(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> dict[str, Any]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    now = _now()
    manual_lease = str(meta.get("doubao_pool_lease_task_id") or "").startswith(
        "manual-capture:"
    ) or str(meta.get("doubao_provider_browser_task_id") or "").startswith(
        "manual-capture:"
    )
    if manual_lease:
        # Cancelling the visible verification window must also release the
        # provider/manual lease.  Leaving either marker behind makes
        # doubao_slot_should_wake() keep the same Chrome profile alive even
        # though the operator has explicitly cancelled the session.
        meta = _clear_manual_verification_lease(meta)
        meta.update(
            {
                "doubao_manual_verification_state": "cancelled",
                "doubao_manual_verification_completed_at": now.isoformat(),
                "doubao_manual_verification_message": (
                    "人工验证已取消，浏览器窗口将关闭；账号不会在未验证前返回生产号池。"
                ),
            }
        )
    meta["doubao_capture_state"] = "cancelled"
    meta["doubao_capture_updated_at"] = now.isoformat()
    meta["doubao_capture_message"] = "豆包实验登录已取消，独立 Profile 已保留。"
    row.meta_json = meta
    row.status = "standby"
    row.load_json = {}
    db.add(row)
    return _safe_session(row)


def restart_doubao_account_login(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> dict[str, Any]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    if meta.get("doubao_pool_lease_task_id"):
        raise APIError(
            "DOUBAO_POOL_ACCOUNT_BUSY",
            "该账号正在生成视频，不能在租约结束前重新登录。",
            409,
        )
    now = _now()
    new_capture_id = "doubao_" + secrets.token_hex(20)
    meta.update(
        {
            "doubao_capture_id": new_capture_id,
            "doubao_capture_state": "awaiting_login",
            "doubao_capture_updated_at": now.isoformat(),
            "doubao_capture_message": "请在该账号原有独立 Profile 中重新登录豆包，完成后关闭窗口。",
            "doubao_capture_error": None,
            "doubao_browser_status": "starting",
            "doubao_target_url": DOUBAO_LAB_URL,
            "doubao_session_context_ciphertext": None,
            "doubao_pool_enabled": False,
            "doubao_pool_cooldown_until": None,
            "doubao_seedance_capability_state": "unknown",
            "doubao_seedance_capability_checked_at": None,
            "doubao_seedance_capability_error": None,
            "doubao_auth_state": AUTH_UNKNOWN,
            "doubao_auth_checked_at": None,
            "doubao_auth_error": None,
            "doubao_network_state": NETWORK_UNKNOWN,
            "doubao_network_checked_at": None,
            "doubao_next_auth_probe_at": None,
        }
    )
    row.meta_json = meta
    row.status = "pending"
    row.load_json = {}
    row.last_seen_at = now
    db.add(row)
    return _safe_session(row)


def rebind_doubao_account_proxy(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    proxy_id: int | None,
) -> dict[str, Any]:
    if proxy_id is not None:
        try:
            resolve_flow_proxy_url(db, int(proxy_id), require_active=True)
        except ValueError as exc:
            raise APIError("DOUBAO_LAB_PROXY_INVALID", str(exc), 409) from exc
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    if meta.get("doubao_pool_lease_task_id"):
        raise APIError(
            "DOUBAO_POOL_ACCOUNT_BUSY",
            "该账号正在生成视频，不能在租约结束前更换代理。",
            409,
        )
    if str(meta.get("doubao_test_state") or "") in {"queued", "running"}:
        raise APIError(
            "DOUBAO_LAB_TEST_BUSY",
            "该账号正在执行测试任务，完成后才能更换代理。",
            409,
        )
    if str(meta.get("doubao_capture_state") or "") in _ACTIVE_STATES:
        raise APIError(
            "DOUBAO_LAB_LOGIN_BUSY",
            "该账号的登录窗口仍在运行，请先关闭或取消登录后再更换代理。",
            409,
        )
    target_mode = "proxy" if proxy_id is not None else "direct"
    current_mode = str(meta.get("doubao_network_mode") or "proxy")
    if current_mode == target_mode and (
        target_mode == "direct"
        or int(meta.get("doubao_proxy_id") or 0) == int(proxy_id or 0)
    ):
        return _safe_session(row)

    now = _now()
    new_capture_id = "doubao_" + secrets.token_hex(20)
    meta.update(
        {
            "doubao_network_mode": target_mode,
            "doubao_proxy_id": int(proxy_id) if proxy_id is not None else None,
            "doubao_capture_id": new_capture_id,
            "doubao_capture_state": "awaiting_login",
            "doubao_capture_updated_at": now.isoformat(),
            "doubao_capture_message": (
                "代理已更换。请在原有独立 Profile 中重新登录豆包，完成后关闭窗口。"
            ),
            "doubao_capture_error": None,
            "doubao_browser_status": "starting",
            "doubao_session_context_ciphertext": None,
            "doubao_fingerprint_state": "missing",
            "doubao_fingerprint_digest": None,
            "doubao_last_verified_at": None,
            "doubao_pool_enabled": False,
            "doubao_pool_cooldown_until": None,
            "doubao_seedance_capability_state": "unknown",
            "doubao_seedance_capability_checked_at": None,
            "doubao_seedance_capability_error": None,
            "doubao_auth_state": AUTH_UNKNOWN,
            "doubao_auth_checked_at": None,
            "doubao_auth_error": None,
            "doubao_network_state": NETWORK_UNKNOWN,
            "doubao_network_checked_at": None,
            "doubao_next_auth_probe_at": None,
        }
    )
    row.meta_json = meta
    row.status = "pending"
    row.load_json = {}
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    row.last_seen_at = now
    db.add(row)
    return _safe_session(row)


def retire_doubao_account(
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
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    if meta.get("doubao_pool_lease_task_id"):
        raise APIError(
            "DOUBAO_POOL_ACCOUNT_BUSY",
            "该账号正在生成视频，不能在租约结束前删除。",
            409,
        )
    if str(meta.get("doubao_test_state") or "") in {"queued", "running"}:
        raise APIError(
            "DOUBAO_LAB_TEST_BUSY",
            "该账号正在执行测试任务，完成后才能删除。",
            409,
        )
    now = _now()
    meta.update(
        {
            "doubao_capture_state": "cancelled",
            "doubao_capture_updated_at": now.isoformat(),
            "doubao_capture_message": "账号已从号池删除；独立浏览器 Profile 已封存。",
            "doubao_capture_error": None,
            "doubao_browser_status": "stopped",
            "doubao_session_context_ciphertext": None,
            "doubao_fingerprint_state": "missing",
            "doubao_fingerprint_digest": None,
            "doubao_pool_enabled": False,
            "doubao_pool_lease_task_id": None,
            "doubao_pool_lease_expires_at": None,
            "doubao_next_auth_probe_at": None,
            "doubao_profile_retired": True,
            "doubao_profile_retired_at": now.isoformat(),
            "retired_reason": "doubao_account_deleted",
            "retired_at": now.isoformat(),
        }
    )
    row.meta_json = meta
    row.status = "retired"
    row.load_json = {}
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    db.add(row)
    db.flush()
    return {
        "deleted": True,
        "bridge_id": str(row.bridge_id),
        "session_id": str(capture_id),
    }


def get_doubao_lab_session(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> dict[str, Any]:
    return _safe_session(
        _row_for_capture(
            db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
        )
    )


def doubao_slot_should_wake(row: HermesBrowserBridge, *, now: datetime) -> bool:
    if not is_doubao_lab_slot(row):
        return False
    meta = dict(row.meta_json or {})
    provider_request_pending = _provider_request_pending(meta, now=now)
    return (
        str(row.status or "").lower() != "retired"
        and not bool(meta.get("doubao_profile_retired"))
        and (
            str(meta.get("doubao_capture_state") or "") in _ACTIVE_STATES
            or provider_request_pending
        )
    )


def doubao_slot_spec(db: Session, row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    runtime = str(meta.get("doubao_runtime") or "chrome").strip().lower()
    if runtime not in {"chrome", "doubao_desktop"}:
        runtime = "chrome"
    state = str(meta.get("doubao_capture_state") or "")
    if str(meta.get("doubao_network_mode") or "proxy") == "direct":
        proxy_url = ""
    else:
        try:
            proxy_url = resolve_flow_proxy_url(
                db, int(meta.get("doubao_proxy_id") or 0), require_active=True
            )
        except ValueError as exc:
            raise APIError("DOUBAO_LAB_PROXY_INVALID", str(exc), 409) from exc
    provider_request = _provider_request_pending(meta, now=_now())
    lease_id = str(meta.get("doubao_pool_lease_task_id") or "")
    provider_request_id = str(
        meta.get("doubao_provider_browser_task_id") or ""
    ).strip()
    # The Windows Agent keys its local browser-restart backoff by CaptureID.
    # A Doubao account keeps one stable capture id for its whole lifetime, so
    # reusing that id for a newly requested provider/manual session makes an
    # explicit operator action inherit an unrelated old slot failure.  In the
    # observed incident Chrome became reachable at the same 90-second boundary
    # where the preparation helper stopped waiting, leaving the visible page
    # in Image mode with none of the requested Seedance parameters applied.
    #
    # A provider request id is already the idempotent generation/probe/manual
    # lease boundary.  Expose it as the runtime generation while that request
    # is active: a genuinely new request clears stale Agent backoff, whereas a
    # retry of the same request retains its bounded backoff.  The durable
    # account capture id and its Profile/cookies remain unchanged.
    runtime_capture_id = str(meta.get("doubao_capture_id") or "")
    if provider_request and provider_request_id:
        runtime_capture_id = provider_request_id
    # Operator-owned manual capture is the only provider runtime that may
    # appear on the interactive Windows desktop. Normal provider work remains
    # minimized so unattended generation never steals focus.
    interactive = bool(
        (provider_request and lease_id.startswith("manual-capture:"))
        or (runtime == "doubao_desktop" and state == "awaiting_login")
    )
    return {
        "purpose": "doubao_lab",
        "target_url": str(meta.get("doubao_target_url") or DOUBAO_LAB_URL),
        "capture_id": runtime_capture_id,
        "capture_required": state == "capture_pending" or (
            runtime == "doubao_desktop" and state == "awaiting_login"
        ),
        "login_only": state == "awaiting_login" and runtime != "doubao_desktop",
        "provider_request": provider_request,
        "interactive": interactive,
        "flow_token_id": None,
        "proxy_url": proxy_url,
        "runtime": runtime,
    }


def record_doubao_browser_report(
    row: HermesBrowserBridge, report: dict[str, Any], *, now: datetime
) -> None:
    meta = dict(row.meta_json or {})
    if str(row.status or "").lower() == "retired":
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
    meta["doubao_browser_status"] = status
    meta["doubao_browser_checked_at"] = now.isoformat()
    # Keep the harmless runtime generation id for diagnosing stale Agent
    # reports.  The durable account id is not enough to tell whether Chrome is
    # serving the currently leased generation/probe/manual request.
    meta["doubao_browser_capture_id"] = (
        str(report.get("capture_id") or "").strip()[:128] or None
    )
    page_url = str(report.get("page_url") or "")[:1000]
    meta["doubao_page_url"] = page_url
    if _DOUBAO_REGION_BLOCK_MARKER in page_url:
        # A region rejection can still retain valid Doubao cookies.  The
        # browser probe historically reported that page as ``login_required``
        # and reopened the same Profile forever.  Region availability is a
        # provider/network capability verdict, never a login or CAPTCHA
        # verdict, so terminate any interactive lease atomically.
        meta = _mark_manual_verification_region_restricted(meta, now=now)
        row.meta_json = meta
        return
    manual_lease = str(meta.get("doubao_pool_lease_task_id") or "").startswith(
        "manual-capture:"
    )
    manual_state = str(meta.get("doubao_manual_verification_state") or "")
    match = _DOUBAO_CHAT_URL_RE.fullmatch(page_url)
    if (
        manual_lease
        and manual_state in {"preparing", "awaiting_human"}
        and meta.get("doubao_manual_verification_challenge_sent_at")
        and match is not None
    ):
        # A manually submitted Seedance challenge redirects the verified
        # browser to a durable numeric chat. That redirect is the CAPTCHA
        # recovery boundary for the exact Profile.
        meta = _mark_manual_verification_complete(
            meta, conversation_id=match.group(1), now=now
        )
        row.meta_json = meta
        return
    state = str(meta.get("doubao_capture_state") or "")
    if status == "login_complete" and state == "awaiting_login":
        meta["doubao_capture_state"] = "capture_pending"
        meta["doubao_capture_updated_at"] = now.isoformat()
        meta["doubao_capture_probe_started_at"] = now.isoformat()
        meta["doubao_capture_login_required_reports"] = 0
        meta["doubao_capture_message"] = (
            "登录窗口已关闭，正在从同一独立 Profile 采集豆包登录态。"
        )
    elif status == "login_required" and state == "capture_pending":
        # Chrome is restarted with CDP after the normal login window closes.
        # Its first report can arrive before the Doubao tab has restored the
        # persistent profile and applicable cookies.  Treating that one early
        # report as a real logout creates an endless visible-browser reopen
        # loop. Keep the same capture generation alive for a bounded loading
        # grace, then fail back to interactive login only if the page remains
        # unauthenticated after the grace has elapsed.
        started_at = _parsed_datetime(meta.get("doubao_capture_probe_started_at"))
        if started_at is None:
            started_at = _parsed_datetime(meta.get("doubao_capture_updated_at")) or now
            meta["doubao_capture_probe_started_at"] = started_at.isoformat()
        meta["doubao_capture_login_required_reports"] = int(
            meta.get("doubao_capture_login_required_reports") or 0
        ) + 1
        if now - started_at >= _CAPTURE_LOGIN_GRACE:
            meta["doubao_capture_state"] = "awaiting_login"
            meta["doubao_capture_updated_at"] = now.isoformat()
            meta["doubao_capture_message"] = "尚未检测到有效豆包登录，请登录后关闭窗口。"
        else:
            meta["doubao_capture_message"] = (
                "豆包页面正在恢复同一 Profile 的登录态，采集会自动继续。"
            )
    row.meta_json = meta


def _clear_manual_verification_lease(meta: dict[str, Any]) -> dict[str, Any]:
    result = dict(meta)
    lease_id = str(result.get("doubao_pool_lease_task_id") or "")
    provider_id = str(result.get("doubao_provider_browser_task_id") or "")
    if lease_id.startswith("manual-capture:"):
        result["doubao_pool_lease_task_id"] = None
        result["doubao_pool_lease_expires_at"] = None
    if provider_id.startswith("manual-capture:") or provider_id == lease_id:
        result["doubao_provider_browser_task_id"] = None
    return result


def _mark_manual_verification_region_restricted(
    meta: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    result = apply_seedance_capability_result(
        dict(meta), success=False, error_code="doubao_region_restricted"
    )
    result = mark_auth_probe_result(
        result,
        state=AUTH_UNKNOWN,
        network_state=NETWORK_REGION_RESTRICTED,
        error_code="doubao_region_restricted",
        now=now,
    )
    result = _clear_manual_verification_lease(result)
    result.update(
        {
            "doubao_capture_state": "ready",
            "doubao_capture_error": "doubao_region_restricted",
            "doubao_capture_message": (
                "登录态仍有效，但当前固定网络出口受地区限制；请更换出口后重新验证。"
            ),
            "doubao_pool_enabled": False,
            "doubao_pool_last_error": "doubao_region_restricted",
            "doubao_pool_cooldown_until": None,
            "doubao_manual_verification_state": "region_restricted",
            "doubao_manual_verification_completed_at": now.isoformat(),
            "doubao_manual_verification_message": (
                "当前页面是地区限制，不是登录失效或 CAPTCHA；人工验证已停止。"
            ),
        }
    )
    return result


def _mark_manual_verification_complete(
    meta: dict[str, Any], *, conversation_id: str, now: datetime
) -> dict[str, Any]:
    result = _clear_manual_verification_lease(dict(meta))
    result = apply_seedance_capability_result(result, success=True)
    result = mark_authenticated(result, now=now)
    result.update(
        {
            "doubao_capture_state": "ready",
            "doubao_capture_error": None,
            "doubao_capture_message": "人工 CAPTCHA 已通过，账号已恢复生产号池。",
            "doubao_pool_enabled": True,
            "doubao_pool_last_error": None,
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_cooldown_until": None,
            "doubao_manual_verification_state": "complete",
            "doubao_manual_verification_completed_at": now.isoformat(),
            "doubao_manual_verification_conversation_id": str(conversation_id),
            "doubao_manual_verification_message": (
                "Seedance 人工验证已通过并进入真实生成会话，账号已恢复生产号池。"
            ),
            "doubao_last_verified_at": now.isoformat(),
        }
    )
    return result


def _normalize_fingerprint(value: dict[str, Any]) -> dict[str, str]:
    limits = {
        "user_agent": 512,
        "accept_language": 256,
        "sec_ch_ua": 512,
        "sec_ch_ua_mobile": 16,
        "sec_ch_ua_platform": 64,
        "timezone": 128,
    }
    result = {
        key: str(value.get(key) or "").strip()[:limit]
        for key, limit in limits.items()
        if str(value.get(key) or "").strip()
    }
    if "user_agent" not in result or "sec_ch_ua_platform" not in result:
        raise APIError("DOUBAO_LAB_FINGERPRINT_INVALID", "浏览器指纹采集不完整。", 400)
    return result


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
        if (
            not _COOKIE_NAME_RE.fullmatch(name)
            or not raw_value
            or len(raw_value) > 8192
            or not (domain == "doubao.com" or domain.endswith(".doubao.com"))
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
                "path": str(item.get("path") or "/").strip()[:255] or "/",
                "secure": bool(item.get("secure")),
                "http_only": bool(item.get("http_only")),
                "expires": float(item.get("expires") or 0),
            }
        )
    return cookies


def _device_params(
    diagnostics: dict[str, Any] | None, *, profile_id: str
) -> dict[str, str]:
    source = (
        diagnostics.get("device_params")
        if isinstance(diagnostics, dict)
        and isinstance(diagnostics.get("device_params"), dict)
        else {}
    )
    digest = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
    fp = str(source.get("fp") or "").strip()
    device_id = str(source.get("device_id") or "").strip()
    web_id = str(source.get("web_id") or "").strip()
    return {
        "fp": fp[:256] if fp else f"verify_{digest[:48]}",
        "device_id": device_id[:64] if device_id.isdigit() else str(7_100_000_000_000_000 + int(digest[:11], 16) % 800_000_000_000_000),
        "web_id": web_id[:64] if web_id.isdigit() else str(7_600_000_000_000_000_000 + int(digest[11:22], 16) % 80_000_000_000_000_000),
    }


def decrypt_doubao_session_context(meta: dict[str, Any]) -> dict[str, Any] | None:
    ciphertext = str(meta.get("doubao_session_context_ciphertext") or "")
    if not ciphertext:
        return None
    context = json.loads(decrypt_api_key(ciphertext))
    cookies = _normalize_session_cookies(context.get("cookies"))
    fingerprint = _normalize_fingerprint(context.get("fingerprint") or {})
    device_params = {
        key: str(value or "")[:256]
        for key, value in dict(context.get("device_params") or {}).items()
        if key in {"fp", "device_id", "web_id"}
    }
    if not cookies or not any(
        item["name"] in {"sessionid", "sessionid_ss"} for item in cookies
    ):
        raise ValueError("Doubao browser session contains no usable session cookie")
    return {
        "cookies": cookies,
        "fingerprint": fingerprint,
        "device_params": device_params,
    }


async def ingest_doubao_browser_capture(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    bridge_id: str,
    capture_id: str,
    session_cookies: list[dict[str, Any]] | None,
    session_diagnostics: dict[str, Any] | None,
    profile_id: str,
    fingerprint: dict[str, Any],
    **_: Any,
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
    if row is None or not is_doubao_lab_slot(row):
        raise APIError("DOUBAO_LAB_SLOT_FORBIDDEN", "豆包实验 Slot 不属于当前设备。", 403)
    meta = dict(row.meta_json or {})
    if str(meta.get("agent_device_id") or "") != str(device_id):
        raise APIError("DOUBAO_LAB_DEVICE_MISMATCH", "豆包实验浏览器设备不匹配。", 403)
    if str(meta.get("doubao_capture_id") or "") != str(capture_id):
        raise APIError("DOUBAO_LAB_CAPTURE_STALE", "该豆包登录采集已过期。", 409)
    expected_profile = f"{device_id}/slot-{int(meta.get('local_port') or 0)}"
    if str(profile_id or "") != expected_profile:
        raise APIError("DOUBAO_LAB_PROFILE_MISMATCH", "豆包浏览器 Profile 身份不匹配。", 403)
    cookies = _normalize_session_cookies(session_cookies)
    if not any(item["name"] in {"sessionid", "sessionid_ss"} for item in cookies):
        raise APIError("DOUBAO_LAB_SESSION_INVALID", "浏览器没有返回有效豆包登录会话。", 400)
    normalized_fingerprint = _normalize_fingerprint(fingerprint)
    params = _device_params(session_diagnostics, profile_id=expected_profile)
    now = _now()
    meta.update(
        {
            "doubao_session_context_ciphertext": encrypt_api_key(
                json.dumps(
                    {
                        "cookies": cookies,
                        "fingerprint": normalized_fingerprint,
                        "device_params": params,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
            "doubao_profile_id": expected_profile,
            "doubao_capture_state": "ready",
            "doubao_capture_updated_at": now.isoformat(),
            "doubao_capture_message": "豆包网页登录态已采集，可测试 Seedance 2.0 Mini。",
            "doubao_capture_error": None,
            "doubao_fingerprint_state": "captured",
            "doubao_fingerprint_digest": hashlib.sha256(
                repr(sorted(normalized_fingerprint.items())).encode("utf-8")
            ).hexdigest(),
            "doubao_last_verified_at": now.isoformat(),
            "doubao_pool_enabled": True,
            "doubao_pool_consecutive_errors": 0,
            "doubao_pool_last_error": None,
            "doubao_pool_cooldown_until": None,
            "doubao_auth_state": AUTH_UNKNOWN,
            "doubao_auth_checked_at": None,
            "doubao_auth_error": None,
            "doubao_network_state": NETWORK_UNKNOWN,
            "doubao_network_checked_at": None,
            "doubao_next_auth_probe_at": now.isoformat(),
            "doubao_seedance_capability_state": "unknown",
            "doubao_seedance_capability_checked_at": None,
            "doubao_seedance_capability_error": None,
        }
    )
    row.meta_json = meta
    row.status = "standby"
    row.load_json = {}
    db.add(row)
    return {"success": True}


async def verify_doubao_lab_session(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> dict[str, Any]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    try:
        context = decrypt_doubao_session_context(meta)
    except (ValueError, json.JSONDecodeError) as exc:
        context = None
        error = str(exc)[:500]
    else:
        error = None
    now = _now()
    meta["doubao_last_verified_at"] = now.isoformat()
    meta["doubao_capture_updated_at"] = now.isoformat()
    if context is not None and str(meta.get("doubao_capture_state") or "") != "captcha_required":
        meta["doubao_capture_state"] = "ready"
        meta["doubao_capture_message"] = "豆包网页登录态结构有效。"
        meta["doubao_capture_error"] = None
    elif context is not None:
        # A decryptable cookie envelope does not prove that an account-level
        # CAPTCHA has been cleared. Preserve the fail-closed pool state until
        # the operator completes the prepared Seedance challenge.
        meta["doubao_capture_message"] = (
            "登录态仍然有效，但账号需要完成一次 Seedance 人工验证后才能恢复号池。"
        )
        meta["doubao_capture_error"] = "captcha_required"
    else:
        meta["doubao_capture_state"] = "failed"
        meta["doubao_capture_message"] = "豆包登录态已失效，请重新登录。"
        meta["doubao_capture_error"] = error or "session_invalid"
    row.meta_json = meta
    db.add(row)
    return _safe_session(row)


def start_doubao_manual_verification(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> tuple[dict[str, Any], bool]:
    """Open one exact account Profile for operator-owned CAPTCHA recovery.

    This is deliberately separate from re-login: CAPTCHA recovery must retain
    the account cookies, device identity, fixed proxy and browser Profile.
    The ``manual-capture:`` lease is also the Bridge boundary that makes this
    one browser visible while ordinary provider work remains minimized.
    """
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    if not meta.get("doubao_session_context_ciphertext"):
        raise APIError(
            "DOUBAO_MANUAL_VERIFICATION_LOGIN_REQUIRED",
            "该账号没有可复用登录态，请先重新登录。",
            409,
        )
    page_url = str(meta.get("doubao_page_url") or "").strip()
    capability_state = seedance_capability_state(meta)
    if (
        capability_state == "region_restricted"
        or _DOUBAO_REGION_BLOCK_MARKER in page_url
    ):
        meta = _mark_manual_verification_region_restricted(meta, now=_now())
        row.meta_json = meta
        db.add(row)
        raise APIError(
            "DOUBAO_MANUAL_VERIFICATION_REGION_RESTRICTED",
            "当前固定网络出口受地区限制，这不是 CAPTCHA。请先更换网络出口。",
            409,
        )
    if (
        str(meta.get("doubao_capture_state") or "") != "captcha_required"
        and capability_state != "captcha_required"
    ):
        raise APIError(
            "DOUBAO_MANUAL_VERIFICATION_NOT_REQUIRED",
            "该账号当前没有 CAPTCHA 待处理；登录或地区问题请使用对应操作。",
            409,
        )
    current_lease = str(meta.get("doubao_pool_lease_task_id") or "")
    if current_lease and not current_lease.startswith("manual-capture:"):
        raise APIError(
            "DOUBAO_POOL_ACCOUNT_BUSY",
            "该账号正在执行其他任务，不能同时打开人工验证。",
            409,
        )
    now = _now()
    if current_lease.startswith("manual-capture:"):
        if str(meta.get("doubao_manual_verification_state") or "") == "captcha_required":
            meta["doubao_manual_verification_state"] = "preparing"
            meta["doubao_manual_verification_message"] = (
                "正在重新进入 AI 创作并预设 Seedance 参数。"
            )
            row.meta_json = meta
            db.add(row)
            db.flush()
            return _safe_session(row), True
        return _safe_session(row), False
    challenge_id = "mvc_" + secrets.token_hex(16)
    lease_id = f"manual-capture:{challenge_id}"
    meta.update(
        {
            "doubao_pool_enabled": False,
            "doubao_pool_lease_task_id": lease_id,
            "doubao_pool_lease_expires_at": (
                now + _MANUAL_VERIFICATION_LEASE
            ).isoformat(),
            "doubao_provider_browser_task_id": lease_id,
            "doubao_target_url": DOUBAO_LAB_URL,
            "doubao_manual_verification_state": "preparing",
            "doubao_manual_verification_started_at": now.isoformat(),
            "doubao_manual_verification_completed_at": None,
            "doubao_manual_verification_conversation_id": None,
            "doubao_manual_verification_challenge_id": challenge_id,
            "doubao_manual_verification_challenge_sent_at": None,
            "doubao_manual_verification_message": (
                "正在打开 AI 创作并预设 Seedance 2.0 Mini、9:16、4 秒和验证提示词。"
            ),
            "doubao_capture_message": (
                "正在准备可直接提交的 Seedance 人工验证；不要切换账号、代理或 Profile。"
            ),
        }
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return _safe_session(row), True


async def complete_doubao_manual_verification(
    db: Session, *, workspace_id: int, user_id: int, capture_id: str
) -> dict[str, Any]:
    """Recover an account after the prepared Seedance request enters a chat."""
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    lease_id = str(meta.get("doubao_pool_lease_task_id") or "")
    if not lease_id.startswith("manual-capture:"):
        raise APIError(
            "DOUBAO_MANUAL_VERIFICATION_NOT_STARTED",
            "请先打开该账号的人工验证浏览器。",
            409,
        )
    page_url = str(meta.get("doubao_page_url") or "").strip()
    now = _now()
    if _DOUBAO_REGION_BLOCK_MARKER in page_url:
        meta = _mark_manual_verification_region_restricted(meta, now=now)
        row.meta_json = meta
        db.add(row)
        db.flush()
        return _safe_session(row)
    match = _DOUBAO_CHAT_URL_RE.fullmatch(page_url)
    if (
        match is None
        or not meta.get("doubao_manual_verification_challenge_sent_at")
    ):
        meta["doubao_manual_verification_state"] = "awaiting_human"
        meta["doubao_manual_verification_message"] = (
            "AI 创作参数已预设。请点击发送并完成人工验证；进入生成会话即验证成功。"
        )
        row.meta_json = meta
        db.add(row)
        db.flush()
        return _safe_session(row)
    meta = _mark_manual_verification_complete(
        meta, conversation_id=match.group(1), now=now
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return _safe_session(row)


def apply_doubao_manual_video_challenge_result(
    row: HermesBrowserBridge,
    *,
    challenge_id: str,
    status: str,
    conversation_id: str | None = None,
    error_code: str | None = None,
) -> bool:
    """Persist one idempotent result from the prepared Seedance challenge."""
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_manual_verification_challenge_id") or "") != str(
        challenge_id
    ):
        return False
    lease_id = f"manual-capture:{challenge_id}"
    if str(meta.get("doubao_pool_lease_task_id") or "") != lease_id:
        return False
    now = _now()
    state = str(status or "failed").strip().lower()
    if state == "verified" and str(conversation_id or "").isdigit():
        meta = _mark_manual_verification_complete(
            meta, conversation_id=str(conversation_id), now=now
        )
    elif state == "ready_to_submit":
        meta.update(
            {
                "doubao_capture_state": "captcha_required",
                "doubao_capture_error": "captcha_required",
                "doubao_pool_enabled": False,
                "doubao_manual_verification_state": "awaiting_human",
                "doubao_manual_verification_challenge_sent_at": now.isoformat(),
                "doubao_manual_verification_message": (
                    "AI 创作已预设 Seedance 2.0 Mini、9:16、4 秒和提示词；请直接点击发送并完成人工验证。"
                ),
                "doubao_capture_message": (
                    "等待人工点击发送并完成 CAPTCHA；完成后会自动恢复号池。"
                ),
            }
        )
    elif state == "captcha_required":
        meta.update(
            {
                "doubao_capture_state": "captcha_required",
                "doubao_capture_error": "captcha_required",
                "doubao_manual_verification_state": "captcha_required",
                "doubao_manual_verification_message": (
                    "当前窗口已有 CAPTCHA。请先完成验证，然后再次点击“打开人工验证”以预设 Seedance 参数。"
                ),
                "doubao_capture_message": (
                    "等待人工完成 CAPTCHA；系统不会自动识别或绕过验证。"
                ),
            }
        )
    elif state == "region_restricted" or error_code == "doubao_region_restricted":
        meta = _mark_manual_verification_region_restricted(meta, now=now)
    elif state == "auth_required" or error_code in {
        "doubao_auth_required",
        "doubao_account_context_invalid",
    }:
        meta = _clear_manual_verification_lease(meta)
        meta = mark_auth_probe_result(
            meta,
            state=AUTH_REQUIRED,
            network_state=NETWORK_REACHABLE,
            error_code="doubao_auth_required",
            now=now,
        )
        meta.update(
            {
                "doubao_capture_state": "failed",
                "doubao_capture_error": "doubao_auth_required",
                "doubao_capture_message": "登录态已失效，请使用重新登录流程。",
                "doubao_pool_enabled": False,
                "doubao_pool_last_error": "doubao_auth_required",
                "doubao_manual_verification_state": "login_required",
                "doubao_manual_verification_completed_at": now.isoformat(),
                "doubao_manual_verification_message": (
                    "当前是登录失效，不是 CAPTCHA；人工验证已停止。"
                ),
            }
        )
    else:
        meta = _clear_manual_verification_lease(meta)
        code = str(error_code or "doubao_manual_challenge_failed")[:64]
        meta.update(
            {
                "doubao_capture_state": "captcha_required",
                "doubao_capture_error": code,
                "doubao_pool_enabled": False,
                "doubao_pool_last_error": code,
                "doubao_manual_verification_state": "failed",
                "doubao_manual_verification_completed_at": now.isoformat(),
                "doubao_manual_verification_message": (
                    "Seedance 人工验证未能准备完成，浏览器已停止；可稍后重新发起。"
                ),
            }
        )
    row.meta_json = meta
    return True


def fail_doubao_manual_video_challenge_dispatch(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    challenge_id: str,
) -> None:
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    if apply_doubao_manual_video_challenge_result(
        row,
        challenge_id=challenge_id,
        status="failed",
        error_code="doubao_manual_video_challenge_dispatch_failed",
    ):
        db.add(row)


def reconcile_doubao_account_pool(
    db: Session, *, workspace_id: int, user_id: int
) -> dict[str, int]:
    """Remove stale interactive state without deleting account Profiles."""
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        .order_by(HermesBrowserBridge.id.asc())
        .with_for_update()
        .all()
    )
    now = _now()
    summary = {
        "scanned": 0,
        "region_restricted": 0,
        "legacy_manual_cancelled": 0,
        "unchanged": 0,
    }
    for row in rows:
        if not is_doubao_lab_slot(row):
            continue
        summary["scanned"] += 1
        meta = dict(row.meta_json or {})
        changed = False
        page_url = str(meta.get("doubao_page_url") or "")
        if (
            _DOUBAO_REGION_BLOCK_MARKER in page_url
            or seedance_capability_state(meta) == "region_restricted"
            or str(meta.get("doubao_pool_last_error") or "")
            == "doubao_region_restricted"
        ):
            meta = _mark_manual_verification_region_restricted(meta, now=now)
            summary["region_restricted"] += 1
            changed = True
        elif str(meta.get("doubao_pool_lease_task_id") or "").startswith(
            "manual-capture:"
        ) and not meta.get("doubao_manual_verification_challenge_id"):
            # Pre-upgrade leases used the obsolete ordinary-chat challenge and
            # cannot satisfy the real AI Creation contract. Cancel them once so the
            # Bridge does not keep reopening the same visible browser.
            meta = _clear_manual_verification_lease(meta)
            meta.update(
                {
                    "doubao_pool_enabled": False,
                    "doubao_manual_verification_state": "cancelled",
                    "doubao_manual_verification_completed_at": now.isoformat(),
                    "doubao_manual_verification_message": (
                        "旧版验证已取消；如确有 CAPTCHA，请重新发起 Seedance 人工验证。"
                    ),
                }
            )
            summary["legacy_manual_cancelled"] += 1
            changed = True
        if changed:
            row.meta_json = meta
            row.status = "standby"
            row.load_json = {}
            db.add(row)
        else:
            summary["unchanged"] += 1
    db.flush()
    return summary


def queue_doubao_capability_probe(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> tuple[dict[str, Any], bool]:
    """Lease one exact Profile for a quota-free Seedance composer probe."""
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_capture_state") or "") != "ready" or not meta.get(
        "doubao_session_context_ciphertext"
    ):
        raise APIError("DOUBAO_LAB_NOT_READY", "请先完成豆包浏览器登录。", 409)
    if meta.get("doubao_pool_lease_task_id"):
        if str(meta.get("doubao_seedance_capability_state") or "") == "probing":
            return _safe_session(row), False
        raise APIError("DOUBAO_POOL_ACCOUNT_BUSY", "该账号正在执行其他任务。", 409)
    probe_id = "dp_" + secrets.token_hex(16)
    lease_id = f"probe:{probe_id}"
    now = _now()
    previous_state = seedance_capability_state(meta)
    meta.update(
        {
            "doubao_seedance_probe_id": probe_id,
            "doubao_seedance_capability_previous_state": previous_state,
            "doubao_seedance_capability_state": "probing",
            "doubao_seedance_capability_checked_at": None,
            "doubao_seedance_capability_error": None,
            "doubao_seedance_capability_message": "正在检测该账号的 Seedance 视频能力。",
            "doubao_pool_lease_task_id": lease_id,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            "doubao_provider_browser_task_id": lease_id,
        }
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return _safe_session(row), True


def fail_doubao_capability_probe_dispatch(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    probe_id: str,
    error: str,
) -> None:
    row = _row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        for_update=True,
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_seedance_probe_id") or "") != str(probe_id):
        return
    lease_id = f"probe:{probe_id}"
    if str(meta.get("doubao_pool_lease_task_id") or "") == lease_id:
        meta["doubao_pool_lease_task_id"] = None
        meta["doubao_pool_lease_expires_at"] = None
    if str(meta.get("doubao_provider_browser_task_id") or "") == lease_id:
        meta["doubao_provider_browser_task_id"] = None
    meta = apply_seedance_capability_result(
        meta, success=False, error_code="probe_dispatch_failed"
    )
    if seedance_capability_ready(meta):
        meta["doubao_seedance_capability_message"] = (
            "能力检测任务暂时无法派发；继续沿用已确认的视频能力。"
        )
    else:
        meta["doubao_seedance_capability_message"] = (
            "能力检测任务暂时无法派发，请稍后重试。"
        )
    meta["doubao_capture_error"] = str(error or "")[:500] or None
    row.meta_json = meta
    db.add(row)


def queue_doubao_lab_test(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
    prompt: str,
    duration: int,
    ratio: str,
) -> tuple[dict[str, Any], bool]:
    row = _row_for_capture(
        db, workspace_id=workspace_id, user_id=user_id, capture_id=capture_id
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_capture_state") or "") != "ready" or not meta.get(
        "doubao_session_context_ciphertext"
    ):
        raise APIError("DOUBAO_LAB_NOT_READY", "请先完成并验证豆包浏览器登录。", 409)
    if str(meta.get("doubao_test_state") or "") in {"queued", "running"}:
        return _safe_session(row), False
    if meta.get("doubao_pool_lease_task_id"):
        raise APIError(
            "DOUBAO_POOL_ACCOUNT_BUSY",
            "该账号正在生成视频，请在当前任务结束后再运行链路验收。",
            409,
        )
    if not supports_duration(meta, duration):
        membership = membership_payload(meta)
        raise APIError(
            "DOUBAO_MEMBERSHIP_REQUIRED",
            (
                f"该账号是{membership['label']}，最长支持 "
                f"{membership['max_duration_seconds']} 秒；{int(duration)} 秒需要加强套餐。"
            ),
            409,
        )
    test_id = "dt_" + secrets.token_hex(16)
    lease_id = f"lab:{test_id}"
    now = _now()
    meta.update(
        {
            "doubao_test_id": test_id,
            "doubao_test_state": "queued",
            "doubao_test_message": "Seedance 2.0 Mini 实验任务已进入后台队列。",
            "doubao_test_error": None,
            "doubao_test_prompt": str(prompt).strip()[:2000],
            "doubao_test_model": "seedance_v2.0_mini",
            "doubao_test_duration": int(duration),
            "doubao_test_ratio": ratio,
            "doubao_test_result": None,
            "doubao_test_started_at": now.isoformat(),
            "doubao_test_completed_at": None,
            # A lab generation owns the exact same account/Profile exclusion
            # boundary as a production generation.  The matching browser
            # marker makes the Bridge wake this otherwise dormant Profile.
            "doubao_pool_lease_task_id": lease_id,
            "doubao_pool_lease_expires_at": (now + timedelta(minutes=15)).isoformat(),
            "doubao_provider_browser_task_id": lease_id,
        }
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return _safe_session(row), True


def fail_doubao_lab_test_dispatch(
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
    if str(meta.get("doubao_test_id") or "") != str(test_id):
        return
    meta["doubao_test_state"] = "failed"
    meta["doubao_test_message"] = "后台任务派发失败，请稍后重试。"
    meta["doubao_test_error"] = str(error)[:500]
    meta["doubao_test_completed_at"] = _now().isoformat()
    row.meta_json = meta
    db.add(row)


__all__ = [
    "DOUBAO_LAB_URL",
    "apply_doubao_manual_video_challenge_result",
    "cancel_doubao_lab_onboarding",
    "decrypt_doubao_session_context",
    "doubao_slot_should_wake",
    "doubao_slot_spec",
    "fail_doubao_lab_test_dispatch",
    "fail_doubao_manual_video_challenge_dispatch",
    "fail_doubao_capability_probe_dispatch",
    "get_doubao_lab_session",
    "ingest_doubao_browser_capture",
    "is_doubao_lab_slot",
    "list_doubao_lab_sessions",
    "complete_doubao_manual_verification",
    "queue_doubao_lab_test",
    "queue_doubao_capability_probe",
    "reconcile_doubao_account_pool",
    "record_doubao_browser_report",
    "rebind_doubao_account_proxy",
    "retire_doubao_account",
    "restart_doubao_account_login",
    "start_doubao_manual_verification",
    "start_doubao_lab_onboarding",
    "verify_doubao_lab_session",
]
