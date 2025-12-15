"""GMV Max campaign metrics and snapshot synchronization (PRODUCT & LIVE).

The functions here implement idempotent MySQL upserts for the new GMV Max
campaign fact tables and snapshot caches using TikTok report/get responses.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import sqlite
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxLiveCampaignMetricsDaily,
    GmvmaxLiveCampaignMetricsHourly,
    GmvmaxProductCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
)
from app.data.models.gmvmax_campaign_snapshots import (
    GmvmaxLiveCampaignSnapshotBatch,
    GmvmaxLiveCampaignSnapshotRow,
    GmvmaxProductCampaignSnapshotBatch,
    GmvmaxProductCampaignSnapshotRow,
)
from app.gmvmax.services.gmvmax_value_parser import (
    money_to_cents,
    parse_stat_time_day,
    parse_stat_time_hour,
    to_decimal,
    to_int,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxReportData,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
    TikTokBusinessGMVMaxClient,
)

logger = logging.getLogger("gmv.gmvmax.report_sync")

PAGE_SIZE = 200
MAX_RETRIES = 3
UPSERT_CHUNK_SIZE = 1000

METRIC_UPDATE_FIELDS = {
    GmvmaxProductCampaignMetricsDaily: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
    ],
    GmvmaxProductCampaignMetricsHourly: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
    ],
    GmvmaxLiveCampaignMetricsDaily: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
        "all_shops_orders",
        "all_shops_gross_revenue_cents",
        "all_shops_roi",
        "all_shops_cost_per_order",
        "live_views",
        "cost_per_live_view",
        "live_10s_views",
        "cost_per_10s_live_view",
        "live_follows",
    ],
    GmvmaxLiveCampaignMetricsHourly: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
        "all_shops_orders",
        "all_shops_gross_revenue_cents",
        "all_shops_roi",
        "all_shops_cost_per_order",
        "live_views",
        "cost_per_live_view",
        "live_10s_views",
        "cost_per_10s_live_view",
        "live_follows",
    ],
}


@dataclass
class SyncIdentifiers:
    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str


def _unique_columns(model) -> list[str]:
    unique_sets = []
    for constraint in model.__table__.constraints:
        if isinstance(constraint, UniqueConstraint):
            unique_sets.append([col.name for col in constraint.columns])
    if unique_sets:
        return unique_sets[0]
    if model.__table__.primary_key:
        return [col.name for col in model.__table__.primary_key.columns]
    return []


def _upsert(session: Session, model, values: Mapping[str, Any], update_fields: Sequence[str]):
    unique_cols = _unique_columns(model)
    dialect = session.bind.dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(model).values(values)
        stmt = stmt.on_duplicate_key_update(
            **{field: stmt.inserted[field] for field in update_fields}
        )
        session.execute(stmt)
        return

    if dialect == "sqlite":
        insert_values = dict(values)
        if hasattr(model, "id") and "id" not in insert_values:
            max_id = session.execute(select(func.max(model.id))).scalar() or 0
            insert_values["id"] = int(max_id) + 1
        if hasattr(model, "created_at") and "created_at" not in insert_values:
            insert_values["created_at"] = datetime.utcnow()
        if hasattr(model, "updated_at") and "updated_at" not in insert_values:
            insert_values["updated_at"] = datetime.utcnow()

        if unique_cols:
            stmt = sqlite.insert(model).values(insert_values)
            stmt = stmt.on_conflict_do_update(
                index_elements=unique_cols,
                set_={field: stmt.excluded[field] for field in update_fields},
            )
            session.execute(stmt)
            return
        session.execute(sqlite.insert(model).values(insert_values))
        return

    # SQLite testing fallback if dialect lacks conflict support
    if unique_cols:
        conditions = [getattr(model, col) == values[col] for col in unique_cols if col in values]
        existing = session.execute(select(model).where(*conditions)).scalar_one_or_none()
        if existing:
            for field in update_fields:
                if field in values:
                    setattr(existing, field, values[field])
            session.add(existing)
            session.flush()
            return
    instance_kwargs = dict(values)
    if hasattr(model, "id") and "id" not in instance_kwargs:
        max_id = session.execute(select(func.max(model.id))).scalar() or 0
        instance_kwargs["id"] = int(max_id) + 1
    if session.bind.dialect.name != "mysql":
        if hasattr(model, "created_at") and "created_at" not in instance_kwargs:
            instance_kwargs["created_at"] = datetime.utcnow()
        if hasattr(model, "updated_at") and "updated_at" not in instance_kwargs:
            instance_kwargs["updated_at"] = datetime.utcnow()
    session.add(model(**instance_kwargs))
    session.flush()


def _bulk_upsert_mysql(session: Session, model, values: list[Mapping[str, Any]], update_fields: Sequence[str]):
    if not values:
        return
    stmt = mysql_insert(model).values(values)
    stmt = stmt.on_duplicate_key_update(
        **{field: stmt.inserted[field] for field in update_fields}
    )
    session.execute(stmt)


def _normalize_entry(entry: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = {}
    dimensions = {}
    if isinstance(entry, Mapping):
        metrics = dict(entry.get("metrics") or {})
        dimensions = dict(entry.get("dimensions") or {})
    else:
        metrics = dict(getattr(entry, "metrics", {}) or {})
        dimensions = dict(getattr(entry, "dimensions", {}) or {})
    return metrics, dimensions


async def _fetch_report_pages(client: TikTokBusinessGMVMaxClient, request: GMVMaxReportGetRequest) -> Iterable[Any]:
    page = 1
    while True:
        request.page = page
        request.page_size = PAGE_SIZE
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp: GMVMaxResponse[GMVMaxReportData] = await client.gmv_max_report_get(request)
                break
            except Exception:  # noqa: BLE001
                if attempt >= MAX_RETRIES:
                    logger.exception("gmvmax report/get failed after retries")
                    raise
                await asyncio.sleep(2 ** (attempt - 1))
        data = resp.data or GMVMaxReportData()
        for row in data.list or []:
            yield row
        page_info = data.page_info
        has_more = False
        if page_info:
            has_more = bool(page_info.has_more or page_info.has_next)
            if not has_more and page_info.total_page is not None:
                has_more = page < int(page_info.total_page)
        if not has_more:
            break
        page += 1


def _ensure_catalog_stub(
    session: Session,
    identifiers: SyncIdentifiers,
    campaign_id: str,
    promotion_type: str,
    seen_campaigns: set[str],
):
    if campaign_id in seen_campaigns:
        return
    seen_campaigns.add(campaign_id)
    catalog_model = (
        GmvmaxProductCampaignCatalog if promotion_type == "PRODUCT" else GmvmaxLiveCampaignCatalog
    )
    values = {
        "workspace_id": identifiers.workspace_id,
        "auth_id": identifiers.auth_id,
        "advertiser_id": str(identifiers.advertiser_id),
        "campaign_id": str(campaign_id),
        "store_id": str(identifiers.store_id),
        "list_synced_at": datetime.utcnow(),
        "detail_synced_at": datetime.utcnow(),
    }
    dialect = session.bind.dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(catalog_model).values(values)
        stmt = stmt.on_duplicate_key_update(
            list_synced_at=stmt.inserted.list_synced_at,
            detail_synced_at=stmt.inserted.detail_synced_at,
            store_id=stmt.inserted.store_id,
            updated_at=func.current_timestamp(6),
        )
        session.execute(stmt)
        return

    # SQLite/testing path: tolerate concurrent insert races
    try:
        if hasattr(catalog_model, "id"):
            max_id = session.execute(select(func.max(catalog_model.id))).scalar() or 0
            values.setdefault("id", int(max_id) + 1)
            values.setdefault("created_at", datetime.utcnow())
            values.setdefault("updated_at", datetime.utcnow())
        session.add(catalog_model(**values))
        session.flush()
    except IntegrityError:
        session.rollback()
        session.execute(
            select(catalog_model)
            .where(
                catalog_model.workspace_id == identifiers.workspace_id,
                catalog_model.auth_id == identifiers.auth_id,
                catalog_model.advertiser_id == str(identifiers.advertiser_id),
                catalog_model.campaign_id == str(campaign_id),
            )
            .with_for_update(nowait=False)
        )


def _prepare_product_metric_row(
    identifiers: SyncIdentifiers, metrics: Mapping[str, Any], dimensions: Mapping[str, Any], granularity: str
) -> Mapping[str, Any] | None:
    base = {
        "workspace_id": identifiers.workspace_id,
        "auth_id": identifiers.auth_id,
        "advertiser_id": str(identifiers.advertiser_id),
        "store_id": str(identifiers.store_id),
        "campaign_id": str(dimensions.get("campaign_id")),
        "cost_cents": money_to_cents(metrics.get("cost")),
        "net_cost_cents": money_to_cents(metrics.get("net_cost")),
        "orders": to_int(metrics.get("orders")),
        "gross_revenue_cents": money_to_cents(metrics.get("gross_revenue")),
        "roi": to_decimal(metrics.get("roi")),
        "cost_per_order": to_decimal(metrics.get("cost_per_order")),
    }
    if not base["campaign_id"]:
        return None
    if granularity == "DAILY":
        stat_time_day = parse_stat_time_day(dimensions.get("stat_time_day"))
        if stat_time_day is None:
            return None
        base["stat_time_day"] = stat_time_day
    else:
        stat_time_hour = parse_stat_time_hour(dimensions.get("stat_time_hour"))
        if stat_time_hour is None:
            return None
        base["stat_time_hour"] = stat_time_hour
    return base


def _prepare_live_metric_row(
    identifiers: SyncIdentifiers, metrics: Mapping[str, Any], dimensions: Mapping[str, Any], granularity: str
) -> Mapping[str, Any] | None:
    base = _prepare_product_metric_row(identifiers, metrics, dimensions, granularity)
    if base is None:
        return None
    base.update(
        {
            "all_shops_orders": to_int(metrics.get("all_shops_orders")),
            "all_shops_gross_revenue_cents": money_to_cents(metrics.get("all_shops_gross_revenue")),
            "all_shops_roi": to_decimal(metrics.get("all_shops_roi")),
            "all_shops_cost_per_order": to_decimal(metrics.get("all_shops_cost_per_order")),
            "live_views": to_int(metrics.get("live_views")),
            "cost_per_live_view": to_decimal(metrics.get("cost_per_live_view")),
            "live_10s_views": to_int(metrics.get("10_second_live_views")),
            "cost_per_10s_live_view": to_decimal(metrics.get("cost_per_10_second_live_view")),
            "live_follows": to_int(metrics.get("live_follows")),
        }
    )
    return base


async def sync_campaign_metrics(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    identifiers: SyncIdentifiers,
    promotion_type: str,
    granularity: str,
    start_date: date,
    end_date: date,
) -> int:
    metrics_list_product = ["cost", "net_cost", "orders", "gross_revenue", "roi", "cost_per_order"]
    metrics_list_live = metrics_list_product + [
        "all_shops_orders",
        "all_shops_gross_revenue",
        "all_shops_roi",
        "all_shops_cost_per_order",
        "live_views",
        "cost_per_live_view",
        "10_second_live_views",
        "cost_per_10_second_live_view",
        "live_follows",
    ]

    metrics = metrics_list_product if promotion_type == "PRODUCT" else metrics_list_live
    dimensions = ["campaign_id"]
    dimensions.append("stat_time_day" if granularity == "DAILY" else "stat_time_hour")

    request = GMVMaxReportGetRequest(
        advertiser_id=str(identifiers.advertiser_id),
        store_ids=[str(identifiers.store_id)],
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        metrics=metrics,
        dimensions=dimensions,
        gmv_max_promotion_types=[promotion_type],
        filtering=None,
        page=1,
        page_size=PAGE_SIZE,
    )

    rows_synced = 0
    prepared_rows: list[Mapping[str, Any]] = []
    seen_campaigns: set[str] = set()
    async for row in _fetch_report_pages(client, request):
        metrics_block, dims = _normalize_entry(row)
        prepared = (
            _prepare_product_metric_row(identifiers, metrics_block, dims, granularity)
            if promotion_type == "PRODUCT"
            else _prepare_live_metric_row(identifiers, metrics_block, dims, granularity)
        )
        if prepared is None:
            logger.warning("gmvmax metrics row skipped missing stat time", extra={"dimensions": dims})
            continue
        _ensure_catalog_stub(session, identifiers, prepared["campaign_id"], promotion_type, seen_campaigns)
        prepared_rows.append(prepared)
        rows_synced += 1

    model = {
        ("PRODUCT", "DAILY"): GmvmaxProductCampaignMetricsDaily,
        ("PRODUCT", "HOURLY"): GmvmaxProductCampaignMetricsHourly,
        ("LIVE", "DAILY"): GmvmaxLiveCampaignMetricsDaily,
        ("LIVE", "HOURLY"): GmvmaxLiveCampaignMetricsHourly,
    }[(promotion_type, granularity)]

    if session.bind.dialect.name == "mysql":
        for i in range(0, len(prepared_rows), UPSERT_CHUNK_SIZE):
            chunk = prepared_rows[i : i + UPSERT_CHUNK_SIZE]
            _bulk_upsert_mysql(session, model, chunk, METRIC_UPDATE_FIELDS[model])
    else:
        for prepared in prepared_rows:
            _upsert(session, model, prepared, METRIC_UPDATE_FIELDS[model])

    session.flush()
    session.commit()
    return rows_synced


async def sync_campaign_snapshot(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    identifiers: SyncIdentifiers,
    promotion_type: str,
    start_date: date,
    end_date: date,
    snapshot_type: str = "MANUAL",
) -> int:
    metrics_list_product = ["cost", "net_cost", "orders", "gross_revenue", "roi", "cost_per_order"]
    metrics_list_live = metrics_list_product + [
        "all_shops_orders",
        "all_shops_gross_revenue",
        "all_shops_roi",
        "all_shops_cost_per_order",
        "live_views",
        "cost_per_live_view",
        "10_second_live_views",
        "cost_per_10_second_live_view",
        "live_follows",
    ]
    metrics = metrics_list_product if promotion_type == "PRODUCT" else metrics_list_live
    request = GMVMaxReportGetRequest(
        advertiser_id=str(identifiers.advertiser_id),
        store_ids=[str(identifiers.store_id)],
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        metrics=metrics,
        dimensions=["campaign_id"],
        gmv_max_promotion_types=[promotion_type],
        filtering=None,
        page=1,
        page_size=PAGE_SIZE,
    )

    batch_model = (
        GmvmaxProductCampaignSnapshotBatch
        if promotion_type == "PRODUCT"
        else GmvmaxLiveCampaignSnapshotBatch
    )
    row_model = (
        GmvmaxProductCampaignSnapshotRow if promotion_type == "PRODUCT" else GmvmaxLiveCampaignSnapshotRow
    )

    rows: list[Mapping[str, Any]] = []
    async for row in _fetch_report_pages(client, request):
        metrics_block, dims = _normalize_entry(row)
        campaign_id = dims.get("campaign_id")
        if not campaign_id:
            continue
        prepared = {
            "campaign_id": str(campaign_id),
            "cost_cents": money_to_cents(metrics_block.get("cost")),
            "net_cost_cents": money_to_cents(metrics_block.get("net_cost")),
            "orders": to_int(metrics_block.get("orders")),
            "gross_revenue_cents": money_to_cents(metrics_block.get("gross_revenue")),
            "roi": to_decimal(metrics_block.get("roi")),
            "cost_per_order": to_decimal(metrics_block.get("cost_per_order")),
        }
        if promotion_type == "LIVE":
            prepared.update(
                {
                    "all_shops_orders": to_int(metrics_block.get("all_shops_orders")),
                    "all_shops_gross_revenue_cents": money_to_cents(metrics_block.get("all_shops_gross_revenue")),
                    "all_shops_roi": to_decimal(metrics_block.get("all_shops_roi")),
                    "all_shops_cost_per_order": to_decimal(metrics_block.get("all_shops_cost_per_order")),
                    "live_views": to_int(metrics_block.get("live_views")),
                    "cost_per_live_view": to_decimal(metrics_block.get("cost_per_live_view")),
                    "live_10s_views": to_int(metrics_block.get("10_second_live_views")),
                    "cost_per_10s_live_view": to_decimal(metrics_block.get("cost_per_10_second_live_view")),
                    "live_follows": to_int(metrics_block.get("live_follows")),
                }
            )
        rows.append(prepared)

    for attempt in range(3):
        try:
            with session.begin():
                batch_values = {
                    "workspace_id": identifiers.workspace_id,
                    "auth_id": identifiers.auth_id,
                    "advertiser_id": str(identifiers.advertiser_id),
                    "store_id": str(identifiers.store_id),
                    "start_date": start_date,
                    "end_date": end_date,
                    "snapshot_type": snapshot_type,
                    "snapshot_at": datetime.utcnow(),
                }
                _upsert(session, batch_model, batch_values, ["snapshot_at"])

                batch = session.execute(
                    select(batch_model)
                    .where(batch_model.workspace_id == identifiers.workspace_id)
                    .where(batch_model.auth_id == identifiers.auth_id)
                    .where(batch_model.advertiser_id == str(identifiers.advertiser_id))
                    .where(batch_model.store_id == str(identifiers.store_id))
                    .where(batch_model.start_date == start_date)
                    .where(batch_model.end_date == end_date)
                    .where(batch_model.snapshot_type == snapshot_type)
                    .with_for_update()
                ).scalar_one()

                session.query(row_model).filter(row_model.batch_id == batch.id).delete()
                if rows:
                    rows_to_insert = [{"batch_id": batch.id, **row} for row in rows]
                    if session.bind.dialect.name != "mysql":
                        current_max = session.execute(select(func.max(row_model.id))).scalar() or 0
                        updated_rows = []
                        for idx, row in enumerate(rows_to_insert, start=1):
                            if hasattr(row_model, "id") and "id" not in row:
                                row["id"] = current_max + idx
                            if hasattr(row_model, "created_at") and "created_at" not in row:
                                row["created_at"] = datetime.utcnow()
                            if hasattr(row_model, "updated_at") and "updated_at" not in row:
                                row["updated_at"] = datetime.utcnow()
                            updated_rows.append(row)
                        rows_to_insert = updated_rows
                    for i in range(0, len(rows_to_insert), UPSERT_CHUNK_SIZE):
                        chunk = rows_to_insert[i : i + UPSERT_CHUNK_SIZE]
                        session.execute(row_model.__table__.insert(), chunk)
            break
        except IntegrityError:
            session.rollback()
            if attempt >= 2:
                raise
            await asyncio.sleep(0)
    return len(rows)


__all__ = ["SyncIdentifiers", "sync_campaign_metrics", "sync_campaign_snapshot"]
