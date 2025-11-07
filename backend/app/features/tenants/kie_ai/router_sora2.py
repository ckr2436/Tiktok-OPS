# app/features/tenants/kie_ai/router_sora2.py
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    UploadFile,
    HTTPException,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_tenant_member, SessionUser
from app.data.db import get_db
from app.data.models.kie_api import KieTask, KieFile
from app.services.kie_api.accounts import decrypt_api_key
from app.services.kie_api.common import (
    refresh_download_url_for_file,
    select_best_kie_key_for_task,
)
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.kie_api.tasks import (
    create_sora2_task,
)
from app.tasks.kie_ai.sora.sora2_image_to_video_tasks import (
    poll_sora2_task_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/kie-ai/sora2",
    tags=["Tenant / Kie AI (Sora2)"],
)


class Sora2TaskOut(BaseModel):
    id: int
    workspace_id: int
    model: str
    task_id: str
    state: str
    prompt: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class KieFileOut(BaseModel):
    id: int
    kind: str
    file_url: str
    download_url: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None

    class Config:
        from_attributes = True


class Sora2CreateResponse(BaseModel):
    task: Sora2TaskOut
    upload_file: Optional[KieFileOut] = None


class Sora2TaskListResponse(BaseModel):
    items: List[Sora2TaskOut]
    total: int


MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB
ASPECT_RATIOS_ALLOWED: set[str] = {"portrait", "landscape"}
DURATIONS_STD: set[int] = {10, 15}
DURATIONS_STORYBOARD: set[int] = {10, 15, 25}


def _validate_image_file(upload: UploadFile) -> None:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image/* files are supported",
        )


async def _upload_image_to_kie(
    *,
    db: Session,
    workspace_id: int,
    key_id: int,
    image: UploadFile,
) -> KieFile:
    """
    上传图片到 KIE，返回对应的 KieFile 记录（kind=upload）。
    """
    _validate_image_file(image)

    file_bytes = await image.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(file_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    # 取 key & client（此处 key 已经在 select_best_kie_key_for_task 中保证 is_active）
    from app.data.models.kie_api import KieApiKey

    key = (
        db.query(KieApiKey)
        .filter(
            KieApiKey.id == int(key_id),
        )
        .one_or_none()
    )
    if key is None:
        raise HTTPException(status_code=500, detail="KIE API key not found")

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 上传
    try:
        upload_resp = await client.upload_file_stream(
            filename=image.filename or "upload",
            file_bytes=file_bytes,
            upload_path=f"workspace-{workspace_id}",
            file_name=image.filename or "upload",
            mime_type=image.content_type or "application/octet-stream",
        )
    except KieApiError as e:
        raise HTTPException(
            status_code=502,
            detail=f"KIE upload error: {e}",
        ) from e

    raw = upload_resp or {}
    candidates: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        data_obj = raw.get("data")
        if isinstance(data_obj, dict):
            candidates.append(data_obj)
        candidates.append(raw)

    file_url: Optional[str] = None
    for obj in candidates:
        for key_name in ("fileUrl", "file_url", "url", "downloadUrl", "download_url"):
            v = obj.get(key_name)
            if isinstance(v, str) and v.strip():
                file_url = v.strip()
                break
        if file_url:
            break

    if not file_url:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "KIE upload response missing usable fileUrl",
                "raw": upload_resp,
            },
        )

    upload_file = KieFile(
        workspace_id=int(workspace_id),
        key_id=int(key_id),
        task_id=None,
        file_url=file_url,
        kind="upload",
        mime_type=image.content_type,
        size_bytes=len(file_bytes),
    )
    db.add(upload_file)
    db.flush()
    return upload_file


async def _select_key_for_task(db: Session) -> int:
    """
    统一选择一个适合当前任务的 KIE key，返回 key_id。
    """
    try:
        key = await select_best_kie_key_for_task(db)
    except Exception:  # noqa: BLE001
        logger.exception("select_best_kie_key_for_task failed, fallback to default")
        from app.services.kie_api.accounts import get_effective_key

        key = get_effective_key(db, key_id=None, require_active=True)
    return int(key.id)


def _validate_aspect_ratio(aspect_ratio: str) -> str:
    ar = (aspect_ratio or "").strip() or "portrait"
    if ar not in ASPECT_RATIOS_ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"aspect_ratio must be one of {sorted(ASPECT_RATIOS_ALLOWED)}",
        )
    return ar


def _validate_duration(n_frames: Optional[int], *, storyboard: bool = False) -> Optional[int]:
    if n_frames is None:
        return None
    try:
        n = int(n_frames)
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="n_frames must be integer",
        )
    allowed = DURATIONS_STORYBOARD if storyboard else DURATIONS_STD
    if n not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"n_frames must be one of {sorted(allowed)}",
        )
    return n


# ---------------------- 创建任务：Text / Image / Pro / Storyboard ----------------------


@router.post(
    "/text-to-video",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_text_to_video(
    workspace_id: int,
    prompt: str = Form(..., min_length=1, max_length=10_000),
    aspect_ratio: str = Form("portrait"),
    n_frames: Optional[int] = Form(None),
    remove_watermark: bool = Form(
        True,
        description="是否去除水印（true/false）",
    ),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    aspect_ratio = _validate_aspect_ratio(aspect_ratio)
    n_frames = _validate_duration(n_frames, storyboard=False)

    key_id = await _select_key_for_task(db)

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-2-text-to-video",
        input_params={
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "n_frames": str(n_frames) if n_frames is not None else None,
            "remove_watermark": bool(remove_watermark),
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    # 丢给 Celery 轮询
    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (text-to-video)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=None)


@router.post(
    "/pro-text-to-video",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_pro_text_to_video(
    workspace_id: int,
    prompt: str = Form(..., min_length=1, max_length=10_000),
    aspect_ratio: str = Form("portrait"),
    n_frames: Optional[int] = Form(None),
    size: str = Form(
        "standard",
        description="画质 / 规格：standard / high",
    ),
    remove_watermark: bool = Form(
        True,
        description="是否去除水印（true/false）",
    ),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    aspect_ratio = _validate_aspect_ratio(aspect_ratio)
    n_frames = _validate_duration(n_frames, storyboard=False)

    size = (size or "standard").strip()
    if size not in {"standard", "high"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="size must be 'standard' or 'high'",
        )

    key_id = await _select_key_for_task(db)

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-2-pro-text-to-video",
        input_params={
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "n_frames": str(n_frames) if n_frames is not None else None,
            "remove_watermark": bool(remove_watermark),
            "size": size,
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (pro-text-to-video)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=None)


@router.post(
    "/image-to-video",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_image_to_video(
    workspace_id: int,
    prompt: str = Form(..., min_length=1, max_length=10_000),
    aspect_ratio: str = Form("landscape"),
    n_frames: Optional[int] = Form(None),
    remove_watermark: bool = Form(
        True,
        description="是否去除水印（true/false）",
    ),
    image: UploadFile = File(...),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    aspect_ratio = _validate_aspect_ratio(aspect_ratio)
    n_frames = _validate_duration(n_frames, storyboard=False)

    key_id = await _select_key_for_task(db)
    upload_file = await _upload_image_to_kie(
        db=db,
        workspace_id=int(workspace_id),
        key_id=key_id,
        image=image,
    )

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-2-image-to-video",
        input_params={
            "prompt": prompt,
            "image_urls": [upload_file.file_url],
            "aspect_ratio": aspect_ratio,
            "n_frames": str(n_frames) if n_frames is not None else None,
            "remove_watermark": bool(remove_watermark),
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    # 反向关联上传文件和任务
    upload_file.task_id = task.id
    db.add(upload_file)
    db.flush()

    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (image-to-video)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=upload_file)


@router.post(
    "/pro-image-to-video",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_pro_image_to_video(
    workspace_id: int,
    prompt: str = Form(..., min_length=1, max_length=10_000),
    aspect_ratio: str = Form("landscape"),
    n_frames: Optional[int] = Form(None),
    size: str = Form(
        "standard",
        description="画质 / 规格：standard / high",
    ),
    remove_watermark: bool = Form(
        True,
        description="是否去除水印（true/false）",
    ),
    image: UploadFile = File(...),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    aspect_ratio = _validate_aspect_ratio(aspect_ratio)
    n_frames = _validate_duration(n_frames, storyboard=False)

    size = (size or "standard").strip()
    if size not in {"standard", "high"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="size must be 'standard' or 'high'",
        )

    key_id = await _select_key_for_task(db)
    upload_file = await _upload_image_to_kie(
        db=db,
        workspace_id=int(workspace_id),
        key_id=key_id,
        image=image,
    )

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-2-pro-image-to-video",
        input_params={
            "prompt": prompt,
            "image_urls": [upload_file.file_url],
            "aspect_ratio": aspect_ratio,
            "n_frames": str(n_frames) if n_frames is not None else None,
            "remove_watermark": bool(remove_watermark),
            "size": size,
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    upload_file.task_id = task.id
    db.add(upload_file)
    db.flush()

    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (pro-image-to-video)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=upload_file)


@router.post(
    "/pro-storyboard",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_pro_storyboard(
    workspace_id: int,
    aspect_ratio: str = Form("portrait"),
    n_frames: int = Form(..., description="总时长：10 / 15 / 25 秒"),
    shots: str = Form(
        ...,
        description="分镜 JSON 数组：[{\"Scene\": \"...\", \"duration\": 7.5}, ...]",
    ),
    image: Optional[UploadFile] = File(
        None,
        description="可选参考图（PNG/JPG，≤ 20MB）",
    ),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    aspect_ratio = _validate_aspect_ratio(aspect_ratio)
    n_frames = _validate_duration(n_frames, storyboard=True)

    # 解析 shots JSON
    try:
        shots_data = json.loads(shots)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"shots must be valid JSON: {exc}",
        ) from exc

    if not isinstance(shots_data, list) or not shots_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="shots must be a non-empty array",
        )

    normalized_shots: list[dict[str, Any]] = []
    for idx, item in enumerate(shots_data):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"shots[{idx}] must be an object",
            )
        scene = str(
            item.get("Scene")
            or item.get("scene")
            or ""
        ).strip()
        if not scene:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"shots[{idx}].Scene is required",
            )
        try:
            duration = float(item.get("duration"))
        except Exception:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"shots[{idx}].duration must be number",
            )
        normalized_shots.append(
            {
                "Scene": scene,
                "duration": duration,
            },
        )

    key_id = await _select_key_for_task(db)

    upload_file: Optional[KieFile] = None
    image_urls: list[str] = []
    if image is not None:
        upload_file = await _upload_image_to_kie(
            db=db,
            workspace_id=int(workspace_id),
            key_id=key_id,
            image=image,
        )
        image_urls.append(upload_file.file_url)

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-2-pro-storyboard",
        input_params={
            "n_frames": str(n_frames),
            "aspect_ratio": aspect_ratio,
            "image_urls": image_urls or None,
            "shots": normalized_shots,
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    if upload_file is not None:
        upload_file.task_id = task.id
        db.add(upload_file)
        db.flush()

    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (pro-storyboard)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=upload_file)


# ---------------------- 去水印：单链接，一个任务 ----------------------


@router.post(
    "/watermark-remover",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_watermark_remover(
    workspace_id: int,
    video_url: str = Form(..., min_length=1),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    单个 Sora 分享链接 → 去水印任务。

    批量模式在前端实现：
    - 文本框中按回车分割多行
    - 浏览器内控制并发：同时最多发起 10 个请求，其余排队
    """
    url = video_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="video_url is empty")

    key_id = await _select_key_for_task(db)

    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        model="sora-watermark-remover",
        input_params={
            "video_url": url,
        },
        key_id=key_id,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    try:
        poll_sora2_task_status.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(task.id),
            },
            queue="gmv.tasks.kie_ai",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue poll_sora2_task_status (watermark-remover)",
            extra={"workspace_id": workspace_id, "local_task_id": task.id},
        )

    return Sora2CreateResponse(task=task, upload_file=None)


# ---------------------- 任务查询 & 文件下载 ----------------------


@router.get(
    "/tasks",
    response_model=Sora2TaskListResponse,
)
async def list_sora2_tasks(
    workspace_id: int,
    page: int = 1,
    size: int = 10,
    state: Optional[str] = None,
    model: Optional[str] = None,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    租户成员/管理员：分页查询本 workspace 下的 Sora2 任务列表。

    - GET /tenants/{wid}/kie-ai/sora2/tasks?page=&size=&state=&model=
    - model 为空则返回所有 Sora2 相关任务
    """
    page = max(int(page or 1), 1)
    size = max(min(int(size or 10), 100), 1)
    offset = (page - 1) * size

    q = db.query(KieTask).filter(KieTask.workspace_id == int(workspace_id))

    if model:
        q = q.filter(KieTask.model == model)

    if state:
        q = q.filter(KieTask.state == state)

    total = q.count()
    items = (
        q.order_by(KieTask.id.desc())
        .offset(offset)
        .limit(size)
        .all()
    )

    return Sora2TaskListResponse(items=items, total=total)


@router.get(
    "/tasks/{task_id}",
    response_model=Sora2TaskOut,
)
async def get_sora2_task(
    workspace_id: int,
    task_id: int,
    refresh: bool = False,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    查询本地任务状态。
    refresh=true 时，只触发一次 Celery 轮询任务（不等待）。
    """
    task = (
        db.query(KieTask)
        .filter(
            KieTask.id == int(task_id),
            KieTask.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if refresh and (task.state or "").lower() not in {
        "success",
        "failed",
        "error",
        "timeout",
    }:
        try:
            poll_sora2_task_status.apply_async(
                kwargs={
                    "workspace_id": int(workspace_id),
                    "local_task_id": int(task.id),
                },
                queue="gmv.tasks.kie_ai",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue poll_sora2_task_status from get_sora2_task",
                extra={"workspace_id": workspace_id, "local_task_id": task.id},
            )

    return task


@router.get(
    "/tasks/{task_id}/files",
    response_model=List[KieFileOut],
)
async def list_task_files(
    workspace_id: int,
    task_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
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


@router.get(
    "/files/{file_id}/download-url",
    response_model=str,
)
async def get_file_download_url(
    workspace_id: int,
    file_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    把 KIE 文件 URL 换成 20 分钟有效的下载 URL。
    """
    file = (
        db.query(KieFile)
        .filter(
            KieFile.id == int(file_id),
            KieFile.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")

    url = await refresh_download_url_for_file(
        db,
        file=file,
    )
    return url

