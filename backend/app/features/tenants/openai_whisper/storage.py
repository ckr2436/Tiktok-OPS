"""Utility helpers to persist Whisper jobs under a configurable directory."""
from __future__ import annotations

import json
from json import JSONDecodeError
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.core.config import settings

BASE_DIR = Path(settings.OPENAI_WHISPER_STORAGE_DIR).expanduser()


class MetadataCorruptedError(RuntimeError):
    """Raised when a metadata JSON file cannot be decoded."""


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    raw = path.read_text()
    if not raw.strip():
        raise MetadataCorruptedError(f"metadata file {path} is empty")
    try:
        return json.loads(raw)
    except JSONDecodeError as exc:
        raise MetadataCorruptedError(f"metadata file {path} contains invalid JSON") from exc


def _dump_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    """Persist JSON atomically to avoid truncated metadata files."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(_dump_json(payload))
    temp_path.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir(workspace_id: int) -> Path:
    return _ensure_dir(BASE_DIR / f"workspace_{workspace_id}")


def job_dir(workspace_id: int, job_id: str) -> Path:
    return _ensure_dir(workspace_dir(workspace_id) / job_id)


def uploads_dir(workspace_id: int) -> Path:
    return _ensure_dir(workspace_dir(workspace_id) / "uploads")


def upload_dir(workspace_id: int, upload_id: str) -> Path:
    directory = uploads_dir(workspace_id) / upload_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def metadata_path(job_directory: Path) -> Path:
    return job_directory / "job.json"


def result_path(job_directory: Path) -> Path:
    return job_directory / "result.json"


def subtitles_path(job_directory: Path, variant: str) -> Path:
    variant = variant.lower()
    if variant == "source":
        return job_directory / "source.srt"
    if variant == "translation":
        return job_directory / "translation.srt"
    raise ValueError("Unknown subtitle variant.")


def write_metadata(workspace_id: int, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = job_dir(workspace_id, job_id)
    payload.setdefault("created_at", _utc_now())
    payload.setdefault("updated_at", payload["created_at"])
    _write_json_file(metadata_path(directory), payload)
    return payload


def upload_metadata_path(upload_directory: Path) -> Path:
    return upload_directory / "upload.json"


def write_upload_metadata(workspace_id: int, upload_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = upload_dir(workspace_id, upload_id)
    payload.setdefault("created_at", _utc_now())
    payload.setdefault("updated_at", payload["created_at"])
    _write_json_file(upload_metadata_path(directory), payload)
    return payload


def load_upload_metadata(workspace_id: int, upload_id: str) -> Dict[str, Any]:
    directory = uploads_dir(workspace_id) / upload_id
    metadata_file = upload_metadata_path(directory)
    return _read_json_file(metadata_file)


def delete_upload(workspace_id: int, upload_id: str) -> None:
    directory = uploads_dir(workspace_id) / upload_id
    shutil.rmtree(directory, ignore_errors=True)


def _atomic_update(path: Path, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    existing = _read_json_file(path)
    updated = updater(existing)
    updated["updated_at"] = _utc_now()
    _write_json_file(path, updated)
    return updated


def update_metadata(workspace_id: int, job_id: str, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    directory = job_dir(workspace_id, job_id)
    return _atomic_update(metadata_path(directory), updater)


def load_metadata(workspace_id: int, job_id: str) -> Dict[str, Any]:
    directory = BASE_DIR / f"workspace_{workspace_id}" / job_id
    meta_file = metadata_path(directory)
    return _read_json_file(meta_file)


def save_results(workspace_id: int, job_id: str, result_payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = job_dir(workspace_id, job_id)
    _write_json_file(result_path(directory), result_payload)

    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["result"] = result_payload
        meta["status"] = "success"
        meta["error"] = None
        meta["completed_at"] = _utc_now()
        return meta

    return update_metadata(workspace_id, job_id, _apply)


def mark_failed(workspace_id: int, job_id: str, message: str) -> Dict[str, Any]:
    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["status"] = "failed"
        meta["error"] = message
        return meta

    return update_metadata(workspace_id, job_id, _apply)


def mark_processing(workspace_id: int, job_id: str, started_at: Optional[str] = None) -> Dict[str, Any]:
    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["status"] = "processing"
        if started_at:
            meta["started_at"] = started_at
        else:
            meta["started_at"] = _utc_now()
        return meta

    return update_metadata(workspace_id, job_id, _apply)


def write_subtitles_file(workspace_id: int, job_id: str, variant: str, content: str) -> Path:
    directory = job_dir(workspace_id, job_id)
    dest = subtitles_path(directory, variant)
    dest.write_text(content, encoding="utf-8")
    return dest


def resolve_download_path(workspace_id: int, job_id: str, variant: str) -> Path:
    directory = BASE_DIR / f"workspace_{workspace_id}" / job_id
    path = subtitles_path(directory, variant)
    if not path.exists():
        raise FileNotFoundError(path)
    return path

