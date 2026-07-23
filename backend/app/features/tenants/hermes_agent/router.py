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
    register_browser_bridge,
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
)

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
    ContentFactoryBridgeRegister,
    ContentFactoryBridgeDeviceAction,
    ContentFactoryBridgeAgentHeartbeat,
    ContentFactoryBridgeStatus,
    ContentFactoryCommand,
    ContentFactoryPauseRequest,
    ContentFactoryRolloutGateRequest,
    ContentFactoryProjectCreate,
    ContentFactoryProjectRestart,
    ContentFactoryProjectUpdate,
    ContentFactoryProjectList,
    ContentFactoryProjectOut,
    ContentFactoryProducerConfirmRequest,
    ContentFactoryProducerAttachmentOut,
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


@router.post("/content-factory/bridge/register", response_model=ContentFactoryBridgeStatus)
async def register_content_factory_bridge(
    workspace_id: int,
    payload: ContentFactoryBridgeRegister,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    register_browser_bridge(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        device_id=payload.device_id,
        device_name=payload.device_name,
        cdp_url=payload.cdp_url,
        inbox_root=payload.inbox_root,
        outbox_root=payload.outbox_root,
        browser=payload.browser,
        load_json=payload.load_json,
    )
    db.commit()
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
        reported_slots=[item.model_dump() for item in payload.slots],
    )
    result["inbox_files"] = bridge_agent_inbox_manifest(
        db,
        workspace_id=int(workspace_id),
        user_id=int(identity["user_id"]),
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
    if dict(conversation.meta_json or {}).get("created_project_id"):
        raise APIError(
            "CONTENT_PRODUCER_SESSION_ALREADY_CREATED",
            "This conversation already created a project. Start a new conversation to upload different references.",
            409,
        )
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
                }
            )
            row.analysis_status = "ready"
            row.analysis_json = analysis
            db.add(row)
            db.commit()
    return [producer_attachment_out(row) for row in rows]


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
    return ContentFactoryProducerSessionOut(
        session_key=str(meta.get("session_key") or session_key),
        status=str(meta.get("status") or "needs_input"),
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
    creative_copy_contract: dict[str, Any] = {}
    if authoritative_script is not None:
        script_message_id, script_text = authoritative_script
        creative_copy_contract.update({
            "required_verbatim_voiceover": script_text,
            "source_message_id": int(script_message_id),
            "source_sha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
            "source_version": int(
                meta.get("authoritative_script_current_version") or 1
            ),
        })
    project = create_content_project(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        title=proposal.title,
        content_objective=proposal.content_objective,
        target_audience=proposal.target_audience,
        content_mode=proposal.content_mode,
        product_required=proposal.content_mode == "product",
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
        video_resolution=proposal.video_resolution,
        video_aspect_ratio=proposal.video_aspect_ratio,
        video_language=proposal.video_language,
        video_reference_limit=(9 if proposal.video_model == "seedance_2_0_mini" else 7),
        video_frame_mode="reference",
        allow_reference_video=has_reference_video,
        visual_reference_generation_mode=(
            proposal.visual_reference_generation_mode
        ),
        visual_image_model_chain=["gpt-image-2", "nano_banana_pro"],
        confirmed_promotions=proposal.confirmed_offer,
        allow_promotional_cta=bool(proposal.conversion_direction),
        publishing_profile={"platform": proposal.platform},
        creative_copy_contract=creative_copy_contract,
        creative_cast_policy={"instructions": [proposal.audio_direction]},
        director_creative_constraints=constraints,
        content_director_mode="enforce",
        preferred_browser_device_id=None,
        auto_run=True,
    )
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
        "source_text_assets": list(meta.get("source_text_assets") or []),
    }
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
    rows = visible_project_query(db, workspace_id, me.id).order_by(HermesContentFactoryProject.updated_at.desc()).limit(120).all()
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
    rows = query.order_by(HermesContentFactoryProject.updated_at.desc()).limit(200).all()
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


@router.post("/content-factory/projects", response_model=ContentFactoryProjectOut)
def create_content_factory_project(
    workspace_id: int,
    payload: ContentFactoryProjectCreate,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type="general")
    product_required = (
        payload.content_mode.strip().lower() == "product"
        if payload.product_required is None
        else bool(payload.product_required)
    )
    if product_required and payload.product_id is None and not (payload.brand_name and payload.product_name):
        raise APIError("CONTENT_PRODUCT_REQUIRED", "Choose a product from the company library or enter a product name.", 400)
    project = create_content_project(
        db, workspace_id=workspace_id, user_id=me.id, title=payload.title,
        content_objective=payload.content_objective,
        target_audience=payload.target_audience,
        content_mode=payload.content_mode,
        product_required=product_required,
        product_id=payload.product_id,
        product_name=payload.product_name or "", market=payload.market,
        product_brief=payload.product_brief, brand_name=payload.brand_name or "",
        video_count=payload.video_count,
        max_api_video_variants_in_flight=(
            payload.max_api_video_variants_in_flight
        ),
        video_duration_seconds=payload.video_duration_seconds,
        video_duration_min_seconds=payload.video_duration_min_seconds,
        video_duration_max_seconds=payload.video_duration_max_seconds,
        video_model=payload.video_model,
        video_resolution=payload.video_resolution,
        video_aspect_ratio=payload.video_aspect_ratio,
        video_language=payload.video_language,
        video_reference_limit=payload.video_reference_limit,
        video_frame_mode=payload.video_frame_mode,
        allow_reference_video=payload.allow_reference_video,
        visual_reference_generation_mode=(
            payload.visual_reference_generation_mode
        ),
        visual_image_model_chain=payload.visual_image_model_chain,
        confirmed_claims=payload.confirmed_claims,
        confirmed_selling_points=payload.confirmed_selling_points,
        confirmed_promotions=payload.confirmed_promotions,
        promotion_cta=payload.promotion_cta,
        allow_promotional_cta=payload.allow_promotional_cta,
        publishing_profile=(
            payload.publishing_profile.model_dump(exclude_none=True)
            if payload.publishing_profile is not None
            else None
        ),
        creative_copy_contract=(
            payload.creative_copy_contract.model_dump(exclude_none=True)
            if payload.creative_copy_contract is not None
            else None
        ),
        creative_cast_policy=(
            payload.creative_cast_policy.model_dump(exclude_none=True)
            if payload.creative_cast_policy is not None
            else None
        ),
        product_presentation_policy=(
            payload.product_presentation_policy.model_dump(exclude_none=True)
            if payload.product_presentation_policy is not None
            else None
        ),
        content_director_mode=payload.content_director_mode,
        director_series_brief=payload.director_series_brief,
        director_briefs_by_variant=payload.director_briefs_by_variant,
        director_loop_policy=(
            payload.director_loop_policy.model_dump()
            if payload.director_loop_policy is not None
            else None
        ),
        director_creative_constraints=(
            payload.director_creative_constraints
        ),
        director_copy_review_criteria=(
            payload.director_copy_review_criteria
        ),
        director_series_page_review_criteria=(
            payload.director_series_page_review_criteria
        ),
        director_series_global_review_criteria=(
            payload.director_series_global_review_criteria
        ),
        director_diversity_requirements=(
            payload.director_diversity_requirements
        ),
        director_structured_intent_contract_required=(
            payload.director_structured_intent_contract_required
        ),
        preferred_browser_device_id=payload.preferred_browser_device_id,
        auto_run=payload.auto_run,
    )
    # The browser may upload character/reference assets as soon as this
    # response arrives. Persist the project before returning or publishing a
    # stage so the next request and fast workers can always resolve it.
    db.commit()
    db.refresh(project)
    if payload.auto_run and not payload.allow_reference_video:
        try:
            queue_stage(
                db, project=project, user_id=me.id,
                instruction=(
                    "Continue unattended from the universal Director using "
                    "the saved project objective, supplied truth, and only "
                    "the configured engagement or conversion boundary."
                ),
                target_stage=project.current_stage, continue_workflow=True,
            )
        except APIError as exc:
            if exc.code not in {
                "CONTENT_BROWSER_BRIDGE_REQUIRED",
                "CONTENT_BROWSER_BRIDGE_OFFLINE",
                "CONTENT_BROWSER_CAPACITY_FULL",
            }:
                raise
            # The project and its slot request are durable. The local bridge
            # agent may need a few seconds to create the requested Chrome slot;
            # periodic self-heal will acquire it and publish the first stage.
            db.expire_all()
            project = get_content_project(db, workspace_id, me.id, project.project_key)
    return project_out(db, project)


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
