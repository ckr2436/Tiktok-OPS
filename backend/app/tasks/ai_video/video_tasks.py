from __future__ import annotations

import asyncio
import hashlib
import math
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
    HermesContentSegmentRun,
    HermesContentVariantRun,
)
from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.bandianwa.client import BandianwaApiError
from app.services.bandianwa.tasks import (
    refresh_bandianwa_task_status,
    submit_bandianwa_task,
)
from app.services.ai_video.task_state import is_local_task_id, reset_video_task_for_retry
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
from app.services.sub2api.client import Sub2ApiApiError
from app.services.sub2api.video_tasks import (
    refresh_sub2api_video_task,
    submit_sub2api_video_task,
)
from app.services.doubao_provider.client import DoubaoProviderError
from app.services.doubao_provider.tasks import (
    refresh_doubao_task,
    release_doubao_task_account,
    submit_doubao_task,
)
from app.services.ai_routing.video_provider_recovery import (
    SELF_HOSTED_PROVIDERS,
    VideoProviderIncident,
    VideoRecoveryAction,
    classify_video_provider_fault,
    decide_video_provider_recovery,
    inspect_local_provider_health,
)
from app.services.ai_routing.video_attempts import (
    begin_video_route_attempt,
    finish_video_route_attempt,
)
from app.services.ai_video.accounts import (
    BANDIANWA_PROVIDER_KEY,
    DOUBAO_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    SUB2API_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    get_effective_key,
    has_active_key,
    key_supports_model,
    normalize_provider_key,
    normalize_video_model_id,
    resolve_video_model_key,
)
from app.services.ai_video.local_storage import get_task_local_meta, set_task_local_meta
from app.services.ai_video.queues import (
    AI_VIDEO_API_TASK_QUEUE,
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    production_video_queue,
    polling_video_queue,
)
from app.services.ai_video.retry_policy import (
    MAX_AUTO_RETRIES,
    QUOTA_FAILURE_KEYWORDS,
    delete_task_result_files,
    is_quota_failure,
    restore_archived_task_result_files,
    retry_count,
    should_auto_retry,
)
from app.tasks.ai_video.result_download_tasks import queue_task_result_download

logger = get_task_logger(__name__)

SUPPORTED_VIDEO_PROVIDERS = {
    BANDIANWA_PROVIDER_KEY,
    KYY_PROVIDER_KEY,
    GOOGLE_GEMINI_PROVIDER_KEY,
    VOLCENGINE_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    SUB2API_PROVIDER_KEY,
    DOUBAO_PROVIDER_KEY,
}
PROVIDER_API_ERRORS = (
    BandianwaApiError,
    GlobalAiOpcApiError,
    GoogleGeminiApiError,
    VolcengineApiError,
    ToApisApiError,
    Sub2ApiApiError,
    DoubaoProviderError,
)
PROVIDER_TASK_ERRORS = PROVIDER_API_ERRORS + (ValueError,)
CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE = "cf_variant_superseded"
CONTENT_FACTORY_NOT_AUTHORITATIVE_CODE = "cf_task_not_authoritative"
CONTENT_FACTORY_PROMPT_CONTRACT_CODE = "content_provider_prompt_contract_invalid"


class VideoProviderRouteUnavailable(ValueError):
    """No enabled route can satisfy the task's immutable media contract.

    This is a local routing decision, not a provider outage.  Retrying the
    same Celery delivery cannot change reference-count, aspect-ratio,
    duration, or generation-mode compatibility and previously left tasks in
    a misleading ``queued_local`` loop.
    """

    code = "video_provider_route_unavailable"


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
        raise ValueError("AI video task not found")
    return task


def _payload(task: KieTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.task_id,
        "state": task.state,
        "fail_code": task.fail_code,
        "fail_msg": task.fail_msg,
    }


def _content_factory_provider_prompt_contract_error(
    task: KieTask,
) -> str | None:
    """Validate the exact paid-provider packet at the final I/O boundary.

    The Content Factory compiler stamps a proof after its last compaction.
    Late Celery deliveries created by an older worker must fail locally and be
    rebuilt by Hermes instead of spending provider quota with incomplete copy,
    stale image aliases, or a missing product authority reference.
    """

    input_params = dict(task.input_json or {})
    if not (
        input_params.get("content_factory_project_id")
        or input_params.get("content_factory_project_key")
    ):
        return None
    contract = dict(
        input_params.get("content_factory_provider_prompt_contract") or {}
    )
    if not bool(contract.get("validated")):
        return "missing validated provider prompt contract"
    prompt = str(input_params.get("prompt") or "")
    actual_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if str(contract.get("actual_sha256") or "") != actual_hash:
        return "provider prompt changed after semantic validation"
    manifest = [
        row
        for row in list(
            input_params.get("content_factory_reference_manifest") or []
        )
        if isinstance(row, dict)
    ]
    missing_aliases = [
        str(row.get("alias") or f"@image{position}")
        for position, row in enumerate(manifest, 1)
        if str(row.get("alias") or f"@image{position}") not in prompt
    ]
    if missing_aliases:
        return "provider prompt is missing reference aliases: " + ", ".join(
            missing_aliases
        )
    product_required = bool(
        input_params.get("content_factory_product_required")
        or input_params.get("content_factory_product_anchor_required")
    )
    text_only = str(
        input_params.get("content_factory_product_render_mode") or ""
    ).strip().lower() == "text_only"
    if (
        product_required
        and not text_only
        and not any(bool(row.get("is_product_anchor")) for row in manifest)
    ):
        return "product-visible provider packet has no product authority"
    return None


def _is_unconfirmed_doubao_submit_marker(task: KieTask) -> bool:
    """Detect a submit lease that never acquired a remote conversation id.

    ``doubao-local-*`` is committed before the browser helper runs. Normally
    the adapter either replaces it with ``doubao:<conversation>`` or resets
    the task to ``queued_local`` on a bounded error. A killed helper process
    can leave the intermediate marker behind. A later Celery redelivery must
    not enter the poll path because there is no pollable remote identity.
    """

    return (
        str(task.state or "").strip().lower() == "submitting"
        and str(task.task_id or "").startswith("doubao-local-")
        and _active_provider(task) == DOUBAO_PROVIDER_KEY
    )


def _recover_unconfirmed_doubao_submit(
    db: Session,
    task: KieTask,
) -> KieTask:
    """Release the abandoned account lease and reopen the same logical task."""

    release_doubao_task_account(
        db,
        task=task,
        error_code="doubao_submit_unconfirmed",
    )
    task = reset_video_task_for_retry(
        db,
        task=task,
        retry_kind="provider_submit_unconfirmed_recovery",
    )
    set_task_local_meta(
        task,
        doubao_submit_unconfirmed_recovered_at=(
            datetime.now(timezone.utc).isoformat()
        ),
        doubao_submit_unconfirmed_recovery_reason=(
            "local submit marker had no remote conversation id"
        ),
    )
    db.add(task)
    db.commit()
    return task


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
    meta = _local_meta(task)
    heartbeat = _parse_utc_datetime(meta.get("poll_heartbeat_at"))
    recovered_at = _parse_utc_datetime(
        meta.get("doubao_submit_unconfirmed_recovered_at")
    )
    # The unconfirmed-submit repair is an ownership fence: once it has reset
    # the same logical task to ``queued_local``, an older heartbeat can only
    # belong to the helper/poller that was abandoned. Treating that heartbeat
    # as live makes every replacement delivery return early for the full
    # provider lease and leaves Content Factory spinning in video wait.
    if (
        heartbeat is not None
        and recovered_at is not None
        and recovered_at >= heartbeat
    ):
        return False
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


def _provider_submit_lease_seconds(interval_seconds: int) -> int:
    """Cover the longest synchronous provider submit before takeover.

    Sub2API Flow may legitimately occupy the HTTP client for its configured
    180-second timeout.  A 60-second owner lease allowed the Content Factory
    waiter to publish two more deliveries while the first request was still
    in flight.  The provider idempotency key limits duplicate paid jobs, but
    the duplicate workers still exhaust concurrency and delay self-heal.
    """
    return max(
        # The self-hosted recovery supervisor may wait as long as five
        # minutes before redelivering the same Celery request id.  Keep the
        # owner fence beyond that bounded cooldown so an older delayed
        # delivery cannot take over a freshly reset row.  Periodic stale
        # recovery still uses its explicit 20-minute heartbeat boundary.
        10 * 60,
        int(interval_seconds) * 4,
        int(math.ceil(float(settings.SUB2API_HTTP_TIMEOUT_SECONDS))) + 30,
    )


def _handoff_doubao_poll(
    db: Session,
    task: KieTask,
    *,
    workspace_id: int,
    local_task_id: int,
    interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Continue a pending Doubao job in a fresh, bounded Celery delivery.

    Doubao can legitimately remain queued longer than the global Celery soft
    time limit.  Keeping one worker process in a sleep/poll loop eventually
    kills that process and leaves the durable provider job without a poller.
    Release the poll-owner lease before publishing the next short delivery so
    the replacement worker can claim it without submitting another video.
    """
    meta = _local_meta(task)
    set_task_local_meta(
        task,
        poll_owner_task_id=None,
        poll_handoff_at=datetime.now(timezone.utc).isoformat(),
        poll_handoff_count=int(meta.get("poll_handoff_count") or 0) + 1,
    )
    task.updated_at = datetime.now()
    db.add(task)
    # Persist the released owner first. If broker publication itself fails,
    # the periodic stale-poll recovery sees this durable active row and repairs
    # it instead of allowing two live owners.
    db.commit()
    submit_and_poll_ai_video_task.apply_async(
        kwargs={
            "workspace_id": int(workspace_id),
            "local_task_id": int(local_task_id),
            "interval_seconds": int(interval_seconds),
            "timeout_seconds": int(timeout_seconds),
        },
        countdown=max(5, int(interval_seconds)),
        queue=polling_video_queue(task),
    )
    return _payload(task)


def _defer_provider_pool_backpressure(
    db: Session,
    task: KieTask,
    *,
    workspace_id: int,
    local_task_id: int,
    interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Republish an unsubmitted task after local account-lane contention.

    No remote identity exists at this point, so retaining the provider route
    and local task id is idempotent.  Persist the released owner before broker
    publication; the Content Factory queued-local compensator is the fallback
    if publication itself is interrupted.
    """

    meta = _local_meta(task)
    now = datetime.now(timezone.utc)
    delay_seconds = max(15, min(60, int(interval_seconds) * 2))
    task.state = "queued_local"
    task.fail_code = None
    task.fail_msg = None
    set_task_local_meta(
        task,
        submit_enqueued_at=now.isoformat(),
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
        doubao_pool_backpressure_count=(
            int(meta.get("doubao_pool_backpressure_count") or 0) + 1
        ),
        doubao_pool_backpressure_retry_at=(
            now + timedelta(seconds=delay_seconds)
        ).isoformat(),
    )
    task.updated_at = datetime.now()
    db.add(task)
    db.commit()
    submit_and_poll_ai_video_task.apply_async(
        kwargs={
            "workspace_id": int(workspace_id),
            "local_task_id": int(local_task_id),
            "interval_seconds": int(interval_seconds),
            "timeout_seconds": int(timeout_seconds),
        },
        countdown=delay_seconds,
        queue=production_video_queue(task),
    )
    logger.info(
        "AI video task deferred for healthy local provider lane",
        extra={
            "workspace_id": int(workspace_id),
            "local_task_id": int(local_task_id),
            "provider": _active_provider(task),
            "delay_seconds": delay_seconds,
        },
    )
    return _payload(task)


def _provider_error_is_pool_backpressure(exc: Exception) -> bool:
    # Both outcomes are pre-submit local capacity states. ``pool_busy`` means
    # an eligible account exists but its browser/network lane is occupied;
    # ``pool_unavailable`` can also be observed for the brief window in which
    # maintenance owns the account rows (``claim_account`` uses SKIP LOCKED)
    # or every account is between probes.  Neither outcome proves a provider
    # failure and neither has a remote task to abandon, so keep the same
    # logical task queued and let the next delivery re-evaluate the live pool.
    return str(getattr(exc, "code", "") or "").strip().lower() in {
        "doubao_pool_busy",
        "doubao_pool_unavailable",
    }


def _doubao_remote_wait_expired(
    task: KieTask,
    *,
    timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Apply one durable deadline across all short Doubao poll deliveries."""
    accepted_at = _parse_utc_datetime(
        _local_meta(task).get("doubao_remote_accepted_at")
    )
    if accepted_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    effective_timeout = max(
        1,
        int(timeout_seconds),
        int(getattr(settings, "DOUBAO_POLL_TIMEOUT_SECONDS", 30 * 60)),
    )
    return current >= accepted_at + timedelta(seconds=effective_timeout)


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
    params = dict(task.input_json or {})
    routing_mode = str(params.get("routing_mode") or "").strip().lower()
    if routing_mode == "auto":
        return "auto"
    value = str(
        params.get("requested_service_provider")
        if routing_mode == "pinned"
        else params.get("service_provider")
        or "auto"
    ).strip().lower()
    if value == "auto":
        return "auto"
    provider = normalize_provider_key(value)
    return provider if provider in SUPPORTED_VIDEO_PROVIDERS else "auto"


def _active_provider(task: KieTask) -> str:
    # The normalized task input is the durable owner of the current route.
    # Older failed Sub2API submissions could roll back the freshly written
    # local metadata and leave a stale ``active_provider=bandianwa`` marker.
    # Prefer the explicit current route so manual retries heal those rows;
    # auto-routed legacy tasks still use the local provider marker below.
    requested = _service_provider(task)
    if requested != "auto":
        return normalize_provider_key(requested)
    value = str(_local_meta(task).get("active_provider") or "").strip().lower()
    if value:
        return normalize_provider_key(value)
    return BANDIANWA_PROVIDER_KEY


def _task_key(db: Session, task: KieTask) -> KieApiKey:
    key = db.get(KieApiKey, int(task.key_id))
    model = normalize_video_model_id(task.model)
    requested_provider = _service_provider(task)
    try:
        if (
            key is not None
            and (
                requested_provider == "auto"
                or normalize_provider_key(key.provider_key) == requested_provider
            )
        ):
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
    if requested_provider != "auto":
        try:
            requested_key = get_effective_key(
                db,
                require_active=True,
                provider_key=requested_provider,
                model_id=model,
            )
            requested_key = resolve_video_model_key(
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
                key_id=int(requested_key.id),
            )
        except ValueError as exc:
            raise VideoProviderRouteUnavailable(
                f"Requested video provider {requested_provider} is unavailable "
                "or incompatible with this task"
            ) from exc
        task.key_id = int(requested_key.id)
        return requested_key
    if key is None or not key.is_active or not key_supports_model(key, model):
        replacement = _next_provider_key(db, task)
        if replacement is None:
            raise VideoProviderRouteUnavailable(
                "Selected video provider API key is missing or inactive"
            )
        task.key_id = int(replacement.id)
        key = replacement
        return key
    replacement = _next_provider_key(db, task)
    if replacement is None:
        raise VideoProviderRouteUnavailable(
            "Selected video provider does not support the requested model inputs"
        )
    task.key_id = int(replacement.id)
    return replacement


def _submit_current_provider(db: Session, task: KieTask) -> KieTask:
    params = dict(task.input_json or {})
    if str(
        params.get("content_factory_video_generation_mode") or ""
    ).strip().lower() == "text_to_video":
        # Last-mile, provider-agnostic fence. Even if a stale retry row or a
        # legacy KieFile survived upstream cleanup, no enabled provider may
        # receive reference media for a text-to-video task.
        base_prompt = str(
            # ``prompt`` is the final, semantically validated provider packet.
            # ``content_factory_base_prompt`` intentionally predates segment
            # scope and continuity controls.  Preferring the older field here
            # changed the packet after validation; a transient provider retry
            # was then rejected locally as contract drift.
            params.get("prompt")
            or params.get("content_factory_base_prompt")
            or task.prompt
            or ""
        ).strip()
        params.update({
            "prompt": base_prompt,
            "reference_images": [],
            "images": [],
            "reference_file_paths": [],
            "reference_videos": [],
            "reference_video_file_paths": [],
            "content_factory_reference_manifest": [],
            "content_factory_reference_video_count": 0,
            "content_factory_first_frame": False,
            "content_factory_product_anchor_required": False,
            "content_factory_product_render_mode": "text_only",
        })
        task.input_json = params
        db.query(KieFile).filter(
            KieFile.task_id == int(task.id),
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.kind.in_((
                "reference_upload", "reference_video_upload",
            )),
        ).delete(synchronize_session=False)
        db.add(task)
        db.flush()
    key = _task_key(db, task)
    provider = normalize_provider_key(key.provider_key)
    set_task_local_meta(task, active_provider=provider, active_provider_key_id=int(key.id))
    params = dict(task.input_json or {})
    params["service_provider"] = provider
    task.input_json = params
    attempt_id, attempt_started = begin_video_route_attempt(db, task)
    error: Exception | None = None
    try:
        if provider == KYY_PROVIDER_KEY:
            return asyncio.run(submit_GlobalAiOpc_task(db, task=task, key=key, file_key_id=int(key.id)))
        if provider == GOOGLE_GEMINI_PROVIDER_KEY:
            return asyncio.run(submit_google_gemini_task(db, task=task))
        if provider == VOLCENGINE_PROVIDER_KEY:
            return asyncio.run(submit_volcengine_task(db, task=task))
        if provider == TOAPIS_PROVIDER_KEY:
            return asyncio.run(submit_toapis_task(db, task=task))
        if provider == SUB2API_PROVIDER_KEY:
            return asyncio.run(submit_sub2api_video_task(db, task=task))
        if provider == DOUBAO_PROVIDER_KEY:
            return asyncio.run(submit_doubao_task(db, task=task))
        return asyncio.run(submit_bandianwa_task(db, task=task))
    except Exception as exc:
        error = exc
        raise
    finally:
        finish_video_route_attempt(
            db,
            attempt_id,
            attempt_started,
            error=error,
        )


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
        task = reset_video_task_for_retry(db, task=task, retry_kind="provider_scope_change")
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
    if provider == SUB2API_PROVIDER_KEY:
        return asyncio.run(refresh_sub2api_video_task(db, task=task))
    if provider == DOUBAO_PROVIDER_KEY:
        return asyncio.run(refresh_doubao_task(db, task=task))
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


def _is_explicit_prompt_violation(task: KieTask) -> bool:
    """Return true only for a provider-declared prompt/content violation."""
    if str(task.fail_code or "").strip().lower() == "flow_request_rejected":
        return True
    text = f"{task.fail_code or ''} {task.fail_msg or ''}".lower()
    return any(
        marker in text
        for marker in (
            "prompt violation",
            "prompt_violation",
            "content policy violation",
            "safety policy violation",
            "提示词违规",
            "提示词违反",
            "内容违规",
        )
    )


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
    if not authorized and reason == "variant_group_missing" and project is not None:
        # ``state_json.ai_video_groups`` is a mutable scheduling cache.  The
        # append-only execution ledger is committed in the same transaction
        # as every provider task and remains the durable authority if a
        # concurrent waiter/control-stage JSON merge temporarily drops that
        # cache entry.  Only the newest non-superseded attempt for the exact
        # variant, manifest, source stage and segment may use this fallback;
        # a genuinely replaced task therefore stays fail-closed.
        params = dict(task.input_json or {})
        try:
            variant_index = int(
                params.get("content_factory_variant_index")
                or params.get("content_factory_video_index")
                or 0
            )
            segment_index = int(
                params.get("content_factory_segment_index") or 0
            )
            source_stage_id = int(
                params.get("content_factory_source_stage_id") or 0
            )
        except (TypeError, ValueError):
            variant_index = segment_index = source_stage_id = 0
        manifest_sha256 = str(
            params.get("content_factory_media_manifest_sha256") or ""
        ).strip()
        segment_run = (
            db.query(HermesContentSegmentRun)
            .filter(
                HermesContentSegmentRun.project_id == int(project.id),
                HermesContentSegmentRun.workspace_id == int(project.workspace_id),
                HermesContentSegmentRun.provider_task_row_id == int(task.id),
                HermesContentSegmentRun.segment_index == segment_index,
            )
            .order_by(
                HermesContentSegmentRun.attempt.desc(),
                HermesContentSegmentRun.id.desc(),
            )
            .first()
            if variant_index > 0 and segment_index > 0
            else None
        )
        variant_run = (
            db.get(HermesContentVariantRun, int(segment_run.variant_run_id))
            if segment_run is not None
            else None
        )
        newest_variant_run = (
            db.query(HermesContentVariantRun)
            .filter(
                HermesContentVariantRun.project_id == int(project.id),
                HermesContentVariantRun.workspace_id == int(project.workspace_id),
                HermesContentVariantRun.variant_index == variant_index,
            )
            .order_by(
                HermesContentVariantRun.attempt.desc(),
                HermesContentVariantRun.id.desc(),
            )
            .first()
            if variant_index > 0
            else None
        )
        variant_meta = dict(getattr(variant_run, "meta_json", None) or {})
        if (
            segment_run is not None
            and variant_run is not None
            and newest_variant_run is not None
            and int(newest_variant_run.id) == int(variant_run.id)
            and int(variant_run.variant_index) == variant_index
            and str(variant_run.state or "").lower() != "superseded"
            and str(variant_run.media_manifest_sha256 or "") == manifest_sha256
            and int(variant_meta.get("source_stage_id") or 0) == source_stage_id
            and (
                project.user_id is None
                or int(variant_run.user_id or 0) == int(project.user_id)
            )
        ):
            authorized, reason = True, "current_variant_segment_ledger"
    meta = _local_meta(task)
    remote_visible = not is_local_task_id(task.task_id)
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
    db: Session | None = None,
) -> KieTask:
    """Make stale broker deliveries terminal without touching a provider."""
    was_explicitly_superseded = (
        str(task.fail_code or "").strip().lower() == CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE
        or reason in {"superseded_marker", "segment_owned_by_replacement"}
    )
    # A delivery can lose project authority after Doubao reserved an account
    # but before remote provider I/O starts (for example when a concurrent
    # quality-repair child wins the segment pointer). Release that exact lease
    # before clearing the task's local owner metadata; otherwise one ignored
    # broker delivery can hold a pool account for the full lease window and
    # make every legitimate retry report `doubao_pool_unavailable`.
    if db is not None:
        release_doubao_task_account(
            db,
            task=task,
            error_code=CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE,
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
    restore_previous: bool = True,
) -> dict[str, Any]:
    if active_provider:
        set_task_local_meta(task, active_provider=active_provider)
    set_task_local_meta(
        task,
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    task.state = "failed"
    task.fail_code = task.fail_code or str(
        getattr(exc, "code", "") or "bandianwa_worker_error"
    )[:32]
    task.fail_msg = str(exc)[:512]
    db.add(task)
    db.flush()
    if restore_previous:
        restore_archived_task_result_files(
            db,
            task,
            failure_code=task.fail_code,
            failure_message=task.fail_msg,
        )
    db.commit()
    return _payload(task)


def _provider_error_is_quota_failure(exc: Exception) -> bool:
    text = f"{getattr(exc, 'code', '')} {str(exc or '')}".lower()
    return any(keyword in text for keyword in QUOTA_FAILURE_KEYWORDS)


def _provider_error_is_generation_conflict(exc: Exception) -> bool:
    return str(getattr(exc, "code", "") or "").strip().lower() == (
        "flow_idempotency_conflict"
    )


def _provider_error_is_route_unavailable(exc: Exception) -> bool:
    return isinstance(exc, VideoProviderRouteUnavailable)


def _provider_error_is_terminal_request_rejection(exc: Exception) -> bool:
    return str(getattr(exc, "code", "") or "").strip().lower() in {
        "flow_request_rejected",
        "flow_idempotency_conflict",
        "doubao_membership_required",
        "doubao_face_ref_unsupported",
        "doubao_content_rejected",
        "doubao_prompt_contract_invalid",
        "doubao_prompt_too_long",
    }


def _task_has_terminal_request_rejection(task: KieTask) -> bool:
    return str(task.fail_code or "").strip().lower() in {
        "flow_request_rejected",
        "flow_idempotency_conflict",
        "doubao_membership_required",
        "doubao_face_ref_unsupported",
        "doubao_content_rejected",
        "doubao_prompt_contract_invalid",
        "doubao_prompt_too_long",
    }


def _seal_terminal_request_rejection(db: Session, task: KieTask) -> None:
    """Fence late poll deliveries after a provider returned a final answer."""
    release_doubao_task_account(
        db,
        task=task,
        error_code=str(task.fail_code or "doubao_content_rejected"),
    )
    task.state = "failed"
    set_task_local_meta(
        task,
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    task.updated_at = datetime.now()
    db.add(task)
    db.flush()
    restore_archived_task_result_files(
        db,
        task,
        failure_code=task.fail_code,
        failure_message=task.fail_msg,
    )


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
    task = reset_video_task_for_retry(db, task=task, retry_kind="auto")
    set_task_local_meta(
        task,
        provider_retry_counts=provider_attempts,
        active_provider=provider,
        # A poll worker may discover the terminal provider response.  It must
        # never perform the next paid submission itself, otherwise long-lived
        # polling once again competes with fresh production work.  Persist the
        # new local generation and hand it to the provider-owned submit lane.
        submit_enqueued_at=datetime.now(timezone.utc).isoformat(),
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    db.add(task)
    db.commit()
    submit_and_poll_ai_video_task.apply_async(
        kwargs={
            "workspace_id": int(task.workspace_id),
            "local_task_id": int(task.id),
            "interval_seconds": int(settings.BANDIANWA_POLL_INTERVAL_SECONDS),
            "timeout_seconds": int(settings.BANDIANWA_POLL_TIMEOUT_SECONDS),
        },
        queue=production_video_queue(task),
    )
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
    # Explicit quota failures are durable for this logical paid task.  A
    # later transient failure on another provider may restart the bounded
    # provider cycle after cooldown, but it must not upload references and
    # call an account that already declared insufficient balance.
    attempted.update({
        int(value)
        for value in list(meta.get("provider_quota_failed_key_ids") or [])
        if str(value).isdigit()
    })
    attempted.add(int(task.key_id))
    attempted_providers = {
        normalize_provider_key(value)
        for value in list(meta.get("attempted_provider_keys") or [])
        if normalize_provider_key(value)
    }
    # A self-hosted account-pool outage belongs to the adapter, not to one
    # platform credential row.  Two Sub2API keys can address the same local
    # Flow pool; treating the second key as a provider switch only repeats the
    # same 503 and strands Content Factory in another cooldown.  Walk past all
    # keys owned by a provider already fenced for this round.  The fence is
    # cleared by _prepare_provider_cycle_after_cooldown, so a later healthy
    # round can use the provider again.
    while True:
        try:
            candidate = resolve_video_model_key(
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
        if normalize_provider_key(candidate.provider_key) not in attempted_providers:
            return candidate
        attempted.add(int(candidate.id))


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
    task = reset_video_task_for_retry(db, task=task, retry_kind="auto")
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
        # The replacement route is always handed to its semantic production
        # queue.  This prevents an API worker from opening a browser after a
        # failover (or a browser worker from blocking on an API provider).
        submit_enqueued_at=datetime.now(timezone.utc).isoformat(),
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    # Persist provider ownership before network I/O. A failed failover then
    # resumes from the replacement provider and cannot resubmit to the
    # exhausted provider under the same logical task.
    db.add(task)
    db.commit()
    submit_and_poll_ai_video_task.apply_async(
        kwargs={
            "workspace_id": int(task.workspace_id),
            "local_task_id": int(task.id),
            "interval_seconds": int(settings.BANDIANWA_POLL_INTERVAL_SECONDS),
            "timeout_seconds": int(settings.BANDIANWA_POLL_TIMEOUT_SECONDS),
        },
        queue=production_video_queue(task),
    )
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
    task = reset_video_task_for_retry(db, task=task, retry_kind="auto")
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
    # The provider-local budget prevents hammering one route within a round,
    # while the global budget bounds the lifetime of the logical paid task.
    # Keeping only the provider counter allowed a manual retry (which starts a
    # fresh provider round) to submit beyond MAX_AUTO_RETRIES indefinitely.
    return should_auto_retry(task) and _provider_retry_count(task) < 3


def _should_switch_provider(db: Session, task: KieTask) -> bool:
    if _is_content_factory_task(task):
        # One provider round walks every enabled compatible route in the API
        # management priority order. A declared prompt violation is global to
        # the request and must not be evaded by sending it to another vendor.
        return (
            not _is_explicit_prompt_violation(task)
            and _next_provider_key(db, task) is not None
        )
    if is_quota_failure(task):
        return _next_provider_key(db, task) is not None
    return _provider_retry_count(task) >= 3 and _next_provider_key(db, task) is not None


def _should_advance_exhausted_provider(db: Session, task: KieTask) -> bool:
    """Advance after the Celery transport budget for this provider is spent.

    ``provider_retry_counts`` tracks provider-declared terminal generations.
    Celery transport retries are tracked separately on the delivery and are
    intentionally not persisted as new paid provider attempts. Requiring the
    former counter here stranded tasks after the latter retry budget expired.
    """
    return (
        not _is_explicit_prompt_violation(task)
        and _next_provider_key(db, task) is not None
    )


def _retry_countdown_for_provider_error(exc: Exception, retry_number: int) -> int:
    """Honor provider backoff and never hammer a locally cooled-down pool."""
    explicit = getattr(exc, "retry_after_seconds", None)
    try:
        if explicit is not None:
            return max(15, min(15 * 60, int(explicit)))
    except (TypeError, ValueError):
        pass
    status_code = getattr(exc, "status_code", None)
    message = str(exc or "").lower()
    transient = (
        status_code in {429, 500, 502, 503, 504, 529}
        or "temporarily unavailable" in message
        or "no available accounts" in message
        or "transport error" in message
    )
    if transient:
        return min(5 * 60, 60 * max(1, int(retry_number) + 1))
    return 15


def _provider_retry_wait_seconds(
    exc: Exception,
    *,
    retry_number: int,
    recovery_wait_seconds: int | None = None,
) -> int:
    """Keep Celery redelivery outside the provider route circuit.

    The recovery adviser may choose a shorter wait than the routing layer's
    deterministic circuit (for example 240 seconds while RATE_LIMIT opens a
    300-second circuit).  Retrying inside that window cannot reach an account
    and only creates another delayed delivery.  Honor both boundaries and add
    a small scheduling margin for the exact circuit-expiry edge.
    """
    wait_seconds = _retry_countdown_for_provider_error(exc, retry_number)
    if recovery_wait_seconds is not None:
        wait_seconds = max(wait_seconds, int(recovery_wait_seconds))
    if classify_video_provider_fault(exc) == "RATE_LIMIT":
        # Ai routing opens a five-minute circuit when the upstream does not
        # publish an explicit Retry-After value.
        wait_seconds = max(wait_seconds, 5 * 60) + 5
    return max(15, min(15 * 60, int(wait_seconds)))


def _should_advance_route_immediately(
    task: KieTask,
    exc: Exception,
) -> bool:
    """Walk the next configured route after an explicit transient response.

    Every auto-routed AI-video task owns a provider *round*: compatible enabled
    routes are tried once in platform priority order before a later recovery
    cycle may reopen transient routes.  Celery delivery retries are still
    required for ambiguous transport failures, because the provider may have
    accepted a billable request.  An explicit HTTP 429/5xx response is
    unambiguous, however, and waiting on the same route strands both direct AI
    video and Content Factory tasks while healthy fallbacks remain unused.
    """
    error_code = str(getattr(exc, "code", "") or "").strip().lower()
    if error_code in {
        "doubao_silent_timeout",
        # The conversation returned ordinary assistant text but never
        # created a Seedance model task.  The adapter raises this only after
        # a bounded grace period and releases the account lease, so this
        # remote identity is conclusively non-pollable.  Advance the provider
        # round instead of redelivering the same dead conversation.
        "doubao_text_only_response",
        # ``submit_doubao_task`` raises these only after the account pool has
        # failed before returning a remote conversation id.  The adapter has
        # already reset the logical task from its temporary ``submitting``
        # marker, so advancing to the next configured route is idempotent and
        # avoids replaying the entire unusable account pool on Celery retries.
        "doubao_account_context_invalid",
        "doubao_auth_required",
        "doubao_browser_unstable",
        "doubao_captcha_required",
        "doubao_composer_unavailable",
        "doubao_region_restricted",
        "doubao_risk_rate_limited",
    }:
        return True
    try:
        status_code = int(getattr(exc, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return False
    return status_code in {429, 500, 502, 503, 504, 529}


def _prepare_provider_cycle_after_cooldown(task: KieTask) -> bool:
    """Open a fresh transient-provider round after a bounded cooldown.

    ``attempted_provider_key_ids`` is intentionally durable within one round,
    so a failover cannot bounce immediately back to a provider that just
    failed.  It must not, however, survive a supervisor-approved WAIT as a
    permanent deny-list: doing so makes every later retry fail capability
    resolution without contacting any provider.  Explicit quota failures are
    stored separately and remain excluded by ``_next_provider_key``.
    """
    meta = _local_meta(task)
    attempted = [
        int(value)
        for value in list(meta.get("attempted_provider_key_ids") or [])
        if str(value).isdigit()
    ]
    attempted_providers = [
        normalize_provider_key(value)
        for value in list(meta.get("attempted_provider_keys") or [])
        if normalize_provider_key(value)
    ]
    if not attempted and not attempted_providers:
        return False
    set_task_local_meta(
        task,
        attempted_provider_key_ids=[],
        attempted_provider_keys=[],
        provider_cycle_count=int(meta.get("provider_cycle_count") or 0) + 1,
        provider_cycle_reopened_at=datetime.now(timezone.utc).isoformat(),
    )
    return True


def _remember_attempted_provider_key(task: KieTask, key_id: int | None = None) -> bool:
    """Durably fence one failed route from the remainder of this provider round.

    Provider adapters are allowed to replace their response payload while a
    submission is being attempted.  The routing deny-list must therefore be
    restored at the orchestration boundary before another route is selected;
    otherwise a later failover can circle back to the route that opened the
    round and duplicate an already ambiguous paid submission.
    """
    failed_key_id = int(key_id or task.key_id)
    meta = _local_meta(task)
    attempted = [
        int(value)
        for value in list(meta.get("attempted_provider_key_ids") or [])
        if str(value).strip().isdigit()
    ]
    if failed_key_id in attempted:
        return False
    attempted.append(failed_key_id)
    set_task_local_meta(task, attempted_provider_key_ids=attempted)
    return True


def _provider_failure_is_pool_wide(task: KieTask) -> bool:
    """Return whether a self-hosted failure applies to its whole account pool."""
    provider = _active_provider(task)
    if provider not in SELF_HOSTED_PROVIDERS:
        return False
    value = f"{str(task.fail_code or '')} {str(task.fail_msg or '')}".lower()
    return any(
        marker in value
        for marker in (
            "account pool is temporarily unavailable",
            "no available accounts",
            "active_accounts=0",
            "grant_expired",
            "pool_unavailable",
        )
    )


def _remember_attempted_provider(task: KieTask, provider: str) -> bool:
    normalized = normalize_provider_key(provider)
    if not normalized:
        return False
    meta = _local_meta(task)
    attempted = [
        normalize_provider_key(value)
        for value in list(meta.get("attempted_provider_keys") or [])
        if normalize_provider_key(value)
    ]
    if normalized in attempted:
        return False
    attempted.append(normalized)
    set_task_local_meta(task, attempted_provider_keys=attempted)
    return True


def _self_hosted_recovery_decision(
    db: Session,
    task: KieTask,
    exc: Exception,
    *,
    retry_number: int,
):
    provider = _active_provider(task)
    if provider not in SELF_HOSTED_PROVIDERS or retry_number < 2:
        return None
    fallback_available = _next_provider_key(db, task) is not None

    async def _decide():
        health = await inspect_local_provider_health(provider)
        incident = VideoProviderIncident(
            incident_id=f"video-{int(task.id)}-{provider}-{int(retry_number)}",
            provider=provider,
            fault_class=classify_video_provider_fault(exc),
            status_code=getattr(exc, "status_code", None),
            retry_number=int(retry_number),
            fallback_available=bool(fallback_available),
            local_health=health,
        )
        return await decide_video_provider_recovery(incident)

    decision = asyncio.run(_decide())
    set_task_local_meta(
        task,
        video_provider_recovery={
            "provider": provider,
            "action": decision.action.value,
            "wait_seconds": int(decision.wait_seconds),
            "reason_code": decision.reason_code,
            "decision_source": decision.decision_source,
            "retry_number": int(retry_number),
            "decided_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(task)
    db.commit()
    return decision


def _fail_provider_and_advance(
    db: Session,
    task: KieTask,
    exc: Exception,
) -> KieTask:
    """Persist one provider failure, then walk the remaining priority routes.

    Submission and polling transport failures used to stop the logical task
    after the Celery retry budget was consumed, even when another compatible
    provider was enabled.  A content-factory provider round owns that failure
    locally and advances in-place.  Every failed replacement is persisted
    before the next route is selected, preserving idempotency and the audit
    trail while preventing a return to an already attempted key.
    """
    current_exc: Exception = exc
    current = task
    while True:
        failed_key_id = int(current.key_id)
        failed_provider = _active_provider(current)
        # A replacement provider can fail during the immediate in-place
        # submit performed by ``_switch_to_next_provider_in_place``.  Those
        # exceptions arrive here rather than through the outer worker quota
        # handler.  Persist the same durable quota exclusion here as well;
        # otherwise a balance failure is flattened into
        # ``bandianwa_worker_error`` and the next recovery round may select
        # the exhausted key again (or treat the whole logical task as a
        # terminal generic failure).
        if _provider_error_is_quota_failure(current_exc):
            _mark_provider_quota_failure(db, current, current_exc)
        else:
            _mark_failed(
                db,
                current,
                current_exc,
                active_provider=_active_provider(current),
                restore_previous=False,
            )
        current = _load_task(
            db,
            workspace_id=int(current.workspace_id),
            local_task_id=int(current.id),
            for_update=True,
        )
        if _provider_failure_is_pool_wide(current):
            _remember_attempted_provider(current, failed_provider)
        # Do this again after the provider failure has been committed and the
        # row reloaded.  It makes the route-round fence independent from any
        # provider-specific result_json replacement and from transaction
        # rollback during an immediate replacement submit.
        if _remember_attempted_provider_key(current, failed_key_id):
            db.add(current)
            db.commit()
            current = _load_task(
                db,
                workspace_id=int(current.workspace_id),
                local_task_id=int(current.id),
                for_update=True,
            )
        if not _should_advance_exhausted_provider(db, current):
            restore_archived_task_result_files(
                db,
                current,
                failure_code=current.fail_code,
                failure_message=current.fail_msg,
            )
            db.commit()
            return current
        try:
            return _switch_to_next_provider_in_place(db, current)
        except PROVIDER_TASK_ERRORS as failover_exc:
            db.rollback()
            current = _load_task(
                db,
                workspace_id=int(current.workspace_id),
                local_task_id=int(current.id),
                for_update=True,
            )
            current_exc = failover_exc


def _content_factory_dependency_pending(task: KieTask) -> bool:
    """Never submit a chained Omni segment without its continuity frame."""
    if not _is_content_factory_task(task):
        return False
    params = dict(task.input_json or {})
    # Text-to-video segments are intentionally independent provider requests.
    # A segment number greater than one is not evidence of an image
    # dependency; applying the legacy Omni chaining rule here silently turns
    # T2V back into image-to-video after the first clip.
    if (
        str(
            params.get("content_factory_video_generation_mode") or ""
        ).strip().lower()
        == "text_to_video"
    ):
        return False
    if str(params.get("model") or "").strip().lower() != "omni_flash":
        return False
    try:
        segment_index = int(params.get("content_factory_segment_index") or 0)
    except (TypeError, ValueError):
        segment_index = 0
    if segment_index <= 1:
        return False
    return not bool(params.get("content_factory_first_frame"))


def _content_factory_terminal_delivery(task: KieTask) -> bool:
    """Reject a late Celery delivery unless an explicit retry reset the row.

    Both content-factory and direct AI-video retries are explicit state
    transitions on the same local task id.  A delayed delivery for an older
    attempt must not clear a terminal decision or resubmit provider work after
    the retry budget ended.  ``reset_video_task_for_retry`` moves an authorized
    retry back to ``queued_local``, so the state check is sufficient for both
    task origins.
    """
    return str(task.state or "").strip().lower() in {
        "failed",
        "fail",
        "error",
        "timeout",
        "cancelled",
        "canceled",
    }


@celery_app.task(
    name="ai_video.video.submit_and_poll",
    bind=True,
    queue=AI_VIDEO_API_TASK_QUEUE,
    max_retries=MAX_AUTO_RETRIES,
    default_retry_delay=15,
)
def submit_and_poll_ai_video_task(
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
            if _task_has_terminal_request_rejection(task):
                _seal_terminal_request_rejection(db, task)
                db.commit()
                return _payload(task)
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(
                    task, reason=authority_reason, db=db
                )
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
            if _content_factory_terminal_delivery(task):
                db.commit()
                logger.info(
                    "Ignored late delivery for terminal content-factory video task",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "state": task.state,
                    },
                )
                return _payload(task)
            if _content_factory_dependency_pending(task):
                task.state = "waiting_dependency"
                task.fail_code = None
                task.fail_msg = None
                db.commit()
                return _payload(task)
            prompt_contract_error = (
                _content_factory_provider_prompt_contract_error(task)
            )
            if prompt_contract_error:
                task.state = "failed"
                task.fail_code = CONTENT_FACTORY_PROMPT_CONTRACT_CODE
                task.fail_msg = (
                    "CONTENT_FACTORY_PROVIDER_PROMPT_CONTRACT_INVALID: "
                    + prompt_contract_error
                )
                set_task_local_meta(
                    task,
                    provider_prompt_contract_rejected_at=(
                        datetime.now(timezone.utc).isoformat()
                    ),
                    provider_prompt_contract_rejected_before_io=True,
                )
                db.add(task)
                db.commit()
                logger.warning(
                    "Rejected invalid Content Factory provider packet before I/O",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "reason": prompt_contract_error,
                    },
                )
                return _payload(task)
            request_id = str(getattr(self.request, "id", "") or "")
            if not _claim_poll_owner(
                task,
                owner_task_id=request_id,
                max_age_seconds=_provider_submit_lease_seconds(interval_seconds),
            ):
                db.commit()
                return _payload(task)
            db.add(task)
            db.commit()
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id, for_update=True)
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(
                    task, reason=authority_reason, db=db
                )
                db.add(task)
                db.commit()
                return _payload(task)
            prompt_contract_error = (
                _content_factory_provider_prompt_contract_error(task)
            )
            if prompt_contract_error:
                task.state = "failed"
                task.fail_code = CONTENT_FACTORY_PROMPT_CONTRACT_CODE
                task.fail_msg = (
                    "CONTENT_FACTORY_PROVIDER_PROMPT_CONTRACT_INVALID: "
                    + prompt_contract_error
                )
                set_task_local_meta(
                    task,
                    poll_owner_task_id=None,
                    provider_prompt_contract_rejected_at=(
                        datetime.now(timezone.utc).isoformat()
                    ),
                    provider_prompt_contract_rejected_before_io=True,
                )
                db.add(task)
                db.commit()
                return _payload(task)
            if _is_unconfirmed_doubao_submit_marker(task):
                # This delivery owns the same durable poll lease, while the
                # previous helper invocation is already gone. Recover before
                # provider dispatch; refreshing this marker can only raise a
                # missing-binding error forever because it contains no remote
                # conversation id.
                task = _recover_unconfirmed_doubao_submit(db, task)
            if is_local_task_id(task.task_id):
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
            if _provider_error_is_pool_backpressure(exc):
                return _defer_provider_pool_backpressure(
                    db,
                    task,
                    workspace_id=int(workspace_id),
                    local_task_id=int(local_task_id),
                    interval_seconds=int(interval_seconds),
                    timeout_seconds=int(timeout_seconds),
                )
            if _provider_error_is_generation_conflict(exc):
                return _mark_failed(
                    db,
                    task,
                    exc,
                    active_provider=_active_provider(task),
                )
            if _provider_error_is_route_unavailable(exc):
                task = _fail_provider_and_advance(db, task, exc)
                return _payload(task)
            if _provider_error_is_terminal_request_rejection(exc):
                task = _fail_provider_and_advance(db, task, exc)
                state = str(task.state or "").lower()
                if state == "downloading":
                    queue_task_result_download(
                        workspace_id=int(workspace_id),
                        local_task_id=int(local_task_id),
                    )
                    return _payload(task)
                if state in {"failed", "error", "timeout"}:
                    return _payload(task)
                start_ts = time.monotonic()
            elif _provider_error_is_quota_failure(exc):
                task = _mark_provider_quota_failure(db, task, exc)
                if _next_provider_key(db, task) is None:
                    restore_archived_task_result_files(
                        db,
                        task,
                        failure_code=task.fail_code,
                        failure_message=task.fail_msg,
                    )
                    db.commit()
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
                    task = _load_task(
                        db,
                        workspace_id=workspace_id,
                        local_task_id=local_task_id,
                        for_update=True,
                    )
                    task = _fail_provider_and_advance(db, task, failover_exc)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if str(task.state or "").lower() in {"failed", "error", "timeout"}:
                        return _payload(task)
            elif _should_advance_route_immediately(task, exc):
                task = _fail_provider_and_advance(db, task, exc)
                advanced_state = str(task.state or "").lower()
                if advanced_state == "downloading":
                    queue_task_result_download(
                        workspace_id=int(workspace_id),
                        local_task_id=int(local_task_id),
                    )
                    return _payload(task)
                if advanced_state in {"failed", "error", "timeout"}:
                    return _payload(task)
                start_ts = time.monotonic()
            else:
                recovery_decision = _self_hosted_recovery_decision(
                    db,
                    task,
                    exc,
                    retry_number=int(self.request.retries),
                )
                if (
                    recovery_decision is not None
                    and recovery_decision.action == VideoRecoveryAction.SWITCH_PROVIDER
                ):
                    task = _fail_provider_and_advance(db, task, exc)
                    switched_state = str(task.state or "").lower()
                    if switched_state == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if switched_state in {"failed", "error", "timeout"}:
                        return _payload(task)
                else:
                    if recovery_decision is not None and recovery_decision.action in {
                        VideoRecoveryAction.PAUSE_AUTH,
                        VideoRecoveryAction.PAUSE_POLICY,
                    }:
                        return _mark_failed(db, task, exc, active_provider=_active_provider(task))
                    if (
                        recovery_decision is not None
                        and recovery_decision.action == VideoRecoveryAction.WAIT_RETRY_SAME
                        and self.request.retries < self.max_retries
                        and classify_video_provider_fault(exc)
                        in {"UPSTREAM_TRANSIENT", "NETWORK", "UNKNOWN"}
                        and _next_provider_key(db, task) is None
                        and _prepare_provider_cycle_after_cooldown(task)
                    ):
                        db.add(task)
                        db.commit()
                    if self.request.retries >= self.max_retries:
                        task = _fail_provider_and_advance(db, task, exc)
                        start_ts = time.monotonic()
                        if str(task.state or "").lower() == "downloading":
                            queue_task_result_download(
                                workspace_id=int(workspace_id),
                                local_task_id=int(local_task_id),
                            )
                            return _payload(task)
                        if str(task.state or "").lower() in {"failed", "error", "timeout"}:
                            return _payload(task)
                    else:
                        raise self.retry(
                            exc=exc,
                            countdown=_provider_retry_wait_seconds(
                                exc,
                                retry_number=int(self.request.retries),
                                recovery_wait_seconds=(
                                    int(recovery_decision.wait_seconds)
                                    if recovery_decision is not None
                                    else None
                                ),
                            ),
                        )

        while True:
            task = _load_task(
                db,
                workspace_id=workspace_id,
                local_task_id=local_task_id,
                for_update=True,
            )
            if _task_has_terminal_request_rejection(task):
                _seal_terminal_request_rejection(db, task)
                db.commit()
                return _payload(task)
            authorized, authority_reason = _content_factory_execution_authority(db, task)
            if not authorized:
                _quarantine_non_authoritative_content_task(
                    task, reason=authority_reason, db=db
                )
                db.add(task)
                db.commit()
                return _payload(task)
            if _content_factory_terminal_delivery(task):
                db.commit()
                return _payload(task)
            pre_refresh_state = str(task.state or "").lower()
            if pre_refresh_state == "queued_local" and is_local_task_id(task.task_id):
                # A provider failover was durably republished to the queue
                # that owns the replacement provider.  The current delivery
                # must relinquish control rather than execute cross-lane I/O.
                db.commit()
                return _payload(task)
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
                if _provider_error_is_generation_conflict(exc):
                    return _mark_failed(
                        db,
                        task,
                        exc,
                        active_provider=_active_provider(task),
                    )
                if _provider_error_is_route_unavailable(exc):
                    task = _fail_provider_and_advance(db, task, exc)
                    return _payload(task)
                if _provider_error_is_terminal_request_rejection(exc):
                    task = _fail_provider_and_advance(db, task, exc)
                    state = str(task.state or "").lower()
                    if state == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if state in {"failed", "error", "timeout"}:
                        return _payload(task)
                    start_ts = time.monotonic()
                    continue
                if _provider_error_is_quota_failure(exc):
                    task = _mark_provider_quota_failure(db, task, exc)
                    if _next_provider_key(db, task) is None:
                        restore_archived_task_result_files(
                            db,
                            task,
                            failure_code=task.fail_code,
                            failure_message=task.fail_msg,
                        )
                        db.commit()
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
                        task = _load_task(
                            db,
                            workspace_id=workspace_id,
                            local_task_id=local_task_id,
                            for_update=True,
                        )
                        task = _fail_provider_and_advance(db, task, failover_exc)
                        start_ts = time.monotonic()
                        if str(task.state or "").lower() == "downloading":
                            queue_task_result_download(
                                workspace_id=int(workspace_id),
                                local_task_id=int(local_task_id),
                            )
                            return _payload(task)
                        if str(task.state or "").lower() in {"failed", "error", "timeout"}:
                            return _payload(task)
                        continue
                if _should_advance_route_immediately(task, exc):
                    task = _fail_provider_and_advance(db, task, exc)
                    advanced_state = str(task.state or "").lower()
                    if advanced_state == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if advanced_state in {"failed", "error", "timeout"}:
                        return _payload(task)
                    start_ts = time.monotonic()
                    continue
                if self.request.retries >= self.max_retries:
                    task = _fail_provider_and_advance(db, task, exc)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if str(task.state or "").lower() in {"failed", "error", "timeout"}:
                        return _payload(task)
                    continue
                recovery_decision = _self_hosted_recovery_decision(
                    db,
                    task,
                    exc,
                    retry_number=int(self.request.retries),
                )
                if (
                    recovery_decision is not None
                    and recovery_decision.action == VideoRecoveryAction.SWITCH_PROVIDER
                ):
                    task = _fail_provider_and_advance(db, task, exc)
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    if str(task.state or "").lower() in {"failed", "error", "timeout"}:
                        return _payload(task)
                    continue
                if recovery_decision is not None and recovery_decision.action in {
                    VideoRecoveryAction.PAUSE_AUTH,
                    VideoRecoveryAction.PAUSE_POLICY,
                }:
                    return _mark_failed(db, task, exc, active_provider=_active_provider(task))
                if (
                    recovery_decision is not None
                    and recovery_decision.action == VideoRecoveryAction.WAIT_RETRY_SAME
                    and classify_video_provider_fault(exc)
                    in {"UPSTREAM_TRANSIENT", "NETWORK", "UNKNOWN"}
                    and _next_provider_key(db, task) is None
                    and _prepare_provider_cycle_after_cooldown(task)
                ):
                    db.add(task)
                    db.commit()
                logger.warning(
                    "AI video provider query error, will retry",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "error": str(exc),
                    },
                )
                raise self.retry(
                    exc=exc,
                    countdown=_provider_retry_wait_seconds(
                        exc,
                        retry_number=int(self.request.retries),
                        recovery_wait_seconds=(
                            int(recovery_decision.wait_seconds)
                            if recovery_decision is not None
                            else None
                        ),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "AI video provider polling iteration failed",
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
                    "AI video task reached terminal state",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "state": state,
                        "fail_code": task.fail_code,
                        "fail_msg": task.fail_msg,
                    },
                )
                return _payload(task)

            if _active_provider(task) == DOUBAO_PROVIDER_KEY:
                if _doubao_remote_wait_expired(
                    task,
                    timeout_seconds=int(timeout_seconds),
                ):
                    release_doubao_task_account(
                        db,
                        task=task,
                        error_code="doubao_poll_timeout",
                    )
                    task.state = "timeout"
                    task.fail_code = "provider_poll_timeout"
                    task.fail_msg = "豆包远端任务超过总等待时限，系统将重新调度。"
                    set_task_local_meta(
                        task,
                        doubao_remote_timeout_at=datetime.now(timezone.utc).isoformat(),
                        poll_owner_task_id=None,
                        poll_heartbeat_at=None,
                        poll_heartbeat_provider=None,
                    )
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
                            task = _load_task(
                                db,
                                workspace_id=workspace_id,
                                local_task_id=local_task_id,
                            )
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
                            task = _load_task(
                                db,
                                workspace_id=workspace_id,
                                local_task_id=local_task_id,
                            )
                            return _mark_failed(
                                db,
                                task,
                                exc,
                                active_provider=_active_provider(task),
                            )
                    return _payload(task)
                return _handoff_doubao_poll(
                    db,
                    task,
                    workspace_id=int(workspace_id),
                    local_task_id=int(local_task_id),
                    interval_seconds=int(interval_seconds),
                    timeout_seconds=int(timeout_seconds),
                )

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
                        submit_and_poll_ai_video_task.apply_async(
                            kwargs={
                                "workspace_id": int(workspace_id),
                                "local_task_id": int(local_task_id),
                                "interval_seconds": int(interval_seconds),
                                "timeout_seconds": int(timeout_seconds),
                            },
                            countdown=30,
                            queue=production_video_queue(task),
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
    name="ai_video.video.recover_stale_polling",
    queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE,
)
def recover_stale_ai_video_polling(*, stale_minutes: int = 20, limit: int = 200) -> dict[str, Any]:
    db = _db_session()
    try:
        now = datetime.now()
        cutoff = now - timedelta(minutes=max(1, int(stale_minutes)))
        orphan_cutoff = now - timedelta(hours=24)
        throttle_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        all_active_states = {
            "submitted", "pending", "waiting", "queued", "queuing",
            "queued_local", "submitting", "waiting_dependency", "in_progress",
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
                _quarantine_non_authoritative_content_task(
                    task, reason=authority_reason, db=db
                )
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
            "submitting",
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
                _quarantine_non_authoritative_content_task(
                    task, reason=authority_reason, db=db
                )
                db.add(task)
                db.commit()
                if int(task.id) not in quarantined_non_authoritative_ids:
                    quarantined_non_authoritative_ids.append(int(task.id))
                continue
            # A worker soft-timeout can kill the Doubao browser helper after
            # the local submit marker is committed but before a remote
            # conversation id is returned.  There is nothing to poll in that
            # state.  Reopen the same logical task (and release its account
            # lease) before scheduling the recovery delivery; creating a new
            # task here would break content-factory idempotency.
            if _is_unconfirmed_doubao_submit_marker(task):
                task = _recover_unconfirmed_doubao_submit(db, task)
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
            submit_and_poll_ai_video_task.apply_async(
                kwargs={
                    "workspace_id": int(task.workspace_id),
                    "local_task_id": int(task.id),
                    "interval_seconds": int(getattr(settings, "BANDIANWA_POLL_INTERVAL_SECONDS", 15)),
                    "timeout_seconds": int(getattr(settings, "BANDIANWA_POLL_TIMEOUT_SECONDS", 10 * 60)),
                },
                queue=production_video_queue(task),
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
