from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any

from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.doubao_lab import decrypt_doubao_session_context
from app.services.doubao_lab import apply_doubao_manual_video_challenge_result
from app.services.flow_proxy_pool import resolve_flow_proxy_url
from app.services.doubao_provider.client import DoubaoProviderError, invoke_doubao_helper
from app.services.doubao_provider.pool import (
    account_request_payload,
    auth_probe_eligible,
    due_auth_probe_accounts,
    due_capability_probe_accounts,
    fail_capability_probe_dispatch,
)
from app.services.doubao_provider.capability import (
    apply_seedance_capability_result,
    seedance_capability_ready,
)
from app.services.doubao_provider.health import (
    AUTHENTICATED,
    AUTH_REQUIRED,
    AUTH_UNKNOWN,
    NETWORK_REACHABLE,
    NETWORK_REGION_RESTRICTED,
    NETWORK_UNREACHABLE,
    authentication_is_fresh,
    mark_auth_probe_result,
    mark_authenticated,
)
from app.services.ai_video.queues import AI_VIDEO_MAINTENANCE_TASK_QUEUE


logger = get_task_logger(__name__)
_HELPER_PYTHON = "/opt/apps/doubao2api-lab/.venv/bin/python"
_HELPER_SCRIPT = "/opt/apps/doubao2api-lab/scripts/context_generate.py"
_MANUAL_VIDEO_CHALLENGE = (
    "一束柔和晨光照进安静房间，镜头缓慢前移，无人物、无文字、无品牌。"
)


def _apply_auth_probe_observation(
    meta: dict[str, Any],
    *,
    auth_state: str,
    network_state: str,
    error_code: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Keep a still-fresh login through one inconclusive network probe."""
    previous_auth_state = str(meta.get("doubao_auth_state") or "")
    previous_auth_checked_at = meta.get("doubao_auth_checked_at")
    preserve_fresh_auth = bool(
        auth_state == AUTH_UNKNOWN
        and network_state == NETWORK_UNREACHABLE
        and authentication_is_fresh(meta, now=now)
    )
    result = mark_auth_probe_result(
        meta,
        state=auth_state,
        network_state=network_state,
        error_code=error_code,
        now=now,
    )
    if preserve_fresh_auth:
        result["doubao_auth_state"] = previous_auth_state
        result["doubao_auth_checked_at"] = previous_auth_checked_at
    return result


def _manual_browser_generation_ready(meta: dict[str, Any], *, lease_id: str) -> bool:
    """Return true only after Bridge reports the exact interactive runtime."""
    return bool(
        str(meta.get("doubao_pool_lease_task_id") or "") == lease_id
        and str(meta.get("doubao_provider_browser_task_id") or "") == lease_id
        and str(meta.get("doubao_browser_capture_id") or "") == lease_id
        and str(meta.get("doubao_browser_status") or "").lower() == "ready"
        and "doubao.com" in str(meta.get("doubao_page_url") or "").lower()
    )


def _wait_for_manual_browser_generation(
    *, workspace_id: int, user_id: int, bridge_id: str, lease_id: str
) -> None:
    """Fence maintenance execution behind the Bridge runtime generation."""
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            row = _load_owned_row(
                db,
                workspace_id=int(workspace_id),
                user_id=int(user_id),
                bridge_id=bridge_id,
            )
            meta = dict(row.meta_json or {})
            if str(meta.get("doubao_pool_lease_task_id") or "") != lease_id:
                raise DoubaoProviderError(
                    "豆包人工验证租约已被取消。",
                    code="doubao_manual_challenge_cancelled",
                )
            if _manual_browser_generation_ready(meta, lease_id=lease_id):
                return
        time.sleep(0.5)
    raise DoubaoProviderError(
        "豆包人工验证浏览器尚未完成可见模式切换。",
        code="doubao_browser_unstable",
    )


def _invoke_capability_probe(payload: dict[str, Any]) -> dict[str, Any]:
    last_error: DoubaoProviderError | None = None
    for attempt in range(2):
        try:
            result = invoke_doubao_helper(payload, timeout_seconds=140)
            if str(result.get("status") or "") != "capable":
                raise DoubaoProviderError(
                    "豆包视频能力探测未返回可用状态。",
                    code="doubao_composer_unavailable",
                )
            return result
        except DoubaoProviderError as exc:
            last_error = exc
            if attempt == 0 and exc.code in {
                "doubao_failed",
                "doubao_timeout",
                "doubao_browser_unstable",
                "doubao_composer_unavailable",
            }:
                time.sleep(5)
                continue
            raise
    raise last_error or DoubaoProviderError(
        "豆包视频能力探测失败。", code="doubao_capability_probe_failed"
    )


def _now_iso() -> str:
    return datetime.now().astimezone().replace(tzinfo=None).isoformat()


def _load_owned_row(
    db, *, workspace_id: int, user_id: int, bridge_id: str
) -> HermesBrowserBridge:
    row = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.bridge_id == str(bridge_id),
            HermesBrowserBridge.status != "retired",
        )
        .one_or_none()
    )
    if row is None or not bool(dict(row.meta_json or {}).get("doubao_lab_slot")):
        raise ValueError("Doubao lab browser profile not found")
    return row


def _generation_request_payload(
    db, row: HermesBrowserBridge, *, meta: dict[str, Any]
) -> dict[str, Any]:
    """Build one lab request from the same account context as production.

    Seedance submission is performed in the account's managed browser Profile.
    Keeping the CDP endpoint in this shared builder prevents the platform test
    path from reporting a false browser-unavailable failure while production
    uses the exact same account successfully.
    """
    context = decrypt_doubao_session_context(meta)
    if context is None:
        raise RuntimeError("browser_session_context_missing")
    proxy_url = ""
    if str(meta.get("doubao_network_mode") or "proxy") != "direct":
        proxy_url = resolve_flow_proxy_url(
            db, int(meta.get("doubao_proxy_id") or 0), require_active=True
        )
    return {
        **context,
        "proxy_url": proxy_url,
        "prompt": str(meta.get("doubao_test_prompt") or "").strip(),
        "duration": int(meta.get("doubao_test_duration") or 4),
        "ratio": str(meta.get("doubao_test_ratio") or "9:16"),
        "browser_cdp_url": str(row.cdp_url or ""),
    }


def _mark(
    db,
    row: HermesBrowserBridge,
    *,
    test_id: str,
    state: str,
    message: str,
    error: str | None = None,
    error_code: str | None = None,
    result: dict[str, Any] | None = None,
) -> bool:
    meta = dict(row.meta_json or {})
    if str(meta.get("doubao_test_id") or "") != str(test_id):
        return False
    meta["doubao_test_state"] = state
    meta["doubao_test_message"] = str(message)[:500]
    meta["doubao_test_error"] = str(error or "")[:1000] or None
    if result is not None:
        meta["doubao_test_result"] = result
    if state in {"complete", "failed", "captcha_required"}:
        meta["doubao_test_completed_at"] = _now_iso()
        lease_id = f"lab:{test_id}"
        if str(meta.get("doubao_pool_lease_task_id") or "") == lease_id:
            meta["doubao_pool_lease_task_id"] = None
            meta["doubao_pool_lease_expires_at"] = None
        if str(meta.get("doubao_provider_browser_task_id") or "") == lease_id:
            meta["doubao_provider_browser_task_id"] = None
        if state == "complete":
            meta = apply_seedance_capability_result(meta, success=True)
            meta = mark_authenticated(meta)
            meta["doubao_seedance_capability_message"] = (
                "Seedance 2.0 Mini 已通过真实生成验证。"
            )
        elif error_code:
            meta = apply_seedance_capability_result(
                meta, success=False, error_code=error_code
            )
        if state == "captcha_required":
            # A CAPTCHA verdict belongs to the account, not only this lab
            # attempt. Remove it from production routing until an operator
            # reopens the same Profile and captures a verified session.
            meta["doubao_capture_state"] = "captcha_required"
            meta["doubao_pool_enabled"] = False
            meta["doubao_pool_last_error"] = "doubao_captcha_required"
            meta["doubao_pool_cooldown_until"] = None
    row.meta_json = meta
    db.add(row)
    db.commit()
    return True


@celery_app.task(
    name="doubao_lab.generate_test",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=12 * 60,
    time_limit=13 * 60,
)
def generate_doubao_lab_test(
    *, workspace_id: int, user_id: int, bridge_id: str, test_id: str
) -> dict[str, Any]:
    try:
        with SessionLocal() as db:
            row = _load_owned_row(
                db,
                workspace_id=int(workspace_id),
                user_id=int(user_id),
                bridge_id=bridge_id,
            )
            meta = dict(row.meta_json or {})
            if str(meta.get("doubao_test_id") or "") != str(test_id):
                return {"status": "stale", "test_id": test_id}
            request_payload = _generation_request_payload(db, row, meta=meta)
            _mark(
                db,
                row,
                test_id=test_id,
                state="running",
                message="豆包正在生成 Seedance 2.0 Mini 测试视频。",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        logger.warning(
            "Doubao lab context load failed workspace=%s bridge=%s test=%s error=%s",
            workspace_id,
            bridge_id,
            test_id,
            error,
        )
        try:
            with SessionLocal() as db:
                row = _load_owned_row(
                    db,
                    workspace_id=int(workspace_id),
                    user_id=int(user_id),
                    bridge_id=bridge_id,
                )
                _mark(
                    db,
                    row,
                    test_id=test_id,
                    state="failed",
                    message="豆包测试上下文加载失败，请检查登录态和固定代理。",
                    error=error,
                )
        except Exception:
            logger.exception(
                "Doubao lab could not persist context failure bridge=%s test=%s",
                bridge_id,
                test_id,
            )
        return {"status": "failed", "test_id": test_id}

    try:
        completed = subprocess.run(
            [_HELPER_PYTHON, _HELPER_SCRIPT],
            input=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=11 * 60,
            check=False,
            close_fds=True,
        )
        raw = completed.stdout.decode("utf-8", errors="replace").strip()
        payload = json.loads(raw) if raw else {}
        state = str(payload.get("status") or "failed")
        if state == "captcha_required":
            message = "豆包要求人工完成一次 CAPTCHA；请重新打开该账号的独立浏览器。"
            error = f"captcha_required code={int(payload.get('error_code') or 0)}"
            error_code = "doubao_captcha_required"
            result = None
        elif state == "complete" and str(payload.get("video_url") or "").startswith(
            "https://"
        ):
            message = "豆包 Seedance 2.0 Mini 测试视频已生成。"
            error = None
            error_code = None
            result = {
                "video_url": str(payload["video_url"]),
                "width": int(payload.get("width") or 0),
                "height": int(payload.get("height") or 0),
                "duration": float(payload.get("duration") or 0),
            }
        else:
            state = "failed"
            message = "豆包 Seedance 2.0 Mini 测试生成失败。"
            error = str(payload.get("error") or f"helper_exit_{completed.returncode}")[:800]
            error_code = str(payload.get("error_code") or "doubao_failed")[:64]
            result = None
    except Exception as exc:  # bounded worker failure, no credential logging
        state = "failed"
        message = "豆包 Seedance 2.0 Mini 测试生成失败。"
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        error_code = "doubao_lab_runtime_failed"
        result = None
    if state != "complete":
        logger.warning(
            "Doubao lab test failed workspace=%s bridge=%s test=%s state=%s error=%s",
            workspace_id,
            bridge_id,
            test_id,
            state,
            error,
        )
    with SessionLocal() as db:
        row = _load_owned_row(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
        )
        _mark(
            db,
            row,
            test_id=test_id,
            state=state,
            message=message,
            error=error,
            error_code=error_code,
            result=result,
        )
    return {"status": state, "test_id": test_id}


@celery_app.task(
    name="doubao_provider.manual_video_challenge",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=210,
    time_limit=240,
)
def run_doubao_manual_video_challenge(
    *, workspace_id: int, user_id: int, bridge_id: str, challenge_id: str
) -> dict[str, Any]:
    """Prepare the real Seedance composer for one operator-owned CAPTCHA."""
    lease_id = f"manual-capture:{challenge_id}"
    try:
        _wait_for_manual_browser_generation(
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
            lease_id=lease_id,
        )
        with SessionLocal() as db:
            row = _load_owned_row(
                db,
                workspace_id=int(workspace_id),
                user_id=int(user_id),
                bridge_id=bridge_id,
            )
            meta = dict(row.meta_json or {})
            if (
                str(meta.get("doubao_manual_verification_challenge_id") or "")
                != str(challenge_id)
                or str(meta.get("doubao_pool_lease_task_id") or "") != lease_id
            ):
                return {"status": "stale", "challenge_id": challenge_id}
            payload = {
                **account_request_payload(db, row),
                "action": "manual_video_challenge",
                "prompt": _MANUAL_VIDEO_CHALLENGE,
                "ratio": "9:16",
                "duration": 4,
                "browser_cdp_url": str(row.cdp_url or ""),
            }
            db.commit()
        result = invoke_doubao_helper(payload, timeout_seconds=190)
        status = str(result.get("status") or "failed").strip().lower()
        conversation_id = str(result.get("conversation_id") or "").strip() or None
        error_code = None
    except DoubaoProviderError as exc:
        error_code = exc.code
        status = {
            "doubao_captcha_required": "captcha_required",
            "doubao_region_restricted": "region_restricted",
            "doubao_auth_required": "auth_required",
            "doubao_account_context_invalid": "auth_required",
        }.get(error_code, "failed")
        conversation_id = None
    except Exception:
        logger.exception(
            "Doubao manual video challenge failed bridge=%s challenge=%s",
            bridge_id,
            challenge_id,
        )
        status = "failed"
        conversation_id = None
        error_code = "doubao_manual_challenge_failed"

    with SessionLocal() as db:
        row = _load_owned_row(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
        )
        changed = apply_doubao_manual_video_challenge_result(
            row,
            challenge_id=challenge_id,
            status=status,
            conversation_id=conversation_id,
            error_code=error_code,
        )
        if changed:
            db.add(row)
            db.commit()
    return {
        "status": status if changed else "stale",
        "challenge_id": challenge_id,
        "conversation_id": conversation_id,
        "error_code": error_code,
    }


@celery_app.task(
    name="doubao_provider.probe_account_capability",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=150,
    time_limit=180,
)
def probe_doubao_provider_account_capability(
    *, workspace_id: int, user_id: int, bridge_id: str, probe_id: str
) -> dict[str, Any]:
    lease_id = f"probe:{probe_id}"
    try:
        with SessionLocal() as db:
            row = _load_owned_row(
                db,
                workspace_id=int(workspace_id),
                user_id=int(user_id),
                bridge_id=bridge_id,
            )
            meta = dict(row.meta_json or {})
            if (
                str(meta.get("doubao_seedance_probe_id") or "") != str(probe_id)
                or str(meta.get("doubao_pool_lease_task_id") or "") != lease_id
            ):
                return {"status": "stale", "probe_id": probe_id}
            payload = {
                **account_request_payload(db, row),
                "action": "probe",
                "browser_cdp_url": str(row.cdp_url or ""),
            }
            db.commit()
        _invoke_capability_probe(payload)
        success = True
        error_code = None
    except DoubaoProviderError as exc:
        success = False
        error_code = exc.code
    except Exception:
        logger.exception(
            "Doubao capability probe failed bridge=%s probe=%s",
            bridge_id,
            probe_id,
        )
        success = False
        error_code = "doubao_capability_probe_failed"

    with SessionLocal() as db:
        row = _load_owned_row(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
        )
        meta = dict(row.meta_json or {})
        if str(meta.get("doubao_seedance_probe_id") or "") != str(probe_id):
            return {"status": "stale", "probe_id": probe_id}
        meta = apply_seedance_capability_result(
            meta, success=success, error_code=error_code
        )
        if success:
            meta = mark_authenticated(meta)
        if success:
            meta["doubao_seedance_capability_message"] = (
                "Seedance 2.0 Mini 视频编辑器可用。"
            )
        elif seedance_capability_ready(meta):
            meta["doubao_seedance_capability_message"] = (
                "最近一次能力复检未完成；继续沿用已确认的视频能力。"
            )
        else:
            meta["doubao_seedance_capability_message"] = (
                "尚未确认 Seedance 视频能力，请稍后重试检测。"
            )
        if str(meta.get("doubao_pool_lease_task_id") or "") == lease_id:
            meta["doubao_pool_lease_task_id"] = None
            meta["doubao_pool_lease_expires_at"] = None
        if str(meta.get("doubao_provider_browser_task_id") or "") == lease_id:
            meta["doubao_provider_browser_task_id"] = None
        if error_code in {
            "doubao_captcha_required",
            "doubao_auth_required",
            "doubao_account_context_invalid",
            "doubao_region_restricted",
        }:
            meta["doubao_pool_enabled"] = False
            if error_code == "doubao_captcha_required":
                meta["doubao_capture_state"] = "captcha_required"
            elif error_code == "doubao_region_restricted":
                meta["doubao_capture_state"] = "ready"
                meta["doubao_capture_error"] = error_code
                meta["doubao_pool_cooldown_until"] = None
            else:
                meta["doubao_capture_state"] = "failed"
        row.meta_json = meta
        db.add(row)
        db.commit()
    return {
        "status": "ready" if success else "failed",
        "probe_id": probe_id,
        "error_code": error_code,
    }


@celery_app.task(
    name="doubao_provider.dispatch_auth_probes",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
)
def dispatch_doubao_provider_auth_probes(*, limit: int = 10) -> dict[str, Any]:
    with SessionLocal() as db:
        account_ids = due_auth_probe_accounts(db, limit=limit)
        db.commit()
    for account_id in account_ids:
        probe_doubao_provider_account_auth.apply_async(
            kwargs={"account_id": int(account_id)},
            queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        )
    return {"queued": len(account_ids), "account_ids": account_ids}


@celery_app.task(
    name="doubao_provider.dispatch_capability_probes",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
)
def dispatch_doubao_provider_capability_probes(
    *, limit: int | None = None
) -> dict[str, Any]:
    from app.core.config import settings

    batch_limit = int(
        limit
        if limit is not None
        else settings.DOUBAO_CAPABILITY_PROBE_BATCH_SIZE
    )
    with SessionLocal() as db:
        claims = due_capability_probe_accounts(
            db,
            limit=batch_limit,
            retry_after_seconds=int(
                settings.DOUBAO_CAPABILITY_RECHECK_SECONDS
            ),
        )
        db.commit()
    queued: list[int] = []
    dispatch_failed: list[int] = []
    for claim in claims:
        try:
            probe_doubao_provider_account_capability.apply_async(
                kwargs={
                    "workspace_id": int(claim["workspace_id"]),
                    "user_id": int(claim["user_id"]),
                    "bridge_id": str(claim["bridge_id"]),
                    "probe_id": str(claim["probe_id"]),
                },
                queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
            )
            queued.append(int(claim["account_id"]))
        except Exception:
            logger.exception(
                "Failed to dispatch Doubao capability probe account=%s probe=%s",
                claim["account_id"],
                claim["probe_id"],
            )
            with SessionLocal() as db:
                fail_capability_probe_dispatch(
                    db,
                    account_id=int(claim["account_id"]),
                    probe_id=str(claim["probe_id"]),
                )
                db.commit()
            dispatch_failed.append(int(claim["account_id"]))
    return {
        "queued": len(queued),
        "account_ids": queued,
        "dispatch_failed": dispatch_failed,
    }


@celery_app.task(
    name="doubao_provider.probe_account_auth",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=90,
    time_limit=120,
)
def probe_doubao_provider_account_auth(*, account_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        row = (
            db.query(HermesBrowserBridge)
            .filter(HermesBrowserBridge.id == int(account_id))
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            return {"status": "missing", "account_id": int(account_id)}
        meta = dict(row.meta_json or {})
        if (
            not bool(meta.get("doubao_lab_slot"))
            or not auth_probe_eligible(meta)
            or meta.get("doubao_pool_lease_task_id")
        ):
            return {"status": "skipped", "account_id": int(account_id)}
        request_payload = {**account_request_payload(db, row), "action": "auth_probe"}
        db.commit()
    try:
        result = invoke_doubao_helper(request_payload, timeout_seconds=60)
        authenticated = str(result.get("status") or "").lower() == AUTHENTICATED
        auth_state = AUTHENTICATED if authenticated else AUTH_UNKNOWN
        network_state = NETWORK_REACHABLE
        error_code = None if authenticated else "doubao_auth_probe_inconclusive"
    except DoubaoProviderError as exc:
        error_code = exc.code
        if error_code in {"doubao_auth_required", "doubao_account_context_invalid"}:
            auth_state = AUTH_REQUIRED
            network_state = NETWORK_REACHABLE
        elif error_code == "doubao_region_restricted":
            auth_state = AUTH_UNKNOWN
            network_state = NETWORK_REGION_RESTRICTED
        else:
            auth_state = AUTH_UNKNOWN
            network_state = NETWORK_UNREACHABLE
    now = datetime.now()
    with SessionLocal() as db:
        row = (
            db.query(HermesBrowserBridge)
            .filter(HermesBrowserBridge.id == int(account_id))
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            return {"status": "missing", "account_id": int(account_id)}
        meta = dict(row.meta_json or {})
        was_auto_recovery = bool(
            not bool(meta.get("doubao_pool_enabled", True))
            and auth_probe_eligible(meta)
        )
        meta = _apply_auth_probe_observation(
            meta,
            auth_state=auth_state,
            network_state=network_state,
            error_code=error_code,
            now=now,
        )
        if auth_state == AUTHENTICATED:
            if str(meta.get("doubao_pool_last_error") or "") in {
                "doubao_auth_required",
                "doubao_account_context_invalid",
                "doubao_auth_probe_inconclusive",
            }:
                meta["doubao_pool_last_error"] = None
            meta["doubao_capture_state"] = "ready"
            if was_auto_recovery:
                meta["doubao_pool_enabled"] = True
                meta["doubao_pool_cooldown_until"] = None
                meta["doubao_pool_consecutive_errors"] = 0
                if int(meta.get("doubao_pool_success_count") or 0) > 0:
                    # A downloaded historical video proves Seedance ability;
                    # the current auth probe proves the saved login is still
                    # valid. Together they are sufficient to re-enter routing
                    # without a paid or interactive capability check.
                    meta = apply_seedance_capability_result(meta, success=True)
                else:
                    meta["doubao_seedance_capability_state"] = "unknown"
                    meta["doubao_seedance_capability_error"] = None
                    meta["doubao_next_capability_probe_at"] = None
        elif auth_state == AUTH_REQUIRED:
            meta["doubao_pool_last_error"] = "doubao_auth_required"
            meta["doubao_pool_enabled"] = False
            meta["doubao_capture_state"] = "failed"
            meta["doubao_pool_cooldown_until"] = None
        elif network_state == NETWORK_REGION_RESTRICTED:
            meta["doubao_pool_last_error"] = "doubao_region_restricted"
            meta["doubao_pool_enabled"] = False
            meta["doubao_capture_state"] = "ready"
            meta["doubao_pool_cooldown_until"] = None
        else:
            meta["doubao_pool_last_error"] = str(
                error_code or "doubao_auth_probe_inconclusive"
            )[:64]
        row.meta_json = meta
        db.add(row)
        db.commit()
    return {
        "status": auth_state,
        "account_id": int(account_id),
        "error_code": error_code,
    }


__all__ = [
    "dispatch_doubao_provider_capability_probes",
    "dispatch_doubao_provider_auth_probes",
    "generate_doubao_lab_test",
    "probe_doubao_provider_account_auth",
    "probe_doubao_provider_account_capability",
    "run_doubao_manual_video_challenge",
]
