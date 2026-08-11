from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieTask
from app.services.ai_video.local_storage import set_task_local_meta
from app.services.ai_video.retry_policy import next_retry_meta
from app.services.audit import log_event


LOCAL_TASK_PREFIX = "local-ai-video-"
LEGACY_LOCAL_TASK_PREFIXES = ("local-bandianwa-", "local-globalaiopc-")


def is_local_task_id(value: Any) -> bool:
    task_id = str(value or "")
    return task_id.startswith((LOCAL_TASK_PREFIX, *LEGACY_LOCAL_TASK_PREFIXES))


def _effective_prompt(input_params: Mapping[str, Any]) -> str:
    for key in ("provider_prompt", "full_prompt", "prompt"):
        value = input_params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def create_local_video_task(
    db: Session,
    *,
    workspace_id: int,
    key_id: int,
    input_params: Mapping[str, Any],
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieTask:
    """Create one provider-neutral local task owned by the unified router."""

    from app.services.ai_video.accounts import get_key_by_id, normalize_video_model_id

    key = get_key_by_id(db, key_id=int(key_id))
    if key is None or not bool(key.is_active):
        raise ValueError("AI video provider credential not found or inactive")

    model = normalize_video_model_id(str(input_params.get("model") or "").strip())
    if not model:
        raise ValueError("model is required")
    clean_input = {
        key_name: value
        for key_name, value in dict(input_params or {}).items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }
    # This column is the platform's logical model identity. Provider-specific
    # aliases belong to route metadata only; persisting them here makes an
    # otherwise authorized Content Factory task disappear from AI Video's
    # canonical model history and contaminates later retry generations.
    clean_input["model"] = model
    requested_provider = str(
        clean_input.get("requested_service_provider")
        or clean_input.get("service_provider")
        or "auto"
    ).strip().lower()
    routing_mode = str(clean_input.get("routing_mode") or "").strip().lower()
    if routing_mode not in {"auto", "pinned"}:
        routing_mode = "auto" if requested_provider == "auto" else "pinned"
    clean_input["routing_mode"] = routing_mode
    clean_input["requested_service_provider"] = (
        requested_provider if routing_mode == "pinned" else "auto"
    )
    prompt_value = _effective_prompt(clean_input)
    task = KieTask(
        workspace_id=int(workspace_id),
        key_id=int(key.id),
        created_by_user_id=int(actor_user_id) if actor_user_id is not None else None,
        model=model,
        task_id=f"{LOCAL_TASK_PREFIX}{uuid4().hex}",
        state="queued_local",
        prompt=prompt_value[:2000] or None,
        input_json=clean_input,
    )
    db.add(task)
    db.flush()
    set_task_local_meta(
        task,
        download_name_base=str(int(task.id)),
        active_provider=str(key.provider_key),
        active_provider_key_id=int(key.id),
    )
    db.add(task)
    db.flush()
    log_event(
        db,
        action="ai_video.task.create_local",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id or int(workspace_id),
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=int(workspace_id),
        details={
            "model": model,
            "task_id": task.task_id,
            "key_id": int(key.id),
            "provider_key": str(key.provider_key),
        },
    )
    return task


def reset_video_task_for_retry(
    db: Session,
    *,
    task: KieTask,
    input_params: Mapping[str, Any] | None = None,
    retry_kind: str = "manual",
) -> KieTask:
    if input_params is not None:
        from app.services.ai_video.accounts import normalize_video_model_id

        source_input = dict(task.input_json or {})
        source_input.update(dict(input_params or {}))
        clean_input = {
            key: value
            for key, value in source_input.items()
            if value is not None and not (isinstance(value, str) and not value.strip())
        }
        if not clean_input.get("model"):
            clean_input["model"] = task.model
        task.input_json = clean_input
        task.model = normalize_video_model_id(
            str(clean_input.get("model") or task.model)
        )
        clean_input["model"] = task.model
        prompt_value = _effective_prompt(clean_input)
        task.prompt = prompt_value[:2000] or None

    meta = next_retry_meta(task, kind=retry_kind)
    if str(retry_kind).strip().lower() == "manual":
        try:
            previous_generation = int(meta.get("generation_epoch") or 0)
        except (TypeError, ValueError):
            previous_generation = 0
        try:
            manual_generation = int(meta.get("manual_retry_count") or 0)
        except (TypeError, ValueError):
            manual_generation = 0
        generation_epoch = max(previous_generation + 1, manual_generation, 1)
        meta["generation_epoch"] = generation_epoch
        # Keep the previous successful artifact at its original path until the
        # replacement is fully downloaded and validated. A generation-scoped
        # basename prevents the atomic replacement download from overwriting
        # that rollback copy.
        meta["download_name_base"] = f"{int(task.id)}-g{generation_epoch}"
        # A user-triggered retry is a new scheduling round.  Preserve explicit
        # quota exclusions for this paid logical task, but do not inherit the
        # transient route deny-list or the previous supervisor decision.
        meta["attempted_provider_key_ids"] = []
        meta["attempted_provider_keys"] = []
        meta["provider_retry_counts"] = {}
        for stale_key in (
            "active_provider",
            "active_provider_key_id",
            "video_provider_recovery",
            "provider_cycle_reopened_at",
            "doubao_failed_account_bridge_ids",
        ):
            meta.pop(stale_key, None)
    meta.update(
        {
            "download_enqueued_at": None,
            "download_started_at": None,
            "download_finished_at": None,
            "download_error": None,
            "submit_worker_task_id": None,
            "submit_worker_heartbeat_at": None,
        }
    )
    for stale_key in (
        "doubao_account_bridge_id",
        "doubao_remote_accepted_at",
        "doubao_remote_timeout_at",
    ):
        meta.pop(stale_key, None)
    retry_kind_value = str(retry_kind or "").strip().lower()
    if retry_kind_value == "manual":
        # A manual retry is a new scheduling generation and will be published
        # with a new Celery request id.  Release the previous poll owner so the
        # newly authorized delivery can claim the row.
        for lease_key in (
            "poll_owner_task_id",
            "poll_heartbeat_at",
            "poll_heartbeat_provider",
        ):
            meta.pop(lease_key, None)
    elif meta.get("poll_owner_task_id"):
        # Provider-submit failures call ``self.retry`` and therefore keep the
        # same Celery request id.  Preserve that owner across the local task-id
        # reset.  Clearing it allowed an older delayed delivery to claim the
        # freshly queued row and exhaust its own retry budget over a newer
        # delivery.  Refresh the heartbeat so the preserved owner fence covers
        # the bounded retry countdown as well as the synchronous submit.
        meta["poll_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    task.task_id = f"{LOCAL_TASK_PREFIX}{uuid4().hex}"
    task.state = "queued_local"
    task.result_json = {"__local": meta}
    task.fail_code = None
    task.fail_msg = None
    db.add(task)
    db.flush()
    log_event(
        db,
        action="ai_video.task.retry",
        resource_type="kie_task",
        resource_id=int(task.id),
        actor_workspace_id=int(task.workspace_id),
        workspace_id=int(task.workspace_id),
        details={
            "model": task.model,
            "task_id": task.task_id,
            "retry_kind": retry_kind,
            "key_id": int(task.key_id),
        },
    )
    return task


__all__ = [
    "LEGACY_LOCAL_TASK_PREFIXES",
    "LOCAL_TASK_PREFIX",
    "create_local_video_task",
    "is_local_task_id",
    "reset_video_task_for_retry",
]
