# app/services/kie_api/tasks.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieTask, KieFile
from app.services.kie_api.accounts import (
    get_effective_key,
    decrypt_api_key,
)
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.audit import log_event


def _ms_to_dt(ms: Any) -> datetime | None:
    if ms is None:
        return None
    try:
        ms_int = int(ms)
    except Exception:  # noqa: BLE001
        return None
    return datetime.fromtimestamp(ms_int / 1000.0, tz=timezone.utc)


def _ensure_kie_file(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    task_pk: int | None,
    file_url: str,
    kind: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> KieFile:
    file_url = file_url.strip()
    if not file_url:
        raise ValueError("file_url cannot be empty")

    existing = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == workspace_id,
            KieFile.key_id == key_id,
            KieFile.task_id == task_pk,
            KieFile.file_url == file_url,
            KieFile.kind == kind,
        )
        .one_or_none()
    )
    if existing:
        # 更新一下 mime / size（如果有）
        updated = False
        if mime_type and existing.mime_type != mime_type:
            existing.mime_type = mime_type
            updated = True
        if size_bytes is not None and existing.size_bytes != size_bytes:
            existing.size_bytes = size_bytes
            updated = True
        if updated:
            db.add(existing)
        return existing

    f = KieFile(
        workspace_id=workspace_id,
        key_id=key_id,
        task_id=task_pk,
        file_url=file_url,
        kind=kind,
        mime_type=mime_type,
        size_bytes=size_bytes,
    )
    db.add(f)
    return f


async def create_sora2_task(
    db: Session,
    *,
    workspace_id: int,
    input_params: Mapping[str, Any],
    key_id: int | None = None,
    callback_url: str | None = None,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieTask:
    """
    创建 sora-2-image-to-video 任务，并在本地落一条 KieTask 记录。
    key 从平台配置中挑选（key_id 可选，默认用 is_default）。
    """
    # 选 key & client
    key: KieApiKey = get_effective_key(db, key_id=key_id, require_active=True)
    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 参数整理
    prompt = str(input_params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    image_urls = input_params.get("image_urls")
    if not isinstance(image_urls, (list, tuple)) or not image_urls:
        raise ValueError("image_urls must be a non-empty list of URLs")

    aspect_ratio = input_params.get("aspect_ratio")
    n_frames = input_params.get("n_frames")
    remove_watermark = input_params.get("remove_watermark")

    payload_input: dict[str, Any] = {
        "prompt": prompt,
        "image_urls": list(image_urls),
    }
    if aspect_ratio:
        payload_input["aspect_ratio"] = str(aspect_ratio)
    if n_frames is not None:
        payload_input["n_frames"] = str(n_frames)
    if remove_watermark is not None:
        payload_input["remove_watermark"] = bool(remove_watermark)

    # 调用 createTask
    resp = await client.create_image_to_video_task(
        model="sora-2-image-to-video",
        input_data=payload_input,
        callback_url=callback_url,
    )
    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"createTask failed: code={code}, msg={resp.get('msg')}")

    data = resp.get("data") or {}
    task_id = data.get("taskId") or data.get("task_id")
    if not task_id:
        raise KieApiError("createTask response missing taskId")

    task = KieTask(
        workspace_id=workspace_id,
        key_id=key.id,
        model="sora-2-image-to-video",
        task_id=str(task_id),
        state="waiting",
        prompt=prompt[:2000],
        input_json=dict(payload_input),
    )
    db.add(task)
    db.flush()

    log_event(
        db,
        action="kie.sora2.create_task",
        resource_type="kie_task",
        resource_id=task.id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id or workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=workspace_id,
        details={
            "model": "sora-2-image-to-video",
            "task_id": task.task_id,
            "callback_url": callback_url,
            "request": payload_input,
            "raw_response": resp,
        },
    )

    return task


async def refresh_sora2_task_status(
    db: Session,
    *,
    task: KieTask,
    key: KieApiKey | None = None,
) -> KieTask:
    """
    通过 /api/v1/jobs/recordInfo 刷新某个任务的最新状态，并同步 resultUrls 到 KieFile。
    """
    if key is None:
        key = db.query(KieApiKey).filter(KieApiKey.id == task.key_id).one_or_none()
        if key is None:
            raise ValueError("Associated KIE API key not found")

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    resp = await client.get_task_record(task_id=task.task_id)
    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"recordInfo failed: code={code}, msg={resp.get('msg')}")

    data = resp.get("data") or {}

    # 状态
    state = data.get("state")
    if state:
        task.state = str(state)

    fail_code = data.get("failCode")
    fail_msg = data.get("failMsg")
    if fail_code:
        task.fail_code = str(fail_code)
    if fail_msg:
        task.fail_msg = str(fail_msg)

    task.external_create_time = _ms_to_dt(data.get("createTime"))
    task.external_complete_time = _ms_to_dt(data.get("completeTime"))

    # resultJson 解析
    result_raw = data.get("resultJson")
    parsed_result: dict[str, Any] | None = None
    if result_raw:
        if isinstance(result_raw, dict):
            parsed_result = result_raw
        else:
            try:
                parsed_result = json.loads(result_raw)
            except Exception:  # noqa: BLE001
                parsed_result = {"__raw__": result_raw}
    if parsed_result is not None:
        task.result_json = parsed_result

        result_urls = parsed_result.get("resultUrls") or []
        wm_urls = parsed_result.get("resultWaterMarkUrls") or []
        for url in result_urls:
            _ensure_kie_file(
                db,
                workspace_id=task.workspace_id,
                key_id=key.id,
                task_pk=task.id,
                file_url=str(url),
                kind="result",
            )
        for url in wm_urls:
            _ensure_kie_file(
                db,
                workspace_id=task.workspace_id,
                key_id=key.id,
                task_pk=task.id,
                file_url=str(url),
                kind="result_watermark",
            )

    db.add(task)
    db.flush()

    log_event(
        db,
        action="kie.sora2.refresh_task",
        resource_type="kie_task",
        resource_id=task.id,
        actor_user_id=None,
        actor_workspace_id=task.workspace_id,
        workspace_id=task.workspace_id,
        details={
            "task_id": task.task_id,
            "state": task.state,
            "fail_code": task.fail_code,
            "fail_msg": task.fail_msg,
        },
    )

    return task


async def refresh_sora2_task_status_by_task_id(
    db: Session,
    *,
    workspace_id: int,
    local_task_id: int,
) -> KieTask:
    task = (
        db.query(KieTask)
        .filter(
            KieTask.id == local_task_id,
            KieTask.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if task is None:
        raise ValueError("KIE task not found")

    return await refresh_sora2_task_status(db, task=task)


__all__ = [
    "create_sora2_task",
    "refresh_sora2_task_status",
    "refresh_sora2_task_status_by_task_id",
]

