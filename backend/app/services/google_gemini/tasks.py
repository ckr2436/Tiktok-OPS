from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.kie_api.accounts import decrypt_api_key
from app.services.kie_api.local_storage import set_task_local_meta


GEMINI_OMNI_MODEL = "gemini-omni-flash-preview"
MAX_GEMINI_REFERENCE_IMAGES = 7
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_RESULTS_ROOT = Path("/data/gmv_ops/google_gemini_results")


class GoogleGeminiApiError(RuntimeError):
    pass


def _aspect_ratio(params: Mapping[str, Any]) -> str:
    raw = str(params.get("aspect_ratio") or params.get("ratio") or "9:16").strip()
    return "16:9" if raw == "16:9" else "9:16"


def _file_part(path: str, mime_type: str | None = None) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        raise GoogleGeminiApiError(f"Gemini reference file not found: {path}")
    mime = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return {
        "type": "image" if mime.startswith("image/") else "video",
        "data": base64.b64encode(file_path.read_bytes()).decode("ascii"),
        "mime_type": mime,
    }


def _reference_files(db: Session, task: KieTask, kind: str, limit: int) -> list[KieFile]:
    return (
        db.query(KieFile)
        .filter(KieFile.task_id == task.id, KieFile.kind == kind)
        .order_by(KieFile.id.asc())
        .limit(limit)
        .all()
    )


def _prompt_with_reference_tags(prompt: str, image_count: int, *, first_last: bool = False) -> str:
    prompt = str(prompt or "").strip()
    if image_count <= 0:
        return prompt
    if first_last and image_count >= 1:
        tags = ["[# Sources <FIRST_FRAME>@Image1]"]
        if image_count > 1:
            tags.append(" ".join(f"<IMAGE_REF_{index - 2}>@Image{index}" for index in range(2, image_count + 1)))
            tags[-1] = f"[# References {tags[-1]}]"
        return (
            f"{' '.join(tags)} {prompt} "
            "Use Image1 as the starting frame. Use the other images as references for video generation."
        ).strip()
    tags = " ".join(f"<IMAGE_REF_{index}>@Image{index + 1}" for index in range(image_count))
    return (
        f"[# References {tags}] {prompt} "
        "Use the given image(s) as references for video generation. "
        "The images should not be used as literal initial frames unless explicitly requested."
    ).strip()


def _extract_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _extract_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _extract_strings(item)


def _extract_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _extract_mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _extract_mappings(item)


def _find_video_payload(resp: Mapping[str, Any]) -> tuple[str | None, bytes | None]:
    url: str | None = None
    data: bytes | None = None
    for value in _extract_mappings(resp):
        media_type = str(value.get("type") or "").lower()
        mime_type = str(value.get("mime_type") or value.get("mimeType") or "").lower()
        candidate_url = str(value.get("uri") or value.get("url") or value.get("download_uri") or "")
        if candidate_url and (
            media_type == "video"
            or mime_type.startswith("video/")
            or ".mp4" in candidate_url
            or "/files/" in candidate_url
        ):
            url = candidate_url
        raw = value.get("data") or value.get("bytes")
        if isinstance(raw, str) and raw.strip():
            try:
                decoded = base64.b64decode(raw)
            except Exception:  # noqa: BLE001
                continue
            if decoded.startswith(b"\x00\x00") or decoded.startswith(b"ftyp") or b"ftyp" in decoded[:64]:
                data = decoded
    for item in _extract_strings(resp):
        if not url and item.startswith(("http://", "https://")) and (".mp4" in item or "video" in item):
            url = item
        if data is None and len(item) > 1000:
            try:
                decoded = base64.b64decode(item, validate=True)
            except Exception:  # noqa: BLE001
                continue
            if decoded.startswith(b"\x00\x00") or decoded.startswith(b"ftyp") or b"ftyp" in decoded[:64]:
                data = decoded
    return url, data


def _interaction_state(payload: Mapping[str, Any]) -> str:
    value = str(
        payload.get("state")
        or payload.get("status")
        or dict(payload.get("metadata") or {}).get("state")
        or ""
    ).strip().lower()
    if value in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if value in {"completed", "complete", "succeeded", "success", "done", "active"}:
        return "success"
    return "in_progress"


def _interaction_id(payload: Mapping[str, Any]) -> str:
    value = str(payload.get("id") or payload.get("name") or "").strip()
    if value.startswith("interactions/"):
        value = value.split("/", 1)[1]
    if "/interactions/" in value:
        value = value.rsplit("/interactions/", 1)[1]
    return value


def _google_file_name(uri: str) -> str | None:
    match = re.search(r"(?:^|/)(files/[^/?#:]+)", str(uri or ""))
    return match.group(1) if match else None


def _result_target(task: KieTask) -> Path:
    target = GEMINI_RESULTS_ROOT / f"workspace_{task.workspace_id}" / f"{task.id}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _result_filename(task: KieTask) -> str:
    return str((dict(task.result_json or {}).get("__local") or {}).get("download_name_base") or task.id) + ".mp4"


def _persist_video_bytes(db: Session, *, task: KieTask, video_bytes: bytes, source: str) -> KieTask:
    if not video_bytes:
        raise GoogleGeminiApiError("Google Gemini returned an empty video file")
    target = _result_target(task)
    target.write_bytes(video_bytes)
    target.chmod(0o644)
    existing = (
        db.query(KieFile)
        .filter(KieFile.task_id == task.id, KieFile.kind == "result")
        .order_by(KieFile.id.asc())
        .first()
    )
    if existing is None:
        existing = KieFile(
            workspace_id=task.workspace_id,
            key_id=task.key_id,
            task_id=task.id,
            kind="result",
            mime_type="video/mp4",
        )
    existing.key_id = task.key_id
    existing.file_url = str(target)
    existing.size_bytes = target.stat().st_size
    existing.meta_json = {
        **dict(existing.meta_json or {}),
        "source": source,
        "local_download_status": "success",
        "local_path": str(target),
        "local_bytes": target.stat().st_size,
        "filename": _result_filename(task),
    }
    db.add(existing)
    task.state = "success"
    task.fail_code = None
    task.fail_msg = None
    db.add(task)
    db.flush()
    return task


async def _download_google_video(client: httpx.AsyncClient, *, uri: str, api_key: str) -> bytes | None:
    headers = {"x-goog-api-key": api_key}
    file_name = _google_file_name(uri)
    candidates: list[str] = []
    if uri.startswith(("http://", "https://")):
        candidates.append(uri if ":download" in uri else f"{uri}:download?alt=media")
        candidates.append(uri)
    if file_name:
        candidates.extend([
            f"{GEMINI_API_ROOT}/{file_name}:download?alt=media",
            f"https://generativelanguage.googleapis.com/download/v1beta/{file_name}:download?alt=media",
        ])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        response = await client.get(candidate, headers=headers)
        if response.is_success and response.content:
            content_type = str(response.headers.get("content-type") or "").lower()
            if "video" in content_type or b"ftyp" in response.content[:64]:
                return bytes(response.content)
    return None


async def _poll_google_file(client: httpx.AsyncClient, *, uri: str, api_key: str) -> tuple[str, bytes | None]:
    file_name = _google_file_name(uri)
    if not file_name:
        video = await _download_google_video(client, uri=uri, api_key=api_key)
        return ("success", video) if video else ("in_progress", None)
    response = await client.get(f"{GEMINI_API_ROOT}/{file_name}", headers={"x-goog-api-key": api_key})
    if response.is_success:
        payload = response.json()
        state = _interaction_state(payload)
        if state == "failed":
            raise GoogleGeminiApiError(str(payload)[:1000])
        if state == "success":
            video = await _download_google_video(client, uri=uri, api_key=api_key)
            return ("success", video) if video else ("in_progress", None)
    return "in_progress", None


async def submit_google_gemini_task(db: Session, *, task: KieTask) -> KieTask:
    key = db.get(KieApiKey, int(task.key_id))
    if key is None:
        raise GoogleGeminiApiError("Gemini API key not found")
    api_key = decrypt_api_key(key.api_key_ciphertext)
    if not api_key:
        raise GoogleGeminiApiError("Gemini API key is empty")

    params = dict(task.input_json or {})
    image_files = _reference_files(db, task, "reference_upload", MAX_GEMINI_REFERENCE_IMAGES)
    # The preview schema accepts a video part, but the Omni model currently does
    # not process reference video reliably. Ignore it instead of billing a known-
    # invalid request; image references remain fully supported.
    video_files: list[KieFile] = []
    contents: list[dict[str, Any]] = []
    for file in image_files:
        contents.append(_file_part(file.file_url, file.mime_type))
    for file in video_files:
        contents.append(_file_part(file.file_url, file.mime_type))

    prompt = _prompt_with_reference_tags(
        str(params.get("prompt") or task.prompt or ""),
        len(image_files),
        first_last=str(params.get("video_frame_mode") or "").lower() == "first_last",
    )
    contents.append({"type": "text", "text": prompt})
    generation_task = (
        "text_to_video"
        if not image_files
        else "image_to_video"
        if len(image_files) == 1
        else "reference_to_video"
    )
    payload: dict[str, Any] = {
        "model": str(params.get("google_model") or GEMINI_OMNI_MODEL),
        "input": contents,
        "response_format": {
            "type": "video",
            "aspect_ratio": _aspect_ratio(params),
            "delivery": "uri",
        },
        "generation_config": {
            "video_config": {
                "task": generation_task,
            },
        },
    }

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(
            f"{GEMINI_API_ROOT}/interactions",
            headers={"x-goog-api-key": api_key},
            json=payload,
        )
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise GoogleGeminiApiError(response.text[:1000]) from exc
    if response.is_error:
        raise GoogleGeminiApiError(str(data)[:1000])

    video_url, video_bytes = _find_video_payload(data)
    interaction_id = _interaction_id(data)
    task.task_id = interaction_id or f"google-gemini-{uuid4().hex}"
    task.state = "in_progress"
    task.result_json = {
        **dict(task.result_json or {}),
        "submit_payload": {**payload, "input": f"{len(contents) - 1} media refs + prompt" if len(contents) > 1 else prompt},
        "submit_response": data,
    }
    set_task_local_meta(task, active_provider="google-gemini")
    db.add(task)
    db.flush()

    if video_bytes:
        return _persist_video_bytes(db, task=task, video_bytes=video_bytes, source="google_gemini_inline")
    if video_url:
        task.result_json = {**dict(task.result_json or {}), "google_video_uri": video_url}
    elif not interaction_id:
        task.state = "failed"
        task.fail_code = "google_gemini_missing_interaction"
        task.fail_msg = "Google Gemini response contained neither an interaction id nor video output"
    elif _interaction_state(data) == "failed":
        task.state = "failed"
        task.fail_code = "google_gemini_generation_failed"
        task.fail_msg = str(data.get("error") or data)[:1000]
    task.fail_code = None if task.state != "failed" else task.fail_code
    task.fail_msg = None if task.state != "failed" else task.fail_msg
    db.add(task)
    db.flush()
    return task


async def refresh_google_gemini_task(db: Session, *, task: KieTask) -> KieTask:
    if str(task.state or "").lower() in {"success", "failed", "cancelled", "canceled"}:
        return task
    key = db.get(KieApiKey, int(task.key_id))
    if key is None:
        raise GoogleGeminiApiError("Gemini API key not found")
    api_key = decrypt_api_key(key.api_key_ciphertext)
    if not api_key:
        raise GoogleGeminiApiError("Gemini API key is empty")

    result = dict(task.result_json or {})
    video_url = str(result.get("google_video_uri") or "").strip()
    async with httpx.AsyncClient(timeout=180) as client:
        if not video_url:
            interaction_id = str(task.task_id or "").strip()
            if not interaction_id:
                raise GoogleGeminiApiError("Gemini interaction id is missing")
            response = await client.get(
                f"{GEMINI_API_ROOT}/interactions/{interaction_id}",
                headers={"x-goog-api-key": api_key},
            )
            if response.status_code == 404:
                return task
            if response.is_error:
                raise GoogleGeminiApiError(response.text[:1000])
            payload = response.json()
            result["poll_response"] = payload
            video_url, video_bytes = _find_video_payload(payload)
            if video_bytes:
                task.result_json = result
                return _persist_video_bytes(db, task=task, video_bytes=video_bytes, source="google_gemini_inline")
            if _interaction_state(payload) == "failed":
                task.state = "failed"
                task.fail_code = "google_gemini_generation_failed"
                task.fail_msg = str(payload.get("error") or payload)[:1000]
                task.result_json = result
                db.add(task)
                db.flush()
                return task
            if video_url:
                result["google_video_uri"] = video_url
                task.result_json = result
        if video_url:
            state, video_bytes = await _poll_google_file(client, uri=video_url, api_key=api_key)
            if state == "success" and video_bytes:
                task.result_json = result
                return _persist_video_bytes(db, task=task, video_bytes=video_bytes, source="google_gemini_uri")
    task.state = "in_progress"
    task.result_json = result
    db.add(task)
    db.flush()
    return task
