"""Celery task layer orchestrating GMV Max syncs and actions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Awaitable, Callable, Optional, TypeVar

from fastapi import HTTPException
from sqlalchemy import delete, func, select, or_, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.gmv_restructured import GmvOverviewSnapshot
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.gmvmax_sync_state import GmvCreative10MinSyncState
from app.data.models.scheduling import ScheduleRun
from app.data.models.ttb_entities import TTBAdvertiserStoreLink, TTBBindingConfig
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.gmvmax.services.campaign_cleanup import cleanup_campaign_tables
from app.gmvmax.services.create_intent_recovery import (
    recover_incomplete_gmvmax_create_intents,
)
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.ttb_balances import select_latest_balance, sync_advertiser_balance
from app.services.gmvmax_heating import run_creative_heating_cycle
from app.services.gmvmax_creative_guard import run_creative_guard_cycle
from app.services.gmvmax_hermes_daily_report import run_hermes_daily_report_cycle
from app.services.gmvmax_hermes_advisor import run_hermes_advisor_cycle
from app.services.gmvmax_smart_guard import run_smart_guard_cycle
from app.services.gmvmax_creative_metrics import (
    sync_creative_metrics_10min_for_campaign,
)
from app.services.gmvmax_lifecycle import _derive_campaign_lifecycle
from app.services.ttb_api import (
    TTBApiError,
    TTBBusinessError,
    TTBHttpError,
    TTBRateLimitBudgetError,
    ttb_retry_countdown,
)
from app.gmvmax.services.sync_service import GmvMaxSyncService
from app.gmvmax.services.sync_execution_lock import (
    GmvMaxAccountSyncFenceLost,
    acquire_account_sync_fence,
    acquire_creative_10min_sync_fence,
    build_account_sync_lock,
    build_creative_10min_sync_lock,
    release_account_sync_fence,
    release_sync_fence,
)
from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxCampaignPauseIntent,
    GMVMaxTaskOwnership,
    GMVMaxSyncSchedule,
    acquire_guard_action_lease,
    claim_campaign_pause_intent,
    clear_manual_override,
    complete_campaign_pause_intent,
    defer_campaign_pause_intent,
    due_campaign_pause_intent_ids,
    new_owned_task_id,
    record_task_ownership,
    release_guard_action_lease,
    recover_orphaned_pending_pause_override_intent_ids,
    remove_task_ownership,
    utcnow_naive,
)
from app.services.ttb_gmvmax import (
    fetch_and_cache_campaign_detail,
    sync_gmvmax_campaigns,
)
from app.services.redis_locks import RedisDistributedLock
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxStoreAdUsageCheckRequest,
    fetch_all_occupied_custom_shop_ads,
)

logger = logging.getLogger("gmv.tasks.gmvmax")

GMVMAX_OVERVIEW_SNAPSHOT_TTL_DAYS = int(
    getattr(settings, "GMVMAX_OVERVIEW_SNAPSHOT_TTL_DAYS", 90)
)
GMVMAX_CAMPAIGN_METRICS_HOURLY_TTL_DAYS = int(
    getattr(settings, "GMVMAX_CAMPAIGN_METRICS_HOURLY_TTL_DAYS", 90)
)
GMVMAX_CAMPAIGN_METRICS_DAILY_TTL_DAYS = int(
    getattr(settings, "GMVMAX_CAMPAIGN_METRICS_DAILY_TTL_DAYS", 730)
)
GMVMAX_CAMPAIGN_SNAPSHOT_TTL_DAYS = int(
    getattr(settings, "GMVMAX_CAMPAIGN_SNAPSHOT_TTL_DAYS", 90)
)
GMVMAX_CREATIVE_10MIN_TTL_DAYS = int(
    getattr(settings, "GMVMAX_CREATIVE_10MIN_TTL_DAYS", 90)
)

BALANCE_FETCH_MIN_INTERVAL_SECONDS = int(
    getattr(settings, "TTB_BALANCE_MIN_FETCH_INTERVAL_SECONDS", 300)
)


T = TypeVar("T")

def _run_with_client(db: Session, auth_id: int, fn: Callable[[Any], Awaitable[T]]) -> T:
    async def _runner() -> T:
        client = build_ttb_gmvmax_client(
            db,
            auth_id=auth_id,
            timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
        )
        try:
            return await fn(client)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("gmvmax client close failed", exc_info=True)

    return asyncio.run(_runner())


def _db_session() -> Session:
    gen = get_db()
    sess = next(gen)
    setattr(sess, "__gen__", gen)
    return sess


def _close_session(sess: Session) -> None:
    gen = getattr(sess, "__gen__", None)
    try:
        sess.close()
    finally:
        try:
            if gen:
                next(gen)
        except StopIteration:
            pass


def _pause_intent_error_is_retryable(exc: HTTPException) -> bool:
    """Retry lease, quota, and transport failures; fail closed otherwise."""

    if int(exc.status_code) >= 500:
        return True
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("code") or "")
    if code in {"GMVMAX_MUTATION_INFLIGHT", "GMVMAX_MUTATION_FENCE_LOST"}:
        return True
    message = str(detail.get("message") or "").lower()
    payload = detail.get("details")
    payload_text = json.dumps(payload, ensure_ascii=False, default=str).lower()
    return any(
        marker in f"{message} {payload_text}"
        for marker in ("too frequent", "rate limit", "rate_limit", "quota", "timeout")
    )


def _pause_intent_error_is_mutation_conflict(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return str(detail.get("code") or "") in {
        "GMVMAX_MUTATION_INFLIGHT",
        "GMVMAX_MUTATION_FENCE_LOST",
    }


def _pause_intent_retry_countdown(exc: HTTPException) -> int:
    """Honor a parsed upstream cooldown when it survived the router boundary."""

    detail = exc.detail if isinstance(exc.detail, dict) else {}
    if str(detail.get("code") or "") in {
        "GMVMAX_MUTATION_INFLIGHT",
        "GMVMAX_MUTATION_FENCE_LOST",
    }:
        # Manual pauses use their own worker; retry almost immediately after
        # the one in-flight same-account request releases its fence.
        return 2
    payload = detail.get("details")
    nested_payload = payload.get("payload") if isinstance(payload, dict) else None
    retry_source = nested_payload if isinstance(nested_payload, dict) else {}
    try:
        return ttb_retry_countdown(
            TTBRateLimitBudgetError(
                str(detail.get("message") or "GMV Max pause retry"),
                payload=retry_source,
            ),
            default_seconds=30,
            maximum_seconds=10 * 60,
        )
    except Exception:  # noqa: BLE001
        return 30


def _pause_intent_actor(value: str | None) -> SimpleNamespace:
    """Router audit code only needs a stable operator label for async work."""

    label = str(value or "GMV Max pause intent")[:191]
    return SimpleNamespace(id=None, email=label, username=label, display_name=label)


def _finish_schedule_run(
    db: Session,
    *,
    idempotency_key: str | None,
    status: str,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    if not idempotency_key:
        return
    run = (
        db.query(ScheduleRun)
        .filter(ScheduleRun.idempotency_key == str(idempotency_key))
        .order_by(ScheduleRun.id.desc())
        .first()
    )
    if run is None:
        return
    run.status = status
    stats = dict(run.stats_json or {})
    if result is not None:
        stats["result"] = dict(result)
    run.stats_json = stats
    if error is not None:
        run.error_code = error.__class__.__name__
        run.error_message = str(error)[:512]
    else:
        run.error_code = None
        run.error_message = None
    db.add(run)


def _advertiser_report_day(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> date:
    tz_name = _advertiser_timezone_name(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    if not tz_name:
        return date.today()
    try:
        return datetime.now(ZoneInfo(str(tz_name))).date()
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "gmvmax task ignored invalid advertiser timezone",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "timezone_name": tz_name,
            },
        )
        return date.today()


def _advertiser_timezone_name(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    row = db.execute(
        text(
            """
            select display_timezone, timezone
            from ttb_advertisers
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
            order by last_seen_at desc
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
        },
    ).mappings().first()
    tz_name = (row or {}).get("display_timezone") or (row or {}).get("timezone")
    return str(tz_name) if tz_name else None


def _parse_date_param(value: str | date | datetime | None, fallback: date) -> date:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_aware_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _campaign_start_at_utc(campaign: Any) -> datetime | None:
    for attr in (
        "schedule_start_time_utc",
        "schedule_start_time",
        "create_time_utc",
        "created_at",
        "updated_at",
    ):
        value = getattr(campaign, attr, None)
        parsed = _as_aware_utc(value)
        if parsed:
            return parsed
    raw_json = getattr(campaign, "raw_json", None)
    if isinstance(raw_json, dict):
        for key in ("schedule_start_time", "create_time", "created_time", "create_time_utc"):
            parsed = _as_aware_utc(raw_json.get(key))
            if parsed:
                return parsed
    return None


def _campaign_sync_window(
    db: Session,
    campaign: Any,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    lookback_days: int = 1,
) -> tuple[date, date]:
    end_day = _advertiser_report_day(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
    )
    start_day = end_day - timedelta(days=max(0, int(lookback_days)))
    start_at = _campaign_start_at_utc(campaign)
    if start_at:
        tz_name = _advertiser_timezone_name(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
        )
        campaign_start_day = start_at.date()
        if tz_name:
            try:
                campaign_start_day = start_at.astimezone(ZoneInfo(str(tz_name))).date()
            except (ZoneInfoNotFoundError, ValueError):
                campaign_start_day = start_at.date()
        start_day = max(start_day, campaign_start_day)
    if start_day > end_day:
        start_day = end_day
    return start_day, end_day


def _iter_sync_scopes(db: Session) -> list[tuple[int, int, str]]:
    stmt = (
        select(
            TTBAdvertiserStoreLink.workspace_id,
            TTBAdvertiserStoreLink.auth_id,
            TTBAdvertiserStoreLink.advertiser_id,
        )
        .where(TTBAdvertiserStoreLink.advertiser_id.is_not(None))
        .distinct()
    )
    rows = []
    for workspace_id, auth_id, advertiser_id in db.execute(stmt):
        if not auth_id or not advertiser_id:
            continue
        rows.append((int(workspace_id), int(auth_id), str(advertiser_id)))
    return rows


def _mysql_error_code(exc: Exception) -> Any:
    return getattr(getattr(exc, "orig", None), "args", [None])[0]


def _find_catalog_campaign_scope(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str | None = None,
    store_id: str | None = None,
) -> Any | None:
    row = db.execute(
        text(
            """
            select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   campaign_name, operation_status, secondary_status,
                   shopping_ads_type, detail_raw_json,
                   schedule_start_time_utc, create_time_utc, created_at, updated_at
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
              and (:advertiser_id is null or advertiser_id=:advertiser_id)
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "campaign_id": str(campaign_id),
            "advertiser_id": str(advertiser_id) if advertiser_id else None,
        },
    ).mappings().first()
    if not row:
        return None
    raw_json = row.get("detail_raw_json") or {}
    return SimpleNamespace(
        workspace_id=int(row["workspace_id"]),
        auth_id=int(row["auth_id"]),
        advertiser_id=str(row.get("advertiser_id") or advertiser_id or ""),
        store_id=str(row.get("store_id") or store_id or ""),
        campaign_id=str(row.get("campaign_id") or campaign_id),
        name=row.get("campaign_name"),
        raw_json=raw_json,
        shopping_ads_type=row.get("shopping_ads_type") or "PRODUCT",
        operation_status=row.get("operation_status"),
        secondary_status=row.get("secondary_status"),
        schedule_start_time_utc=row.get("schedule_start_time_utc"),
        create_time_utc=row.get("create_time_utc"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        provider="tiktok-business",
    )


def _iter_active_catalog_campaign_scopes(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None = None,
    limit: int | None = None,
) -> list[Any]:
    now = utcnow_naive()
    scope_filters = (
        GmvCreative10MinSyncState.workspace_id
        == GmvmaxProductCampaignCatalog.workspace_id,
        GmvCreative10MinSyncState.auth_id
        == GmvmaxProductCampaignCatalog.auth_id,
        GmvCreative10MinSyncState.advertiser_id
        == GmvmaxProductCampaignCatalog.advertiser_id,
        GmvCreative10MinSyncState.campaign_id
        == GmvmaxProductCampaignCatalog.campaign_id,
    )
    last_attempt_at = (
        select(GmvCreative10MinSyncState.last_attempt_at)
        .where(*scope_filters)
        .correlate(GmvmaxProductCampaignCatalog)
        .scalar_subquery()
    )
    next_attempt_at = (
        select(GmvCreative10MinSyncState.next_attempt_at)
        .where(*scope_filters)
        .correlate(GmvmaxProductCampaignCatalog)
        .scalar_subquery()
    )
    stmt = (
        select(GmvmaxProductCampaignCatalog)
        .where(
            GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id),
            GmvmaxProductCampaignCatalog.auth_id == int(auth_id),
            func.upper(func.coalesce(GmvmaxProductCampaignCatalog.operation_status, "")).in_(
                ("ENABLE", "ENABLED", "ACTIVE")
            ),
            or_(next_attempt_at.is_(None), next_attempt_at <= now),
        )
        .order_by(
            func.coalesce(
                last_attempt_at,
                GmvmaxProductCampaignCatalog.created_at,
            ).asc(),
            GmvmaxProductCampaignCatalog.id.asc(),
        )
    )
    if advertiser_id:
        stmt = stmt.where(
            GmvmaxProductCampaignCatalog.advertiser_id == str(advertiser_id)
        )
    if limit is not None and int(limit) > 0:
        stmt = stmt.limit(int(limit))
    if db.get_bind().dialect.name == "mysql":
        stmt = stmt.with_for_update(skip_locked=True)

    rows = list(db.scalars(stmt).all())
    if not rows:
        return []

    campaign_ids = [str(row.campaign_id) for row in rows]
    states = {
        (str(state.advertiser_id), str(state.campaign_id)): state
        for state in db.scalars(
            select(GmvCreative10MinSyncState).where(
                GmvCreative10MinSyncState.workspace_id == int(workspace_id),
                GmvCreative10MinSyncState.auth_id == int(auth_id),
                GmvCreative10MinSyncState.advertiser_id.in_(
                    [str(row.advertiser_id) for row in rows]
                ),
                GmvCreative10MinSyncState.campaign_id.in_(campaign_ids),
            )
        ).all()
    }
    claim_until = now + timedelta(
        minutes=max(
            1,
            int(
                getattr(
                    settings,
                    "GMVMAX_CREATIVE_10MIN_CLAIM_STALE_MINUTES",
                    30,
                )
            ),
        )
    )
    attempt_tokens: dict[str, str] = {}
    for row in rows:
        campaign_id = str(row.campaign_id)
        state_key = (str(row.advertiser_id), campaign_id)
        state = states.get(state_key)
        if state is None:
            state = GmvCreative10MinSyncState(
                workspace_id=int(row.workspace_id),
                auth_id=int(row.auth_id),
                advertiser_id=str(row.advertiser_id),
                campaign_id=campaign_id,
                store_id=str(row.store_id) if row.store_id else None,
            )
            db.add(state)
            states[state_key] = state
        token = str(uuid4())
        state.store_id = str(row.store_id) if row.store_id else None
        state.last_attempt_at = now
        state.next_attempt_at = claim_until
        state.last_status = "QUEUED"
        state.last_error = None
        state.attempt_count = int(state.attempt_count or 0) + 1
        state.attempt_token = token
        db.add(state)
        attempt_tokens[f"{row.advertiser_id}\x00{campaign_id}"] = token

    # This is the durable claim: commit before publishing so zero-row workers,
    # failed workers, or a failed broker publish cannot keep the same campaign
    # at the head of every bounded sweep.
    db.commit()
    return [
        SimpleNamespace(
            workspace_id=int(row.workspace_id),
            auth_id=int(row.auth_id),
            advertiser_id=str(row.advertiser_id or ""),
            store_id=str(row.store_id or ""),
            campaign_id=str(row.campaign_id or ""),
            name=row.campaign_name,
            raw_json=row.detail_raw_json or {},
            shopping_ads_type=row.shopping_ads_type or "PRODUCT",
            operation_status=row.operation_status,
            secondary_status=row.secondary_status,
            schedule_start_time_utc=row.schedule_start_time_utc,
            create_time_utc=row.create_time_utc,
            created_at=row.created_at,
            updated_at=row.updated_at,
            provider="tiktok-business",
            sync_attempt_token=attempt_tokens[
                f"{row.advertiser_id}\x00{row.campaign_id}"
            ],
        )
        for row in rows
    ]


def _creative_10min_state(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
) -> GmvCreative10MinSyncState | None:
    return db.scalar(
        select(GmvCreative10MinSyncState).where(
            GmvCreative10MinSyncState.workspace_id == int(workspace_id),
            GmvCreative10MinSyncState.auth_id == int(auth_id),
            GmvCreative10MinSyncState.advertiser_id == str(advertiser_id),
            GmvCreative10MinSyncState.campaign_id == str(campaign_id),
        )
    )


def _begin_creative_10min_attempt(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str | None,
) -> str:
    now = utcnow_naive()
    state = _creative_10min_state(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
    )
    if state is None:
        state = GmvCreative10MinSyncState(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
        )
    token = str(uuid4())
    state.store_id = str(store_id) if store_id else None
    state.last_attempt_at = now
    state.next_attempt_at = now + timedelta(
        minutes=max(
            1,
            int(
                getattr(
                    settings,
                    "GMVMAX_CREATIVE_10MIN_CLAIM_STALE_MINUTES",
                    30,
                )
            ),
        )
    )
    state.last_status = "PROCESSING"
    state.last_error = None
    state.attempt_count = int(state.attempt_count or 0) + 1
    state.attempt_token = token
    state.updated_at = now
    db.add(state)
    db.commit()
    return token


def _record_creative_10min_result(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str | None,
    attempt_token: str | None,
    success: bool,
    rows: int | None = None,
    error: BaseException | str | None = None,
) -> bool:
    now = utcnow_naive()
    state = _creative_10min_state(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
    )
    if state is None:
        state = GmvCreative10MinSyncState(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            last_attempt_at=now,
            attempt_count=1,
            attempt_token=str(attempt_token) if attempt_token else str(uuid4()),
        )
    elif attempt_token and state.attempt_token and str(state.attempt_token) != str(
        attempt_token
    ):
        # A stale worker must not overwrite a newer sweep claim.
        return False

    state.store_id = str(store_id) if store_id else state.store_id
    state.updated_at = now
    if success:
        row_count = max(0, int(rows or 0))
        state.last_status = "ZERO_ROWS" if row_count == 0 else "SUCCESS"
        state.last_result_rows = row_count
        state.last_success_at = now
        state.last_error = None
        state.consecutive_failures = 0
        state.next_attempt_at = now + timedelta(
            minutes=max(
                1,
                int(
                    getattr(
                        settings,
                        "GMVMAX_CREATIVE_10MIN_SUCCESS_INTERVAL_MINUTES",
                        10,
                    )
                ),
            )
        )
    else:
        failures = int(state.consecutive_failures or 0) + 1
        base_minutes = max(
            1,
            int(
                getattr(
                    settings,
                    "GMVMAX_CREATIVE_10MIN_RETRY_BASE_MINUTES",
                    10,
                )
            ),
        )
        max_minutes = max(
            base_minutes,
            int(
                getattr(
                    settings,
                    "GMVMAX_CREATIVE_10MIN_RETRY_MAX_MINUTES",
                    120,
                )
            ),
        )
        delay_minutes = min(
            max_minutes,
            base_minutes * (2 ** min(max(0, failures - 1), 8)),
        )
        state.last_status = "ERROR"
        state.last_error_at = now
        state.last_error = str(error or "unknown error")[:4000]
        state.last_result_rows = None
        state.consecutive_failures = failures
        state.next_attempt_at = now + timedelta(minutes=delay_minutes)
    db.add(state)
    return True


@celery_app.task(
    name="gmvmax.cleanup_overview_snapshots",
    queue="gmvmax",
)
def cleanup_overview_snapshots() -> int:
    """删除超出 TTL 的 GMV Max overview 快照。"""

    cutoff = date.today() - timedelta(days=GMVMAX_OVERVIEW_SNAPSHOT_TTL_DAYS)
    db = _db_session()
    try:
        deleted = (
            db.query(GmvOverviewSnapshot)
            .filter(GmvOverviewSnapshot.end_date < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        logger.info(
            "gmvmax overview snapshots cleanup done",
            extra={"cutoff": cutoff.isoformat(), "deleted": int(deleted)},
        )
        return int(deleted)
    finally:
        _close_session(db)


@celery_app.task(
    name="gmvmax.cleanup_campaign_tables",
    queue="gmvmax",
)
def cleanup_campaign_tables_task() -> dict:
    """Scheduled cleanup for GMV Max campaign metrics and snapshots."""

    db = _db_session()
    try:
        result = cleanup_campaign_tables(
            db,
            hourly_retention_days=GMVMAX_CAMPAIGN_METRICS_HOURLY_TTL_DAYS,
            daily_retention_days=GMVMAX_CAMPAIGN_METRICS_DAILY_TTL_DAYS,
            snapshot_retention_days=GMVMAX_CAMPAIGN_SNAPSHOT_TTL_DAYS,
            creative_10min_retention_days=GMVMAX_CREATIVE_10MIN_TTL_DAYS,
        )
        return result
    finally:
        _close_session(db)


@celery_app.task(
    name="gmvmax.reconcile_create_intents",
    queue="gmvmax",
)
def reconcile_create_intents_task() -> dict[str, Any]:
    """Manually runnable fail-closed recovery for interrupted campaign creates."""

    db = _db_session()
    try:
        return asyncio.run(recover_incomplete_gmvmax_create_intents(db))
    finally:
        _close_session(db)


@celery_app.task(
    name="gmvmax.execute_campaign_pause_intent",
    bind=True,
    queue="gmvmax_control",
    max_retries=None,
)
def execute_campaign_pause_intent_task(self, *, intent_id: str) -> dict[str, Any]:
    """Execute a persisted user pause after account sync ownership is free.

    The intent is claimed before upstream I/O, then the ordinary router path
    performs the official status mutation under the same mutation lease used
    by UI actions and automated Guard work.
    """

    owner_token = f"{self.request.id or 'pause-intent'}:{uuid4()}"
    db = _db_session()
    context = None
    intent: GMVMaxCampaignPauseIntent | None = None
    try:
        intent = claim_campaign_pause_intent(
            db,
            intent_id=str(intent_id),
            owner_token=owner_token,
        )
        if intent is None:
            db.commit()
            return {"status": "stale_or_not_due", "intent_id": str(intent_id)}
        intent_data = {
            "id": str(intent.id),
            "workspace_id": int(intent.workspace_id),
            "auth_id": int(intent.auth_id),
            "advertiser_id": str(intent.advertiser_id),
            "store_id": str(intent.store_id) if intent.store_id else None,
            "campaign_id": str(intent.campaign_id),
            "actor": intent.actor,
            "reason": intent.reason,
            "attempt_count": int(intent.attempt_count or 0),
        }
        db.commit()

        # Import here to avoid a task/router import cycle at worker startup.
        from app.features.tenants.ttb.gmv_max.router_provider import (
            _build_route_context,
            apply_gmvmax_campaign_action_provider,
        )

        context = _build_route_context(
            intent_data["workspace_id"],
            "tiktok-business",
            intent_data["auth_id"],
            db,
            allow_missing_advertiser=False,
        )
        # Bindings can be deliberately changed by an operator.  Never apply a
        # queued pause using a newly selected advertiser/store scope.
        if (
            str(context.advertiser_id or "") != intent_data["advertiser_id"]
            or (str(context.store_id) if context.store_id else None)
            != intent_data["store_id"]
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GMVMAX_PAUSE_INTENT_SCOPE_CHANGED",
                    "message": "The account binding changed before the queued pause ran.",
                },
            )

        async def _apply_and_close() -> Any:
            try:
                return await apply_gmvmax_campaign_action_provider(
                    workspace_id=intent_data["workspace_id"],
                    provider="tiktok-business",
                    auth_id=intent_data["auth_id"],
                    campaign_id=intent_data["campaign_id"],
                    payload={
                        "type": "pause",
                        "reason": intent_data["reason"],
                        "_system_pause_intent_id": intent_data["id"],
                    },
                    advertiser_id=intent_data["advertiser_id"],
                    me=_pause_intent_actor(intent_data["actor"]),
                    context=context,
                )
            finally:
                await context.client.aclose()

        action_result = asyncio.run(_apply_and_close())
        # The HTTP client is bound to the event loop above and was closed
        # there. Avoid trying to close it again from a new, closed loop.
        context = None
        if str(getattr(action_result, "status", "")).lower() != "success":
            # The router may have safely persisted the same intent again when
            # the account lock was still held. That is not completion: keep
            # this intent active and retry it on the dedicated control worker.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GMVMAX_MUTATION_INFLIGHT",
                    "message": "The manual pause still awaits the current same-account mutation.",
                },
            )
        if not complete_campaign_pause_intent(
            db,
            intent_id=intent_data["id"],
            owner_token=owner_token,
            status="SUCCEEDED",
        ):
            db.commit()
            return {"status": "superseded", "intent_id": intent_data["id"]}
        db.commit()
        return {"status": "succeeded", "intent_id": intent_data["id"]}
    except HTTPException as exc:
        db.rollback()
        if intent is None:
            raise
        mutation_conflict = _pause_intent_error_is_mutation_conflict(exc)
        if _pause_intent_error_is_retryable(exc) and (
            mutation_conflict or int(intent_data["attempt_count"]) < 24
        ):
            countdown = _pause_intent_retry_countdown(exc)
            if defer_campaign_pause_intent(
                db,
                intent_id=intent_data["id"],
                owner_token=owner_token,
                countdown_seconds=countdown,
                error=_extract_http_error_message(exc),
            ):
                db.commit()
            else:
                db.rollback()
                return {"status": "superseded", "intent_id": intent_data["id"]}
            raise self.retry(exc=exc, countdown=countdown)

        # A scope change, missing campaign, or validated upstream rejection is
        # terminal.  Release the temporary automatic-action hold and retain a
        # durable audit row instead of retrying a potentially wrong mutation.
        complete_campaign_pause_intent(
            db,
            intent_id=str(intent_id),
            owner_token=owner_token,
            status="FAILED",
            error=_extract_http_error_message(exc),
        )
        if intent is not None:
            clear_manual_override(
                db,
                workspace_id=int(intent.workspace_id),
                auth_id=int(intent.auth_id),
                advertiser_id=str(intent.advertiser_id),
                store_id=str(intent.store_id) if intent.store_id else None,
                campaign_id=str(intent.campaign_id),
            )
        db.commit()
        logger.warning(
            "gmvmax queued pause failed permanently",
            extra={"pause_intent_id": str(intent_id), "error": _extract_http_error_message(exc)},
        )
        return {"status": "failed", "intent_id": str(intent_id)}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if intent is None:
            raise
        if int(intent_data["attempt_count"]) < 24 and defer_campaign_pause_intent(
            db,
            intent_id=intent_data["id"],
            owner_token=owner_token,
            countdown_seconds=30,
            error=str(exc),
        ):
            db.commit()
            raise self.retry(exc=exc, countdown=30)
        db.rollback()
        complete_campaign_pause_intent(
            db,
            intent_id=str(intent_id),
            owner_token=owner_token,
            status="FAILED",
            error=str(exc),
        )
        if intent is not None:
            clear_manual_override(
                db,
                workspace_id=int(intent.workspace_id),
                auth_id=int(intent.auth_id),
                advertiser_id=str(intent.advertiser_id),
                store_id=str(intent.store_id) if intent.store_id else None,
                campaign_id=str(intent.campaign_id),
            )
        db.commit()
        raise
    finally:
        if context is not None:
            try:
                asyncio.run(context.client.aclose())
            except Exception:  # noqa: BLE001
                logger.warning("gmvmax pause intent client close failed", exc_info=True)
        _close_session(db)


def _extract_http_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or detail)
    return str(detail)


@celery_app.task(
    name="gmvmax.recover_campaign_pause_intents",
    queue="gmvmax",
)
def recover_campaign_pause_intents_task() -> dict[str, Any]:
    """Re-enqueue due/abandoned pause intents after broker or worker loss."""

    db = _db_session()
    try:
        intent_ids = due_campaign_pause_intent_ids(db, limit=100)
        intent_ids.extend(
            recover_orphaned_pending_pause_override_intent_ids(db, limit=100)
        )
        db.commit()
    finally:
        _close_session(db)

    queued = 0
    for intent_id in dict.fromkeys(intent_ids):
        try:
            celery_app.send_task(
                "gmvmax.execute_campaign_pause_intent",
                kwargs={"intent_id": intent_id},
                queue="gmvmax_control",
            )
            queued += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "gmvmax pause intent recovery enqueue failed",
                extra={"pause_intent_id": intent_id},
            )
    return {"due": len(intent_ids), "queued": queued}


async def _run_smart_guard_with_create_recovery(
    db: Session,
) -> dict[str, Any]:
    # Smart Guard is already scheduled every minute on the isolated gmvmax
    # queue.  Reuse that cadence and its existing global durable lease instead
    # of adding a shared Celery Beat schedule.
    recovery = await recover_incomplete_gmvmax_create_intents(db)
    result = await run_smart_guard_cycle(db)
    result["create_intent_recovery"] = recovery
    return result


@celery_app.task(
    name="gmvmax.smart_guard_cycle",
    queue="gmvmax",
)
def smart_guard_cycle_task() -> dict[str, Any]:
    """Evaluate campaign-level GMV Max guardrails and apply pause/resume actions."""

    owner_token = str(uuid4())
    lock = RedisDistributedLock(
        key="gmvmax:guard-actions:cycle",
        owner_token=owner_token,
        ttl_seconds=300,
        heartbeat_interval=20,
    )
    # Smart and creative guards deliberately share a lock so remote mutations
    # stay serialized. Both are scheduled together, so a near-zero timeout
    # makes the smart guard lose the race and skip whole cycles.
    if not lock.acquire(timeout=30.0, retry_interval=0.2):
        logger.info("gmvmax smart guard cycle skipped: previous cycle still running")
        return {
            "status": "skipped",
            "reason": "inflight",
            "strategies": 0,
            "checked": 0,
            "errors": 0,
        }

    db = _db_session()
    fencing_token = acquire_guard_action_lease(
        db,
        lease_name="gmvmax:guard-actions:cycle",
        owner_token=owner_token,
    )
    if fencing_token is None:
        db.rollback()
        _close_session(db)
        lock.release()
        logger.info("gmvmax smart guard cycle skipped: durable lease held")
        return {
            "status": "skipped",
            "reason": "durable_inflight",
            "strategies": 0,
            "checked": 0,
            "errors": 0,
        }
    db.commit()
    db.info["gmvmax_guard_owner_token"] = owner_token
    db.info["gmvmax_guard_fencing_token"] = fencing_token
    db.info["gmvmax_guard_redis_lock"] = lock
    try:
        result = asyncio.run(_run_smart_guard_with_create_recovery(db))
        result["fencing_token"] = fencing_token
        db.commit()
        return result
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("gmvmax smart guard cycle failed")
        raise
    finally:
        try:
            db.rollback()
            release_guard_action_lease(
                db,
                lease_name="gmvmax:guard-actions:cycle",
                owner_token=owner_token,
                fencing_token=fencing_token,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("gmvmax smart guard durable lease release failed")
        _close_session(db)
        lock.release()


@celery_app.task(
    name="gmvmax.creative_guard_cycle",
    queue="gmvmax",
)
def creative_guard_cycle_task() -> dict[str, Any]:
    """Evaluate creative-level GMV Max guardrails and remove poor creatives."""

    owner_token = str(uuid4())
    lock = RedisDistributedLock(
        key="gmvmax:guard-actions:cycle",
        owner_token=owner_token,
        ttl_seconds=300,
        heartbeat_interval=20,
    )
    if not lock.acquire(timeout=30.0, retry_interval=0.2):
        logger.info("gmvmax creative guard cycle skipped: another guard action is running")
        return {
            "status": "skipped",
            "reason": "inflight",
            "campaigns": 0,
            "checked_creatives": 0,
            "errors": 0,
        }

    db = _db_session()
    fencing_token = acquire_guard_action_lease(
        db,
        lease_name="gmvmax:guard-actions:cycle",
        owner_token=owner_token,
    )
    if fencing_token is None:
        db.rollback()
        _close_session(db)
        lock.release()
        logger.info("gmvmax creative guard cycle skipped: durable lease held")
        return {
            "status": "skipped",
            "reason": "durable_inflight",
            "campaigns": 0,
            "checked_creatives": 0,
            "errors": 0,
        }
    db.commit()
    db.info["gmvmax_guard_owner_token"] = owner_token
    db.info["gmvmax_guard_fencing_token"] = fencing_token
    db.info["gmvmax_guard_redis_lock"] = lock
    try:
        result = asyncio.run(run_creative_guard_cycle(db))
        result["fencing_token"] = fencing_token
        db.commit()
        return result
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("gmvmax creative guard cycle failed")
        raise
    finally:
        try:
            db.rollback()
            release_guard_action_lease(
                db,
                lease_name="gmvmax:guard-actions:cycle",
                owner_token=owner_token,
                fencing_token=fencing_token,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("gmvmax creative guard durable lease release failed")
        _close_session(db)
        lock.release()


@celery_app.task(
    name="gmvmax.hermes_advisor_cycle",
    queue="gmvmax",
)
def hermes_advisor_cycle_task() -> dict[str, Any]:
    """Generate and safely apply Hermes GMV Max strategy recommendations."""

    db = _db_session()
    try:
        result = run_hermes_advisor_cycle(db)
        db.commit()
        return result
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("gmvmax hermes advisor cycle failed")
        raise
    finally:
        _close_session(db)


@celery_app.task(
    name="gmvmax.hermes_daily_report",
    queue="gmvmax",
)
def hermes_daily_report_task(report_date: str | None = None, force: bool = False) -> dict[str, Any]:
    """Build a daily GMV Max report through Hermes/ChatGPT and persist it."""

    parsed_date = None
    if report_date:
        parsed_date = date.fromisoformat(str(report_date))
    db = _db_session()
    try:
        result = asyncio.run(run_hermes_daily_report_cycle(db, report_date=parsed_date, force=bool(force)))
        db.commit()
        return result
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("gmvmax hermes daily report failed")
        raise
    finally:
        _close_session(db)


@celery_app.task(
    bind=True,
    name="gmvmax.reconcile_campaign_status",
    queue="gmvmax",
)
def reconcile_campaign_status(self, batch_size: int = 500) -> dict:
    """Repair lifecycle_status/is_deleted drift for GMV campaigns."""

    db = _db_session()
    scanned = 0
    updated = 0
    last_id = 0

    try:
        while True:
            rows = (
                db.execute(
                    select(TTBGmvMaxCampaign)
                    .where(TTBGmvMaxCampaign.id > last_id)
                    .order_by(TTBGmvMaxCampaign.id)
                    .limit(int(batch_size))
                )
                .scalars()
                .all()
            )

            if not rows:
                break

            last_id = getattr(rows[-1], "id", last_id)

            for row in rows:
                scanned += 1
                expected_status, expected_deleted = _derive_campaign_lifecycle(
                    row.operation_status, row.secondary_status
                )

                if (
                    row.lifecycle_status != expected_status
                    or bool(row.is_deleted) != bool(expected_deleted)
                ):
                    logger.warning(
                        "gmvmax lifecycle drift repaired",
                        extra={
                            "campaign_pk": getattr(row, "id", None),
                            "campaign_id": getattr(row, "campaign_id", None),
                            "old": {
                                "lifecycle_status": row.lifecycle_status,
                                "is_deleted": bool(row.is_deleted),
                            },
                            "new": {
                                "lifecycle_status": expected_status,
                                "is_deleted": bool(expected_deleted),
                            },
                        },
                    )
                    row.lifecycle_status = expected_status
                    row.status = expected_status
                    row.is_deleted = bool(expected_deleted)
                    updated += 1

            db.commit()
    finally:
        _close_session(db)

    return {"status": "ok", "scanned": scanned, "updated": updated}


@celery_app.task(
    bind=True,
    name="gmvmax.dispatch_account_syncs",
    queue="gmvmax",
)
def dispatch_account_syncs_task(self, **_: Any) -> dict[str, Any]:
    """Enqueue each due account schedule persisted by the tenant control plane."""

    now = utcnow_naive()
    db = _db_session()
    dispatches: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    disabled: list[dict[str, Any]] = []
    expired_ownerships = 0
    try:
        cleanup_result = db.execute(
            delete(GMVMaxTaskOwnership).where(
                GMVMaxTaskOwnership.expires_at <= now
            )
        )
        expired_ownerships = max(0, int(cleanup_result.rowcount or 0))
        due_rows = list(
            db.execute(
                select(GMVMaxSyncSchedule)
                .where(
                    GMVMaxSyncSchedule.enabled.is_(True),
                    GMVMaxSyncSchedule.next_run_at <= now,
                )
                .order_by(GMVMaxSyncSchedule.next_run_at.asc(), GMVMaxSyncSchedule.id.asc())
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for row in due_rows:
            interval = max(1, int(row.interval_minutes or 10))
            binding = (
                db.execute(
                    select(TTBBindingConfig).where(
                        TTBBindingConfig.workspace_id == int(row.workspace_id),
                        TTBBindingConfig.auth_id == int(row.auth_id),
                    )
                )
                .scalars()
                .first()
            )
            current_advertiser_id = (
                str(binding.advertiser_id or "").strip()
                if binding is not None
                else ""
            )
            current_store_id = (
                str(binding.store_id or "").strip()
                if binding is not None
                else ""
            )
            if not current_advertiser_id or not current_store_id:
                reason = (
                    "GMV Max sync disabled: current ttb_binding_configs row "
                    "is missing advertiser_id or store_id"
                    if binding is not None
                    else "GMV Max sync disabled: current ttb_binding_configs row is missing"
                )
                row.enabled = False
                row.last_error = reason
                disabled.append(
                    {
                        "schedule_id": int(row.id),
                        "workspace_id": int(row.workspace_id),
                        "auth_id": int(row.auth_id),
                        "reason": reason,
                    }
                )
                continue

            # The schedule stores cadence only. Account scope is rebound from
            # the authoritative tenant binding on every due dispatch.
            row.advertiser_id = current_advertiser_id
            row.store_id = current_store_id
            task_id = new_owned_task_id()
            end_day = _advertiser_report_day(
                db,
                workspace_id=int(row.workspace_id),
                auth_id=int(row.auth_id),
                advertiser_id=current_advertiser_id,
            )
            # This cadence is the realtime lane. Historical daily facts are
            # immutable once settled and have their own backfill/finalization
            # paths, so re-reading prior days every ten minutes only increases
            # queue occupancy and makes interactive series syncs wait longer.
            start_day = end_day
            record_task_ownership(
                db,
                task_id=task_id,
                workspace_id=int(row.workspace_id),
                auth_id=int(row.auth_id),
                provider=str(row.provider),
                task_name="gmvmax.manual_sync_levels",
            )
            row.last_enqueued_at = now
            row.last_task_id = task_id
            row.last_error = None
            next_run_at = row.next_run_at
            while next_run_at <= now:
                next_run_at += timedelta(minutes=interval)
            row.next_run_at = next_run_at
            dispatches.append(
                {
                    "schedule_id": int(row.id),
                    "task_id": task_id,
                    "workspace_id": int(row.workspace_id),
                    "auth_id": int(row.auth_id),
                    "advertiser_id": current_advertiser_id,
                    "store_id": current_store_id,
                    "provider": str(row.provider),
                    "start_date": start_day.isoformat(),
                    "end_date": end_day.isoformat(),
                }
            )
        db.commit()

        for dispatch in dispatches:
            try:
                celery_app.send_task(
                    "gmvmax.manual_sync_levels",
                    kwargs={
                        "workspace_id": dispatch["workspace_id"],
                        "auth_id": dispatch["auth_id"],
                        "advertiser_id": dispatch["advertiser_id"],
                        "store_id": dispatch["store_id"],
                        "levels": [
                            "OVERVIEW",
                            "CAMPAIGN",
                            "PRODUCT",
                            "CREATIVE",
                            "LIVESTREAM",
                            "DURATION",
                        ],
                        "start_date": dispatch["start_date"],
                        "end_date": dispatch["end_date"],
                        # Recurring refreshes need current deliverable facts,
                        # not a ten-minute re-download of every disabled
                        # historical campaign. The catalog remains complete.
                        "require_active_campaigns": True,
                        "refresh_creative_assets": False,
                        # campaign/get already carries the delivery status used
                        # by realtime cards. Per-campaign info enrichment is a
                        # slower metadata lane and must not occupy the account
                        # sync lock every ten minutes.
                        "refresh_catalog_details": False,
                    },
                    queue="gmvmax",
                    task_id=dispatch["task_id"],
                )
                logger.info(
                    "gmvmax account sync dispatched",
                    extra=dispatch,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                schedule = db.get(GMVMaxSyncSchedule, dispatch["schedule_id"])
                if schedule is not None:
                    schedule.last_error = str(exc)[:1000]
                    schedule.next_run_at = min(
                        schedule.next_run_at,
                        utcnow_naive() + timedelta(minutes=1),
                    )
                remove_task_ownership(db, dispatch["task_id"])
                db.commit()
                failures.append(dispatch)
                logger.exception(
                    "gmvmax account sync dispatch failed",
                    extra=dispatch,
                )
    finally:
        _close_session(db)

    return {
        "status": "ok" if not failures else "partial",
        "due": len(dispatches) + len(disabled),
        "enqueued": len(dispatches) - len(failures),
        "failed": len(failures),
        "disabled": len(disabled),
        "disabled_schedules": disabled,
        "expired_ownerships_deleted": expired_ownerships,
        "failures": failures,
    }


# Sync campaigns for a specific binding (scheduled ~every 10 minutes); writes
# TTBGmvMaxCampaign/TTBGmvMaxCampaignProduct via TikTok /gmv_max/campaign/get/.
@celery_app.task(
    bind=True,
    name="gmvmax.sync_campaigns",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_campaigns(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    filters: Optional[dict[str, Any]] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
    ) -> dict:
    """同步 GMV Max Campaign 列表到本地 DB（幂等）。"""
    owner_token = f"{self.request.id or 'campaign-sync'}:{uuid4()}"
    lock = build_account_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_token=owner_token,
    )
    if not lock.acquire(timeout=30.0, retry_interval=0.2):
        raise RuntimeError("GMV Max account sync already running")

    fence = None
    fence_db = _db_session()
    try:
        fence = acquire_account_sync_fence(
            fence_db,
            redis_lock=lock,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_token=owner_token,
        )
        if fence is None:
            fence_db.rollback()
            raise RuntimeError("GMV Max durable account sync lease is busy")
        fence_db.commit()
    except Exception:
        fence_db.rollback()
        lock.release()
        raise
    finally:
        _close_session(fence_db)

    db = _db_session()
    try:
        payload_filters = dict(filters or {})
        if not payload_filters and params and isinstance(params, dict):
            payload_filters = dict(params.get("filters") or {})

        result = _run_with_client(
            db,
            auth_id,
            lambda client: sync_gmvmax_campaigns(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                **payload_filters,
            ),
        )
        fence.assert_current(db)
        db.commit()
        logger.info(
            "gmvmax.sync_campaigns done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "result": result,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return result or {}
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.sync_campaigns failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        raise
    finally:
        _close_session(db)
        release_db = _db_session()
        try:
            release_account_sync_fence(release_db, fence=fence)
            release_db.commit()
        except Exception:  # noqa: BLE001
            release_db.rollback()
            logger.exception(
                "gmvmax.sync_campaigns durable fence release failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                },
            )
        finally:
            _close_session(release_db)
            lock.release()


@celery_app.task(
    bind=True,
    name="gmvmax.fetch_campaign_detail",
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
    queue="gmvmax",
)
def task_gmvmax_fetch_campaign_detail(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    include_sessions: bool = True,
    **extra: Any,
) -> dict:
    """Fetch campaign detail via TikTok and refresh cached campaign rows asynchronously.

    Session data is fetched on-demand by the detail route and is no longer cached by
    this task.
    """
    owner_token = f"{self.request.id or 'campaign-detail-sync'}:{uuid4()}"
    lock = build_account_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_token=owner_token,
    )
    if not lock.acquire(timeout=30.0, retry_interval=0.2):
        raise RuntimeError("GMV Max account sync already running")

    fence = None
    fence_db = _db_session()
    try:
        fence = acquire_account_sync_fence(
            fence_db,
            redis_lock=lock,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_token=owner_token,
        )
        if fence is None:
            fence_db.rollback()
            raise RuntimeError("GMV Max durable account sync lease is busy")
        fence_db.commit()
    except Exception:
        fence_db.rollback()
        lock.release()
        raise
    finally:
        _close_session(fence_db)

    db = _db_session()
    try:
        result = _run_with_client(
            db,
            auth_id,
            lambda client: fetch_and_cache_campaign_detail(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
                include_sessions=include_sessions,
                execution_guard=fence.assert_current,
            ),
        )
        logger.info(
            "gmvmax.fetch_campaign_detail done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "task_id": self.request.id,
            },
        )
        return result
    except Exception:
        logger.exception(
            "gmvmax.fetch_campaign_detail failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "task_id": self.request.id,
            },
        )
        raise
    finally:
        _close_session(db)
        release_db = _db_session()
        try:
            release_account_sync_fence(release_db, fence=fence)
            release_db.commit()
        except Exception:  # noqa: BLE001
            release_db.rollback()
            logger.exception(
                "gmvmax.fetch_campaign_detail durable fence release failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                },
            )
        finally:
            _close_session(release_db)
            lock.release()


# Advertiser balance sync helper (triggered manually); no direct GMV Max API
# but persists balance snapshot used for eligibility checks.
@celery_app.task(
    bind=True,
    name="gmvmax.sync_advertiser_balance",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_advertiser_balance(
    self,
    *,
    workspace_id: int,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    bc_id: str | None = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **_: Any,
) -> dict[str, Any]:
    params = params or {}
    auth_id = auth_id or params.get("auth_id")
    advertiser_id = advertiser_id or params.get("advertiser_id")
    bc_id = bc_id or params.get("bc_id")
    if auth_id is None or advertiser_id is None or bc_id is None:
        logger.warning(
            "gmvmax.sync_advertiser_balance missing required identifiers",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "params": params,
                "schedule_id": schedule_id,
            },
        )
        return {"status": "skipped", "reason": "missing identifiers"}
    db = _db_session()
    try:
        last_balance = None
        if advertiser_id and auth_id:
            last_balance = select_latest_balance(
                db,
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                advertiser_id=str(advertiser_id),
            )
        if last_balance and last_balance.fetched_at:
            fetched_at = last_balance.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - fetched_at) < timedelta(
                seconds=BALANCE_FETCH_MIN_INTERVAL_SECONDS
            ):
                logger.info(
                    "gmvmax.sync_advertiser_balance skipped (recently synced)",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "bc_id": bc_id,
                        "schedule_id": schedule_id,
                        "idempotency_key": idempotency_key,
                        "run_id": run_id,
                        "fetched_at": fetched_at.isoformat(),
                    },
                )
                result = {
                    "status": "skipped",
                    "reason": "recently_synced",
                    "currency": last_balance.currency,
                    "cash_balance": float(last_balance.cash_balance)
                    if last_balance.cash_balance is not None
                    else None,
                    "fetched_at": fetched_at.isoformat(),
                }
                _finish_schedule_run(
                    db,
                    idempotency_key=idempotency_key,
                    status="success",
                    result=result,
                )
                db.commit()
                return result
        result = asyncio.run(
            sync_advertiser_balance(
                db,
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                bc_id=str(bc_id),
                advertiser_id=str(advertiser_id),
            )
        )
        _finish_schedule_run(
            db,
            idempotency_key=idempotency_key,
            status="success",
            result=result,
        )
        db.commit()
        logger.info(
            "gmvmax.sync_advertiser_balance done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "params": params,
            },
        )
        return result
    except APIError as exc:
        if exc.code != "NOT_FOUND":
            raise

        db.rollback()
        result = {
            "status": "skipped",
            "reason": "oauth_account_unavailable",
            "auth_id": int(auth_id),
        }
        try:
            _finish_schedule_run(
                db,
                idempotency_key=idempotency_key,
                status="success",
                result=result,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("gmvmax balance schedule run update failed")
            raise
        logger.warning(
            "gmvmax.sync_advertiser_balance skipped (OAuth account unavailable)",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return result
    except Exception as exc:
        db.rollback()
        try:
            _finish_schedule_run(
                db,
                idempotency_key=idempotency_key,
                status="failed",
                error=exc,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("gmvmax balance schedule run update failed")
        logger.exception(
            "gmvmax.sync_advertiser_balance failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "params": params,
            },
        )
        raise
    finally:
        _close_session(db)


@celery_app.task(
    bind=True,
    name="gmvmax.sync_creative_metrics_10min_for_campaign",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_creative_metrics_10min_for_campaign(
    self,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    sync_attempt_token: str | None = None,
    **extra: Any,
) -> dict:
    # Celery retry/redelivery keeps request.id. A per-execution nonce prevents
    # a stale delivery from verifying or releasing a newer delivery's lock.
    owner_token = (
        f"{self.request.id or 'creative-10min'}:{uuid4()}"
    )
    lock = build_creative_10min_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=str(advertiser_id),
        campaign_id=str(campaign_id),
        owner_token=owner_token,
    )
    if not lock.acquire(timeout=0.2, retry_interval=0.05):
        logger.warning(
            "gmvmax creative metrics sync deferred: campaign already inflight",
            extra={"campaign_id": campaign_id, "advertiser_id": advertiser_id},
        )
        raise self.retry(
            exc=RuntimeError("GMV Max creative metrics sync already running"),
            countdown=min(120, 15 * (int(self.request.retries or 0) + 1)),
        )

    fence = None
    fence_db = _db_session()
    try:
        fence = acquire_creative_10min_sync_fence(
            fence_db,
            redis_lock=lock,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            owner_token=owner_token,
        )
        if fence is None:
            fence_db.rollback()
        else:
            fence_db.commit()
    except Exception:
        fence_db.rollback()
        lock.release()
        raise
    finally:
        _close_session(fence_db)
    if fence is None:
        lock.release()
        raise self.retry(
            exc=RuntimeError(
                "GMV Max creative metrics durable sync lease is busy"
            ),
            countdown=min(120, 15 * (int(self.request.retries or 0) + 1)),
        )

    db = _db_session()
    effective_attempt_token = str(sync_attempt_token or "").strip()
    try:
        if not effective_attempt_token:
            # assert_current locks the durable generation in this transaction;
            # _begin_creative_10min_attempt commits the claim and renewal
            # together before upstream I/O starts.
            fence.assert_current(db)
            effective_attempt_token = _begin_creative_10min_attempt(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
                store_id=store_id,
            )
        campaign = _find_catalog_campaign_scope(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=campaign_id,
            store_id=store_id,
        )
        if not campaign:
            raise RuntimeError(f"campaign not found: {campaign_id}")

        def _sync(client: Any) -> Awaitable[dict[str, Any]]:
            window_start, window_end = _campaign_sync_window(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                lookback_days=int(getattr(settings, "GMVMAX_CREATIVE_10MIN_LOOKBACK_DAYS", 1)),
            )
            end = _parse_date_param(end_date, window_end)
            start = _parse_date_param(start_date, window_start)
            return sync_creative_metrics_10min_for_campaign(
                db,
                client,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=campaign,
                start_date=start,
                end_date=end,
            )

        result = _run_with_client(db, auth_id, _sync)
        _record_creative_10min_result(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            store_id=store_id,
            attempt_token=effective_attempt_token,
            success=True,
            rows=int((result or {}).get("rows", 0) or 0),
        )
        # Metric rows and completeness manifests are still uncommitted here.
        # Prove both Redis ownership and the persistent fencing generation in
        # this exact transaction before making the batch visible.
        fence.assert_current(db)
        db.commit()
        logger.info(
            "gmvmax.sync_creative_metrics_10min_for_campaign done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "result": result,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return result or {}
    except GmvMaxAccountSyncFenceLost as exc:
        db.rollback()
        logger.warning(
            "gmvmax creative metrics sync lost its execution fence",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
            },
        )
        raise self.retry(
            exc=exc,
            countdown=min(120, 15 * (int(self.request.retries or 0) + 1)),
        )
    except (TTBRateLimitBudgetError, TTBHttpError) as exc:
        db.rollback()
        _record_creative_10min_result(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            store_id=store_id,
            attempt_token=effective_attempt_token,
            success=False,
            error=exc,
        )
        fence.assert_current(db)
        db.commit()
        raise self.retry(exc=exc, countdown=ttb_retry_countdown(exc))
    except TTBBusinessError as exc:
        db.rollback()
        code = getattr(exc, "code", None)
        _record_creative_10min_result(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            store_id=store_id,
            attempt_token=effective_attempt_token,
            success=False,
            error=exc,
        )
        fence.assert_current(db)
        db.commit()
        if code in {
            "40002",
            40002,
            "GMVMAX_REPORT_ITEM_GROUP_REQUIRED",
            "GMVMAX_REPORT_DATE_INCOMPLETE",
        }:
            logger.warning(
                "gmvmax.sync_creative_metrics_10min_for_campaign business error",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                    "schedule_id": schedule_id,
                    "idempotency_key": idempotency_key,
                    "run_id": run_id,
                    "params": params,
                    "code": code,
                    "message": str(exc),
                },
            )
            return {"error": str(exc), "code": code}
        raise
    except TTBApiError as exc:
        db.rollback()
        code = getattr(exc, "code", None)
        message = str(exc)
        _record_creative_10min_result(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign_id),
            store_id=store_id,
            attempt_token=effective_attempt_token,
            success=False,
            error=exc,
        )
        fence.assert_current(db)
        db.commit()
        transient_report_error = (
            str(code) in {"51010", "52404"}
            or "timed out" in message.lower()
            or message.strip().lower() == "internal error."
        )
        if transient_report_error:
            logger.warning(
                "gmvmax.sync_creative_metrics_10min_for_campaign skipped after tiktok report transient error",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                    "schedule_id": schedule_id,
                    "idempotency_key": idempotency_key,
                    "run_id": run_id,
                    "params": params,
                    "code": code,
                    "error_message": message,
                },
            )
            return {"error": "tiktok_report_transient_error", "code": code, "retryable_next_sweep": True}
        raise
    except Exception as exc:
        db.rollback()
        try:
            _record_creative_10min_result(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
                store_id=store_id,
                attempt_token=effective_attempt_token,
                success=False,
                error=exc,
            )
            fence.assert_current(db)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "gmvmax creative 10min attempt state update failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                },
            )
        logger.exception(
            "gmvmax.sync_creative_metrics_10min_for_campaign failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "params": params,
            },
        )
        raise
    finally:
        _close_session(db)
        release_db = _db_session()
        try:
            release_sync_fence(release_db, fence=fence)
            release_db.commit()
        except Exception:  # noqa: BLE001
            release_db.rollback()
            logger.exception(
                "gmvmax creative metrics durable fence release failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                },
            )
        finally:
            _close_session(release_db)
            with contextlib.suppress(Exception):
                lock.release()


@celery_app.task(
    bind=True,
    name="gmvmax.sync_creative_metrics_10min",
    queue="gmvmax",
)
def task_gmvmax_sync_creative_metrics_10min(self, **extra: Any) -> dict:
    db = _db_session()
    enqueued = 0
    dispatch_failed = 0
    ranges: list[dict[str, Any]] = []
    try:
        scopes = _iter_sync_scopes(db)
        for workspace_id, auth_id, advertiser_id in scopes:
            max_campaigns = max(
                1,
                int(getattr(settings, "GMVMAX_CREATIVE_10MIN_MAX_CAMPAIGNS_PER_SWEEP", 6)),
            )
            campaigns = _iter_active_catalog_campaign_scopes(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                limit=max_campaigns,
            )
            campaign_windows: list[dict[str, Any]] = []
            dispatch_errors: list[dict[str, str]] = []
            ranges.append(
                {
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": str(advertiser_id),
                    "campaigns": len(campaigns),
                    "campaign_windows": campaign_windows,
                    "dispatch_errors": dispatch_errors,
                }
            )
            for campaign in campaigns:
                try:
                    start_day, end_day = _campaign_sync_window(
                        db,
                        campaign,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        lookback_days=int(
                            getattr(
                                settings,
                                "GMVMAX_CREATIVE_10MIN_LOOKBACK_DAYS",
                                1,
                            )
                        ),
                    )
                    campaign_windows.append(
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "start_date": start_day.isoformat(),
                            "end_date": end_day.isoformat(),
                        }
                    )
                    celery_app.send_task(
                        "gmvmax.sync_creative_metrics_10min_for_campaign",
                        kwargs={
                            "workspace_id": workspace_id,
                            "provider": getattr(campaign, "provider", "tiktok-business"),
                            "auth_id": auth_id,
                            "advertiser_id": str(advertiser_id),
                            "campaign_id": str(campaign.campaign_id),
                            "store_id": getattr(campaign, "store_id", None),
                            "start_date": start_day.isoformat(),
                            "end_date": end_day.isoformat(),
                            "sync_attempt_token": getattr(
                                campaign,
                                "sync_attempt_token",
                                None,
                            ),
                        },
                        queue="gmvmax",
                    )
                    enqueued += 1
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    _record_creative_10min_result(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        campaign_id=str(campaign.campaign_id),
                        store_id=getattr(campaign, "store_id", None),
                        attempt_token=getattr(campaign, "sync_attempt_token", None),
                        success=False,
                        error=f"DispatchPreparationError: {type(exc).__name__}: {exc}",
                    )
                    db.commit()
                    dispatch_failed += 1
                    dispatch_errors.append(
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
                    logger.exception(
                        "gmvmax creative 10min campaign dispatch failed",
                        extra={
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "advertiser_id": advertiser_id,
                            "campaign_id": str(campaign.campaign_id),
                        },
                    )
    finally:
        _close_session(db)

    logger.info(
        "gmvmax.sync_creative_metrics_10min sweep enqueued",
        extra={"tasks": enqueued, "dispatch_failed": dispatch_failed, "ranges": ranges},
    )
    return {
        "tasks": enqueued,
        "dispatch_failed": dispatch_failed,
        "ranges": ranges,
    }


# Periodic evaluations (strategy scheduler) to evaluate TTBGmvMaxCreativeHeating
# and stop creatives via official creative boost session deletion when needed.
@celery_app.task(
    bind=True,
    name="gmvmax.creative_heating_cycle",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_creative_heating_cycle(self, **extra: Any) -> dict:
    db = _db_session()
    try:
        result = asyncio.run(run_creative_heating_cycle(db))
        db.commit()
        logger.info("gmvmax.creative_heating_cycle done", extra=result)
        return result
    except Exception:
        db.rollback()
        logger.exception("gmvmax.creative_heating_cycle failed")
        raise
    finally:
        _close_session(db)


# Eligibility precheck task: calls /gmv_max/store/shop_ad_usage_check/,
# /gmv_max/identity/get/, and /gmv_max/occupied_custom_shop_ads/list/.
def _resolve_identity_occupied_asset_type(
    identity_data: Any,
    *,
    identity_id: str,
    requested_type: str | None,
) -> str:
    valid_types = {
        "IDENTITY_TT_USER",
        "IDENTITY_BC_AUTH_TT",
        "IDENTITY_TTS_TT",
    }
    normalized_requested = str(requested_type or "").upper()
    if normalized_requested in valid_types:
        return normalized_requested
    identity_type = None
    for identity in getattr(identity_data, "identity_list", None) or []:
        info = getattr(identity, "identity_info", None)
        if str(getattr(info, "identity_id", None) or "") == str(identity_id):
            identity_type = str(
                getattr(info, "identity_type", None) or ""
            ).upper()
            break
    resolved = {
        "TT_USER": "IDENTITY_TT_USER",
        "BC_AUTH_TT": "IDENTITY_BC_AUTH_TT",
        "TTS_TT": "IDENTITY_TTS_TT",
    }.get(identity_type or "")
    if not resolved:
        raise TTBApiError(
            "identity occupancy type could not be resolved",
            code="GMVMAX_IDENTITY_OCCUPANCY_TYPE_UNKNOWN",
            payload={"identity_id": str(identity_id)},
        )
    return resolved


@celery_app.task(
    bind=True,
    name="gmvmax.precheck",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_precheck(
    self,
    *,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    identity_id: Optional[str] = None,
    product_item_group_ids: Optional[list[str]] = None,
    occupied_asset_type: Optional[str] = None,
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        async def _worker(client: Any) -> dict:
            usage_resp = await client.gmv_max_store_shop_ad_usage_check(
                GMVMaxStoreAdUsageCheckRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                )
            )
            identity_resp = await client.gmv_max_identity_get(
                GMVMaxIdentityGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=str(store_authorized_bc_id),
                )
            )

            occupancy_entries: list[dict[str, Any]] = []
            occupancy_request_ids: list[str] = []
            occupancy_checked = False
            if identity_id:
                identity_asset_type = _resolve_identity_occupied_asset_type(
                    identity_resp.data,
                    identity_id=str(identity_id),
                    requested_type=occupied_asset_type,
                )
                identity_occupancy = await fetch_all_occupied_custom_shop_ads(
                    client,
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    occupied_asset_type=identity_asset_type,
                    asset_ids=[str(identity_id)],
                )
                occupancy_checked = True
                occupancy_entries.extend(
                    item.model_dump(exclude_none=True)
                    for item in identity_occupancy.data.occupied_custom_shop_ads
                )
                if identity_occupancy.request_id:
                    occupancy_request_ids.append(identity_occupancy.request_id)
            product_ids = [
                str(item)
                for item in (product_item_group_ids or [])
                if str(item).strip()
            ]
            if product_ids:
                product_occupancy = await fetch_all_occupied_custom_shop_ads(
                    client,
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    occupied_asset_type="SPU",
                    asset_ids=product_ids,
                )
                occupancy_checked = True
                occupancy_entries.extend(
                    item.model_dump(exclude_none=True)
                    for item in product_occupancy.data.occupied_custom_shop_ads
                )
                if product_occupancy.request_id:
                    occupancy_request_ids.append(product_occupancy.request_id)

            return {
                "store_usage": usage_resp.data.model_dump(exclude_none=True),
                "identities": [
                    entry.model_dump(exclude_none=True)
                    for entry in getattr(identity_resp.data, "identity_list", [])
                ],
                "occupancy": (
                    {"occupied_custom_shop_ads": occupancy_entries}
                    if occupancy_checked
                    else None
                ),
                "request_ids": {
                    "store_usage": usage_resp.request_id,
                    "identities": identity_resp.request_id,
                    "occupancy": ",".join(occupancy_request_ids) or None,
                },
            }

        return _run_with_client(db, auth_id, _worker)
    except Exception:
        logger.exception(
            "gmvmax.precheck failed",
            extra={
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "store_id": store_id,
            },
        )
        raise
    finally:
        _close_session(db)


# Strategy preview helper calling TikTok GET /gmv_max/bid/recommend/ for
# recommendations.
@celery_app.task(
    bind=True,
    name="gmvmax.strategy_preview",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_strategy_preview(
    self,
    *,
    auth_id: int,
    bid_request: dict[str, Any],
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        async def _worker(client: Any) -> dict:
            request = GMVMaxBidRecommendRequest.model_validate(bid_request)
            response = await client.gmv_max_bid_recommend(request)
            return {
                "recommendation": response.data.model_dump(exclude_none=True),
                "request_id": response.request_id,
            }

        return _run_with_client(db, auth_id, _worker)
    except Exception:
        logger.exception(
            "gmvmax.strategy_preview failed",
            extra={"auth_id": auth_id, "advertiser_id": bid_request.get("advertiser_id")},
        )
        raise
    finally:
        _close_session(db)


def _run_manual_sync_levels_unlocked(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None = None,
    store_id: str | None = None,
    levels: list[str],
    start_date: str,
    end_date: str,
    campaign_ids: Optional[list[str]] = None,
    item_group_ids: Optional[list[str]] = None,
    require_active_campaigns: bool = False,
    refresh_creative_assets: bool = False,
    backfill_missing_creative_assets: bool = False,
    refresh_catalog_details: bool = True,
    execution_guard: Callable[[Session], None] | None = None,
) -> dict:
    """Run one account sync after the shared execution lock is acquired."""

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        # Let Celery capture the exception so the backend stores a serializable
        # failure payload with exc_type/traceback instead of a custom dict.
        raise

    if not advertiser_id:
        raise ValueError("advertiser_id is required for GMV Max account sync")

    # Refresh the authoritative catalog/status first. Metrics without this step
    # can be fresh while the UI still displays an obsolete delivery status.
    catalog_db = _db_session()
    try:
        normalized_campaign_ids = list(
            dict.fromkeys(str(value).strip() for value in (campaign_ids or []) if str(value).strip())
        )
        if len(normalized_campaign_ids) == 1:
            # A detail-page refresh already has an exact campaign scope. One
            # campaign/info call is authoritative and avoids two paginated
            # account-list rounds (active + deleted) before every click.
            catalog_result = _run_with_client(
                catalog_db,
                auth_id,
                lambda client: fetch_and_cache_campaign_detail(
                    catalog_db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    campaign_id=normalized_campaign_ids[0],
                    include_sessions=False,
                    execution_guard=execution_guard,
                ),
            )
            catalog_result = {"synced": 1, "mode": "campaign_detail", **(catalog_result or {})}
        else:
            catalog_result = _run_with_client(
                catalog_db,
                auth_id,
                lambda client: sync_gmvmax_campaigns(
                    catalog_db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    include_campaign_details=bool(refresh_catalog_details),
                    store_ids=[str(store_id)] if store_id else None,
                    campaign_ids=normalized_campaign_ids or None,
                    gmv_max_promotion_types=[
                        "PRODUCT_GMV_MAX",
                        "LIVE_GMV_MAX",
                    ],
                ),
            )
        if execution_guard is not None:
            execution_guard(catalog_db)
        catalog_db.commit()
    except Exception:
        catalog_db.rollback()
        logger.exception(
            "gmvmax.manual_sync_levels catalog refresh failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "store_id": store_id,
            },
        )
        raise
    finally:
        _close_session(catalog_db)

    service = GmvMaxSyncService(execution_guard=execution_guard)
    try:
        results = service.sync_levels_for_account(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            levels=levels,
            start_date=start,
            end_date=end,
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            require_active_campaigns=bool(require_active_campaigns),
            refresh_creative_assets=bool(refresh_creative_assets),
            backfill_missing_creative_assets=bool(backfill_missing_creative_assets),
        )
        errors = [
            {"level": level, **value["error"]}
            for level, value in results.items()
            if isinstance(value, dict) and value.get("error")
        ]
        payload = {
            "catalog": catalog_result or {},
            "results": results,
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "levels": levels,
            "start_date": start_date,
            "end_date": end_date,
            "errors": errors,
            "require_active_campaigns": bool(require_active_campaigns),
            "refresh_creative_assets": bool(refresh_creative_assets),
            "backfill_missing_creative_assets": bool(backfill_missing_creative_assets),
            "refresh_catalog_details": bool(refresh_catalog_details),
        }
        logger.info(
            "gmvmax.manual_sync_levels succeeded",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "levels": levels},
        )
        if errors:
            raise RuntimeError(
                "GMV Max manual sync completed with level errors: "
                + json.dumps(errors, ensure_ascii=False, default=str)
            )
        return payload
    except Exception:  # pragma: no cover - logging and bubbling for Celery
        logger.exception(
            "gmvmax.manual_sync_levels failed",
            extra={"workspace_id": workspace_id, "auth_id": auth_id, "levels": levels},
        )
        raise


@celery_app.task(
    name="gmvmax.manual_sync_levels",
    bind=True,
    queue="gmvmax",
    max_retries=60,
)
def manual_sync_levels_task(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None = None,
    store_id: str | None = None,
    levels: list[str],
    start_date: str,
    end_date: str,
    campaign_ids: Optional[list[str]] = None,
    item_group_ids: Optional[list[str]] = None,
    require_active_campaigns: bool = False,
    refresh_creative_assets: bool = False,
    backfill_missing_creative_assets: bool = False,
    refresh_catalog_details: bool = True,
) -> dict:
    """Serialize account-wide catalog and fact refreshes across all entrypoints."""

    # Celery retry/redelivery preserves request.id.  Every concrete execution
    # needs a distinct owner so a stale finally block cannot release a newer
    # delivery's Redis lock or durable fence.
    owner_token = f"{self.request.id or 'manual-sync'}:{uuid4()}"
    lock = build_account_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_token=owner_token,
    )
    # Never occupy a worker for 30 seconds while another account sync owns the
    # fence. Requeue briefly instead so the task can begin close to the actual
    # lock release and other accounts keep using the worker pool.
    if not lock.acquire(timeout=0.0, retry_interval=0.2):
        logger.warning(
            "gmvmax manual account sync deferred: account sync already running",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
            },
        )
        raise self.retry(
            exc=RuntimeError("GMV Max account sync already running"),
            countdown=5,
        )
    fence = None
    fence_db = _db_session()
    try:
        fence = acquire_account_sync_fence(
            fence_db,
            redis_lock=lock,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_token=owner_token,
        )
        if fence is None:
            fence_db.rollback()
        else:
            fence_db.commit()
    except Exception:
        fence_db.rollback()
        lock.release()
        raise
    finally:
        _close_session(fence_db)
    if fence is None:
        lock.release()
        raise self.retry(
            exc=RuntimeError("GMV Max durable account sync lease is busy"),
            countdown=5,
        )
    try:
        return _run_manual_sync_levels_unlocked(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            levels=levels,
            start_date=start_date,
            end_date=end_date,
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            require_active_campaigns=bool(require_active_campaigns),
            refresh_creative_assets=bool(refresh_creative_assets),
            backfill_missing_creative_assets=bool(backfill_missing_creative_assets),
            refresh_catalog_details=bool(refresh_catalog_details),
            execution_guard=fence.assert_current,
        )
    except (TTBRateLimitBudgetError, TTBHttpError) as exc:
        countdown = ttb_retry_countdown(exc)
        logger.warning(
            "gmvmax manual account sync deferred by TikTok quota",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "countdown": countdown,
            },
        )
        raise self.retry(exc=exc, countdown=countdown)
    finally:
        release_db = _db_session()
        try:
            release_db.rollback()
            release_account_sync_fence(release_db, fence=fence)
            release_db.commit()
        except Exception:  # noqa: BLE001
            release_db.rollback()
            logger.exception(
                "gmvmax manual account sync durable fence release failed",
                extra={"workspace_id": workspace_id, "auth_id": auth_id},
            )
        finally:
            _close_session(release_db)
            lock.release()
