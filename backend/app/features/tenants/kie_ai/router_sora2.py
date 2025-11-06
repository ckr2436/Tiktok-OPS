# app/features/tenants/kie_ai/router_sora2.py
from __future__ import annotations

from typing import List, Optional

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
from app.services.kie_api.accounts import get_effective_key, decrypt_api_key
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.kie_api.tasks import (
    create_sora2_task,
    refresh_sora2_task_status_by_task_id,
)
from app.services.kie_api.common import refresh_download_url_for_file

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/kie-ai",
    tags=["Tenant / Kie AI (Sora2)"],
)


class Sora2TaskOut(BaseModel):
    id: int
    workspace_id: int
    model: str
    task_id: str
    state: str
    prompt: Optional[str] = None

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


MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB
ASPECT_RATIOS_ALLOWED: set[str] = {"portrait", "landscape"}  # 官方示例：竖屏/横屏


def _validate_image_file(upload: UploadFile) -> None:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image/* files are supported",
        )


@router.post(
    "/sora2/image-to-video",
    response_model=Sora2CreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sora2_image_to_video(
    workspace_id: int,
    prompt: str = Form(..., min_length=1, max_length=10_000),
    aspect_ratio: str = Form(
        "landscape",
        description="画幅比例：portrait（竖屏）或 landscape（横屏）",
    ),
    n_frames: Optional[int] = Form(
        None,
        description="视频时长（秒）：只能 10 或 15；为空则使用 KIE 默认值",
    ),
    remove_watermark: bool = Form(
        True,
        description="是否去除水印（true/false）",
    ),
    image: UploadFile = File(...),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    租户成员/管理员：上传一张图片 + prompt → 触发 sora-2-image-to-video 任务。
    参数规则严格按官方文档：
    - prompt: 文本提示，最多 10000 字符
    - aspect_ratio: portrait / landscape
    - n_frames: 10 或 15（秒），底层会按字符串 "10"/"15" 传给 KIE
    - remove_watermark: 布尔
    """
    # require_tenant_member 已经校验：
    # - 当前登录用户是该 workspace 成员
    # - 并且不是平台管理员

    # 1) 参数合法性校验（在进 KIE 之前拦截）
    if aspect_ratio not in ASPECT_RATIOS_ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"aspect_ratio must be one of {sorted(ASPECT_RATIOS_ALLOWED)}",
        )

    if n_frames is not None and n_frames not in (10, 15):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="n_frames must be 10 or 15 seconds",
        )

    _validate_image_file(image)

    file_bytes = await image.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(file_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    # 平台默认 key（租户侧不能选 key）
    key = get_effective_key(db, key_id=None, require_active=True)
    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 上传文件到 KIE，拿 fileUrl
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

    # 兼容多种返回格式：既检查 data，又检查顶层
    raw = upload_resp or {}
    candidates: list[dict] = []
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
        # 把原始响应也带回去，方便排查（上线后可以改成只打日志）
        raise HTTPException(
            status_code=502,
            detail={
                "error": "KIE upload response missing usable fileUrl",
                "raw": upload_resp,
            },
        )

    # 本地记录上传文件
    upload_file = KieFile(
        workspace_id=int(workspace_id),
        key_id=int(key.id),
        task_id=None,
        file_url=file_url,
        kind="upload",
        mime_type=image.content_type,
        size_bytes=len(file_bytes),
    )
    db.add(upload_file)
    db.flush()

    # 创建 sora2 任务（使用上传后的 fileUrl）
    task = await create_sora2_task(
        db,
        workspace_id=int(workspace_id),
        input_params={
            "prompt": prompt,
            "image_urls": [file_url],
            "aspect_ratio": aspect_ratio,
            # 官方要求是字符串 "10"/"15"，这里转一下；None 会在服务层被剔除
            "n_frames": str(n_frames) if n_frames is not None else None,
            "remove_watermark": remove_watermark,
            # 将来如果启用回调，可在这里加 "callBackUrl": settings.KIE_SORA2_CALLBACK_URL
        },
        key_id=int(key.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )

    # 把上传文件和任务关联起来
    upload_file.task_id = task.id
    db.add(upload_file)
    db.flush()

    return Sora2CreateResponse(
        task=task,
        upload_file=upload_file,
    )


@router.get(
    "/sora2/tasks/{task_id}",
    response_model=Sora2TaskOut,
)
async def get_sora2_task(
    workspace_id: int,
    task_id: int,
    refresh: bool = True,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """
    租户成员/管理员：查询本地任务状态；可选是否实时刷新一次 KIE recordInfo。
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

    if refresh:
        task = await refresh_sora2_task_status_by_task_id(
            db,
            workspace_id=int(workspace_id),
            local_task_id=int(task_id),
        )

    return task


@router.get(
    "/sora2/tasks/{task_id}/files",
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
    租户成员/管理员：把 KIE 文件 URL 换成 20 分钟有效的下载 URL。
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

