# app/services/ttb_sync.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple, Literal, Set, Iterable
import logging
import contextlib

logger = logging.getLogger("gmv.ttb.sync")

from sqlalchemy import text, and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.data.models.ttb_entities import (
    TTBSyncCursor,
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBProduct,
    TTBBCAdvertiserLink,
    TTBAdvertiserStoreLink,
)
from app.services.ttb_api import TTBApiClient
from app.services.ttb_schema import advertiser_display_timezone_supported
from app.core.config import settings


# --------------------------- 工具：字段提取与时间解析 ---------------------------
def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _normalize_relation_type(value: Any, default: str = "UNKNOWN") -> str:
    if value is None:
        return default
    candidate = str(value).strip().upper()
    if not candidate:
        return default
    if candidate not in _ALLOWED_RELATION_TYPES:
        return default
    return candidate


def _relation_rank(value: Optional[str]) -> int:
    if not value:
        return max(_RELATION_PRIORITY.values()) + 1
    return _RELATION_PRIORITY.get(value.upper(), max(_RELATION_PRIORITY.values()) + 1)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


_ALLOWED_RELATION_TYPES = {"OWNER", "PARTNER", "AUTHORIZER", "UNKNOWN"}
_RELATION_PRIORITY = {"OWNER": 1, "AUTHORIZER": 2, "PARTNER": 3, "UNKNOWN": 4}


def _clamp_advertiser_batch_size(raw: Any, default: int = 50) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    value = max(1, value)
    return value if value <= 50 else 50


_ADVERTISER_INFO_BATCH_SIZE = _clamp_advertiser_batch_size(
    getattr(settings, "TTB_ADVERTISER_INFO_BATCH_SIZE", 50)
)
_ADVERTISER_INFO_FIELDS: tuple[str, ...] = (
    "advertiser_id",
    "name",
    "display_name",
    "status",
    "industry",
    "currency",
    "timezone",
    "display_timezone",
    "country",
    "country_code",
    "owner_bc_id",
    "bc_id",
    "create_time",
    "update_time",
)


def _chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    bucket: list[str] = []
    for value in values:
        bucket.append(value)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


# --------------------------- 游标获取/创建 ---------------------------
def _get_or_create_cursor(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    resource_type: str,
    provider: str = "tiktok-business",
) -> TTBSyncCursor:
    row = (
        db.query(TTBSyncCursor)
        .filter(
            TTBSyncCursor.workspace_id == workspace_id,
            TTBSyncCursor.auth_id == auth_id,
            TTBSyncCursor.provider == provider,
            TTBSyncCursor.resource_type == resource_type,
        )
        .one_or_none()
    )
    if row is None:
        row = TTBSyncCursor(
            workspace_id=workspace_id,
            auth_id=auth_id,
            provider=provider,
            resource_type=resource_type,
        )
        db.add(row)
        db.flush()
    return row


# --------------------------- UPSERT helpers ---------------------------
def _dialect(db: Session) -> str:
    bind = getattr(db, "bind", None)
    if not bind or not getattr(bind, "dialect", None):
        return ""
    return bind.dialect.name


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(
    db: Session,
    model,
    *,
    values: Dict[str, Any],
    conflict_columns: tuple[str, ...],
    update_columns: tuple[str, ...],
) -> None:
    table = model.__table__
    dialect = _dialect(db)
    if dialect == "sqlite":
        filters = [getattr(table.c, col) == values[col] for col in conflict_columns]
        update_payload = {col: values[col] for col in update_columns if col in values}
        if "last_seen_at" in table.c:
            update_payload["last_seen_at"] = _now()
        result = db.execute(table.update().where(and_(*filters)).values(**update_payload))
        if result.rowcount == 0:
            insert_values = dict(values)
            if "last_seen_at" in table.c and "last_seen_at" not in insert_values:
                insert_values["last_seen_at"] = _now()
            db.execute(table.insert().values(**insert_values))
        return

    stmt = mysql_insert(table).values(values)
    update_payload = {
        col: getattr(stmt.inserted, col)
        for col in update_columns
        if col != "last_seen_at"
    }
    if "last_seen_at" in table.c:
        update_payload["last_seen_at"] = text("CURRENT_TIMESTAMP(6)")
    stmt = stmt.on_duplicate_key_update(**update_payload)
    db.execute(stmt)


def _touch_bc_advertiser_link(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: Optional[str],
    bc_id: Optional[str],
    relation_type: Optional[str] = None,
    source: Optional[str] = None,
    raw: Optional[dict] = None,
) -> None:
    adv = _normalize_identifier(advertiser_id)
    bc = _normalize_identifier(bc_id)
    if not adv or not bc:
        return

    relation = _normalize_relation_type(relation_type)
    existing = (
        db.query(TTBBCAdvertiserLink)
        .filter(
            TTBBCAdvertiserLink.workspace_id == workspace_id,
            TTBBCAdvertiserLink.auth_id == auth_id,
            TTBBCAdvertiserLink.advertiser_id == adv,
            TTBBCAdvertiserLink.bc_id == bc,
        )
        .one_or_none()
    )

    if existing is None:
        row = TTBBCAdvertiserLink(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=adv,
            bc_id=bc,
            relation_type=relation,
            source=_clean_str(source) or None,
            raw_json=raw if isinstance(raw, dict) else None,
        )
        if hasattr(row, "last_seen_at"):
            row.last_seen_at = _now()
        db.add(row)
        return

    updated = False
    normalized_relation = relation
    if _relation_rank(existing.relation_type) > _relation_rank(normalized_relation):
        existing.relation_type = normalized_relation
        updated = True
    cleaned_source = _clean_str(source) if source else None
    if cleaned_source and cleaned_source != existing.source:
        existing.source = cleaned_source
        updated = True
    if isinstance(raw, dict):
        existing.raw_json = raw
        updated = True
    if updated or hasattr(existing, "last_seen_at"):
        existing.last_seen_at = _now()
    db.add(existing)


def _touch_advertiser_store_link(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: Optional[str],
    store_id: Optional[str],
    relation_type: Optional[str] = None,
    store_authorized_bc_id: Optional[str] = None,
    bc_id_hint: Optional[str] = None,
    source: Optional[str] = None,
    raw: Optional[dict] = None,
) -> None:
    adv = _normalize_identifier(advertiser_id)
    store = _normalize_identifier(store_id)
    if not adv or not store:
        return

    relation = _normalize_relation_type(relation_type)
    authorized_bc = _normalize_identifier(store_authorized_bc_id)
    bc_hint = _normalize_identifier(bc_id_hint)

    existing = (
        db.query(TTBAdvertiserStoreLink)
        .filter(
            TTBAdvertiserStoreLink.workspace_id == workspace_id,
            TTBAdvertiserStoreLink.auth_id == auth_id,
            TTBAdvertiserStoreLink.advertiser_id == adv,
            TTBAdvertiserStoreLink.store_id == store,
        )
        .one_or_none()
    )

    if existing is None:
        row = TTBAdvertiserStoreLink(
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=adv,
            store_id=store,
            relation_type=relation,
            store_authorized_bc_id=authorized_bc,
            bc_id_hint=bc_hint,
            source=_clean_str(source) or None,
            raw_json=raw if isinstance(raw, dict) else None,
        )
        if hasattr(row, "last_seen_at"):
            row.last_seen_at = _now()
        db.add(row)
        return

    changed = False
    if _relation_rank(existing.relation_type) > _relation_rank(relation):
        existing.relation_type = relation
        changed = True
    if authorized_bc and authorized_bc != existing.store_authorized_bc_id:
        existing.store_authorized_bc_id = authorized_bc
        changed = True
    if bc_hint and bc_hint != existing.bc_id_hint:
        existing.bc_id_hint = bc_hint
        changed = True
    cleaned_source = _clean_str(source) if source else None
    if cleaned_source and cleaned_source != existing.source:
        existing.source = cleaned_source
        changed = True
    if isinstance(raw, dict):
        existing.raw_json = raw
        changed = True
    if changed or hasattr(existing, "last_seen_at"):
        existing.last_seen_at = _now()
    db.add(existing)


def _upsert_bc(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    # 严格按官方字段（不做历史兼容）：bc_id, bc_name, status, timezone, country_code, owner_user_id, create_time, update_time, version
    bc_id = str(_pick(item, "bc_id"))
    if not bc_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        bc_id=bc_id,
        name=_clean_str(_pick(item, "bc_name", "name")),
        status=_clean_str(_pick(item, "status")),
        timezone=_clean_str(_pick(item, "timezone")),
        country_code=_clean_str(_pick(item, "country_code")),
        owner_user_id=_clean_str(_pick(item, "owner_user_id")),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=_clean_str(_pick(item, "version", default="")),
        raw_json=item,
    )
    _upsert(
        db,
        TTBBusinessCenter,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "bc_id"),
        update_columns=(
            "name",
            "status",
            "timezone",
            "country_code",
            "owner_user_id",
            "ext_created_time",
            "ext_updated_time",
            "sync_rev",
            "raw_json",
        ),
    )
    return True


def _upsert_adv(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    # 严格字段：advertiser_id, bc_id, name, display_name, status, industry, currency, timezone, country_code, create_time, update_time, version
    advertiser_id = str(_pick(item, "advertiser_id"))
    if not advertiser_id:
        return False
    supports_display_timezone = advertiser_display_timezone_supported(db)
    owner_bc = _normalize_identifier(_pick(item, "bc_id", "owner_bc_id"))
    relation_hint = _normalize_relation_type(_pick(item, "relation_type"))
    relation_for_link = relation_hint if relation_hint != "UNKNOWN" else "OWNER"
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        bc_id=owner_bc,
        name=_clean_str(_pick(item, "name", "advertiser_name")),
        display_name=_clean_str(_pick(item, "display_name", "advertiser_name", "name")),
        status=_clean_str(_pick(item, "status")),
        industry=_clean_str(_pick(item, "industry")),
        currency=_clean_str(_pick(item, "currency")),
        timezone=_clean_str(_pick(item, "timezone")),
        display_timezone=_clean_str(_pick(item, "display_timezone")),
        country_code=_clean_str(_pick(item, "country_code")),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=_clean_str(_pick(item, "version", default="")),
        raw_json=item,
    )
    if not supports_display_timezone:
        values.pop("display_timezone", None)
    if supports_display_timezone:
        values["display_timezone"] = _pick(item, "display_timezone")
    _upsert(
        db,
        TTBAdvertiser,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "advertiser_id"),
        update_columns=tuple(
            column
            for column in (
                "bc_id",
                "name",
                "display_name",
                "status",
                "industry",
                "currency",
                "timezone",
                "display_timezone",
                "country_code",
                "ext_created_time",
                "ext_updated_time",
                "sync_rev",
                "raw_json",
            )
            if supports_display_timezone or column != "display_timezone"
        ),
    )
    if owner_bc:
        _touch_bc_advertiser_link(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            bc_id=owner_bc,
            relation_type=relation_for_link,
            source="advertiser.get",
            raw=item if isinstance(item, dict) else None,
        )
    return True


def _apply_advertiser_info(
    row: TTBAdvertiser, info: dict, *, allow_display_timezone: bool
) -> bool:
    changed = False

    def _set(attr: str, value: Any) -> None:
        nonlocal changed
        if value is None:
            return
        if isinstance(value, str):
            value = value.strip()
        if value == "":
            return
        if attr == "display_timezone" and not allow_display_timezone:
            return
        if getattr(row, attr) != value:
            setattr(row, attr, value)
            changed = True

    preferred_name = _pick(info, "name", "advertiser_name")
    display = _pick(info, "display_name", "advertiser_name", "name")
    _set("name", preferred_name)
    _set("display_name", display)
    _set("status", _pick(info, "status"))
    _set("industry", _pick(info, "industry"))
    _set("currency", _pick(info, "currency"))
    _set("timezone", _pick(info, "timezone"))
    _set("display_timezone", _pick(info, "display_timezone"))

    country = _pick(info, "country_code", "country", "region_code")
    _set("country_code", country)

    owner_bc = _normalize_identifier(_pick(info, "owner_bc_id", "bc_id"))
    if owner_bc:
        _set("bc_id", owner_bc)

    if info:
        row.raw_json = info
        changed = True

    row.last_seen_at = _now()
    return changed


def _upsert_store(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    item: dict,
    bc_id: Optional[str] = None,
) -> bool:
    # 严格字段：官方 /store/list/ 字段：store_id, advertiser_id, bc_id, store_name, status, region_code, create_time, update_time, version
    store_id = _pick(item, "store_id")
    if store_id is None:
        return False

    normalized_bc_id = _normalize_identifier(bc_id or _pick(item, "bc_id"))
    advertiser_id = _normalize_identifier(_pick(item, "advertiser_id"))
    store_type_raw = _pick(item, "store_type", "type")
    store_code_raw = _pick(item, "store_code", "code")
    authorized_bc = _normalize_identifier(
        _pick(item, "store_authorized_bc_id", "authorized_bc_id", "bc_id")
    )
    name = _clean_str(_pick(item, "store_name", "name"))
    status = _clean_str(_pick(item, "status"))
    region_code = _clean_str(_pick(item, "region_code", "country_code"))

    ext_created_time = _parse_dt(_pick(item, "create_time"))
    ext_updated_time = _parse_dt(_pick(item, "update_time"))
    version = _pick(item, "version")
    sync_rev = str(version) if version is not None else ""

    existing = (
        db.query(TTBStore)
        .filter(
            TTBStore.workspace_id == workspace_id,
            TTBStore.auth_id == auth_id,
            TTBStore.store_id == str(store_id),
        )
        .one_or_none()
    )

    store_type_clean = _clean_str(store_type_raw) or "TIKTOK_SHOP"
    store_code_clean = _clean_str(store_code_raw)
    authorized_bc_clean = authorized_bc

    if existing is None:
        row = TTBStore(
            workspace_id=workspace_id,
            auth_id=auth_id,
            store_id=str(store_id),
            advertiser_id=advertiser_id,
            bc_id=normalized_bc_id,
            name=name,
            status=status,
            region_code=region_code,
            store_type=store_type_clean,
            store_code=store_code_clean,
            store_authorized_bc_id=authorized_bc_clean,
            ext_created_time=ext_created_time,
            ext_updated_time=ext_updated_time,
            sync_rev=sync_rev,
            raw_json=item,
        )
        if hasattr(row, "last_seen_at"):
            row.last_seen_at = _now()
        db.add(row)
    else:
        existing.advertiser_id = advertiser_id
        existing.name = name
        existing.status = status
        existing.region_code = region_code
        existing.store_type = store_type_clean or existing.store_type or "TIKTOK_SHOP"
        existing.store_code = store_code_clean
        if authorized_bc_clean is not None:
            existing.store_authorized_bc_id = authorized_bc_clean
        existing.ext_created_time = ext_created_time
        existing.ext_updated_time = ext_updated_time
        existing.sync_rev = sync_rev
        existing.raw_json = item
        if normalized_bc_id and normalized_bc_id != existing.bc_id:
            existing.bc_id = normalized_bc_id
        existing.last_seen_at = _now()
        db.add(existing)
    if advertiser_id:
        _touch_advertiser_store_link(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=str(store_id),
            relation_type="AUTHORIZER" if authorized_bc_clean else "UNKNOWN",
            store_authorized_bc_id=authorized_bc_clean,
            bc_id_hint=normalized_bc_id,
            source="store.list",
            raw=item if isinstance(item, dict) else None,
        )
        if authorized_bc_clean:
            _touch_bc_advertiser_link(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                bc_id=authorized_bc_clean,
                relation_type="AUTHORIZER",
                source="store.list",
                raw=item if isinstance(item, dict) else None,
            )
        if normalized_bc_id:
            if authorized_bc_clean and authorized_bc_clean == normalized_bc_id:
                inferred = "OWNER"
            elif authorized_bc_clean:
                inferred = "PARTNER"
            else:
                inferred = "UNKNOWN"
            _touch_bc_advertiser_link(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                bc_id=normalized_bc_id,
                relation_type=inferred,
                source="store.list",
                raw=item if isinstance(item, dict) else None,
            )
    return True


def _upsert_product(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    # 严格字段：product_id, store_id, title, status, currency, price/stock, create_time, update_time, version
    product_id = _pick(item, "product_id")
    if product_id is None:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        product_id=str(product_id),
        store_id=str(_pick(item, "store_id")) if _pick(item, "store_id") is not None else None,
        title=_clean_str(_pick(item, "title")),
        status=_clean_str(_pick(item, "status")),
        currency=_clean_str(_pick(item, "currency")),
        price=_pick(item, "price"),
        stock=_pick(item, "stock"),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=_clean_str(_pick(item, "version", default="")),
        raw_json=item,
    )
    _upsert(
        db,
        TTBProduct,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "product_id"),
        update_columns=(
            "store_id",
            "title",
            "status",
            "currency",
            "price",
            "stock",
            "ext_created_time",
            "ext_updated_time",
            "sync_rev",
            "raw_json",
        ),
    )
    return True


# --------------------------- 同步服务 ---------------------------
def _eligibility_to_api(value: Optional[Literal["gmv_max", "ads", "all"]]) -> Optional[Literal["GMV_MAX", "CUSTOM_STORE_ADS"]]:
    if not value or value == "all":
        return None
    if value == "gmv_max":
        return "GMV_MAX"
    if value == "ads":
        return "CUSTOM_STORE_ADS"
    return None


class TTBSyncService:
    """
    原子同步服务（幂等）：
    - sync_bc / sync_advertisers / sync_stores / sync_products / sync_all
    - 只使用官方字段，不做历史兼容。
    """

    def __init__(self, db: Session, client: TTBApiClient, *, workspace_id: int, auth_id: int):
        self.db = db
        self.client = client
        self.workspace_id = int(workspace_id)
        self.auth_id = int(auth_id)

    def _cursor_checkpoint(self, cursor: TTBSyncCursor, *, last_rev: str | None) -> None:
        cursor.last_rev = last_rev or str(int(datetime.now(timezone.utc).timestamp()))
        cursor.since_time = datetime.now(timezone.utc)
        self.db.add(cursor)

    def _collect_ids(self, model, column) -> Set[str]:
        rows = (
            self.db.query(column)
            .filter(
                model.workspace_id == self.workspace_id,
                model.auth_id == self.auth_id,
            )
            .all()
        )
        result: Set[str] = set()
        for value, in rows:
            if value is None:
                continue
            result.add(str(value))
        return result

    @staticmethod
    def _diff_sets(before: Set[str], after: Set[str]) -> Dict[str, int]:
        added = len(after - before)
        removed = len(before - after)
        unchanged = len(before & after)
        return {"added": added, "removed": removed, "unchanged": unchanged}

    async def sync_meta(self, *, page_size: int = 50) -> tuple[list[tuple[str, dict, int]], Dict[str, Dict[str, int]]]:
        phases: list[tuple[str, dict, int]] = []
        summary: Dict[str, Dict[str, int]] = {}

        resources = [
            ("bc", TTBBusinessCenter, TTBBusinessCenter.bc_id, self.sync_bc),
            ("advertisers", TTBAdvertiser, TTBAdvertiser.advertiser_id, self.sync_advertisers),
            ("stores", TTBStore, TTBStore.store_id, self.sync_stores),
        ]

        for scope, model, column, runner in resources:
            before = self._collect_ids(model, column)
            started = datetime.now(timezone.utc)
            stats = await runner(page_size=page_size)
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            after = self._collect_ids(model, column)
            summary_key = "bc" if scope == "bc" else scope
            summary[summary_key] = self._diff_sets(before, after)
            phases.append((scope, stats, duration_ms))

        return phases, summary

    async def sync_bc(self, *, page_size: int = 50) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="bc"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        async for item in self.client.iter_business_centers(page_size=page_size):
            stats["fetched"] += 1
            ok = _upsert_bc(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            if ok:
                stats["upserts"] += 1
                rev = _pick(item, "version")
                if rev:
                    latest_rev = str(rev)
            else:
                stats["skipped"] += 1
        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "bc", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def _hydrate_advertisers(
        self,
        *,
        advertiser_ids: Optional[Iterable[str]] = None,
        only_missing: bool = False,
    ) -> dict:
        query = (
            self.db.query(TTBAdvertiser)
            .filter(TTBAdvertiser.workspace_id == self.workspace_id)
            .filter(TTBAdvertiser.auth_id == self.auth_id)
        )
        normalized_ids: Optional[Set[str]] = None
        if advertiser_ids is not None:
            normalized_ids = set()
            for value in advertiser_ids:
                normalized = _normalize_identifier(value)
                if normalized:
                    normalized_ids.add(normalized)
            if not normalized_ids:
                return {"batches": 0, "updates": 0}
            query = query.filter(TTBAdvertiser.advertiser_id.in_(normalized_ids))
        if only_missing:
            query = query.filter(
                or_(
                    TTBAdvertiser.name.is_(None),
                    TTBAdvertiser.timezone.is_(None),
                    TTBAdvertiser.bc_id.is_(None),
                )
            )

        rows: List[TTBAdvertiser] = query.all()
        mapping = {
            str(row.advertiser_id): row
            for row in rows
            if row and row.advertiser_id is not None
        }
        ids = [key for key in mapping.keys() if key]
        if not ids:
            return {"batches": 0, "updates": 0}

        batches = 0
        updates = 0
        allow_display_timezone = advertiser_display_timezone_supported(self.db)
        for chunk in _chunked(ids, _ADVERTISER_INFO_BATCH_SIZE):
            try:
                info_items = await self.client.fetch_advertiser_info(
                    advertiser_ids=chunk,
                    fields=_ADVERTISER_INFO_FIELDS,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to fetch advertiser info",  # pragma: no cover - logging path
                    extra={
                        "provider": "tiktok-business",
                        "workspace_id": int(self.workspace_id),
                        "auth_id": int(self.auth_id),
                        "advertiser_ids": chunk,
                    },
                )
                continue

            batches += 1
            if not info_items:
                continue
            for info in info_items:
                advertiser_id = _normalize_identifier(_pick(info, "advertiser_id", "advertiserId"))
                if not advertiser_id:
                    continue
                owner_bc = _normalize_identifier(_pick(info, "owner_bc_id", "bc_id"))
                if owner_bc:
                    _touch_bc_advertiser_link(
                        self.db,
                        workspace_id=self.workspace_id,
                        auth_id=self.auth_id,
                        advertiser_id=advertiser_id,
                        bc_id=owner_bc,
                        relation_type="OWNER",
                        source="advertiser.info",
                        raw=info if isinstance(info, dict) else None,
                    )
                row = mapping.get(advertiser_id)
                if not row:
                    continue
                if _apply_advertiser_info(
                    row,
                    info,
                    allow_display_timezone=allow_display_timezone,
                ):
                    updates += 1
                    self.db.add(row)

        if updates:
            self.db.flush()

        return {"batches": batches, "updates": updates}

    async def sync_advertisers(self, *, page_size: int = 50) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="advertiser"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        fetched_ids: Set[str] = set()
        try:
            fetch_limit = int(page_size)
        except Exception:
            fetch_limit = 0
        fetch_limit = max(fetch_limit, 0)

        async for item in self.client.iter_advertisers(page_size=_ADVERTISER_INFO_BATCH_SIZE):
            stats["fetched"] += 1
            adv_id = _normalize_identifier(_pick(item, "advertiser_id"))
            if adv_id:
                fetched_ids.add(adv_id)
            ok = _upsert_adv(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            if ok:
                stats["upserts"] += 1
                rev = _pick(item, "version")
                if rev:
                    latest_rev = str(rev)
            else:
                stats["skipped"] += 1
            if fetch_limit and stats["fetched"] >= fetch_limit:
                break
        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        info_stats = await self._hydrate_advertisers(
            advertiser_ids=fetched_ids if fetched_ids else None
        )
        stats.update(
            {
                "info_batches": info_stats.get("batches", 0),
                "info_updates": info_stats.get("updates", 0),
            }
        )
        return {"resource": "advertisers", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def repair_advertisers(
        self, *, advertiser_ids: Optional[Iterable[str]] = None
    ) -> dict:
        return await self._hydrate_advertisers(advertiser_ids=advertiser_ids, only_missing=True)

    async def sync_stores(self, *, page_size: int = 50) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="store"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}

        latest_rev: str | None = cursor.last_rev
        advs: List[TTBAdvertiser] = (
            self.db.query(TTBAdvertiser)
            .filter(
                TTBAdvertiser.workspace_id == self.workspace_id,
                TTBAdvertiser.auth_id == self.auth_id,
            )
            .all()
        )

        for adv in advs:
            if not adv or not adv.advertiser_id:
                continue
            async for item in self.client.iter_stores(
                advertiser_id=str(adv.advertiser_id),
                page_size=page_size,
            ):
                stats["fetched"] += 1
                bc_hint = (
                    item.get("store_authorized_bc_id")
                    or item.get("authorized_bc_id")
                    or item.get("bc_id")
                )
                ok = _upsert_store(
                    self.db,
                    workspace_id=self.workspace_id,
                    auth_id=self.auth_id,
                    item=item,
                    bc_id=bc_hint,
                )
                if ok:
                    stats["upserts"] += 1
                    rev = _pick(item, "version")
                    if rev:
                        latest_rev = str(rev)
                else:
                    stats["skipped"] += 1

        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "stores", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_products(
        self,
        *,
        page_size: int = 50,
        store_id: str | None = None,
        product_eligibility: Optional[Literal["gmv_max", "ads", "all"]] = "gmv_max",
    ) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="product"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev

        stores: List[TTBStore]
        if store_id:
            row = (
                self.db.query(TTBStore)
                .filter(
                    TTBStore.workspace_id == self.workspace_id,
                    TTBStore.auth_id == self.auth_id,
                    TTBStore.store_id == str(store_id),
                )
                .one_or_none()
            )
            stores = [row] if row else []
        else:
            stores = (
                self.db.query(TTBStore)
                .filter(
                    TTBStore.workspace_id == self.workspace_id,
                    TTBStore.auth_id == self.auth_id,
                )
                .all()
            )

        eligibility_api = _eligibility_to_api(product_eligibility)

        for s in stores:
            if not s or not s.store_id:
                continue

            bc_id = _normalize_identifier(s.bc_id)
            if not bc_id:
                raw_data = getattr(s, "raw_json", None)
                raw = raw_data if isinstance(raw_data, dict) else {}
                bc_from_raw = (
                    raw.get("store_authorized_bc_id")
                    or raw.get("authorized_bc_id")
                    or raw.get("bc_id")
                )
                bc_id = _normalize_identifier(bc_from_raw)
                if bc_id:
                    s.bc_id = bc_id
                    s.last_seen_at = _now()
                    self.db.add(s)
                    self.db.flush()
                else:
                    logger.warning(
                        "ttb_sync.store_missing_bc_id",
                        extra={
                            "workspace_id": self.workspace_id,
                            "auth_id": self.auth_id,
                            "store_id": s.store_id,
                        },
                    )
            async for item in self.client.iter_products(
                store_id=str(s.store_id),
                bc_id=str(bc_id) if bc_id else None,
                advertiser_id=str(s.advertiser_id) if s.advertiser_id else None,
                page_size=page_size,
                eligibility=eligibility_api,
            ):
                stats["fetched"] += 1
                ok = _upsert_product(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                if ok:
                    stats["upserts"] += 1
                    rev = _pick(item, "version")
                    if rev:
                        latest_rev = str(rev)
                else:
                    stats["skipped"] += 1

        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "products", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_all(
        self,
        *,
        page_size: int = 50,
        product_page_size: Optional[int] = None,
        product_eligibility: Optional[Literal["gmv_max", "ads", "all"]] = "gmv_max",
    ) -> Dict[str, Dict[str, Any]]:
        phases: list[Tuple[str, dict]] = []
        logger.info(
            "ttb_sync.start",
            extra={"workspace_id": self.workspace_id, "auth_id": self.auth_id, "scope": "all"},
        )
        phases.append(("bc", await self.sync_bc(page_size=page_size)))
        phases.append(("advertisers", await self.sync_advertisers(page_size=page_size)))
        phases.append(("stores", await self.sync_stores(page_size=page_size)))
        phases.append(
            (
                "products",
                await self.sync_products(
                    page_size=product_page_size or page_size,
                    product_eligibility=product_eligibility,
                ),
            )
        )
        return {name: stats for name, stats in phases}


async def run_sync_all(
    service: TTBSyncService,
    *,
    page_size: int = 50,
    product_page_size: Optional[int] = None,
    product_eligibility: Optional[Literal["gmv_max", "ads", "all"]] = "gmv_max",
) -> Dict[str, Dict[str, Any]]:
    try:
        return await service.sync_all(
            page_size=page_size,
            product_page_size=product_page_size,
            product_eligibility=product_eligibility,
        )
    finally:
        with contextlib.suppress(Exception):
            await service.client.aclose()


