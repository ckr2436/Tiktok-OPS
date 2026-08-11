from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.hermes_agent import HermesBrowserBridge
from app.services.flow_proxy_pool import resolve_flow_proxy_url
from app.services.ai_video.accounts import decrypt_api_key
from app.services.jimeng_lab import decrypt_jimeng_session_context
from app.services.ai_video.queues import AI_VIDEO_MAINTENANCE_TASK_QUEUE


logger = get_task_logger(__name__)


_ACTIVE_UPSTREAM_STATES = {"running", "waiting_upstream"}


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
    if row is None or not bool(dict(row.meta_json or {}).get("jimeng_lab_slot")):
        raise ValueError("JiMeng lab browser profile not found")
    return row


def _mark(
    db,
    row: HermesBrowserBridge,
    *,
    test_id: str,
    state: str,
    message: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> bool:
    meta = dict(row.meta_json or {})
    if str(meta.get("jimeng_test_id") or "") != str(test_id):
        return False
    meta["jimeng_test_state"] = state
    meta["jimeng_test_message"] = str(message)[:500]
    meta["jimeng_test_error"] = str(error or "")[:1000] or None
    if result is not None:
        meta["jimeng_test_result"] = result
    if state in {"complete", "failed", "upstream_timeout"}:
        meta["jimeng_test_completed_at"] = _now_iso()
    row.meta_json = meta
    db.add(row)
    db.commit()
    return True


def _reverse_auth_and_context(
    db, row: HermesBrowserBridge
) -> tuple[str, dict[str, Any]]:
    meta = dict(row.meta_json or {})
    ciphertext = str(meta.get("jimeng_session_ciphertext") or "")
    if not ciphertext:
        raise ValueError("credential_missing")
    token = decrypt_api_key(ciphertext)
    proxy_url = resolve_flow_proxy_url(
        db, int(meta.get("jimeng_proxy_id") or 0), require_active=True
    )
    session_context = decrypt_jimeng_session_context(meta, proxy_url)
    if session_context is None:
        raise ValueError("browser_session_context_missing")
    auth_token = f"{proxy_url}@{token}" if proxy_url else token
    return auth_token, session_context


def _payload_error(payload: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(payload, dict) or int(payload.get("code") or 0) == 0:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(payload.get("message") or "JiMeng request failed")[:800], data


def _reschedule_status_poll(
    *, workspace_id: int, user_id: int, bridge_id: str, test_id: str
) -> None:
    poll_jimeng_lab_test.apply_async(
        kwargs={
            "workspace_id": int(workspace_id),
            "user_id": int(user_id),
            "bridge_id": str(bridge_id),
            "test_id": str(test_id),
        },
        queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        countdown=max(15, int(settings.JIMENG_LAB_STATUS_RETRY_SECONDS)),
    )


@celery_app.task(
    name="jimeng_lab.generate_test",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=18 * 60,
    time_limit=19 * 60,
)
def generate_jimeng_lab_test(
    *, workspace_id: int, user_id: int, bridge_id: str, test_id: str
) -> dict[str, Any]:
    with SessionLocal() as db:
        row = _load_owned_row(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
        )
        meta = dict(row.meta_json or {})
        if str(meta.get("jimeng_test_id") or "") != str(test_id):
            return {"status": "stale", "test_id": test_id}
        try:
            auth_token, session_context = _reverse_auth_and_context(db, row)
        except ValueError as exc:
            reason = str(exc)
            _mark(
                db,
                row,
                test_id=test_id,
                state="failed",
                message="即梦登录上下文缺失，请重新登录。",
                error=reason,
            )
            return {"status": "failed", "test_id": test_id}
        prompt = str(meta.get("jimeng_test_prompt") or "").strip()
        model = str(meta.get("jimeng_test_model") or "jimeng-video-seedance-2.0-fast")
        _mark(
            db,
            row,
            test_id=test_id,
            state="running",
            message="即梦正在生成 4 秒 Seedance 测试视频。",
        )

    try:
        with httpx.Client(
            timeout=float(settings.JIMENG_LAB_REQUEST_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            response = client.post(
                f"{str(settings.JIMENG_LAB_API_URL).rstrip('/')}/v1/videos/generations",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={
                    "model": model,
                    "prompt": prompt,
                    "ratio": "9:16",
                    "resolution": "720p",
                    "duration": 4,
                    "response_format": "url",
                    "poll_timeout_seconds": int(
                        settings.JIMENG_LAB_INITIAL_POLL_TIMEOUT_SECONDS
                    ),
                    "session_context": session_context,
                },
            )
        response.raise_for_status()
        payload = response.json()
        upstream_error = _payload_error(payload)
        if upstream_error is not None:
            upstream_message, upstream_data = upstream_error
            history_id = str(upstream_data.get("history_id") or "").strip()
            if bool(upstream_data.get("upstream_pending")) and history_id.isdigit():
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
                        state="waiting_upstream",
                        message="即梦已接受任务，正在后台继续查询；系统不会重复提交。",
                        result={"upstream_history_id": history_id},
                    )
                _reschedule_status_poll(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    bridge_id=bridge_id,
                    test_id=test_id,
                )
                return {
                    "status": "waiting_upstream",
                    "test_id": test_id,
                    "upstream_history_id": history_id,
                }
            raise ValueError(upstream_message)
        items = payload.get("data") if isinstance(payload, dict) else None
        video_url = (
            str(items[0].get("url") or "").strip()
            if isinstance(items, list) and items and isinstance(items[0], dict)
            else ""
        )
        if not video_url.startswith("https://"):
            raise ValueError("JiMeng did not return an HTTPS video URL")
    except Exception as exc:  # noqa: BLE001 - persisted, bounded worker failure
        logger.warning(
            "JiMeng lab test failed workspace=%s bridge=%s test=%s error=%s",
            workspace_id,
            bridge_id,
            test_id,
            str(exc)[:300],
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
                state="failed",
                message="4 秒即梦测试视频生成失败。",
                error=str(exc),
            )
        return {"status": "failed", "test_id": test_id}

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
            state="complete",
            message="4 秒 Seedance 测试视频已生成。",
            result={"video_url": video_url},
        )
    return {"status": "complete", "test_id": test_id}


@celery_app.task(
    name="jimeng_lab.poll_test",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    soft_time_limit=55,
    time_limit=60,
)
def poll_jimeng_lab_test(
    *, workspace_id: int, user_id: int, bridge_id: str, test_id: str
) -> dict[str, Any]:
    with SessionLocal() as db:
        row = _load_owned_row(
            db,
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            bridge_id=bridge_id,
        )
        meta = dict(row.meta_json or {})
        if str(meta.get("jimeng_test_id") or "") != str(test_id):
            return {"status": "stale", "test_id": test_id}
        if str(meta.get("jimeng_test_state") or "") not in _ACTIVE_UPSTREAM_STATES:
            return {"status": "inactive", "test_id": test_id}
        result = dict(meta.get("jimeng_test_result") or {})
        history_id = str(result.get("upstream_history_id") or "")
        if not history_id.isdigit():
            _mark(
                db,
                row,
                test_id=test_id,
                state="failed",
                message="即梦任务恢复信息缺失。",
                error="upstream_history_id_missing",
            )
            return {"status": "failed", "test_id": test_id}
        started_raw = str(meta.get("jimeng_test_started_at") or "")
        try:
            started_at = datetime.fromisoformat(started_raw)
        except ValueError:
            started_at = datetime.now()
        elapsed = max(0, int((datetime.now() - started_at).total_seconds()))
        if elapsed >= int(settings.JIMENG_LAB_STATUS_MAX_AGE_SECONDS):
            _mark(
                db,
                row,
                test_id=test_id,
                state="upstream_timeout",
                message="即梦长时间未返回结果，已停止轮询；为避免重复扣费，没有再次提交。",
                error=f"upstream_timeout history_id={history_id}",
                result=result,
            )
            return {"status": "upstream_timeout", "test_id": test_id}
        auth_token, session_context = _reverse_auth_and_context(db, row)

    try:
        with httpx.Client(timeout=50.0, follow_redirects=False) as client:
            response = client.post(
                f"{str(settings.JIMENG_LAB_API_URL).rstrip('/')}/v1/videos/status",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={
                    "history_id": history_id,
                    "session_context": session_context,
                },
            )
        response.raise_for_status()
        payload = response.json()
        upstream_error = _payload_error(payload)
        if upstream_error is not None:
            raise ValueError(upstream_error[0])
        # The upstream project returns successful route values directly, while
        # failures use its {code, message, data} envelope.
        status_data = None
        if isinstance(payload, dict):
            status_data = (
                payload.get("data")
                if isinstance(payload.get("data"), dict)
                else payload
            )
        if not isinstance(status_data, dict):
            raise ValueError("JiMeng status response was invalid")
    except Exception as exc:  # transient one-shot status failure
        logger.warning(
            "JiMeng status poll failed workspace=%s bridge=%s test=%s error=%s",
            workspace_id,
            bridge_id,
            test_id,
            str(exc)[:300],
        )
        _reschedule_status_poll(
            workspace_id=workspace_id,
            user_id=user_id,
            bridge_id=bridge_id,
            test_id=test_id,
        )
        return {"status": "retrying", "test_id": test_id}

    if bool(status_data.get("complete")) and str(
        status_data.get("videoUrl") or ""
    ).startswith("https://"):
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
                state="complete",
                message="4 秒 Seedance 测试视频已生成。",
                result={
                    "upstream_history_id": history_id,
                    "video_url": str(status_data["videoUrl"]),
                },
            )
        return {"status": "complete", "test_id": test_id}

    if bool(status_data.get("failed")):
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
                message="即梦已确认该生成任务失败。",
                error=f"upstream_failed code={status_data.get('failCode')}",
                result={"upstream_history_id": history_id},
            )
        return {"status": "failed", "test_id": test_id}

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
            state="waiting_upstream",
            message="即梦已接受任务，正在后台继续查询；系统不会重复提交。",
            result={"upstream_history_id": history_id},
        )
    _reschedule_status_poll(
        workspace_id=workspace_id,
        user_id=user_id,
        bridge_id=bridge_id,
        test_id=test_id,
    )
    return {"status": "waiting_upstream", "test_id": test_id}


__all__ = ["generate_jimeng_lab_test", "poll_jimeng_lab_test"]
