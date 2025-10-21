# app/services/ttb_sync.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, List, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.data.models.ttb_entities import (
    TTBSyncCursor,
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBShop,
    TTBProduct,
    TTBAdgroup,
)
from app.services.ttb_api import TTBApiClient
from app.services.oauth_ttb import get_access_token_for_auth_id
from app.core.config import settings


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
def _upsert_bc(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> None:
    bc_id = str(_pick(item, "bc_id", "business_center_id", "id", "bcId"))
    if not bc_id:
        return
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
    stmt = mysql_insert(TTBBusinessCenter).values(values)
    ondup = stmt.on_duplicate_key_update(
        name=stmt.inserted.name,
        status=stmt.inserted.status,
        timezone=stmt.inserted.timezone,
        country_code=stmt.inserted.country_code,
        owner_user_id=stmt.inserted.owner_user_id,
        ext_created_time=stmt.inserted.ext_created_time,
        ext_updated_time=stmt.inserted.ext_updated_time,
        sync_rev=stmt.inserted.sync_rev,
        raw_json=stmt.inserted.raw_json,
        last_seen_at=text("CURRENT_TIMESTAMP(6)"),
    )
    db.execute(ondup)


def _upsert_adv(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> None:
    advertiser_id = str(_pick(item, "advertiser_id", "id"))
    if not advertiser_id:
        return
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
    stmt = mysql_insert(TTBAdvertiser).values(values)
    ondup = stmt.on_duplicate_key_update(
        bc_id=stmt.inserted.bc_id,
        name=stmt.inserted.name,
        display_name=stmt.inserted.display_name,
        status=stmt.inserted.status,
        industry=stmt.inserted.industry,
        currency=stmt.inserted.currency,
        timezone=stmt.inserted.timezone,
        country_code=stmt.inserted.country_code,
        ext_created_time=stmt.inserted.ext_created_time,
        ext_updated_time=stmt.inserted.ext_updated_time,
        sync_rev=stmt.inserted.sync_rev,
        raw_json=stmt.inserted.raw_json,
        last_seen_at=text("CURRENT_TIMESTAMP(6)"),
    )
    db.execute(ondup)


def _upsert_shop(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> None:
    shop_id = str(_pick(item, "shop_id", "store_id", "id"))
    if not shop_id:
        return
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
    stmt = mysql_insert(TTBShop).values(values)
    ondup = stmt.on_duplicate_key_update(
        advertiser_id=stmt.inserted.advertiser_id,
        bc_id=stmt.inserted.bc_id,
        name=stmt.inserted.name,
        status=stmt.inserted.status,
        region_code=stmt.inserted.region_code,
        ext_created_time=stmt.inserted.ext_created_time,
        ext_updated_time=stmt.inserted.ext_updated_time,
        sync_rev=stmt.inserted.sync_rev,
        raw_json=stmt.inserted.raw_json,
        last_seen_at=text("CURRENT_TIMESTAMP(6)"),
    )
    db.execute(ondup)


def _upsert_adgroup(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> None:
    adgroup_id = str(_pick(item, "adgroup_id", "id"))
    if not adgroup_id:
        return

    advertiser_id = _pick(item, "advertiser_id")
    campaign_id = _pick(item, "campaign_id")

    values = dict(
        workspace_id=workspace_id,
        auth_id=auth_id,
        adgroup_id=adgroup_id,
        advertiser_id=str(advertiser_id) if advertiser_id else None,
        campaign_id=str(campaign_id) if campaign_id else None,
        name=_pick(item, "adgroup_name", "name"),
        operation_status=_pick(item, "operation_status", "status"),
        primary_status=_pick(item, "primary_status"),
        secondary_status=_pick(item, "secondary_status"),
        budget=_pick(item, "budget"),
        budget_mode=_pick(item, "budget_mode"),
        optimization_goal=_pick(item, "optimization_goal"),
        promotion_type=_pick(item, "promotion_type"),
        bid_type=_pick(item, "bid_type"),
        bid_strategy=_pick(item, "bid_strategy"),
        schedule_start_time=_parse_dt(_pick(item, "schedule_start_time")),
        schedule_end_time=_parse_dt(_pick(item, "schedule_end_time")),
        ext_created_time=_parse_dt(_pick(item, "create_time", "created_time")),
        ext_updated_time=_parse_dt(_pick(item, "modify_time", "update_time", "updated_time")),
        raw_json=item,
    )

    stmt = mysql_insert(TTBAdgroup).values(values)
    ondup = stmt.on_duplicate_key_update(
        advertiser_id=stmt.inserted.advertiser_id,
        campaign_id=stmt.inserted.campaign_id,
        name=stmt.inserted.name,
        operation_status=stmt.inserted.operation_status,
        primary_status=stmt.inserted.primary_status,
        secondary_status=stmt.inserted.secondary_status,
        budget=stmt.inserted.budget,
        budget_mode=stmt.inserted.budget_mode,
        optimization_goal=stmt.inserted.optimization_goal,
        promotion_type=stmt.inserted.promotion_type,
        bid_type=stmt.inserted.bid_type,
        bid_strategy=stmt.inserted.bid_strategy,
        schedule_start_time=stmt.inserted.schedule_start_time,
        schedule_end_time=stmt.inserted.schedule_end_time,
        ext_created_time=stmt.inserted.ext_created_time,
        ext_updated_time=stmt.inserted.ext_updated_time,
        raw_json=stmt.inserted.raw_json,
        last_seen_at=text("CURRENT_TIMESTAMP(6)"),
    )
    db.execute(ondup)


def _upsert_product(db: Session, *, workspace_id: int, auth_id: int, item: dict) -> None:
    product_id = str(_pick(item, "product_id", "id"))
    if not product_id:
        return
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
    stmt = mysql_insert(TTBProduct).values(values)
    ondup = stmt.on_duplicate_key_update(
        shop_id=stmt.inserted.shop_id,
        title=stmt.inserted.title,
        status=stmt.inserted.status,
        currency=stmt.inserted.currency,
        price=stmt.inserted.price,
        stock=stmt.inserted.stock,
        ext_created_time=stmt.inserted.ext_created_time,
        ext_updated_time=stmt.inserted.ext_updated_time,
        sync_rev=stmt.inserted.sync_rev,
        raw_json=stmt.inserted.raw_json,
        last_seen_at=text("CURRENT_TIMESTAMP(6)"),
    )
    db.execute(ondup)


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

    async def sync_bc(self, *, limit: int = 200) -> dict:
        cursor = _get_or_create_cursor(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="bc")
        count = 0
        async for item in self.client.iter_business_centers(limit=limit):
            _upsert_bc(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            count += 1
        cursor.last_rev = str(int(datetime.utcnow().timestamp()))
        self.db.add(cursor)
        return {"synced": count, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_advertisers(self, *, limit: int = 200, app_id: Optional[str] = None, secret: Optional[str] = None) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="advertiser"
        )
        count = 0
        async for item in self.client.iter_advertisers(limit=limit, app_id=app_id, secret=secret):
            _upsert_adv(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
            count += 1
        cursor.last_rev = str(int(datetime.utcnow().timestamp()))
        self.db.add(cursor)
        return {"synced": count, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_shops(self, *, limit: int = 200) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="shop"
        )
        # 确保有广告主；若 DB 为空先拉一次
        advs: List[TTBAdvertiser] = self.db.query(TTBAdvertiser).filter(
            TTBAdvertiser.workspace_id == self.workspace_id,
            TTBAdvertiser.auth_id == self.auth_id,
        ).all()
        if not advs:
            # 如果没有广告主，本方法不主动去调用 token API；由上游编排先执行 advertisers
            pass

        count = 0
        for adv in advs:
            if not adv.advertiser_id:
                continue
            async for item in self.client.iter_shops(advertiser_id=str(adv.advertiser_id), page_size=min(limit, 1000)):
                _upsert_shop(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                count += 1

        cursor.last_rev = str(int(datetime.utcnow().timestamp()))
        self.db.add(cursor)
        return {"synced": count, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_adgroups(
        self,
        *,
        limit: int = 200,
        advertiser_id: str | None = None,
        fields: Iterable[str] | None = None,
        filtering: Dict[str, Any] | None = None,
        exclude_field_types_in_response: Iterable[str] | None = None,
    ) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="adgroup"
        )

        fields_list = list(fields) if fields else None
        exclude_list = list(exclude_field_types_in_response) if exclude_field_types_in_response else None

        if advertiser_id:
            row = self.db.query(TTBAdvertiser).filter(
                TTBAdvertiser.workspace_id == self.workspace_id,
                TTBAdvertiser.auth_id == self.auth_id,
                TTBAdvertiser.advertiser_id == str(advertiser_id),
            ).one_or_none()
            advertisers = [row] if row else []
        else:
            advertisers = self.db.query(TTBAdvertiser).filter(
                TTBAdvertiser.workspace_id == self.workspace_id,
                TTBAdvertiser.auth_id == self.auth_id,
            ).all()

        try:
            page_size = int(limit)
        except (TypeError, ValueError):
            page_size = 200
        page_size = max(1, min(page_size, 1000))

        count = 0
        for adv in advertisers:
            if not adv or not adv.advertiser_id:
                continue
            async for item in self.client.iter_adgroups(
                advertiser_id=str(adv.advertiser_id),
                fields=fields_list,
                filtering=filtering,
                exclude_field_types_in_response=exclude_list,
                page_size=page_size,
            ):
                _upsert_adgroup(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                count += 1

        cursor.last_rev = str(int(datetime.utcnow().timestamp()))
        self.db.add(cursor)
        return {"synced": count, "cursor": {"last_rev": cursor.last_rev}}

    async def sync_products(self, *, limit: int = 200, shop_id: str | None = None) -> dict:
        cursor = _get_or_create_cursor(
            self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, resource_type="product"
        )
        shops: List[TTBShop]
        if shop_id:
            row = self.db.query(TTBShop).filter(
                TTBShop.workspace_id == self.workspace_id,
                TTBShop.auth_id == self.auth_id,
                TTBShop.shop_id == str(shop_id),
            ).one_or_none()
            shops = [row] if row else []
        else:
            shops = self.db.query(TTBShop).filter(
                TTBShop.workspace_id == self.workspace_id,
                TTBShop.auth_id == self.auth_id,
            ).all()

        count = 0
        for s in shops:
            if not s or not s.shop_id or not s.bc_id:
                continue
            async for item in self.client.iter_products(
                bc_id=str(s.bc_id),
                store_id=str(s.shop_id),
                advertiser_id=str(s.advertiser_id) if s.advertiser_id else None,
                page_size=min(limit, 1000),
            ):
                _upsert_product(self.db, workspace_id=self.workspace_id, auth_id=self.auth_id, item=item)
                count += 1

        cursor.last_rev = str(int(datetime.utcnow().timestamp()))
        self.db.add(cursor)
        return {"synced": count, "cursor": {"last_rev": cursor.last_rev}}

