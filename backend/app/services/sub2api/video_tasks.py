from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.ai_video.accounts import (
    OMNI_FLASH_MODEL,
    SUB2API_PROVIDER_KEY,
    decrypt_api_key,
    provider_reference_limit,
)
from app.services.ai_video.local_storage import mark_result_file_pending, set_task_local_meta
from app.services.sub2api.client import Sub2ApiApiError
from app.services.sub2api.video_client import Sub2ApiVideoClient


ALLOWED_DURATIONS = {4, 6, 8, 10}


def _key(db: Session, task: KieTask) -> KieApiKey:
    key = db.get(KieApiKey, int(task.key_id))
    if key is None or not key.is_active or key.provider_key != SUB2API_PROVIDER_KEY:
        raise Sub2ApiApiError("Sub2API Flow API key not found or inactive")
    return key


def _references(db: Session, task: KieTask) -> list[KieFile]:
    return (
        db.query(KieFile)
        .filter(KieFile.task_id == int(task.id), KieFile.kind == "reference_upload")
        .order_by(KieFile.id.asc())
        .all()
    )


def _prompt(params: Mapping[str, Any], task: KieTask) -> str:
    for name in ("provider_prompt", "full_prompt", "prompt"):
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(task.prompt or "").strip()


def _model_name(params: Mapping[str, Any], reference_count: int) -> str:
    try:
        duration = int(params.get("seconds") or params.get("duration") or 8)
    except (TypeError, ValueError):
        duration = 8
    if duration not in ALLOWED_DURATIONS:
        raise Sub2ApiApiError("Sub2API Flow duration must be 4, 6, 8, or 10 seconds")
    aspect_ratio = str(params.get("aspect_ratio") or "9:16").strip()
    if aspect_ratio not in {"9:16", "16:9"}:
        raise Sub2ApiApiError("Sub2API Flow aspect ratio must be 9:16 or 16:9")
    resolution = str(params.get("resolution") or "720p").strip().lower()
    if resolution not in {"720", "720p", "1080", "1080p"}:
        raise Sub2ApiApiError("Sub2API Flow resolution must be 720p or 1080p")
    mode = "r2v" if reference_count else "t2v"
    portrait = "_portrait" if aspect_ratio == "9:16" else ""
    high_resolution = "_1080p" if resolution in {"1080", "1080p"} else ""
    return f"gemini_omni_{mode}{portrait}_{duration}s{high_resolution}"


def _ensure_result_file(db: Session, task: KieTask, url: str) -> KieFile:
    row = (
        db.query(KieFile)
        .filter(
            KieFile.task_id == int(task.id),
            KieFile.kind == "result",
            KieFile.file_url == url,
        )
        .order_by(KieFile.id.asc())
        .first()
    )
    if row is None:
        row = KieFile(
            workspace_id=int(task.workspace_id),
            key_id=int(task.key_id),
            task_id=int(task.id),
            kind="result",
            file_url=url,
            mime_type="video/mp4",
            meta_json={"source": "sub2api_flow"},
        )
    local_meta = dict(dict(task.result_json or {}).get("__local") or {})
    mark_result_file_pending(
        row,
        filename=str(local_meta.get("download_name_base") or task.id),
    )
    db.add(row)
    return row


def _generation_idempotency_key(task: KieTask) -> str:
    """Return one stable upstream identity per paid generation.

    Provider retries and worker redeliveries reuse the same generation.  An
    explicit user regeneration increments ``generation_epoch`` and must use a
    new identity even though the local task row is intentionally stable.  The
    manual retry count is a compatibility fallback for tasks reset before the
    generation field was introduced (including the live 3587 incident).
    """

    local_meta = dict(dict(task.result_json or {}).get("__local") or {})
    try:
        generation_epoch = int(local_meta.get("generation_epoch") or 0)
    except (TypeError, ValueError):
        generation_epoch = 0
    try:
        manual_retry_count = int(local_meta.get("manual_retry_count") or 0)
    except (TypeError, ValueError):
        manual_retry_count = 0
    generation_epoch = max(generation_epoch, manual_retry_count)
    base = f"workspace:{task.workspace_id}:task:{task.id}"
    return (
        f"{base}:generation:{generation_epoch}"
        if generation_epoch > 0
        else base
    )


async def submit_sub2api_video_task(db: Session, *, task: KieTask) -> KieTask:
    key = _key(db, task)
    references = _references(db, task)
    reference_limit = provider_reference_limit(SUB2API_PROVIDER_KEY, OMNI_FLASH_MODEL)
    if reference_limit >= 0 and len(references) > reference_limit:
        raise Sub2ApiApiError(
            "Sub2API Flow reference image count exceeds the configured "
            f"provider capability ({reference_limit})"
        )
    params = dict(task.input_json or {})
    model = _model_name(params, len(references))
    client = Sub2ApiVideoClient(api_key=decrypt_api_key(key.api_key_ciphertext))

    async def keep_poll_lease_alive() -> None:
        """Keep one durable owner while Flow performs a long synchronous call.

        Flow can spend several minutes recovering reCAPTCHA before it creates
        the remote video.  Committing this advisory heartbeat also releases
        the task row lock while the network request is in flight, so control
        operations remain responsive without allowing another worker to take
        over the same idempotent submission.
        """
        set_task_local_meta(
            task,
            poll_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            poll_heartbeat_provider=SUB2API_PROVIDER_KEY,
        )
        task.updated_at = datetime.now()
        db.add(task)
        db.commit()

    response, urls = await client.generate(
        model=model,
        prompt=_prompt(params, task),
        references=[(str(row.file_url), row.mime_type) for row in references],
        idempotency_key=_generation_idempotency_key(task),
        heartbeat=keep_poll_lease_alive,
    )
    # A heartbeat commit intentionally releases the original row lock. Reclaim
    # it before materializing provider results so two idempotent callers cannot
    # both observe an empty result set and insert duplicate KieFile rows.
    task = (
        db.query(KieTask)
        .filter(KieTask.id == int(task.id))
        .populate_existing()
        .with_for_update()
        .one()
    )
    task.task_id = f"sub2api-flow-{int(task.id)}"
    task.result_json = {
        **dict(task.result_json or {}),
        "sub2api_flow_model": model,
        "sub2api_flow_response": response,
    }
    for url in urls:
        _ensure_result_file(db, task, url)
    task.state = "downloading"
    task.fail_code = None
    task.fail_msg = None
    set_task_local_meta(task, download_enqueued_at=None)
    db.add(task)
    db.flush()
    return task


async def refresh_sub2api_video_task(db: Session, *, task: KieTask) -> KieTask:
    # Flow generation completes during the background submit call.  The common
    # worker only reaches refresh after a broker redelivery; keep the terminal
    # download state idempotent rather than issuing a second billable request.
    if str(task.state or "").lower() in {"downloading", "success"}:
        return task
    raise Sub2ApiApiError("Sub2API Flow task has no completed result to refresh")


__all__ = [
    "_generation_idempotency_key",
    "refresh_sub2api_video_task",
    "submit_sub2api_video_task",
]
