from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.core.security import client_ip
from app.data.db import get_db
from app.services.audit import log_event
from app.services.flow_proxy_pool import list_flow_proxies, serialize_proxy
from app.services.jimeng_lab import (
    cancel_jimeng_lab_onboarding,
    fail_jimeng_lab_test_dispatch,
    get_jimeng_lab_session,
    list_jimeng_lab_sessions,
    queue_jimeng_lab_test,
    start_jimeng_lab_onboarding,
    verify_jimeng_lab_session,
)
from app.services.ai_video.queues import AI_VIDEO_MAINTENANCE_TASK_QUEUE


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/jimeng-lab",
    tags=["Platform / JiMeng Lab"],
    dependencies=[Depends(require_platform_admin)],
)


class JimengBrowserSessionRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    proxy_id: int = Field(ge=1)


class JimengTestRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    model: str = Field(
        default="jimeng-video-seedance-2.0-fast", min_length=3, max_length=128
    )


def _audit(
    db: Session,
    request: Request,
    me: SessionUser,
    *,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    log_event(
        db,
        action=action,
        resource_type="jimeng_lab",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details=dict(details or {}),
    )


async def _service_status() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(
                f"{str(settings.JIMENG_LAB_API_URL).rstrip('/')}/ping"
            )
        healthy = response.status_code == 200 and "pong" in response.text.lower()
    except httpx.HTTPError:
        healthy = False
    return {
        "status": "healthy" if healthy else "unavailable",
        "base_url": "loopback",
        "production_routing_enabled": False,
    }


@router.get("/overview")
async def jimeng_lab_overview(
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.hermes_agent.content_factory import browser_devices

    return {
        "service": await _service_status(),
        "capabilities": {
            "model": "Seedance 2.0 / 2.0 Fast",
            "test_duration_seconds": 4,
            "browser_profile_isolated": True,
            "credential_storage": "encrypted",
        },
        "devices": browser_devices(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
        "proxies": [serialize_proxy(row) for row in list_flow_proxies(db)],
        "sessions": list_jimeng_lab_sessions(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
    }


@router.post("/browser-sessions")
def start_jimeng_browser_session(
    payload: JimengBrowserSessionRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = start_jimeng_lab_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
        proxy_id=int(payload.proxy_id),
    )
    _audit(
        db,
        request,
        me,
        action="platform.jimeng_lab.browser_onboarding_start",
        details={
            "device_id": payload.device_id,
            "session_id": session.get("session_id"),
            "proxy_id": int(payload.proxy_id),
        },
    )
    db.commit()
    return session


@router.get("/browser-sessions/{capture_id}")
def get_jimeng_browser_session_status(
    capture_id: str,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return get_jimeng_lab_session(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )


@router.post("/browser-sessions/{capture_id}/cancel")
def cancel_jimeng_browser_session(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = cancel_jimeng_lab_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.jimeng_lab.browser_onboarding_cancel",
        details={"session_id": capture_id},
    )
    db.commit()
    return session


@router.post("/browser-sessions/{capture_id}/verify")
async def verify_jimeng_browser_session_route(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = await verify_jimeng_lab_session(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.jimeng_lab.session_verify",
        details={"session_id": capture_id, "state": session.get("state")},
    )
    db.commit()
    return session


@router.post("/browser-sessions/{capture_id}/tests")
def create_jimeng_test(
    capture_id: str,
    payload: JimengTestRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    if payload.model not in {
        "jimeng-video-seedance-2.0",
        "jimeng-video-seedance-2.0-fast",
    }:
        raise HTTPException(status_code=422, detail="仅允许 Seedance 2.0 实验模型")
    session, dispatch_required = queue_jimeng_lab_test(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
        prompt=payload.prompt,
        model=payload.model,
    )
    _audit(
        db,
        request,
        me,
        action="platform.jimeng_lab.test_queued",
        details={
            "session_id": capture_id,
            "test_id": session.get("test", {}).get("id"),
            "model": payload.model,
            "duration": 4,
        },
    )
    db.commit()
    if not dispatch_required:
        return session
    test_id = str(session.get("test", {}).get("id") or "")
    try:
        from app.tasks.jimeng_lab_tasks import generate_jimeng_lab_test

        generate_jimeng_lab_test.apply_async(
            kwargs={
                "workspace_id": int(me.workspace_id),
                "user_id": int(me.id),
                "bridge_id": str(session.get("bridge_id") or ""),
                "test_id": test_id,
            },
            queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001 - convert broker failure to durable state
        fail_jimeng_lab_test_dispatch(
            db,
            workspace_id=int(me.workspace_id),
            user_id=int(me.id),
            capture_id=capture_id,
            test_id=test_id,
            error=str(exc),
        )
        db.commit()
        raise HTTPException(
            status_code=503, detail="即梦测试任务暂时无法派发，请稍后重试。"
        ) from exc
    return session


__all__ = ["router"]
