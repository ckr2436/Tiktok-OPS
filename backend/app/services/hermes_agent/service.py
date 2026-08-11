from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import ADMIN_ROLES, SessionUser
from app.core.errors import APIError
from app.services.audit import log_event

from . import repository
from .client import HermesAgentClient, extract_output_text, extract_usage
from .prompts import build_input_text, build_instructions, feature_for_task, normalize_task_type


def ensure_hermes_enabled() -> None:
    if not settings.HERMES_AGENT_ENABLED:
        raise APIError("HERMES_DISABLED", "Hermes Agent is not enabled.", 503)


def ensure_user_can_use_task(
    db: Session,
    *,
    workspace_id: int,
    me: SessionUser,
    task_type: str,
) -> None:
    """Authorize a tenant user for a Hermes task without bypassing the existing session model."""
    if me.is_platform_admin:
        raise APIError("FORBIDDEN", "Platform users cannot access tenant Hermes Agent APIs.", 403)
    if int(me.workspace_id) != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    task_type = normalize_task_type(task_type)
    if me.role in ADMIN_ROLES:
        return

    if not settings.HERMES_AGENT_ALLOW_MEMBER:
        raise APIError("FORBIDDEN", "Hermes Agent permission required.", 403)

    # Default mode: any active member can use Hermes. Production can opt into explicit permission.
    if not settings.HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION:
        return

    feature = feature_for_task(task_type)
    if repository.has_feature_permission(db, workspace_id=workspace_id, user_id=me.id, feature_key=feature):
        return
    if feature != "hermes_agent.use" and repository.has_feature_permission(
        db,
        workspace_id=workspace_id,
        user_id=me.id,
        feature_key="hermes_agent.use",
    ):
        return

    raise APIError("FORBIDDEN", "Hermes Agent permission required.", 403)


def _validate_input_size(input_text: str) -> None:
    limit = int(settings.HERMES_AGENT_MAX_INPUT_CHARS)
    if len(input_text) > limit:
        raise APIError(
            "HERMES_INPUT_TOO_LARGE",
            f"Hermes Agent input exceeds {limit} characters.",
            413,
        )


async def create_and_run(
    db: Session,
    *,
    workspace_id: int,
    me: SessionUser,
    task_type: str,
    title: str | None,
    user_input: str | None,
    input_json: Any | None,
    workspace_context: dict[str, Any] | None,
    conversation_key: str | None,
    async_mode: bool,
    request_ip: str | None = None,
    user_agent: str | None = None,
):
    ensure_hermes_enabled()
    task_type = normalize_task_type(task_type)
    ensure_user_can_use_task(db, workspace_id=workspace_id, me=me, task_type=task_type)

    instructions = build_instructions(task_type=task_type, workspace_context=workspace_context)
    input_text = build_input_text(task_type=task_type, user_input=user_input, input_json=input_json)
    _validate_input_size(input_text)

    conversation = repository.get_or_create_conversation(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        task_type=task_type,
        title=title,
        key=conversation_key,
    )
    repository.add_message(
        db,
        conversation=conversation,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        role="user",
        content_text=input_text,
        content_json=input_json,
    )
    run = repository.create_run(
        db,
        workspace_id=int(workspace_id),
        user_id=int(me.id),
        task_type=task_type,
        title=title,
        input_text=input_text,
        input_json=input_json,
        instructions=instructions,
        conversation=conversation,
        meta_json={"workspace_context": workspace_context or {}},
    )

    log_event(
        db,
        action="hermes_agent.run.create",
        resource_type="hermes_agent_run",
        resource_id=int(run.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=request_ip,
        user_agent=user_agent,
        workspace_id=int(workspace_id),
        details={
            "run_id": run.run_id,
            "task_type": task_type,
            "async": bool(async_mode),
            "title": title,
        },
    )
    db.flush()

    if async_mode:
        from app.tasks.hermes_agent.tasks import run_hermes_agent

        async_result = run_hermes_agent.apply_async(
            kwargs={"workspace_id": int(workspace_id), "run_id": run.run_id},
            queue=settings.HERMES_AGENT_TASK_QUEUE,
        )
        run.celery_task_id = async_result.id
        db.add(run)
        db.flush()
        return run

    return await execute_run(db, workspace_id=int(workspace_id), run_id=run.run_id)


async def execute_run(db: Session, *, workspace_id: int, run_id: str):
    run = repository.get_run(db, workspace_id=int(workspace_id), run_id=run_id)
    if run is None:
        raise APIError("HERMES_RUN_NOT_FOUND", "Hermes Agent run not found.", 404)
    if run.status == "success":
        return run
    if run.status == "processing":
        raise APIError("HERMES_RUN_IN_PROGRESS", "Hermes Agent run is already processing.", 409)

    repository.mark_run_processing(db, run)
    db.flush()

    conversation = db.get(repository.HermesAgentConversation, int(run.conversation_id)) if run.conversation_id else None
    previous_response_id = conversation.last_response_id if conversation else None

    try:
        payload, latency_ms = await HermesAgentClient().create_response(
            input_text=run.input_text or "",
            instructions=run.instructions or "",
            conversation=run.hermes_conversation,
            previous_response_id=previous_response_id,
            metadata={
                "workspace_id": str(run.workspace_id),
                "user_id": str(run.user_id or ""),
                "run_id": run.run_id,
                "task_type": run.task_type,
            },
        )
        # The upstream response is the auditable execution result.  Do not
        # apply a second GMV-side character cap: HermesAgentRun.result_text and
        # the mirrored conversation message are long-text columns, so a local
        # truncation would make the UI/history disagree with the real model
        # output even though the complete provider envelope was received.
        result_text = extract_output_text(payload)
        usage = extract_usage(payload)
        response_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        repository.mark_run_success(
            db,
            run,
            result_text=result_text,
            result_json=payload,
            response_id=response_id,
            usage=usage,
            latency_ms=latency_ms,
        )
        log_event(
            db,
            action="hermes_agent.run.success",
            resource_type="hermes_agent_run",
            resource_id=int(run.id),
            actor_user_id=int(run.user_id) if run.user_id else None,
            actor_workspace_id=int(run.workspace_id),
            workspace_id=int(run.workspace_id),
            details={
                "run_id": run.run_id,
                "task_type": run.task_type,
                "response_id": response_id,
                "usage": usage,
                "latency_ms": latency_ms,
            },
        )
        db.flush()
        return run
    except APIError as exc:
        repository.mark_run_failed(db, run, code=exc.code, message=exc.message)
        log_event(
            db,
            action="hermes_agent.run.failed",
            resource_type="hermes_agent_run",
            resource_id=int(run.id),
            actor_user_id=int(run.user_id) if run.user_id else None,
            actor_workspace_id=int(run.workspace_id),
            workspace_id=int(run.workspace_id),
            details={"run_id": run.run_id, "task_type": run.task_type, "code": exc.code, "message": exc.message},
        )
        db.flush()
        raise
    except Exception as exc:  # noqa: BLE001
        repository.mark_run_failed(db, run, code="HERMES_INTERNAL_ERROR", message=str(exc))
        log_event(
            db,
            action="hermes_agent.run.failed",
            resource_type="hermes_agent_run",
            resource_id=int(run.id),
            actor_user_id=int(run.user_id) if run.user_id else None,
            actor_workspace_id=int(run.workspace_id),
            workspace_id=int(run.workspace_id),
            details={"run_id": run.run_id, "task_type": run.task_type, "code": "HERMES_INTERNAL_ERROR", "message": str(exc)},
        )
        db.flush()
        raise APIError("HERMES_INTERNAL_ERROR", "Hermes Agent execution failed.", 500) from exc
