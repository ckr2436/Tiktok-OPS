"""Utility helpers to persist Whisper jobs under /data/gmv_ops."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

BASE_DIR = Path("/data/gmv_ops/openai_whisper")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir(workspace_id: int) -> Path:
    return _ensure_dir(BASE_DIR / f"workspace_{workspace_id}")


def job_dir(workspace_id: int, job_id: str) -> Path:
    return _ensure_dir(workspace_dir(workspace_id) / job_id)


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
    metadata_path(directory).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _atomic_update(path: Path, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    existing = json.loads(path.read_text())
    updated = updater(existing)
    updated["updated_at"] = _utc_now()
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2))
    return updated


def update_metadata(workspace_id: int, job_id: str, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    directory = job_dir(workspace_id, job_id)
    return _atomic_update(metadata_path(directory), updater)


def load_metadata(workspace_id: int, job_id: str) -> Dict[str, Any]:
    directory = BASE_DIR / f"workspace_{workspace_id}" / job_id
    meta_file = metadata_path(directory)
    if not meta_file.exists():
        raise FileNotFoundError(meta_file)
    data = json.loads(meta_file.read_text())
    return data


def save_results(workspace_id: int, job_id: str, result_payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = job_dir(workspace_id, job_id)
    result_path(directory).write_text(json.dumps(result_payload, ensure_ascii=False, indent=2))

    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["result"] = result_payload
        meta["status"] = "success"
        meta["error"] = None
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

