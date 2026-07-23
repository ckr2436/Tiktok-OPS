from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.globalaiopc.client import extract_error, extract_status, extract_task_id, extract_video_urls
from app.services.kie_api.accounts import TOAPIS_PROVIDER_KEY, decrypt_api_key
from app.services.kie_api.local_storage import mark_result_file_pending, set_task_local_meta
from app.services.toapis.client import ToApisApiError, ToApisVideoClient


ALLOWED_REFERENCE_COUNTS = {0, 1, 3}
ALLOWED_DURATIONS = {4, 6, 10}


def _key(db: Session, task: KieTask) -> KieApiKey:
    key = db.get(KieApiKey, int(task.key_id))
    if key is None or not key.is_active or key.provider_key != TOAPIS_PROVIDER_KEY:
        raise ToApisApiError("ToAPIs API key not found or inactive")
    return key


def _references(db: Session, task: KieTask) -> list[KieFile]:
    return (
        db.query(KieFile)
        .filter(KieFile.task_id == int(task.id), KieFile.kind == "reference_upload")
        .order_by(KieFile.id.asc())
        .all()
    )


def _prompt(params: Mapping[str, Any], task: KieTask) -> str:
    for name in ("provider_prompt", "full_prompt", "prompt"):
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(task.prompt or "").strip()


def _duration(params: Mapping[str, Any]) -> int:
    try:
        value = int(params.get("seconds") or params.get("duration") or 10)
    except (TypeError, ValueError):
        value = 10
    if value not in ALLOWED_DURATIONS:
        raise ToApisApiError("ToAPIs Omni Flash duration must be 4, 6, or 10 seconds")
    return value


def _resolution(params: Mapping[str, Any], aspect_ratio: str) -> str:
    value = str(params.get("resolution") or "720p").strip().lower()
    if value in {"720", "720p"}:
        return "720P"
    if value in {"1080", "1080p"} and aspect_ratio == "16:9":
        return "1080p"
    raise ToApisApiError("ToAPIs Omni Flash supports 720P, or 1080p for 16:9 only")


def _ensure_result_file(db: Session, task: KieTask, url: str, source: str) -> KieFile:
    row = (
        db.query(KieFile)
        .filter(KieFile.task_id == int(task.id), KieFile.kind == "result", KieFile.file_url == url)
        .order_by(KieFile.id.asc())
        .first()
    )
    if row is None:
        row = KieFile(
            workspace_id=int(task.workspace_id),
            key_id=int(task.key_id),
            task_id=int(task.id),
            kind="result",
            file_url=url,
            mime_type="video/mp4",
            meta_json={"source": source},
        )
    local_meta = dict(dict(task.result_json or {}).get("__local") or {})
    mark_result_file_pending(
        row,
        filename=str(local_meta.get("download_name_base") or task.id),
    )
    db.add(row)
    return row


def _apply_response(db: Session, task: KieTask, response: Mapping[str, Any], source: str) -> KieTask:
    urls = extract_video_urls(response)
    remote_status = extract_status(response)
    state = {
        "completed": "success",
        "failed": "failed",
        "queued": "queued",
        "in_progress": "in_progress",
    }.get(str(remote_status or ""), str(remote_status or task.state or "in_progress"))
    task.state = state
    code, message = extract_error(response)
    if state == "failed":
        task.fail_code = str(code or "toapis_generation_failed")[:128]
        task.fail_msg = str(message or response.get("error") or "ToAPIs video generation failed")[:1000]
    prepared = False
    for url in urls:
        _ensure_result_file(db, task, url, source)
        prepared = True
    if prepared and state == "success":
        task.state = "downloading"
        task.fail_code = None
        task.fail_msg = None
        set_task_local_meta(task, download_enqueued_at=None)
    db.add(task)
    db.flush()
    return task


async def submit_toapis_task(db: Session, *, task: KieTask) -> KieTask:
    key = _key(db, task)
    client = ToApisVideoClient(api_key=decrypt_api_key(key.api_key_ciphertext))
    references = _references(db, task)
    if len(references) not in ALLOWED_REFERENCE_COUNTS:
        raise ToApisApiError("ToAPIs Omni Flash accepts exactly 0, 1, or 3 reference images")
    params = dict(task.input_json or {})
    aspect_ratio = str(params.get("aspect_ratio") or "9:16").strip()
    if aspect_ratio not in {"9:16", "16:9"}:
        raise ToApisApiError("ToAPIs Omni Flash aspect ratio must be 9:16 or 16:9")
    image_urls = [await client.upload_image(row.file_url, row.mime_type) for row in references]
    payload: dict[str, Any] = {
        "model": "gemini_omni_flash",
        "prompt": _prompt(params, task),
        "duration": _duration(params),
        "aspect_ratio": aspect_ratio,
        "resolution": _resolution(params, aspect_ratio),
        "image_urls": image_urls,
    }
    response = await client.create_video(payload)
    remote_task_id = extract_task_id(response)
    if not remote_task_id:
        code, message = extract_error(response)
        raise ToApisApiError(f"ToAPIs create failed: {code or ''} {message or response}"[:1000])
    task.task_id = remote_task_id
    task.result_json = {**dict(task.result_json or {}), "toapis_submit_payload": payload, "toapis_submit_response": dict(response)}
    return _apply_response(db, task, response, "toapis_submit")


async def refresh_toapis_task(db: Session, *, task: KieTask) -> KieTask:
    key = _key(db, task)
    client = ToApisVideoClient(api_key=decrypt_api_key(key.api_key_ciphertext))
    response = await client.get_video(task.task_id)
    task.result_json = {**dict(task.result_json or {}), "toapis_poll_response": dict(response)}
    return _apply_response(db, task, response, "toapis_poll")


__all__ = ["ToApisApiError", "refresh_toapis_task", "submit_toapis_task"]
