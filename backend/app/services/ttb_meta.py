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
    TTBBCAdvertiserLink,
    TTBAdvertiserStoreLink,
)
from app.services.ttb_schema import advertiser_display_timezone_supported
from app.services.ttb_sync import _normalize_identifier

_PROVIDER = "tiktok-business"
_LOGGER = logging.getLogger("gmv.ttb.meta")
_RELATION_PRIORITY = {"OWNER": 1, "AUTHORIZER": 2, "PARTNER": 3, "UNKNOWN": 4}


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


def _relation_rank(value: Optional[str]) -> int:
    return _RELATION_PRIORITY.get((value or "UNKNOWN").upper(), 5)


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
        try:
            digest = hashlib.sha1(data)
        except (ValueError, RuntimeError):
            digest = hashlib.blake2s(data)
    except (ValueError, RuntimeError):
        digest = hashlib.blake2s(data)
    return digest.hexdigest()


def _collect_store_candidates(store: TTBStore) -> Iterable[str]:
    raw_payload = getattr(store, "raw_json", None) or getattr(store, "raw", None) or {}
    for candidate in (
        raw_payload.get("store_authorized_bc_id"),
        raw_payload.get("authorized_bc_id"),
        raw_payload.get("bc_id"),
        store.bc_id,
        getattr(store, "store_authorized_bc_id", None),
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

    # ---------------- BC 列表 ----------------
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

    # ---------------- Advertiser 列表 ----------------
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

    # ---------------- Store 列表 ----------------
    store_rows = (
        db.query(TTBStore)
        .filter(
            TTBStore.workspace_id == int(workspace_id),
            TTBStore.auth_id == int(auth_id),
        )
        .order_by(TTBStore.store_id.asc())
        .all()
    )

    # ---------------- Link 表 ----------------
    bc_link_rows = (
        db.query(
            TTBBCAdvertiserLink.advertiser_id,
            TTBBCAdvertiserLink.bc_id,
            TTBBCAdvertiserLink.relation_type,
            TTBBCAdvertiserLink.last_seen_at,
        )
        .filter(
            TTBBCAdvertiserLink.workspace_id == int(workspace_id),
            TTBBCAdvertiserLink.auth_id == int(auth_id),
        )
        .all()
    )

    store_link_rows = (
        db.query(
            TTBAdvertiserStoreLink.advertiser_id,
            TTBAdvertiserStoreLink.store_id,
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
            TTBAdvertiserStoreLink.last_seen_at,
        )
        .filter(
            TTBAdvertiserStoreLink.workspace_id == int(workspace_id),
            TTBAdvertiserStoreLink.auth_id == int(auth_id),
        )
        .all()
    )

    # ---------------- 聚合时间戳 ----------------
    timestamps: list[datetime] = []

    # ---------------- BC payload ----------------
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

    # ---------------- Store link 映射（主数据来源） ----------------
    store_link_map: dict[str, dict[str, Optional[str]]] = {}
    advertiser_to_stores: defaultdict[str, set[str]] = defaultdict(set)

    for adv_id, store_id, authorized_bc, bc_hint, link_seen in store_link_rows:
        if link_seen:
            timestamps.append(_to_utc(link_seen))

        normalized_adv = _normalize_identifier(adv_id)
        normalized_store = _normalize_identifier(store_id)
        if not normalized_adv or not normalized_store:
            continue

        advertiser_to_stores[normalized_adv].add(normalized_store)
        info = store_link_map.setdefault(
            normalized_store,
            {"store_authorized_bc_id": None, "bc_id_hint": None},
        )
        auth_norm = _normalize_identifier(authorized_bc)
        hint_norm = _normalize_identifier(bc_hint)
        if auth_norm and not info.get("store_authorized_bc_id"):
            info["store_authorized_bc_id"] = auth_norm
        if hint_norm and not info.get("bc_id_hint"):
            info["bc_id_hint"] = hint_norm

    # ---------------- Store payload（安全读取 + 用 link 兜底） ----------------
    stores_payload: list[dict] = []
    store_by_id: dict[str, TTBStore] = {}

    for store in store_rows:
        if store.last_seen_at:
            timestamps.append(_to_utc(store.last_seen_at))

        # 安全读取，不假设模型里一定有 advertiser_id 字段
        adv_raw = getattr(store, "advertiser_id", None)
        adv_key = _normalize_identifier(adv_raw)
        store_key = _normalize_identifier(store.store_id)

        if store_key:
            store_by_id[store_key] = store

        # 如果模型里带了 advertiser_id（旧表结构残留），仅“补充性”加入映射，不覆盖 link 的主判断
        if adv_key and store_key:
            advertiser_to_stores[adv_key].add(store_key)

        payload = {
            "store_id": store.store_id,
            "name": store.name,
            "advertiser_id": adv_raw,  # 只是补充信息，可能为 None
            "bc_id": store.bc_id,
            "store_type": getattr(store, "store_type", None),
            "store_code": getattr(store, "store_code", None),
            "store_authorized_bc_id": getattr(store, "store_authorized_bc_id", None),
        }

        # 用 link 提供的线索把缺失字段补齐
        link_info = store_link_map.get(store_key or "")
        if link_info:
            if link_info.get("store_authorized_bc_id") and not payload.get("store_authorized_bc_id"):
                payload["store_authorized_bc_id"] = link_info["store_authorized_bc_id"]
            if link_info.get("bc_id_hint") and not payload.get("bc_id"):
                payload["bc_id"] = link_info["bc_id_hint"]

        stores_payload.append(payload)

    # ---------------- BC link 映射（主数据来源） ----------------
    bc_link_map: dict[str, tuple[int, str]] = {}
    bc_to_advertisers: defaultdict[str, set[str]] = defaultdict(set)

    for adv_id, bc_id, relation_type, link_seen in bc_link_rows:
        if link_seen:
            timestamps.append(_to_utc(link_seen))

        normalized_adv = _normalize_identifier(adv_id)
        normalized_bc = _normalize_identifier(bc_id)
        if not normalized_adv or not normalized_bc:
            continue

        rank = _relation_rank(relation_type)
        existing = bc_link_map.get(normalized_adv)
        if existing is None or rank < existing[0]:
            bc_link_map[normalized_adv] = (rank, normalized_bc)
        bc_to_advertisers[normalized_bc].add(normalized_adv)

    # ---------------- Advertiser payload（必要时从 store 线索反推 bc） ----------------
    advertisers = []
    for adv in advertiser_rows:
        if adv.last_seen_at:
            timestamps.append(_to_utc(adv.last_seen_at))

        adv_key = _normalize_identifier(adv.advertiser_id)
        resolved_bc_id = adv.bc_id

        link_hint = bc_link_map.get(adv_key or "") if adv_key else None
        if not resolved_bc_id and link_hint:
            resolved_bc_id = link_hint[1]

        # 仍拿不到 bc_id 的话，从与其绑定的 store 里根据 authorized_bc/bc 线索投票
        if not resolved_bc_id and adv_key:
            candidates: Counter[str] = Counter()
            for store_id in advertiser_to_stores.get(adv_key, set()):
                store_obj = store_by_id.get(store_id)
                if not store_obj:
                    continue
                for value in _collect_store_candidates(store_obj):
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

        if resolved_bc_id and adv_key:
            bc_to_advertisers[str(resolved_bc_id)].add(adv_key)

    # ---------------- Links 汇总 ----------------
    links_bc_to_adv = {bc_id: sorted(values) for bc_id, values in bc_to_advertisers.items()}
    links_adv_to_store = {adv_id: sorted(ids) for adv_id, ids in advertiser_to_stores.items()}

    # ---------------- 最终时间戳与输出 ----------------
    timestamps = [ts for ts in timestamps if ts]
    synced_candidate = max(timestamps) if timestamps else None
    if not synced_candidate:
        synced_candidate = _to_utc(fallback_synced_at)

    payload: Dict[str, object] = {
        "bcs": [
            {
                "bc_id": row["bc_id"],
                "name": row["name"],
                "timezone": row["timezone"],
                "country_code": row["country_code"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in bcs
        ],
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
    """
    Enqueue a background sync for a freshly bound account.

    规则：
    - 优先尝试跑全量 task（ttb.sync.all），则 envelope.scope 必须是 "all"
    - 如不可用则回退到 meta（ttb.sync.meta），同时把 envelope.scope 改成 "meta"
    - 其它字段保持不变（幂等键沿用 bind-init-meta-...，不改名）
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%d%H%M")
    idempotency_key = f"bind-init-meta-{workspace_id}-{auth_id}-{stamp}"

    queue_name = getattr(settings, "CELERY_DEFAULT_QUEUE", None) or "gmv.tasks.events"
    from app.celery_app import celery_app  # noqa: WPS433

    # --- primary: run "all"
    primary_task = "ttb.sync.all"
    primary_scope = "all"

    envelope_all = {
        "envelope_version": 1,
        "provider": _PROVIDER,
        "scope": primary_scope,
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "options": {"mode": "incremental"},
        "meta": {"run_id": None, "schedule_id": None, "idempotency_key": idempotency_key},
    }
    payload_all = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "scope": primary_scope,
        "params": {"envelope": envelope_all},
        "idempotency_key": idempotency_key,
    }

    task_name = primary_task
    try:
        celery_app.send_task(primary_task, kwargs=payload_all, queue=queue_name)
    except Exception:  # noqa: BLE001
        # --- fallback: run "meta"
        fallback_task = "ttb.sync.meta"
        fallback_scope = "meta"

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

        envelope_meta = {
            "envelope_version": 1,
            "provider": _PROVIDER,
            "scope": fallback_scope,
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "options": {"mode": "incremental"},
            "meta": {"run_id": None, "schedule_id": None, "idempotency_key": idempotency_key},
        }
        payload_meta = {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "scope": fallback_scope,
            "params": {"envelope": envelope_meta},
            "idempotency_key": idempotency_key,
        }
        celery_app.send_task(fallback_task, kwargs=payload_meta, queue=queue_name)
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

