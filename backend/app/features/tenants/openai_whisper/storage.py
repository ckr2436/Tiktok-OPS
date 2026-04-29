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


def _safe_child_path(base: Path, candidate: Path) -> Path | None:
    try:
        base_resolved = base.resolve()
        candidate_resolved = candidate.resolve()
        candidate_resolved.relative_to(base_resolved)
        return candidate_resolved
    except Exception:
        return None


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


def contact_sheet_path(job_directory: Path) -> Path:
    return job_directory / "contact_sheet.png"


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
    _write_json_file(metadata_path(directory), _ensure_status_defaults(payload))
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


def delete_uploads_older_than(workspace_id: int | None, cutoff_ts: float, *, limit: int = 500) -> int:
    roots = []
    if workspace_id is not None:
        roots.append(BASE_DIR / f"workspace_{workspace_id}" / "uploads")
    else:
        roots.extend(BASE_DIR.glob("workspace_*/uploads"))

    removed = 0
    for root in roots:
        if not root.exists() or removed >= limit:
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if removed >= limit:
                break
            try:
                if child.stat().st_mtime >= cutoff_ts:
                    continue
                safe = _safe_child_path(root, child)
                if safe:
                    shutil.rmtree(safe, ignore_errors=True)
                    removed += 1
            except FileNotFoundError:
                continue
    return removed


def _atomic_update(path: Path, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    existing = _read_json_file(path)
    updated = _ensure_status_defaults(updater(existing))
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
        meta["subtitle_status"] = "success"
        meta["error"] = None
        meta["subtitle_error"] = None
        meta["completed_at"] = _utc_now()
        return meta

    return update_metadata(workspace_id, job_id, _apply)


def mark_failed(workspace_id: int, job_id: str, message: str) -> Dict[str, Any]:
    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["status"] = "failed"
        meta["subtitle_status"] = "failed"
        meta["error"] = message
        meta["subtitle_error"] = message
        return meta

    return update_metadata(workspace_id, job_id, _apply)


def mark_processing(workspace_id: int, job_id: str, started_at: Optional[str] = None) -> Dict[str, Any]:
    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["status"] = "processing"
        meta["subtitle_status"] = "processing"
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


def resolve_contact_sheet_path(workspace_id: int, job_id: str) -> Path:
    directory = BASE_DIR / f"workspace_{workspace_id}" / job_id
    path = contact_sheet_path(directory)
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def delete_job_files(workspace_id: int, job_id: str) -> bool:
    base = workspace_dir(workspace_id)
    directory = base / job_id
    safe = _safe_child_path(base, directory)
    if not safe or not safe.exists():
        return False
    shutil.rmtree(safe, ignore_errors=True)
    return True


def purge_large_artifacts(workspace_id: int, job_id: str) -> list[str]:
    base = workspace_dir(workspace_id)
    directory = base / job_id
    safe_dir = _safe_child_path(base, directory)
    if not safe_dir or not safe_dir.exists():
        return []

    removed: list[str] = []
    keep_names = {"job.json", "result.json", "source.srt", "translation.srt"}
    for child in safe_dir.iterdir():
        try:
            if child.name in keep_names:
                continue
            safe = _safe_child_path(safe_dir, child)
            if not safe:
                continue
            if safe.is_dir():
                shutil.rmtree(safe, ignore_errors=True)
            else:
                safe.unlink(missing_ok=True)
            removed.append(child.name)
        except FileNotFoundError:
            continue
    if removed:
        try:
            update_metadata(
                workspace_id,
                job_id,
                lambda meta: {
                    **meta,
                    "video_path": None,
                    "download_url": None,
                    "contact_sheet_url": None,
                    "large_artifacts_purged_at": _utc_now(),
                    "large_artifacts_purged": True,
                },
            )
        except Exception:
            pass
    return removed


def _ensure_status_defaults(meta: Dict[str, Any]) -> Dict[str, Any]:
    if "do_subtitle" not in meta:
        meta["do_subtitle"] = True
    if "do_contact_sheet" not in meta:
        meta["do_contact_sheet"] = False
    if "do_download_only" not in meta:
        meta["do_download_only"] = False
    if "subtitle_status" not in meta:
        meta["subtitle_status"] = "pending" if meta.get("do_subtitle") else "skipped"
    if "contact_sheet_status" not in meta:
        meta["contact_sheet_status"] = "pending" if meta.get("do_contact_sheet") else "skipped"
    if "download_status" not in meta:
        meta["download_status"] = "pending" if meta.get("share_url") else "skipped"
    meta["status"] = derive_overall_status(meta)
    return meta


def derive_overall_status(meta: Dict[str, Any]) -> str:
    statuses = []
    for key in ("subtitle_status", "contact_sheet_status", "download_status"):
        status = meta.get(key)
        if status and status != "skipped":
            statuses.append(status)
    if not statuses:
        return meta.get("status") or "pending"
    if any(st == "failed" for st in statuses):
        return "failed"
    if any(st in {"processing", "pending"} for st in statuses):
        return "processing"
    return "success"


def update_component_status(
    workspace_id: int,
    job_id: str,
    component: str,
    *,
    status: str,
    error: Optional[str] = None,
    url: Optional[str] = None,
    started_at: Optional[str] = None,
) -> Dict[str, Any]:
    def _apply(meta: Dict[str, Any]) -> Dict[str, Any]:
        status_key = f"{component}_status"
        error_key = "error" if component == "subtitle" else f"{component}_error"
        url_key = f"{component}_url"
        meta[status_key] = status
        if error_key:
            meta[error_key] = error
        if url_key in meta or url is not None:
            meta[url_key] = url
        if component == "subtitle" and started_at:
            meta["started_at"] = started_at
        meta["status"] = derive_overall_status(meta)
        return meta

    return update_metadata(workspace_id, job_id, _apply)

