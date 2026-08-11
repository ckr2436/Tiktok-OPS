from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieFile, KieTask
from app.services.ai_video.local_storage import (
    RESULT_FILE_KINDS,
    get_local_path,
    get_task_local_meta,
    set_task_local_meta,
)


MAX_AUTO_RETRIES = 5

ARCHIVED_RESULT_KIND_BY_ACTIVE_KIND = {
    "result": "previous_result",
    "result_watermark": "previous_result_watermark",
}
ACTIVE_RESULT_KIND_BY_ARCHIVED_KIND = {
    archived: active
    for active, archived in ARCHIVED_RESULT_KIND_BY_ACTIVE_KIND.items()
}
ARCHIVED_RESULT_FILE_KINDS = set(ACTIVE_RESULT_KIND_BY_ARCHIVED_KIND)

QUOTA_FAILURE_KEYWORDS = (
    "quota",
    "credit",
    "credits",
    "balance",
    "billing",
    "payment",
    "insufficient",
    "not enough",
    "recharge",
    "top up",
    "余额",
    "额度",
    "积分",
    "点数",
    "欠费",
    "充值",
)


def retry_meta(task: KieTask) -> dict[str, Any]:
    return dict(get_task_local_meta(task) or {})


def retry_count(task: KieTask, kind: str = "auto") -> int:
    key = "auto_retry_count" if kind == "auto" else "manual_retry_count"
    try:
        return int(retry_meta(task).get(key) or 0)
    except Exception:  # noqa: BLE001
        return 0


def failure_text(task: KieTask) -> str:
    chunks: list[str] = []
    for value in (task.fail_code, task.fail_msg):
        if value:
            chunks.append(str(value))
    result_json = task.result_json or {}
    if isinstance(result_json, dict):
        for key in ("error", "message", "msg", "failCode", "failMsg"):
            value = result_json.get(key)
            if value:
                chunks.append(str(value))
        for key in ("poll_response", "submit_response"):
            value = result_json.get(key)
            if isinstance(value, dict):
                for sub_key in ("error", "message", "msg", "failCode", "failMsg", "code"):
                    sub_value = value.get(sub_key)
                    if sub_value:
                        chunks.append(str(sub_value))
    return " ".join(chunks).lower()


def is_quota_failure(task: KieTask) -> bool:
    text = failure_text(task)
    return any(keyword in text for keyword in QUOTA_FAILURE_KEYWORDS)


def should_auto_retry(task: KieTask, *, max_retries: int = MAX_AUTO_RETRIES) -> bool:
    if is_quota_failure(task):
        return False
    return retry_count(task, "auto") < int(max_retries)


def next_retry_meta(task: KieTask, *, kind: str) -> dict[str, Any]:
    meta = retry_meta(task)
    key = "auto_retry_count" if kind == "auto" else "manual_retry_count"
    try:
        current = int(meta.get(key) or 0)
    except Exception:  # noqa: BLE001
        current = 0
    meta[key] = current + 1
    return meta


def archive_successful_task_result_files(db: Session, task: KieTask) -> int:
    """Retire the current deliverable without deleting it during regeneration.

    A successful video remains the last known-good deliverable until the new
    generation has been downloaded and validated.  Moving its rows out of the
    active result kinds prevents the download worker and UI from confusing the
    two generations while keeping the local RAID files recoverable.
    """

    if str(task.state or "").strip().lower() != "success":
        return 0
    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind.in_(tuple(sorted(RESULT_FILE_KINDS))),
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    if not files:
        return 0
    for file in files:
        original_kind = str(file.kind)
        file.kind = ARCHIVED_RESULT_KIND_BY_ACTIVE_KIND[original_kind]
        file_meta = dict(file.meta_json or {})
        file_meta["previous_active_kind"] = original_kind
        file.meta_json = file_meta
        db.add(file)
    local_meta = get_task_local_meta(task)
    set_task_local_meta(
        task,
        previous_success={
            "remote_task_id": str(task.task_id or ""),
            "download_name_base": str(
                local_meta.get("download_name_base") or int(task.id)
            ),
            "active_provider": local_meta.get("active_provider"),
            "active_provider_key_id": local_meta.get("active_provider_key_id"),
            "file_ids": [int(file.id) for file in files],
            "archived_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(task)
    db.flush()
    return len(files)


def delete_archived_task_result_files(db: Session, task: KieTask) -> int:
    """Delete the retired deliverable after its replacement passed locally."""

    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind.in_(tuple(sorted(ARCHIVED_RESULT_FILE_KINDS))),
        )
        .all()
    )
    for file in files:
        local_path = get_local_path(file)
        if local_path is not None:
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        db.delete(file)
    if files:
        set_task_local_meta(task, previous_success=None)
        db.add(task)
        db.flush()
    return len(files)


def restore_archived_task_result_files(
    db: Session,
    task: KieTask,
    *,
    failure_code: str | None,
    failure_message: str | None,
) -> bool:
    """Restore the last successful deliverable when regeneration fails."""

    archived = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind.in_(tuple(sorted(ARCHIVED_RESULT_FILE_KINDS))),
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    if not archived:
        return False

    # Remove only the failed replacement generation. Its generation-scoped
    # filename cannot alias the archived successful file.
    delete_task_result_files(db, task)
    for file in archived:
        file.kind = ACTIVE_RESULT_KIND_BY_ARCHIVED_KIND[str(file.kind)]
        file_meta = dict(file.meta_json or {})
        file_meta.pop("previous_active_kind", None)
        file.meta_json = file_meta
        db.add(file)

    meta = get_task_local_meta(task)
    previous = dict(meta.get("previous_success") or {})
    previous_remote_task_id = str(previous.get("remote_task_id") or "").strip()
    if previous_remote_task_id:
        task.task_id = previous_remote_task_id
    task.state = "success"
    task.fail_code = None
    task.fail_msg = None
    meta["last_regeneration_failure"] = {
        "code": str(failure_code or "regeneration_failed")[:128],
        "message": str(failure_message or "Regeneration failed")[:512],
        "restored_at": datetime.now(timezone.utc).isoformat(),
    }
    if previous.get("download_name_base"):
        meta["download_name_base"] = str(previous["download_name_base"])
    if previous.get("active_provider"):
        meta["active_provider"] = previous["active_provider"]
    if previous.get("active_provider_key_id") is not None:
        meta["active_provider_key_id"] = previous["active_provider_key_id"]
    meta.pop("previous_success", None)
    for key in (
        "download_enqueued_at",
        "download_started_at",
        "download_execution_token",
        "download_error",
        "poll_owner_task_id",
        "poll_heartbeat_at",
        "poll_heartbeat_provider",
    ):
        meta.pop(key, None)
    task.result_json = {
        "__local": meta,
        "restored_previous_generation": True,
    }
    db.add(task)
    db.flush()
    return True


def delete_task_result_files(db: Session, task: KieTask) -> int:
    files = (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind.in_(tuple(sorted(RESULT_FILE_KINDS))),
        )
        .all()
    )
    for file in files:
        local_path = get_local_path(file)
        if local_path is None:
            continue
        try:
            Path(local_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if not files:
        return 0
    return (
        db.query(KieFile)
        .filter(
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.task_id == int(task.id),
            KieFile.kind.in_(tuple(sorted(RESULT_FILE_KINDS))),
        )
        .delete(synchronize_session=False)
    )
