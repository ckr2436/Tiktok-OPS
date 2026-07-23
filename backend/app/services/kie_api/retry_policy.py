from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieFile, KieTask
from app.services.kie_api.local_storage import (
    RESULT_FILE_KINDS,
    get_local_path,
    get_task_local_meta,
)


MAX_AUTO_RETRIES = 5

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
