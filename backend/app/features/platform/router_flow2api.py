from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.core.security import client_ip
from app.data.db import get_db
from app.services.audit import log_event
from app.services.flow2api_admin import Flow2ApiAdminClient, Flow2ApiAdminError
from app.services.flow_proxy_pool import (
    create_flow_proxy,
    find_proxy_by_url,
    get_flow_proxy,
    list_flow_proxies,
    normalize_proxy_url,
    resolve_flow_proxy_url,
    serialize_proxy,
    update_flow_proxy,
)


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/flow2api",
    tags=["Platform / Flow2API Account Pool"],
    dependencies=[Depends(require_platform_admin)],
)

TOKEN_FIELDS = {
    "id",
    "has_st",
    "has_at",
    "at_expires",
    "email",
    "name",
    "remark",
    "is_active",
    "routable",
    "auth_state",
    "ban_reason",
    "banned_at",
    "created_at",
    "last_used_at",
    "use_count",
    "credits",
    "user_paygate_tier",
    "current_project_id",
    "current_project_name",
    "image_enabled",
    "video_enabled",
    "image_concurrency",
    "video_concurrency",
    "image_count",
    "video_count",
    "error_count",
    "membership_confirmed_status",
    "membership_candidate",
    "membership_candidate_count",
    "keepalive_enabled",
    "runtime_mode",
    "profile_state",
    "verified_email",
    "last_keepalive_success_at",
    "last_keepalive_status",
    "last_keepalive_error",
    "keepalive_failure_count",
    "next_due_at",
    "last_failure_at",
    "last_failure_code",
    "last_observed_tier",
    "last_observed_at",
    "retired_at",
    "restored_at",
    "browser_profile_id",
    "browser_fingerprint_state",
    "browser_fingerprint_updated_at",
    "captcha_proxy_url",
}
STAT_FIELDS = {
    "total_tokens",
    "active_tokens",
    "today_images",
    "total_images",
    "today_videos",
    "total_videos",
    "today_errors",
    "total_errors",
}


class FlowTokenImportRequest(BaseModel):
    raw: str = Field(min_length=20, max_length=2_000_000)
    remark: str | None = Field(default=None, max_length=500)
    image_enabled: bool = False
    video_enabled: bool = True
    image_concurrency: int = Field(default=1, ge=-1, le=32)
    video_concurrency: int = Field(default=1, ge=-1, le=32)

    @field_validator("raw")
    @classmethod
    def validate_raw(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("账号凭据不能为空")
        return cleaned


class FlowTokenUpdateRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=500)
    image_enabled: bool | None = None
    video_enabled: bool | None = None
    image_concurrency: int | None = Field(default=None, ge=-1, le=32)
    video_concurrency: int | None = Field(default=None, ge=-1, le=32)
    proxy_id: int | None = Field(default=None, ge=1)


class FlowBrowserSessionRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    remark: str | None = Field(default=None, max_length=500)
    image_enabled: bool = False
    video_enabled: bool = True
    image_concurrency: int = Field(default=1, ge=-1, le=32)
    video_concurrency: int = Field(default=1, ge=-1, le=32)
    proxy_id: int = Field(ge=1)


class FlowBridgeHostAssignmentRequest(BaseModel):
    target_device_id: str = Field(min_length=1, max_length=128)
    source_workspace_id: int = Field(ge=1)
    source_user_id: int = Field(ge=1)
    source_device_id: str = Field(min_length=1, max_length=128)


class FlowProxyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    proxy_url: str = Field(min_length=8, max_length=500)
    is_active: bool = True

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        return normalize_proxy_url(value)


class FlowProxyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    proxy_url: str | None = Field(default=None, min_length=8, max_length=500)
    is_active: bool | None = None

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        return normalize_proxy_url(value) if value is not None else None


@router.get("/bridge-agent/download")
def download_flow_bridge_agent(
    request: Request,
    device_id: str = Query(min_length=1, max_length=128),
    device_name: str | None = Query(default=None, max_length=255),
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.hermes_agent.content_factory import (
        _new_agent_slot,
        build_bridge_agent_executable,
    )

    registration = _new_agent_slot(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=device_id,
        device_name=device_name or "Windows device",
        inbox_root="%LOCALAPPDATA%\\MYUPONA\\HermesInbox",
        slot_index=0,
    )
    meta = dict(registration.meta_json or {})
    meta["account_device_bound"] = True
    registration.meta_json = meta
    registration.status = "standby"
    db.add(registration)
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.bridge_agent_download",
        details={"device_id": device_id},
    )
    db.commit()
    filename, executable = build_bridge_agent_executable(
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=device_id,
        device_name=device_name,
        api_base_url=f"{request.url.scheme}://{request.url.netloc}",
    )
    return Response(
        content=executable,
        media_type="application/vnd.microsoft.portable-executable",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _flow_error(exc: Flow2ApiAdminError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _safe_token(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    safe = {key: row.get(key) for key in TOKEN_FIELDS if key in row}
    safe["routable"] = bool(
        row.get("routable", bool(row.get("is_active")) and not row.get("ban_reason"))
    )
    if not safe.get("auth_state"):
        safe["auth_state"] = (
            "blocked"
            if row.get("ban_reason") in {"GRANT_EXPIRED", "ST_REVOKED"}
            else "missing"
            if not row.get("has_at")
            else "ready"
        )
    proxy_url = str(safe.get("captcha_proxy_url") or "").strip()
    if proxy_url:
        try:
            parsed = urlsplit(proxy_url)
            host = str(parsed.hostname or "").strip()
            if not host:
                safe["captcha_proxy_url"] = None
            else:
                display_host = f"[{host}]" if ":" in host else host
                netloc = f"{display_host}:{parsed.port}" if parsed.port else display_host
                safe["captcha_proxy_url"] = urlunsplit(
                    (parsed.scheme.lower(), netloc, "", "", "")
                )
        except ValueError:
            safe["captcha_proxy_url"] = None
    return safe


def _proxy_usage(
    db: Session, upstream_tokens: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    usage = {int(row.id): 0 for row in list_flow_proxies(db)}
    safe_tokens: list[dict[str, Any]] = []
    for raw in upstream_tokens:
        token = _safe_token(raw)
        bound = find_proxy_by_url(db, str(raw.get("captcha_proxy_url") or ""))
        if bound is not None:
            token["proxy_id"] = int(bound.id)
            token["proxy_name"] = bound.name
            usage[int(bound.id)] = usage.get(int(bound.id), 0) + 1
        else:
            token["proxy_id"] = None
            token["proxy_name"] = None
        safe_tokens.append(token)
    return safe_tokens, usage


def _require_proxy_url(db: Session, proxy_id: int) -> str:
    try:
        return resolve_flow_proxy_url(db, int(proxy_id), require_active=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _audit(
    db: Session,
    request: Request,
    me: SessionUser,
    *,
    action: str,
    token_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    safe_details = dict(details or {})
    if token_id is not None:
        safe_details["token_id"] = int(token_id)
    log_event(
        db,
        action=action,
        resource_type="flow2api_account_pool",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details=safe_details,
    )


@router.get("/overview")
async def flow_account_pool_overview(
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    client = Flow2ApiAdminClient()
    try:
        stats = await client.request("GET", "/api/stats")
        rows = await client.request("GET", "/api/tokens")
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    safe_stats = (
        {key: stats.get(key) for key in STAT_FIELDS if key in stats}
        if isinstance(stats, dict)
        else {}
    )
    upstream_tokens = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    tokens, proxy_usage = _proxy_usage(db, upstream_tokens)
    proxies = [
        serialize_proxy(row, in_use_count=proxy_usage.get(int(row.id), 0))
        for row in list_flow_proxies(db)
    ]
    from app.services.flow_browser_onboarding import (
        list_flow_browser_sessions,
        reconcile_flow_browser_bindings_from_upstream,
    )
    from app.services.hermes_agent.content_factory import browser_devices, observed_bridge_hosts

    if reconcile_flow_browser_bindings_from_upstream(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        upstream_tokens=upstream_tokens,
    ):
        db.commit()

    return {
        "service": {"status": "healthy", "account_count": len(tokens)},
        "stats": safe_stats,
        "tokens": tokens,
        "capabilities": {
            "credential_import": False,
            "credit_refresh": True,
            "access_token_refresh": True,
            "profile_onboarding": True,
            "http_keepalive": True,
            "browser_reauth": True,
            "automatic_browser_reauth": True,
            "browser_keepalive": False,
            "reference_image_limit": int(settings.SUB2API_FLOW_REFERENCE_IMAGE_LIMIT),
            "proxy_pool_managed": True,
        },
        "devices": browser_devices(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
        "bridge_hosts": observed_bridge_hosts(db),
        "browser_sessions": list_flow_browser_sessions(
            db, workspace_id=int(me.workspace_id), user_id=int(me.id)
        ),
        "proxies": proxies,
    }


@router.post("/bridge-host-assignments")
def assign_flow_bridge_host(
    payload: FlowBridgeHostAssignmentRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.hermes_agent.content_factory import assign_bridge_device_to_host

    assignment = assign_bridge_device_to_host(
        db,
        target_workspace_id=int(me.workspace_id),
        target_user_id=int(me.id),
        target_device_id=payload.target_device_id,
        source_workspace_id=payload.source_workspace_id,
        source_user_id=payload.source_user_id,
        source_device_id=payload.source_device_id,
        assigned_by=int(me.id),
    )
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.bridge_host_assign",
        details={
            "target_device_id": payload.target_device_id,
            "source_workspace_id": payload.source_workspace_id,
            "source_user_id": payload.source_user_id,
            "source_device_id": payload.source_device_id,
            "host_id": assignment["host_id"],
        },
    )
    db.commit()
    return {"ok": True, "assignment": assignment}


@router.post("/proxies")
def create_proxy(
    payload: FlowProxyCreateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        row = create_flow_proxy(
            db,
            name=payload.name,
            proxy_url=payload.proxy_url,
            is_active=payload.is_active,
            actor_user_id=int(me.id),
        )
        _audit(
            db,
            request,
            me,
            action="platform.flow2api_proxy.create",
            details={"proxy_id": int(row.id), "display_url": row.display_url},
        )
        db.commit()
    except (IntegrityError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="代理名称或地址已经存在") from exc
    return serialize_proxy(row)


async def _upstream_proxy_usage(db: Session) -> tuple[list[dict[str, Any]], dict[int, int]]:
    try:
        rows = await Flow2ApiAdminClient().request("GET", "/api/tokens")
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    upstream_tokens = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return _proxy_usage(db, upstream_tokens)


@router.patch("/proxies/{proxy_id}")
async def update_proxy(
    proxy_id: int,
    payload: FlowProxyUpdateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    try:
        row = get_flow_proxy(db, proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _, usage = await _upstream_proxy_usage(db)
    if payload.proxy_url is not None and usage.get(int(row.id), 0) > 0:
        raise HTTPException(status_code=409, detail="该代理仍绑定账号，请先为账号更换代理")
    try:
        row = update_flow_proxy(
            db,
            row=row,
            name=payload.name,
            proxy_url=payload.proxy_url,
            is_active=payload.is_active,
            actor_user_id=int(me.id),
        )
        _audit(
            db,
            request,
            me,
            action="platform.flow2api_proxy.update",
            details={"proxy_id": int(row.id), "fields": sorted(changes)},
        )
        db.commit()
    except (IntegrityError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="代理名称或地址已经存在") from exc
    return serialize_proxy(row, in_use_count=usage.get(int(row.id), 0))


@router.delete("/proxies/{proxy_id}")
async def delete_proxy(
    proxy_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        row = get_flow_proxy(db, proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _, usage = await _upstream_proxy_usage(db)
    if usage.get(int(row.id), 0) > 0:
        raise HTTPException(status_code=409, detail="该代理仍绑定账号，不能删除")
    display_url = row.display_url
    db.delete(row)
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_proxy.delete",
        details={"proxy_id": int(proxy_id), "display_url": display_url},
    )
    db.commit()
    return {"success": True}


@router.post("/browser-sessions")
async def start_flow_browser_session(
    payload: FlowBrowserSessionRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.flow_browser_onboarding import (
        reconcile_flow_browser_bindings_from_upstream,
        start_flow_browser_onboarding,
    )

    try:
        upstream_tokens = await Flow2ApiAdminClient().request("GET", "/api/tokens")
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    reconcile_flow_browser_bindings_from_upstream(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        upstream_tokens=(
            upstream_tokens if isinstance(upstream_tokens, list) else []
        ),
    )

    session = start_flow_browser_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
        remark=payload.remark,
        image_enabled=payload.image_enabled,
        video_enabled=payload.video_enabled,
        image_concurrency=payload.image_concurrency,
        video_concurrency=payload.video_concurrency,
        proxy_url=_require_proxy_url(db, payload.proxy_id),
    )
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.browser_onboarding_start",
        details={"device_id": payload.device_id, "session_id": session.get("session_id"), "proxy_id": payload.proxy_id},
    )
    db.commit()
    return session


@router.post("/tokens/{token_id}/browser-reauth")
def start_flow_browser_reauth(
    token_id: int,
    payload: FlowBrowserSessionRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.flow_browser_onboarding import start_flow_browser_onboarding

    session = start_flow_browser_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
        remark=payload.remark,
        image_enabled=payload.image_enabled,
        video_enabled=payload.video_enabled,
        image_concurrency=payload.image_concurrency,
        video_concurrency=payload.video_concurrency,
        proxy_url=_require_proxy_url(db, payload.proxy_id),
        token_id=int(token_id),
    )
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.browser_reauth_start",
        token_id=token_id,
        details={"device_id": payload.device_id, "session_id": session.get("session_id"), "proxy_id": payload.proxy_id},
    )
    db.commit()
    return session


@router.get("/browser-sessions/{capture_id}")
def get_flow_browser_session_status(
    capture_id: str,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.flow_browser_onboarding import get_flow_browser_session

    return get_flow_browser_session(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )


@router.post("/browser-sessions/{capture_id}/cancel")
def cancel_flow_browser_session(
    capture_id: str,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    from app.services.flow_browser_onboarding import cancel_flow_browser_onboarding

    session = cancel_flow_browser_onboarding(
        db,
        workspace_id=int(me.workspace_id),
        user_id=int(me.id),
        capture_id=capture_id,
    )
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.browser_onboarding_cancel",
        details={"session_id": capture_id},
    )
    db.commit()
    return session


@router.post("/tokens")
async def import_flow_account(
    payload: FlowTokenImportRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    client = Flow2ApiAdminClient()
    try:
        result = await client.request(
            "POST",
            "/api/tokens",
            payload={
                "raw": payload.raw,
                "remark": payload.remark,
                "image_enabled": payload.image_enabled,
                "video_enabled": payload.video_enabled,
                "image_concurrency": payload.image_concurrency,
                "video_concurrency": payload.video_concurrency,
            },
        )
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    token = result.get("token") if isinstance(result, dict) else None
    token_id = token.get("id") if isinstance(token, dict) else None
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.import",
        token_id=int(token_id) if token_id is not None else None,
        details={
            "image_enabled": payload.image_enabled,
            "video_enabled": payload.video_enabled,
            "image_concurrency": payload.image_concurrency,
            "video_concurrency": payload.video_concurrency,
        },
    )
    return {
        "success": True,
        "message": str(result.get("message") or "账号已验证并加入号池"),
        "token": _safe_token(token) if isinstance(token, dict) else {},
    }


@router.patch("/tokens/{token_id}")
async def update_flow_account(
    token_id: int,
    payload: FlowTokenUpdateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    proxy_id = changes.pop("proxy_id", None)
    if proxy_id is not None:
        try:
            changes["captcha_proxy_url"] = _require_proxy_url(db, int(proxy_id))
        except HTTPException:
            raise
    try:
        result = await Flow2ApiAdminClient().request(
            "PUT", f"/api/tokens/{int(token_id)}", payload=changes
        )
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.update",
        token_id=token_id,
        details={"fields": sorted(changes), "proxy_id": proxy_id},
    )
    return result


async def _token_action(
    token_id: int,
    action_name: str,
    upstream_path: str,
    request: Request,
    me: SessionUser,
    db: Session,
):
    try:
        result = await Flow2ApiAdminClient().request("POST", upstream_path)
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    _audit(
        db,
        request,
        me,
        action=f"platform.flow2api_account.{action_name}",
        token_id=token_id,
    )
    return result


@router.post("/tokens/{token_id}/enable")
async def enable_flow_account(
    token_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return await _token_action(
        token_id, "enable", f"/api/tokens/{token_id}/enable", request, me, db
    )


@router.post("/tokens/{token_id}/disable")
async def disable_flow_account(
    token_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return await _token_action(
        token_id, "disable", f"/api/tokens/{token_id}/disable", request, me, db
    )


@router.post("/tokens/{token_id}/refresh-credits")
async def refresh_flow_account_credits(
    token_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return await _token_action(
        token_id,
        "refresh_credits",
        f"/api/tokens/{token_id}/refresh-credits",
        request,
        me,
        db,
    )


@router.post("/tokens/{token_id}/refresh-access")
async def refresh_flow_account_access(
    token_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    return await _token_action(
        token_id,
        "refresh_access",
        f"/api/tokens/{token_id}/refresh-at",
        request,
        me,
        db,
    )


@router.delete("/tokens/{token_id}")
async def delete_flow_account(
    token_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        result = await Flow2ApiAdminClient().request(
            "DELETE", f"/api/tokens/{int(token_id)}"
        )
    except Flow2ApiAdminError as exc:
        raise _flow_error(exc) from None
    # Preserve the Windows profile tombstone so a later account can never be
    # allocated onto a directory that still contains the deleted Google login.
    from app.data.models.hermes_agent import HermesBrowserBridge
    from app.services.flow_browser_onboarding import is_flow_account_slot

    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(me.workspace_id),
            HermesBrowserBridge.user_id == int(me.id),
            HermesBrowserBridge.status != "retired",
        )
        .all()
    )
    for row in rows:
        meta = dict(row.meta_json or {})
        if not is_flow_account_slot(row) or int(meta.get("flow_token_id") or 0) != int(token_id):
            continue
        meta.update(
            {
                "flow_capture_state": "cancelled",
                "flow_capture_message": "账号已从号池删除；原浏览器 Profile 已封存且不会复用。",
                "flow_profile_retired": True,
                "flow_profile_retired_at": datetime.now().isoformat(),
            }
        )
        row.meta_json = meta
        row.status = "standby"
        db.add(row)
    _audit(
        db,
        request,
        me,
        action="platform.flow2api_account.delete",
        token_id=token_id,
    )
    return result


__all__ = ["router"]
