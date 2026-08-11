from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.data.models.hermes_agent import (
    HermesContentEvaluation,
    HermesContentFactoryProject,
    HermesContentRuntimeEvent,
    HermesContentSegmentRun,
    HermesContentVariantRun,
)
from app.data.models.kie_api import KieFile, KieTask
from app.services.ai_video.local_storage import get_local_path, set_task_local_meta


FAILED_TRANSPORT_STATES = {
    "failed",
    "fail",
    "error",
    "timeout",
    "cancelled",
    "canceled",
}
SEMANTIC_LEGACY_FAIL_CODES = {
    "segment_execution_qa",
    "product_visual_qa",
    "voice_continuity_qa",
    "spoken_copy_qa",
    "copy_delivery_missing",
    "segment_release_quality_gate",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_task_project_id(task: KieTask) -> int:
    params = dict(task.input_json or {})
    value = params.get("content_factory_project_id")
    return int(value) if str(value or "").strip().isdigit() else 0


def _result_path_by_task(
    db: Session,
    task_ids: Iterable[int],
) -> dict[int, Path]:
    normalized = sorted({int(value) for value in task_ids if int(value) > 0})
    if not normalized:
        return {}
    result: dict[int, Path] = {}
    rows = (
        db.query(KieFile)
        .filter(
            KieFile.task_id.in_(tuple(normalized)),
            KieFile.kind.in_(("result", "result_watermark")),
        )
        .order_by(KieFile.task_id.asc(), KieFile.id.asc())
        .all()
    )
    for row in rows:
        task_id = int(row.task_id or 0)
        if task_id in result:
            continue
        path = get_local_path(row)
        if path is not None and path.is_file() and path.stat().st_size > 0:
            result[task_id] = path
    return result


def _transport_projection(task: KieTask, local_path: Path | None) -> tuple[str, str | None, str | None]:
    state = str(task.state or "").strip().lower()
    fail_code = str(task.fail_code or "").strip().lower()
    if local_path is not None:
        return "downloaded", None, None
    if state in FAILED_TRANSPORT_STATES:
        if fail_code in SEMANTIC_LEGACY_FAIL_CODES:
            # Old workers changed a provider-success row into a semantic
            # failure.  Without a local file it is not safe to claim success,
            # but keep the transport ledger distinct from that obsolete code.
            return "result_missing", "local_result_missing", str(task.fail_msg or "")
        return "failed_provider", fail_code or "provider_failure", str(task.fail_msg or "")
    if state in {"success", "succeeded", "completed", "complete"}:
        return "awaiting_download", None, None
    if state in {"downloading"}:
        return "downloading", None, None
    if state in {"waiting_dependency"}:
        return "waiting_dependency", None, None
    if state in {"queued_local", "queued", "queuing"}:
        return "queued", None, None
    if state in {"submitting"}:
        return "submitting", None, None
    return "running", None, None


def _runtime_event_type(state: str) -> str:
    if state == "downloaded":
        return "segment.downloaded"
    if state in {"failed_provider", "result_missing"}:
        return "segment.failed"
    return "segment.progressed"


def _append_runtime_event(
    db: Session,
    *,
    segment: HermesContentSegmentRun,
    task: KieTask,
    state: str,
    local_path: Path | None,
    event_origin: str,
) -> HermesContentRuntimeEvent | None:
    project = db.get(HermesContentFactoryProject, int(segment.project_id))
    if project is None:
        return None
    variant = db.get(HermesContentVariantRun, int(segment.variant_run_id))
    if variant is None:
        return None
    payload = {
        "schema_version": "content-runtime-event-v1",
        "segment_run_id": int(segment.id),
        "provider_task_row_id": int(task.id),
        "provider_task_id": str(task.task_id or ""),
        "transport_state": state,
        "local_file_path": str(local_path) if local_path is not None else None,
        "fail_code": str(task.fail_code or "") or None,
        "event_origin": str(event_origin or "provider_commit")[:48],
    }
    event_type = _runtime_event_type(state)
    key = _digest({"event_type": event_type, **payload})
    existing = (
        db.query(HermesContentRuntimeEvent)
        .filter(HermesContentRuntimeEvent.idempotency_key == key)
        .one_or_none()
    )
    if existing is not None:
        return None
    event = HermesContentRuntimeEvent(
        idempotency_key=key,
        workspace_id=int(segment.workspace_id),
        project_id=int(segment.project_id),
        execution_id=int(variant.execution_id),
        variant_run_id=int(variant.id),
        segment_run_id=int(segment.id),
        provider_task_row_id=int(task.id),
        event_type=event_type,
        status="pending",
        payload_json=payload,
        attempts=0,
        available_at=_now(),
    )
    db.add(event)
    db.info["hermes_runtime_events_pending"] = True
    return event


def _refresh_variant_state(db: Session, variant_id: int) -> None:
    variant = db.get(HermesContentVariantRun, int(variant_id))
    if variant is None or str(variant.state or "") == "composed":
        return
    rows = (
        db.query(HermesContentSegmentRun)
        .filter(HermesContentSegmentRun.variant_run_id == int(variant_id))
        .all()
    )
    latest: dict[int, HermesContentSegmentRun] = {}
    for row in rows:
        current = latest.get(int(row.segment_index))
        if current is None or int(row.attempt) > int(current.attempt):
            latest[int(row.segment_index)] = row
    states = {str(row.state or "") for row in latest.values()}
    if latest and states == {"downloaded"}:
        variant.state = "segments_downloaded"
        variant.error_class = None
        variant.error_message = None
    elif states.intersection({"failed_provider", "result_missing"}):
        variant.state = "segment_failed"
    else:
        variant.state = "segments_running"
    db.add(variant)


def sync_content_provider_tasks(
    db: Session,
    *,
    task_ids: Iterable[int],
    event_origin: str = "provider_commit",
) -> dict[str, Any]:
    """Atomically project provider transport rows into the execution ledger."""

    normalized = sorted({int(value) for value in task_ids if int(value) > 0})
    if not normalized:
        return {"tasks": 0, "segments": 0, "events": 0, "projects": []}
    tasks = (
        db.query(KieTask)
        .filter(KieTask.id.in_(tuple(normalized)))
        .all()
    )
    task_by_id = {int(row.id): row for row in tasks}
    segments = (
        db.query(HermesContentSegmentRun)
        .filter(HermesContentSegmentRun.provider_task_row_id.in_(tuple(normalized)))
        .all()
    )
    local_paths = _result_path_by_task(db, normalized)
    changed = 0
    emitted = 0
    variant_ids: set[int] = set()
    project_ids: set[int] = set()
    now = _now()
    for segment in segments:
        task_id = int(segment.provider_task_row_id or 0)
        task = task_by_id.get(task_id)
        if task is None:
            continue
        # Tenant/project ownership is part of the projection boundary.
        if int(task.workspace_id) != int(segment.workspace_id):
            continue
        task_project_id = _content_task_project_id(task)
        if task_project_id and task_project_id != int(segment.project_id):
            continue
        local_path = local_paths.get(task_id)
        state, error_class, error_message = _transport_projection(task, local_path)
        previous = str(segment.state or "")
        row_meta = dict(segment.meta_json or {})
        if local_path is not None:
            stat = local_path.stat()
            unchanged = bool(
                previous == "downloaded"
                and str(segment.local_file_path or "") == str(local_path)
                and str(segment.output_sha256 or "").strip()
                and int(row_meta.get("output_size_bytes") or -1) == int(stat.st_size)
                and int(row_meta.get("output_mtime_ns") or -1) == int(stat.st_mtime_ns)
            )
            segment.local_file_path = str(local_path)
            segment.output_sha256 = (
                str(segment.output_sha256) if unchanged else _file_sha256(local_path)
            )
            row_meta.update(
                {
                    "output_size_bytes": int(stat.st_size),
                    "output_mtime_ns": int(stat.st_mtime_ns),
                }
            )
        segment.state = state
        segment.error_class = error_class[:96] if error_class else None
        segment.error_message = error_message[:4000] if error_message else None
        segment.meta_json = row_meta
        if state in {"downloaded", "failed_provider", "result_missing"}:
            segment.completed_at = segment.completed_at or now
        elif previous != state:
            segment.completed_at = None
        db.add(segment)
        variant_ids.add(int(segment.variant_run_id))
        project_ids.add(int(segment.project_id))
        if previous != state:
            changed += 1
            if _append_runtime_event(
                db,
                segment=segment,
                task=task,
                state=state,
                local_path=local_path,
                event_origin=event_origin,
            ) is not None:
                emitted += 1
        # Repair the obsolete mixed-state representation only when local RAID
        # proves that provider transport actually completed.  Preserve the
        # prior semantic evidence under local metadata, then restore KieTask to
        # transport truth so old rows cannot trigger another paid generation.
        legacy_fail_code = str(task.fail_code or "").strip().lower()
        if (
            local_path is not None
            and str(task.state or "").strip().lower() in FAILED_TRANSPORT_STATES
            and legacy_fail_code in SEMANTIC_LEGACY_FAIL_CODES
        ):
            set_task_local_meta(
                task,
                legacy_content_quality_incident={
                    "code": legacy_fail_code,
                    "message": str(task.fail_msg or "")[:4000],
                    "reconciled_at": now.isoformat(),
                    "local_file_path": str(local_path),
                },
            )
            task.state = "success"
            task.fail_code = None
            task.fail_msg = None
            db.add(task)
    for variant_id in variant_ids:
        _refresh_variant_state(db, variant_id)
    if project_ids:
        (
            db.query(HermesContentFactoryProject)
            .filter(HermesContentFactoryProject.id.in_(tuple(project_ids)))
            .update(
                {HermesContentFactoryProject.updated_at: now},
                synchronize_session=False,
            )
        )
    return {
        "tasks": len(tasks),
        "segments": changed,
        "events": emitted,
        "projects": sorted(project_ids),
    }


def record_content_evaluation(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    task: KieTask | None,
    evaluation_kind: str,
    policy_version: str,
    status: str,
    blocking: bool,
    input_sha256: str,
    evidence: dict[str, Any],
) -> HermesContentEvaluation:
    segment = None
    variant = None
    execution_id = None
    if task is not None and int(task.id or 0) > 0:
        segment = (
            db.query(HermesContentSegmentRun)
            .filter(
                HermesContentSegmentRun.project_id == int(project.id),
                HermesContentSegmentRun.provider_task_row_id == int(task.id),
            )
            .order_by(HermesContentSegmentRun.attempt.desc())
            .first()
        )
        if segment is not None:
            variant = db.get(HermesContentVariantRun, int(segment.variant_run_id))
            execution_id = int(variant.execution_id) if variant is not None else None
    evaluation_key = _digest(
        {
            "project_id": int(project.id),
            "provider_task_row_id": int(task.id) if task is not None else None,
            "evaluation_kind": str(evaluation_kind),
            "policy_version": str(policy_version),
            "input_sha256": str(input_sha256),
        }
    )
    row = (
        db.query(HermesContentEvaluation)
        .filter(HermesContentEvaluation.evaluation_key == evaluation_key)
        .one_or_none()
    )
    if row is None:
        row = HermesContentEvaluation(
            evaluation_key=evaluation_key,
            workspace_id=int(project.workspace_id),
            user_id=project.user_id,
            project_id=int(project.id),
            execution_id=execution_id,
            variant_run_id=int(variant.id) if variant is not None else None,
            segment_run_id=int(segment.id) if segment is not None else None,
            provider_task_row_id=int(task.id) if task is not None else None,
            evaluation_kind=str(evaluation_kind)[:64],
            policy_version=str(policy_version)[:128],
            status=str(status)[:32],
            blocking=bool(blocking),
            input_sha256=str(input_sha256)[:64],
            evidence_json=dict(evidence or {}),
        )
        db.add(row)
    else:
        row.status = str(status)[:32]
        row.blocking = bool(blocking)
        row.evidence_json = dict(evidence or {})
        db.add(row)
    return row


def reconcile_content_execution_ledger(
    db: Session,
    *,
    project_id: int | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    query = db.query(HermesContentSegmentRun.provider_task_row_id).filter(
        HermesContentSegmentRun.provider_task_row_id.is_not(None)
    )
    if project_id is not None:
        query = query.filter(HermesContentSegmentRun.project_id == int(project_id))
    task_ids = [
        int(value)
        for value, in query.order_by(HermesContentSegmentRun.id.asc())
        .limit(max(1, min(int(limit), 5000)))
        .all()
        if int(value or 0) > 0
    ]
    return sync_content_provider_tasks(
        db,
        task_ids=task_ids,
        event_origin="periodic_reconciliation",
    )


__all__ = [
    "FAILED_TRANSPORT_STATES",
    "SEMANTIC_LEGACY_FAIL_CODES",
    "record_content_evaluation",
    "reconcile_content_execution_ledger",
    "sync_content_provider_tasks",
]
