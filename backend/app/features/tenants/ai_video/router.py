from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import false
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import ADMIN_ROLES, SessionUser, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.data.models.users import User
from app.services.bandianwa.tasks import (
    refresh_bandianwa_task_status_by_task_id,
)
from app.services.ai_video.task_state import create_local_video_task, reset_video_task_for_retry
from app.services.ai_video.accounts import (
    BANDIANWA_PROVIDER_KEY,
    DOUBAO_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    OMNI_FLASH_MODEL,
    SEEDANCE_2_0_MINI_MODEL,
    SUB2API_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    count_keys,
    get_effective_key,
    key_model_priorities,
    list_model_keys,
    model_scope,
    normalize_video_model_id,
    normalize_provider_key,
    resolve_video_model_key,
    video_model_routing_catalog,
)
from app.services.ai_video.retry_policy import (
    archive_successful_task_result_files,
    delete_task_result_files,
)
from app.services.ai_video.local_storage import (
    RESULT_FILE_KINDS,
    get_task_local_meta,
    get_local_path,
    managed_reference_roots,
    managed_result_roots,
    mark_result_file_pending,
    resolve_managed_file,
    set_task_local_meta,
)
from app.services.ai_video.reference_capability import verify_reference_capability
from app.services.ai_video.queues import production_video_queue
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/ai-video/videos",
    tags=["Tenant / AI Video"],
)


def _assert_persisted_tenant_actor(
    db: Session, *, workspace_id: int, me: SessionUser
) -> User:
    """Defend tenant writes even when an endpoint is called as Python code.

    FastAPI normally enforces ``require_tenant_member`` before entering the
    route.  Internal callers do not execute dependency injection, so trusting
    a constructed SessionUser here allowed a platform administrator to create
    a tenant video task accidentally.  Re-read the persisted user boundary at
    the write site instead of trusting caller-supplied session fields.
    """
    user = db.get(User, int(me.id))
    if (
        user is None
        or user.deleted_at is not None
        or not bool(user.is_active)
        or bool(user.is_platform_admin)
        or int(user.workspace_id) != int(workspace_id)
        or bool(me.is_platform_admin)
        or int(me.workspace_id) != int(workspace_id)
    ):
        raise APIError(
            "FORBIDDEN",
            "Platform users cannot create tenant AI video tasks.",
            403,
        )
    return user

VIDEO_PROVIDER_KEYS = (
    DOUBAO_PROVIDER_KEY,
    SUB2API_PROVIDER_KEY,
    BANDIANWA_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
)


_STORED_VIDEO_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    OMNI_FLASH_MODEL: (
        OMNI_FLASH_MODEL,
        "gemini-omni-flash-preview",
        "gemini_omni_flash_preview",
    ),
    SEEDANCE_2_0_MINI_MODEL: (
        SEEDANCE_2_0_MINI_MODEL,
        "doubao-seedance-2-0-mini-260615",
        "doubao_seedance_2_0_mini_260615",
    ),
}


def _stored_video_model_values(model_id: str | None) -> tuple[str, ...]:
    """Return every persisted spelling belonging to one logical video model.

    Provider-specific task model names were historically written into
    ``KieTask.model`` by Content Factory while the member AI Video page filters
    by the canonical logical model id.  Keep authorization in the surrounding
    task query and make only the model predicate alias-aware so those owned
    tasks remain visible and manageable without broadening tenancy.
    """
    canonical = normalize_video_model_id(model_id)
    aliases = _STORED_VIDEO_MODEL_ALIASES.get(canonical, (canonical,))
    return tuple(dict.fromkeys(str(value) for value in aliases if str(value)))


def _filter_task_model(query, model_id: str | None):
    values = _stored_video_model_values(model_id)
    if len(values) == 1:
        return query.filter(KieTask.model == values[0])
    return query.filter(KieTask.model.in_(values))


def _batch_limit() -> int:
    return max(1, min(int(getattr(settings, "AI_VIDEO_BATCH_LIMIT", 50)), 200))


class AiVideoTaskOut(BaseModel):
    id: int
    workspace_id: int
    model: str
    task_id: str
    state: str
    created_by_user_id: Optional[int] = None
    created_by_username: Optional[str] = None
    created_by_display_name: Optional[str] = None
    created_by_usercode: Optional[str] = None
    created_by_label: Optional[str] = None
    prompt: Optional[str] = None
    input_json: Optional[dict[str, Any]] = None
    batch_root_task_id: Optional[int] = None
    batch_index: Optional[int] = None
    batch_size: Optional[int] = None
    fail_code: Optional[str] = None
    fail_msg: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    routing_mode: Optional[str] = None
    current_provider: Optional[str] = None
    status_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AiVideoFileOut(BaseModel):
    id: int
    kind: str
    file_url: str
    download_url: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class AiVideoBatchItem(BaseModel):
    model: str = Field(..., min_length=1, max_length=128)
    prompt: str = Field(..., min_length=1, max_length=10_000)
    aspect_ratio: Optional[str] = Field("16:9", max_length=16)
    generate_audio: Optional[bool] = True
    reference_images: List[str] = Field(default_factory=list)
    reference_file_paths: List[dict[str, Any]] = Field(default_factory=list)
    reference_mode: Optional[str] = Field(None, max_length=32)
    size: Optional[str] = Field(None, max_length=64)
    seconds: Optional[int] = None
    duration: Optional[int] = None
    resolution: Optional[str] = Field(None, max_length=16)
    submit_path: Optional[str] = Field(None, max_length=64)
    reference_file_count: int = 0
    service_provider: Optional[str] = Field("auto", max_length=32)


class AiVideoCreateRequest(AiVideoBatchItem):
    key_id: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None


class AiVideoBatchRequest(BaseModel):
    items: List[AiVideoBatchItem] = Field(..., min_length=1)
    key_id: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None


class AiVideoCreateResponse(BaseModel):
    task: AiVideoTaskOut


class AiVideoBatchCreateResponse(BaseModel):
    tasks: List[AiVideoTaskOut]
    total: int


class AiVideoTaskListResponse(BaseModel):
    items: List[AiVideoTaskOut]
    total: int


class AiVideoClearTasksResponse(BaseModel):
    deleted_tasks: int
    deleted_files: int


class AiVideoTaskRegenerateRequest(BaseModel):
    input_params: Optional[dict[str, Any]] = None


class AiVideoBatchDownloadRequest(BaseModel):
    task_ids: List[int]


def _local_download_url(workspace_id: int, file_id: int, *, admin: bool = False) -> str:
    scope = "/admin" if admin else ""
    return (
        f"{settings.API_PREFIX}/tenants/{int(workspace_id)}"
        f"/ai-video/videos{scope}/files/{int(file_id)}/download"
    )


def _remove_temp_file(path_value: str) -> None:
    try:
        Path(path_value).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to remove temp batch download zip", extra={"path": path_value})


def _zip_entry_name(task_id: int, file: KieFile, local_path: Path, used: set[str]) -> str:
    ext = local_path.suffix or ".mp4"
    filename = f"{int(task_id)}{ext}"
    if file.kind == "result_watermark":
        filename = f"{int(task_id)}-watermark{ext}"
    meta = file.meta_json or {}
    if isinstance(meta, dict) and meta.get("filename"):
        filename = str(meta.get("filename"))
    filename = Path(filename).name or f"{int(task_id)}{ext}"
    if filename not in used:
        used.add(filename)
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ext
    idx = 2
    while True:
        candidate = f"{stem}-{idx}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        idx += 1


def _create_batch_download_zip(
    rows: list[tuple[KieFile, int]],
    *,
    workspace_id: int,
    provider_label: str,
) -> tuple[str, str, int]:
    selected: list[tuple[KieFile, int, Path]] = []
    for file, task_id in rows:
        local_path = get_local_path(file)
        if local_path is None or not local_path.exists() or not local_path.is_file():
            continue
        selected.append((file, int(task_id), local_path))

    if not selected:
        raise HTTPException(status_code=404, detail="No local result files are available for batch download")

    tmp = tempfile.NamedTemporaryFile(
        prefix=f"gmv-ai-video-w{int(workspace_id)}-",
        suffix=".zip",
        delete=False,
    )
    tmp_path = tmp.name
    tmp.close()

    used_names: set[str] = set()
    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_STORED) as archive:
            for file, task_id, local_path in selected:
                archive.write(
                    str(local_path),
                    arcname=_zip_entry_name(task_id, file, local_path, used_names),
                )
    except Exception:
        _remove_temp_file(tmp_path)
        raise

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return tmp_path, f"{provider_label}-w{int(workspace_id)}-{stamp}.zip", len(selected)


def _task_query(db: Session, workspace_id: int, user_id: int | None = None):
    provider_key_ids = [
        int(row[0])
        for row in (
            db.query(KieApiKey.id)
            .filter(KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS))
            .all()
        )
    ]
    if not provider_key_ids:
        return db.query(KieTask).filter(false())

    q = db.query(KieTask).filter(
        KieTask.workspace_id == int(workspace_id),
        KieTask.key_id.in_(provider_key_ids),
    )
    if user_id is not None:
        q = q.filter(KieTask.created_by_user_id == int(user_id))
    return q


def _can_view_workspace_tasks(me: SessionUser) -> bool:
    return str(me.role) in ADMIN_ROLES


def _visible_task_query(db: Session, workspace_id: int, me: SessionUser):
    return _task_query(db, workspace_id, int(me.id))


def _workspace_task_query(db: Session, workspace_id: int):
    return _task_query(db, workspace_id, None)


def _paged_task_rows(q, *, offset: int, size: int) -> list[KieTask]:
    """Page task ids first so MySQL never filesorts the large JSON payloads.

    KieTask carries provider request/result JSON. MySQL may choose a filesort
    even when the workspace/id index exists; sorting full ORM rows can exhaust
    the per-session sort buffer. The narrow id page keeps that operation
    bounded, then hydrates only the selected rows while preserving id order.
    """
    id_rows = (
        q.with_entities(KieTask.id)
        .order_by(KieTask.id.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, int(size)))
        .all()
    )
    task_ids = [int(task_id) for (task_id,) in id_rows]
    if not task_ids:
        return []
    rows = q.filter(KieTask.id.in_(task_ids)).all()
    by_id = {int(task.id): task for task in rows}
    return [by_id[task_id] for task_id in task_ids if task_id in by_id]


def _safe_unlink(path_value: str | None) -> None:
    if not path_value:
        return
    resolved = resolve_managed_file(path_value, managed_result_roots())
    if resolved is None:
        return
    try:
        resolved.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to delete local artifact", extra={"path": str(resolved)})


def _delete_task_local_artifacts(files: list[KieFile]) -> None:
    for file in files:
        local_path = get_local_path(file)
        if local_path is not None:
            _safe_unlink(str(local_path))
        if file.kind == "reference_upload":
            _safe_unlink(file.file_url)


def _delete_task_records(db: Session, *, workspace_id: int, task_ids: list[int]) -> tuple[int, int, list[KieFile]]:
    if not task_ids:
        return 0, 0, []

    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.task_id.in_(task_ids),
        )
        .all()
    )
    deleted_files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.task_id.in_(task_ids),
        )
        .delete(synchronize_session=False)
    )
    deleted_tasks = (
        db.query(KieTask)
        .filter(
            KieTask.workspace_id == int(workspace_id),
            KieTask.id.in_(task_ids),
        )
        .delete(synchronize_session=False)
    )
    return deleted_tasks, deleted_files, files


def _can_retry_task(task: KieTask) -> bool:
    return str(task.state or "").lower() in {"failed", "error", "timeout"}


def _can_regenerate_task(task: KieTask) -> bool:
    return str(task.state or "").lower() in {"success", "failed", "error", "timeout"}


def _require_workspace_task_admin(me: SessionUser) -> None:
    if not _can_view_workspace_tasks(me):
        raise HTTPException(status_code=403, detail="Admin role required")


def _batch_meta(task: KieTask) -> dict[str, Any]:
    payload = task.result_json or {}
    local_meta = payload.get("__local") if isinstance(payload, dict) else None
    if not isinstance(local_meta, dict):
        return {}
    return local_meta


def _elapsed_minutes(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        observed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return max(
        0,
        int((datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds() // 60),
    )


def _attach_batch_fields(tasks: list[KieTask]) -> list[KieTask]:
    for task in tasks:
        meta = _batch_meta(task)
        root_id = meta.get("batch_root_task_id")
        batch_index = meta.get("batch_index")
        batch_size = meta.get("batch_size")
        task.batch_root_task_id = int(root_id) if root_id is not None else None
        task.batch_index = int(batch_index) if batch_index is not None else None
        task.batch_size = int(batch_size) if batch_size is not None else None
        params = dict(task.input_json or {})
        routing_mode = str(params.get("routing_mode") or "").strip().lower()
        if routing_mode not in {"auto", "pinned"}:
            requested = str(params.get("service_provider") or "auto").strip().lower()
            routing_mode = "auto" if requested == "auto" else "pinned"
        current_provider = normalize_provider_key(
            str(meta.get("active_provider") or params.get("service_provider") or "")
        )
        provider_labels = {
            "sub2api": "自建 Sub2API",
            "bandianwa": "斑点蛙",
            "doubao": "豆包",
            "kyy": "客易云",
            "google-gemini": "Google Gemini",
            "volcengine": "火山引擎",
            "toapis": "ToAPIs",
        }
        provider_label = provider_labels.get(current_provider, current_provider or "统一路由")
        state = str(task.state or "").strip().lower()
        retrying = bool(
            int(meta.get("auto_retry_count") or 0) > 0
            or meta.get("video_provider_recovery")
        )
        if state == "queued_local":
            status_message = (
                f"{provider_label}暂时未响应，系统正在自动重试或切换可用供应商"
                if retrying
                else f"已进入统一调度，准备提交至{provider_label}"
            )
        elif state == "submitting" and current_provider == "doubao":
            submit_phase = str(meta.get("doubao_submit_phase") or "").strip().lower()
            status_message = {
                "selecting_account": "正在选择豆包生产账号",
                "starting_profile": "正在启动豆包账号的独立浏览器环境",
                "checking_composer": "正在检查 Seedance 视频编辑器",
                "submitting_request": "正在向豆包提交视频任务",
                "switching_account": "当前账号不可用，正在快速切换可用账号",
            }.get(submit_phase, "正在向豆包提交视频任务")
        elif state in {"queued", "pending", "in_progress", "processing", "running"}:
            if current_provider == "doubao" and meta.get("doubao_remote_accepted_at"):
                elapsed_minutes = _elapsed_minutes(meta.get("doubao_remote_accepted_at"))
                elapsed_suffix = (
                    f"（已等待约 {elapsed_minutes} 分钟）"
                    if elapsed_minutes is not None and elapsed_minutes >= 1
                    else ""
                )
                status_message = f"豆包已接收，正在远端排队或生成{elapsed_suffix}"
            else:
                status_message = f"已提交至{provider_label}，正在生成"
        elif state == "downloading":
            status_message = f"{provider_label}已生成，正在保存到本地存储"
        elif state == "success":
            status_message = "视频已生成并保存到本地存储"
        elif state in {"failed", "error", "timeout"}:
            status_message = str(task.fail_msg or "任务未完成")[:240]
        else:
            status_message = str(task.state or "等待处理")
        task.routing_mode = routing_mode
        task.current_provider = current_provider or None
        task.status_message = status_message
    return tasks


def _set_routing_intent(input_params: dict[str, Any], *, selected_provider: str) -> None:
    """Keep the user's routing choice separate from the active transport."""
    requested = str(input_params.get("service_provider") or "auto").strip().lower()
    input_params["routing_mode"] = "auto" if requested == "auto" else "pinned"
    input_params["requested_service_provider"] = requested
    input_params["service_provider"] = str(selected_provider)


def _attach_creator_fields(
    db: Session,
    workspace_id: int,
    tasks: list[KieTask],
) -> list[KieTask]:
    user_ids = {
        int(task.created_by_user_id)
        for task in tasks
        if task.created_by_user_id is not None
    }
    users: dict[int, User] = {}
    if user_ids:
        users = {
            int(user.id): user
            for user in (
                db.query(User)
                .filter(
                    User.workspace_id == int(workspace_id),
                    User.id.in_(user_ids),
                )
                .all()
            )
        }

    for task in tasks:
        uid = int(task.created_by_user_id) if task.created_by_user_id is not None else None
        user = users.get(uid) if uid is not None else None
        username = user.username if user else None
        display_name = user.display_name if user else None
        usercode = user.usercode if user else None
        label = display_name or username or usercode
        if not label:
            label = f"用户 #{uid}" if uid is not None else "历史任务"
        task.created_by_username = username
        task.created_by_display_name = display_name
        task.created_by_usercode = usercode
        task.created_by_label = label
    _attach_batch_fields(tasks)
    return tasks


def _is_pending_state(state: str | None) -> bool:
    s = (state or "").strip().lower()
    if not s:
        return False
    if s in {"success", "failed", "error", "timeout"}:
        return False
    return any(token in s for token in ("queue", "pending", "wait", "progress", "process", "run", "gen"))


def _supports_inline_refresh(task: KieTask) -> bool:
    provider_supported = normalize_provider_key(
        str((task.input_json or {}).get("service_provider") or BANDIANWA_PROVIDER_KEY)
    ) == BANDIANWA_PROVIDER_KEY
    if not provider_supported:
        return False
    # The Celery poller owns all provider polling and task/file transitions
    # once it has claimed this task.  A list/detail request must then remain
    # read-only: inline polling from two Gunicorn workers otherwise updates the
    # same task and result-file rows in the inverse order and can deadlock
    # MySQL.  A stale/died poller is recovered by the queue state machine, not
    # by an HTTP GET racing it.
    poll_owner = str(
        get_task_local_meta(task).get("poll_owner_task_id") or ""
    ).strip()
    return not bool(poll_owner)


def _resolve_key(db: Session, key_id: int | None, provider_key: str = BANDIANWA_PROVIDER_KEY) -> KieApiKey:
    try:
        return get_effective_key(
            db,
            key_id=key_id,
            require_active=True,
            provider_key=provider_key,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


PROVIDER_LABELS = {
    BANDIANWA_PROVIDER_KEY: "Bandianwa",
    KYY_PROVIDER_KEY: "KYY",
    VOLCENGINE_PROVIDER_KEY: "Volcengine Ark",
    GOOGLE_GEMINI_PROVIDER_KEY: "Google Gemini",
    TOAPIS_PROVIDER_KEY: "ToAPIs",
    SUB2API_PROVIDER_KEY: "Sub2API",
    DOUBAO_PROVIDER_KEY: "Doubao Seedance Pool",
}


def _provider_count(db: Session, provider_key: str, *, active_only: bool = True) -> int:
    return count_keys(
        db,
        provider_key=provider_key,
        require_active=active_only,
    )


def _provider_is_active(db: Session, provider_key: str) -> bool:
    return _provider_count(db, provider_key, active_only=True) > 0


def _provider_status_payload(db: Session) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    for raw_key in VIDEO_PROVIDER_KEYS:
        provider_key = normalize_provider_key(raw_key)
        active_count = _provider_count(db, provider_key, active_only=True)
        total_count = _provider_count(db, provider_key, active_only=False)
        providers[provider_key] = {
            "provider_key": provider_key,
            "label": PROVIDER_LABELS.get(provider_key, provider_key),
            "is_active": active_count > 0,
            "active_key_count": active_count,
            "total_key_count": total_count,
            "disabled_reason": None if active_count > 0 else "No active API key",
        }
        if provider_key == DOUBAO_PROVIDER_KEY:
            from app.data.models.hermes_agent import HermesBrowserBridge
            from app.services.doubao_provider.pool import account_is_ready

            pool_rows = db.query(HermesBrowserBridge).filter(
                HermesBrowserBridge.status != "retired"
            ).all()
            ready_accounts = sum(1 for row in pool_rows if account_is_ready(row))
            providers[provider_key].update(
                {
                    "is_active": active_count > 0 and ready_accounts > 0,
                    "ready_account_count": ready_accounts,
                    "disabled_reason": (
                        None
                        if active_count > 0 and ready_accounts > 0
                        else "No ready Doubao account in the self-hosted pool"
                    ),
                }
            )
    return {
        "providers": providers,
        "models": video_model_routing_catalog(db),
    }


def _normalize_service_provider(value: str | None) -> str:
    provider = str(value or "auto").strip().lower()
    if provider == "auto":
        return "auto"
    normalized = normalize_provider_key(provider)
    return normalized if normalized in VIDEO_PROVIDER_KEYS else "auto"


def _resolve_model_route(
    db: Session,
    *,
    model: str,
    reference_count: int,
    aspect_ratio: str | None = None,
    reference_mode: str | None = None,
    duration: int | None = None,
    resolution: str | None = None,
    provider_key: str | None = None,
    key_id: int | None = None,
) -> KieApiKey:
    try:
        requested_provider = _normalize_service_provider(provider_key)
        if key_id is None and requested_provider != "auto":
            candidates = list_model_keys(
                db,
                model_id=model,
                reference_count=reference_count,
                aspect_ratio=aspect_ratio,
                reference_mode=reference_mode,
                duration=duration,
                resolution=resolution,
                require_active=True,
            )
            selected = next(
                (key for key in candidates if normalize_provider_key(key.provider_key) == requested_provider),
                None,
            )
            if selected is None:
                raise ValueError(f"No active compatible API key for provider {requested_provider}")
            return selected
        return resolve_video_model_key(
            db,
            model_id=model,
            reference_count=reference_count,
            aspect_ratio=aspect_ratio,
            reference_mode=reference_mode,
            duration=duration,
            resolution=resolution,
            key_id=key_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/provider-status")
async def get_ai_video_provider_status(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return _provider_status_payload(db)


def _normalize_item(item: AiVideoBatchItem) -> dict[str, Any]:
    model = normalize_video_model_id(item.model)
    prompt = item.prompt.strip()
    service_provider = str(item.service_provider or "auto").strip().lower()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if service_provider not in {
        "auto", "bandianwa", "kyy", "volcengine", "google-gemini", "google_gemini", "toapis", "sub2api", "doubao"
    }:
        raise HTTPException(status_code=400, detail="service_provider is invalid")
    if model not in {OMNI_FLASH_MODEL, SEEDANCE_2_0_MINI_MODEL}:
        raise HTTPException(status_code=400, detail="Unsupported video model")
    if model == SEEDANCE_2_0_MINI_MODEL and len(prompt) > 495:
        raise HTTPException(
            status_code=400,
            detail=(
                "豆包页面输入硬上限为 500 个字符；系统按已验证的 "
                "495 字符安全预算提交，不会写入重复模式指令或静默截断。"
            ),
        )

    references = [x.strip() for x in item.reference_images if str(x).strip()]
    reference_files = [
        dict(ref)
        for ref in item.reference_file_paths
        if isinstance(ref, dict) and str(ref.get("path") or "").strip()
    ]
    total_reference_count = len(references) + len(reference_files) + int(item.reference_file_count or 0)
    reference_limit = 10 if model == SEEDANCE_2_0_MINI_MODEL else 7
    if len(references) > reference_limit:
        raise HTTPException(status_code=400, detail=f"reference_images supports at most {reference_limit} images")
    if total_reference_count > reference_limit:
        raise HTTPException(status_code=400, detail=f"reference files supports at most {reference_limit} images")

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": item.aspect_ratio,
        "generate_audio": item.generate_audio,
        "reference_images": references or None,
        "reference_file_paths": reference_files or None,
        "reference_mode": item.reference_mode,
        "size": item.size,
        "seconds": item.seconds,
        "duration": item.duration,
        "resolution": item.resolution,
        "submit_path": item.submit_path,
        "service_provider": service_provider,
    }
    return {k: v for k, v in payload.items() if v is not None}


def _safe_image_extension(upload: UploadFile) -> str:
    filename = upload.filename or ""
    ext = Path(filename).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    content_type = (upload.content_type or "").lower()
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    return ".jpg"


async def _save_reference_upload(
    *,
    workspace_id: int,
    upload: UploadFile,
) -> dict[str, Any]:
    content_type = upload.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are supported")

    base_dir = Path(settings.BANDIANWA_UPLOAD_STORAGE_DIR).expanduser()
    target_dir = base_dir / f"workspace_{int(workspace_id)}" / datetime.now(timezone.utc).strftime("%Y%m%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}{_safe_image_extension(upload)}"
    target_path = target_dir / filename

    total = 0
    max_bytes = int(settings.BANDIANWA_UPLOAD_MAX_IMAGE_BYTES)
    with target_path.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                try:
                    target_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail="Image file too large")
            handle.write(chunk)

    await upload.close()
    return {
        "path": str(target_path),
        "filename": upload.filename or filename,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": total,
    }


async def _save_reference_uploads(
    *,
    workspace_id: int,
    uploads: list[UploadFile],
) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for upload in uploads:
        saved.append(await _save_reference_upload(workspace_id=workspace_id, upload=upload))
    return saved


def _enqueue(task: KieTask, *, interval_seconds: int, timeout_seconds: int) -> None:
    # Keep Celery task modules out of API-router import time.  The Celery app
    # imports both task modules while building its registry; importing them
    # eagerly from this router creates a result_download -> celery_app ->
    # video_tasks -> result_download cycle in diagnostic and migration tools.
    from app.tasks.ai_video.video_tasks import submit_and_poll_ai_video_task

    submit_and_poll_ai_video_task.apply_async(
        kwargs={
            "workspace_id": int(task.workspace_id),
            "local_task_id": int(task.id),
            "interval_seconds": int(interval_seconds),
            "timeout_seconds": int(timeout_seconds),
        },
        queue=production_video_queue(task),
    )


def _enqueue_result_download(*, workspace_id: int, local_task_id: int) -> None:
    from app.tasks.ai_video.result_download_tasks import queue_task_result_download

    queue_task_result_download(
        workspace_id=int(workspace_id),
        local_task_id=int(local_task_id),
    )


def _mark_enqueue_failed(db: Session, task: KieTask, exc: Exception) -> None:
    task.state = "failed"
    task.fail_code = "enqueue_failed"
    task.fail_msg = str(exc)[:512]
    db.add(task)


def _add_reference_file_records(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    task: KieTask,
    refs: list[dict[str, Any]],
) -> None:
    for reference_index, ref in enumerate(refs or [], start=1):
        path = str(ref.get("path") or "").strip()
        if not path:
            continue
        db.add(
            KieFile(
                workspace_id=int(workspace_id),
                key_id=int(key_id),
                task_id=int(task.id),
                file_url=path,
                kind="reference_upload",
                mime_type=str(ref.get("content_type") or "image/*"),
                size_bytes=int(ref.get("size_bytes") or 0),
                meta_json={
                    "filename": ref.get("filename"),
                    "source": "ai_video_upload",
                    "reference_index": reference_index,
                },
            )
        )


def _managed_reference_path(*, workspace_id: int, path_value: str) -> Path:
    root = (
        Path(settings.BANDIANWA_UPLOAD_STORAGE_DIR)
        .expanduser()
        .resolve()
        / f"workspace_{int(workspace_id)}"
    )
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="参考图文件已失效，请重新上传") from exc
    if not path.is_file() or not path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="参考图文件无效，请重新上传")
    return path


def _clone_inherited_reference_files(
    db: Session,
    *,
    workspace_id: int,
    actor_user_id: int,
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cloned: list[dict[str, Any]] = []
    created_paths: list[Path] = []
    target_dir = (
        Path(settings.BANDIANWA_UPLOAD_STORAGE_DIR).expanduser()
        / f"workspace_{int(workspace_id)}"
        / datetime.now(timezone.utc).strftime("%Y%m%d")
        / "task_refs"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        for ref in refs or []:
            raw_path = str(ref.get("path") or "").strip()
            if not raw_path:
                continue
            owned_file = (
                db.query(KieFile)
                .join(KieTask, KieFile.task_id == KieTask.id)
                .filter(
                    KieFile.workspace_id == int(workspace_id),
                    KieFile.kind == "reference_upload",
                    KieFile.file_url == raw_path,
                    KieTask.workspace_id == int(workspace_id),
                    KieTask.created_by_user_id == int(actor_user_id),
                )
                .first()
            )
            if owned_file is None:
                candidates = (
                    db.query(KieFile)
                    .join(KieTask, KieFile.task_id == KieTask.id)
                    .filter(
                        KieFile.workspace_id == int(workspace_id),
                        KieFile.kind == "reference_upload",
                        KieTask.workspace_id == int(workspace_id),
                        KieTask.created_by_user_id == int(actor_user_id),
                    )
                    .all()
                )
                owned_file = next(
                    (
                        file
                        for file in candidates
                        if str((file.meta_json or {}).get("migrated_from") or "") == raw_path
                    ),
                    None,
                )
            if owned_file is None:
                raise HTTPException(status_code=403, detail="无权使用该参考图，请重新上传")
            source = _managed_reference_path(
                workspace_id=workspace_id,
                path_value=str(owned_file.file_url),
            )
            suffix = source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
            target = target_dir / f"{uuid4().hex}{suffix}"
            shutil.copy2(source, target)
            created_paths.append(target)
            cloned.append(
                {
                    "path": str(target),
                    "filename": ref.get("filename") or source.name,
                    "content_type": ref.get("content_type") or "image/*",
                    "size_bytes": int(target.stat().st_size),
                }
            )
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return cloned


def _sync_task_reference_records(
    db: Session,
    *,
    workspace_id: int,
    task: KieTask,
    refs: list[dict[str, Any]],
) -> None:
    current = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind == "reference_upload",
        )
        .all()
    )
    by_path = {str(file.file_url): file for file in current}
    aliases = {
        str((file.meta_json or {}).get("migrated_from")): file
        for file in current
        if (file.meta_json or {}).get("migrated_from")
    }
    wanted_paths: set[str] = set()
    for reference_index, ref in enumerate(refs or [], start=1):
        path = str(ref.get("path") or "").strip()
        if not path:
            continue
        file = by_path.get(path) or aliases.get(path)
        if file is None:
            raise HTTPException(status_code=403, detail="参考图不属于当前任务，请重新上传")
        current_path = str(file.file_url)
        _managed_reference_path(workspace_id=workspace_id, path_value=current_path)
        ref["path"] = current_path
        meta = dict(file.meta_json or {})
        meta["reference_index"] = reference_index
        file.meta_json = meta
        db.add(file)
        wanted_paths.add(current_path)
    for path, file in by_path.items():
        if path not in wanted_paths:
            db.delete(file)


@router.post(
    "/generate",
    response_model=AiVideoCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_video(
    workspace_id: int,
    payload: AiVideoCreateRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    batch_payload = AiVideoBatchRequest(
        key_id=payload.key_id,
        poll_interval_seconds=payload.poll_interval_seconds,
        timeout_seconds=payload.timeout_seconds,
        items=[
            AiVideoBatchItem(
                model=payload.model,
                prompt=payload.prompt,
                aspect_ratio=payload.aspect_ratio,
                generate_audio=payload.generate_audio,
                reference_images=payload.reference_images,
                reference_file_paths=payload.reference_file_paths,
                reference_mode=payload.reference_mode,
                size=payload.size,
                seconds=payload.seconds,
                duration=payload.duration,
                submit_path=payload.submit_path,
                service_provider=payload.service_provider,
            ),
        ],
    )
    created = await create_ai_video_batch(
        workspace_id=workspace_id,
        payload=batch_payload,
        me=me,
        db=db,
    )
    return AiVideoCreateResponse(task=created.tasks[0])


@router.post(
    "/batch",
    response_model=AiVideoBatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_video_batch(
    workspace_id: int,
    payload: AiVideoBatchRequest,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _assert_persisted_tenant_actor(db, workspace_id=workspace_id, me=me)
    limit = _batch_limit()
    if len(payload.items) > limit:
        raise HTTPException(status_code=400, detail=f"Batch size cannot exceed {limit}")

    tasks: list[KieTask] = []

    for item in payload.items:
        input_params = _normalize_item(item)
        inherited_refs = input_params.get("reference_file_paths") or []
        if inherited_refs:
            input_params["reference_file_paths"] = _clone_inherited_reference_files(
                db,
                workspace_id=int(workspace_id),
                actor_user_id=int(me.id),
                refs=inherited_refs,
            )
        reference_count = len(input_params.get("reference_images") or []) + len(input_params.get("reference_file_paths") or [])
        key = _resolve_model_route(
            db,
            model=str(input_params["model"]),
            reference_count=reference_count,
            aspect_ratio=str(input_params.get("aspect_ratio") or ""),
            reference_mode=str(input_params.get("reference_mode") or "reference"),
            duration=int(input_params.get("seconds") or input_params.get("duration")) if (input_params.get("seconds") or input_params.get("duration")) else None,
            resolution=str(input_params.get("resolution") or "") or None,
            provider_key=str(input_params.get("service_provider") or "auto"),
            key_id=payload.key_id,
        )
        _set_routing_intent(input_params, selected_provider=str(key.provider_key))
        input_params["routing_scope"] = model_scope(str(input_params["model"]))
        input_params["routing_priority"] = key_model_priorities(key).get(str(input_params["model"]), 9999)
        task = create_local_video_task(
            db,
            workspace_id=int(workspace_id),
            key_id=int(key.id),
            input_params=input_params,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
        )
        tasks.append(task)
        _add_reference_file_records(
            db,
            workspace_id=int(workspace_id),
            key_id=int(key.id),
            task=task,
            refs=input_params.get("reference_file_paths") or [],
        )

    if len(tasks) > 1:
        batch_root_id = max(int(task.id) for task in tasks)
        for index, task in enumerate(tasks, start=1):
            set_task_local_meta(
                task,
                download_name_base=f"{batch_root_id}-{index}",
                batch_root_task_id=batch_root_id,
                batch_index=index,
                batch_size=len(tasks),
            )
            db.add(task)

    db.commit()

    interval_seconds = payload.poll_interval_seconds or settings.BANDIANWA_POLL_INTERVAL_SECONDS
    timeout_seconds = payload.timeout_seconds or settings.BANDIANWA_POLL_TIMEOUT_SECONDS
    for task in tasks:
        try:
            _enqueue(
                task,
                interval_seconds=int(interval_seconds),
                timeout_seconds=int(timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue AI video task",
                extra={"workspace_id": workspace_id, "local_task_id": int(task.id)},
            )
            _mark_enqueue_failed(db, task, exc)

    db.commit()
    tasks = _attach_batch_fields(tasks)
    return AiVideoBatchCreateResponse(tasks=tasks, total=len(tasks))


@router.post(
    "/batch-upload",
    response_model=AiVideoBatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_video_batch_upload(
    workspace_id: int,
    items: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    key_id: Optional[int] = Form(None),
    poll_interval_seconds: Optional[int] = Form(None),
    timeout_seconds: Optional[int] = Form(None),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _assert_persisted_tenant_actor(db, workspace_id=workspace_id, me=me)
    try:
        raw_items = json.loads(items)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"items must be valid JSON: {exc}") from exc
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail="items must be an array")

    limit = _batch_limit()
    if len(raw_items) < 1:
        raise HTTPException(status_code=400, detail="items cannot be empty")
    if len(raw_items) > limit:
        raise HTTPException(status_code=400, detail=f"Batch size cannot exceed {limit}")

    parsed_items = [AiVideoBatchItem.model_validate(raw) for raw in raw_items]
    all_files = list(files or [])
    cursor = 0
    tasks: list[KieTask] = []

    for item in parsed_items:
        count = int(item.reference_file_count or 0)
        normalized_model = normalize_video_model_id(item.model)
        limit_for_item = 10 if normalized_model == SEEDANCE_2_0_MINI_MODEL else 7
        if count < 0 or count > limit_for_item:
            raise HTTPException(status_code=400, detail=f"reference_file_count must be between 0 and {limit_for_item}")
        item_files = all_files[cursor : cursor + count]
        if len(item_files) != count:
            raise HTTPException(status_code=400, detail="Uploaded file count does not match items metadata")
        cursor += count

        input_params = _normalize_item(item)
        inherited_refs = input_params.get("reference_file_paths") or []
        if inherited_refs:
            input_params["reference_file_paths"] = _clone_inherited_reference_files(
                db,
                workspace_id=int(workspace_id),
                actor_user_id=int(me.id),
                refs=inherited_refs,
            )
        if item_files:
            saved_files = await _save_reference_uploads(
                workspace_id=int(workspace_id),
                uploads=item_files,
            )
            input_params["reference_file_paths"] = [
                *(input_params.get("reference_file_paths") or []),
                *saved_files,
            ]

        reference_count = len(input_params.get("reference_images") or []) + len(input_params.get("reference_file_paths") or [])
        key = _resolve_model_route(
            db,
            model=str(input_params["model"]),
            reference_count=reference_count,
            aspect_ratio=str(input_params.get("aspect_ratio") or ""),
            reference_mode=str(input_params.get("reference_mode") or "reference"),
            duration=int(input_params.get("seconds") or input_params.get("duration")) if (input_params.get("seconds") or input_params.get("duration")) else None,
            resolution=str(input_params.get("resolution") or "") or None,
            provider_key=str(input_params.get("service_provider") or "auto"),
            key_id=key_id,
        )
        _set_routing_intent(input_params, selected_provider=str(key.provider_key))
        input_params["routing_scope"] = model_scope(str(input_params["model"]))
        input_params["routing_priority"] = key_model_priorities(key).get(str(input_params["model"]), 9999)

        task = create_local_video_task(
            db,
            workspace_id=int(workspace_id),
            key_id=int(key.id),
            input_params=input_params,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
        )
        tasks.append(task)

        _add_reference_file_records(
            db,
            workspace_id=int(workspace_id),
            key_id=int(key.id),
            task=task,
            refs=input_params.get("reference_file_paths") or [],
        )

    if len(tasks) > 1:
        batch_root_id = max(int(task.id) for task in tasks)
        for index, task in enumerate(tasks, start=1):
            set_task_local_meta(
                task,
                download_name_base=f"{batch_root_id}-{index}",
                batch_root_task_id=batch_root_id,
                batch_index=index,
                batch_size=len(tasks),
            )
            db.add(task)

    if cursor != len(all_files):
        raise HTTPException(status_code=400, detail="Too many uploaded files for items metadata")

    db.commit()

    interval_seconds = poll_interval_seconds or settings.BANDIANWA_POLL_INTERVAL_SECONDS
    timeout_seconds_value = timeout_seconds or settings.BANDIANWA_POLL_TIMEOUT_SECONDS
    for task in tasks:
        try:
            _enqueue(
                task,
                interval_seconds=int(interval_seconds),
                timeout_seconds=int(timeout_seconds_value),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue AI uploaded video task",
                extra={"workspace_id": workspace_id, "local_task_id": int(task.id)},
            )
            _mark_enqueue_failed(db, task, exc)

    db.commit()
    tasks = _attach_batch_fields(tasks)
    return AiVideoBatchCreateResponse(tasks=tasks, total=len(tasks))


@router.get("/tasks", response_model=AiVideoTaskListResponse)
async def list_ai_video_tasks(
    workspace_id: int,
    page: int = 1,
    size: int = 10,
    state: Optional[str] = None,
    model: Optional[str] = None,
    refresh_pending: bool = False,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    page = max(int(page or 1), 1)
    size = max(min(int(size or 10), 100), 1)
    offset = (page - 1) * size

    q = _visible_task_query(db, int(workspace_id), me)
    if model:
        q = _filter_task_model(q, model)
    if state:
        q = q.filter(KieTask.state == state)

    total = q.count()
    items = _paged_task_rows(q, offset=offset, size=size)

    if refresh_pending:
        refreshed = 0
        for item in items:
            if refreshed >= 20:
                break
            if not _is_pending_state(item.state):
                continue
            if not _supports_inline_refresh(item):
                continue
            try:
                await refresh_bandianwa_task_status_by_task_id(
                    db,
                    workspace_id=int(workspace_id),
                    local_task_id=int(item.id),
                )
                db.commit()
                refreshed += 1
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "Failed to refresh pending AI video task",
                    extra={"workspace_id": workspace_id, "task_id": item.id},
                )
        if refreshed:
            items = _paged_task_rows(q, offset=offset, size=size)

    items = _attach_creator_fields(db, int(workspace_id), items)
    return AiVideoTaskListResponse(items=items, total=total)


@router.get("/admin/tasks", response_model=AiVideoTaskListResponse)
async def list_ai_video_admin_tasks(
    workspace_id: int,
    page: int = 1,
    size: int = 10,
    state: Optional[str] = None,
    model: Optional[str] = None,
    creator_user_id: Optional[int] = None,
    refresh_pending: bool = False,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _require_workspace_task_admin(me)

    page = max(int(page or 1), 1)
    size = max(min(int(size or 10), 100), 1)
    offset = (page - 1) * size

    q = _workspace_task_query(db, int(workspace_id))
    if model:
        q = _filter_task_model(q, model)
    if state:
        q = q.filter(KieTask.state == state)
    if creator_user_id:
        q = q.filter(KieTask.created_by_user_id == int(creator_user_id))

    total = q.count()
    items = _paged_task_rows(q, offset=offset, size=size)

    if refresh_pending:
        refreshed = 0
        for item in items:
            if refreshed >= 20:
                break
            if not _is_pending_state(item.state):
                continue
            if not _supports_inline_refresh(item):
                continue
            try:
                await refresh_bandianwa_task_status_by_task_id(
                    db,
                    workspace_id=int(workspace_id),
                    local_task_id=int(item.id),
                )
                db.commit()
                refreshed += 1
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "Failed to refresh pending AI video member task",
                    extra={
                        "workspace_id": workspace_id,
                        "task_id": item.id,
                        "actor_user_id": int(me.id),
                    },
                )
        if refreshed:
            items = _paged_task_rows(q, offset=offset, size=size)

    items = _attach_creator_fields(db, int(workspace_id), items)
    return AiVideoTaskListResponse(items=items, total=total)


@router.delete("/tasks", response_model=AiVideoClearTasksResponse)
async def clear_ai_video_tasks(
    workspace_id: int,
    model: Optional[str] = None,
    state: Optional[str] = None,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _assert_persisted_tenant_actor(db, workspace_id=workspace_id, me=me)
    q_tasks = _task_query(db, int(workspace_id), int(me.id))
    if model:
        q_tasks = _filter_task_model(q_tasks, model)
    if state:
        q_tasks = q_tasks.filter(KieTask.state == state)

    task_ids = [tid for (tid,) in q_tasks.with_entities(KieTask.id).all()]
    if not task_ids:
        return AiVideoClearTasksResponse(deleted_tasks=0, deleted_files=0)

    deleted_tasks, deleted_files, files = _delete_task_records(
        db,
        workspace_id=int(workspace_id),
        task_ids=task_ids,
    )
    db.commit()
    _delete_task_local_artifacts(files)

    logger.info(
        "Cleared AI video tasks",
        extra={
            "workspace_id": int(workspace_id),
            "model": model,
            "state": state,
            "deleted_tasks": deleted_tasks,
            "deleted_files": deleted_files,
            "actor_user_id": int(me.id),
        },
    )
    return AiVideoClearTasksResponse(deleted_tasks=deleted_tasks, deleted_files=deleted_files)


@router.post("/tasks/{task_id}/retry", response_model=AiVideoCreateResponse)
async def retry_ai_video_task(
    workspace_id: int,
    task_id: int,
    payload: Optional[AiVideoTaskRegenerateRequest] = None,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _assert_persisted_tenant_actor(db, workspace_id=workspace_id, me=me)
    task = _visible_task_query(db, int(workspace_id), me).filter(KieTask.id == int(task_id)).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _can_regenerate_task(task):
        raise HTTPException(status_code=400, detail="Only terminal tasks can be regenerated")

    override_input = payload.input_params if payload and payload.input_params else None
    if override_input is not None:
        override_input = dict(override_input)
        override_input.setdefault("model", task.model)
        if "service_provider" in override_input:
            requested_provider = str(
                override_input.get("service_provider") or "auto"
            ).strip().lower()
            override_input["routing_mode"] = (
                "auto" if requested_provider == "auto" else "pinned"
            )
            override_input["requested_service_provider"] = requested_provider
        if not str(override_input.get("prompt") or "").strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        if "reference_file_paths" in override_input:
            refs = [ref for ref in (override_input.get("reference_file_paths") or []) if isinstance(ref, dict)]
            _sync_task_reference_records(
                db,
                workspace_id=int(workspace_id),
                task=task,
                refs=refs,
            )
    if override_input is None and str(task.fail_code or "").lower() == "local_download_failed":
        files = (
            db.query(KieFile)
            .filter(
                KieFile.workspace_id == int(workspace_id),
                KieFile.task_id == int(task.id),
                KieFile.kind.in_(("result", "result_watermark")),
            )
            .order_by(KieFile.id.asc())
            .all()
        )
        if not files:
            raise HTTPException(status_code=400, detail="No result file is available for local retry")
        base_name = str((task.result_json or {}).get("__local", {}).get("download_name_base") or task.id)
        for file in files:
            file.meta_json = mark_result_file_pending(
                file,
                filename=f"{base_name}-watermark" if file.kind == "result_watermark" else base_name,
            )
            db.add(file)
        task.state = "downloading"
        task.fail_code = None
        task.fail_msg = None
        set_task_local_meta(
            task,
            download_enqueued_at=datetime.now(timezone.utc).isoformat(),
            download_error=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        _enqueue_result_download(workspace_id=int(workspace_id), local_task_id=int(task.id))
        return AiVideoCreateResponse(task=task)

    if str(task.state or "").strip().lower() == "success":
        archive_successful_task_result_files(db, task)
    else:
        delete_task_result_files(db, task)
    task = reset_video_task_for_retry(
        db,
        task=task,
        input_params=override_input,
        retry_kind="manual",
    )
    db.commit()
    db.refresh(task)
    _enqueue(
        task,
        interval_seconds=int(settings.BANDIANWA_POLL_INTERVAL_SECONDS),
        timeout_seconds=int(settings.BANDIANWA_POLL_TIMEOUT_SECONDS),
    )
    return AiVideoCreateResponse(task=task)


@router.delete("/tasks/{task_id}", response_model=AiVideoClearTasksResponse)
async def delete_ai_video_task(
    workspace_id: int,
    task_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _assert_persisted_tenant_actor(db, workspace_id=workspace_id, me=me)
    task = _visible_task_query(db, int(workspace_id), me).filter(KieTask.id == int(task_id)).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    deleted_tasks, deleted_files, files = _delete_task_records(
        db,
        workspace_id=int(workspace_id),
        task_ids=[int(task.id)],
    )
    db.commit()
    _delete_task_local_artifacts(files)
    return AiVideoClearTasksResponse(deleted_tasks=deleted_tasks, deleted_files=deleted_files)


@router.post("/tasks/batch-download")
async def batch_download_ai_video_tasks(
    workspace_id: int,
    payload: AiVideoBatchDownloadRequest,
    background_tasks: BackgroundTasks,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    task_ids = []
    seen: set[int] = set()
    for raw_id in payload.task_ids or []:
        task_id = int(raw_id)
        if task_id > 0 and task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids is required")
    if len(task_ids) > 100:
        raise HTTPException(status_code=400, detail="Batch download supports at most 100 tasks")

    visible_task_ids = [
        tid
        for (tid,) in (
            _visible_task_query(db, int(workspace_id), me)
            .filter(
                KieTask.id.in_(task_ids),
                KieTask.state == "success",
            )
            .with_entities(KieTask.id)
            .all()
        )
    ]
    if not visible_task_ids:
        raise HTTPException(status_code=404, detail="No successful tasks are available for batch download")

    rows = (
        db.query(KieFile, KieTask.id)
        .join(KieApiKey, KieFile.key_id == KieApiKey.id)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieTask.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS),
            KieTask.id.in_(visible_task_ids),
            KieFile.kind.in_(("result", "result_watermark")),
        )
        .order_by(KieTask.id.asc(), KieFile.id.asc())
        .all()
    )
    zip_path, filename, file_count = _create_batch_download_zip(
        rows,
        workspace_id=int(workspace_id),
        provider_label="ai-videos",
    )
    background_tasks.add_task(_remove_temp_file, zip_path)
    logger.info(
        "Created AI video batch video download",
        extra={
            "workspace_id": int(workspace_id),
            "actor_user_id": int(me.id),
            "task_count": len(visible_task_ids),
            "file_count": file_count,
        },
    )
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
    )


@router.get("/admin/tasks/{task_id}", response_model=AiVideoTaskOut)
async def get_ai_video_admin_task(
    workspace_id: int,
    task_id: int,
    refresh: bool = False,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _require_workspace_task_admin(me)
    task = (
        _workspace_task_query(db, int(workspace_id))
        .filter(KieTask.id == int(task_id))
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if refresh and _is_pending_state(task.state) and _supports_inline_refresh(task):
        try:
            task = await refresh_bandianwa_task_status_by_task_id(
                db,
                workspace_id=int(workspace_id),
                local_task_id=int(task.id),
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "Failed to refresh AI video member task",
                extra={
                    "workspace_id": workspace_id,
                    "local_task_id": task.id,
                    "actor_user_id": int(me.id),
                },
            )

    task = _attach_creator_fields(db, int(workspace_id), [task])[0]
    return task


@router.get("/admin/tasks/{task_id}/files", response_model=List[AiVideoFileOut])
async def list_ai_video_admin_task_files(
    workspace_id: int,
    task_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _require_workspace_task_admin(me)
    task = (
        _workspace_task_query(db, int(workspace_id))
        .filter(KieTask.id == int(task_id))
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.task_id == int(task_id),
            KieFile.kind.in_(tuple(sorted(RESULT_FILE_KINDS))),
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    return files


@router.get("/tasks/{task_id}", response_model=AiVideoTaskOut)
async def get_ai_video_task(
    workspace_id: int,
    task_id: int,
    refresh: bool = False,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    task = (
        _visible_task_query(db, int(workspace_id), me)
        .filter(KieTask.id == int(task_id))
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if refresh and _is_pending_state(task.state) and _supports_inline_refresh(task):
        try:
            task = await refresh_bandianwa_task_status_by_task_id(
                db,
                workspace_id=int(workspace_id),
                local_task_id=int(task.id),
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "Failed to refresh AI video task",
                extra={"workspace_id": workspace_id, "local_task_id": task.id},
            )

    task = _attach_creator_fields(db, int(workspace_id), [task])[0]
    return task


@router.get("/tasks/{task_id}/batch", response_model=List[AiVideoTaskOut])
async def get_ai_video_task_batch(
    workspace_id: int,
    task_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    task = (
        _visible_task_query(db, int(workspace_id), me)
        .filter(KieTask.id == int(task_id))
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    meta = _batch_meta(task)
    root_id = int(meta.get("batch_root_task_id") or task.id)
    batch_size = int(meta.get("batch_size") or 1)
    if batch_size <= 1:
        tasks = [task]
    else:
        start_id = max(root_id - batch_size + 1, 1)
        tasks = (
            _visible_task_query(db, int(workspace_id), me)
            .filter(
                KieTask.id >= start_id,
                KieTask.id <= root_id,
                KieTask.model == task.model,
            )
            .order_by(KieTask.id.asc())
            .all()
        )
        tasks = [
            item
            for item in tasks
            if int(_batch_meta(item).get("batch_root_task_id") or item.id) == root_id
        ]
        if not tasks:
            tasks = [task]

    tasks = _attach_creator_fields(db, int(workspace_id), tasks)
    return sorted(tasks, key=lambda item: (item.batch_index or 999999, int(item.id)))


@router.get("/tasks/{task_id}/files", response_model=List[AiVideoFileOut])
async def list_ai_video_task_files(
    workspace_id: int,
    task_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    task = _visible_task_query(db, int(workspace_id), me).filter(KieTask.id == int(task_id)).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.task_id == int(task_id),
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    return files


@router.get("/public-reference/{task_id}/{file_id}")
async def download_public_reference_file(
    workspace_id: int,
    task_id: int,
    file_id: int,
    expires: int = Query(..., gt=0),
    signature: str = Query(..., min_length=64, max_length=64),
    db: Session = Depends(get_db),
):
    """Serve an uploaded reference to an upstream provider during generation."""
    if not verify_reference_capability(
        workspace_id,
        task_id,
        file_id,
        expires=expires,
        signature=signature,
    ):
        raise HTTPException(status_code=403, detail="Reference capability is invalid or expired")
    file = (
        db.query(KieFile)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.id == int(file_id),
            KieFile.task_id == int(task_id),
            KieFile.workspace_id == int(workspace_id),
            KieFile.kind == "reference_upload",
            KieTask.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if file is None:
        raise HTTPException(status_code=404, detail="Reference file not found")

    local_path = get_local_path(file)
    if local_path is None:
        try:
            resolved = Path(str(file.file_url or "")).expanduser().resolve()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="Reference file not found") from exc
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Reference file not found")
        managed = resolve_managed_file(resolved, managed_reference_roots())
        if managed is None:
            raise HTTPException(status_code=403, detail="Reference file path is not allowed")
        local_path = managed

    meta = file.meta_json if isinstance(file.meta_json, dict) else {}
    return FileResponse(
        path=str(local_path),
        media_type=file.mime_type or "image/jpeg",
        filename=str(meta.get("filename") or local_path.name),
    )


@router.get("/files/{file_id}/download-url", response_model=str)
async def get_ai_video_file_download_url(
    workspace_id: int,
    file_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    q = (
        db.query(KieFile, KieTask.created_by_user_id)
        .join(KieApiKey, KieFile.key_id == KieApiKey.id)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.id == int(file_id),
            KieFile.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS),
            KieTask.workspace_id == int(workspace_id),
        )
    )
    q = q.filter(KieTask.created_by_user_id == int(me.id))
    row = q.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    file, owner_user_id = row
    if not file.file_url:
        raise HTTPException(status_code=502, detail="File URL is empty")
    if get_local_path(file) is not None:
        return _local_download_url(workspace_id, file_id)
    return file.file_url


@router.get("/files/{file_id}/download")
async def download_ai_video_file(
    workspace_id: int,
    file_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    q = (
        db.query(KieFile)
        .join(KieApiKey, KieFile.key_id == KieApiKey.id)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.id == int(file_id),
            KieFile.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS),
            KieTask.workspace_id == int(workspace_id),
        )
    )
    q = q.filter(KieTask.created_by_user_id == int(me.id))
    file = q.one_or_none()
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")

    local_path = get_local_path(file)
    if local_path is None:
        raise HTTPException(status_code=404, detail="Local file not found")

    filename = local_path.name
    meta = file.meta_json or {}
    if isinstance(meta, dict) and meta.get("filename"):
        filename = str(meta.get("filename"))
    return FileResponse(
        path=str(local_path),
        media_type=file.mime_type or "video/mp4",
        filename=filename,
    )


@router.get("/admin/files/{file_id}/download-url", response_model=str)
async def get_ai_video_admin_file_download_url(
    workspace_id: int,
    file_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _require_workspace_task_admin(me)
    row = (
        db.query(KieFile, KieTask.created_by_user_id)
        .join(KieApiKey, KieFile.key_id == KieApiKey.id)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.id == int(file_id),
            KieFile.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS),
            KieTask.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    file, owner_user_id = row
    if not file.file_url:
        raise HTTPException(status_code=502, detail="File URL is empty")
    if get_local_path(file) is not None:
        return _local_download_url(workspace_id, file_id, admin=True)
    return file.file_url


@router.get("/admin/files/{file_id}/download")
async def download_ai_video_admin_file(
    workspace_id: int,
    file_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _require_workspace_task_admin(me)
    file = (
        db.query(KieFile)
        .join(KieApiKey, KieFile.key_id == KieApiKey.id)
        .join(KieTask, KieFile.task_id == KieTask.id)
        .filter(
            KieFile.id == int(file_id),
            KieFile.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(VIDEO_PROVIDER_KEYS),
            KieTask.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")

    local_path = get_local_path(file)
    if local_path is None:
        raise HTTPException(status_code=404, detail="Local file not found")

    filename = local_path.name
    meta = file.meta_json or {}
    if isinstance(meta, dict) and meta.get("filename"):
        filename = str(meta.get("filename"))
    return FileResponse(
        path=str(local_path),
        media_type=file.mime_type or "video/mp4",
        filename=filename,
    )
