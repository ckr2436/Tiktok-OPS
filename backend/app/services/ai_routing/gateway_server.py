from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.ai_routing import AiModelRoute
from app.services.ai_routing.router import AiGatewayError, call_chat_with_failover


app = FastAPI(title="GMV AI Gateway", docs_url=None, redoc_url=None)


def _positive_env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return float(default)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _long_request_retry_overrides(model_id: str) -> dict[str, Any]:
    """Return an opt-in retry envelope for large structured responses.

    Most gateway calls should retain the short global retry budget.  A small
    configured set of logical models may need substantially more generation
    time, though.  Keeping the model list and all limits in service-owned
    environment values avoids hard-coding a content-factory role into the
    generic router while ensuring one slow response is not mistaken for a
    provider outage at the global 90-second boundary.
    """

    configured_models = {
        item.strip()
        for item in os.environ.get(
            "AI_ROUTING_LONG_REQUEST_MODEL_IDS",
            "",
        ).split(",")
        if item.strip()
    }
    if str(model_id or "").strip() not in configured_models:
        return {}
    total_budget = _positive_env_float(
        "AI_ROUTING_LONG_REQUEST_TOTAL_BUDGET_SECONDS",
        540.0,
    )
    return {
        "timeout_seconds": _positive_env_float(
            "AI_ROUTING_LONG_REQUEST_TIMEOUT_SECONDS",
            total_budget,
        ),
        "max_attempts": _positive_env_int(
            "AI_ROUTING_LONG_REQUEST_MAX_ATTEMPTS",
            6,
        ),
        "total_budget_seconds": total_budget,
        "attempt_timeout_seconds": _positive_env_float(
            "AI_ROUTING_LONG_REQUEST_ATTEMPT_TIMEOUT_SECONDS",
            180.0,
        ),
    }


def _session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _authorize(authorization: str | None = Header(default=None)) -> None:
    configured = os.environ.get("GMV_AI_GATEWAY_KEY", "").strip()
    supplied = str(authorization or "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    if not configured or not supplied or not hmac.compare_digest(configured, supplied):
        raise HTTPException(status_code=401, detail="Invalid gateway token")


class ChatCompletionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., min_length=1, max_length=191)
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(None, ge=1, le=200000)
    max_completion_tokens: int | None = Field(None, ge=1, le=200000)
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    reasoning_effort: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None


def _message_has_media(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in {"image_url", "input_image", "image", "video_url"}:
                return True
    return False


def _resolve_route_scope(
    db: Session,
    *,
    model_id: str,
    requested_workload: str,
    messages: list[dict[str, Any]],
) -> tuple[str, str]:
    """Resolve role workload/capability from materialized routes, not names."""

    rows = (
        db.query(AiModelRoute.workload, AiModelRoute.capability)
        .filter(
            AiModelRoute.logical_model_id == model_id,
            AiModelRoute.is_enabled.is_(True),
            AiModelRoute.is_verified.is_(True),
            AiModelRoute.adapter_type == "openai_chat_completions",
        )
        .distinct()
        .all()
    )
    scopes = {(str(workload), str(capability)) for workload, capability in rows}
    requested = str(requested_workload or "default").strip().lower()[:64] or "default"
    workloads = {workload for workload, _capability in scopes}
    if requested == "default" and "default" not in workloads and len(workloads) == 1:
        requested = next(iter(workloads))
    available_capabilities = {
        capability for workload, capability in scopes if workload in {requested, "default"}
    }
    preferred = "multimodal" if _message_has_media(messages) else "text"
    if preferred in available_capabilities:
        capability = preferred
    elif len(available_capabilities) == 1:
        capability = next(iter(available_capabilities))
    else:
        capability = preferred
    return requested, capability


_WORKLOAD_RETRY_PROFILES: dict[str, dict[str, Any]] = {
    "ads_realtime": {
        "timeout_seconds": 75.0,
        "max_routes": 6,
        "max_attempts": 4,
        "total_budget_seconds": 70.0,
        "attempt_timeout_seconds": 35.0,
    },
    "ads_review": {
        "timeout_seconds": 570.0,
        "max_routes": 8,
        "max_attempts": 6,
        "total_budget_seconds": 540.0,
        "attempt_timeout_seconds": 180.0,
    },
    "general": {
        "timeout_seconds": 570.0,
        "max_routes": 8,
        "max_attempts": 6,
        "total_budget_seconds": 540.0,
        "attempt_timeout_seconds": 180.0,
    },
    "video_analyst": {
        "timeout_seconds": 180.0,
        "max_routes": 6,
        "max_attempts": 4,
        "total_budget_seconds": 175.0,
        "attempt_timeout_seconds": 90.0,
    },
}


@app.get("/health")
def health(db: Session = Depends(_session)) -> dict[str, Any]:
    db.execute(text("SELECT 1"))
    enabled = db.query(AiModelRoute).filter(AiModelRoute.is_enabled.is_(True)).count()
    return {"ok": True, "enabled_routes": int(enabled)}


@app.get("/v1/models", dependencies=[Depends(_authorize)])
def models(db: Session = Depends(_session)) -> dict[str, Any]:
    model_ids = [
        value[0]
        for value in db.query(AiModelRoute.logical_model_id)
        .filter(
            AiModelRoute.is_enabled.is_(True),
            AiModelRoute.is_verified.is_(True),
            AiModelRoute.adapter_type == "openai_chat_completions",
        )
        .distinct()
        .order_by(AiModelRoute.logical_model_id.asc())
        .all()
    ]
    return {"object": "list", "data": [{"id": value, "object": "model", "owned_by": "gmv"} for value in model_ids]}


@app.post("/v1/chat/completions", dependencies=[Depends(_authorize)])
async def chat_completions(
    payload: ChatCompletionIn,
    request: Request,
    db: Session = Depends(_session),
) -> Any:
    workload, capability = _resolve_route_scope(
        db,
        model_id=payload.model,
        requested_workload=str(request.headers.get("x-gmv-workload") or "default"),
        messages=payload.messages,
    )
    request_id = str(request.headers.get("idempotency-key") or uuid.uuid4())[:96]
    overrides = payload.model_dump(
        exclude={"model", "messages", "stream", "stream_options"},
        exclude_none=True,
    )
    retry_profile = {
        **_WORKLOAD_RETRY_PROFILES.get(workload, {}),
        **_long_request_retry_overrides(payload.model),
    }
    try:
        result = await call_chat_with_failover(
            db,
            logical_model_id=payload.model,
            messages=payload.messages,
            capability=capability,
            workload=workload,
            request_id=request_id,
            payload_overrides=overrides,
            metadata={"source": "ai_gateway", "workload": workload},
            **retry_profile,
        )
    except AiGatewayError as exc:
        raise HTTPException(
            status_code=int(exc.status_code or 502),
            detail={"message": str(exc), "type": exc.error_class},
        ) from exc
    if not payload.stream:
        return result

    # Some OpenAI-compatible clients always request SSE. Provider calls remain
    # non-streaming so failover can finish before headers are committed; the
    # successful completion is then emitted as a standards-compatible stream.
    choice = (result.get("choices") or [{}])[0]
    message = dict(choice.get("message") or {})
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    completion_id = str(result.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
    created = int(result.get("created") or time.time())

    def event_stream():
        initial = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        }
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        if content:
            body = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.model,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
        if tool_calls:
            tool_body = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": index, **dict(tool_call)}
                            for index, tool_call in enumerate(tool_calls)
                            if isinstance(tool_call, dict)
                        ]
                    },
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(tool_body, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        if bool((payload.stream_options or {}).get("include_usage")):
            usage_body = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.model,
                "choices": [],
                "usage": dict(result.get("usage") or {}),
            }
            yield f"data: {json.dumps(usage_body, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
