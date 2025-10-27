# app/services/ttb_sync.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple
import logging
import contextlib

logger = logging.getLogger("gmv.ttb.sync")

from sqlalchemy import text, and_
from sqlalchemy.orm import Session
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.data.models.ttb_entities import (
    TTBSyncCursor,
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBShop,
    TTBProduct,
)
from app.services.ttb_api import TTBApiClient


# --------------------------- 工具：字段提取与时间解析 ---------------------------
def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


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


# --------------------------- UPSERTs ---------------------------
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


def _upsert_bc(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    bc_id = str(_pick(item, "bc_id", "business_center_id", "id", "bcId"))
    if not bc_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        bc_id=bc_id,
        name=_pick(item, "bc_name", "name"),
        status=_pick(item, "status"),
        timezone=_pick(item, "timezone", "time_zone"),
        country_code=_pick(item, "country_code", "country"),
        owner_user_id=_pick(item, "owner_user_id", "owner_id"),
        ext_created_time=_parse_dt(_pick(item, "create_time", "created_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time", "updated_time", "last_modified_time")),
        sync_rev=str(_pick(item, "sync_rev", "rev", "version", default="")),
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
    advertiser_id = str(_pick(item, "advertiser_id", "id"))
    if not advertiser_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        bc_id=_pick(item, "bc_id", "business_center_id"),
        name=_pick(item, "name", "advertiser_name"),
        display_name=_pick(item, "display_name"),
        status=_pick(item, "status"),
        industry=_pick(item, "industry"),
        currency=_pick(item, "currency"),
        timezone=_pick(item, "timezone", "time_zone"),
        country_code=_pick(item, "country_code", "country"),
        ext_created_time=_parse_dt(_pick(item, "create_time", "created_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time", "updated_time", "last_modified_time")),
        sync_rev=str(_pick(item, "sync_rev", "rev", "version", default="")),
        raw_json=item,
    )
    _upsert(
        db,
        TTBAdvertiser,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "advertiser_id"),
        update_columns=(
            "bc_id",
            "name",
            "display_name",
            "status",
            "industry",
            "currency",
            "timezone",
            "country_code",
            "ext_created_time",
            "ext_updated_time",
            "sync_rev",
            "raw_json",
        ),
    )
    return True


def _upsert_shop(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    shop_id = str(_pick(item, "shop_id", "store_id", "id"))
    if not shop_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        shop_id=shop_id,
        advertiser_id=_pick(item, "advertiser_id"),
        bc_id=_pick(item, "bc_id", "business_center_id"),
        name=_pick(item, "name", "shop_name", "store_name"),
        status=_pick(item, "status"),
        region_code=_pick(item, "region_code", "region", "country", "market"),
        ext_created_time=_parse_dt(_pick(item, "create_time", "created_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time", "updated_time", "last_modified_time")),
        sync_rev=str(_pick(item, "sync_rev", "rev", "version", default="")),
        raw_json=item,
    )
    _upsert(
        db,
        TTBShop,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "shop_id"),
        update_columns=(
            "advertiser_id",
            "bc_id",
            "name",
            "status",
            "region_code",
            "ext_created_time",
            "ext_updated_time",
            "sync_rev",
            "raw_json",
        ),
    )
    return True


def _upsert_product(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    product_id = str(_pick(item, "product_id", "id"))
    if not product_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        product_id=product_id,
        shop_id=_pick(item, "shop_id", "store_id"),
        title=_pick(item, "title", "name"),
        status=_pick(item, "status"),
        currency=_pick(item, "currency"),
        price=_pick(item, "price", "sale_price", "min_price"),
        stock=_pick(item, "stock", "inventory"),
        ext_created_time=_parse_dt(_pick(item, "create_time", "created_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time", "updated_time", "last_modified_time")),
        sync_rev=str(_pick(item, "sync_rev", "rev", "version", default="")),
        raw_json=item,
    )
    _upsert(
        db,
        TTBProduct,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "product_id"),
        update_columns=(
            "shop_id",
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
class TTBSyncService:
    """
    原子同步服务（幂等）：
    - sync_bc / sync_advertisers / sync_shops / sync_products
    - 每步分页遍历 → UPSERT → 推进 cursor.last_rev
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

    async def sync_bc(self, *, limit: int = 200) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="bc"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        async for item in self.client.iter_business_centers(limit=limit):
            stats["fetched"] += 1
            ok = _upsert_bc(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            if ok:
                stats["upserts"] += 1
                rev = _pick(item, "sync_rev", "rev", "version")
                if rev:
                    latest_rev = str(rev)
            else:
                stats["skipped"] += 1
        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "bc", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_advertisers(
        self, *, limit: int = 200, app_id: Optional[str] = None, secret: Optional[str] = None
    ) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="advertiser"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        async for item in self.client.iter_advertisers(limit=limit, app_id=app_id, secret=secret):
            stats["fetched"] += 1
            ok = _upsert_adv(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            if ok:
                stats["upserts"] += 1
                rev = _pick(item, "sync_rev", "rev", "version")
                if rev:
                    latest_rev = str(rev)
            else:
                stats["skipped"] += 1
        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "advertisers", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_shops(self, *, limit: int = 200) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="shop"
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
            async for item in self.client.iter_shops(
                advertiser_id=str(adv.advertiser_id), page_size=min(limit, 1000)
            ):
                stats["fetched"] += 1
                ok = _upsert_shop(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                if ok:
                    stats["upserts"] += 1
                    rev = _pick(item, "sync_rev", "rev", "version")
                    if rev:
                        latest_rev = str(rev)
                else:
                    stats["skipped"] += 1

        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "shops", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_products(self, *, limit: int = 200, shop_id: str | None = None) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="product"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        shops: List[TTBShop]
        if shop_id:
            row = (
                self.db.query(TTBShop)
                .filter(
                    TTBShop.workspace_id == self.workspace_id,
                    TTBShop.auth_id == self.auth_id,
                    TTBShop.shop_id == str(shop_id),
                )
                .one_or_none()
            )
            shops = [row] if row else []
        else:
            shops = (
                self.db.query(TTBShop)
                .filter(
                    TTBShop.workspace_id == self.workspace_id,
                    TTBShop.auth_id == self.auth_id,
                )
                .all()
            )

        for s in shops:
            if not s or not s.shop_id or not s.bc_id:
                continue
            async for item in self.client.iter_products(
                bc_id=str(s.bc_id),
                store_id=str(s.shop_id),
                advertiser_id=str(s.advertiser_id) if s.advertiser_id else None,
                page_size=min(limit, 1000),
            ):
                stats["fetched"] += 1
                ok = _upsert_product(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                if ok:
                    stats["upserts"] += 1
                    rev = _pick(item, "sync_rev", "rev", "version")
                    if rev:
                        latest_rev = str(rev)
                else:
                    stats["skipped"] += 1

        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "products", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_all(
        self,
        *,
        limit: int = 200,
        app_id: Optional[str] = None,
        secret: Optional[str] = None,
        product_limit: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        phases: list[Tuple[str, dict]] = []
        logger.info(
            "ttb_sync.start", extra={"workspace_id": self.workspace_id, "auth_id": self.auth_id, "scope": "all"}
        )
        phases.append(("bc", await self.sync_bc(limit=limit)))
        phases.append(
            (
                "advertisers",
                await self.sync_advertisers(limit=limit, app_id=app_id, secret=secret),
            )
        )
        phases.append(("shops", await self.sync_shops(limit=limit)))
        phases.append(("products", await self.sync_products(limit=product_limit or limit)))

        return {name: stats for name, stats in phases}


async def run_sync_all(
    service: TTBSyncService,
    *,
    limit: int = 200,
    app_id: Optional[str] = None,
    secret: Optional[str] = None,
    product_limit: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    try:
        return await service.sync_all(limit=limit, app_id=app_id, secret=secret, product_limit=product_limit)
    finally:
        with contextlib.suppress(Exception):
            await service.client.aclose()

