from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow2api_admin import Flow2ApiAdminClient, Flow2ApiAdminError


FLOW_ACCOUNT_URL = "https://labs.google/fx/tools/flow"
_ACTIVE_CAPTURE_STATES = {"awaiting_login", "capture_pending"}
_TERMINAL_CAPTURE_STATES = {"ready", "failed", "human_required", "cancelled"}
_AUTO_REAUTH_REASONS = {"grant_expired"}
_AUTO_REAUTH_MAX_ATTEMPTS_PER_GRANT = 1
_AUTO_REAUTH_STRATEGY = "project_renderer_diagnostics_then_capture_v6"
_PROFILE_LAYOUT_RECOVERY_AGENT_VERSION = "2026.08.11.4"


def _flow_project_url(project_id: Any) -> str:
    """Return the real Flow editor URL for one upstream-owned project.

    ``/tools/flow`` is now a public marketing page.  Visiting it no longer
    exercises the signed-in Flow application and therefore cannot rotate the
    page-owned grant.  Automatic reauthorization must visit the account's
    existing editor project; onboarding without a verified project keeps the
    public entry URL and remains interactive.
    """
    normalized = str(project_id or "").strip()
    if not normalized:
        return FLOW_ACCOUNT_URL
    return f"{FLOW_ACCOUNT_URL}/project/{normalized}"


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def is_flow_account_slot(row: HermesBrowserBridge) -> bool:
    return bool(dict(row.meta_json or {}).get("flow_account_slot"))


def _safe_session(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    return {
        "session_id": meta.get("flow_capture_id"),
        "bridge_id": str(row.bridge_id),
        "device_id": meta.get("agent_device_id"),
        "device_name": row.device_name,
        "state": meta.get("flow_capture_state") or "unknown",
        "purpose": meta.get("flow_capture_purpose") or "onboarding",
        "token_id": meta.get("flow_token_id"),
        "email": meta.get("flow_account_email"),
        "message": meta.get("flow_capture_message"),
        "error": meta.get("flow_capture_error"),
        "browser_status": meta.get("flow_browser_status"),
        "profile_id": meta.get("flow_profile_id"),
        "fingerprint_state": meta.get("flow_fingerprint_state") or "missing",
        "proxy_url": meta.get("flow_proxy_url"),
        "last_keepalive_at": meta.get("flow_last_keepalive_success_at"),
        "next_keepalive_at": meta.get("flow_next_keepalive_at"),
        "auto_reauth_attempts": int(meta.get("flow_auto_reauth_attempts") or 0),
        "auto_reauth_next_at": meta.get("flow_auto_reauth_next_at"),
        "human_required": str(meta.get("flow_capture_state") or "") == "human_required",
        "created_at": meta.get("flow_capture_created_at"),
        "updated_at": meta.get("flow_capture_updated_at"),
    }


def list_flow_browser_sessions(
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
    return [_safe_session(row) for row in rows if is_flow_account_slot(row)]


def start_flow_browser_onboarding(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    remark: str | None,
    image_enabled: bool,
    video_enabled: bool,
    image_concurrency: int,
    video_concurrency: int,
    proxy_url: str,
    token_id: int | None = None,
) -> dict[str, Any]:
    # Import lazily: content_factory owns the generic device/slot allocator;
    # this module only reserves a purpose-scoped row and must not create a
    # second browser transport implementation.
    from app.services.hermes_agent.content_factory import (
        _agent_rows,
        _bridge_agent_recent,
        _bridge_device_bound,
        _next_agent_profile_slot_index,
        _new_agent_slot,
    )

    device = str(device_id or "").strip()
    rows = _agent_rows(
        db, workspace_id=int(workspace_id), user_id=int(user_id), device_id=device
    )
    if not rows or not any(_bridge_device_bound(row) and _bridge_agent_recent(row) for row in rows):
        raise APIError(
            "FLOW_BROWSER_DEVICE_OFFLINE",
            "请选择当前在线且已绑定的 Windows 浏览器桥设备。",
            409,
        )

    if token_id is not None:
        existing = next(
            (
                row
                for row in rows
                if is_flow_account_slot(row)
                and int(dict(row.meta_json or {}).get("flow_token_id") or 0) == int(token_id)
            ),
            None,
        )
        if existing is None:
            raise APIError(
                "FLOW_BROWSER_PROFILE_NOT_FOUND",
                "该账号尚未绑定固定浏览器 Profile，不能在其他 Slot 中刷新登录。",
                409,
            )
        row = existing
    else:
        active_onboarding = next(
            (
                row
                for row in rows
                if is_flow_account_slot(row)
                and str(dict(row.meta_json or {}).get("flow_capture_state") or "")
                in _ACTIVE_CAPTURE_STATES
                and dict(row.meta_json or {}).get("flow_token_id") in (None, "")
            ),
            None,
        )
        if active_onboarding is not None:
            return _safe_session(active_onboarding)
        slot_index = _next_agent_profile_slot_index(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            device_id=device,
            error_code="FLOW_BROWSER_PROFILE_CAPACITY_FULL",
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
    capture_id = "flow_" + secrets.token_hex(20)
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "flow_account_slot": True,
            "flow_capture_id": capture_id,
            "flow_capture_state": "awaiting_login",
            "flow_capture_purpose": "reauth" if token_id is not None else "onboarding",
            "flow_capture_created_at": now.isoformat(),
            "flow_capture_updated_at": now.isoformat(),
            "flow_capture_message": "请在已打开的 Chrome 中登录 Google 并进入 Flow，系统会自动完成采集。",
            "flow_capture_error": None,
            "flow_browser_status": "starting",
            "flow_target_url": FLOW_ACCOUNT_URL,
            "flow_proxy_url": str(proxy_url or "").strip(),
            "flow_token_id": int(token_id) if token_id is not None else None,
            "flow_account_settings": {
                "remark": str(remark or "")[:500] or None,
                "image_enabled": bool(image_enabled),
                "video_enabled": bool(video_enabled),
                "image_concurrency": int(image_concurrency),
                "video_concurrency": int(video_concurrency),
                "captcha_proxy_url": str(proxy_url or "").strip(),
            },
            "flow_auto_reauth_attempts": 0,
            "flow_auto_reauth_window_started_at": None,
            "flow_auto_reauth_next_at": None,
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


def cancel_flow_browser_onboarding(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> dict[str, Any]:
    row = _flow_row_for_capture(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        capture_id=capture_id,
        include_retired=True,
    )
    meta = dict(row.meta_json or {})
    if str(meta.get("flow_capture_state") or "") not in _TERMINAL_CAPTURE_STATES:
        now = _now()
        bound_token_id = int(meta.get("flow_token_id") or 0)
        if bound_token_id > 0:
            # Cancelling a re-auth attempt must stop the browser without
            # destroying the already-bound account/profile relationship.
            meta["flow_capture_state"] = "ready"
            meta["flow_capture_purpose"] = "keepalive"
            meta["flow_capture_message"] = "重新登录已取消；账号继续使用原有登录状态。"
            row.status = "standby"
        else:
            # An unfinished onboarding profile may contain a partial Google
            # login. Tombstone it instead of returning it to the reusable slot
            # pool, and make cancellation an explicit stop command.
            meta["flow_capture_state"] = "cancelled"
            meta["flow_capture_message"] = "账号添加已取消；临时浏览器 Profile 已封存。"
            meta["flow_profile_retired"] = True
            meta["flow_profile_retired_at"] = now.isoformat()
            row.status = "retired"
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_error"] = None
        row.meta_json = meta
        row.active_project_id = None
        row.active_stage_id = None
        row.lease_expires_at = None
        db.add(row)
    return _safe_session(row)


def get_flow_browser_session(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    capture_id: str,
) -> dict[str, Any]:
    return _safe_session(
        _flow_row_for_capture(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            capture_id=capture_id,
        )
    )


def _flow_row_for_capture(
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
    rows = query.all()
    row = next(
        (
            item
            for item in rows
            if is_flow_account_slot(item)
            and str(dict(item.meta_json or {}).get("flow_capture_id") or "") == str(capture_id)
        ),
        None,
    )
    if row is None:
        raise APIError("FLOW_BROWSER_SESSION_NOT_FOUND", "Flow 登录会话不存在。", 404)
    return row


def _sync_upstream_keepalive(
    meta: dict[str, Any],
    *,
    upstream: dict[str, Any],
    now: datetime,
) -> bool:
    """Mirror Flow2API health without turning maintenance into browser churn.

    Flow2API owns ordinary AT renewal from its encrypted ST.  One explicit
    GRANT_EXPIRED episode may request a renderer-backed visit followed by one
    headless capture from the account's immutable Profile. Healthy upstream
    state resets that one-shot budget.
    """
    changed = False
    expiry_raw = upstream.get("at_expires")
    expiry = _parse_time(expiry_raw)
    expiry_text = expiry.isoformat() if expiry is not None else None
    if meta.get("flow_upstream_at_expires") != expiry_text:
        meta["flow_upstream_at_expires"] = expiry_text
        changed = True

    active = bool(upstream.get("is_active"))
    ban_reason = str(upstream.get("ban_reason") or "").strip() or None
    if meta.get("flow_upstream_active") != active:
        meta["flow_upstream_active"] = active
        changed = True
    if meta.get("flow_upstream_ban_reason") != ban_reason:
        meta["flow_upstream_ban_reason"] = ban_reason
        changed = True
    upstream_project_id = str(upstream.get("current_project_id") or "").strip() or None
    if meta.get("flow_upstream_project_id") != upstream_project_id:
        meta["flow_upstream_project_id"] = upstream_project_id
        changed = True

    # Once Flow2API has recovered the grant, release any stale automatic
    # browser-recovery marker immediately.  An old human-required state must
    # not keep a now-healthy account out of the pool.
    if not _is_auto_reauth_reason(ban_reason):
        if (
            str(meta.get("flow_capture_purpose") or "")
            in {"auto_reauth", "reauth_required"}
            and str(meta.get("flow_capture_state") or "")
            in {"failed", "human_required"}
        ):
            meta["flow_capture_state"] = "ready"
            meta["flow_capture_purpose"] = "keepalive"
            meta["flow_capture_message"] = "Flow 授权已由服务端恢复。"
            meta["flow_capture_error"] = None
            changed = True
        for key, value in (
            ("flow_auto_reauth_attempts", 0),
            ("flow_auto_reauth_window_started_at", None),
            ("flow_auto_reauth_next_at", None),
            ("flow_auto_reauth_last_reason", None),
            ("flow_auto_reauth_strategy", None),
            # A healthy observation made by the current code is the boundary
            # between a pre-deployment GRANT_EXPIRED marker and a genuinely
            # new grant failure.  Only failures observed after this boundary
            # may open a browser automatically.
            ("flow_auto_reauth_policy_ready", True),
        ):
            if meta.get(key) != value:
                meta[key] = value
                changed = True

    state = str(meta.get("flow_capture_state") or "")
    if state in {"keepalive_pending", "retry_wait"}:
        meta["flow_capture_state"] = "ready" if active else "failed"
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_message"] = (
            "Flow 服务端登录状态正常，无需打开浏览器。"
            if active
            else "Flow 登录态需要人工重新验证，请使用“浏览器重新登录”。"
        )
        meta["flow_capture_error"] = None if active else (ban_reason or "reauth_required")
        meta["flow_next_retry_at"] = None
        changed = True
    if meta.get("flow_next_keepalive_at") is not None:
        meta["flow_next_keepalive_at"] = None
        changed = True
    return changed


def _is_auto_reauth_reason(value: Any) -> bool:
    return str(value or "").strip().lower() in _AUTO_REAUTH_REASONS


def _schedule_automatic_reauth_once(
    row: HermesBrowserBridge,
    *,
    upstream: dict[str, Any],
    now: datetime,
    base_meta: dict[str, Any] | None = None,
) -> bool:
    """Lease one fixed Profile for one automatic grant-repair transaction.

    GRANT_EXPIRED is not equivalent to a missing Google login. Flow sometimes
    needs its real renderer-backed page to refresh the page-owned grant before
    a fresh session cookie can be validated. Therefore the transaction starts
    in ``awaiting_login`` with ``automatic_visit`` enabled. The bridge visits
    Flow using the immutable Profile and proxy, closes that exact browser, then
    the server advances the same capture id to a short headless cookie capture.

    The strategy marker is observability only. Deploying a new strategy must
    never reset the per-grant attempt budget, because that would reopen every
    failed account Profile after each release.
    """
    # Reconciliation may already have mirrored a newer project, proxy, expiry,
    # or ban state into memory.  Start the browser transaction from that same
    # snapshot so scheduling cannot silently roll those fields back.
    meta = dict(base_meta if base_meta is not None else (row.meta_json or {}))
    if not _is_auto_reauth_reason(upstream.get("ban_reason")):
        return False
    if int(meta.get("flow_token_id") or 0) <= 0:
        return False
    state = str(meta.get("flow_capture_state") or "")
    purpose = str(meta.get("flow_capture_purpose") or "")
    error = str(meta.get("flow_capture_error") or "")
    attempts = int(meta.get("flow_auto_reauth_attempts") or 0)
    strategy = str(meta.get("flow_auto_reauth_strategy") or "")

    # Bridge 2026.08.11.3 accidentally resolved the browser store beneath
    # each logical binding. Existing Flow rows therefore opened a newly-made
    # blank Profile and exhausted their one automatic attempt as
    # ``interactive_login_required``. Version .4 restores the persisted
    # host/slot Profile before Chrome starts and refuses to create another
    # blank Profile for automatic work. Re-arm exactly that known failed
    # transaction once after the fixed Bridge has actually heartbeated.
    recovery_version = str(meta.get("flow_profile_layout_recovery_version") or "")
    agent_version = str(meta.get("agent_version") or "")
    affected_blank_profile_failure = (
        state == "human_required"
        and purpose == "auto_reauth"
        and error == "interactive_login_required"
    ) or (
        state == "ready"
        and purpose == "keepalive"
        and str(meta.get("flow_browser_status") or "") == "login_required"
    )
    if (
        agent_version == _PROFILE_LAYOUT_RECOVERY_AGENT_VERSION
        and recovery_version != _PROFILE_LAYOUT_RECOVERY_AGENT_VERSION
        and bool(meta.get("flow_auto_reauth_policy_ready"))
        and strategy == _AUTO_REAUTH_STRATEGY
        and affected_blank_profile_failure
        and attempts >= _AUTO_REAUTH_MAX_ATTEMPTS_PER_GRANT
    ):
        meta["flow_profile_layout_recovery_version"] = (
            _PROFILE_LAYOUT_RECOVERY_AGENT_VERSION
        )
        meta["flow_auto_reauth_attempts"] = 0
        meta["flow_capture_purpose"] = "reauth_required"
        meta["flow_capture_error"] = "grant_expired"
        attempts = 0
        purpose = "reauth_required"
        error = "grant_expired"
    if state in _ACTIVE_CAPTURE_STATES:
        return False
    if not bool(meta.get("flow_auto_reauth_policy_ready")):
        # Existing account rows can predate the automatic renderer repair.
        # Import their current failure as historical state without opening
        # Chrome.  A later healthy upstream observation resets the attempt
        # budget and arms the policy for the next, newly observed grant loss.
        meta.update(
            {
                "flow_auto_reauth_policy_ready": True,
                "flow_auto_reauth_attempts": _AUTO_REAUTH_MAX_ATTEMPTS_PER_GRANT,
                "flow_auto_reauth_last_reason": str(
                    upstream.get("ban_reason") or ""
                )[:128],
                "flow_auto_reauth_strategy": strategy or _AUTO_REAUTH_STRATEGY,
                "flow_auto_reauth_next_at": None,
            }
        )
        if state not in _ACTIVE_CAPTURE_STATES:
            meta["flow_capture_message"] = (
                "已保留部署前的授权异常；不会自动打开该账号浏览器。"
            )
        row.meta_json = meta
        row.status = "standby"
        return True
    if state == "human_required":
        if attempts > 0:
            return False
        if not (purpose == "reauth_required" and error == "grant_expired"):
            return False
    if attempts >= _AUTO_REAUTH_MAX_ATTEMPTS_PER_GRANT:
        return False
    meta.update(
        {
            "flow_capture_id": "flow_" + secrets.token_hex(20),
            "flow_capture_state": "awaiting_login",
            "flow_capture_purpose": "auto_reauth",
            "flow_capture_created_at": now.isoformat(),
            "flow_capture_updated_at": now.isoformat(),
            "flow_capture_message": "Hermes 正在使用该账号的固定 Profile 后台访问 Flow，并自动刷新授权。",
            "flow_capture_error": None,
            "flow_browser_status": "starting",
            # This must match the proven manual re-auth path exactly. Opening
            # a stale project URL can land on a public/error shell even while
            # the account's root Flow session is still signed in.
            "flow_target_url": FLOW_ACCOUNT_URL,
            "flow_auto_reauth_attempts": attempts + 1,
            "flow_auto_reauth_window_started_at": None,
            "flow_auto_reauth_next_at": None,
            "flow_auto_reauth_last_at": now.isoformat(),
            "flow_auto_reauth_last_reason": str(upstream.get("ban_reason") or "")[:128],
            "flow_auto_reauth_strategy": _AUTO_REAUTH_STRATEGY,
        }
    )
    row.meta_json = meta
    row.status = "pending"
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    return True


def _flow_admin_error_retryable(exc: Flow2ApiAdminError) -> bool:
    if int(exc.status_code) >= 500:
        return True
    message = str(exc).strip().lower()
    terminal_markers = (
        "account identity mismatch",
        "profile identity mismatch",
        "session token is already assigned",
        "invalid session token",
        "session_rejected",
        "grant_expired",
        "登录状态无效",
        "尚未登录",
        "login required",
    )
    return not any(marker in message for marker in terminal_markers)


def _flow_session_candidates(primary: str, candidates: list[str] | None) -> list[str]:
    """Return a bounded, de-duplicated credential set without persisting it.

    Chrome may retain more than one path-scoped rolling NextAuth cookie for the
    exact Flow page.  CDP does not promise an order, so treating its first row
    as authoritative can reject a freshly logged-in account because an older
    cookie happened to be returned first.
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in [primary, *(candidates or [])]:
        value = str(raw or "").strip()
        if value in seen or len(value) < 20 or len(value) > 20_000:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= 8:
            break
    return result


def _flow_candidate_rejected(exc: Flow2ApiAdminError) -> bool:
    """Whether another cookie candidate may be tried safely.

    These failures occur before Flow2API publishes a verified credential
    snapshot.  Network/server failures are deliberately excluded so an outage
    is not multiplied across every candidate.
    """
    if int(exc.status_code) != 400:
        return False
    message = str(exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "session validation failed",
            "session_rejected",
            "grant_expired",
            "credits validation failed",
            "account identity mismatch",
            "invalid session token",
            "no access token",
        )
    )


def reconcile_flow_browser_bindings_from_upstream(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    upstream_tokens: list[dict[str, Any]],
) -> int:
    """Repair a committed-upstream/lost-local-pointer onboarding boundary.

    Only safe Flow2API list metadata is consumed. The exact dedicated browser
    profile is the idempotency key; credentials never flow back into GMV.
    """
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        # The overview endpoint performs this reconciliation after a remote
        # Flow2API read.  A browser capture may concurrently own one exact
        # account row while it validates and imports a fresh session.  Skip
        # that busy row instead of making a read-oriented overview wait for
        # MySQL's lock timeout (and return 500); the next heartbeat/overview
        # will reconcile it from the authoritative upstream state.
        .with_for_update(skip_locked=True)
        .all()
    )
    flow_rows = [row for row in rows if is_flow_account_slot(row)]
    active_devices = {
        str(dict(row.meta_json or {}).get("agent_device_id") or "")
        for row in flow_rows
        if str(dict(row.meta_json or {}).get("flow_capture_state") or "")
        in _ACTIVE_CAPTURE_STATES
    }
    already_bound = {
        int(dict(row.meta_json or {}).get("flow_token_id") or 0)
        for row in flow_rows
        if int(dict(row.meta_json or {}).get("flow_token_id") or 0) > 0
    }
    by_token_id = {
        int(item.get("id") or 0): item
        for item in upstream_tokens
        if isinstance(item, dict) and int(item.get("id") or 0) > 0
    }
    by_profile: dict[str, list[dict[str, Any]]] = {}
    for item in upstream_tokens:
        if not isinstance(item, dict) or not bool(item.get("has_st")):
            continue
        profile = str(item.get("browser_profile_id") or "").strip()
        token_id = int(item.get("id") or 0)
        if profile and token_id > 0:
            by_profile.setdefault(profile, []).append(item)

    repaired = 0
    now = _now()
    for row in flow_rows:
        meta = dict(row.meta_json or {})
        bound_token_id = int(meta.get("flow_token_id") or 0)
        if bound_token_id > 0:
            upstream = by_token_id.get(bound_token_id) or {}
            changed = bool(upstream) and _sync_upstream_keepalive(
                meta,
                upstream=upstream,
                now=now,
            )
            proxy_url = str(upstream.get("captcha_proxy_url") or "").strip()
            if proxy_url and str(meta.get("flow_proxy_url") or "") != proxy_url:
                meta["flow_proxy_url"] = proxy_url
                settings = dict(meta.get("flow_account_settings") or {})
                settings["captcha_proxy_url"] = proxy_url
                meta["flow_account_settings"] = settings
                changed = True
            device_id = str(meta.get("agent_device_id") or "")
            if (
                upstream
                and device_id not in active_devices
                and _schedule_automatic_reauth_once(
                    row, upstream=upstream, now=now, base_meta=meta
                )
            ):
                meta = dict(row.meta_json or {})
                changed = True
                if str(meta.get("flow_capture_state") or "") in _ACTIVE_CAPTURE_STATES:
                    active_devices.add(device_id)
            if changed:
                meta["flow_capture_updated_at"] = now.isoformat()
                row.meta_json = meta
                db.add(row)
                repaired += 1
            continue
        expected_profile = (
            f"{str(meta.get('agent_device_id') or '')}/"
            f"slot-{int(meta.get('local_port') or 0)}"
        )
        matches = by_profile.get(expected_profile, [])
        if len(matches) != 1:
            continue
        token = matches[0]
        token_id = int(token.get("id") or 0)
        if token_id <= 0 or token_id in already_bound:
            continue
        meta.update(
            {
                "flow_token_id": token_id,
                "flow_account_email": str(token.get("email") or "")[:320]
                or None,
                "flow_profile_id": expected_profile,
                "flow_capture_state": "ready",
                "flow_capture_purpose": "keepalive",
                "flow_capture_updated_at": now.isoformat(),
                "flow_capture_message": "已根据固定浏览器 Profile 恢复账号绑定。",
                "flow_capture_error": None,
                "flow_fingerprint_state": (
                    "captured"
                    if str(token.get("browser_fingerprint_state") or "")
                    == "captured"
                    else "missing"
                ),
                "flow_last_keepalive_success_at": now.isoformat(),
                "flow_next_keepalive_at": None,
                "flow_upstream_at_expires": (
                    _parse_time(token.get("at_expires")).isoformat()
                    if _parse_time(token.get("at_expires")) is not None
                    else None
                ),
                "flow_upstream_active": bool(token.get("is_active")),
                "flow_upstream_ban_reason": str(token.get("ban_reason") or "").strip()
                or None,
                "flow_next_retry_at": None,
            }
        )
        row.meta_json = meta
        row.status = "standby"
        row.load_json = {}
        db.add(row)
        already_bound.add(token_id)
        repaired += 1
    return repaired


def flow_slot_should_wake(row: HermesBrowserBridge, *, now: datetime) -> bool:
    """Wake explicit login work or one bounded automatic grant repair."""
    if not is_flow_account_slot(row):
        return False
    meta = dict(row.meta_json or {})
    if str(row.status or "").lower() == "retired" or bool(meta.get("flow_profile_retired")):
        return False
    state = str(meta.get("flow_capture_state") or "")
    if state in _ACTIVE_CAPTURE_STATES:
        started = _parse_time(meta.get("flow_capture_updated_at"))
        timeout_minutes = 15 if state == "awaiting_login" else 8
        if started is not None and started <= now - timedelta(minutes=timeout_minutes):
            auto_reauth = str(meta.get("flow_capture_purpose") or "") == "auto_reauth"
            meta["flow_capture_state"] = "human_required" if auto_reauth else "failed"
            meta["flow_next_retry_at"] = None
            meta["flow_capture_updated_at"] = now.isoformat()
            meta["flow_capture_message"] = (
                "固定 Profile 自动授权超时；本次授权周期不再重试，需要时请人工重新授权。"
                if auto_reauth
                else
                "Flow 登录窗口已超时并自动关闭；需要时请手动重新登录。"
                if state == "awaiting_login"
                else "固定 Profile 采集超时，已关闭本轮浏览器；请手动重新登录后再试。"
            )
            meta["flow_capture_error"] = (
                "auto_reauth_capture_timeout"
                if auto_reauth
                else "interactive_login_timeout"
                if state == "awaiting_login"
                else "bridge_capture_timeout"
            )
            if auto_reauth:
                meta["flow_auto_reauth_next_at"] = None
            row.meta_json = meta
            row.status = "standby"
            return False
        return True
    if state in {"keepalive_pending", "retry_wait"}:
        meta["flow_capture_state"] = "ready" if meta.get("flow_token_id") else "failed"
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_message"] = (
            "已停止后台浏览器保活；登录刷新由 Flow2API 服务端负责。"
        )
        meta["flow_capture_error"] = None
        meta["flow_next_retry_at"] = None
        meta["flow_next_keepalive_at"] = None
        row.meta_json = meta
    return False


def flow_slot_spec(row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    state = str(meta.get("flow_capture_state") or "")
    runnable = (
        str(row.status or "").lower() != "retired"
        and not bool(meta.get("flow_profile_retired"))
    )
    return {
        "purpose": "flow_account",
        "target_url": str(meta.get("flow_target_url") or FLOW_ACCOUNT_URL),
        "capture_id": str(meta.get("flow_capture_id") or ""),
        "capture_required": runnable and state in _ACTIVE_CAPTURE_STATES and state != "awaiting_login",
        "login_only": runnable and state == "awaiting_login",
        # A GRANT_EXPIRED repair starts with a real renderer-backed visit so
        # Flow can refresh its page-owned grant. This is not an interactive
        # login: the bridge opens the immutable Profile minimized, closes only
        # that Profile after a bounded visit, then captures through CDP.
        "automatic_visit": (
            runnable
            and state == "awaiting_login"
            and str(meta.get("flow_capture_purpose") or "") == "auto_reauth"
            and str(meta.get("flow_auto_reauth_strategy") or "")
            == _AUTO_REAUTH_STRATEGY
        ),
        "flow_token_id": int(meta.get("flow_token_id")) if meta.get("flow_token_id") else None,
        "proxy_url": str(meta.get("flow_proxy_url") or ""),
    }


def record_flow_browser_report(
    row: HermesBrowserBridge, report: dict[str, Any], *, now: datetime
) -> bool:
    meta = dict(row.meta_json or {})
    if (
        str(row.status or "").lower() == "retired"
        or bool(meta.get("flow_profile_retired"))
        or str(meta.get("flow_capture_state") or "") == "cancelled"
    ):
        # A late heartbeat from a browser that was just cancelled must never
        # revive the onboarding state and cause Chrome to be launched again.
        return False
    state = str(meta.get("flow_capture_state") or "")
    current_capture_id = str(meta.get("flow_capture_id") or "").strip()
    reported_capture_id = str(report.get("capture_id") or "").strip()
    if (
        state in _ACTIVE_CAPTURE_STATES
        and current_capture_id
        and reported_capture_id != current_capture_id
    ):
        # Agent heartbeats are asynchronous.  Immediately after a new
        # server-owned capture cycle is created, one last report from the old
        # browser runtime can still arrive.  Never let that stale result (most
        # importantly ``login_required``) terminate the new automatic repair.
        return False
    status = str(report.get("flow_status") or "checking").strip().lower()
    if status not in {"starting", "checking", "login_required", "login_complete", "capturing", "submitted", "ready"}:
        status = "checking"
    meta["flow_browser_status"] = status
    meta["flow_browser_checked_at"] = now.isoformat()
    meta["flow_page_url"] = str(report.get("page_url") or "")[:1000]
    diagnostics = report.get("session_diagnostics")
    if isinstance(diagnostics, dict):
        safe_diagnostics: dict[str, Any] = {}
        candidate_count = diagnostics.get("candidate_count")
        if isinstance(candidate_count, int):
            safe_diagnostics["candidate_count"] = max(0, min(candidate_count, 100))
        window_login_state = diagnostics.get("window_login_state")
        if isinstance(window_login_state, bool) or window_login_state is None:
            safe_diagnostics["window_login_state"] = window_login_state
        for key in ("local_storage_keys", "document_cookie_names"):
            values = diagnostics.get(key)
            if isinstance(values, list):
                safe_diagnostics[key] = [str(value)[:128] for value in values[:80]]
        cookies = diagnostics.get("applicable_cookies")
        if isinstance(cookies, list):
            safe_diagnostics["applicable_cookies"] = [
                {
                    "name": str(cookie.get("name") or "")[:128],
                    "domain": str(cookie.get("domain") or "")[:255],
                    "path": str(cookie.get("path") or "")[:255],
                    "expired": bool(cookie.get("expired")),
                }
                for cookie in cookies[:80]
                if isinstance(cookie, dict)
            ]
        meta["flow_session_diagnostics"] = safe_diagnostics
    if (
        status == "login_complete"
        and str(meta.get("flow_capture_state") or "") == "awaiting_login"
    ):
        # The user closed a normal, non-debug Chrome after signing in. Wake
        # the exact same profile once more with CDP only for local capture.
        meta["flow_capture_state"] = "capture_pending"
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_message"] = "登录窗口已关闭，正在从同一固定 Profile 安全采集登录态。"
    elif (
        status == "login_required"
        and str(meta.get("flow_capture_state") or "") == "capture_pending"
    ):
        if str(meta.get("flow_capture_purpose") or "") == "auto_reauth":
            meta["flow_capture_state"] = "human_required"
            meta["flow_capture_updated_at"] = now.isoformat()
            meta["flow_capture_message"] = (
                "固定 Profile 已进入登录或账号验证页面，需要管理员人工完成后再重新授权。"
            )
            meta["flow_capture_error"] = "interactive_login_required"
            meta["flow_auto_reauth_next_at"] = None
            row.meta_json = meta
            return True
        # The normal window was closed before Google/Flow login finished. A
        # CDP browser must never become an interactive Google login surface;
        # return the same profile to the normal-login phase instead.
        meta["flow_capture_state"] = "awaiting_login"
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_message"] = (
            "尚未检测到有效 Flow 登录。已切回普通 Chrome，请登录并确认 Flow 可访问后再关闭窗口。"
        )
    row.meta_json = meta
    return True


async def ingest_flow_browser_capture(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    bridge_id: str,
    capture_id: str,
    session_token: str,
    session_tokens: list[str] | None = None,
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
    if row is None or not is_flow_account_slot(row):
        raise APIError("FLOW_BROWSER_SLOT_FORBIDDEN", "Flow 浏览器 Slot 不属于当前设备。", 403)
    meta = dict(row.meta_json or {})
    if str(meta.get("agent_device_id") or "") != str(device_id):
        raise APIError("FLOW_BROWSER_DEVICE_MISMATCH", "Flow 浏览器设备不匹配。", 403)
    if str(meta.get("flow_capture_id") or "") != str(capture_id):
        raise APIError("FLOW_BROWSER_CAPTURE_STALE", "该浏览器采集已过期。", 409)
    if str(meta.get("flow_capture_state") or "") == "ready":
        return {"success": True, "token_id": meta.get("flow_token_id")}

    credential_candidates = _flow_session_candidates(session_token, session_tokens)
    if not credential_candidates:
        raise APIError("FLOW_BROWSER_SESSION_INVALID", "浏览器没有返回有效的 Flow 登录会话。", 400)
    normalized_fingerprint = _normalize_fingerprint(fingerprint)
    expected_profile = f"{device_id}/slot-{int(meta.get('local_port') or 0)}"
    if str(profile_id or "") != expected_profile:
        raise APIError("FLOW_BROWSER_PROFILE_MISMATCH", "Flow 浏览器 Profile 身份不匹配。", 403)

    settings = dict(meta.get("flow_account_settings") or {})
    token_id = int(meta.get("flow_token_id") or 0) or None
    payload = {
        "browser_profile_id": expected_profile,
        "browser_fingerprint": normalized_fingerprint,
        "remark": settings.get("remark"),
        "image_enabled": bool(settings.get("image_enabled", False)),
        "video_enabled": bool(settings.get("video_enabled", True)),
        "image_concurrency": int(settings.get("image_concurrency", 1)),
        "video_concurrency": int(settings.get("video_concurrency", 1)),
        "captcha_proxy_url": str(settings.get("captcha_proxy_url") or meta.get("flow_proxy_url") or "").strip(),
    }

    now = _now()
    admin = Flow2ApiAdminClient()
    try:
        if token_id is None:
            # The upstream account may already exist when Flow2API committed
            # successfully but an older concurrent bridge heartbeat used to
            # overwrite GMV's pointer. Exact profile identity is the durable
            # idempotency key; reconcile it instead of creating a duplicate.
            upstream_tokens = await admin.request("GET", "/api/tokens")
            profile_matches = [
                int(item.get("id") or 0)
                for item in upstream_tokens
                if isinstance(item, dict)
                and str(item.get("browser_profile_id") or "") == expected_profile
                and int(item.get("id") or 0) > 0
            ] if isinstance(upstream_tokens, list) else []
            profile_matches = sorted(set(profile_matches))
            if len(profile_matches) > 1:
                raise APIError(
                    "FLOW_BROWSER_PROFILE_AMBIGUOUS",
                    "Flow2API 中存在重复的浏览器 Profile 绑定，请先清理账号池。",
                    409,
                )
            if profile_matches:
                token_id = int(profile_matches[0])
        method, path = (
            ("PUT", f"/api/tokens/{token_id}")
            if token_id is not None
            else ("POST", "/api/tokens")
        )
        result: dict[str, Any] | Any | None = None
        last_candidate_error: Flow2ApiAdminError | None = None
        for candidate in credential_candidates:
            try:
                result = await admin.request(
                    method,
                    path,
                    payload={**payload, "st": candidate},
                )
                break
            except Flow2ApiAdminError as exc:
                last_candidate_error = exc
                if not _flow_candidate_rejected(exc):
                    raise
        if result is None and last_candidate_error is not None:
            raise last_candidate_error
        # Both create and update validate the ST and mint a fresh AT, but older
        # Flow2API responses do not include its authoritative expiry.  Read the
        # safe token list after the mutation so the next browser wake is based
        # on the new expiry instead of a hard-coded interval.
        refreshed_tokens = await admin.request("GET", "/api/tokens")
        if isinstance(refreshed_tokens, list):
            refreshed = next(
                (
                    item
                    for item in refreshed_tokens
                    if isinstance(item, dict)
                    and (
                        (token_id is not None and int(item.get("id") or 0) == token_id)
                        or str(item.get("browser_profile_id") or "") == expected_profile
                    )
                ),
                None,
            )
            if refreshed is not None:
                result = {**(result if isinstance(result, dict) else {}), "token": refreshed}
    except Flow2ApiAdminError as exc:
        meta["flow_capture_updated_at"] = now.isoformat()
        meta["flow_capture_error"] = str(exc)[:500]
        retryable = _flow_admin_error_retryable(exc)
        auto_reauth = str(meta.get("flow_capture_purpose") or "") == "auto_reauth"
        if retryable:
            meta["flow_capture_state"] = "failed"
            meta["flow_next_retry_at"] = None
            if auto_reauth:
                meta["flow_capture_state"] = "human_required"
                meta["flow_capture_purpose"] = "reauth_required"
                meta["flow_auto_reauth_next_at"] = None
                meta["flow_capture_message"] = (
                    "旧的后台浏览器授权任务已停止；请稍后手动重新授权。"
                )
            else:
                meta["flow_capture_message"] = "Flow2API 暂时不可用，本轮已停止；请稍后手动重试。"
        elif "grant_expired" in str(exc).lower():
            meta["flow_capture_state"] = "human_required" if auto_reauth else "failed"
            meta["flow_capture_message"] = (
                "登录态已采集，但 Google API 仍拒绝授权。请重新登录后在 Flow 内打开或创建项目，"
                "完成 Google 的账号验证提示，再关闭窗口。"
            )
        else:
            meta["flow_capture_state"] = "failed"
            meta["flow_capture_message"] = "账号验证未通过，请检查浏览器中的 Google Flow 登录状态。"
        row.meta_json = meta
        row.status = "standby"
        db.add(row)
        return {
            "success": False,
            "retry": retryable,
            "message": str(meta["flow_capture_message"]),
        }

    upstream_token = result.get("token") if isinstance(result, dict) else None
    resolved_token_id = token_id
    if resolved_token_id is None and isinstance(upstream_token, dict):
        resolved_token_id = int(upstream_token.get("id") or 0) or None
    if resolved_token_id is None:
        raise APIError("FLOW_BROWSER_IMPORT_INVALID", "Flow2API 未返回账号 ID。", 502)

    email = upstream_token.get("email") if isinstance(upstream_token, dict) else None
    upstream_expiry = (
        upstream_token.get("at_expires") if isinstance(upstream_token, dict) else None
    )
    meta.update(
        {
            "flow_token_id": int(resolved_token_id),
            "flow_account_email": str(email or meta.get("flow_account_email") or "")[:320] or None,
            "flow_profile_id": expected_profile,
            "flow_capture_state": "ready",
            "flow_capture_purpose": "keepalive",
            "flow_capture_updated_at": now.isoformat(),
            "flow_capture_message": "账号已验证并绑定固定浏览器 Profile。",
            "flow_capture_error": None,
            "flow_fingerprint_state": "captured",
            "flow_fingerprint_digest": hashlib.sha256(
                repr(sorted(normalized_fingerprint.items())).encode("utf-8")
            ).hexdigest(),
            "flow_last_keepalive_success_at": now.isoformat(),
            "flow_next_keepalive_at": None,
            "flow_upstream_at_expires": (
                _parse_time(upstream_expiry).isoformat()
                if _parse_time(upstream_expiry) is not None
                else None
            ),
            "flow_upstream_active": (
                bool(upstream_token.get("is_active"))
                if isinstance(upstream_token, dict)
                else True
            ),
            "flow_upstream_ban_reason": (
                str(upstream_token.get("ban_reason") or "").strip() or None
                if isinstance(upstream_token, dict)
                else None
            ),
            "flow_next_retry_at": None,
            "flow_auto_reauth_attempts": 0,
            "flow_auto_reauth_window_started_at": None,
            "flow_auto_reauth_next_at": None,
            "flow_auto_reauth_last_reason": None,
            "flow_auto_reauth_strategy": None,
        }
    )
    row.meta_json = meta
    row.status = "standby"
    row.load_json = {}
    db.add(row)
    return {"success": True, "token_id": int(resolved_token_id)}


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
        raise APIError("FLOW_BROWSER_FINGERPRINT_INVALID", "浏览器指纹采集不完整。", 400)
    return result


__all__ = [
    "FLOW_ACCOUNT_URL",
    "cancel_flow_browser_onboarding",
    "flow_slot_should_wake",
    "flow_slot_spec",
    "get_flow_browser_session",
    "ingest_flow_browser_capture",
    "is_flow_account_slot",
    "list_flow_browser_sessions",
    "record_flow_browser_report",
    "reconcile_flow_browser_bindings_from_upstream",
    "start_flow_browser_onboarding",
]
