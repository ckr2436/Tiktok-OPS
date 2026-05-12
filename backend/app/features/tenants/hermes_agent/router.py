from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.services.audit import log_event
from app.services.hermes_agent import repository
from app.services.hermes_agent.client import HermesAgentClient
from app.services.hermes_agent.prompts import SUPPORTED_TASK_TYPES, normalize_task_type
from app.services.hermes_agent.service import create_and_run, ensure_user_can_use_task

from .schemas import (
    FeaturePermissionIn,
    FeaturePermissionListResponse,
    FeaturePermissionOut,
    HermesCapabilitiesResponse,
    HermesConversationListResponse,
    HermesMessageListResponse,
    HermesHealthResponse,
    HermesRunListResponse,
    HermesRunRequest,
    HermesRunResponse,
    SpecializedHermesRequest,
)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/hermes-agent",
    tags=["Tenant / Hermes Agent"],
)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _run_out(run) -> HermesRunResponse:
    return HermesRunResponse.model_validate(run)


@router.get("/capabilities", response_model=HermesCapabilitiesResponse)
def capabilities(
    workspace_id: int,
    task_type: str | None = Query(default="general", max_length=64),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    normalized_task_type = normalize_task_type(task_type)
    can_use_task = True
    try:
        ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type=normalized_task_type)
    except APIError:
        can_use_task = False

    return HermesCapabilitiesResponse(
        enabled=bool(settings.HERMES_AGENT_ENABLED),
        model=settings.HERMES_AGENT_MODEL,
        task_types=dict(SUPPORTED_TASK_TYPES),
        max_input_chars=int(settings.HERMES_AGENT_MAX_INPUT_CHARS),
        allow_member=bool(settings.HERMES_AGENT_ALLOW_MEMBER),
        require_explicit_permission=bool(settings.HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION),
        can_use_task=can_use_task,
    )


@router.get("/health", response_model=HermesHealthResponse)
async def health(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    detail = await HermesAgentClient().health()
    return HermesHealthResponse(ok=True, detail=detail)


@router.post("/runs", response_model=HermesRunResponse)
async def create_run(
    workspace_id: int,
    payload: HermesRunRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    run = await create_and_run(
        db,
        workspace_id=int(workspace_id),
        me=me,
        task_type=payload.task_type,
        title=payload.title,
        user_input=payload.input,
        input_json=payload.input_json,
        workspace_context=payload.workspace_context,
        conversation_key=payload.conversation_key,
        async_mode=bool(payload.async_mode),
        request_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _run_out(run)


@router.get("/runs", response_model=HermesRunListResponse)
def list_runs(
    workspace_id: int,
    task_type: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    mine: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type=task_type or "general")
    rows = repository.list_runs(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id) if mine else None,
        task_type=normalize_task_type(task_type) if task_type else None,
        status=status,
        limit=limit,
        offset=offset,
    )
    return HermesRunListResponse(items=[_run_out(row) for row in rows])


@router.get("/runs/{run_id}", response_model=HermesRunResponse)
def get_run(
    workspace_id: int,
    run_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    run = repository.get_run(db, workspace_id=int(workspace_id), run_id=run_id)
    if run is None:
        from app.core.errors import APIError

        raise APIError("HERMES_RUN_NOT_FOUND", "Hermes Agent run not found.", 404)
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type=run.task_type)
    return _run_out(run)


@router.post("/seo", response_model=HermesRunResponse)
async def seo(
    workspace_id: int,
    payload: SpecializedHermesRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _run_out(await create_and_run(db, workspace_id=int(workspace_id), me=me, task_type="seo", title=payload.title, user_input=payload.input, input_json=payload.input_json, workspace_context=payload.workspace_context, conversation_key=payload.conversation_key, async_mode=payload.async_mode, request_ip=_client_ip(request), user_agent=request.headers.get("user-agent")))


@router.post("/geo", response_model=HermesRunResponse)
async def geo(
    workspace_id: int,
    payload: SpecializedHermesRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _run_out(await create_and_run(db, workspace_id=int(workspace_id), me=me, task_type="geo", title=payload.title, user_input=payload.input, input_json=payload.input_json, workspace_context=payload.workspace_context, conversation_key=payload.conversation_key, async_mode=payload.async_mode, request_ip=_client_ip(request), user_agent=request.headers.get("user-agent")))


@router.post("/video-analysis", response_model=HermesRunResponse)
async def video_analysis(
    workspace_id: int,
    payload: SpecializedHermesRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _run_out(await create_and_run(db, workspace_id=int(workspace_id), me=me, task_type="video_analysis", title=payload.title, user_input=payload.input, input_json=payload.input_json, workspace_context=payload.workspace_context, conversation_key=payload.conversation_key, async_mode=payload.async_mode, request_ip=_client_ip(request), user_agent=request.headers.get("user-agent")))


@router.post("/script", response_model=HermesRunResponse)
async def script(
    workspace_id: int,
    payload: SpecializedHermesRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _run_out(await create_and_run(db, workspace_id=int(workspace_id), me=me, task_type="script", title=payload.title, user_input=payload.input, input_json=payload.input_json, workspace_context=payload.workspace_context, conversation_key=payload.conversation_key, async_mode=payload.async_mode, request_ip=_client_ip(request), user_agent=request.headers.get("user-agent")))


@router.post("/product-copy", response_model=HermesRunResponse)
async def product_copy(
    workspace_id: int,
    payload: SpecializedHermesRequest,
    request: Request,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _run_out(await create_and_run(db, workspace_id=int(workspace_id), me=me, task_type="product_copy", title=payload.title, user_input=payload.input, input_json=payload.input_json, workspace_context=payload.workspace_context, conversation_key=payload.conversation_key, async_mode=payload.async_mode, request_ip=_client_ip(request), user_agent=request.headers.get("user-agent")))


@router.get("/conversations", response_model=HermesConversationListResponse)
def list_conversations(
    workspace_id: int,
    mine: bool = Query(default=True),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    rows = repository.list_conversations(db, workspace_id=int(workspace_id), user_id=int(me.id) if mine else None)
    return HermesConversationListResponse(items=rows)


@router.get("/conversations/{conversation_id}/messages", response_model=HermesMessageListResponse)
def list_messages(
    workspace_id: int,
    conversation_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    rows = repository.list_messages(db, workspace_id=int(workspace_id), conversation_id=int(conversation_id), limit=limit)
    return HermesMessageListResponse(items=rows)


@router.get("/permissions", response_model=FeaturePermissionListResponse)
def list_permissions(
    workspace_id: int,
    user_id: int | None = Query(default=None, ge=1),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    rows = repository.list_feature_permissions(db, workspace_id=int(workspace_id), user_id=user_id)
    return FeaturePermissionListResponse(items=[FeaturePermissionOut.model_validate(row) for row in rows])


@router.put("/permissions", response_model=FeaturePermissionOut)
def set_permission(
    workspace_id: int,
    payload: FeaturePermissionIn,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = repository.set_feature_permission(
        db,
        workspace_id=int(workspace_id),
        user_id=int(payload.user_id),
        feature_key=payload.feature_key,
        is_enabled=bool(payload.is_enabled),
        actor_user_id=int(me.id),
    )
    log_event(
        db,
        action="hermes_agent.permission.set",
        resource_type="user_feature_permission",
        resource_id=int(row.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        target_user_id=int(payload.user_id),
        workspace_id=int(workspace_id),
        details={"feature_key": payload.feature_key, "is_enabled": bool(payload.is_enabled)},
    )
    db.flush()
    return FeaturePermissionOut.model_validate(row)
