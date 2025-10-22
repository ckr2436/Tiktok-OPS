from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request, Response

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_member
from app.core.errors import APIError, RateLimitExceeded
from app.features.platform.router_tasks import (
    TriggerRequest,
    TriggerResponse,
    _ACTIONS,
    _TASK_TRIGGER_COUNTER,
    _TASK_TRIGGER_DURATION,
    _check_concurrency,
    _enqueue_task,
    _idempotent_get_or_set,
    _rate_limit,
)

try:  # pragma: no cover - optional audit logging
    from app.services.audit import log_event  # type: ignore
except Exception:  # pragma: no cover
    log_event = None  # type: ignore

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants/{{workspace_id}}/actions",
    tags=["Tenant / Actions"],
)


@router.post("/{action}", response_model=TriggerResponse)
def trigger_tenant_action(
    workspace_id: int,
    action: str,
    req: TriggerRequest,
    http: Request,
    response: Response,
    me: SessionUser = Depends(require_tenant_member),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if workspace_id != int(me.workspace_id):
        raise APIError("FORBIDDEN", "Not allowed to operate on this workspace.", 403)

    spec = _ACTIONS.get(action)
    if not spec or spec.domain != "tenant":
        raise APIError("INVALID_ARGUMENT", f"Unknown tenant action: {action}", 404)

    start = time.perf_counter()
    status_label = "error"

    try:
        limit, remaining, reset_ts = _rate_limit(spec, workspace_id, me.id)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        _check_concurrency(spec, workspace_id)

        if not idempotency_key:
            raise APIError("INVALID_ARGUMENT", "Missing Idempotency-Key header.", 400)

        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "args": req.args or {},
                    "priority": req.priority or "normal",
                    "delay_seconds": int(req.delay_seconds or 0),
                    "dedupe_key": req.dedupe_key or None,
                    "action": spec.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        request_id = getattr(http.state, "request_id", None)

        def _produce() -> Dict[str, Any]:
            task_id, meta = _enqueue_task(
                spec=spec,
                workspace_id=workspace_id,
                args=req.args or {},
                priority=req.priority or "normal",
                delay_seconds=int(req.delay_seconds or 0),
                request_id=request_id,
            )

            if log_event:
                try:
                    args_digest = hashlib.sha1(
                        json.dumps(req.args or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    log_event(  # type: ignore[misc]
                        db=None,
                        action="tenant.action.trigger",
                        resource_type="task",
                        resource_id=None,
                        actor_user_id=int(me.id),
                        actor_workspace_id=int(workspace_id),
                        actor_ip=http.client.host if http.client else None,
                        user_agent=http.headers.get("user-agent"),
                        details={
                            "action": spec.name,
                            "task_id": task_id,
                            "args_sha1": args_digest,
                            "idempotency_key_sha1": hashlib.sha1((idempotency_key or "").encode()).hexdigest(),
                            "status": "ENQUEUED",
                            "affected_workspaces": [int(workspace_id)],
                        },
                    )
                except Exception:
                    pass

            return {
                "task_id": task_id,
                "action": spec.name,
                "workspace_id": int(workspace_id),
                "state": "ENQUEUED",
                "enqueued_at": meta["enqueued_at"],
                "status_url": f"{settings.API_PREFIX}/platform/tasks/{task_id}",
            }

        payload = _idempotent_get_or_set(
            action=spec.name,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            response_payload_factory=_produce,
        )

        payload["_rate"] = {
            "limit": limit,
            "remaining": remaining,
            "reset": reset_ts,
        }

        status_label = "idempotent" if payload.get("_idempotent_hit") else "accepted"
        return TriggerResponse(**{k: v for k, v in payload.items() if k in TriggerResponse.model_fields})
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _TASK_TRIGGER_COUNTER.labels(task_key=spec.name, status=status_label).inc()
        _TASK_TRIGGER_DURATION.labels(task_key=spec.name).observe(duration)
