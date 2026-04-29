from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.hermes_agent import (
    HermesAgentConversation,
    HermesAgentMessage,
    HermesAgentRun,
    UserFeaturePermission,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def has_feature_permission(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    feature_key: str,
) -> bool:
    row = db.scalar(
        select(UserFeaturePermission).where(
            UserFeaturePermission.workspace_id == int(workspace_id),
            UserFeaturePermission.user_id == int(user_id),
            UserFeaturePermission.feature_key == feature_key,
            UserFeaturePermission.is_enabled.is_(True),
            UserFeaturePermission.deleted_at.is_(None),
        )
    )
    return row is not None


def set_feature_permission(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    feature_key: str,
    is_enabled: bool,
    actor_user_id: int | None,
) -> UserFeaturePermission:
    existing = db.scalar(
        select(UserFeaturePermission).where(
            UserFeaturePermission.workspace_id == int(workspace_id),
            UserFeaturePermission.user_id == int(user_id),
            UserFeaturePermission.feature_key == feature_key,
            UserFeaturePermission.deleted_at.is_(None),
        )
    )
    if existing:
        existing.is_enabled = bool(is_enabled)
        existing.updated_by_user_id = actor_user_id
        db.add(existing)
        db.flush()
        return existing

    row = UserFeaturePermission(
        workspace_id=int(workspace_id),
        user_id=int(user_id),
        feature_key=feature_key,
        is_enabled=bool(is_enabled),
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def list_feature_permissions(db: Session, *, workspace_id: int, user_id: int | None = None) -> list[UserFeaturePermission]:
    stmt = select(UserFeaturePermission).where(
        UserFeaturePermission.workspace_id == int(workspace_id),
        UserFeaturePermission.deleted_at.is_(None),
    )
    if user_id is not None:
        stmt = stmt.where(UserFeaturePermission.user_id == int(user_id))
    return list(db.scalars(stmt.order_by(UserFeaturePermission.user_id.asc(), UserFeaturePermission.feature_key.asc())).all())


def conversation_key(*, workspace_id: int, user_id: int, task_type: str, key: str | None = None) -> str:
    suffix = (key or "default").strip().lower().replace(" ", "-")[:48] or "default"
    return f"ws-{int(workspace_id)}-user-{int(user_id)}-{task_type}-{suffix}"


def get_or_create_conversation(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    task_type: str,
    title: str | None,
    key: str | None = None,
) -> HermesAgentConversation:
    ckey = conversation_key(workspace_id=workspace_id, user_id=user_id, task_type=task_type, key=key)
    row = db.scalar(select(HermesAgentConversation).where(HermesAgentConversation.conversation_key == ckey))
    if row:
        if title and not row.title:
            row.title = title[:255]
            db.add(row)
            db.flush()
        return row
    row = HermesAgentConversation(
        conversation_key=ckey,
        workspace_id=int(workspace_id),
        user_id=int(user_id),
        task_type=task_type,
        title=title[:255] if title else None,
    )
    db.add(row)
    db.flush()
    return row


def create_run(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    task_type: str,
    title: str | None,
    input_text: str,
    input_json: Any | None,
    instructions: str,
    conversation: HermesAgentConversation | None,
    meta_json: dict[str, Any] | None = None,
) -> HermesAgentRun:
    row = HermesAgentRun(
        run_id=uuid4().hex,
        workspace_id=int(workspace_id),
        user_id=int(user_id),
        conversation_id=int(conversation.id) if conversation else None,
        task_type=task_type,
        title=title[:255] if title else None,
        status="pending",
        input_text=input_text,
        input_json=input_json,
        instructions=instructions,
        hermes_conversation=conversation.conversation_key if conversation else None,
        meta_json=meta_json or None,
    )
    db.add(row)
    db.flush()
    return row


def get_run(db: Session, *, workspace_id: int, run_id: str) -> HermesAgentRun | None:
    return db.scalar(
        select(HermesAgentRun).where(
            HermesAgentRun.workspace_id == int(workspace_id),
            HermesAgentRun.run_id == run_id,
        )
    )


def list_runs(
    db: Session,
    *,
    workspace_id: int,
    user_id: int | None = None,
    task_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[HermesAgentRun]:
    stmt = select(HermesAgentRun).where(HermesAgentRun.workspace_id == int(workspace_id))
    if user_id is not None:
        stmt = stmt.where(HermesAgentRun.user_id == int(user_id))
    if task_type:
        stmt = stmt.where(HermesAgentRun.task_type == task_type)
    if status:
        stmt = stmt.where(HermesAgentRun.status == status)
    stmt = stmt.order_by(HermesAgentRun.id.desc()).offset(max(0, int(offset))).limit(max(1, min(int(limit), 100)))
    return list(db.scalars(stmt).all())


def mark_run_processing(db: Session, run: HermesAgentRun) -> HermesAgentRun:
    run.status = "processing"
    run.started_at = now_utc()
    db.add(run)
    db.flush()
    return run


def mark_run_success(
    db: Session,
    run: HermesAgentRun,
    *,
    result_text: str,
    result_json: Any,
    response_id: str | None,
    usage: dict[str, int | None],
    latency_ms: int | None,
) -> HermesAgentRun:
    run.status = "success"
    run.result_text = result_text
    run.result_json = result_json
    run.hermes_response_id = response_id
    run.prompt_tokens = usage.get("prompt_tokens")
    run.completion_tokens = usage.get("completion_tokens")
    run.total_tokens = usage.get("total_tokens")
    run.latency_ms = latency_ms
    run.completed_at = now_utc()
    db.add(run)

    if run.conversation_id:
        conversation = db.get(HermesAgentConversation, int(run.conversation_id))
        if conversation:
            conversation.last_response_id = response_id or conversation.last_response_id
            db.add(conversation)
            add_message(
                db,
                conversation=conversation,
                workspace_id=int(run.workspace_id),
                user_id=int(run.user_id) if run.user_id else None,
                role="assistant",
                content_text=result_text,
                content_json=result_json,
                run_id=run.run_id,
            )
    db.flush()
    return run


def mark_run_failed(db: Session, run: HermesAgentRun, *, code: str, message: str) -> HermesAgentRun:
    run.status = "failed"
    run.error_code = code[:64]
    run.error_message = message
    run.completed_at = now_utc()
    db.add(run)
    db.flush()
    return run


def add_message(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    workspace_id: int,
    user_id: int | None,
    role: str,
    content_text: str | None,
    content_json: Any | None = None,
    run_id: str | None = None,
) -> HermesAgentMessage:
    row = HermesAgentMessage(
        conversation_id=int(conversation.id),
        workspace_id=int(workspace_id),
        user_id=int(user_id) if user_id is not None else None,
        role=role,
        content_text=content_text,
        content_json=content_json,
        run_id=run_id,
    )
    db.add(row)
    db.flush()
    return row


def list_conversations(db: Session, *, workspace_id: int, user_id: int | None = None) -> list[HermesAgentConversation]:
    stmt = select(HermesAgentConversation).where(HermesAgentConversation.workspace_id == int(workspace_id))
    if user_id is not None:
        stmt = stmt.where(HermesAgentConversation.user_id == int(user_id))
    return list(db.scalars(stmt.order_by(HermesAgentConversation.updated_at.desc(), HermesAgentConversation.id.desc())).all())


def list_messages(db: Session, *, workspace_id: int, conversation_id: int, limit: int = 100) -> list[HermesAgentMessage]:
    stmt = (
        select(HermesAgentMessage)
        .where(
            HermesAgentMessage.workspace_id == int(workspace_id),
            HermesAgentMessage.conversation_id == int(conversation_id),
        )
        .order_by(HermesAgentMessage.id.asc())
        .limit(max(1, min(int(limit), 500)))
    )
    return list(db.scalars(stmt).all())
