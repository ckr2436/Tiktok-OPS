from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.kie_api.accounts import decrypt_api_key
from app.services.kie_api.local_storage import mark_result_file_pending, set_task_local_meta


VOLCENGINE_MODEL = "doubao-seedance-2-0-mini-260615"


class VolcengineApiError(RuntimeError):
    pass


def _duration_seconds(params: dict[str, Any]) -> int:
    try:
        value = int(float(params.get("seconds") or params.get("duration") or 10))
    except (TypeError, ValueError):
        value = 10
    return max(1, min(15, value))


def _resolution(params: dict[str, Any]) -> str:
    raw = str(params.get("resolution") or params.get("size") or "720p").strip().lower()
    aliases = {
        "standard": "720p",
        "high": "720p",
        "hd": "720p",
        "fhd": "720p",
        "1080": "720p",
        "720": "720p",
        "480": "480p",
    }
    value = aliases.get(raw, raw)
    return value if value in {"480p", "720p"} else "720p"


def _reference_image_content(url: str) -> dict[str, Any]:
    return {
        "type": "image_url",
        "role": "reference_image",
        "image_url": {"url": url},
    }


def _reference_video_content(url: str) -> dict[str, Any]:
    return {
        "type": "video_url",
        "role": "reference_video",
        "video_url": {"url": url},
    }


def _data_url(path: str, content_type: str | None = None) -> str:
    file_path = Path(path)
    mime = content_type or mimetypes.guess_type(file_path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(file_path.read_bytes()).decode('ascii')}"


def _client(key: KieApiKey) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        headers={"Authorization": f"Bearer {decrypt_api_key(key.api_key_ciphertext)}"},
        timeout=90,
    )


async def submit_volcengine_task(db: Session, *, task: KieTask) -> KieTask:
    refs = (
        db.query(KieFile)
        .filter(KieFile.task_id == task.id, KieFile.kind == "reference_upload")
        .order_by(KieFile.id.asc())
        .all()
    )
    video_refs = (
        db.query(KieFile)
        .filter(KieFile.task_id == task.id, KieFile.kind == "reference_video_upload")
        .order_by(KieFile.id.asc())
        .all()
    )
    reference_urls = list((task.input_json or {}).get("reference_images") or [])
    if len(refs) + len(reference_urls) > 9:
        raise VolcengineApiError("Seedance 2.0 Mini supports at most 9 reference images")
    content: list[dict[str, Any]] = [{"type": "text", "text": str((task.input_json or {}).get("prompt") or task.prompt or "")}]
    for url in reference_urls:
        content.append(_reference_image_content(str(url)))
    for file in refs:
        content.append(_reference_image_content(_data_url(file.file_url, file.mime_type)))
    for file in video_refs[:1]:
        content.append(_reference_video_content(_data_url(file.file_url, file.mime_type)))
    params = dict(task.input_json or {})
    payload = {
        "model": VOLCENGINE_MODEL,
        "content": content,
        "generate_audio": bool(params.get("generate_audio", True)),
        "ratio": str(params.get("aspect_ratio") or "9:16"),
        "duration": _duration_seconds(params),
        "resolution": _resolution(params),
        "watermark": False,
    }
    key = db.get(KieApiKey, int(task.key_id))
    async with _client(key) as client:
        response = await client.post("/contents/generations/tasks", json=payload)
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise VolcengineApiError(response.text[:1000]) from exc
        if response.is_error:
            raise VolcengineApiError(str(data)[:1000])
    remote_id = str(data.get("id") or "")
    if not remote_id:
        raise VolcengineApiError(f"Seedance task id missing: {data}")
    task.task_id = remote_id
    task.state = "queued"
    task.result_json = {**dict(task.result_json or {}), "submit_response": data}
    set_task_local_meta(task, active_provider="volcengine")
    db.add(task)
    db.flush()
    return task


async def refresh_volcengine_task(db: Session, *, task: KieTask) -> KieTask:
    key = db.get(KieApiKey, int(task.key_id))
    async with _client(key) as client:
        response = await client.get(f"/contents/generations/tasks/{task.task_id}")
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise VolcengineApiError(response.text[:1000]) from exc
        if response.is_error:
            raise VolcengineApiError(str(data)[:1000])
    state = str(data.get("status") or "queued").lower()
    mapping = {"succeeded": "success", "failed": "failed", "expired": "failed"}
    task.state = mapping.get(state, state)
    task.result_json = {**dict(task.result_json or {}), "poll_response": data}
    if task.state == "failed":
        error = data.get("error") or {}
        task.fail_code = str(error.get("code") or "volcengine_failed")
        task.fail_msg = str(error.get("message") or error or "Seedance generation failed")[:1000]
    video_url = str((data.get("content") or {}).get("video_url") or "")
    if video_url:
        file = db.query(KieFile).filter(KieFile.task_id == task.id, KieFile.file_url == video_url).first()
        if file is None:
            file = KieFile(
                workspace_id=task.workspace_id, key_id=task.key_id, task_id=task.id,
                file_url=video_url, kind="result", mime_type="video/mp4",
                meta_json={"source": "volcengine_seedance_2_mini"},
            )
            db.add(file)
            db.flush()
        mark_result_file_pending(file, filename=str((task.result_json.get("__local") or {}).get("download_name_base") or task.id))
        task.state = "downloading"
        task.fail_code = None
        task.fail_msg = None
    db.add(task)
    db.flush()
    return task
