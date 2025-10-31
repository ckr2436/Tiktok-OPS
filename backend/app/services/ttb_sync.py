# app/services/ttb_sync.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple, Literal
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


def _upsert_bc(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> bool:
    # 严格按官方字段（不做历史兼容）：bc_id, bc_name, status, timezone, country_code, owner_user_id, create_time, update_time, version
    bc_id = str(_pick(item, "bc_id"))
    if not bc_id:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        bc_id=bc_id,
        name=_pick(item, "bc_name"),
        status=_pick(item, "status"),
        timezone=_pick(item, "timezone"),
        country_code=_pick(item, "country_code"),
        owner_user_id=_pick(item, "owner_user_id"),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=str(_pick(item, "version", default="")),
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
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        bc_id=_pick(item, "bc_id"),
        name=_pick(item, "name"),
        display_name=_pick(item, "display_name"),
        status=_pick(item, "status"),
        industry=_pick(item, "industry"),
        currency=_pick(item, "currency"),
        timezone=_pick(item, "timezone"),
        country_code=_pick(item, "country_code"),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=str(_pick(item, "version", default="")),
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
    # 严格字段：store_id/shop_id 统一使用 store_id；返回字段以官方 /store/list/ 为准：store_id, advertiser_id, bc_id, store_name, status, region_code, create_time, update_time, version
    store_id = _pick(item, "store_id")
    if store_id is None:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        shop_id=str(store_id),
        advertiser_id=_pick(item, "advertiser_id"),
        bc_id=_pick(item, "bc_id"),
        name=_pick(item, "store_name"),
        status=_pick(item, "status"),
        region_code=_pick(item, "region_code"),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=str(_pick(item, "version", default="")),
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
    # 严格字段：product_id, store_id, title, status, currency, price/stock, create_time, update_time, version
    product_id = _pick(item, "product_id")
    if product_id is None:
        return False
    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        product_id=str(product_id),
        shop_id=str(_pick(item, "store_id")) if _pick(item, "store_id") is not None else None,
        title=_pick(item, "title"),
        status=_pick(item, "status"),
        currency=_pick(item, "currency"),
        price=_pick(item, "price"),
        stock=_pick(item, "stock"),
        ext_created_time=_parse_dt(_pick(item, "create_time")),
        ext_updated_time=_parse_dt(_pick(item, "update_time")),
        sync_rev=str(_pick(item, "version", default="")),
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
def _eligibility_to_api(value: Optional[Literal["gmv_max", "ads", "all"]]) -> Optional[Literal["GMV_MAX", "CUSTOM_SHOP_ADS"]]:
    if not value or value == "all":
        return None
    if value == "gmv_max":
        return "GMV_MAX"
    if value == "ads":
        return "CUSTOM_SHOP_ADS"
    return None


from app.services.oauth_ttb import get_credentials_for_auth_id

class TTBSyncService:
    """
    原子同步服务（幂等）：
    - sync_bc / sync_advertisers / sync_shops / sync_products / sync_all
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

    async def sync_advertisers(self, *, page_size: int = 50) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="advertiser"
        )
        stats = {"fetched": 0, "upserts": 0, "skipped": 0}
        latest_rev: str | None = cursor.last_rev
        app_id, app_secret, _ = get_credentials_for_auth_id(self.db, auth_id=self.auth_id)
        async for item in self.client.iter_advertisers(app_id=app_id, secret=app_secret, page_size=page_size):
            stats["fetched"] += 1
            ok = _upsert_adv(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            if ok:
                stats["upserts"] += 1
                rev = _pick(item, "version")
                if rev:
                    latest_rev = str(rev)
            else:
                stats["skipped"] += 1
        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "advertisers", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_shops(self, *, page_size: int = 50) -> dict:
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
                advertiser_id=str(adv.advertiser_id),
                page_size=page_size,
            ):
                stats["fetched"] += 1
                ok = _upsert_shop(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                if ok:
                    stats["upserts"] += 1
                    rev = _pick(item, "version")
                    if rev:
                        latest_rev = str(rev)
                else:
                    stats["skipped"] += 1

        self._cursor_checkpoint(cursor, last_rev=latest_rev)
        return {"resource": "shops", **stats, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_products(
        self,
        *,
        page_size: int = 50,
        shop_id: str | None = None,
        product_eligibility: Optional[Literal["gmv_max", "ads", "all"]] = "gmv_max",
    ) -> dict:
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

        eligibility_api = _eligibility_to_api(product_eligibility)

        for s in shops:
            if not s or not s.shop_id or not s.bc_id:
                continue
            async for item in self.client.iter_products(
                bc_id=str(s.bc_id),
                store_id=str(s.shop_id),
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
        phases.append(("shops", await self.sync_shops(page_size=page_size)))
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


