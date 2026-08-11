from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import Iterable
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import settings
from app.data.models.kie_api import KieFile, KieTask

logger = logging.getLogger(__name__)

RESULT_FILE_KINDS = {"result", "result_watermark"}


def _meta(file: KieFile) -> dict:
    return dict(file.meta_json or {})


def get_task_local_meta(task: KieTask) -> dict:
    payload = dict(task.result_json or {})
    local_meta = payload.get("__local") or {}
    if isinstance(local_meta, dict):
        return dict(local_meta)
    return {}


def set_task_local_meta(task: KieTask, **updates) -> dict:
    payload = dict(task.result_json or {})
    local_meta = get_task_local_meta(task)
    for key, value in updates.items():
        if value is None:
            local_meta.pop(key, None)
        else:
            local_meta[key] = value
    payload["__local"] = local_meta
    task.result_json = payload
    return local_meta


def get_task_download_name_base(task: KieTask) -> str:
    local_meta = get_task_local_meta(task)
    base = str(local_meta.get("download_name_base") or "").strip()
    return base or str(int(task.id))


def get_task_download_filename(task: KieTask, file: KieFile) -> str:
    base = get_task_download_name_base(task)
    if file.kind == "result_watermark":
        return f"{base}-watermark"
    return base


def mark_result_file_pending(file: KieFile, *, filename: str | None = None) -> dict:
    meta = _meta(file)
    meta["local_download_status"] = "queued"
    meta.pop("local_download_error", None)
    meta.pop("local_path", None)
    meta.pop("local_bytes", None)
    if filename:
        meta["filename"] = filename
    file.meta_json = meta
    return meta


def managed_reference_roots() -> tuple[Path, ...]:
    return tuple(
        Path(value).expanduser().resolve()
        for value in (
            settings.BANDIANWA_UPLOAD_STORAGE_DIR,
            settings.GLOBALAIOPC_OMNI_FLASH_UPLOAD_STORAGE_DIR,
            getattr(
                settings,
                "CONTENT_FACTORY_STORAGE_ROOT",
                "/data/gmv_ops/hermes_content_factory",
            ),
        )
        if str(value or "").strip()
    )


def managed_result_roots() -> tuple[Path, ...]:
    return (
        Path(settings.AI_VIDEO_RESULT_STORAGE_DIR).expanduser().resolve(),
        *managed_reference_roots(),
    )


def resolve_managed_file(
    path_value: str | Path | None,
    roots: Iterable[Path],
) -> Path | None:
    if not path_value:
        return None
    try:
        resolved = Path(path_value).expanduser().resolve()
        allowed = any(
            resolved == root or resolved.is_relative_to(root)
            for root in roots
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if not allowed or not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def get_local_path(file: KieFile) -> Path | None:
    path = _meta(file).get("local_path")
    if not path and file.kind == "reference_upload":
        path = file.file_url
    if not path:
        return None
    roots = (
        managed_reference_roots()
        if file.kind == "reference_upload"
        else managed_result_roots()
    )
    return resolve_managed_file(str(path), roots)


def has_local_file(file: KieFile) -> bool:
    return get_local_path(file) is not None


def _extension_from(url: str, content_type: str | None) -> str:
    parsed_ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if parsed_ext in {".mp4", ".mov", ".webm", ".m4v"}:
        return parsed_ext
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "video/webm":
        return ".webm"
    if ct in {"video/quicktime", "video/mov"}:
        return ".mov"
    return ".mp4"


def _target_dir(*, workspace_id: int, owner_user_id: int | None, task_id: int | None) -> Path:
    base = Path(settings.AI_VIDEO_RESULT_STORAGE_DIR).expanduser()
    user_part = f"user_{int(owner_user_id)}" if owner_user_id else "user_unknown"
    task_part = f"task_{int(task_id)}" if task_id else "task_unknown"
    return base / f"workspace_{int(workspace_id)}" / user_part / task_part


async def save_remote_file_locally(
    db: Session,
    *,
    file: KieFile,
    owner_user_id: int | None = None,
    preferred_filename: str | None = None,
) -> KieFile:
    if file.kind not in RESULT_FILE_KINDS:
        return file
    if has_local_file(file):
        return file

    url = (file.download_url or file.file_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return file

    file_id = int(file.id)
    max_bytes = int(settings.AI_VIDEO_RESULT_MAX_BYTES)
    timeout = float(settings.AI_VIDEO_RESULT_DOWNLOAD_TIMEOUT_SECONDS)
    directory = _target_dir(
        workspace_id=int(file.workspace_id),
        owner_user_id=owner_user_id,
        task_id=int(file.task_id) if file.task_id is not None else None,
    )
    directory.mkdir(parents=True, exist_ok=True)

    total = 0
    tmp_path: Path | None = None
    final_path: Path | None = None
    content_type: str | None = None

    try:
        meta = _meta(file)
        if preferred_filename:
            meta["filename"] = preferred_filename
        meta["local_download_status"] = "downloading"
        meta.pop("local_download_error", None)
        file.meta_json = meta
        db.add(file)
        db.commit()
        db.refresh(file)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                file_name_hint = str(preferred_filename or meta.get("filename") or file.id).strip()
                hint_path = Path(file_name_hint)
                stem = hint_path.stem if hint_path.suffix else file_name_hint
                suffix = hint_path.suffix.lower() if hint_path.suffix else ""
                final_ext = suffix or _extension_from(url, content_type)
                final_path = directory / f"{stem}{final_ext}"
                # A broker redelivery or stale-recovery takeover must never
                # share a temporary pathname with the active downloader.
                # Each writer completes into its own file and the final rename
                # remains atomic.
                tmp_path = final_path.with_suffix(
                    final_path.suffix + f".part.{uuid4().hex}"
                )
                with tmp_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("AI video result exceeds local download size limit")
                        handle.write(chunk)
                tmp_path.replace(final_path)

        db.expire_all()
        current_file = db.get(KieFile, file_id)
        if current_file is None:
            raise StaleDataError("Result file was superseded while downloading")
        file = current_file
        meta = _meta(current_file)
        meta.update(
            {
                "local_download_status": "success",
                "local_path": str(final_path),
                "local_bytes": total,
                "filename": final_path.name,
            }
        )
        current_file.meta_json = meta
        if content_type and not current_file.mime_type:
            current_file.mime_type = content_type.split(";")[0].strip()[:64]
        if total and not current_file.size_bytes:
            current_file.size_bytes = total
        db.add(current_file)
        db.commit()
        db.refresh(current_file)
        file = current_file
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        current_file = db.get(KieFile, file_id)
        if current_file is not None:
            if get_local_path(current_file) is not None:
                # Another authoritative downloader finished first. Preserve
                # its successful metadata instead of turning the shared file
                # record back into a false failure.
                return current_file
            meta = _meta(current_file)
            meta.update(
                {
                    "local_download_status": "failed",
                    "local_download_error": str(exc)[:500],
                }
            )
            current_file.meta_json = meta
            db.add(current_file)
            try:
                db.commit()
                db.refresh(current_file)
                file = current_file
            except StaleDataError:
                db.rollback()
        logger.warning(
            "Failed to save AI video result locally",
            extra={"file_id": int(file.id), "url": url, "error": str(exc)},
        )

    return file


__all__ = [
    "RESULT_FILE_KINDS",
    "get_local_path",
    "get_task_download_filename",
    "get_task_download_name_base",
    "get_task_local_meta",
    "has_local_file",
    "mark_result_file_pending",
    "save_remote_file_locally",
    "set_task_local_meta",
]
