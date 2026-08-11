from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute, AiRouteAttempt
from app.data.models.kie_api import KieTask
from app.services.ai_routing.router import AiGatewayError, _attempt, _mark_failure, _mark_success
from app.services.ai_video.accounts import normalize_video_model_id


def begin_video_route_attempt(db: Session, task: KieTask) -> tuple[int | None, float]:
    route = (
        db.query(AiModelRoute)
        .filter(
            AiModelRoute.key_id == int(task.key_id),
            AiModelRoute.logical_model_id == normalize_video_model_id(task.model),
            AiModelRoute.capability == "video",
        )
        .order_by(AiModelRoute.priority.asc(), AiModelRoute.id.asc())
        .first()
    )
    started = time.monotonic()
    if route is None:
        return None, started
    local = dict(dict(task.result_json or {}).get("__local") or {})
    row = _attempt(
        db,
        route=route,
        request_id=f"ai-video-{int(task.id)}-{int(task.key_id)}",
        switched_from_route_id=None,
        metadata={
            "source": "ai_video_worker",
            "workload": "default",
            "retry_attempt": int(local.get("auto_retry_count") or 0),
            "retry_round": int(local.get("provider_failover_count") or 0),
        },
    )
    db.commit()
    return int(row.id), started


def finish_video_route_attempt(
    db: Session,
    attempt_id: int | None,
    started: float,
    *,
    error: Exception | None,
) -> None:
    if attempt_id is None:
        return
    attempt = db.get(AiRouteAttempt, int(attempt_id))
    if attempt is None or attempt.status != "STARTED":
        return
    route = db.get(AiModelRoute, int(attempt.route_id))
    if route is None:
        return
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    if error is None:
        _mark_success(route, attempt, latency_ms=latency_ms, usage=None)
    else:
        status = getattr(error, "status_code", None)
        code = str(getattr(error, "code", "") or "").strip().lower()
        message = str(error or "").lower()
        if code in {"doubao_prompt_contract_invalid", "doubao_prompt_too_long"}:
            error_class = "REQUEST_INVALID"
        elif status in {401, 403} or "auth" in message:
            error_class = "AUTH"
        elif status == 429 or "rate limit" in message:
            error_class = "RATE_LIMIT"
        elif status in {500, 502, 503, 504, 529} or "temporarily unavailable" in message:
            error_class = "UPSTREAM_5XX"
        elif "timeout" in message or "transport" in message or "connection" in message:
            error_class = "NETWORK"
        elif "prompt" in message and "policy" in message:
            error_class = "POLICY"
        else:
            error_class = "UPSTREAM"
        wrapped = AiGatewayError(
            "Video provider request failed",
            error_class=error_class,
            status_code=status,
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
        )
        _mark_failure(route, attempt, wrapped, latency_ms=latency_ms)
    db.add_all((route, attempt))
    db.commit()


__all__ = ["begin_video_route_attempt", "finish_video_route_attempt"]
