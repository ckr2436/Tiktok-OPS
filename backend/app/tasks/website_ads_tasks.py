from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.celery_app import WHISPER_TASK_QUEUE, celery_app
from app.core.config import settings
from app.data.db import get_db
from app.data.models.website_ads import WebsiteAdsCreativeAsset, WebsiteAdsMediaPlan, WebsiteAdsUploadFingerprint
from app.features.tenants.ttb.gmv_max._helpers import get_ttb_client_for_account
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.website_ads_asset_pipeline import (
    ASSET_ANALYSIS_VERSION,
    run_asset_analysis_pipeline,
    sync_asset_libraries,
)
from app.services.website_ads_asset_expansion import run_website_ads_asset_expansion_cycle
from app.services.website_ads_monitor import run_website_ads_monitor_cycle
from app.services.website_ads_daily_report import run_website_ads_daily_report_cycle
from app.services.website_ads_hermes_planner import generate_media_plan
from app.services.website_ads_media_cache import (
    LOCAL_CACHE_KEY,
    cleanup_stale_media_partials,
    ensure_asset_media_cache,
    resolve_asset_media,
)
from app.services.website_ads_plan_launch import execute_media_plan
from app.services.website_ads_targeting_catalog import sync_all_targeting_catalogs
from app.services.website_ads_uploads import (
    UPLOAD_JOB_KEY,
    archived_media_for_upload,
    complete_upload_fingerprint,
    fail_upload_fingerprint,
    upload_job,
    upload_result,
)
from app.services.gmvmax_creative_media_cache import (
    cache_creative_asset_media,
    claim_creative_media_cache_batch,
    mark_creative_media_queue_error,
)


logger = logging.getLogger("gmv.tasks.website_ads")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_MEDIA_PLAN_GENERATION_TERMINAL_STATES = {
    "READY",
    "EXECUTION_QUEUED",
    "EXECUTING",
    "CREATED",
    "ACTIVE",
    "FAILED",
}


def _media_plan_generation_claim_action(
    *,
    status: str | None,
    stored_task_id: str | None,
    request_task_id: str | None,
    redelivered: bool,
) -> str:
    normalized_status = str(status or "MISSING").upper()
    stored_id = str(stored_task_id or "")
    request_id = str(request_task_id or "")
    if normalized_status in _MEDIA_PLAN_GENERATION_TERMINAL_STATES:
        return "TERMINAL"
    if normalized_status == "GENERATING":
        return "CLAIM" if not stored_id or stored_id == request_id else "DEDUPLICATE"
    if (
        normalized_status == "PROCESSING"
        and stored_id
        and stored_id == request_id
        and bool(redelivered)
    ):
        return "RESUME"
    return "DEDUPLICATE"


def dispatch_asset_analysis(
    db: Session,
    *,
    workspace_id: int | None = None,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    asset_ids: list[int] | None = None,
    force: bool = False,
    limit: int = 8,
) -> dict[str, object]:
    now = _utcnow()
    query = select(WebsiteAdsCreativeAsset).where(WebsiteAdsCreativeAsset.is_active.is_(True))
    if workspace_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.workspace_id == int(workspace_id))
    if auth_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.auth_id == int(auth_id))
    if advertiser_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id))
    if asset_ids:
        query = query.where(WebsiteAdsCreativeAsset.id.in_([int(value) for value in asset_ids]))
    if not force:
        query = query.where(
            WebsiteAdsCreativeAsset.analysis_status.notin_(("QUEUED", "EXTRACTING", "ANALYZING")),
            or_(
                WebsiteAdsCreativeAsset.analysis_version.is_(None),
                WebsiteAdsCreativeAsset.analysis_version != ASSET_ANALYSIS_VERSION,
                WebsiteAdsCreativeAsset.analysis_status.in_(("NOT_ANALYZED", "FAILED")),
            ),
            or_(
                WebsiteAdsCreativeAsset.analysis_next_retry_at.is_(None),
                WebsiteAdsCreativeAsset.analysis_next_retry_at <= now,
            ),
        )
    rows = list(
        db.scalars(
            query.order_by(
                func.coalesce(
                    WebsiteAdsCreativeAsset.analysis_next_retry_at,
                    WebsiteAdsCreativeAsset.updated_at,
                ).asc(),
                WebsiteAdsCreativeAsset.id.asc(),
            ).limit(max(1, int(limit)))
        ).all()
    )
    for row in rows:
        row.analysis_status = "QUEUED"
        row.analysis_error = None
        db.add(row)
    db.commit()

    queued: list[int] = []
    for row in rows:
        try:
            analyze_asset_task.apply_async(kwargs={"asset_id": int(row.id)}, queue=WHISPER_TASK_QUEUE)
            queued.append(int(row.id))
        except Exception as exc:
            row.analysis_status = "NOT_ANALYZED"
            row.analysis_error = f"QueueError: {exc}"[:2000]
            failed_at = _utcnow()
            row.analysis_next_retry_at = failed_at + timedelta(minutes=5)
            row.updated_at = failed_at
            db.add(row)
    db.commit()
    return {"queued": len(queued), "asset_ids": queued}


def dispatch_asset_media_cache(
    db: Session,
    *,
    workspace_id: int | None = None,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    asset_ids: list[int] | None = None,
    limit: int = 12,
) -> dict[str, object]:
    query = select(WebsiteAdsCreativeAsset)
    if workspace_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.workspace_id == int(workspace_id))
    if auth_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.auth_id == int(auth_id))
    if advertiser_id is not None:
        query = query.where(WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id))
    if asset_ids:
        query = query.where(WebsiteAdsCreativeAsset.id.in_([int(value) for value in asset_ids]))
    dispatch_checked_at = WebsiteAdsCreativeAsset.raw_json[LOCAL_CACHE_KEY][
        "dispatch_checked_at"
    ].as_string()
    rows = list(
        db.scalars(
            query.order_by(
                dispatch_checked_at.is_not(None),
                dispatch_checked_at.asc(),
                WebsiteAdsCreativeAsset.id.asc(),
            ).limit(max(100, int(limit) * 20))
        ).all()
    )
    now = datetime.now(timezone.utc)
    candidates: list[WebsiteAdsCreativeAsset] = []
    for row in rows:
        raw = dict(row.raw_json or {})
        cache = dict(raw.get(LOCAL_CACHE_KEY) or {}) if isinstance(raw.get(LOCAL_CACHE_KEY), dict) else {}
        # Persist the inspection time for every bounded-scan row, including
        # already-cached and not-yet-due rows.  The next dispatcher invocation
        # therefore starts with rows it has never inspected (or inspected least
        # recently), instead of repeatedly scanning only recently synced assets.
        cache["dispatch_checked_at"] = now.isoformat()
        raw[LOCAL_CACHE_KEY] = cache
        row.raw_json = raw
        db.add(row)
        video_ready = resolve_asset_media(row, "video") is not None
        cover_ready = resolve_asset_media(row, "cover") is not None
        metadata_ready = isinstance(cache.get("video"), dict) and isinstance(cache.get("cover"), dict)
        state = str(cache.get("state") or "").upper()
        if video_ready and cover_ready and metadata_ready:
            if state != "READY":
                cache["state"] = "READY"
                cache["checked_at"] = now.isoformat()
                cache.pop("last_errors", None)
            continue
        queued_at = None
        try:
            queued_at = datetime.fromisoformat(str(cache.get("queued_at") or cache.get("processing_at") or ""))
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=timezone.utc)
        except ValueError:
            queued_at = None
        if state in {"QUEUED", "PROCESSING"} and queued_at and queued_at >= now - timedelta(minutes=30):
            continue
        checked_at = None
        try:
            checked_at = datetime.fromisoformat(str(cache.get("checked_at") or ""))
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
        except ValueError:
            checked_at = None
        if (
            state == "PARTIAL"
            and checked_at
            and checked_at >= now - timedelta(minutes=int(settings.WEBSITE_ADS_MEDIA_CACHE_RETRY_MINUTES))
        ):
            continue
        cache["state"] = "QUEUED"
        cache["queued_at"] = now.isoformat()
        raw[LOCAL_CACHE_KEY] = cache
        row.raw_json = raw
        candidates.append(row)
        if len(candidates) >= max(1, int(limit)):
            break
    db.commit()

    queued: list[int] = []
    for row in candidates:
        try:
            cache_asset_media_task.apply_async(
                kwargs={"asset_id": int(row.id)},
                queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
            )
            queued.append(int(row.id))
        except Exception as exc:
            raw = dict(row.raw_json or {})
            cache = dict(raw.get(LOCAL_CACHE_KEY) or {}) if isinstance(raw.get(LOCAL_CACHE_KEY), dict) else {}
            cache["state"] = "PARTIAL"
            cache["queue_error"] = f"{type(exc).__name__}: {exc}"[:1000]
            cache["checked_at"] = now.isoformat()
            raw[LOCAL_CACHE_KEY] = cache
            row.raw_json = raw
            db.add(row)
    db.commit()
    return {"queued": len(queued), "asset_ids": queued}


def dispatch_gmvmax_creative_media_cache(db: Session, *, limit: int = 12) -> dict[str, object]:
    asset_ids = claim_creative_media_cache_batch(db, limit=max(1, int(limit)))
    queued: list[int] = []
    failed: list[int] = []
    for asset_id in asset_ids:
        try:
            cache_gmvmax_creative_media_task.apply_async(
                kwargs={"asset_id": int(asset_id)},
                queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
            )
            queued.append(int(asset_id))
        except Exception as exc:
            mark_creative_media_queue_error(db, int(asset_id), exc)
            failed.append(int(asset_id))
    return {"queued": len(queued), "asset_ids": queued, "dispatch_failed": failed}


def recover_stale_upload_fingerprints(db: Session) -> int:
    stale_before = _utcnow() - timedelta(minutes=int(settings.WEBSITE_ADS_UPLOAD_STALE_MINUTES))
    rows = list(
        db.scalars(
            select(WebsiteAdsUploadFingerprint).where(
                WebsiteAdsUploadFingerprint.status.in_(("UPLOADING", "PROCESSING", "RETRYING")),
                WebsiteAdsUploadFingerprint.updated_at < stale_before,
            )
        ).all()
    )
    now = _utcnow()
    for row in rows:
        raw = dict(row.response_json or {})
        if isinstance(raw.get(UPLOAD_JOB_KEY), dict):
            row.status = "QUEUED"
            row.error_message = "UploadRecovered: interrupted background upload was queued again"
        else:
            row.status = "FAILED"
            row.error_message = "UploadInterrupted: upload did not finish before the recovery window"
        row.updated_at = now
        db.add(row)
    db.commit()
    return len(rows)


def dispatch_pending_uploads(db: Session, *, limit: int = 20) -> dict[str, object]:
    dispatch_now = _utcnow()
    rows = list(
        db.scalars(
            select(WebsiteAdsUploadFingerprint)
            .where(
                or_(
                    WebsiteAdsUploadFingerprint.status == "QUEUED",
                    (
                        (WebsiteAdsUploadFingerprint.status == "RETRYING")
                        & (
                            WebsiteAdsUploadFingerprint.updated_at
                            <= dispatch_now - timedelta(minutes=5)
                        )
                    ),
                )
            )
            .order_by(WebsiteAdsUploadFingerprint.updated_at.asc(), WebsiteAdsUploadFingerprint.id.asc())
            .limit(max(1, int(limit)))
        ).all()
    )
    claimed_at = dispatch_now
    for row in rows:
        row.status = "UPLOADING"
        row.error_message = None
        row.updated_at = claimed_at
        db.add(row)
    # Claim before publishing.  Successful rows leave the eligible query
    # immediately; a process crash is recovered by recover_stale_upload_fingerprints.
    db.commit()

    queued: list[int] = []
    failed: list[int] = []
    for row in rows:
        try:
            job = upload_job(row)
            upload_video_task.apply_async(
                kwargs={"upload_id": int(row.id), "provider": str(job.get("provider") or "tiktok-business")},
                queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
            )
            queued.append(int(row.id))
        except Exception as exc:
            row.status = "RETRYING"
            row.error_message = f"QueueDispatchPending: {type(exc).__name__}: {exc}"[:4000]
            row.updated_at = _utcnow()
            db.add(row)
            failed.append(int(row.id))
    db.commit()
    return {"queued": len(queued), "upload_ids": queued, "dispatch_failed": failed}


def _db_session() -> Session:
    generator = get_db()
    session = next(generator)
    setattr(session, "__generator", generator)
    return session


def _close_session(session: Session) -> None:
    generator = getattr(session, "__generator", None)
    try:
        session.close()
    finally:
        if generator:
            try:
                next(generator)
            except StopIteration:
                pass


async def _upload_archived_video(
    db: Session,
    *,
    row: WebsiteAdsUploadFingerprint,
    provider: str,
) -> dict:
    job = upload_job(row)
    archived = archived_media_for_upload(row)
    client = TikTokWebsiteAdsClient(
        get_ttb_client_for_account(db, int(row.workspace_id), provider, int(row.auth_id))
    )
    try:
        with archived.path.open("rb") as stream:
            return dict(
                await client.upload_video_file(
                    str(row.advertiser_id),
                    str(job.get("upload_name") or row.file_name or archived.original_name),
                    stream,
                    archived.md5,
                    content_type=archived.content_type,
                    flaw_detect=bool(job.get("flaw_detect")),
                    auto_fix_enabled=bool(job.get("auto_fix_enabled")),
                )
                or {}
            )
    finally:
        await client.aclose()


@celery_app.task(
    name="website_ads.upload_video",
    bind=True,
    queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
    max_retries=2,
    track_started=True,
    soft_time_limit=900,
    time_limit=960,
)
def upload_video_task(self, *, upload_id: int, provider: str):
    db = _db_session()
    try:
        claim = db.execute(
            update(WebsiteAdsUploadFingerprint)
            .where(
                WebsiteAdsUploadFingerprint.id == int(upload_id),
                WebsiteAdsUploadFingerprint.status.in_(("QUEUED", "RETRYING", "UPLOADING")),
            )
            .values(status="PROCESSING", error_message=None, updated_at=_utcnow())
        )
        db.commit()
        row = db.get(WebsiteAdsUploadFingerprint, int(upload_id))
        if row is None:
            return {"upload_id": int(upload_id), "upload_status": "MISSING"}
        if int(claim.rowcount or 0) != 1:
            return upload_result(row, deduplicated=str(row.status or "").upper() == "UPLOADED")

        self.update_state(
            state="PROGRESS",
            meta={"upload_id": int(upload_id), "stage": "TIKTOK_UPLOAD"},
        )
        job = upload_job(row)
        archived = archived_media_for_upload(row)
        payload = asyncio.run(_upload_archived_video(db, row=row, provider=str(provider)))
        result = complete_upload_fingerprint(
            db,
            row,
            payload,
            archived=archived,
            original_name=str(job.get("original_name") or archived.original_name),
            source_url=str(job.get("source_url")) if job.get("source_url") else None,
        )
        if result.get("asset_id"):
            try:
                dispatch_asset_analysis(
                    db,
                    workspace_id=int(row.workspace_id),
                    auth_id=int(row.auth_id),
                    asset_ids=[int(result["asset_id"])],
                    limit=1,
                )
            except Exception:
                db.rollback()
                logger.exception(
                    "Website Ads upload completed but analysis dispatch failed",
                    extra={"upload_id": int(upload_id), "asset_id": int(result["asset_id"])},
                )
        return result
    except Exception as exc:
        db.rollback()
        row = db.get(WebsiteAdsUploadFingerprint, int(upload_id))
        if row and str(row.status or "").upper() == "UPLOADED":
            return upload_result(row, deduplicated=False)
        retries = int(self.request.retries or 0)
        if row and retries < int(self.max_retries or 0):
            row.status = "RETRYING"
            row.error_message = f"Retrying: {type(exc).__name__}: {exc}"[:4000]
            row.updated_at = _utcnow()
            db.add(row)
            db.commit()
            logger.warning(
                "Website Ads background upload will retry",
                extra={"upload_id": int(upload_id), "attempt": retries + 1},
            )
            raise self.retry(exc=exc, countdown=min(180, 30 * (2 ** retries)))
        if row:
            fail_upload_fingerprint(db, row, exc)
        logger.exception("Website Ads background upload failed", extra={"upload_id": int(upload_id)})
        raise
    finally:
        _close_session(db)


@celery_app.task(name="website_ads.monitor_cycle", bind=True, max_retries=2)
def monitor_cycle(self):
    db = _db_session()
    try:
        return asyncio.run(run_website_ads_monitor_cycle(db))
    except Exception as exc:
        db.rollback()
        logger.exception("Website Ads monitor cycle failed")
        raise self.retry(exc=exc, countdown=min(180, 30 * (self.request.retries + 1)))
    finally:
        _close_session(db)


@celery_app.task(name="website_ads.daily_report_cycle", bind=True, max_retries=2)
def daily_report_cycle(self):
    db = _db_session()
    try:
        return asyncio.run(run_website_ads_daily_report_cycle(db))
    except Exception as exc:
        db.rollback()
        logger.exception("Website Ads daily report cycle failed")
        raise self.retry(exc=exc, countdown=min(300, 60 * (self.request.retries + 1)))
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.generate_media_plan",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
    max_retries=0,
    track_started=True,
    soft_time_limit=1200,
    time_limit=1260,
)
def generate_media_plan_task(
    self,
    *,
    plan_id: int,
    workspace_id: int,
    auth_id: int,
    provider: str,
    landing_page_id: int,
    creative_asset_ids: list[int] | None,
    daily_budget: float,
    activate_after_create: bool,
    request_notes: str | None,
):
    db = _db_session()
    request_task_id = str(getattr(self.request, "id", "") or "")
    delivery_info = getattr(self.request, "delivery_info", None) or {}
    redelivered = bool(delivery_info.get("redelivered"))
    try:
        current = db.get(WebsiteAdsMediaPlan, int(plan_id))
        metadata = dict(current.hermes_response_json or {}) if current else {}
        action = _media_plan_generation_claim_action(
            status=current.status if current else None,
            stored_task_id=metadata.get("generation_task_id"),
            request_task_id=request_task_id,
            redelivered=redelivered,
        )
        if action == "CLAIM":
            claim = db.execute(
                update(WebsiteAdsMediaPlan)
                .where(
                    WebsiteAdsMediaPlan.id == int(plan_id),
                    WebsiteAdsMediaPlan.status == "GENERATING",
                )
                .values(status="PROCESSING", error_message=None, updated_at=_utcnow())
            )
            db.commit()
            if int(claim.rowcount or 0) != 1:
                current = db.get(WebsiteAdsMediaPlan, int(plan_id))
                metadata = dict(current.hermes_response_json or {}) if current else {}
                action = _media_plan_generation_claim_action(
                    status=current.status if current else None,
                    stored_task_id=metadata.get("generation_task_id"),
                    request_task_id=request_task_id,
                    redelivered=redelivered,
                )
            else:
                current = db.get(WebsiteAdsMediaPlan, int(plan_id))
        if action not in {"CLAIM", "RESUME"}:
            if action == "TERMINAL" and current:
                return {"plan_id": int(current.id), "state": str(current.status), "deduplicated": True}
            logger.warning(
                "Website Ads media plan task ignored because another worker owns it",
                extra={
                    "plan_id": int(plan_id),
                    "state": str(current.status if current else "MISSING"),
                    "task_id": request_task_id,
                    "stored_task_id": metadata.get("generation_task_id"),
                    "redelivered": redelivered,
                },
            )
            return {"plan_id": int(plan_id), "state": str(current.status if current else "MISSING"), "deduplicated": True}
        if current is None:
            return {"plan_id": int(plan_id), "state": "MISSING", "deduplicated": True}
        metadata = dict(current.hermes_response_json or {})
        if action == "RESUME" and int(metadata.get("generation_recovery_count") or 0) >= 2:
            current.status = "FAILED"
            current.error_message = "Hermes media plan generation stopped after repeated worker interruptions"
            metadata["generation_stage"] = "FAILED"
            metadata["generation_failed_at"] = _utcnow().isoformat()
            current.hermes_response_json = metadata
            db.add(current)
            db.commit()
            return {"plan_id": int(plan_id), "state": "FAILED", "recovery_limit_reached": True}
        metadata["generation_task_id"] = request_task_id
        metadata["generation_stage"] = "HERMES_PLANNING"
        metadata.setdefault("generation_started_at", _utcnow().isoformat())
        if action == "RESUME":
            metadata["generation_recovery_count"] = int(metadata.get("generation_recovery_count") or 0) + 1
            metadata["generation_recovered_at"] = _utcnow().isoformat()
            logger.warning(
                "Website Ads media plan generation resumed after broker redelivery",
                extra={"plan_id": int(plan_id), "task_id": request_task_id},
            )
        current.hermes_response_json = metadata
        current.status = "PROCESSING"
        current.error_message = None
        current.updated_at = _utcnow()
        db.add(current)
        db.commit()
        self.update_state(
            state="PROGRESS",
            meta={"plan_id": int(plan_id), "workspace_id": int(workspace_id), "stage": "HERMES_PLANNING"},
        )
        plan = asyncio.run(generate_media_plan(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            provider=provider,
            landing_page_id=int(landing_page_id),
            creative_asset_ids=creative_asset_ids,
            daily_budget=float(daily_budget),
            activate_after_create=bool(activate_after_create),
            request_notes=request_notes,
            pending_plan_id=int(plan_id),
        ))
        return {"plan_id": int(plan.id), "workspace_id": int(workspace_id), "auth_id": int(auth_id)}
    except Exception as exc:
        db.rollback()
        plan = db.get(WebsiteAdsMediaPlan, int(plan_id))
        plan_metadata = dict(plan.hermes_response_json or {}) if plan else {}
        owns_plan = not plan_metadata.get("generation_task_id") or str(
            plan_metadata.get("generation_task_id")
        ) == request_task_id
        if plan and owns_plan and str(plan.status or "").upper() in {"GENERATING", "PROCESSING"}:
            plan.status = "FAILED"
            plan.error_message = f"{type(exc).__name__}: {exc}"[:8000]
            plan_metadata["generation_stage"] = "FAILED"
            plan_metadata["generation_failed_at"] = _utcnow().isoformat()
            plan.hermes_response_json = plan_metadata
            db.add(plan)
            db.commit()
        logger.exception("Website Ads media plan generation failed", extra={"plan_id": int(plan_id)})
        raise
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.execute_media_plan",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
    max_retries=0,
    track_started=True,
    soft_time_limit=1200,
    time_limit=1260,
)
def execute_media_plan_task(self, *, plan_id: int):
    db = _db_session()
    try:
        claim = db.execute(
            update(WebsiteAdsMediaPlan)
            .where(
                WebsiteAdsMediaPlan.id == int(plan_id),
                WebsiteAdsMediaPlan.status == "EXECUTION_QUEUED",
            )
            .values(status="EXECUTING", error_message=None)
        )
        db.commit()
        if int(claim.rowcount or 0) != 1:
            current = db.get(WebsiteAdsMediaPlan, int(plan_id))
            state = str(current.status if current else "MISSING")
            logger.warning(
                "Website Ads media plan execution ignored because another worker owns it",
                extra={"plan_id": int(plan_id), "state": state},
            )
            return {"plan_id": int(plan_id), "state": state, "deduplicated": True}
        self.update_state(
            state="PROGRESS",
            meta={"plan_id": int(plan_id), "stage": "TIKTOK_PREFLIGHT"},
        )
        plan = db.get(WebsiteAdsMediaPlan, int(plan_id))
        if plan is None:
            raise RuntimeError("Media plan no longer exists")
        result = asyncio.run(execute_media_plan(db, plan, claimed=True))
        lock_reason = str(result.get("reason") or "")
        if lock_reason in {
            "EXECUTION_LOCK_UNAVAILABLE",
            "EXECUTION_LOCK_LOST",
        }:
            db.rollback()
            plan = db.get(WebsiteAdsMediaPlan, int(plan_id))
            if plan and str(plan.status or "").upper() == "EXECUTING":
                plan.status = (
                    "READY"
                    if lock_reason == "EXECUTION_LOCK_UNAVAILABLE"
                    else "FAILED"
                )
                plan.error_message = lock_reason
                db.add(plan)
                db.commit()
        return result
    except Exception as exc:
        db.rollback()
        plan = db.get(WebsiteAdsMediaPlan, int(plan_id))
        if plan and str(plan.status or "").upper() in {"EXECUTION_QUEUED", "EXECUTING"}:
            plan.status = "FAILED"
            plan.error_message = f"{type(exc).__name__}: {exc}"[:8000]
            db.add(plan)
            db.commit()
        logger.exception("Website Ads media plan execution failed", extra={"plan_id": int(plan_id)})
        raise
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.asset_library_cycle",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
)
def asset_library_cycle(self):
    db = _db_session()
    try:
        recovered_uploads = recover_stale_upload_fingerprints(db)
        pending_uploads = dispatch_pending_uploads(db)
        sync_result = asyncio.run(sync_asset_libraries(db))
        cache_result = dispatch_asset_media_cache(
            db,
            limit=int(settings.WEBSITE_ADS_MEDIA_CACHE_BATCH_SIZE),
        )
        dispatch_result = dispatch_asset_analysis(db, limit=8)
        return {
            "stale_uploads_recovered": recovered_uploads,
            "pending_uploads": pending_uploads,
            "sync": sync_result,
            "media_cache": cache_result,
            "analysis": dispatch_result,
        }
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.targeting_catalog_sync",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
    max_retries=2,
)
def targeting_catalog_sync(self, *, workspace_id: int | None = None, force: bool = False):
    db = _db_session()
    try:
        return asyncio.run(
            sync_all_targeting_catalogs(
                db,
                workspace_id=workspace_id,
                force=bool(force),
            )
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Website Ads targeting catalog sync failed")
        raise self.retry(exc=exc, countdown=min(300, 60 * (self.request.retries + 1)))
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.asset_analysis_dispatch",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
)
def asset_analysis_dispatch(self):
    db = _db_session()
    try:
        return dispatch_asset_analysis(db, limit=8)
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.asset_media_cache_dispatch",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
)
def asset_media_cache_dispatch(self):
    db = _db_session()
    try:
        stale_partials_removed = cleanup_stale_media_partials()
        recovered_uploads = recover_stale_upload_fingerprints(db)
        pending_uploads = dispatch_pending_uploads(db)
        result = dispatch_asset_media_cache(
            db,
            limit=int(settings.WEBSITE_ADS_MEDIA_CACHE_BATCH_SIZE),
        )
        result["stale_uploads_recovered"] = recovered_uploads
        result["pending_uploads"] = pending_uploads
        result["stale_partials_removed"] = stale_partials_removed
        return result
    finally:
        _close_session(db)


@celery_app.task(name="gmvmax.creative_asset_media_cache_dispatch", bind=True, queue="gmvmax")
def gmvmax_creative_asset_media_cache_dispatch(self):
    db = _db_session()
    try:
        return dispatch_gmvmax_creative_media_cache(
            db,
            limit=int(settings.GMVMAX_MEDIA_CACHE_BATCH_SIZE),
        )
    finally:
        _close_session(db)


@celery_app.task(
    name="website_ads.asset_expansion_cycle",
    bind=True,
    queue=settings.WEBSITE_ADS_TASK_QUEUE,
    max_retries=2,
)
def asset_expansion_cycle(self):
    db = _db_session()
    try:
        return asyncio.run(run_website_ads_asset_expansion_cycle(db))
    except Exception as exc:
        db.rollback()
        logger.exception("Website Ads automatic creative expansion cycle failed")
        raise self.retry(exc=exc, countdown=min(300, 60 * (self.request.retries + 1)))
    finally:
        _close_session(db)


@celery_app.task(name="openai_whisper.website_ads_asset_analysis", bind=True, queue=WHISPER_TASK_QUEUE)
def analyze_asset_task(self, *, asset_id: int):
    db = _db_session()
    try:
        row = asyncio.run(run_asset_analysis_pipeline(db, int(asset_id)))
        return {"asset_id": int(row.id), "status": row.analysis_status}
    except Exception as exc:
        db.rollback()
        row = db.get(WebsiteAdsCreativeAsset, int(asset_id))
        if row:
            attempts = max(1, int(row.analysis_attempts or 1))
            delay_minutes = min(360, 5 * (2 ** min(attempts - 1, 6)))
            row.analysis_status = "FAILED"
            row.analysis_error = f"{type(exc).__name__}: {exc}"[:2000]
            row.analysis_next_retry_at = _utcnow() + timedelta(minutes=delay_minutes)
            db.add(row)
            db.commit()
        logger.exception("Website Ads asset analysis failed", extra={"asset_id": int(asset_id)})
        raise
    finally:
        _close_session(db)


@celery_app.task(
    name="openai_whisper.website_ads_asset_media_cache",
    bind=True,
    queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
    max_retries=2,
)
def cache_asset_media_task(self, *, asset_id: int):
    db = _db_session()
    try:
        return asyncio.run(ensure_asset_media_cache(db, int(asset_id)))
    except Exception as exc:
        db.rollback()
        logger.exception("Website Ads asset media cache failed", extra={"asset_id": int(asset_id)})
        raise self.retry(exc=exc, countdown=min(300, 60 * (self.request.retries + 1)))
    finally:
        _close_session(db)


@celery_app.task(
    name="gmvmax.creative_asset_media_cache",
    bind=True,
    queue=settings.WEBSITE_ADS_MEDIA_TASK_QUEUE,
    max_retries=1,
    track_started=True,
    soft_time_limit=900,
    time_limit=960,
)
def cache_gmvmax_creative_media_task(self, *, asset_id: int):
    db = _db_session()
    try:
        return asyncio.run(cache_creative_asset_media(db, int(asset_id)))
    except Exception as exc:
        db.rollback()
        try:
            mark_creative_media_queue_error(db, int(asset_id), exc)
        except Exception:
            db.rollback()
        logger.exception("GMV Max creative media cache failed", extra={"asset_id": int(asset_id)})
        raise self.retry(exc=exc, countdown=60)
    finally:
        _close_session(db)
