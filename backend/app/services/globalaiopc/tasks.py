from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.audit import log_event
from app.services.globalaiopc.client import (
    GlobalAiOpcApiError,
    GlobalAiOpcVideoClient,
    extract_error,
    extract_status,
    extract_task_id,
    extract_video_urls,
    normalize_submit_path,
)
from app.services.kie_api.accounts import GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY, decrypt_api_key
from app.services.kie_api.local_storage import get_local_path, mark_result_file_pending, set_task_local_meta
from app.services.kie_api.retry_policy import next_retry_meta


LOCAL_TASK_PREFIX = "local-globalaiopc-"


def _effective_prompt(input_params: Mapping[str, Any]) -> str:
    for key in ("provider_prompt", "full_prompt", "prompt"):
        value = input_params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def choose_submit_path(input_params: Mapping[str, Any]) -> str:
    raw = str(input_params.get("submit_path") or input_params.get("__submit_path") or "").strip()
    if raw:
        return normalize_submit_path(raw)

    return "/v1/omni-flash/videos"


def _public_base_url() -> str:
    base = (
        getattr(settings, "GLOBALAIOPC_OMNI_FLASH_PUBLIC_BASE_URL", "")
        or settings.ISSUER
        or ""
    )
    return str(base).strip().rstrip("/")


def _public_reference_urls(db: Session, *, task: KieTask) -> list[str]:
    base = _public_base_url()
    if not base:
        return []
    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind == "reference_upload",
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    return [
        f"{base}{settings.API_PREFIX}/tenants/{int(task.workspace_id)}"
        f"/ai-video/videos/public-reference/{int(task.id)}/{int(file.id)}"
        for file in files
    ]


def build_GlobalAiOpc_payload(
    input_params: Mapping[str, Any],
    *,
    reference_urls: list[str] | None = None,
) -> dict[str, Any]:
    effective_prompt = _effective_prompt(input_params)
    payload: dict[str, Any] = {}
    for key, value in dict(input_params or {}).items():
        if (
            key in {
                "submit_path",
                "__submit_path",
                "generate_audio",
                "reference_mode",
                "reference_file_paths",
                "size",
                "duration",
                "service_provider",
                "provider_prompt",
                "full_prompt",
                "content_factory_reference_manifest",
                "content_factory_base_prompt",
                "content_factory_conversion_points",
                "content_factory_required_promotion",
                "content_factory_prompt_hash",
                "content_factory_prompt_policy_version",
            }
            or key.startswith("content_factory_")
        ):
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        payload[key] = value
    if effective_prompt:
        payload["prompt"] = effective_prompt

    references = []
    raw_references = payload.pop("reference_images", None) or payload.pop("images", None) or []
    if isinstance(raw_references, str):
        references.append(raw_references)
    elif isinstance(raw_references, (list, tuple, set)):
        references.extend(str(item).strip() for item in raw_references if str(item).strip())
    references.extend(reference_urls or [])

    payload["model"] = "omni-flash"
    if references:
        payload["images"] = references[:5]
    payload.pop("seconds", None)
    try:
        payload["duration"] = int(input_params.get("seconds") or input_params.get("duration") or 10)
    except Exception:
        payload["duration"] = 10
    payload["aspect_ratio"] = str(payload.get("aspect_ratio") or "16:9")
    payload["resolution"] = str(input_params.get("resolution") or payload.get("resolution") or "720p")

    return payload


def _local_state(remote_status: str | None, urls: list[str] | None = None) -> str | None:
    if urls and not remote_status:
        return "success"
    if remote_status == "completed":
        return "success"
    if remote_status == "failed":
        return "failed"
    return remote_status


def _get_GlobalAiOpc_key(db: Session, key_id: int) -> KieApiKey:
    key = (
        db.query(KieApiKey)
        .filter(
            KieApiKey.id == int(key_id),
            KieApiKey.provider_key == GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY,
            KieApiKey.is_active.is_(True),
        )
        .one_or_none()
    )
    if key is None:
        raise ValueError("GlobalAiOpc API key not found or inactive")
    return key


def _ensure_file(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    task_pk: int,
    file_url: str,
    kind: str = "result",
    meta_json: dict[str, Any] | None = None,
) -> KieFile:
    url = (file_url or "").strip()
    if not url:
        raise ValueError("file_url cannot be empty")

    existing_files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(workspace_id),
            KieFile.key_id == int(key_id),
            KieFile.task_id == int(task_pk),
            KieFile.file_url == url,
            KieFile.kind == kind,
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    if existing_files:
        existing = existing_files[0]
        if meta_json:
            existing_meta = dict(existing.meta_json or {})
            incoming_meta = dict(meta_json or {})
            merged_meta = {**incoming_meta, **existing_meta}
            if merged_meta != existing_meta:
                existing.meta_json = merged_meta
                db.add(existing)
        for duplicate in existing_files[1:]:
            if get_local_path(existing) is None and get_local_path(duplicate) is not None:
                existing.meta_json = dict(duplicate.meta_json or {})
                existing.download_url = duplicate.download_url
                existing.expires_at = duplicate.expires_at
                existing.size_bytes = duplicate.size_bytes or existing.size_bytes
                existing.mime_type = duplicate.mime_type or existing.mime_type
                db.add(existing)
            db.delete(duplicate)
            db.add(existing)
        return existing

    file = KieFile(
        workspace_id=int(workspace_id),
        key_id=int(key_id),
        task_id=int(task_pk),
        file_url=url,
        kind=kind,
        mime_type="video/mp4",
        meta_json=meta_json,
    )
    db.add(file)
    return file


def create_GlobalAiOpc_local_task(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    input_params: Mapping[str, Any],
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieTask:
    key = _get_GlobalAiOpc_key(db, key_id)
    model = str(input_params.get("model") or "").strip()
    if not model:
        raise ValueError("model is required")

    prompt: Optional[str] = None
    raw_prompt = input_params.get("prompt")
    raw_prompt = _effective_prompt(input_params) or raw_prompt
    if isinstance(raw_prompt, str) and raw_prompt.strip():
        prompt = raw_prompt.strip()[:2000]

    clean_input = {
        k: v
        for k, v in dict(input_params or {}).items()
        if v is not None and not (isinstance(v, str) and not v.strip())
    }

    task = KieTask(
        workspace_id=int(workspace_id),
        key_id=int(key.id),
        created_by_user_id=int(actor_user_id) if actor_user_id is not None else None,
        model=model,
        task_id=f"{LOCAL_TASK_PREFIX}{uuid4().hex}",
        state="queued_local",
        prompt=prompt,
        input_json=clean_input,
    )
    db.add(task)
    db.flush()
    set_task_local_meta(task, download_name_base=str(int(task.id)))
    db.add(task)
    db.flush()

    log_event(
        db,
        action="GlobalAiOpc.video.create_local_task",
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
            "key_id": int(key.id),
            "submit_path": choose_submit_path(clean_input),
            "request": clean_input,
        },
    )

    return task


def reset_GlobalAiOpc_task_for_retry(
    db: Session,
    *,
    task: KieTask,
    input_params: Mapping[str, Any] | None = None,
    retry_kind: str = "manual",
) -> KieTask:
    if input_params is not None:
        source_input = dict(task.input_json or {})
        source_input.update(dict(input_params or {}))
        clean_input = {
            k: v
            for k, v in source_input.items()
            if v is not None and not (isinstance(v, str) and not v.strip())
        }
        if not clean_input.get("model"):
            clean_input["model"] = task.model
        task.input_json = clean_input
        task.model = str(clean_input.get("model") or task.model)
        raw_prompt = _effective_prompt(clean_input) or clean_input.get("prompt")
        task.prompt = raw_prompt.strip()[:2000] if isinstance(raw_prompt, str) and raw_prompt.strip() else None

    meta = next_retry_meta(task, kind=retry_kind)
    meta.update(
        {
            "download_enqueued_at": None,
            "download_started_at": None,
            "download_finished_at": None,
            "download_error": None,
        }
    )
    task.task_id = f"{LOCAL_TASK_PREFIX}{uuid4().hex}"
    task.state = "queued_local"
    task.result_json = {"__local": meta}
    task.fail_code = None
    task.fail_msg = None
    db.add(task)
    db.flush()

    log_event(
        db,
        action="GlobalAiOpc.video.retry_task",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_workspace_id=int(task.workspace_id),
        workspace_id=int(task.workspace_id),
        details={
            "model": task.model,
            "task_id": task.task_id,
            "retry_kind": retry_kind,
            "key_id": int(task.key_id),
            "request": task.input_json,
        },
    )
    return task


async def submit_GlobalAiOpc_task(
    db: Session,
    *,
    task: KieTask,
    key: KieApiKey | None = None,
    file_key_id: int | None = None,
) -> KieTask:
    if key is None:
        key = _get_GlobalAiOpc_key(db, int(task.key_id))
    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = GlobalAiOpcVideoClient(api_key=api_key)

    input_params = task.input_json or {}
    submit_path = choose_submit_path(input_params)
    payload = build_GlobalAiOpc_payload(
        input_params,
        reference_urls=_public_reference_urls(db, task=task),
    )

    try:
        resp = await client.create_video_task(payload=payload, submit_path=submit_path)
    except Exception as exc:  # noqa: BLE001
        raise GlobalAiOpcApiError(f"GlobalAiOpc create video task error: {exc}") from exc

    remote_task_id = extract_task_id(resp)
    if not remote_task_id:
        code, message = extract_error(resp)
        detail = message or json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
        prefix = f"{code}: " if code else ""
        raise GlobalAiOpcApiError(f"客易云创建任务失败：{prefix}{detail}"[:1000])

    urls = extract_video_urls(resp)
    status = _local_state(extract_status(resp), urls)

    task.task_id = remote_task_id
    task.state = status or "queued"
    task.result_json = {
        **(task.result_json or {}),
        "submit_path": submit_path,
        "submit_payload": payload,
        "submit_response": resp,
    }

    code, message = extract_error(resp)
    if code:
        task.fail_code = str(code)
    if message and task.state == "failed":
        task.fail_msg = str(message)

    prepared_local_download = False
    for url in urls:
        file = _ensure_file(
            db,
            workspace_id=int(task.workspace_id),
            key_id=int(file_key_id or key.id),
            task_pk=int(task.id),
            file_url=url,
            meta_json={"source": "submit_response"},
        )
        mark_result_file_pending(
            file,
            filename=str(task.result_json.get("__local", {}).get("download_name_base") or task.id),
        )
        db.add(file)
        prepared_local_download = True

    if prepared_local_download and str(task.state or "").lower() == "success":
        task.state = "downloading"
        task.fail_code = None
        task.fail_msg = None

    db.add(task)
    db.flush()

    log_event(
        db,
        action="GlobalAiOpc.video.submit_task",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_workspace_id=int(task.workspace_id),
        workspace_id=int(task.workspace_id),
        details={
            "model": task.model,
            "task_id": task.task_id,
            "state": task.state,
            "key_id": int(key.id),
            "submit_path": submit_path,
        },
    )

    return task


async def refresh_GlobalAiOpc_task_status(
    db: Session,
    *,
    task: KieTask,
    key: KieApiKey | None = None,
    file_key_id: int | None = None,
) -> KieTask:
    if str(task.task_id or "").startswith(LOCAL_TASK_PREFIX):
        return task

    if key is None:
        key = _get_GlobalAiOpc_key(db, int(task.key_id))

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = GlobalAiOpcVideoClient(api_key=api_key)

    try:
        resp = await client.get_video_task(task_id=task.task_id)
    except Exception as exc:  # noqa: BLE001
        raise GlobalAiOpcApiError(f"GlobalAiOpc query task error: {exc}") from exc

    urls = extract_video_urls(resp)
    status = _local_state(extract_status(resp), urls)
    if status:
        task.state = status

    code, message = extract_error(resp)
    if task.state == "failed":
        if code:
            task.fail_code = str(code)
        if message:
            task.fail_msg = str(message)

    task.result_json = {
        **(task.result_json or {}),
        "poll_response": resp,
    }

    prepared_local_download = False
    for url in urls:
        file = _ensure_file(
            db,
            workspace_id=int(task.workspace_id),
            key_id=int(file_key_id or key.id),
            task_pk=int(task.id),
            file_url=url,
            meta_json={"source": "poll_response"},
        )
        mark_result_file_pending(
            file,
            filename=str(task.result_json.get("__local", {}).get("download_name_base") or task.id),
        )
        db.add(file)
        prepared_local_download = True

    if prepared_local_download and task.state == "success":
        task.state = "downloading"
        task.fail_code = None
        task.fail_msg = None
        set_task_local_meta(task, download_enqueued_at=None)

    db.add(task)
    db.flush()

    log_event(
        db,
        action="GlobalAiOpc.video.refresh_task",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_workspace_id=int(task.workspace_id),
        workspace_id=int(task.workspace_id),
        details={
            "model": task.model,
            "task_id": task.task_id,
            "state": task.state,
            "fail_code": task.fail_code,
            "fail_msg": task.fail_msg,
        },
    )

    return task


async def refresh_GlobalAiOpc_task_status_by_task_id(
    db: Session,
    *,
    workspace_id: int,
    local_task_id: int,
) -> KieTask:
    task = (
        db.query(KieTask)
        .join(KieApiKey, KieTask.key_id == KieApiKey.id)
        .filter(
            KieTask.id == int(local_task_id),
            KieTask.workspace_id == int(workspace_id),
            KieApiKey.provider_key == GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY,
        )
        .one_or_none()
    )
    if task is None:
        raise ValueError("GlobalAiOpc task not found")

    return await refresh_GlobalAiOpc_task_status(db, task=task)


def get_batch_limit() -> int:
    return max(1, min(int(getattr(settings, "GLOBALAIOPC_OMNI_FLASH_BATCH_LIMIT", 50)), 200))


__all__ = [
    "LOCAL_TASK_PREFIX",
    "build_GlobalAiOpc_payload",
    "choose_submit_path",
    "create_GlobalAiOpc_local_task",
    "get_batch_limit",
    "refresh_GlobalAiOpc_task_status",
    "refresh_GlobalAiOpc_task_status_by_task_id",
    "reset_GlobalAiOpc_task_for_retry",
    "submit_GlobalAiOpc_task",
]
