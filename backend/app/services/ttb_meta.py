"""Utilities for TikTok Business meta aggregation and sync helpers."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Mapping, MutableMapping, Optional

from sqlalchemy import literal
from sqlalchemy.orm import Session
from app.core.config import settings
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBSyncCursor,
)
from app.services.ttb_schema import advertiser_display_timezone_supported

_PROVIDER = "tiktok-business"
_LOGGER = logging.getLogger("gmv.ttb.meta")


@dataclass(frozen=True)
class MetaSyncEnqueueResult:
    """Represents the scheduling outcome for a meta sync enqueue."""

    idempotency_key: str
    task_name: str


@dataclass(frozen=True)
class MetaCursorState:
    """Snapshot of meta cursor revisions for BC / advertiser / store."""

    revisions: Mapping[str, str]
    updated_at: Optional[datetime]


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    utc = _to_utc(dt)
    return utc.isoformat() if utc else None


def get_meta_cursor_state(
    db: Session, *, workspace_id: int, auth_id: int
) -> MetaCursorState:
    """Return the current revision state for meta resources."""

    resource_types = {"bc", "advertiser", "advertisers", "store", "shop", "shops"}
    rows = (
        db.query(TTBSyncCursor)
        .filter(
            TTBSyncCursor.workspace_id == int(workspace_id),
            TTBSyncCursor.auth_id == int(auth_id),
            TTBSyncCursor.provider == _PROVIDER,
            TTBSyncCursor.resource_type.in_(resource_types),
        )
        .all()
    )

    revisions: MutableMapping[str, str] = {"bc": "", "advertiser": "", "store": ""}
    updated_at: Optional[datetime] = None
    normalization_map = {"advertisers": "advertiser", "shop": "store", "shops": "store"}
    for row in rows:
        key = (row.resource_type or "").strip().lower()
        key = normalization_map.get(key, key)
        if key in revisions:
            revisions[key] = (row.last_rev or "").strip()
        if row.updated_at:
            candidate = _to_utc(row.updated_at)
            if not updated_at or (candidate and candidate > updated_at):
                updated_at = candidate

    return MetaCursorState(revisions=dict(revisions), updated_at=updated_at)


def compute_meta_etag(revisions: Mapping[str, str]) -> str:
    """Compute the strong ETag for the given revision tuple."""

    bc_rev = revisions.get("bc", "")
    adv_rev = revisions.get("advertiser", "")
    store_rev = revisions.get("store", "")
    payload = f"{bc_rev}:{adv_rev}:{store_rev}"
    data = payload.encode("utf-8")
    try:
        digest = hashlib.sha1(data, usedforsecurity=False)
    except TypeError:
        # Python builds without the 'usedforsecurity' flag still support sha1.
        try:
            digest = hashlib.sha1(data)
        except (ValueError, RuntimeError):
            digest = hashlib.blake2s(data)
    except (ValueError, RuntimeError):
        digest = hashlib.blake2s(data)
    return digest.hexdigest()


def _collect_store_candidates(store: TTBStore) -> Iterable[str]:
    raw_payload = (
        getattr(store, "raw_json", None)
        or getattr(store, "raw", None)
        or {}
    )
    for candidate in (
        raw_payload.get("store_authorized_bc_id"),
        raw_payload.get("authorized_bc_id"),
        raw_payload.get("bc_id"),
        store.bc_id,
    ):
        if candidate:
            yield str(candidate)


def build_gmvmax_options(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    fallback_synced_at: Optional[datetime] = None,
) -> Dict[str, object]:
    """Assemble the aggregated GMV Max options payload from the database."""

    bc_rows = (
        db.query(TTBBusinessCenter)
        .filter(
            TTBBusinessCenter.workspace_id == int(workspace_id),
            TTBBusinessCenter.auth_id == int(auth_id),
        )
        .order_by(TTBBusinessCenter.bc_id.asc())
        .all()
    )
    supports_display_timezone = advertiser_display_timezone_supported(db)
    display_timezone_column = (
        TTBAdvertiser.display_timezone
        if supports_display_timezone
        else literal(None).label("display_timezone")
    )

    advertiser_rows = (
        db.query(
            TTBAdvertiser.advertiser_id,
            TTBAdvertiser.name,
            TTBAdvertiser.display_name,
            TTBAdvertiser.status,
            TTBAdvertiser.industry,
            TTBAdvertiser.currency,
            TTBAdvertiser.timezone,
            display_timezone_column,
            TTBAdvertiser.country_code,
            TTBAdvertiser.bc_id,
            TTBAdvertiser.last_seen_at,
        )
        .filter(
            TTBAdvertiser.workspace_id == int(workspace_id),
            TTBAdvertiser.auth_id == int(auth_id),
        )
        .order_by(TTBAdvertiser.advertiser_id.asc())
        .all()
    )
    store_rows = (
        db.query(TTBStore)
        .filter(
            TTBStore.workspace_id == int(workspace_id),
            TTBStore.auth_id == int(auth_id),
        )
        .order_by(TTBStore.store_id.asc())
        .all()
    )

    timestamps: list[datetime] = []

    bcs = []
    for row in bc_rows:
        if row.last_seen_at:
            timestamps.append(_to_utc(row.last_seen_at))
        bcs.append(
            {
                "bc_id": row.bc_id,
                "name": row.name,
                "timezone": row.timezone,
                "country_code": row.country_code,
                "last_seen_at": _iso(row.last_seen_at),
            }
        )

    stores_by_advertiser: defaultdict[str, list[TTBStore]] = defaultdict(list)
    stores_payload = []
    for store in store_rows:
        if store.last_seen_at:
            timestamps.append(_to_utc(store.last_seen_at))
        if store.advertiser_id:
            stores_by_advertiser[store.advertiser_id].append(store)
        stores_payload.append(
            {
                "store_id": store.store_id,
                "name": store.name,
                "advertiser_id": store.advertiser_id,
                "bc_id": store.bc_id,
            }
        )

    advertisers = []
    bc_to_advertisers: defaultdict[str, set[str]] = defaultdict(set)
    advertiser_to_stores: defaultdict[str, list[str]] = defaultdict(list)

    for store in stores_payload:
        adv_id = store.get("advertiser_id")
        store_id = store.get("store_id")
        if adv_id and store_id:
            advertiser_to_stores[adv_id].append(store_id)

    for adv in advertiser_rows:
        if adv.last_seen_at:
            timestamps.append(_to_utc(adv.last_seen_at))

        resolved_bc_id = adv.bc_id
        if not resolved_bc_id:
            candidates = Counter()
            for store in stores_by_advertiser.get(adv.advertiser_id or "", []):
                for value in _collect_store_candidates(store):
                    candidates[value] += 1
            if candidates:
                resolved_bc_id, _ = candidates.most_common(1)[0]
                if len(candidates) > 1:
                    _LOGGER.warning(
                        "detected conflicting store_authorized_bc_id hints for advertiser",
                        extra={
                            "provider": _PROVIDER,
                            "workspace_id": int(workspace_id),
                            "auth_id": int(auth_id),
                            "advertiser_id": adv.advertiser_id,
                            "candidates": dict(candidates),
                            "chosen": resolved_bc_id,
                            "idempotency_key": None,
                            "task_name": None,
                        },
                    )
        advertisers.append(
            {
                "advertiser_id": adv.advertiser_id,
                "name": adv.name,
                "display_name": adv.display_name,
                "status": adv.status,
                "industry": adv.industry,
                "currency": adv.currency,
                "timezone": adv.timezone,
                "display_timezone": adv.display_timezone,
                "country_code": adv.country_code,
                "bc_id": resolved_bc_id,
            }
        )
        if resolved_bc_id and adv.advertiser_id:
            bc_to_advertisers[resolved_bc_id].add(adv.advertiser_id)

    # sort advertiser_to_stores lists for stable output
    links_bc_to_adv = {
        bc_id: sorted(values) for bc_id, values in bc_to_advertisers.items()
    }
    links_adv_to_store = {
        adv_id: sorted(ids) for adv_id, ids in advertiser_to_stores.items()
    }

    timestamps = [ts for ts in timestamps if ts]
    synced_candidate = max(timestamps) if timestamps else None
    if not synced_candidate:
        synced_candidate = _to_utc(fallback_synced_at)

    payload: Dict[str, object] = {
        "bcs": bcs,
        "advertisers": advertisers,
        "stores": stores_payload,
        "links": {
            "bc_to_advertisers": links_bc_to_adv,
            "advertiser_to_stores": links_adv_to_store,
        },
        "synced_at": _iso(synced_candidate),
        "source": "db",
    }

    return payload


def enqueue_meta_sync(
    *, workspace_id: int, auth_id: int, now: Optional[datetime] = None
) -> MetaSyncEnqueueResult:
    """Enqueue a background meta sync task with an idempotent key."""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%d%H%M")
    idempotency_key = f"bind-init-meta-{workspace_id}-{auth_id}-{stamp}"

    envelope = {
        "envelope_version": 1,
        "provider": _PROVIDER,
        "scope": "meta",
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "options": {"mode": "incremental"},
        "meta": {
            "run_id": None,
            "schedule_id": None,
            "idempotency_key": idempotency_key,
        },
    }

    payload = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "scope": "meta",
        "params": {"envelope": envelope},
        "idempotency_key": idempotency_key,
    }

    queue_name = getattr(settings, "CELERY_DEFAULT_QUEUE", None) or "gmv.tasks.events"
    primary_task = "ttb.sync.all"
    task_name = primary_task
    from app.celery_app import celery_app  # noqa: WPS433 (lazy import to avoid cycles)

    try:
        celery_app.send_task(primary_task, kwargs=payload, queue=queue_name)
    except Exception:  # noqa: BLE001
        fallback_task = "ttb.sync.meta"
        _LOGGER.warning(
            "primary meta sync task unavailable; falling back",
            exc_info=True,
            extra={
                "provider": _PROVIDER,
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "idempotency_key": idempotency_key,
                "task_name": primary_task,
            },
        )
        celery_app.send_task(fallback_task, kwargs=payload, queue=queue_name)
        task_name = fallback_task

    _LOGGER.info(
        "enqueued meta sync",
        extra={
            "provider": _PROVIDER,
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "idempotency_key": idempotency_key,
            "task_name": task_name,
        },
    )

    return MetaSyncEnqueueResult(idempotency_key=idempotency_key, task_name=task_name)
