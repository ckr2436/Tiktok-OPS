# app/services/kie_api/tasks.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Dict, Optional

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieTask, KieFile
from app.services.kie_api.accounts import decrypt_api_key
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.audit import log_event

# 当前支持的所有 Sora2 模型
SORA2_MODELS: set[str] = {
    "sora-2-text-to-video",
    "sora-2-pro-text-to-video",
    "sora-2-image-to-video",
    "sora-2-pro-image-to-video",
    "sora-2-pro-storyboard",
    "sora-watermark-remover",
}


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
    """
    确保本地 KieFile 存在（幂等）：
    - 以 workspace_id + key_id + task_id + file_url + kind 唯一
    - 已存在则更新 mime_type/size_bytes（如果有变化）
    """
    file_url = (file_url or "").strip()
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
    model: str,
    input_params: Mapping[str, Any],
    key_id: int,
    callback_url: str | None = None,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieTask:
    """
    创建任意 Sora2 任务（统一走 /api/v1/jobs/createTask），并在本地落一条 KieTask 记录。

    - model 必须是 SORA2_MODELS 中的一种
    - 输入校验由上层 router 完成，这里只做最小处理（过滤 None 等）
    - key_id 为使用的平台级 KIE API key 主键
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    if model not in SORA2_MODELS:
        raise ValueError(f"Unsupported Sora2 model: {model}")

    key: Optional[KieApiKey] = (
        db.query(KieApiKey)
        .filter(
            KieApiKey.id == int(key_id),
            KieApiKey.is_active.is_(True),
        )
        .one_or_none()
    )
    if key is None:
        raise ValueError(f"KIE API key not found or inactive: {key_id}")

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 过滤掉 None
    input_clean: Dict[str, Any] = {
        k: v for k, v in (input_params or {}).items() if v is not None
    }

    # 调用 createTask
    try:
        resp = await client.create_image_to_video_task(
            model=model,
            input_data=input_clean,
            callback_url=callback_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise KieApiError(f"KIE createTask error: {exc}") from exc

    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"createTask failed: code={code}, msg={resp.get('msg')}")

    data = resp.get("data") or {}
    task_id = (
        data.get("taskId")
        or data.get("task_id")
        or resp.get("taskId")
        or resp.get("task_id")
    )
    if not task_id:
        raise KieApiError("createTask response missing taskId")

    # prompt 摘要
    prompt_summary: Optional[str] = None
    raw_prompt = input_clean.get("prompt")
    if isinstance(raw_prompt, str) and raw_prompt.strip():
        prompt_summary = raw_prompt.strip()[:2000]
    elif model == "sora-watermark-remover":
        vu = input_clean.get("video_url")
        if isinstance(vu, str) and vu.strip():
            prompt_summary = vu.strip()[:2000]
    elif model == "sora-2-pro-storyboard":
        # 从 shots 中抽一点场景文案
        shots = input_clean.get("shots") or []
        if isinstance(shots, (list, tuple)):
            parts: list[str] = []
            for shot in shots:
                if not isinstance(shot, Mapping):
                    continue
                scene = str(
                    shot.get("Scene")
                    or shot.get("scene")
                    or ""
                ).strip()
                if scene:
                    parts.append(scene)
                if len(parts) >= 2:
                    break
            if parts:
                prompt_summary = " | ".join(parts)[:2000]

    task = KieTask(
        workspace_id=int(workspace_id),
        key_id=int(key.id),
        model=model,
        task_id=str(task_id),
        state="waiting",
        prompt=prompt_summary,
        input_json=input_clean,
    )
    db.add(task)
    db.flush()

    log_event(
        db,
        action="kie.sora2.create_task",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id or int(workspace_id),
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=int(workspace_id),
        details={
            "model": model,
            "task_id": task.task_id,
            "callback_url": callback_url,
            "request": input_clean,
            "raw_response": resp,
            "key_id": int(key.id),
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
    通过 /api/v1/jobs/recordInfo 刷新某个任务的最新状态，
    并同步 resultUrls / resultWaterMarkUrls 到 KieFile。
    """
    if key is None:
        key = (
            db.query(KieApiKey)
            .filter(KieApiKey.id == task.key_id)
            .one_or_none()
        )
        if key is None:
            raise ValueError("Associated KIE API key not found")

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    resp = await client.get_task_record(task_id=task.task_id)
    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"recordInfo failed: code={code}, msg={resp.get('msg')}")

    data = resp.get("data") or {}

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

        def _as_list(v: Any) -> list:
            if v is None:
                return []
            if isinstance(v, (list, tuple, set)):
                return list(v)
            return [v]

        result_urls = _as_list(parsed_result.get("resultUrls"))
        wm_urls = _as_list(parsed_result.get("resultWaterMarkUrls"))

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
        resource_id=int(task.id),
        actor_user_id=None,
        actor_workspace_id=int(task.workspace_id),
        workspace_id=int(task.workspace_id),
        details={
            "task_id": task.task_id,
            "model": task.model,
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
    """
    根据本地自增 ID 查找 KieTask，然后调用 refresh_sora2_task_status。
    """
    task = (
        db.query(KieTask)
        .filter(
            KieTask.id == int(local_task_id),
            KieTask.workspace_id == int(workspace_id),
        )
        .one_or_none()
    )
    if task is None:
        raise ValueError("KIE task not found")

    return await refresh_sora2_task_status(db, task=task)


__all__ = [
    "SORA2_MODELS",
    "create_sora2_task",
    "refresh_sora2_task_status",
    "refresh_sora2_task_status_by_task_id",
]

