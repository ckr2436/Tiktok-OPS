from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.doubao_lab import decrypt_doubao_session_context, is_doubao_lab_slot
from app.services.doubao_provider.membership import (
    FREE_DURATIONS,
    allowed_durations,
    membership_payload,
    set_membership_tier,
    supports_duration,
)
from app.services.doubao_provider.capability import (
    UNKNOWN,
    apply_seedance_capability_result,
    seedance_capability_ready,
    seedance_capability_state,
)
from app.services.doubao_provider.health import (
    AUTH_REQUIRED,
    AUTH_UNKNOWN,
    NETWORK_REACHABLE,
    NETWORK_REGION_RESTRICTED,
    authentication_is_fresh,
    authentication_state,
    mark_auth_probe_result,
    mark_authenticated,
    parse_datetime,
)
from app.services.flow_proxy_pool import resolve_flow_proxy_url
from app.services.doubao_provider.device_health import (
    agent_is_online,
    device_circuit_is_open,
    device_circuit_seconds,
    device_is_available,
    physical_agent_device_id,
)


LEASE_MINUTES = 20
TRANSIENT_COOLDOWN_MINUTES = 3
QUOTA_COOLDOWN_HOURS = 6


class DoubaoPoolBusyError(RuntimeError):
    """Healthy Doubao accounts exist, but every browser lane is occupied.

    This is ordinary local backpressure, not an account/provider outage.  The
    caller must keep the same logical video task queued and try it again after
    a short delay without consuming a provider or content-revision budget.
    """

_TRANSIENT_CAPABILITY_ERRORS = {
    "doubao_risk_rate_limited",
    "doubao_failed",
    "doubao_timeout",
    "doubao_browser_unstable",
    "doubao_composer_unavailable",
    # The session and account remain valid when Doubao answers in chat text
    # without starting Seedance.  This is a transient composer/mode outcome;
    # after cooldown the account must re-enter rotation instead of remaining
    # permanently stranded in capability=unknown.
    "doubao_text_only_response",
}
_NEUTRAL_LEASE_RELEASE_CODES = {
    # These are local ownership decisions, not observations about the remote
    # account, login, quota, composer or Seedance capability.  Releasing a
    # superseded Content Factory child must not poison an otherwise healthy
    # account and remove it from the production pool.
    "cf_variant_superseded",
    "cf_task_not_authoritative",
    # These outcomes describe one local delivery or one remote conversation,
    # not the account's durable Seedance capability.  The current logical
    # request already excludes the account before rotating, so poisoning the
    # global pool here only strands otherwise healthy accounts indefinitely.
    "doubao_submit_unconfirmed",
    "doubao_silent_timeout",
    "doubao_text_only_response",
    # Browser/Bridge liveness is a device-level condition.  It must not lower
    # Seedance capability or cool every account bound to the same PC.
    "doubao_browser_unstable",
    "doubao_device_offline",
}
_DEVICE_INFRA_ERROR_CODES = {
    "doubao_browser_unstable",
    "doubao_device_offline",
}
_AUTO_AUTH_RECOVERY_ERRORS = {
    "doubao_auth_required",
    "doubao_account_context_invalid",
    "doubao_auth_probe_inconclusive",
}


def utcnow_naive() -> datetime:
    # Hermes bridge metadata uses the server's local naive datetime convention.
    return datetime.now()


def _parse(value: Any) -> datetime | None:
    return parse_datetime(value)


def _pool_enabled(meta: dict[str, Any]) -> bool:
    # Existing verified lab profiles are promoted automatically.  An operator
    # can explicitly disable one account without deleting its encrypted login.
    return bool(meta.get("doubao_pool_enabled", True))


def auth_probe_eligible(meta: dict[str, Any]) -> bool:
    """Allow normal checks plus bounded recovery of encrypted login state.

    CAPTCHA and region restrictions require a different recovery path and are
    deliberately excluded.  An account disabled only because a restarted
    Profile appeared logged out may be rechecked through the quota-free
    account endpoint using its encrypted session capture.
    """

    if bool(meta.get("doubao_pool_enabled", True)) and str(
        meta.get("doubao_capture_state") or ""
    ) == "ready":
        return True
    return bool(
        authentication_state(meta) == AUTH_REQUIRED
        and str(meta.get("doubao_capture_state") or "") == "failed"
        and str(meta.get("doubao_pool_last_error") or "")
        in _AUTO_AUTH_RECOVERY_ERRORS
        and str(meta.get("doubao_manual_verification_state") or "")
        not in {"preparing", "awaiting_human"}
    )


def _clear_expired_lease(meta: dict[str, Any], *, now: datetime) -> bool:
    expires = _parse(meta.get("doubao_pool_lease_expires_at"))
    lease_task_id = str(meta.get("doubao_pool_lease_task_id") or "")
    browser_task_id = str(meta.get("doubao_provider_browser_task_id") or "")
    if lease_task_id and (expires is None or expires <= now):
        if browser_task_id == lease_task_id:
            meta["doubao_provider_browser_task_id"] = None
        meta["doubao_provider_browser_hold_until"] = None
        meta["doubao_provider_submission_accepted_at"] = None
        meta["doubao_pool_lease_task_id"] = None
        meta["doubao_pool_lease_expires_at"] = None
        return True
    if browser_task_id and not lease_task_id:
        # A browser marker is only valid while the exact same generation lease
        # is alive.  Clearing an orphan here prevents an expired submission
        # from repeatedly reopening the fixed Windows Profile.
        meta["doubao_provider_browser_task_id"] = None
        meta["doubao_provider_browser_hold_until"] = None
        meta["doubao_provider_submission_accepted_at"] = None
        return True
    return False


def _network_lane(meta: dict[str, Any]) -> tuple[str, int]:
    if str(meta.get("doubao_network_mode") or "proxy") == "direct":
        return ("direct", 0)
    return ("proxy", int(meta.get("doubao_proxy_id") or 0))


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    observed_at = _parse(value)
    if observed_at is None:
        return None
    return max(0.0, (now - observed_at).total_seconds())


def account_dispatch_score(
    row: HermesBrowserBridge, *, now: datetime | None = None
) -> float:
    """Rank eligible accounts by observed production reliability.

    Readiness remains a hard eligibility boundary in ``account_is_retry_candidate``.
    This score only orders accounts that already passed that boundary.  A recent
    real video success is stronger evidence than an old capability probe, while
    repeated submit failures and slow accepted submits lower confidence.  LRU is
    retained as the final fairness tie-breaker in ``claim_account``.
    """

    current = now or utcnow_naive()
    meta = dict(row.meta_json or {})
    score = 0.0

    success_age = _age_seconds(meta.get("doubao_pool_last_success_at"), now=current)
    if success_age is not None:
        if success_age <= 24 * 60 * 60:
            score += 80.0
        elif success_age <= 7 * 24 * 60 * 60:
            score += 55.0
        elif success_age <= 30 * 24 * 60 * 60:
            score += 30.0
        else:
            score += 10.0
    score += min(20, max(0, int(meta.get("doubao_pool_success_count") or 0)))

    auth_age = _age_seconds(
        meta.get("doubao_last_auth_probe_at") or meta.get("doubao_auth_checked_at"),
        now=current,
    )
    if auth_age is not None:
        score += 15.0 if auth_age <= 60 * 60 else 7.0 if auth_age <= 6 * 60 * 60 else 0.0

    capability_age = _age_seconds(
        meta.get("doubao_seedance_capability_checked_at"), now=current
    )
    if capability_age is not None:
        score += (
            15.0
            if capability_age <= 15 * 60
            else 8.0
            if capability_age <= 60 * 60
            else 3.0
            if capability_age <= 6 * 60 * 60
            else 0.0
        )

    consecutive_errors = max(
        0, int(meta.get("doubao_pool_consecutive_errors") or 0)
    )
    score -= min(150.0, float(consecutive_errors) * 50.0)
    if str(meta.get("doubao_pool_last_error") or "").strip():
        score -= 15.0

    try:
        submit_latency_ms = max(
            0.0, float(meta.get("doubao_pool_submit_latency_ewma_ms") or 0.0)
        )
    except (TypeError, ValueError):
        submit_latency_ms = 0.0
    score -= min(25.0, submit_latency_ms / 5_000.0)
    return round(score, 3)


def record_submit_observation(
    row: HermesBrowserBridge,
    *,
    duration_ms: int,
    success: bool,
    error_code: str | None = None,
) -> None:
    """Persist bounded, secret-free submit telemetry for routing decisions."""

    meta = dict(row.meta_json or {})
    observed_ms = max(0, int(duration_ms))
    now = utcnow_naive()
    meta["doubao_pool_last_submit_at"] = now.isoformat()
    meta["doubao_pool_last_submit_duration_ms"] = observed_ms
    meta["doubao_pool_last_submit_outcome"] = "accepted" if success else "failed"
    if success:
        try:
            previous = max(
                0.0, float(meta.get("doubao_pool_submit_latency_ewma_ms") or 0.0)
            )
        except (TypeError, ValueError):
            previous = 0.0
        # Favor recent behavior without letting one outlier erase history.
        meta["doubao_pool_submit_latency_ewma_ms"] = int(
            observed_ms if previous <= 0 else (previous * 0.7) + (observed_ms * 0.3)
        )
        meta["doubao_pool_last_submit_success_at"] = now.isoformat()
        meta["doubao_pool_last_submit_error"] = None
    else:
        meta["doubao_pool_last_submit_failure_at"] = now.isoformat()
        meta["doubao_pool_last_submit_error"] = str(
            error_code or "doubao_submit_failed"
        )[:64]
    row.meta_json = meta


def _browser_lane_is_busy(meta: dict[str, Any], *, now: datetime) -> bool:
    lease_task_id = str(meta.get("doubao_pool_lease_task_id") or "")
    browser_task_id = str(meta.get("doubao_provider_browser_task_id") or "")
    expires = _parse(meta.get("doubao_pool_lease_expires_at"))
    browser_hold_until = _parse(meta.get("doubao_provider_browser_hold_until"))
    submission_accepted_at = _parse(
        meta.get("doubao_provider_submission_accepted_at")
    )
    return bool(
        lease_task_id
        and browser_task_id == lease_task_id
        and expires is not None
        and expires > now
        # The shared proxy lane protects only the browser submission phase.
        # Once Doubao has returned a durable conversation id, the exact
        # account/Profile may remain open while the media node appears without
        # preventing another account on the same proxy from submitting.
        and submission_accepted_at is None
        and (browser_hold_until is None or browser_hold_until > now)
    )


def account_has_valid_session(row: HermesBrowserBridge) -> bool:
    meta = dict(row.meta_json or {})
    return bool(
        is_doubao_lab_slot(row)
        and str(row.status or "").lower() != "retired"
        and not bool(meta.get("doubao_profile_retired"))
        and _pool_enabled(meta)
        and str(meta.get("doubao_capture_state") or "") == "ready"
        and meta.get("doubao_session_context_ciphertext")
    )


def account_has_saved_session(row: HermesBrowserBridge) -> bool:
    """Return whether an encrypted capture exists, independent of routing."""

    meta = dict(row.meta_json or {})
    return bool(
        is_doubao_lab_slot(row)
        and str(row.status or "").lower() != "retired"
        and not bool(meta.get("doubao_profile_retired"))
        and meta.get("doubao_session_context_ciphertext")
    )


def account_is_ready(row: HermesBrowserBridge, *, now: datetime | None = None) -> bool:
    meta = dict(row.meta_json or {})
    current = now or utcnow_naive()
    cooldown = _parse(meta.get("doubao_pool_cooldown_until"))
    capacity_retry = _parse(meta.get("doubao_pool_capacity_retry_at"))
    return bool(
        account_has_valid_session(row)
        and device_is_available(row, now=current)
        and authentication_is_fresh(meta, now=current)
        and seedance_capability_ready(meta)
        and (cooldown is None or cooldown <= current)
        and (capacity_retry is None or capacity_retry <= current)
    )


def account_is_retry_candidate(
    row: HermesBrowserBridge, *, now: datetime | None = None
) -> bool:
    meta = dict(row.meta_json or {})
    current = now or utcnow_naive()
    cooldown = _parse(meta.get("doubao_pool_cooldown_until"))
    capacity_retry = _parse(meta.get("doubao_pool_capacity_retry_at"))
    capability_state = seedance_capability_state(meta)
    last_error = str(meta.get("doubao_pool_last_error") or "").strip()
    capability_recovered = bool(
        seedance_capability_ready(meta)
        or (
            capability_state in {"rate_limited", "unknown"}
            and last_error
            in (_TRANSIENT_CAPABILITY_ERRORS | _NEUTRAL_LEASE_RELEASE_CODES)
            and (cooldown is None or cooldown <= current)
        )
    )
    return bool(
        account_has_valid_session(row)
        and device_is_available(row, now=current)
        and authentication_is_fresh(meta, now=current)
        and capability_recovered
        and (cooldown is None or cooldown <= current)
        and (capacity_retry is None or capacity_retry <= current)
    )


def claim_account(
    db: Session,
    *,
    task_id: int,
    excluded_bridge_ids: set[str] | None = None,
    requested_duration: int | None = None,
) -> HermesBrowserBridge:
    now = utcnow_naive()
    excluded = {str(value) for value in (excluded_bridge_ids or set()) if str(value)}
    rows = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.status != "retired")
        .order_by(HermesBrowserBridge.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    active_browser_lanes: set[tuple[str, int]] = set()
    for row in rows:
        if not is_doubao_lab_slot(row):
            continue
        meta = dict(row.meta_json or {})
        if _clear_expired_lease(meta, now=now):
            row.meta_json = meta
            db.add(row)
        if device_is_available(row, now=now) and _browser_lane_is_busy(meta, now=now):
            active_browser_lanes.add(_network_lane(meta))

    eligible: list[HermesBrowserBridge] = []
    busy_candidate_count = 0
    for row in rows:
        if not is_doubao_lab_slot(row):
            continue
        if str(row.bridge_id) in excluded:
            continue
        meta = dict(row.meta_json or {})
        if requested_duration is not None and not supports_duration(meta, requested_duration):
            continue
        lease_task = meta.get("doubao_pool_lease_task_id")
        if str(lease_task or "") == str(int(task_id)):
            if device_is_available(row, now=now):
                return row
            # A retry must not remain pinned to a dead physical Agent for the
            # rest of the 20-minute advisory lease.  Release only this local
            # browser ownership; any accepted remote conversation uses
            # ``leased_account`` and never enters ``claim_account`` again.
            if str(meta.get("doubao_provider_browser_task_id") or "") == str(
                int(task_id)
            ):
                meta["doubao_provider_browser_task_id"] = None
            meta["doubao_provider_browser_hold_until"] = None
            meta["doubao_provider_submission_accepted_at"] = None
            meta["doubao_pool_lease_task_id"] = None
            meta["doubao_pool_lease_expires_at"] = None
            meta["doubao_pool_last_neutral_release"] = "doubao_device_offline"
            meta["doubao_pool_last_neutral_release_at"] = now.isoformat()
            row.meta_json = meta
            db.add(row)
            continue
        retry_candidate = account_is_retry_candidate(row, now=now)
        if lease_task:
            if retry_candidate:
                busy_candidate_count += 1
            continue
        if not retry_candidate:
            continue
        if _network_lane(meta) in active_browser_lanes:
            busy_candidate_count += 1
            continue
        eligible.append(row)
    if not eligible:
        if busy_candidate_count:
            raise DoubaoPoolBusyError(
                "豆包账号健康，但当前浏览器网络通道正忙；任务将自动排队。"
            )
        if requested_duration is not None and int(requested_duration) > max(FREE_DURATIONS):
            raise RuntimeError(
                f"豆包号池没有支持 {int(requested_duration)} 秒的加强套餐账号。"
            )
        raise RuntimeError("豆包自建号池当前没有可用账号，请等待冷却或重新登录。")
    eligible.sort(
        key=lambda row: (
            -account_dispatch_score(row, now=now),
            _parse(dict(row.meta_json or {}).get("doubao_pool_last_used_at"))
            or datetime.min,
            int(row.id),
        )
    )
    row = eligible[0]
    meta = dict(row.meta_json or {})
    meta.update(
        {
            "doubao_pool_enabled": True,
            "doubao_pool_lease_task_id": int(task_id),
            "doubao_pool_lease_expires_at": (
                now + timedelta(minutes=LEASE_MINUTES)
            ).isoformat(),
            "doubao_pool_last_used_at": now.isoformat(),
            # This marker owns the browser submit and post-acknowledgement
            # media-start phase.  The browser closes only after a video_model
            # appears or the bounded non-video timeout expires; the account
            # lease remains alive for asynchronous polling/download.
            "doubao_provider_browser_task_id": int(task_id),
            "doubao_provider_browser_hold_until": None,
            "doubao_provider_submission_accepted_at": None,
        }
    )
    row.meta_json = meta
    db.add(row)
    db.flush()
    return row


def leased_account(db: Session, *, bridge_id: str, task_id: int) -> HermesBrowserBridge:
    row = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.bridge_id == str(bridge_id),
            HermesBrowserBridge.status != "retired",
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None or not is_doubao_lab_slot(row):
        raise RuntimeError("豆包任务绑定的账号不存在。")
    meta = dict(row.meta_json or {})
    now = utcnow_naive()
    _clear_expired_lease(meta, now=now)
    lease_task_id = str(meta.get("doubao_pool_lease_task_id") or "")
    if lease_task_id != str(int(task_id)):
        # A remote generation is permanently bound to the account that
        # submitted it.  A worker restart or long provider queue may outlive
        # the advisory lease; when the same account is still idle and healthy,
        # reclaim it for that durable task instead of abandoning a paid result.
        if lease_task_id or not account_has_valid_session(row):
            raise RuntimeError("豆包账号租约已过期，任务将重新调度。")
        meta["doubao_pool_lease_task_id"] = int(task_id)
        meta["doubao_pool_lease_recovered_at"] = now.isoformat()
    meta["doubao_pool_lease_expires_at"] = (
        now + timedelta(minutes=LEASE_MINUTES)
    ).isoformat()
    row.meta_json = meta
    db.add(row)
    return row


def account_request_payload(db: Session, row: HermesBrowserBridge) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    context = decrypt_doubao_session_context(meta)
    if context is None:
        raise RuntimeError("豆包账号登录上下文缺失。")
    proxy_url = ""
    if str(meta.get("doubao_network_mode") or "proxy") != "direct":
        proxy_url = resolve_flow_proxy_url(
            db, int(meta.get("doubao_proxy_id") or 0), require_active=True
        )
    return {**context, "proxy_url": proxy_url}


def release_account(
    db: Session,
    row: HermesBrowserBridge,
    *,
    task_id: int,
    success: bool,
    error_code: str | None = None,
) -> None:
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_pool_lease_task_id") or "") != str(int(task_id)):
        return
    now = utcnow_naive()
    if str(meta.get("doubao_provider_browser_task_id") or "") == str(int(task_id)):
        meta["doubao_provider_browser_task_id"] = None
    meta["doubao_provider_browser_hold_until"] = None
    meta["doubao_provider_submission_accepted_at"] = None
    meta["doubao_pool_lease_task_id"] = None
    meta["doubao_pool_lease_expires_at"] = None
    if success:
        meta = apply_seedance_capability_result(meta, success=True)
        meta = mark_authenticated(meta, now=now)
        meta["doubao_pool_last_success_at"] = now.isoformat()
        meta["doubao_pool_success_count"] = int(
            meta.get("doubao_pool_success_count") or 0
        ) + 1
        meta["doubao_pool_consecutive_errors"] = 0
        meta["doubao_pool_last_error"] = None
        meta["doubao_pool_cooldown_until"] = None
        meta["doubao_pool_capacity_state"] = "available"
        meta["doubao_pool_capacity_exhausted_at"] = None
        meta["doubao_pool_capacity_retry_at"] = None
        meta["doubao_capture_state"] = "ready"
    else:
        code = str(error_code or "doubao_failed")[:64]
        if code in _NEUTRAL_LEASE_RELEASE_CODES:
            meta["doubao_pool_last_neutral_release"] = code
            meta["doubao_pool_last_neutral_release_at"] = now.isoformat()
            row.meta_json = meta
            db.add(row)
            if code in _DEVICE_INFRA_ERROR_CODES:
                _open_device_circuit(db, row, error_code=code, now=now)
            return
        if code in {
            "doubao_membership_required",
            "doubao_face_ref_unsupported",
            "doubao_content_rejected",
        }:
            # The account is healthy; the request exceeded its tier or hit a
            # content/reference restriction. Do not cool a usable account.
            meta["doubao_pool_last_error"] = code
            meta["doubao_pool_last_request_rejected_at"] = now.isoformat()
            row.meta_json = meta
            db.add(row)
            return
        count = int(meta.get("doubao_pool_consecutive_errors") or 0) + 1
        meta["doubao_pool_consecutive_errors"] = count
        meta["doubao_pool_last_error"] = code
        meta["doubao_pool_last_error_at"] = now.isoformat()
        if code == "doubao_quota_exhausted":
            retry_at = now + timedelta(hours=QUOTA_COOLDOWN_HOURS)
            meta["doubao_pool_capacity_state"] = "exhausted"
            meta["doubao_pool_capacity_exhausted_at"] = now.isoformat()
            meta["doubao_pool_capacity_retry_at"] = retry_at.isoformat()
            meta["doubao_pool_cooldown_until"] = retry_at.isoformat()
        elif code in {
            "doubao_captcha_required",
            "doubao_auth_required",
            "doubao_account_context_invalid",
        }:
            meta = apply_seedance_capability_result(
                meta, success=False, error_code=code
            )
            meta["doubao_capture_state"] = (
                "captcha_required" if code == "doubao_captcha_required" else "failed"
            )
            meta["doubao_pool_enabled"] = False
            meta["doubao_pool_cooldown_until"] = None
            meta = mark_auth_probe_result(
                meta,
                state=AUTH_REQUIRED,
                network_state=NETWORK_REACHABLE,
                error_code=code,
                now=now,
            )
        elif code == "doubao_region_restricted":
            # A fixed proxy that is rejected by Doubao will not heal by
            # repeatedly reopening the same Profile. Keep the valid login
            # envelope, remove the account from routing, and require an
            # explicit network change/probe before it can re-enter the pool.
            meta = apply_seedance_capability_result(
                meta, success=False, error_code=code
            )
            meta["doubao_capture_state"] = "ready"
            meta["doubao_capture_error"] = code
            meta["doubao_pool_enabled"] = False
            meta["doubao_pool_cooldown_until"] = None
            meta = mark_auth_probe_result(
                meta,
                state=AUTH_UNKNOWN,
                network_state=NETWORK_REGION_RESTRICTED,
                error_code=code,
                now=now,
            )
        else:
            meta = apply_seedance_capability_result(
                meta, success=False, error_code=code
            )
            meta["doubao_pool_cooldown_until"] = (
                now + timedelta(minutes=TRANSIENT_COOLDOWN_MINUTES)
            ).isoformat()
    row.meta_json = meta
    db.add(row)


def _open_device_circuit(
    db: Session,
    row: HermesBrowserBridge,
    *,
    error_code: str,
    now: datetime,
) -> None:
    """Temporarily fence one physical Agent without poisoning its accounts."""

    device_id = physical_agent_device_id(row)
    if not device_id:
        return
    until = now + timedelta(seconds=device_circuit_seconds())
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(row.workspace_id),
            HermesBrowserBridge.user_id == int(row.user_id),
            HermesBrowserBridge.status != "retired",
        )
        .all()
    )
    for candidate in rows:
        if not is_doubao_lab_slot(candidate):
            continue
        if physical_agent_device_id(candidate) != device_id:
            continue
        candidate_meta = dict(candidate.meta_json or {})
        candidate_meta["doubao_device_circuit_until"] = until.isoformat()
        candidate_meta["doubao_device_last_error"] = str(error_code)[:64]
        candidate_meta["doubao_device_last_error_at"] = now.isoformat()
        candidate.meta_json = candidate_meta
        db.add(candidate)


def due_auth_probe_accounts(db: Session, *, limit: int = 10) -> list[int]:
    now = utcnow_naive()
    result: list[int] = []
    rows = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.status != "retired")
        .order_by(HermesBrowserBridge.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    for row in rows:
        if len(result) >= max(1, min(int(limit), 100)) or not is_doubao_lab_slot(row):
            continue
        meta = dict(row.meta_json or {})
        _clear_expired_lease(meta, now=now)
        if (
            not account_has_saved_session(row)
            or meta.get("doubao_pool_lease_task_id")
            or not auth_probe_eligible(meta)
        ):
            row.meta_json = meta
            db.add(row)
            continue
        due = _parse(meta.get("doubao_next_auth_probe_at"))
        if due is not None and due > now:
            continue
        meta["doubao_auth_probe_claimed_at"] = now.isoformat()
        meta["doubao_next_auth_probe_at"] = (now + timedelta(minutes=15)).isoformat()
        row.meta_json = meta
        db.add(row)
        result.append(int(row.id))
    db.flush()
    return result


def due_capability_probe_accounts(
    db: Session,
    *,
    limit: int = 2,
    retry_after_seconds: int = 15 * 60,
) -> list[dict[str, Any]]:
    """Claim idle, authenticated accounts whose Seedance ability is unknown.

    A login probe cannot prove that the video composer is usable.  This
    scheduler-owned claim uses the same account/profile lease as production so
    maintenance can never open a Profile that is currently generating a
    customer video.  The proxy/direct network lane fence also prevents a
    maintenance Profile from racing a production Profile on the same lane.
    """

    now = utcnow_naive()
    bounded_limit = max(1, min(int(limit), 20))
    retry_delay = max(60, min(int(retry_after_seconds), 24 * 60 * 60))
    result: list[dict[str, Any]] = []
    rows = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.status != "retired")
        .order_by(HermesBrowserBridge.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    active_browser_lanes: set[tuple[str, int]] = set()
    for row in rows:
        if not is_doubao_lab_slot(row):
            continue
        meta = dict(row.meta_json or {})
        if _clear_expired_lease(meta, now=now):
            row.meta_json = meta
            db.add(row)
        if _browser_lane_is_busy(meta, now=now):
            active_browser_lanes.add(_network_lane(meta))

    claimed_lanes: set[tuple[str, int]] = set()
    for row in rows:
        if len(result) >= bounded_limit or not is_doubao_lab_slot(row):
            continue
        meta = dict(row.meta_json or {})
        lane = _network_lane(meta)
        if lane in active_browser_lanes or lane in claimed_lanes:
            continue
        if (
            row.user_id is None
            or not account_has_valid_session(row)
            or not device_is_available(row, now=now)
            or not authentication_is_fresh(meta, now=now)
            or meta.get("doubao_pool_lease_task_id")
            or seedance_capability_ready(meta)
        ):
            continue
        due = _parse(meta.get("doubao_next_capability_probe_at"))
        if due is not None and due > now:
            continue
        probe_id = "dp_" + secrets.token_hex(16)
        lease_id = f"probe:{probe_id}"
        previous_state = seedance_capability_state(meta)
        meta.update(
            {
                "doubao_seedance_probe_id": probe_id,
                "doubao_seedance_capability_previous_state": previous_state,
                "doubao_seedance_capability_state": "probing",
                "doubao_seedance_capability_checked_at": None,
                "doubao_seedance_capability_error": None,
                "doubao_seedance_capability_message": (
                    "系统正在自动复检该账号的 Seedance 视频能力。"
                ),
                "doubao_next_capability_probe_at": (
                    now + timedelta(seconds=retry_delay)
                ).isoformat(),
                "doubao_pool_lease_task_id": lease_id,
                "doubao_pool_lease_expires_at": (
                    now + timedelta(minutes=5)
                ).isoformat(),
                "doubao_provider_browser_task_id": lease_id,
            }
        )
        row.meta_json = meta
        db.add(row)
        claimed_lanes.add(lane)
        result.append(
            {
                "account_id": int(row.id),
                "workspace_id": int(row.workspace_id),
                "user_id": int(row.user_id),
                "bridge_id": str(row.bridge_id),
                "probe_id": probe_id,
            }
        )
    db.flush()
    return result


def fail_capability_probe_dispatch(
    db: Session,
    *,
    account_id: int,
    probe_id: str,
) -> None:
    """Release only the scheduler claim whose Celery dispatch failed."""

    row = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.id == int(account_id))
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        return
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_seedance_probe_id") or "") != str(probe_id):
        return
    lease_id = f"probe:{probe_id}"
    if str(meta.get("doubao_pool_lease_task_id") or "") == lease_id:
        meta["doubao_pool_lease_task_id"] = None
        meta["doubao_pool_lease_expires_at"] = None
    if str(meta.get("doubao_provider_browser_task_id") or "") == lease_id:
        meta["doubao_provider_browser_task_id"] = None
    meta["doubao_provider_browser_hold_until"] = None
    meta["doubao_provider_submission_accepted_at"] = None
    previous_state = str(
        meta.pop("doubao_seedance_capability_previous_state", "") or UNKNOWN
    )
    meta["doubao_seedance_capability_state"] = previous_state
    meta["doubao_seedance_capability_error"] = "probe_dispatch_failed"
    meta["doubao_seedance_capability_message"] = (
        "能力复检任务暂未投递，系统将自动重试。"
    )
    row.meta_json = meta
    db.add(row)


def set_pool_enabled(row: HermesBrowserBridge, *, enabled: bool) -> None:
    meta = dict(row.meta_json or {})
    meta["doubao_pool_enabled"] = bool(enabled)
    if enabled and str(meta.get("doubao_capture_state") or "") in {
        "captcha_required",
        "failed",
    }:
        # Re-enabling never fabricates a valid login. The operator must first
        # complete browser re-login and capture a fresh encrypted context.
        meta["doubao_pool_enabled"] = False
    row.meta_json = meta


def set_account_membership(row: HermesBrowserBridge, *, tier: str) -> None:
    if dict(row.meta_json or {}).get("doubao_pool_lease_task_id"):
        raise RuntimeError("该账号正在生成视频，不能在租约结束前修改账号等级。")
    row.meta_json = set_membership_tier(dict(row.meta_json or {}), tier)


def pool_supported_durations(db: Session) -> list[int]:
    """Return the safe current route contract for the self-hosted pool."""
    durations = set(FREE_DURATIONS)
    rows = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.status != "retired")
        .order_by(HermesBrowserBridge.id.asc())
        .all()
    )
    for row in rows:
        if not isinstance(row, HermesBrowserBridge) or not is_doubao_lab_slot(row):
            continue
        if account_is_ready(row):
            durations.update(allowed_durations(dict(row.meta_json or {})))
    return sorted(durations)


def pool_membership_summary(db: Session) -> dict[str, Any]:
    counts = {"free": 0, "enhanced": 0}
    ready_counts = {"free": 0, "enhanced": 0}
    rows = db.query(HermesBrowserBridge).filter(
        HermesBrowserBridge.status != "retired"
    ).all()
    for row in rows:
        if not isinstance(row, HermesBrowserBridge) or not is_doubao_lab_slot(row):
            continue
        membership = membership_payload(dict(row.meta_json or {}))
        tier = str(membership["tier"])
        counts[tier] += 1
        if account_is_ready(row):
            ready_counts[tier] += 1
    return {
        "accounts": counts,
        "ready_accounts": ready_counts,
        "allowed_durations_seconds": pool_supported_durations(db),
    }


__all__ = [
    "QUOTA_COOLDOWN_HOURS",
    "account_has_valid_session",
    "account_has_saved_session",
    "agent_is_online",
    "account_is_ready",
    "account_is_retry_candidate",
    "account_request_payload",
    "claim_account",
    "account_dispatch_score",
    "auth_probe_eligible",
    "due_auth_probe_accounts",
    "due_capability_probe_accounts",
    "fail_capability_probe_dispatch",
    "leased_account",
    "pool_membership_summary",
    "pool_supported_durations",
    "release_account",
    "record_submit_observation",
    "set_account_membership",
    "set_pool_enabled",
]
