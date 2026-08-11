from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.celery_app import WHISPER_TASK_QUEUE, celery_app
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.hermes_agent import (
    HermesBrowserBridge,
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
    HermesContentProductAsset,
)
from app.data.models.users import User
from app.services.audit import log_event
from app.services.ai_video.accounts import video_model_routing_catalog
from app.services.ai_video.queues import (
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    production_video_queue,
)
from app.services.hermes_agent import repository
from app.services.hermes_agent.client import HermesAgentClient
from app.services.hermes_agent.prompts import SUPPORTED_TASK_TYPES, normalize_task_type
from app.services.hermes_agent.service import create_and_run, ensure_user_can_use_task
from app.services.hermes_agent.content_factory import (
    BROWSER_INBOX,
    MAX_CHARACTER_REFERENCE_BYTES,
    MAX_PRODUCT_ASSET_BYTES,
    MAX_REFERENCE_VIDEO_BYTES,
    STORAGE_ROOT,
    WAITING_STAGES,
    bind_browser_device,
    bridge_status,
    build_project_deliverables_zip,
    bridge_agent_inbox_manifest,
    build_bridge_agent_executable,
    configure_variant_rollout_gate,
    cleanup_uncommitted_asset_files,
    create_product_facts_project,
    create_project as create_content_project,
    create_product as create_content_product,
    delete_product as delete_content_product,
    delete_product_asset as delete_content_product_asset,
    delete_project as delete_content_project,
    finalize_deleted_project,
    get_product as get_content_product,
    get_project as get_content_project,
    is_product_facts_project,
    list_products as list_content_products,
    pause_project as pause_content_project,
    prepare_browser_slot,
    product_out,
    project_deliverables,
    project_out,
    queue_stage,
    remove_browser_slot,
    reconcile_bridge_agent,
    resume_waiting_project_production,
    resume_stage_force_browser,
    select_browser_device,
    unbind_browser_device,
    ensure_bridge_agent_file_access,
    restart_project as restart_content_project,
    resume_project as resume_content_project,
    save_asset,
    save_product_asset,
    update_product as update_content_product,
    update_project as update_content_project,
    visible_project_query,
    verify_bridge_agent_token,
)
from app.services.hermes_agent.content_producer import (
    authoritative_producer_script,
    begin_producer_followup,
    compile_confirmed_creative_copy_contract,
    PRODUCER_PROMPT_VERSION,
    copy_producer_attachments_to_project,
    confirmed_project_parameters,
    delete_producer_attachment,
    get_or_create_producer_conversation,
    producer_attachment_out,
    producer_attachments,
    producer_session,
    run_producer_turn,
    save_producer_attachment,
    stage_producer_reference_link,
)
from app.services.hermes_agent.content_intent import intent_manifest_from_spec

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
    ContentFactoryBridgeDeviceAction,
    ContentFactoryBridgeAgentHeartbeat,
    ContentFactoryBridgeFlowCapture,
    ContentFactoryBridgeJimengCapture,
    ContentFactoryBridgeDoubaoCapture,
    ContentFactoryBridgeYtDlpCapture,
    ContentFactoryBridgeStatus,
    ContentFactoryCommand,
    ContentFactoryPauseRequest,
    ContentFactoryRolloutGateRequest,
    ContentFactoryProjectRestart,
    ContentFactoryProjectUpdate,
    ContentFactoryProjectList,
    ContentFactoryProjectOut,
    ContentFactoryProducerConfirmRequest,
    ContentFactoryProducerAttachmentOut,
    ContentFactoryProducerReferenceLinkRequest,
    ContentFactoryProducerSessionOut,
    ContentFactoryProducerTurnRequest,
    ContentFactoryProducerTurnResponse,
    ContentFactoryAdminProjectList,
    ContentFactoryProductCreate,
    ContentFactoryProductUpdate,
    ContentFactoryProductList,
    ContentFactoryProductOut,
    ContentFactoryProductAssetOut,
    ContentFactoryStageOut,
    ContentFactoryAssetOut,
)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/hermes-agent",
    tags=["Tenant / Hermes Agent"],
)

_PRODUCER_COMMERCIAL_HISTORY_PATTERN = re.compile(
    r"[$€£¥]\s*\d|(?:新客|立减|促销|折扣|优惠券|包邮|"
    r"\bprice\b|\bdiscount\b|\bcoupon\b|\bpromot\w*\b|\bshipping\b|\bshipped\b)",
    re.I,
)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _run_out(run) -> HermesRunResponse:
    return HermesRunResponse.model_validate(run)


def _content_asset_path(raw_path: str | Path) -> Path:
    """Resolve a persisted asset only inside the content repository."""
    root = STORAGE_ROOT.resolve()
    try:
        path = Path(raw_path).resolve(strict=True)
    except OSError as exc:
        raise APIError(
            "CONTENT_ASSET_FILE_MISSING",
            "The stored asset file is missing.",
            404,
        ) from exc
    if root not in path.parents or not path.is_file():
        raise APIError(
            "CONTENT_ASSET_STORAGE_SCOPE_INVALID",
            "The stored asset is outside the content repository.",
            404,
        )
    return path


def _admin_content_project(
    db: Session,
    *,
    workspace_id: int,
    project_key: str,
) -> HermesContentFactoryProject:
    project = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.project_key == str(project_key),
            HermesContentFactoryProject.status != "deleted",
        )
        .one_or_none()
    )
    if project is None:
        raise APIError("CONTENT_PROJECT_NOT_FOUND", "Content factory project not found.", 404)
    return project


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


@router.get("/content-factory/bridge", response_model=ContentFactoryBridgeStatus)
async def content_factory_bridge(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


@router.post("/content-factory/bridge/devices/bind", response_model=ContentFactoryBridgeStatus)
async def bind_content_factory_bridge_device(
    workspace_id: int,
    payload: ContentFactoryBridgeDeviceAction,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    bind_browser_device(
        db, workspace_id=int(workspace_id), user_id=int(me.id), device_id=payload.device_id,
    )
    db.commit()
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


@router.post("/content-factory/bridge/devices/select", response_model=ContentFactoryBridgeStatus)
async def select_content_factory_bridge_device(
    workspace_id: int,
    payload: ContentFactoryBridgeDeviceAction,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    select_browser_device(
        db, workspace_id=int(workspace_id), user_id=int(me.id), device_id=payload.device_id,
    )
    db.commit()
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


@router.delete("/content-factory/bridge/devices/{device_id}", response_model=ContentFactoryBridgeStatus)
async def unbind_content_factory_bridge_device(
    workspace_id: int,
    device_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    unbind_browser_device(
        db, workspace_id=int(workspace_id), user_id=int(me.id), device_id=device_id,
    )
    db.commit()
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


@router.post("/content-factory/bridge/slots", response_model=ContentFactoryBridgeStatus)
async def prepare_content_factory_browser_slot(
    workspace_id: int,
    payload: ContentFactoryBridgeDeviceAction,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    prepare_browser_slot(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
    )
    db.commit()
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


@router.delete("/content-factory/bridge/slots/{bridge_id}", response_model=ContentFactoryBridgeStatus)
async def remove_content_factory_browser_slot(
    workspace_id: int,
    bridge_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    remove_browser_slot(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        bridge_id=bridge_id,
    )
    db.commit()
    return await bridge_status(db, workspace_id=workspace_id, user_id=me.id)


def _bridge_agent_identity(
    authorization: str | None,
    *,
    workspace_id: int,
    db: Session,
) -> dict:
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise APIError("CONTENT_BROWSER_AGENT_AUTH_REQUIRED", "Browser agent authentication is required.", 401)
    identity = verify_bridge_agent_token(authorization[len(prefix):].strip())
    if int(identity["workspace_id"]) != int(workspace_id):
        raise APIError("CONTENT_BROWSER_AGENT_FORBIDDEN", "Browser agent workspace does not match.", 403)
    user_id = int(identity["user_id"])
    user = db.get(User, user_id)  # noqa: F821
    if user is None or user.deleted_at is not None or not user.is_active or int(user.workspace_id) != int(workspace_id):
        raise APIError("CONTENT_BROWSER_AGENT_USER_DISABLED", "Browser agent user is no longer active.", 403)
    from app.services.hermes_agent.content_factory import (
        _account_device_rows,
        _agent_rows,
        _bridge_device_bound,
    )

    bridges = _agent_rows(db, workspace_id=workspace_id, user_id=user_id, device_id=str(identity["device_id"]))
    if not bridges:
        historical = _account_device_rows(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            device_id=str(identity["device_id"]),
            include_retired=True,
        )
        if not any(_bridge_device_bound(row) for row in historical):
            raise APIError("CONTENT_BROWSER_AGENT_UNREGISTERED", "Browser agent device is not registered for this workspace/user.", 401)
    return identity


@router.get("/content-factory/bridge/agent/download")
def download_content_factory_bridge_agent(
    workspace_id: int,
    request: Request,
    device_id: str = Query(min_length=1, max_length=128),
    device_name: str | None = Query(default=None, max_length=255),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    # Retire legacy one-script-per-slot rows for this browser device. The new
    # executable owns child slots and will never reuse their processes/ports.
    legacy_rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(me.id),
        )
        .all()
    )
    for row in legacy_rows:
        from app.services.jimeng_lab import is_external_account_slot

        if is_external_account_slot(row):
            continue
        meta = dict(row.meta_json or {})
        same_legacy_device = row.device_id == str(device_id)
        same_agent_device = str(meta.get("agent_device_id") or "") == str(device_id)
        if not (same_legacy_device or same_agent_device):
            continue
        if row.active_project_id is not None:
            continue
        if not bool(meta.get("agent_managed")) or same_agent_device:
            row.status = "retired"
            row.active_project_id = None
            row.active_stage_id = None
            row.lease_expires_at = None
            meta["retired_reason"] = "bridge_agent_redownload"
            meta["retired_at"] = datetime.now().isoformat()
            row.meta_json = meta
            db.add(row)
    db.commit()
    filename, payload = build_bridge_agent_executable(
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        device_id=device_id,
        device_name=device_name,
        api_base_url=f"{request.url.scheme}://{request.url.netloc}",
    )
    return Response(
        content=payload,
        media_type="application/vnd.microsoft.portable-executable",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/content-factory/bridge/agent/heartbeat")
def content_factory_bridge_agent_heartbeat(
    workspace_id: int,
    payload: ContentFactoryBridgeAgentHeartbeat,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    if str(identity["device_id"]) != str(payload.device_id):
        raise APIError("CONTENT_BROWSER_AGENT_DEVICE_MISMATCH", "Browser agent device does not match.", 403)
    user = db.get(User, int(identity["user_id"]))
    if user is None or user.deleted_at is not None or not user.is_active or int(user.workspace_id) != int(workspace_id):
        raise APIError("CONTENT_BROWSER_AGENT_USER_DISABLED", "Browser agent user is no longer active.", 403)
    result = reconcile_bridge_agent(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=payload.device_id,
        device_name=payload.device_name,
        agent_version=payload.agent_version,
        public_key=payload.public_key,
        inbox_root=payload.inbox_root,
        local_capacity=payload.local_capacity,
        profile_capacity=payload.profile_capacity,
        update_state=payload.update_state,
        update_error=payload.update_error,
        reported_slots=[item.model_dump() for item in payload.slots],
        host_id=payload.host_id,
        installed_bindings=payload.installed_bindings,
        api_base_url=f"{request.url.scheme}://{request.url.netloc}",
    )
    result["inbox_files"] = bridge_agent_inbox_manifest(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
    )
    db.commit()
    return result


@router.post("/content-factory/bridge/agent/flow/capture")
async def content_factory_bridge_agent_flow_capture(
    workspace_id: int,
    payload: ContentFactoryBridgeFlowCapture,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Accept one purpose-scoped Flow session from the authenticated Agent.

    The session token is immediately forwarded to the loopback-only Flow2API
    service.  It is never returned to the browser or stored in bridge metadata.
    """
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    if str(identity["device_id"]) != str(payload.device_id):
        raise APIError("CONTENT_BROWSER_AGENT_DEVICE_MISMATCH", "Browser agent device does not match.", 403)
    from app.services.flow_browser_onboarding import ingest_flow_browser_capture

    result = await ingest_flow_browser_capture(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=payload.device_id,
        bridge_id=payload.bridge_id,
        capture_id=payload.capture_id,
        session_token=payload.session_token,
        session_tokens=payload.session_tokens,
        profile_id=payload.profile_id,
        fingerprint=payload.fingerprint,
    )
    db.commit()
    return result


@router.post("/content-factory/bridge/agent/jimeng/capture")
async def content_factory_bridge_agent_jimeng_capture(
    workspace_id: int,
    payload: ContentFactoryBridgeJimengCapture,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Accept one encrypted-at-rest JiMeng lab session from its exact Agent slot."""
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    if str(identity["device_id"]) != str(payload.device_id):
        raise APIError("CONTENT_BROWSER_AGENT_DEVICE_MISMATCH", "Browser agent device does not match.", 403)
    from app.services.jimeng_lab import ingest_jimeng_browser_capture

    result = await ingest_jimeng_browser_capture(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=payload.device_id,
        bridge_id=payload.bridge_id,
        capture_id=payload.capture_id,
        session_token=payload.session_token,
        session_tokens=payload.session_tokens,
        session_diagnostics=payload.session_diagnostics,
        session_cookies=payload.session_cookies,
        profile_id=payload.profile_id,
        fingerprint=payload.fingerprint,
    )
    db.commit()
    return result


@router.post("/content-factory/bridge/agent/doubao/capture")
async def content_factory_bridge_agent_doubao_capture(
    workspace_id: int,
    payload: ContentFactoryBridgeDoubaoCapture,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Accept one encrypted-at-rest Doubao lab session from its exact Agent slot."""
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    if str(identity["device_id"]) != str(payload.device_id):
        raise APIError("CONTENT_BROWSER_AGENT_DEVICE_MISMATCH", "Browser agent device does not match.", 403)
    from app.services.doubao_lab import (
        fail_doubao_capability_probe_dispatch,
        ingest_doubao_browser_capture,
        queue_doubao_capability_probe,
    )

    result = await ingest_doubao_browser_capture(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=payload.device_id,
        bridge_id=payload.bridge_id,
        capture_id=payload.capture_id,
        session_token=payload.session_token,
        session_tokens=payload.session_tokens,
        session_diagnostics=payload.session_diagnostics,
        session_cookies=payload.session_cookies,
        profile_id=payload.profile_id,
        fingerprint=payload.fingerprint,
    )
    session, dispatch_required = queue_doubao_capability_probe(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        capture_id=payload.capture_id,
    )
    db.commit()
    if dispatch_required:
        probe_id = str(
            session.get("pool", {}).get("capability", {}).get("probe_id") or ""
        )
        try:
            from app.tasks.doubao_lab_tasks import (
                probe_doubao_provider_account_capability,
            )

            probe_doubao_provider_account_capability.apply_async(
                kwargs={
                    "workspace_id": int(workspace_id),
                    "user_id": int(identity["user_id"]),
                    "bridge_id": payload.bridge_id,
                    "probe_id": probe_id,
                },
                queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
            )
        except Exception as exc:
            fail_doubao_capability_probe_dispatch(
                db,
                workspace_id=int(workspace_id),
                user_id=int(identity["user_id"]),
                capture_id=payload.capture_id,
                probe_id=probe_id,
                error=str(exc),
            )
            db.commit()
    return result


@router.post("/content-factory/bridge/agent/yt-dlp/capture")
def content_factory_bridge_agent_yt_dlp_capture(
    workspace_id: int,
    payload: ContentFactoryBridgeYtDlpCapture,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Accept domain-allowlisted yt-dlp cookies from one exact account Profile."""
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    if str(identity["device_id"]) != str(payload.device_id):
        raise APIError("CONTENT_BROWSER_AGENT_DEVICE_MISMATCH", "Browser agent device does not match.", 403)
    from app.services.yt_dlp_browser_onboarding import ingest_yt_dlp_browser_capture

    result = ingest_yt_dlp_browser_capture(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=payload.device_id,
        bridge_id=payload.bridge_id,
        capture_id=payload.capture_id,
        session_cookies=payload.session_cookies,
        profile_id=payload.profile_id,
    )
    db.commit()
    return result


@router.get("/content-factory/bridge/agent/update")
def update_content_factory_bridge_agent(
    workspace_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    user = db.get(User, int(identity["user_id"]))
    if user is None or user.deleted_at is not None or not user.is_active or int(user.workspace_id) != int(workspace_id):
        raise APIError("CONTENT_BROWSER_AGENT_USER_DISABLED", "Browser agent user is no longer active.", 403)
    device_name = "Windows device"
    bridge_rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(identity["user_id"]),
        )
        .order_by(HermesBrowserBridge.id.desc())
        .all()
    )
    for row in bridge_rows:
        if str(dict(row.meta_json or {}).get("agent_device_id") or "") == str(identity["device_id"]):
            device_name = str(row.device_name or device_name)
            break
    filename, payload = build_bridge_agent_executable(
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        device_id=str(identity["device_id"]),
        device_name=device_name,
        api_base_url=f"{request.url.scheme}://{request.url.netloc}",
    )
    return Response(
        content=payload,
        media_type="application/vnd.microsoft.portable-executable",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/content-factory/bridge/agent/inbox/{relative_path:path}")
def content_factory_bridge_agent_inbox(
    workspace_id: int,
    relative_path: str,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _bridge_agent_identity(authorization, workspace_id=workspace_id, db=db)
    path = ensure_bridge_agent_file_access(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
        relative_path=relative_path,
    )
    return FileResponse(path, filename=path.name)


@router.get("/content-factory/products", response_model=ContentFactoryProductList)
def list_content_factory_products(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    rows = list_content_products(db, workspace_id=workspace_id)
    return {"items": [product_out(db, row) for row in rows]}


@router.post("/content-factory/products", response_model=ContentFactoryProductOut)
def create_content_factory_product(
    workspace_id: int,
    payload: ContentFactoryProductCreate,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = create_content_product(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        brand_name=payload.brand_name,
        product_name=payload.product_name,
        market=payload.market,
        product_brief=payload.product_brief,
        facts_json=payload.facts_json,
    )
    db.commit()
    db.refresh(product)
    return product_out(db, product)


@router.patch("/content-factory/products/{product_id}", response_model=ContentFactoryProductOut)
def update_content_factory_product(
    workspace_id: int,
    product_id: int,
    payload: ContentFactoryProductUpdate,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    product = update_content_product(
        db,
        product=product,
        brand_name=payload.brand_name,
        product_name=payload.product_name,
        market=payload.market,
        product_brief=payload.product_brief,
    )
    db.commit()
    db.refresh(product)
    return product_out(db, product)


@router.delete("/content-factory/products/{product_id}")
def delete_content_factory_product(
    workspace_id: int,
    product_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    delete_content_product(db, product=product)
    db.commit()
    return {"ok": True, "product_id": product_id}


@router.post("/content-factory/products/{product_id}/assets", response_model=list[ContentFactoryProductAssetOut])
async def upload_content_factory_product_assets(
    workspace_id: int,
    product_id: int,
    files: list[UploadFile] = File(...),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    if len(files) > 20:
        raise APIError("CONTENT_TOO_MANY_FILES", "Upload at most 20 files at a time.", 400)
    if any(
        file.size is not None and int(file.size) > MAX_PRODUCT_ASSET_BYTES
        for file in files
    ):
        raise APIError(
            "CONTENT_ASSET_TOO_LARGE",
            "Each product-library file must not exceed 100 MB.",
            413,
        )
    rows = []
    try:
        for file in files:
            rows.append(
                await save_product_asset(
                    db,
                    product=product,
                    user_id=me.id,
                    upload=file,
                    kind="source",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        cleanup_uncommitted_asset_files(rows)
        raise
    for row in rows:
        db.refresh(row)
    return rows


@router.post("/content-factory/products/{product_id}/facts", response_model=ContentFactoryProductOut)
def generate_content_factory_product_facts(
    workspace_id: int,
    product_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    project = create_product_facts_project(db, product=product, user_id=me.id)
    db.flush()
    if project.status not in {"queued", "running"}:
        queue_stage(
            db,
            project=project,
            user_id=me.id,
            instruction="Build or refresh the company product library facts from the uploaded product documents and images. Stop after the product facts stage.",
            target_stage="FACTS",
            continue_workflow=False,
        )
    db.refresh(product)
    return product_out(db, product)


@router.get("/content-factory/products/{product_id}/assets/{asset_id}/content")
def content_factory_product_asset_content(
    workspace_id: int,
    product_id: int,
    asset_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    asset = (
        db.query(HermesContentProductAsset)
        .filter_by(id=asset_id, product_id=product_id, workspace_id=workspace_id)
        .one_or_none()
    )
    if asset is None:
        raise APIError("CONTENT_PRODUCT_ASSET_NOT_FOUND", "Product asset not found.", 404)
    path = _content_asset_path(asset.file_path)
    media_type = asset.mime_type or "application/octet-stream"
    filename = asset.original_name
    db.rollback()
    db.close()
    return FileResponse(path, media_type=media_type, filename=filename)


@router.delete("/content-factory/products/{product_id}/assets/{asset_id}")
def delete_content_factory_product_asset(
    workspace_id: int,
    product_id: int,
    asset_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product = get_content_product(db, workspace_id=workspace_id, product_id=product_id)
    delete_content_product_asset(db, product=product, asset_id=asset_id)
    db.commit()
    return {"ok": True, "product_id": product_id, "asset_id": asset_id}


@router.post(
    "/content-factory/producer/sessions/{session_key}/attachments",
    response_model=list[ContentFactoryProducerAttachmentOut],
)
async def upload_content_factory_producer_attachments(
    workspace_id: int,
    session_key: str,
    files: list[UploadFile] = File(...),
    asset_kind: str = Form(...),
    character_name: str | None = Form(default=None),
    character_description: str | None = Form(default=None),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    if not files or len(files) > 20:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_COUNT_INVALID",
            "Upload between 1 and 20 files at a time.",
            400,
        )
    if str(asset_kind or "").strip().lower() == "reference_video" and len(files) != 1:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_VIDEO_COUNT_INVALID",
            "Upload one benchmark video at a time.",
            400,
        )
    if len(str(character_description or "")) > 2000:
        raise APIError(
            "CONTENT_PRODUCER_CHARACTER_DESCRIPTION_TOO_LONG",
            "Character description must not exceed 2000 characters.",
            400,
        )
    conversation = get_or_create_producer_conversation(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    begin_producer_followup(conversation)
    batch_character_key = f"character_{uuid4().hex[:12]}"
    rows = []
    try:
        for upload in files:
            rows.append(
                await save_producer_attachment(
                    db,
                    conversation=conversation,
                    user_id=me.id,
                    upload=upload,
                    kind=asset_kind,
                    character_key=batch_character_key,
                    character_name=character_name,
                    character_description=character_description,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        intake_root = (STORAGE_ROOT / "producer_intake").resolve()
        for row in rows:
            for raw_path in (row.file_path, row.preview_path):
                if not raw_path:
                    continue
                path = Path(raw_path).resolve(strict=False)
                if intake_root in path.parents:
                    path.unlink(missing_ok=True)
        raise
    for row in rows:
        db.refresh(row)
    for row in rows:
        if row.kind != "reference_video" or row.analysis_status != "processing":
            continue
        try:
            celery_app.send_task(
                "openai_whisper.analyze_content_producer_reference",
                kwargs={"attachment_id": int(row.id)},
                queue=str(WHISPER_TASK_QUEUE),
            )
        except Exception as exc:  # noqa: BLE001
            analysis = dict(row.analysis_json or {})
            analysis.update(
                {
                    "transcript_status": "failed",
                    "transcript_error": type(exc).__name__[:120],
                    "transcript_completed_at": datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                        .isoformat(),
                    "multimodal_status": "failed",
                    "multimodal_error": type(exc).__name__[:120],
                }
            )
            row.analysis_status = "failed"
            row.analysis_json = analysis
            db.add(row)
            db.commit()
    return [producer_attachment_out(row) for row in rows]


@router.post(
    "/content-factory/producer/sessions/{session_key}/reference-links",
    response_model=ContentFactoryProducerAttachmentOut,
)
def add_content_factory_producer_reference_link(
    workspace_id: int,
    session_key: str,
    payload: ContentFactoryProducerReferenceLinkRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Download and analyze one supported public benchmark-video URL."""

    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    conversation = get_or_create_producer_conversation(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    begin_producer_followup(conversation)
    row = stage_producer_reference_link(
        db,
        conversation=conversation,
        user_id=me.id,
        source_url=payload.url,
        context_message=payload.context_message,
    )
    row_meta = dict(row.meta_json or {})
    turn_material = (
        f"{row_meta.get('source_url_sha256') or ''}:"
        f"{str(payload.context_message or '').strip()}"
    )
    row_meta.update({
        "producer_turn_client_id": (
            "benchmark_" + hashlib.sha256(turn_material.encode("utf-8")).hexdigest()[:22]
        ),
        "producer_turn_product_id": int(payload.product_id) if payload.product_id else None,
    })
    row.meta_json = row_meta
    analysis = dict(row.analysis_json or {})
    should_queue = row.analysis_status != "ready" or str(
        analysis.get("multimodal_status") or ""
    ) != "success"
    if should_queue:
        row.analysis_status = "queued"
        analysis.update({
            "download_status": (
                "success" if int(row.size_bytes or 0) > 0 else "queued"
            ),
            "transcript_status": "queued",
            "multimodal_status": "queued",
            "producer_turn_status": "waiting_analysis",
        })
        row.analysis_json = analysis
        db.add(row)
    else:
        analysis["producer_turn_status"] = "queued"
        row.analysis_json = analysis
        db.add(row)
    db.commit()
    db.refresh(row)
    if should_queue:
        try:
            celery_app.send_task(
                "openai_whisper.ingest_content_producer_reference_url",
                kwargs={"attachment_id": int(row.id)},
                queue=str(WHISPER_TASK_QUEUE),
            )
        except Exception as exc:  # noqa: BLE001
            analysis = dict(row.analysis_json or {})
            analysis.update({
                "download_status": "failed",
                "download_error": type(exc).__name__[:120],
            })
            row.analysis_status = "failed"
            row.analysis_json = analysis
            db.add(row)
            db.commit()
    else:
        try:
            celery_app.send_task(
                "hermes_content_factory.continue_producer_benchmark_turn",
                kwargs={"attachment_id": int(row.id)},
                queue=str(settings.HERMES_AGENT_TASK_QUEUE),
            )
        except Exception as exc:  # noqa: BLE001
            analysis = dict(row.analysis_json or {})
            analysis.update({
                "producer_turn_status": "failed",
                "producer_turn_error": type(exc).__name__[:120],
            })
            row.analysis_json = analysis
            db.add(row)
            db.commit()
    return producer_attachment_out(row)


@router.delete(
    "/content-factory/producer/sessions/{session_key}/attachments/{attachment_key}",
)
def remove_content_factory_producer_attachment(
    workspace_id: int,
    session_key: str,
    attachment_key: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    conversation, _rows = producer_attachments(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    paths = delete_producer_attachment(
        db,
        conversation=conversation,
        attachment_key=attachment_key,
    )
    db.commit()
    intake_root = (STORAGE_ROOT / "producer_intake").resolve()
    for raw_path in paths:
        path = raw_path.resolve(strict=False)
        if intake_root in path.parents:
            path.unlink(missing_ok=True)
    return {"ok": True, "attachment_key": attachment_key}


@router.post(
    "/content-factory/producer/turn",
    response_model=ContentFactoryProducerTurnResponse,
)
async def content_factory_producer_turn(
    workspace_id: int,
    payload: ContentFactoryProducerTurnRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Convert one plain-language turn into a reviewable project proposal.

    The producer is API-only and cannot create a project or authorize media.
    Project creation remains a separate explicit confirmation transaction.
    """

    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    conversation, decision = await run_producer_turn(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        message=payload.message,
        session_key=payload.session_key,
        product_id=payload.product_id,
        product_selection_explicit="product_id" in payload.model_fields_set,
        client_turn_id=payload.client_turn_id,
    )
    meta = dict(conversation.meta_json or {})
    current_script = authoritative_producer_script(
        db,
        conversation=conversation,
    )
    return ContentFactoryProducerTurnResponse(
        session_key=str(meta.get("session_key") or payload.session_key or ""),
        status=decision.status,
        assistant_message=decision.assistant_message,
        missing_information=list(decision.missing_information),
        proposal=(
            decision.proposal.model_dump(mode="json")
            if decision.proposal is not None
            else None
        ),
        proposal_sha256=(
            str(meta.get("proposal_sha256"))
            if meta.get("proposal_sha256")
            else None
        ),
        selected_product_id=(
            int(meta["selected_product_id"])
            if meta.get("selected_product_id") is not None
            else None
        ),
        authoritative_script_message_id=(
            int(meta["authoritative_script_message_id"])
            if meta.get("authoritative_script_message_id") is not None
            else None
        ),
        authoritative_script=(
            str(current_script[1]) if current_script is not None else None
        ),
        authoritative_script_version=(
            int(meta.get("authoritative_script_current_version") or 1)
            if current_script is not None
            else None
        ),
        revised_authoritative_script=decision.revised_authoritative_script,
        intent_spec=(
            dict(meta["intent_spec"])
            if isinstance(meta.get("intent_spec"), dict)
            else None
        ),
        pending_decision_id=(
            str(meta.get("pending_decision_id"))
            if meta.get("pending_decision_id")
            else None
        ),
    )


@router.get(
    "/content-factory/producer/sessions/{session_key}",
    response_model=ContentFactoryProducerSessionOut,
)
def content_factory_producer_session(
    workspace_id: int,
    session_key: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    conversation, messages = producer_session(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    meta = dict(conversation.meta_json or {})
    _conversation, attachments = producer_attachments(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    current_script = authoritative_producer_script(
        db,
        conversation=conversation,
    )
    return ContentFactoryProducerSessionOut(
        session_key=str(meta.get("session_key") or session_key),
        status=str(meta.get("status") or "needs_input"),
        draft_message=(
            str(meta.get("draft_message"))[:50000]
            if meta.get("draft_message")
            else None
        ),
        source_context=(
            {
                "type": str(meta.get("source_type") or ""),
                "analysis_id": int(meta["source_analysis_id"]),
                "video_id": str(meta.get("source_video_id") or ""),
                "title": str(meta.get("source_title") or ""),
                "handoff_version": str(meta.get("handoff_version") or ""),
            }
            if meta.get("source_type") == "tiktok_shop_video_analysis"
            and meta.get("source_analysis_id") is not None
            else None
        ),
        messages=[
            {
                "role": row.role,
                "content": str(row.content_text or ""),
                "client_turn_id": row.run_id,
                "created_at": row.created_at,
            }
            for row in messages
            if row.role in {"user", "assistant"}
        ],
        attachments=[producer_attachment_out(row) for row in attachments],
        proposal=(
            dict(meta["proposal"])
            if isinstance(meta.get("proposal"), dict)
            else None
        ),
        proposal_sha256=(
            str(meta.get("proposal_sha256"))
            if meta.get("proposal_sha256")
            else None
        ),
        selected_product_id=(
            int(meta["selected_product_id"])
            if meta.get("selected_product_id") is not None
            else None
        ),
        authoritative_script_message_id=(
            int(meta["authoritative_script_message_id"])
            if meta.get("authoritative_script_message_id") is not None
            else None
        ),
        authoritative_script=(
            str(current_script[1]) if current_script is not None else None
        ),
        authoritative_script_version=(
            int(meta.get("authoritative_script_current_version") or 1)
            if current_script is not None
            else None
        ),
        intent_spec=(
            dict(meta["intent_spec"])
            if isinstance(meta.get("intent_spec"), dict)
            else None
        ),
        pending_decision_id=(
            str(meta.get("pending_decision_id"))
            if meta.get("pending_decision_id")
            else None
        ),
        created_project_key=(
            str(meta.get("created_project_key"))
            if meta.get("created_project_key")
            else None
        ),
    )


@router.post(
    "/content-factory/producer/sessions/{session_key}/confirm",
    response_model=ContentFactoryProjectOut,
)
def confirm_content_factory_producer_project(
    workspace_id: int,
    session_key: str,
    payload: ContentFactoryProducerConfirmRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Atomically create exactly one project from the reviewed proposal."""

    ensure_user_can_use_task(
        db,
        workspace_id=workspace_id,
        me=me,
        task_type="general",
    )
    conversation, proposal, product, _user_requirements = confirmed_project_parameters(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    meta = dict(conversation.meta_json or {})
    authoritative_script = authoritative_producer_script(
        db,
        conversation=conversation,
    )
    _conversation, staged_attachments = producer_attachments(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        session_key=session_key,
    )
    has_reference_video = any(
        row.kind == "reference_video" for row in staged_attachments
    )
    if str(meta.get("proposal_sha256") or "") != payload.proposal_sha256:
        raise APIError(
            "CONTENT_PRODUCER_CONFIRMATION_STALE",
            "The proposal changed after it was displayed. Review the latest proposal before starting.",
            409,
        )
    expected_pending_decision_id = str(
        meta.get("pending_decision_id") or ""
    ).strip()
    if (
        expected_pending_decision_id
        and str(payload.pending_decision_id or "").strip()
        != expected_pending_decision_id
    ):
        raise APIError(
            "CONTENT_PRODUCER_CONFIRMATION_DECISION_STALE",
            "The confirmation no longer matches the displayed production decision. Review the latest summary before starting.",
            409,
        )
    existing_project_id = meta.get("created_project_id")
    if existing_project_id is not None:
        existing = (
            db.query(HermesContentFactoryProject)
            .filter(
                HermesContentFactoryProject.id == int(existing_project_id),
                HermesContentFactoryProject.workspace_id == int(workspace_id),
                HermesContentFactoryProject.user_id == int(me.id),
            )
            .one_or_none()
        )
        if existing is None:
            raise APIError(
                "CONTENT_PRODUCER_CREATED_PROJECT_MISSING",
                "The project created by this conversation is no longer available.",
                409,
            )
        return project_out(db, existing)

    constraints = [
        f"Visual style: {proposal.visual_style}",
        f"Pacing: {proposal.pacing}",
        (
            "Selected-product role: context only. The selected product may "
            "inform audience and category understanding, but must not create "
            "an in-video product, brand, package, offer, CTA, reason-to-choose, "
            "or conversion requirement."
            if proposal.product_use_mode == "context_only"
            else (
                "Selected-product role: required in the deliverable."
                if proposal.product_use_mode == "required"
                else "Selected-product role: none."
            )
        ),
        (
            "Spoken-copy density: "
            f"{proposal.spoken_density}. {proposal.spoken_density_reason}"
        ).strip(),
        f"Audio identity: {proposal.audio_direction}",
    ]
    if proposal.confirmed_offer:
        constraints.append(
            "Conversion direction: use only this confirmed current offer: "
            f"{proposal.confirmed_offer}. Historical or canceled offers are not production copy."
        )
    elif proposal.conversion_direction and not _PRODUCER_COMMERCIAL_HISTORY_PATTERN.search(
        proposal.conversion_direction
    ):
        constraints.append(
            f"Conversion direction: {proposal.conversion_direction}"
        )
    constraints.extend(
        item
        for item in proposal.creative_constraints
        if not _PRODUCER_COMMERCIAL_HISTORY_PATTERN.search(str(item or ""))
    )
    requirements = (
        "The user explicitly confirmed the reviewed producer proposal. "
        "Historical chat messages are audit context only and are intentionally "
        "excluded from the production brief. Use the structured proposal, "
        "the current versioned script, stable product facts and the single "
        "confirmed current offer only."
    )
    # The shared product library owns durable attributes only.  Project price,
    # offer, shipping, duration and creative choices come from this confirmed
    # conversation and must never leak from a legacy free-form product brief.
    intent_spec = (
        dict(meta["intent_spec"])
        if isinstance(meta.get("intent_spec"), dict)
        else {}
    )
    try:
        intent_manifest = intent_manifest_from_spec(intent_spec)
    except (TypeError, ValueError) as exc:
        raise APIError(
            "CONTENT_INTENT_MANIFEST_REQUIRED",
            "The producer proposal has no valid signed creative intent "
            "manifest. Continue the assistant conversation so it can rebuild "
            "the requirement evidence before creating the project.",
            409,
        ) from exc
    if not intent_manifest.manifest_sha256:
        raise APIError(
            "CONTENT_INTENT_MANIFEST_UNSIGNED",
            "The producer must sign the creative intent manifest before the "
            "project can be created.",
            409,
        )
    try:
        creative_copy_contract = compile_confirmed_creative_copy_contract(
            intent_spec=intent_spec,
            authoritative_script=authoritative_script,
            authoritative_script_version=int(
                meta.get("authoritative_script_current_version") or 1
            ),
        )
    except (TypeError, ValueError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_COPY_AUTHORITY_INVALID",
            "The reviewed copy authority is internally inconsistent. Continue "
            "the producer conversation so the AI can reconcile the latest "
            "script and deliverable intent before confirmation.",
            409,
            {"validation_error": str(exc)[:1000]},
        ) from exc
    model_record = next(
        (
            item
            for item in video_model_routing_catalog(db)
            if item.get("id") == proposal.video_model
        ),
        None,
    )
    if not model_record or not bool(model_record.get("available")):
        raise APIError(
            "CONTENT_VIDEO_MODEL_UNAVAILABLE",
            "The confirmed video model currently has no enabled provider route. "
            "Return to the producer assistant and choose another model.",
            409,
        )
    reference_limit = max(
        0,
        int(model_record.get("reference_image_limit") or 0),
    )
    try:
        project = create_content_project(
            db,
            workspace_id=workspace_id,
            user_id=me.id,
            title=proposal.title,
            content_objective=proposal.content_objective,
            target_audience=proposal.target_audience,
            content_mode=proposal.content_mode,
            product_use_mode=proposal.product_use_mode,
            product_required=proposal.product_use_mode == "required",
            product_id=int(product.id) if product is not None else None,
            product_name=product.product_name if product is not None else "",
            brand_name=product.brand_name if product is not None else "",
            market=product.market if product is not None else "US",
            product_brief=requirements,
            video_count=proposal.video_count,
            max_api_video_variants_in_flight=min(2, proposal.video_count),
            video_duration_min_seconds=proposal.video_duration_min_seconds,
            video_duration_max_seconds=proposal.video_duration_max_seconds,
            video_model=proposal.video_model,
            video_duration_strategy=proposal.video_duration_strategy,
            preferred_segment_durations_seconds=(
                proposal.preferred_segment_durations_seconds
            ),
            video_resolution=proposal.video_resolution,
            video_aspect_ratio=proposal.video_aspect_ratio,
            video_language=proposal.video_language,
            video_reference_limit=reference_limit,
            video_frame_mode="reference",
            allow_reference_video=(
                has_reference_video
                and proposal.video_generation_mode == "video_to_video"
            ),
            video_generation_mode=proposal.video_generation_mode,
            visual_reference_generation_mode=(
                proposal.visual_reference_generation_mode
            ),
            visual_image_model_chain=["gpt-image-2", "nano_banana_pro"],
            confirmed_promotions=proposal.confirmed_offer,
            # A non-transactional brand-series conversion direction (follow,
            # share, remember the brand) is not permission to invent an offer
            # or purchase CTA. Promotional authority exists only when the user
            # also confirmed an offer.
            allow_promotional_cta=bool(
                proposal.confirmed_offer and proposal.conversion_direction
            ),
            publishing_profile={"platform": proposal.platform},
            creative_copy_contract=creative_copy_contract,
            producer_intent_spec=(intent_spec or None),
            creative_cast_policy={"instructions": [proposal.audio_direction]},
            director_creative_constraints=constraints,
            content_director_mode="enforce",
            preferred_browser_device_id=None,
            auto_run=True,
        )
    except ValueError as exc:
        # Project creation compiles provider timing, copy authority and the
        # universal Director brief before any project row is flushed.  A
        # compiler/preflight mismatch is a reviewable handoff conflict, never
        # an unhandled server error.  Roll back any pending ORM state and keep
        # the Producer session confirmable after an AI reconciliation turn.
        db.rollback()
        raise APIError(
            "CONTENT_PRODUCER_HANDOFF_PREFLIGHT_FAILED",
            "The confirmed brief could not be compiled consistently. Continue "
            "the producer conversation so the AI can reconcile the latest "
            "requirements, copy authority and provider timing.",
            409,
            {"validation_error": str(exc)[:1000]},
        ) from exc
    project_config = dict(project.config_json or {})
    project_config["producer_intake"] = {
        "session_key": session_key,
        "prompt_version": PRODUCER_PROMPT_VERSION,
        "proposal_sha256": str(meta.get("proposal_sha256") or ""),
        "explicit_confirmation": True,
        "authoritative_script_message_id": (
            int(authoritative_script[0])
            if authoritative_script is not None
            else None
        ),
        "authoritative_script_version": int(
            meta.get("authoritative_script_current_version") or 1
        ) if authoritative_script is not None else None,
        "promotion_evidence_quote": proposal.promotion_evidence_quote,
        "confirmed_offer": proposal.confirmed_offer,
        "video_model": proposal.video_model,
        "video_duration_strategy": proposal.video_duration_strategy,
        "spoken_density": proposal.spoken_density,
        "spoken_density_reason": proposal.spoken_density_reason,
        "product_use_mode": proposal.product_use_mode,
        "source_text_assets": list(meta.get("source_text_assets") or []),
        "intent_spec": intent_spec or None,
        "pending_decision_id": expected_pending_decision_id or None,
        "followup_parent_project_id": (
            int(meta["followup_parent_project_id"])
            if meta.get("followup_parent_project_id") is not None
            else None
        ),
        "followup_parent_project_key": (
            str(meta.get("followup_parent_project_key") or "") or None
        ),
    }
    if intent_spec:
        project_config["producer_intent_spec"] = intent_spec
    project.config_json = project_config
    transferred_assets = copy_producer_attachments_to_project(
        db,
        conversation=conversation,
        project=project,
        user_id=me.id,
        storage_root=STORAGE_ROOT,
        browser_inbox=BROWSER_INBOX,
    )
    reference_assets = [
        row for row in transferred_assets if row.kind == "reference_video"
    ]
    if reference_assets:
        state = dict(project.state_json or {})
        state["benchmark_video_analysis"] = {
            "status": "queued",
            "source_asset_id": int(reference_assets[0].id),
            "queued_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "source": "producer_intake_confirmation",
        }
        project.state_json = state
    meta.update(
        {
            "status": "created",
            "created_project_id": int(project.id),
            "created_project_key": project.project_key,
            "project_created_at": datetime.now(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(),
        }
    )
    conversation.meta_json = meta
    db.add(conversation)
    try:
        db.commit()
    except Exception:
        db.rollback()
        cleanup_uncommitted_asset_files(transferred_assets)
        raise
    db.refresh(project)
    if reference_assets:
        from app.tasks.hermes_agent.content_factory_tasks import analyze_content_factory_reference_video

        analyze_content_factory_reference_video.apply_async(
            kwargs={"asset_id": int(reference_assets[0].id)},
            countdown=2,
            queue="gmv.tasks.hermes_agent",
            priority=8,
        )
        return project_out(db, project)
    try:
        queue_stage(
            db,
            project=project,
            user_id=me.id,
            instruction=(
                "Continue unattended from the universal Director using the "
                "AI producer's confirmed project intent, saved product truth, "
                "and configured quality boundaries."
            ),
            target_stage=project.current_stage,
            continue_workflow=True,
        )
    except APIError as exc:
        if exc.code not in {
            "CONTENT_BROWSER_BRIDGE_REQUIRED",
            "CONTENT_BROWSER_BRIDGE_OFFLINE",
            "CONTENT_BROWSER_CAPACITY_FULL",
        }:
            raise
        db.expire_all()
        project = get_content_project(
            db,
            workspace_id,
            me.id,
            project.project_key,
        )
    return project_out(db, project)


@router.get("/content-factory/projects", response_model=ContentFactoryProjectList)
def list_content_factory_projects(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    rows = (
        visible_project_query(db, workspace_id, me.id)
        .order_by(
            HermesContentFactoryProject.updated_at.desc(),
            HermesContentFactoryProject.id.desc(),
        )
        .limit(120)
        .all()
    )
    rows = [row for row in rows if not is_product_facts_project(row)][:100]
    return {"items": [project_out(db, row) for row in rows]}


@router.get("/content-factory/admin/projects", response_model=ContentFactoryAdminProjectList)
def list_admin_content_factory_projects(
    workspace_id: int,
    creator_user_id: int | None = Query(default=None, ge=1),
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    query = db.query(HermesContentFactoryProject).filter(
        HermesContentFactoryProject.workspace_id == int(workspace_id),
        HermesContentFactoryProject.status != "deleted",
    )
    if creator_user_id is not None:
        query = query.filter(HermesContentFactoryProject.user_id == int(creator_user_id))
    rows = (
        query.order_by(
            HermesContentFactoryProject.updated_at.desc(),
            HermesContentFactoryProject.id.desc(),
        )
        .limit(200)
        .all()
    )
    rows = [row for row in rows if not is_product_facts_project(row)]
    user_ids = {int(row.user_id) for row in rows if row.user_id is not None}
    users = {
        int(user.id): user
        for user in db.query(User).filter(
            User.workspace_id == int(workspace_id),
            User.id.in_(user_ids),
        ).all()
    } if user_ids else {}
    items = []
    for row in rows:
        user = users.get(int(row.user_id)) if row.user_id is not None else None
        label = None
        usercode = None
        if user is not None:
            label = user.display_name or user.username or user.email
            usercode = user.usercode
        items.append({
            "project_key": row.project_key,
            "workspace_id": row.workspace_id,
            "user_id": row.user_id,
            "created_by_label": label,
            "created_by_usercode": usercode,
            "title": row.title,
            "product_name": row.product_name,
            "market": row.market,
            "status": row.status,
            "current_stage": row.current_stage,
            "last_error": row.last_error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deliverables": project_deliverables(db, row),
        })
    return {"items": items}


@router.get(
    "/content-factory/admin/projects/{project_key}",
    response_model=ContentFactoryProjectOut,
)
def admin_content_factory_project_detail(
    workspace_id: int,
    project_key: str,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    project = _admin_content_project(db, workspace_id=workspace_id, project_key=project_key)
    return project_out(db, project)


@router.get("/content-factory/admin/projects/{project_key}/deliverables.zip")
def admin_content_factory_project_deliverables_zip(
    workspace_id: int,
    project_key: str,
    kind: str = Query(default="all", pattern="^(all|videos|guides)$"),
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    project = _admin_content_project(db, workspace_id=workspace_id, project_key=project_key)
    archive_path = build_project_deliverables_zip(db, project, kind=kind)
    suffix = str(kind or "all").lower()
    filename = f"{project.title or project.project_key}-{suffix}-deliverables.zip"
    return FileResponse(archive_path, media_type="application/zip", filename=filename)


@router.get(
    "/content-factory/admin/projects/{project_key}/assets/{asset_id}/content"
)
def admin_content_factory_asset_content(
    workspace_id: int,
    project_key: str,
    asset_id: int,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    project = _admin_content_project(db, workspace_id=workspace_id, project_key=project_key)
    asset = (
        db.query(HermesContentFactoryAsset)
        .filter(
            HermesContentFactoryAsset.id == int(asset_id),
            HermesContentFactoryAsset.project_id == int(project.id),
            HermesContentFactoryAsset.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if asset is None:
        raise APIError("CONTENT_ASSET_NOT_FOUND", "Content factory asset not found.", 404)
    path = _content_asset_path(asset.file_path)
    media_type = asset.mime_type or "application/octet-stream"
    filename = asset.original_name
    db.rollback()
    db.close()
    return FileResponse(path, media_type=media_type, filename=filename)




@router.patch("/content-factory/projects/{project_key}", response_model=ContentFactoryProjectOut)
def update_content_factory_project(
    workspace_id: int,
    project_key: str,
    payload: ContentFactoryProjectUpdate,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    update_content_project(db, project, values=payload.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(project)
    return project_out(db, project)


@router.post("/content-factory/projects/{project_key}/restart", response_model=ContentFactoryProjectOut)
def restart_content_factory_project(
    workspace_id: int,
    project_key: str,
    payload: ContentFactoryProjectRestart,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    restart_content_project(
        db,
        project,
        stage=payload.stage,
        instruction=payload.instruction,
        allowed_audio_modes=payload.allowed_audio_modes,
        replace_completed=payload.replace_completed,
    )
    db.commit()
    db.refresh(project)
    if payload.auto_run:
        queue_stage(
            db,
            project=project,
            user_id=me.id,
            instruction=payload.instruction or f"Restart from {payload.stage} using the saved project settings.",
            target_stage=payload.stage,
            continue_workflow=True,
        )
        db.refresh(project)
    return project_out(db, project)


@router.delete("/content-factory/projects/{project_key}")
def delete_content_factory_project(
    workspace_id: int,
    project_key: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    project_id = int(project.id)
    delete_content_project(db, project)
    db.commit()
    cleanup_pending = False
    try:
        tombstone = db.get(HermesContentFactoryProject, project_id)
        if tombstone is not None:
            cleanup_pending = not finalize_deleted_project(db, tombstone)
        db.commit()
    except Exception as exc:
        db.rollback()
        cleanup_pending = True
        tombstone = db.get(HermesContentFactoryProject, project_id)
        if tombstone is not None and str(tombstone.status or "").lower() == "deleted":
            state = dict(tombstone.state_json or {})
            state["deletion_cleanup_pending"] = True
            state["deletion_cleanup_error_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            state["deletion_cleanup_error_type"] = type(exc).__name__[:120]
            tombstone.state_json = state
            tombstone.last_error = "Project is hidden; background storage cleanup will retry automatically."
            db.add(tombstone)
            db.commit()
    return {
        "ok": True,
        "project_key": project_key,
        "cleanup_pending": cleanup_pending,
    }


@router.post("/content-factory/projects/{project_key}/pause", response_model=ContentFactoryProjectOut)
def pause_content_factory_project(
    workspace_id: int,
    project_key: str,
    payload: ContentFactoryPauseRequest | None = None,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    pause_content_project(db, project, note=(payload.note if payload else None))
    db.commit()
    db.refresh(project)
    return project_out(db, project)


@router.post(
    "/content-factory/projects/{project_key}/rollout-gate",
    response_model=ContentFactoryProjectOut,
)
def release_content_factory_rollout_batch(
    workspace_id: int,
    project_key: str,
    payload: ContentFactoryRolloutGateRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    configure_variant_rollout_gate(
        db,
        project,
        authorized_variant_indices=payload.authorized_variant_indices,
        batch_id=payload.batch_id,
        pause_when_complete=payload.pause_when_complete,
        released_by_user_id=me.id,
    )
    db.commit()
    db.refresh(project)
    return project_out(db, project)


@router.post("/content-factory/projects/{project_key}/resume", response_model=ContentFactoryProjectOut)
def resume_content_factory_project(
    workspace_id: int,
    project_key: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    # A final-video quality pause owns immutable multimodal evidence and the
    # exact failed segment ids.  Sending it through the generic operator
    # resume path clears those pause markers before the guardian can create
    # bounded replacement segments, leaving the restored waiter to terminate
    # on the same failed provider rows.  Hand this pause directly to the
    # evidence-scoped recovery routine instead.
    project_state = dict(project.state_json or {})
    if (
        str(project.status or "").strip().lower() == "paused"
        and str(project_state.get("pause_reason_code") or "").strip().lower()
        == "final_video_quality_gate"
        and project_state.get("final_video_quality_failure")
    ):
        from app.tasks.hermes_agent.content_factory_tasks import (
            _resume_final_video_quality_pause,
        )

        _retry_ids, recovery = _resume_final_video_quality_pause(
            db,
            project,
            now=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        if not bool(recovery.get("recovered")):
            raise APIError(
                "CONTENT_FINAL_QUALITY_RECOVERY_NOT_READY",
                "The final-video quality repair could not be resumed yet.",
                409,
                data=recovery,
            )
        db.commit()
        db.refresh(project)
        return project_out(db, project)
    paused_stage = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == project.current_stage,
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .first()
    )
    paused_input = dict(paused_stage.input_json or {}) if paused_stage is not None else {}
    resume_force_browser = bool(
        paused_input.get("api_fallback_to_browser")
        or paused_input.get("visual_api_force_browser_fallback")
        or str(paused_input.get("execution_backend") or "").strip().lower() == "browser"
    )
    resume_content_project(db, project)
    db.commit()
    db.refresh(project)
    # A failed API delivery can have fully downloaded, paid references while
    # its successor row merely records browser fallback intent. Once resume
    # recovers that checkpoint, the API/local post-processing path is the only
    # idempotent continuation; forcing a browser here would discard it.
    resume_force_browser = resume_stage_force_browser(
        paused_input,
        dict(project.state_json or {}),
    )
    resumed_task_id = resume_waiting_project_production(db, project)
    if resumed_task_id:
        db.commit()
        db.refresh(project)
        return project_out(db, project)
    resumed_state = dict(project.state_json or {})
    if (
        resumed_state.get("ai_video_pending_task_ids")
        or resumed_state.get("ai_video_resume_failed_task_ids")
    ):
        from app.tasks.hermes_agent.content_factory_tasks import wait_for_content_factory_videos

        wait_task = wait_for_content_factory_videos.apply_async(
            kwargs={"project_id": int(project.id)},
            countdown=5,
            queue="gmv.tasks.hermes_agent",
            priority=9,
        )
        resumed_state["ai_video_wait_task_id"] = wait_task.id
        resumed_state["ai_video_wait_reason"] = (
            "manual resume restored the project-global video waiter"
        )
        project.state_json = resumed_state
        db.add(project)
        db.commit()
        db.refresh(project)
    elif project.current_stage not in {"COMPLETE", *WAITING_STAGES} and project.status == "ready":
        # Queue immediately so "resume" really continues from the breakpoint.
        queue_stage(
            db,
            project=project,
            user_id=me.id,
            instruction="Resume from the manually paused breakpoint.",
            continue_workflow=True,
            queue_priority=9,
            force_browser=resume_force_browser,
        )
        db.refresh(project)
        return project_out(db, project)
    return project_out(db, project)


@router.get("/content-factory/projects/{project_key}", response_model=ContentFactoryProjectOut)
def content_factory_project_detail(
    workspace_id: int,
    project_key: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    return project_out(db, project)


@router.get("/content-factory/projects/{project_key}/deliverables.zip")
def content_factory_project_deliverables_zip(
    workspace_id: int,
    project_key: str,
    kind: str = Query(default="all", pattern="^(all|videos|guides)$"),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    archive_path = build_project_deliverables_zip(db, project, kind=kind)
    suffix = str(kind or "all").lower()
    filename = f"{project.title or project.project_key}-{suffix}-deliverables.zip"
    return FileResponse(archive_path, media_type="application/zip", filename=filename)


@router.post("/content-factory/projects/{project_key}/assets", response_model=list[ContentFactoryAssetOut])
async def upload_content_factory_assets(
    workspace_id: int,
    project_key: str,
    files: list[UploadFile] = File(...),
    asset_kind: str = Form(default="source"),
    character_key: str | None = Form(default=None),
    character_name: str | None = Form(default=None),
    character_description: str | None = Form(default=None),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    if len(files) > 20:
        raise APIError("CONTENT_TOO_MANY_FILES", "Upload at most 20 files at a time.", 400)
    if asset_kind == "reference_video":
        kind = "reference_video"
    elif asset_kind == "character_reference":
        kind = "character_reference"
    else:
        kind = "source"
    if kind == "reference_video":
        config = dict(project.config_json or {})
        if not bool(config.get("allow_reference_video", False)):
            raise APIError("CONTENT_REFERENCE_VIDEO_DISABLED", "Reference video is not enabled for this project.", 400)
        if len(files) > 1:
            raise APIError("CONTENT_TOO_MANY_REFERENCE_VIDEOS", "Upload one reference video per project.", 400)
        for file in files:
            mime = str(file.content_type or "").lower()
            if not (mime.startswith("video/") or Path(file.filename or "").suffix.lower() in {".mp4", ".mov", ".webm"}):
                raise APIError("CONTENT_REFERENCE_VIDEO_INVALID", "Reference video must be MP4, MOV, or WebM.", 400)
            if file.size is not None and int(file.size) > MAX_REFERENCE_VIDEO_BYTES:
                raise APIError("CONTENT_REFERENCE_VIDEO_TOO_LARGE", "Reference video must not exceed 200 MB.", 400)
    if kind == "character_reference":
        existing_character_count = db.query(HermesContentFactoryAsset.id).filter(
            HermesContentFactoryAsset.project_id == project.id,
            HermesContentFactoryAsset.kind == "character_reference",
        ).count()
        if existing_character_count + len(files) > 16:
            raise APIError(
                "CONTENT_TOO_MANY_CHARACTER_REFERENCES",
                "A project can contain at most 16 character reference images.",
                400,
            )
        for file in files:
            mime = str(file.content_type or "").lower()
            if not (mime.startswith("image/") or Path(file.filename or "").suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}):
                raise APIError("CONTENT_CHARACTER_REFERENCE_INVALID", "Character reference must be JPG, PNG, or WebP.", 400)
            if file.size is not None and int(file.size) > MAX_CHARACTER_REFERENCE_BYTES:
                raise APIError("CONTENT_CHARACTER_REFERENCE_TOO_LARGE", "Each character reference image must not exceed 15 MB.", 400)
    asset_meta: dict[str, str] = {}
    if kind == "character_reference":
        raw_key = str(character_key or "").strip()
        normalized_key = "".join(
            char if (char.isalnum() or char in {"-", "_"}) else "_"
            for char in raw_key
        ).strip("-_")[:64]
        normalized_key = normalized_key or f"character_{uuid4().hex[:12]}"
        display_name = str(character_name or "").strip()[:120] or "Character"
        description = str(character_description or "").strip()
        if len(description) > 2000:
            raise APIError(
                "CONTENT_CHARACTER_DESCRIPTION_TOO_LONG",
                "Character description must not exceed 2000 characters.",
                400,
            )
        asset_meta = {
            "character_key": normalized_key,
            "character_name": display_name,
            "character_description": description,
        }
    rows = []
    try:
        for file in files:
            rows.append(
                await save_asset(
                    db,
                    project=project,
                    user_id=me.id,
                    upload=file,
                    kind=kind,
                    extra_meta=asset_meta,
                )
            )
        # Persist uploaded files before any analyzer task or follow-up project
        # update is allowed to observe their IDs.
        db.commit()
    except Exception:
        db.rollback()
        cleanup_uncommitted_asset_files(rows)
        raise
    for row in rows:
        db.refresh(row)
    if kind == "reference_video":
        from app.tasks.hermes_agent.content_factory_tasks import analyze_content_factory_reference_video

        state = dict(project.state_json or {})
        state["benchmark_video_analysis"] = {
            "status": "queued",
            "source_asset_id": int(rows[0].id) if rows else None,
            "queued_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
        project.state_json = state
        db.commit()
        for row in rows:
            analyze_content_factory_reference_video.apply_async(
                kwargs={"asset_id": int(row.id)},
                countdown=2,
                queue="gmv.tasks.hermes_agent",
                priority=8,
            )
    config = dict(project.config_json or {})
    has_source = db.query(HermesContentFactoryAsset.id).filter(
        HermesContentFactoryAsset.project_id == project.id,
        HermesContentFactoryAsset.kind == "source",
    ).first() is not None
    has_reference_video = db.query(HermesContentFactoryAsset.id).filter(
        HermesContentFactoryAsset.project_id == project.id,
        HermesContentFactoryAsset.kind == "reference_video",
    ).first() is not None
    benchmark_ready = dict(dict(project.state_json or {}).get("benchmark_video_analysis") or {}).get("status") == "success"
    inputs_ready = has_source and (not bool(config.get("allow_reference_video", False)) or (has_reference_video and benchmark_ready))
    if project.status == "draft" and bool(config.get("auto_start_on_upload", False)) and inputs_ready:
        target_stage = (
            project.current_stage
            if project.current_stage
            in {"FACTS", "SERIES_DIRECTOR", "DIRECTOR"}
            else "DIRECTOR"
        )
        instruction = (
            "Build the isolated product knowledge base and continue through the universal Director."
            if target_stage == "FACTS"
            else "Continue through the universal Director using the isolated project facts."
        )
        queue_stage(
            db, project=project, user_id=me.id,
            instruction=instruction,
            target_stage=target_stage, continue_workflow=True,
        )
    else:
        db.commit()
    return rows


@router.get("/content-factory/projects/{project_key}/assets/{asset_id}/content")
def content_factory_asset_content(
    workspace_id: int,
    project_key: str,
    asset_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    asset = db.query(HermesContentFactoryAsset).filter_by(id=asset_id, project_id=project.id).one_or_none()
    if asset is None:
        raise APIError("CONTENT_ASSET_NOT_FOUND", "Content factory asset not found.", 404)
    path = _content_asset_path(asset.file_path)
    media_type = asset.mime_type or "application/octet-stream"
    filename = asset.original_name
    db.rollback()
    db.close()
    return FileResponse(path, media_type=media_type, filename=filename)


@router.post("/content-factory/projects/{project_key}/run", response_model=ContentFactoryStageOut)
def run_content_factory_project(
    workspace_id: int,
    project_key: str,
    payload: ContentFactoryCommand,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    project = get_content_project(db, workspace_id, me.id, project_key)
    return queue_stage(
        db, project=project, user_id=me.id, instruction=payload.instruction,
        target_stage=payload.stage, continue_workflow=payload.run_mode == "continue",
    )
