from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.ai_routing import AiModelRoute
from app.services.ai_routing.router import AiGatewayError, call_chat_with_failover


app = FastAPI(title="GMV AI Gateway", docs_url=None, redoc_url=None)


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
    model: str = Field(..., min_length=1, max_length=191)
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(None, ge=1, le=200000)
    temperature: float | None = None
    reasoning_effort: str | None = None


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
    workload = str(request.headers.get("x-gmv-workload") or "default").strip().lower()
    if payload.model.startswith("video-analyst-"):
        workload = "video_analyst"
    capability = "multimodal" if workload == "video_analyst" else "text"
    request_id = str(request.headers.get("idempotency-key") or uuid.uuid4())[:96]
    overrides = payload.model_dump(exclude={"model", "messages", "stream"}, exclude_none=True)
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
        body = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        }
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
