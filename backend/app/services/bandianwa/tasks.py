from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.audit import log_event
from app.services.bandianwa.client import (
    BandianwaApiError,
    BandianwaVideoClient,
    extract_error,
    extract_status,
    extract_task_id,
    extract_video_urls,
    normalize_submit_path,
)
from app.services.kie_api.accounts import BANDIANWA_PROVIDER_KEY, VOLCENGINE_PROVIDER_KEY, decrypt_api_key
from app.services.kie_api.local_storage import get_local_path, mark_result_file_pending, set_task_local_meta
from app.services.kie_api.retry_policy import next_retry_meta


LOCAL_TASK_PREFIX = "local-bandianwa-"
MAX_OMNI_REFERENCE_FILES = 7


def _effective_prompt(input_params: Mapping[str, Any]) -> str:
    for key in ("provider_prompt", "full_prompt", "prompt"):
        value = input_params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_seconds(value: Any) -> str:
    raw = str(value).strip()
    if not raw:
        raise ValueError("seconds cannot be empty")
    try:
        numeric = float(raw)
    except ValueError:
        return raw
    if numeric.is_integer():
        return str(int(numeric))
    return raw


def _normalize_reference_file_paths(value: Any, *, limit: int = MAX_OMNI_REFERENCE_FILES) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[Any] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            key = str(item.get("path") or item.get("url") or item.get("filename") or item)
        else:
            key = str(item)
        key = key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(item)
        if len(rows) >= limit:
            break
    return rows


def choose_submit_path(input_params: Mapping[str, Any]) -> str:
    raw = str(input_params.get("submit_path") or input_params.get("__submit_path") or "").strip()
    if raw:
        return normalize_submit_path(raw)

    model = str(input_params.get("model") or "").strip().lower()
    if model.startswith("omni") or model.startswith("veo_"):
        return "/v1/videos"
    return "/api/v1/generate"


def build_bandianwa_payload(input_params: Mapping[str, Any]) -> dict[str, Any]:
    submit_path = choose_submit_path(input_params)
    effective_prompt = _effective_prompt(input_params)
    payload: dict[str, Any] = {}
    for key, value in dict(input_params or {}).items():
        if (
            key in {
                "submit_path",
                "__submit_path",
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

    if submit_path == "/v1/videos":
        allowed_v1_keys = {
            "model",
            "prompt",
            "size",
            "seconds",
            "duration",
            "aspect_ratio",
            "generate_audio",
            "input_reference",
            "reference_images",
            "reference_videos",
            "reference_file_paths",
            "reference_video_file_paths",
        }
        payload = {key: value for key, value in payload.items() if key in allowed_v1_keys}
        references = payload.pop("reference_images", None)
        video_references = payload.pop("reference_videos", None)
        reference_file_paths = _normalize_reference_file_paths(payload.get("reference_file_paths"))
        if reference_file_paths:
            payload["reference_file_paths"] = reference_file_paths
        else:
            payload.pop("reference_file_paths", None)
        payload.pop("reference_video_file_paths", None)
        combined_references = list(references or []) + list(video_references or [])
        has_multipart_references = bool(reference_file_paths)
        if has_multipart_references:
            payload.pop("input_reference", None)
        elif combined_references and "input_reference" not in payload:
            payload["input_reference"] = json.dumps(combined_references, ensure_ascii=False)
        elif not combined_references and "input_reference" not in payload:
            payload["input_reference"] = "[]"
        if "duration" in payload and "seconds" not in payload:
            payload["seconds"] = payload.pop("duration")
        if "seconds" in payload:
            payload["seconds"] = _normalize_seconds(payload["seconds"])
        aspect_ratio = str(payload.pop("aspect_ratio", "") or "").strip()
        raw_size = str(payload.get("size") or "").strip().lower()
        size_is_dimensions = "x" in raw_size and all(part.isdigit() for part in raw_size.split("x", 1))
        if aspect_ratio and (not raw_size or not size_is_dimensions):
            if aspect_ratio == "16:9":
                payload["size"] = "1920x1080"
            elif aspect_ratio == "1:1":
                payload["size"] = "1080x1080"
            else:
                payload["size"] = "1080x1920"
        elif not raw_size:
            payload["size"] = "1080x1920"
        if payload.get("generate_audio") is False:
            payload.pop("generate_audio", None)
    else:
        payload.pop("seconds", None)
        payload.pop("duration", None)

    return payload


def _file_data_url(file: KieFile) -> str:
    path = Path(file.file_url)
    mime = file.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _local_state(remote_status: str | None, urls: list[str] | None = None) -> str | None:
    if urls and not remote_status:
        return "success"
    if remote_status == "completed":
        return "success"
    if remote_status == "failed":
        return "failed"
    return remote_status


def _get_provider_key(db: Session, key_id: int, provider_key: str = BANDIANWA_PROVIDER_KEY) -> KieApiKey:
    key = (
        db.query(KieApiKey)
        .filter(
            KieApiKey.id == int(key_id),
            KieApiKey.provider_key == provider_key,
            KieApiKey.is_active.is_(True),
        )
        .one_or_none()
    )
    if key is None:
        raise ValueError(f"{provider_key} API key not found or inactive")
    return key


def _get_bandianwa_key(db: Session, key_id: int) -> KieApiKey:
    return _get_provider_key(db, key_id, BANDIANWA_PROVIDER_KEY)


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


def create_bandianwa_local_task(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    input_params: Mapping[str, Any],
    provider_key: str = BANDIANWA_PROVIDER_KEY,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieTask:
    key = _get_provider_key(db, key_id, provider_key)
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
        action="bandianwa.video.create_local_task",
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
            "provider_key": key.provider_key,
            "submit_path": choose_submit_path(clean_input),
            "request": clean_input,
        },
    )

    return task


def reset_bandianwa_task_for_retry(
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
            "submit_worker_task_id": None,
            "submit_worker_heartbeat_at": None,
        }
    )
    # A retry has a new local task id and must acquire a new poller lease.
    # Carrying the previous worker's lease forward makes the new submit task
    # return early as a duplicate, leaving queued_local permanently stuck.
    for lease_key in ("poll_owner_task_id", "poll_heartbeat_at", "poll_heartbeat_provider"):
        meta.pop(lease_key, None)
    task.task_id = f"{LOCAL_TASK_PREFIX}{uuid4().hex}"
    task.state = "queued_local"
    task.result_json = {"__local": meta}
    task.fail_code = None
    task.fail_msg = None
    db.add(task)
    db.flush()

    log_event(
        db,
        action="bandianwa.video.retry_task",
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


async def submit_bandianwa_task(db: Session, *, task: KieTask) -> KieTask:
    key = _get_bandianwa_key(db, int(task.key_id))
    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = BandianwaVideoClient(api_key=api_key)

    input_params = dict(task.input_json or {})
    image_files = db.query(KieFile).filter(
        KieFile.task_id == task.id, KieFile.kind == "reference_upload"
    ).order_by(KieFile.id.asc()).all()
    video_files = db.query(KieFile).filter(
        KieFile.task_id == task.id, KieFile.kind == "reference_video_upload"
    ).order_by(KieFile.id.asc()).limit(1).all()
    submit_path = choose_submit_path(input_params)
    if image_files and submit_path == "/v1/videos":
        input_params["reference_file_paths"] = [
            *list(input_params.get("reference_file_paths") or []),
            *[
                {
                    "path": file.file_url,
                    "filename": file.meta_json.get("filename") if isinstance(file.meta_json, dict) else None,
                    "content_type": file.mime_type or "image/*",
                }
                for file in image_files
            ],
        ]
    elif image_files:
        input_params["reference_images"] = [
            *list(input_params.get("reference_images") or []),
            *[_file_data_url(file) for file in image_files],
        ]
    if video_files:
        input_params["reference_videos"] = [
            *list(input_params.get("reference_videos") or []),
            *[_file_data_url(file) for file in video_files],
        ]
    payload = build_bandianwa_payload(input_params)

    try:
        # ``task.task_id`` is a stable local UUID for one submission
        # generation and reset_bandianwa_task_for_retry replaces it only when
        # a genuinely new generation is requested. Reusing it across an
        # ambiguous timeout/disconnect lets the provider collapse transport
        # retries instead of charging for duplicate video jobs.
        resp = await client.create_video_task(
            payload=payload,
            submit_path=submit_path,
            idempotency_key=(
                f"gmv-video-{int(task.workspace_id)}-{int(task.id)}-{str(task.task_id)}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise BandianwaApiError(f"Bandianwa create video task error: {exc}") from exc

    remote_task_id = extract_task_id(resp)
    if not remote_task_id:
        raise BandianwaApiError("Bandianwa create response missing task_id")

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
            key_id=int(key.id),
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
        action="bandianwa.video.submit_task",
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


async def refresh_bandianwa_task_status(
    db: Session,
    *,
    task: KieTask,
    key: KieApiKey | None = None,
) -> KieTask:
    if str(task.task_id or "").startswith(LOCAL_TASK_PREFIX):
        return task

    if key is None:
        key = _get_bandianwa_key(db, int(task.key_id))

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = BandianwaVideoClient(api_key=api_key)

    try:
        resp = await client.get_video_task(task_id=task.task_id)
    except Exception as exc:  # noqa: BLE001
        raise BandianwaApiError(f"Bandianwa query task error: {exc}") from exc

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
            key_id=int(key.id),
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

    if task.state == "success" and not urls:
        file = _ensure_file(
            db,
            workspace_id=int(task.workspace_id),
            key_id=int(key.id),
            task_pk=int(task.id),
            file_url=client.content_url(task_id=task.task_id),
            meta_json={"source": "content_endpoint"},
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
        action="bandianwa.video.refresh_task",
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


async def refresh_bandianwa_task_status_by_task_id(
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
            KieApiKey.provider_key.in_([BANDIANWA_PROVIDER_KEY, VOLCENGINE_PROVIDER_KEY]),
        )
        .one_or_none()
    )
    if task is None:
        raise ValueError("Bandianwa task not found")

    return await refresh_bandianwa_task_status(db, task=task)


def get_batch_limit() -> int:
    return max(1, min(int(getattr(settings, "BANDIANWA_BATCH_LIMIT", 50)), 200))


__all__ = [
    "LOCAL_TASK_PREFIX",
    "build_bandianwa_payload",
    "choose_submit_path",
    "create_bandianwa_local_task",
    "get_batch_limit",
    "refresh_bandianwa_task_status",
    "refresh_bandianwa_task_status_by_task_id",
    "reset_bandianwa_task_for_retry",
    "submit_bandianwa_task",
]
