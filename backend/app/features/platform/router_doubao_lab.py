from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.core.security import client_ip
from app.data.db import get_db
from app.services.audit import log_event
from app.services.doubao_lab import (
    cancel_doubao_lab_onboarding,
    complete_doubao_manual_verification,
    fail_doubao_manual_video_challenge_dispatch,
    fail_doubao_lab_test_dispatch,
    fail_doubao_capability_probe_dispatch,
    get_doubao_lab_session,
    list_doubao_lab_sessions,
    queue_doubao_lab_test,
    queue_doubao_capability_probe,
    reconcile_doubao_account_pool,
    rebind_doubao_account_proxy,
    retire_doubao_account,
    restart_doubao_account_login,
    start_doubao_manual_verification,
    start_doubao_lab_onboarding,
    verify_doubao_lab_session,
)
from app.services.flow_proxy_pool import list_flow_proxies, serialize_proxy
from app.services.ai_video.queues import AI_VIDEO_MAINTENANCE_TASK_QUEUE


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/doubao-lab",
    tags=["Platform / Doubao Lab"],
    dependencies=[Depends(require_platform_admin)],
)


class DoubaoBrowserSessionRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    proxy_id: int | None = Field(default=None, ge=1)


class DoubaoTestRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=495)
    duration: int = Field(default=4, ge=4, le=15)
    ratio: Literal["9:16", "16:9", "1:1"] = "9:16"


class DoubaoPoolStateRequest(BaseModel):
    enabled: bool


class DoubaoProxyRequest(BaseModel):
    proxy_id: int | None = Field(default=None, ge=1)


class DoubaoMembershipRequest(BaseModel):
    tier: Literal["free", "enhanced"]


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
        resource_type="doubao_lab",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details=dict(details or {}),
    )


@router.get("/overview")
def doubao_lab_overview(
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.hermes_agent.content_factory import browser_devices

    helper = Path("/opt/apps/doubao2api-lab/scripts/context_generate.py")
    python = Path("/opt/apps/doubao2api-lab/.venv/bin/python")
    from app.services.doubao_provider.pool import pool_membership_summary

    membership_summary = pool_membership_summary(db)
    return {
        "service": {
            "status": "healthy" if helper.is_file() and python.is_file() else "unavailable",
            "base_url": "local-process",
            "production_routing_enabled": True,
        },
        "capabilities": {
            "model": "Seedance 2.0 Mini",
            "durations": membership_summary["allowed_durations_seconds"],
            "ratios": ["9:16", "16:9", "1:1"],
            "browser_profile_isolated": True,
            "credential_storage": "encrypted",
            "account_pool": True,
            "automatic_auth_probe": True,
            "original_resource_download": True,
            "reference_images": 10,
            "numeric_quota_available": False,
            "account_membership_operator_managed": True,
        },
        "membership_summary": membership_summary,
        "devices": browser_devices(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
        "proxies": [serialize_proxy(row) for row in list_flow_proxies(db)],
        "sessions": list_doubao_lab_sessions(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
    }


@router.post("/browser-sessions")
def start_doubao_browser_session(
    payload: DoubaoBrowserSessionRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = start_doubao_lab_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
        proxy_id=int(payload.proxy_id) if payload.proxy_id is not None else None,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_lab.browser_onboarding_start",
        details={
            "device_id": payload.device_id,
            "session_id": session.get("session_id"),
            "proxy_id": int(payload.proxy_id) if payload.proxy_id is not None else None,
        },
    )
    db.commit()
    return session


@router.patch("/browser-sessions/{capture_id}/pool")
def update_doubao_pool_state(
    capture_id: str,
    payload: DoubaoPoolStateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.doubao_lab import _row_for_capture, _safe_session
    from app.services.doubao_provider.pool import set_pool_enabled

    row = _row_for_capture(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    set_pool_enabled(row, enabled=bool(payload.enabled))
    db.add(row)
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.state_update",
        details={"session_id": capture_id, "enabled": bool(payload.enabled)},
    )
    db.commit()
    db.refresh(row)
    return _safe_session(row)


@router.patch("/browser-sessions/{capture_id}/membership")
def update_doubao_account_membership(
    capture_id: str,
    payload: DoubaoMembershipRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.doubao_lab import _row_for_capture, _safe_session
    from app.services.doubao_provider.pool import set_account_membership

    row = _row_for_capture(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    try:
        set_account_membership(row, tier=payload.tier)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(row)
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.membership_update",
        details={"session_id": capture_id, "tier": payload.tier},
    )
    db.commit()
    db.refresh(row)
    return _safe_session(row)


@router.get("/browser-sessions/{capture_id}")
def get_doubao_browser_session_status(
    capture_id: str,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return get_doubao_lab_session(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )


@router.post("/browser-sessions/{capture_id}/cancel")
def cancel_doubao_browser_session(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = cancel_doubao_lab_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_lab.browser_onboarding_cancel",
        details={"session_id": capture_id},
    )
    db.commit()
    return session


@router.post("/browser-sessions/{capture_id}/verify")
async def verify_doubao_browser_session_route(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = await verify_doubao_lab_session(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_lab.session_verify",
        details={"session_id": capture_id, "state": session.get("state")},
    )
    db.commit()
    return session


@router.post("/browser-sessions/{capture_id}/manual-verification/start")
def start_doubao_manual_verification_route(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session, dispatch_required = start_doubao_manual_verification(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.manual_verification_start",
        details={
            "session_id": capture_id,
            "challenge_id": session.get("manual_verification", {}).get(
                "challenge_id"
            ),
        },
    )
    db.commit()
    if not dispatch_required:
        return session
    challenge_id = str(
        session.get("manual_verification", {}).get("challenge_id") or ""
    )
    try:
        from app.tasks.doubao_lab_tasks import run_doubao_manual_video_challenge

        run_doubao_manual_video_challenge.apply_async(
            kwargs={
                "workspace_id": int(me.workspace_id),
                "user_id": int(me.id),
                "bridge_id": str(session.get("bridge_id") or ""),
                "challenge_id": challenge_id,
            },
            queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        )
    except Exception as exc:
        fail_doubao_manual_video_challenge_dispatch(
            db,
            workspace_id=int(me.workspace_id),
            user_id=int(me.id),
            capture_id=capture_id,
            challenge_id=challenge_id,
        )
        db.commit()
        raise HTTPException(
            status_code=503, detail="豆包人工验证任务暂时无法派发，请稍后重试。"
        ) from exc
    return session


@router.post("/pool/reconcile")
def reconcile_doubao_account_pool_route(
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    result = reconcile_doubao_account_pool(
        db, workspace_id=int(me.workspace_id), user_id=int(me.id)
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.reconcile",
        details=result,
    )
    db.commit()
    return result


@router.post("/browser-sessions/{capture_id}/manual-verification/complete")
async def complete_doubao_manual_verification_route(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = await complete_doubao_manual_verification(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.manual_verification_complete",
        details={
            "session_id": capture_id,
            "state": session.get("manual_verification", {}).get("state"),
        },
    )
    db.commit()
    return session


@router.post("/browser-sessions/{capture_id}/probe")
def probe_doubao_browser_session_capability(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session, dispatch_required = queue_doubao_capability_probe(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    probe_id = str(session.get("pool", {}).get("capability", {}).get("probe_id") or "")
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.capability_probe",
        details={"session_id": capture_id, "probe_id": probe_id},
    )
    db.commit()
    if not dispatch_required:
        return session
    try:
        from app.tasks.doubao_lab_tasks import (
            probe_doubao_provider_account_capability,
        )

        probe_doubao_provider_account_capability.apply_async(
            kwargs={
                "workspace_id": int(me.workspace_id),
                "user_id": int(me.id),
                "bridge_id": str(session.get("bridge_id") or ""),
                "probe_id": probe_id,
            },
            queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        )
    except Exception as exc:
        fail_doubao_capability_probe_dispatch(
            db,
            workspace_id=int(me.workspace_id),
            user_id=int(me.id),
            capture_id=capture_id,
            probe_id=probe_id,
            error=str(exc),
        )
        db.commit()
        raise HTTPException(
            status_code=503, detail="豆包视频能力检测暂时无法派发，请稍后重试。"
        ) from exc
    return session


@router.post("/browser-sessions/{capture_id}/relogin")
def restart_doubao_browser_session_route(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = restart_doubao_account_login(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.account_relogin",
        details={"previous_session_id": capture_id, "session_id": session.get("session_id")},
    )
    db.commit()
    return session


@router.patch("/browser-sessions/{capture_id}/proxy")
def update_doubao_account_proxy(
    capture_id: str,
    payload: DoubaoProxyRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session = rebind_doubao_account_proxy(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
        proxy_id=int(payload.proxy_id) if payload.proxy_id is not None else None,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.proxy_rebind",
        details={
            "previous_session_id": capture_id,
            "session_id": session.get("session_id"),
            "proxy_id": int(payload.proxy_id) if payload.proxy_id is not None else None,
        },
    )
    db.commit()
    return session


@router.delete("/browser-sessions/{capture_id}")
def delete_doubao_account(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    result = retire_doubao_account(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_pool.account_delete",
        details={"session_id": capture_id, "bridge_id": result.get("bridge_id")},
    )
    db.commit()
    return result


@router.post("/browser-sessions/{capture_id}/tests")
def create_doubao_test(
    capture_id: str,
    payload: DoubaoTestRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    session, dispatch_required = queue_doubao_lab_test(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
        prompt=payload.prompt,
        duration=int(payload.duration),
        ratio=payload.ratio,
    )
    _audit(
        db,
        request,
        me,
        action="platform.doubao_lab.test_queued",
        details={
            "session_id": capture_id,
            "test_id": session.get("test", {}).get("id"),
            "model": "seedance_v2.0_mini",
            "duration": int(payload.duration),
            "ratio": payload.ratio,
        },
    )
    db.commit()
    if not dispatch_required:
        return session
    test_id = str(session.get("test", {}).get("id") or "")
    try:
        from app.tasks.doubao_lab_tasks import generate_doubao_lab_test

        generate_doubao_lab_test.apply_async(
            kwargs={
                "workspace_id": int(me.workspace_id),
                "user_id": int(me.id),
                "bridge_id": str(session.get("bridge_id") or ""),
                "test_id": test_id,
            },
            queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        )
    except Exception as exc:  # durable dispatch failure
        fail_doubao_lab_test_dispatch(
            db,
            workspace_id=int(me.workspace_id),
            user_id=int(me.id),
            capture_id=capture_id,
            test_id=test_id,
            error=str(exc),
        )
        db.commit()
        raise HTTPException(
            status_code=503, detail="豆包测试任务暂时无法派发，请稍后重试。"
        ) from exc
    return session


__all__ = ["router"]
