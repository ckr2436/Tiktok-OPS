from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.data.models.kie_api import KieApiKey, KieTask
from app.services.bandianwa.client import BandianwaApiError
from app.services.bandianwa.tasks import (
    LOCAL_TASK_PREFIX,
    refresh_bandianwa_task_status,
    reset_bandianwa_task_for_retry,
    submit_bandianwa_task,
)
from app.services.globalaiopc.client import GlobalAiOpcApiError
from app.services.globalaiopc.tasks import (
    refresh_GlobalAiOpc_task_status,
    submit_GlobalAiOpc_task,
)
from app.services.google_gemini.tasks import (
    GoogleGeminiApiError,
    refresh_google_gemini_task,
    submit_google_gemini_task,
)
from app.services.volcengine.tasks import (
    VolcengineApiError,
    refresh_volcengine_task,
    submit_volcengine_task,
)
from app.services.toapis.tasks import ToApisApiError, refresh_toapis_task, submit_toapis_task
from app.services.kie_api.accounts import (
    BANDIANWA_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    get_effective_key,
    has_active_key,
    key_supports_model,
    normalize_provider_key,
    normalize_video_model_id,
    resolve_video_model_key,
)
from app.services.kie_api.local_storage import get_task_local_meta, set_task_local_meta
from app.services.kie_api.retry_policy import (
    MAX_AUTO_RETRIES,
    QUOTA_FAILURE_KEYWORDS,
    delete_task_result_files,
    is_quota_failure,
    retry_count,
    should_auto_retry,
)
from app.tasks.kie_ai.video_result_download_tasks import queue_task_result_download

logger = get_task_logger(__name__)

SUPPORTED_VIDEO_PROVIDERS = {
    BANDIANWA_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
}
PROVIDER_API_ERRORS = (
    BandianwaApiError,
    GlobalAiOpcApiError,
    GoogleGeminiApiError,
    VolcengineApiError,
    ToApisApiError,
)
PROVIDER_TASK_ERRORS = PROVIDER_API_ERRORS + (ValueError,)
CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE = "cf_variant_superseded"
CONTENT_FACTORY_NOT_AUTHORITATIVE_CODE = "cf_task_not_authoritative"


def _db_session() -> Session:
    return SessionLocal()


def _load_task(db: Session, *, workspace_id: int, local_task_id: int, for_update: bool = False) -> KieTask:
    query = (
        db.query(KieTask)
        .join(KieApiKey, KieTask.key_id == KieApiKey.id)
        .filter(
            KieTask.id == int(local_task_id),
            KieTask.workspace_id == int(workspace_id),
            KieApiKey.provider_key.in_(tuple(SUPPORTED_VIDEO_PROVIDERS)),
        )
    )
    if for_update:
        query = query.populate_existing().with_for_update()
    task = query.one_or_none()
    if task is None:
        raise ValueError("Bandianwa task not found")
    return task


def _payload(task: KieTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.task_id,
        "state": task.state,
        "fail_code": task.fail_code,
        "fail_msg": task.fail_msg,
    }


def _local_meta(task: KieTask) -> dict[str, Any]:
    return dict(get_task_local_meta(task) or {})


def _parse_utc_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _poll_heartbeat_is_recent(
    task: KieTask,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> bool:
    heartbeat = _parse_utc_datetime(_local_meta(task).get("poll_heartbeat_at"))
    current = now or datetime.now(timezone.utc)
    return bool(
        heartbeat is not None
        and heartbeat >= current - timedelta(seconds=max(1, int(max_age_seconds)))
    )


def _claim_poll_owner(
    task: KieTask,
    *,
    owner_task_id: str,
    max_age_seconds: int,
) -> bool:
    """Claim one live poller per local task before any provider network call."""
    owner = str(owner_task_id or "").strip()
    meta = _local_meta(task)
    existing_owner = str(meta.get("poll_owner_task_id") or "").strip()
    if existing_owner and existing_owner != owner and _poll_heartbeat_is_recent(
        task,
        max_age_seconds=max_age_seconds,
    ):
        return False
    set_task_local_meta(
        task,
        submit_enqueued_at=None,
        poll_owner_task_id=owner or None,
        poll_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        poll_heartbeat_provider=_active_provider(task),
    )
    task.updated_at = datetime.now()
    return True


def _content_project_drains_submitted_video(
    project: HermesContentFactoryProject | None,
    *,
    task_id: int,
) -> bool:
    """Keep submitted video work alive during an automatic quality pause."""
    if project is None or str(project.status or "").lower() != "paused":
        return False
    state = dict(project.state_json or {})
    config = dict(project.config_json or {})
    active_task_ids = {
        int(value)
        for value in list(state.get("ai_video_task_ids") or [])
        if str(value).strip().isdigit()
    }
    if int(task_id) not in active_task_ids:
        return False
    reason = str(state.get("pause_reason_code") or "").strip().lower()
    if reason == "manual":
        return False
    if reason == "creative_visual_replan_exhausted":
        return True
    if not bool(config.get("manual_paused", False)):
        return True
    quality_event = dict(state.get("creative_visual_replan_exhausted") or {})
    quality_at = _parse_utc_datetime(quality_event.get("at"))
    manual_at = _parse_utc_datetime(state.get("manual_paused_at") or state.get("paused_at"))
    return bool(quality_at is not None and (manual_at is None or quality_at >= manual_at))


def _service_provider(task: KieTask) -> str:
    value = str((task.input_json or {}).get("service_provider") or "auto").strip().lower()
    if value in {"kyy", "keyiyun", "globalaiopc", "globalaiopc-omni-flash"}:
        return "kyy"
    if value in {"bandianwa", "bdw"}:
        return "bandianwa"
    if value in {"google", "gemini", "google-gemini", "google_gemini"}:
        return GOOGLE_GEMINI_PROVIDER_KEY
    if value in {"volcengine", "volcengine-ark", "ark"}:
        return VOLCENGINE_PROVIDER_KEY
    if value in {"toapis", "to-api", "to_api"}:
        return TOAPIS_PROVIDER_KEY
    return "auto"


def _active_provider(task: KieTask) -> str:
    value = str(_local_meta(task).get("active_provider") or "").strip().lower()
    if value:
        return normalize_provider_key(value)
    requested = _service_provider(task)
    return BANDIANWA_PROVIDER_KEY if requested == "auto" else normalize_provider_key(requested)


def _task_key(db: Session, task: KieTask) -> KieApiKey:
    key = db.get(KieApiKey, int(task.key_id))
    model = normalize_video_model_id(task.model)
    try:
        if key is not None:
            return resolve_video_model_key(
                db,
                model_id=model,
                reference_count=_reference_count(task),
                reference_video_count=_reference_video_count(task),
                aspect_ratio=str((task.input_json or {}).get("aspect_ratio") or ""),
                reference_mode=str(
                    (task.input_json or {}).get("video_frame_mode")
                    or (task.input_json or {}).get("reference_mode")
                    or "reference"
                ),
                duration=_requested_duration(task),
                resolution=_requested_resolution(task),
                key_id=int(key.id),
            )
    except ValueError:
        pass
    if key is None or not key.is_active or not key_supports_model(key, model):
        replacement = _next_provider_key(db, task)
        if replacement is None:
            raise ValueError("Selected video provider API key is missing or inactive")
        task.key_id = int(replacement.id)
        key = replacement
        return key
    replacement = _next_provider_key(db, task)
    if replacement is None:
        raise ValueError("Selected video provider does not support the requested model inputs")
    task.key_id = int(replacement.id)
    return replacement


def _submit_current_provider(db: Session, task: KieTask) -> KieTask:
    key = _task_key(db, task)
    provider = normalize_provider_key(key.provider_key)
    set_task_local_meta(task, active_provider=provider, active_provider_key_id=int(key.id))
    params = dict(task.input_json or {})
    params["service_provider"] = provider
    task.input_json = params
    if provider == KYY_PROVIDER_KEY:
        return asyncio.run(submit_GlobalAiOpc_task(db, task=task, key=key, file_key_id=int(key.id)))
    if provider == GOOGLE_GEMINI_PROVIDER_KEY:
        return asyncio.run(submit_google_gemini_task(db, task=task))
    if provider == VOLCENGINE_PROVIDER_KEY:
        return asyncio.run(submit_volcengine_task(db, task=task))
    if provider == TOAPIS_PROVIDER_KEY:
        return asyncio.run(submit_toapis_task(db, task=task))
    return asyncio.run(submit_bandianwa_task(db, task=task))


def _refresh_current_provider(db: Session, task: KieTask) -> KieTask:
    task = _load_task(
        db,
        workspace_id=int(task.workspace_id),
        local_task_id=int(task.id),
        for_update=True,
    )
    previous_provider = _active_provider(task)
    previous_key_id = int(_local_meta(task).get("active_provider_key_id") or task.key_id)
    try:
        key = _task_key(db, task)
    except ValueError as exc:
        task.state = "failed"
        task.fail_code = "provider_disabled"
        task.fail_msg = str(exc)[:512]
        db.add(task)
        return task
    provider = normalize_provider_key(key.provider_key)
    if provider != previous_provider or int(key.id) != previous_key_id:
        delete_task_result_files(db, task)
        task = reset_bandianwa_task_for_retry(db, task=task, retry_kind="provider_scope_change")
        params = dict(task.input_json or {})
        params["service_provider"] = provider
        params["routing_failover_from"] = previous_provider
        task.input_json = params
        set_task_local_meta(
            task,
            active_provider=provider,
            active_provider_key_id=int(key.id),
            routing_failover_from_key_id=previous_key_id,
            provider_scope_failover_at=datetime.now(timezone.utc).isoformat(),
        )
        return _submit_current_provider(db, task)
    if provider == KYY_PROVIDER_KEY:
        return asyncio.run(refresh_GlobalAiOpc_task_status(db, task=task, key=key, file_key_id=int(key.id)))
    if provider == GOOGLE_GEMINI_PROVIDER_KEY:
        return asyncio.run(refresh_google_gemini_task(db, task=task))
    if provider == VOLCENGINE_PROVIDER_KEY:
        return asyncio.run(refresh_volcengine_task(db, task=task))
    if provider == TOAPIS_PROVIDER_KEY:
        return asyncio.run(refresh_toapis_task(db, task=task))
    return asyncio.run(refresh_bandianwa_task_status(db, task=task))


def _is_omni_task(task: KieTask) -> bool:
    model = str(task.model or (task.input_json or {}).get("model") or "").replace("-", "_").lower()
    return model == "omni_flash"


def _is_content_factory_task(task: KieTask) -> bool:
    payload = task.input_json or {}
    return bool(str(payload.get("content_factory_project_key") or "").strip())


def _content_factory_task_authority(
    project: HermesContentFactoryProject | None,
    task: KieTask,
) -> tuple[bool, str]:
    """Prove that a queued delivery still owns its project variant segment."""
    if not _is_content_factory_task(task):
        return True, "not_content_factory"

    meta = _local_meta(task)
    if (
        str(task.fail_code or "").strip().lower() == CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE
        or bool(meta.get("superseded_at"))
        or bool(meta.get("superseded_by_task_ids"))
    ):
        return False, "superseded_marker"
    if project is None:
        return False, "project_missing"

    params = dict(task.input_json or {})
    try:
        variant_index = int(
            params.get("content_factory_variant_index")
            or params.get("content_factory_video_index")
            or meta.get("content_factory_variant_index")
            or meta.get("content_factory_video_index")
            or 0
        )
        segment_index = int(
            params.get("content_factory_segment_index")
            or meta.get("content_factory_segment_index")
            or 0
        )
    except (TypeError, ValueError):
        return False, "invalid_coordinates"
    if variant_index <= 0 or segment_index <= 0:
        return False, "missing_coordinates"

    state = dict(project.state_json or {})
    groups = [
        group
        for group in list(state.get("ai_video_groups") or [])
        if int(group.get("video_index") or 0) == variant_index
    ]
    if not groups:
        return False, "variant_group_missing"
    for group in groups:
        for segment in list(group.get("segments") or []):
            if int(segment.get("segment_index") or 0) != segment_index:
                continue
            if int(segment.get("task_id") or 0) == int(task.id):
                group_stage_id = dict(group).get("source_stage_id")
                task_stage_id = params.get("content_factory_source_stage_id")
                if str(group_stage_id or "").strip() and str(
                    group_stage_id
                ) != str(task_stage_id or ""):
                    return False, "source_stage_identity_mismatch"
                group_manifest = str(
                    dict(group).get("media_manifest_sha256") or ""
                ).strip()
                task_manifest = str(
                    params.get("content_factory_media_manifest_sha256") or ""
                ).strip()
                if group_manifest and group_manifest != task_manifest:
                    return False, "media_manifest_identity_mismatch"
                return True, "current_variant_segment"
            return False, "segment_owned_by_replacement"
    return False, "segment_missing"


def _content_factory_execution_authority(
    db: Session,
    task: KieTask,
) -> tuple[bool, str]:
    if not _is_content_factory_task(task):
        return True, "not_content_factory"
    params = dict(task.input_json or {})
    project_key = str(params.get("content_factory_project_key") or "").strip()
    project = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.project_key == project_key,
            HermesContentFactoryProject.workspace_id == int(task.workspace_id),
        )
        .one_or_none()
    )
    authorized, reason = _content_factory_task_authority(project, task)
    meta = _local_meta(task)
    remote_visible = not str(task.task_id or "").startswith(LOCAL_TASK_PREFIX)
    if not authorized:
        if remote_visible and bool(meta.get("drain_non_authoritative")):
            return True, "superseded_remote_drain"
        return authorized, reason

    params = dict(task.input_json or {})
    source_stage_id = params.get("content_factory_source_stage_id")
    if not str(source_stage_id or "").strip().isdigit():
        # Compatibility boundary for media groups created before stage-bound
        # execution identity was introduced. New deliveries always carry it.
        return True, reason
    source_stage = db.get(HermesContentFactoryStage, int(source_stage_id))
    if (
        source_stage is None
        or int(source_stage.project_id) != int(project.id)
        or str(source_stage.stage or "") != "VIDEO_PROMPTS"
        or str(source_stage.status or "").lower() != "success"
    ):
        if remote_visible and bool(meta.get("drain_non_authoritative")):
            return True, "superseded_remote_drain"
        return False, "source_stage_not_authoritative"
    return True, reason


def _quarantine_non_authoritative_content_task(
    task: KieTask,
    *,
    reason: str,
) -> KieTask:
    """Make stale broker deliveries terminal without touching a provider."""
    was_explicitly_superseded = (
        str(task.fail_code or "").strip().lower() == CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE
        or reason in {"superseded_marker", "segment_owned_by_replacement"}
    )
    task.state = "failed"
    task.fail_code = (
        CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE
        if was_explicitly_superseded
        else CONTENT_FACTORY_NOT_AUTHORITATIVE_CODE
    )
    task.fail_msg = f"Content-factory task ignored before provider I/O: {reason}."[:512]
    set_task_local_meta(
        task,
        authority_rejected_at=datetime.now(timezone.utc).isoformat(),
        authority_rejected_reason=str(reason),
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    task.updated_at = datetime.now()
    return task


def _reference_count(task: KieTask) -> int:
    payload = task.input_json or {}
    count = 0
    for key in ("reference_images", "images"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            count += 1
        elif isinstance(value, (list, tuple, set)):
            count += len([item for item in value if str(item).strip()])
    value = payload.get("reference_file_paths")
    if isinstance(value, (list, tuple, set)):
        count += len([item for item in value if item])
    try:
        count += int(payload.get("reference_file_count") or 0)
    except Exception:  # noqa: BLE001
        pass
    return count


def _reference_video_count(task: KieTask) -> int:
    payload = task.input_json or {}
    value = payload.get("reference_video_file_paths")
    if isinstance(value, (list, tuple, set)):
        return len([item for item in value if item])
    return 1 if value else 0


def _requested_duration(task: KieTask) -> int | None:
    payload = task.input_json or {}
    try:
        return int(payload.get("seconds") or payload.get("duration"))
    except (TypeError, ValueError):
        return None


def _requested_resolution(task: KieTask) -> str | None:
    value = str((task.input_json or {}).get("resolution") or "").strip().lower()
    return value or None


def _load_kyy_key(db: Session) -> KieApiKey:
    return get_effective_key(db, require_active=True, provider_key=KYY_PROVIDER_KEY)


def _kyy_provider_available(db: Session) -> bool:
    return has_active_key(db, provider_key=KYY_PROVIDER_KEY)


def _mark_kyy_provider_disabled(db: Session, task: KieTask, *, manual: bool = False) -> dict[str, Any]:
    set_task_local_meta(
        task,
        active_provider="kyy",
        kyy_fallback_attempted=True,
        kyy_fallback_blocked=True,
        kyy_fallback_blocked_reason="provider_disabled",
    )
    task.state = "failed"
    task.fail_code = "kyy_provider_disabled"
    task.fail_msg = (
        "客易云 KYY API key 已禁用，任务不会继续调用客易云。"
        if manual
        else "客易云 KYY API key 已禁用，自动 fallback 已停止；请启用 key 或切换服务商后重试。"
    )
    db.add(task)
    db.commit()
    return _payload(task)


def _mark_failed(
    db: Session,
    task: KieTask,
    exc: Exception,
    *,
    active_provider: str | None = None,
) -> dict[str, Any]:
    if active_provider:
        set_task_local_meta(task, active_provider=active_provider)
    task.state = "failed"
    task.fail_code = task.fail_code or "bandianwa_worker_error"
    task.fail_msg = str(exc)[:512]
    db.add(task)
    db.commit()
    return _payload(task)


def _provider_error_is_quota_failure(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(keyword in text for keyword in QUOTA_FAILURE_KEYWORDS)


def _mark_provider_quota_failure(db: Session, task: KieTask, exc: Exception) -> KieTask:
    """Persist a non-retryable balance failure before attempting failover."""
    provider = _active_provider(task)
    meta = _local_meta(task)
    quota_failed_key_ids = [
        int(value)
        for value in list(meta.get("provider_quota_failed_key_ids") or [])
        if str(value).strip().isdigit()
    ]
    if int(task.key_id) not in quota_failed_key_ids:
        quota_failed_key_ids.append(int(task.key_id))
    task.state = "failed"
    task.fail_code = "provider_quota_exhausted"
    task.fail_msg = str(exc)[:512]
    set_task_local_meta(
        task,
        active_provider=provider,
        provider_quota_failed_at=datetime.now(timezone.utc).isoformat(),
        provider_quota_failed_provider=provider,
        provider_quota_failed_key_ids=quota_failed_key_ids,
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    task.updated_at = datetime.now()
    db.add(task)
    db.commit()
    return task


def _mark_kyy_switch_blocked(db: Session, task: KieTask, reference_count: int) -> dict[str, Any]:
    set_task_local_meta(
        task,
        kyy_fallback_attempted=True,
        kyy_fallback_blocked=True,
        kyy_fallback_blocked_reason="too_many_reference_images",
        kyy_fallback_reference_count=reference_count,
    )
    task.state = "failed"
    task.fail_code = "kyy_fallback_reference_limit"
    task.fail_msg = (
        f"斑点蛙已连续 3 次失败；当前参考图 {reference_count} 张，"
        "客易云 Omni Flash 最多支持 5 张。请减少参考图后手动切换客易云再生成。"
    )
    db.add(task)
    db.commit()
    return _payload(task)


def _auto_retry_in_place(db: Session, task: KieTask) -> KieTask:
    task = _load_task(
        db,
        workspace_id=int(task.workspace_id),
        local_task_id=int(task.id),
        for_update=True,
    )
    if str(task.state or "").lower() not in {"failed", "error", "timeout"}:
        return task
    provider = _active_provider(task)
    meta = _local_meta(task)
    provider_attempts = dict(meta.get("provider_retry_counts") or {})
    provider_attempts[provider] = int(provider_attempts.get(provider) or 0) + 1
    logger.info(
        "Video provider task failed, auto retrying in place",
        extra={
            "workspace_id": task.workspace_id,
            "local_task_id": task.id,
            "state": task.state,
            "fail_code": task.fail_code,
            "fail_msg": task.fail_msg,
            "provider": provider,
            "provider_retry": provider_attempts[provider],
        },
    )
    delete_task_result_files(db, task)
    task = reset_bandianwa_task_for_retry(db, task=task, retry_kind="auto")
    set_task_local_meta(task, provider_retry_counts=provider_attempts, active_provider=provider)
    task = _submit_current_provider(db, task)
    db.commit()
    return task


def _provider_retry_count(task: KieTask) -> int:
    provider = _active_provider(task)
    try:
        return int(dict(_local_meta(task).get("provider_retry_counts") or {}).get(provider) or 0)
    except (TypeError, ValueError):
        return 0


def _next_provider_key(db: Session, task: KieTask) -> KieApiKey | None:
    meta = _local_meta(task)
    attempted = {
        int(value) for value in list(meta.get("attempted_provider_key_ids") or [])
        if str(value).isdigit()
    }
    attempted.add(int(task.key_id))
    try:
        return resolve_video_model_key(
            db,
            model_id=normalize_video_model_id(task.model),
            reference_count=_reference_count(task),
            reference_video_count=_reference_video_count(task),
            aspect_ratio=str((task.input_json or {}).get("aspect_ratio") or ""),
            reference_mode=str(
                (task.input_json or {}).get("video_frame_mode")
                or (task.input_json or {}).get("reference_mode")
                or "reference"
            ),
            duration=_requested_duration(task),
            resolution=_requested_resolution(task),
            exclude_key_ids=attempted,
        )
    except ValueError:
        return None


def _switch_to_next_provider_in_place(db: Session, task: KieTask) -> KieTask:
    task = _load_task(
        db,
        workspace_id=int(task.workspace_id),
        local_task_id=int(task.id),
        for_update=True,
    )
    if str(task.state or "").lower() not in {"failed", "error", "timeout"}:
        return task
    next_key = _next_provider_key(db, task)
    if next_key is None:
        return task
    old_key_id = int(task.key_id)
    old_provider = _active_provider(task)
    meta = _local_meta(task)
    attempted = [
        int(value) for value in list(meta.get("attempted_provider_key_ids") or [])
        if str(value).isdigit()
    ]
    if old_key_id not in attempted:
        attempted.append(old_key_id)
    delete_task_result_files(db, task)
    task = reset_bandianwa_task_for_retry(db, task=task, retry_kind="auto")
    task.key_id = int(next_key.id)
    params = dict(task.input_json or {})
    params["service_provider"] = str(next_key.provider_key)
    params["routing_failover_from"] = old_provider
    task.input_json = params
    set_task_local_meta(
        task,
        attempted_provider_key_ids=attempted,
        active_provider=str(next_key.provider_key),
        active_provider_key_id=int(next_key.id),
        provider_failover_count=int(meta.get("provider_failover_count") or 0) + 1,
    )
    # Persist provider ownership before network I/O. A failed failover then
    # resumes from the replacement provider and cannot resubmit to the
    # exhausted provider under the same logical task.
    db.add(task)
    db.commit()
    task = _load_task(
        db,
        workspace_id=int(task.workspace_id),
        local_task_id=int(task.id),
        for_update=True,
    )
    task = _submit_current_provider(db, task)
    db.commit()
    logger.info(
        "Video task switched provider in place",
        extra={
            "workspace_id": task.workspace_id,
            "local_task_id": task.id,
            "from_provider": old_provider,
            "to_provider": next_key.provider_key,
        },
    )
    return task


def _switch_to_kyy_in_place(db: Session, task: KieTask) -> KieTask:
    reference_count = _reference_count(task)
    if reference_count > 5:
        _mark_kyy_switch_blocked(db, task, reference_count)
        return task

    logger.info(
        "Bandianwa Omni task switching to KYY",
        extra={
            "workspace_id": task.workspace_id,
            "local_task_id": task.id,
            "reference_count": reference_count,
        },
    )
    delete_task_result_files(db, task)
    task = reset_bandianwa_task_for_retry(db, task=task, retry_kind="auto")
    set_task_local_meta(
        task,
        active_provider="kyy",
        kyy_fallback_attempted=True,
        kyy_fallback_reference_count=reference_count,
    )
    db.add(task)
    db.flush()
    kyy_key = _load_kyy_key(db)
    task = asyncio.run(
        submit_GlobalAiOpc_task(
            db,
            task=task,
            key=kyy_key,
            file_key_id=int(task.key_id),
        )
    )
    db.commit()
    return task


def _mark_kyy_switch_unavailable(db: Session, task: KieTask) -> None:
    set_task_local_meta(
        task,
        kyy_fallback_attempted=True,
        kyy_fallback_blocked=True,
        kyy_fallback_blocked_reason="provider_disabled",
    )
    db.add(task)
    db.commit()


def _should_switch_to_kyy(db: Session, task: KieTask) -> bool:
    if _is_content_factory_task(task):
        return False
    if not _is_omni_task(task):
        return False
    if _service_provider(task) == "bandianwa":
        return False
    if _active_provider(task) == "kyy":
        return False
    if is_quota_failure(task):
        return False
    if _local_meta(task).get("kyy_fallback_attempted"):
        return False
    if retry_count(task, "auto") < 3:
        return False
    if not _kyy_provider_available(db):
        _mark_kyy_switch_unavailable(db, task)
        return False
    return True


def _should_retry_provider(task: KieTask) -> bool:
    if _is_content_factory_task(task):
        return False
    if is_quota_failure(task):
        return False
    return _provider_retry_count(task) < 3


def _should_switch_provider(db: Session, task: KieTask) -> bool:
    if _is_content_factory_task(task):
        return is_quota_failure(task) and _next_provider_key(db, task) is not None
    if is_quota_failure(task):
        return _next_provider_key(db, task) is not None
    return _provider_retry_count(task) >= 3 and _next_provider_key(db, task) is not None


def _content_factory_dependency_pending(task: KieTask) -> bool:
    """Never submit a chained Omni segment without its continuity frame."""
    if not _is_content_factory_task(task):
        return False
    params = dict(task.input_json or {})
    if str(params.get("model") or "").strip().lower() != "omni_flash":
        return False
    try:
        segment_index = int(params.get("content_factory_segment_index") or 0)
    except (TypeError, ValueError):
        segment_index = 0
    if segment_index <= 1:
        return False
    return not bool(params.get("content_factory_first_frame"))


@celery_app.task(
    name="bandianwa.video.submit_and_poll",
    bind=True,
    queue="gmv.tasks.ai_video",
    max_retries=MAX_AUTO_RETRIES,
    default_retry_delay=15,
)
def submit_and_poll_bandianwa_video_task(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    interval_seconds: int = 15,
    timeout_seconds: int = 10 * 60,
    **_: Any,
) -> dict[str, Any]:
    db = _db_session()
    start_ts = time.monotonic()

    try:
        try:
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id, for_update=True)
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(task, reason=authority_reason)
                db.add(task)
                db.commit()
                logger.warning(
                    "Ignored non-authoritative content-factory video delivery before provider I/O",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "reason": authority_reason,
                    },
                )
                return _payload(task)
            if _content_factory_dependency_pending(task):
                task.state = "waiting_dependency"
                task.fail_code = None
                task.fail_msg = None
                db.commit()
                return _payload(task)
            request_id = str(getattr(self.request, "id", "") or "")
            if not _claim_poll_owner(
                task,
                owner_task_id=request_id,
                max_age_seconds=max(60, int(interval_seconds) * 4),
            ):
                db.commit()
                return _payload(task)
            db.add(task)
            db.commit()
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id, for_update=True)
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(task, reason=authority_reason)
                db.add(task)
                db.commit()
                return _payload(task)
            if str(task.task_id or "").startswith(LOCAL_TASK_PREFIX):
                task = _submit_current_provider(db, task)
                set_task_local_meta(
                    task,
                    poll_owner_task_id=request_id or None,
                    poll_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                    poll_heartbeat_provider=_active_provider(task),
                )
                task.updated_at = datetime.now()
                db.commit()
                if str(task.state or "").lower() == "downloading":
                    queue_task_result_download(
                        workspace_id=int(workspace_id),
                        local_task_id=int(local_task_id),
                    )
                    return _payload(task)
        except PROVIDER_TASK_ERRORS as exc:
            db.rollback()
            task = _load_task(
                db,
                workspace_id=workspace_id,
                local_task_id=local_task_id,
                for_update=True,
            )
            if _provider_error_is_quota_failure(exc):
                task = _mark_provider_quota_failure(db, task, exc)
                if _next_provider_key(db, task) is None:
                    return _payload(task)
                try:
                    task = _switch_to_next_provider_in_place(db, task)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                except PROVIDER_TASK_ERRORS as failover_exc:
                    db.rollback()
                    task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                    return _mark_failed(
                        db,
                        task,
                        failover_exc,
                        active_provider=_active_provider(task),
                    )
            else:
                if self.request.retries >= self.max_retries:
                    return _mark_failed(db, task, exc, active_provider=_active_provider(task))
                raise self.retry(exc=exc)

        while True:
            task = _load_task(
                db,
                workspace_id=workspace_id,
                local_task_id=local_task_id,
                for_update=True,
            )
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(task, reason=authority_reason)
                db.add(task)
                db.commit()
                return _payload(task)
            pre_refresh_state = str(task.state or "").lower()
            if pre_refresh_state == "downloading":
                db.commit()
                queue_task_result_download(
                    workspace_id=int(workspace_id),
                    local_task_id=int(local_task_id),
                )
                return _payload(task)
            if pre_refresh_state == "success":
                return _payload(task)
            try:
                task = _refresh_current_provider(db, task)
                set_task_local_meta(
                    task,
                    poll_owner_task_id=request_id or None,
                    poll_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                    poll_heartbeat_provider=_active_provider(task),
                )
                task.updated_at = datetime.now()
                db.commit()
            except PROVIDER_TASK_ERRORS as exc:
                db.rollback()
                task = _load_task(
                    db,
                    workspace_id=workspace_id,
                    local_task_id=local_task_id,
                    for_update=True,
                )
                if _provider_error_is_quota_failure(exc):
                    task = _mark_provider_quota_failure(db, task, exc)
                    if _next_provider_key(db, task) is None:
                        return _payload(task)
                    try:
                        task = _switch_to_next_provider_in_place(db, task)
                        start_ts = time.monotonic()
                        if str(task.state or "").lower() == "downloading":
                            queue_task_result_download(
                                workspace_id=int(workspace_id),
                                local_task_id=int(local_task_id),
                            )
                            return _payload(task)
                        continue
                    except PROVIDER_TASK_ERRORS as failover_exc:
                        db.rollback()
                        task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                        return _mark_failed(
                            db,
                            task,
                            failover_exc,
                            active_provider=_active_provider(task),
                        )
                if self.request.retries >= self.max_retries:
                    return _mark_failed(db, task, exc)
                logger.warning(
                    "Bandianwa query error, will retry",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "error": str(exc),
                    },
                )
                raise self.retry(exc=exc)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "Bandianwa polling iteration failed",
                    extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
                )
                raise exc

            state = (task.state or "").lower()
            if state == "downloading":
                queue_task_result_download(
                    workspace_id=int(workspace_id),
                    local_task_id=int(local_task_id),
                )
                return _payload(task)

            if state in {"failed", "error", "timeout"} and _should_retry_provider(task):
                try:
                    task = _auto_retry_in_place(db, task)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    continue
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                    return _mark_failed(db, task, exc)

            if state in {"failed", "error", "timeout"} and _should_switch_provider(db, task):
                try:
                    task = _switch_to_next_provider_in_place(db, task)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    continue
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                    return _mark_failed(db, task, exc, active_provider=_active_provider(task))

            if state in {"success", "failed", "error", "timeout"}:
                logger.info(
                    "Bandianwa task reached terminal state",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "state": state,
                        "fail_code": task.fail_code,
                        "fail_msg": task.fail_msg,
                    },
                )
                return _payload(task)

            if time.monotonic() - start_ts > timeout_seconds:
                if _active_provider(task) in {KYY_PROVIDER_KEY, GOOGLE_GEMINI_PROVIDER_KEY}:
                    meta = _local_meta(task)
                    provider = _active_provider(task)
                    poll_cycles = dict(meta.get("provider_poll_cycles") or {})
                    cycle = int(poll_cycles.get(provider) or 0) + 1
                    poll_cycles[provider] = cycle
                    set_task_local_meta(task, provider_poll_cycles=poll_cycles, provider_poll_requeued_at=time.time())
                    if cycle < 3:
                        task.state = "in_progress"
                        db.add(task)
                        db.commit()
                        submit_and_poll_bandianwa_video_task.apply_async(
                            kwargs={
                                "workspace_id": int(workspace_id),
                                "local_task_id": int(local_task_id),
                                "interval_seconds": int(interval_seconds),
                                "timeout_seconds": int(timeout_seconds),
                            },
                            countdown=30,
                            queue="gmv.tasks.ai_video",
                        )
                        return _payload(task)
                task.state = "timeout"
                db.add(task)
                db.commit()
                if _should_retry_provider(task):
                    try:
                        task = _auto_retry_in_place(db, task)
                        start_ts = time.monotonic()
                        if str(task.state or "").lower() == "downloading":
                            queue_task_result_download(
                                workspace_id=int(workspace_id),
                                local_task_id=int(local_task_id),
                            )
                            return _payload(task)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                        return _mark_failed(db, task, exc)
                if _should_switch_provider(db, task):
                    try:
                        task = _switch_to_next_provider_in_place(db, task)
                        start_ts = time.monotonic()
                        if str(task.state or "").lower() == "downloading":
                            queue_task_result_download(
                                workspace_id=int(workspace_id),
                                local_task_id=int(local_task_id),
                            )
                            return _payload(task)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                        return _mark_failed(db, task, exc, active_provider=_active_provider(task))
                return _payload(task)

            time.sleep(max(5, int(interval_seconds)))

    finally:
        db.close()


@celery_app.task(
    name="bandianwa.video.recover_stale_polling",
    queue="gmv.tasks.ai_video",
)
def recover_stale_bandianwa_video_polling(*, stale_minutes: int = 20, limit: int = 200) -> dict[str, Any]:
    db = _db_session()
    try:
        now = datetime.now()
        cutoff = now - timedelta(minutes=max(1, int(stale_minutes)))
        orphan_cutoff = now - timedelta(hours=24)
        throttle_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        all_active_states = {
            "submitted", "pending", "waiting", "queued", "queuing",
            "queued_local", "waiting_dependency", "in_progress",
            "running", "generating", "retrying", "downloading",
        }
        # Finalize tasks that can no longer be owned by a live workflow.  This
        # covers legacy providers as well as dependency placeholders, so old
        # records do not remain "running" forever or pollute member history.
        stale_candidates = (
            db.query(KieTask)
            .filter(
                KieTask.state.in_(all_active_states),
                KieTask.updated_at <= cutoff,
            )
            .order_by(KieTask.updated_at.asc(), KieTask.id.asc())
            .limit(max(1, min(int(limit) * 2, 1000)))
            .all()
        )
        finalized_orphan_ids: list[int] = []
        quarantined_non_authoritative_ids: list[int] = []
        project_cache: dict[str, HermesContentFactoryProject | None] = {}
        for task in stale_candidates:
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(task, reason=authority_reason)
                db.add(task)
                quarantined_non_authoritative_ids.append(int(task.id))
                continue
            if _poll_heartbeat_is_recent(
                task,
                max_age_seconds=max(1, int(stale_minutes)) * 60,
            ):
                continue
            params = dict(task.input_json or {})
            project_key = str(params.get("content_factory_project_key") or "").strip()
            project = None
            if project_key:
                if project_key not in project_cache:
                    project_cache[project_key] = (
                        db.query(HermesContentFactoryProject)
                        .filter(
                            HermesContentFactoryProject.project_key == project_key,
                            HermesContentFactoryProject.workspace_id == int(task.workspace_id),
                        )
                        .one_or_none()
                    )
                project = project_cache[project_key]
            project_status = str(project.status or "").lower() if project is not None else ""
            draining_submitted_video = _content_project_drains_submitted_video(
                project,
                task_id=int(task.id),
            )
            owner_terminal = project_status in {"complete", "completed", "deleted", "cancelled"} or (
                project_status == "paused" and not draining_submitted_video
            )
            stale_failed_owner = (
                project_status == "failed"
                and project is not None
                and project.updated_at is not None
                and project.updated_at <= orphan_cutoff
            )
            owner_left_video_wait = (
                project is not None
                and project_key
                and str(project.current_stage or "") != "WAITING_VIDEO_INPUT"
                and task.updated_at is not None
                and task.updated_at <= orphan_cutoff
            )
            unowned_expired = (
                (not project_key or project is None)
                and task.updated_at is not None
                and task.updated_at <= orphan_cutoff
            )
            if not (owner_terminal or stale_failed_owner or owner_left_video_wait or unowned_expired):
                continue
            task.state = "timeout"
            task.fail_code = "stale_orphaned_task"
            task.fail_msg = (
                f"Recovered stale task: owning content project is {project_status}."
                if project is not None
                else "Recovered stale task without a live owning workflow."
            )
            set_task_local_meta(
                task,
                stale_orphan_recovered_at=datetime.now(timezone.utc).isoformat(),
                stale_orphan_project_status=project_status or None,
            )
            task.updated_at = now
            db.add(task)
            finalized_orphan_ids.append(int(task.id))
        if finalized_orphan_ids or quarantined_non_authoritative_ids:
            db.commit()

        active_states = {
            "submitted",
            "pending",
            "waiting",
            "queued",
            "queuing",
            "queued_local",
            "in_progress",
            "running",
            "generating",
            "retrying",
        }
        tasks = (
            db.query(KieTask)
            .join(KieApiKey, KieTask.key_id == KieApiKey.id)
            .filter(
                KieTask.state.in_(active_states),
                KieApiKey.provider_key.in_(tuple(SUPPORTED_VIDEO_PROVIDERS)),
                KieTask.updated_at <= cutoff,
            )
            .order_by(KieTask.updated_at.asc(), KieTask.id.asc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
        queued_ids: list[int] = []
        for task in tasks:
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(task, reason=authority_reason)
                db.add(task)
                db.commit()
                if int(task.id) not in quarantined_non_authoritative_ids:
                    quarantined_non_authoritative_ids.append(int(task.id))
                continue
            meta = _local_meta(task)
            recovered_at_raw = str(meta.get("poll_recovered_at") or "")
            try:
                recovered_at = datetime.fromisoformat(recovered_at_raw).astimezone(timezone.utc)
            except (TypeError, ValueError):
                recovered_at = None
            if recovered_at is not None and recovered_at > throttle_cutoff:
                continue
            if _poll_heartbeat_is_recent(
                task,
                max_age_seconds=max(1, int(stale_minutes)) * 60,
            ):
                continue
            set_task_local_meta(
                task,
                poll_recovered_at=datetime.now(timezone.utc).isoformat(),
                poll_recovered_reason="stale_active_state",
            )
            task.updated_at = now
            db.add(task)
            db.commit()
            submit_and_poll_bandianwa_video_task.apply_async(
                kwargs={
                    "workspace_id": int(task.workspace_id),
                    "local_task_id": int(task.id),
                    "interval_seconds": int(getattr(settings, "BANDIANWA_POLL_INTERVAL_SECONDS", 15)),
                    "timeout_seconds": int(getattr(settings, "BANDIANWA_POLL_TIMEOUT_SECONDS", 10 * 60)),
                },
                queue="gmv.tasks.ai_video",
            )
            queued_ids.append(int(task.id))
        return {
            "queued": len(queued_ids),
            "task_ids": queued_ids,
            "finalized_orphans": len(finalized_orphan_ids),
            "finalized_orphan_ids": finalized_orphan_ids,
            "quarantined_non_authoritative": len(quarantined_non_authoritative_ids),
            "quarantined_non_authoritative_ids": quarantined_non_authoritative_ids,
        }
    finally:
        db.close()
