"""GMV Max campaign metrics and snapshot synchronization (PRODUCT & LIVE).

The functions here implement idempotent MySQL upserts for the new GMV Max
campaign fact tables and snapshot caches using TikTok report/get responses.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import or_, select, UniqueConstraint, func
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
from app.gmvmax.services.fact_freshness import (
    settlement_metadata,
    utc_now_naive,
)
from app.gmvmax.services.fact_reconciliation import StagedFactKeySet
from app.gmvmax.services.report_pagination import (
    OFFICIAL_REPORT_PAGE_SIZE,
    ReportPaginationState,
    chunk_report_filter_ids,
    report_page_has_more,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxReportData,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
    TikTokBusinessGMVMaxClient,
)

logger = logging.getLogger("gmv.gmvmax.report_sync")

PAGE_SIZE = OFFICIAL_REPORT_PAGE_SIZE
MAX_RETRIES = 3
UPSERT_CHUNK_SIZE = 1000

_FACT_METADATA_UPDATE_FIELDS = [
    "source_observed_at",
    "ingested_at",
    "is_final",
    "settled_at",
    "updated_at",
]

METRIC_UPDATE_FIELDS = {
    GmvmaxProductCampaignMetricsDaily: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
        *_FACT_METADATA_UPDATE_FIELDS,
    ],
    GmvmaxProductCampaignMetricsHourly: [
        "cost_cents",
        "net_cost_cents",
        "orders",
        "gross_revenue_cents",
        "roi",
        "cost_per_order",
        *_FACT_METADATA_UPDATE_FIELDS,
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
        *_FACT_METADATA_UPDATE_FIELDS,
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
        *_FACT_METADATA_UPDATE_FIELDS,
    ],
}

_NULL_PRESERVING_FIELDS = {
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
}


@dataclass
class SyncIdentifiers:
    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str
    advertiser_timezone: str | None = None


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
        assignments = {
            field: (
                or_(getattr(model, field), stmt.inserted[field])
                if field == "is_final"
                else func.coalesce(stmt.inserted[field], getattr(model, field))
                if field == "settled_at" or field in _NULL_PRESERVING_FIELDS
                else stmt.inserted[field]
            )
            for field in update_fields
        }
        stmt = stmt.on_duplicate_key_update(
            **assignments
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
            assignments = {
                field: (
                    or_(getattr(model, field), stmt.excluded[field])
                    if field == "is_final"
                    else func.coalesce(stmt.excluded[field], getattr(model, field))
                    if field == "settled_at" or field in _NULL_PRESERVING_FIELDS
                    else stmt.excluded[field]
                )
                for field in update_fields
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=unique_cols,
                set_=assignments,
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
                    if field == "is_final":
                        setattr(existing, field, bool(getattr(existing, field) or values[field]))
                        continue
                    if (
                        field == "settled_at" or field in _NULL_PRESERVING_FIELDS
                    ) and values[field] is None:
                        continue
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
    assignments = {
        field: (
            or_(getattr(model, field), stmt.inserted[field])
            if field == "is_final"
            else func.coalesce(stmt.inserted[field], getattr(model, field))
            if field == "settled_at" or field in _NULL_PRESERVING_FIELDS
            else stmt.inserted[field]
        )
        for field in update_fields
    }
    stmt = stmt.on_duplicate_key_update(
        **assignments
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
    max_pages = 200
    pagination_state = ReportPaginationState(require_dimensions=True)
    while True:
        if page > max_pages:
            raise RuntimeError(f"GMV Max report pagination exceeded {max_pages} pages")
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
        rows = data.list or []
        for row in rows:
            yield row
        has_more = report_page_has_more(
            data,
            current_page=page,
            rows=rows,
            state=pagination_state,
        )
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
    catalog_model = (
        GmvmaxProductCampaignCatalog if promotion_type == "PRODUCT" else GmvmaxLiveCampaignCatalog
    )
    values = {
        "workspace_id": identifiers.workspace_id,
        "auth_id": identifiers.auth_id,
        "advertiser_id": str(identifiers.advertiser_id),
        "campaign_id": str(campaign_id),
        "store_id": str(identifiers.store_id),
    }
    dialect = session.bind.dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(catalog_model).values(values)
        stmt = stmt.on_duplicate_key_update(
            # A report row proves only that this campaign emitted metrics.  It
            # is not an authoritative campaign/get or campaign/info
            # observation and therefore must never advance the catalog
            # freshness fence on an existing row.
            campaign_id=stmt.inserted.campaign_id,
        )
        # Keep catalog races isolated without ever rolling back unrelated facts
        # in the caller's transaction. Lock/deadlock errors intentionally
        # propagate so the task-level retry can rerun the complete window.
        with session.begin_nested():
            session.execute(stmt)
        seen_campaigns.add(campaign_id)
        return

    # SQLite/testing path: tolerate concurrent insert races
    existing = session.execute(
        select(catalog_model.id)
        .where(catalog_model.workspace_id == identifiers.workspace_id)
        .where(catalog_model.auth_id == identifiers.auth_id)
        .where(
            catalog_model.advertiser_id == str(identifiers.advertiser_id)
        )
        .where(catalog_model.campaign_id == str(campaign_id))
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        seen_campaigns.add(campaign_id)
        return
    try:
        with session.begin_nested():
            insert_values = dict(values)
            if hasattr(catalog_model, "id"):
                max_id = session.execute(select(func.max(catalog_model.id))).scalar() or 0
                insert_values.setdefault("id", int(max_id) + 1)
                insert_values.setdefault("created_at", datetime.utcnow())
                insert_values.setdefault("updated_at", datetime.utcnow())
            session.add(catalog_model(**insert_values))
            session.flush()
    except IntegrityError:
        # A concurrent worker may have inserted the same scoped stub between
        # the pre-check and SAVEPOINT. Confirm that exact row; otherwise this
        # was not the benign uniqueness race we know how to absorb.
        concurrent = session.execute(
            select(catalog_model.id)
            .where(
                catalog_model.workspace_id == identifiers.workspace_id,
                catalog_model.auth_id == identifiers.auth_id,
                catalog_model.advertiser_id == str(identifiers.advertiser_id),
                catalog_model.campaign_id == str(campaign_id),
            )
            .limit(1)
        ).scalar_one_or_none()
        if concurrent is None:
            raise
    seen_campaigns.add(campaign_id)


def _prepare_product_metric_row(
    identifiers: SyncIdentifiers, metrics: Mapping[str, Any], dimensions: Mapping[str, Any], granularity: str
) -> Mapping[str, Any] | None:
    campaign_id = str(dimensions.get("campaign_id") or "").strip()
    if not campaign_id:
        return None
    base = {
        "workspace_id": identifiers.workspace_id,
        "auth_id": identifiers.auth_id,
        "advertiser_id": str(identifiers.advertiser_id),
        "store_id": str(identifiers.store_id),
        "campaign_id": campaign_id,
        "cost_cents": money_to_cents(metrics.get("cost")),
        "net_cost_cents": money_to_cents(metrics.get("net_cost")),
        "orders": to_int(metrics.get("orders")),
        "gross_revenue_cents": money_to_cents(metrics.get("gross_revenue")),
        "roi": to_decimal(metrics.get("roi")),
        "cost_per_order": to_decimal(metrics.get("cost_per_order")),
    }
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


def _official_report_windows(
    start_date: date,
    end_date: date,
    *,
    granularity: str,
) -> list[tuple[date, date]]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    max_days = 1 if granularity == "HOURLY" else 30
    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        window_end = min(end_date, current + timedelta(days=max_days - 1))
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


async def sync_campaign_metrics(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    identifiers: SyncIdentifiers,
    promotion_type: str,
    granularity: str,
    start_date: date,
    end_date: date,
    campaign_ids: Sequence[str] | None = None,
) -> int:
    clean_campaign_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (campaign_ids or ())
            if str(item).strip()
        )
    )
    campaign_id_scope = set(clean_campaign_ids)
    campaign_id_chunks: list[list[str] | None] = (
        list(chunk_report_filter_ids(clean_campaign_ids))
        if clean_campaign_ids
        else [None]
    )
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

    model = {
        ("PRODUCT", "DAILY"): GmvmaxProductCampaignMetricsDaily,
        ("PRODUCT", "HOURLY"): GmvmaxProductCampaignMetricsHourly,
        ("LIVE", "DAILY"): GmvmaxLiveCampaignMetricsDaily,
        ("LIVE", "HOURLY"): GmvmaxLiveCampaignMetricsHourly,
    }[(promotion_type, granularity)]
    time_column = "stat_time_day" if granularity == "DAILY" else "stat_time_hour"

    rows_synced = 0
    prepared_rows: list[Mapping[str, Any]] = []
    reconciliation_stages: list[StagedFactKeySet] = []
    seen_campaigns: set[str] = set()
    for window_start, window_end in _official_report_windows(
        start_date,
        end_date,
        granularity=granularity,
    ):
        stage = StagedFactKeySet(
            model=model,
            time_column=time_column,
            range_start=(
                window_start
                if granularity == "DAILY"
                else datetime.combine(window_start, datetime.min.time())
            ),
            range_end_exclusive=(
                window_end + timedelta(days=1)
                if granularity == "DAILY"
                else datetime.combine(
                    window_end + timedelta(days=1),
                    datetime.min.time(),
                )
            ),
            key_columns=("campaign_id", time_column),
            scope_equals={
                "workspace_id": identifiers.workspace_id,
                "auth_id": identifiers.auth_id,
                "advertiser_id": str(identifiers.advertiser_id),
                "store_id": str(identifiers.store_id),
            },
            scope_in={"campaign_id": clean_campaign_ids} if clean_campaign_ids else {},
        )
        for campaign_id_chunk in campaign_id_chunks:
            request = GMVMaxReportGetRequest(
                advertiser_id=str(identifiers.advertiser_id),
                store_ids=[str(identifiers.store_id)],
                start_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
                metrics=metrics,
                dimensions=dimensions,
                gmv_max_promotion_types=[promotion_type],
                campaign_ids=campaign_id_chunk,
                filtering=(
                    GMVMaxReportFiltering(campaign_ids=campaign_id_chunk)
                    if campaign_id_chunk
                    else None
                ),
                page=1,
                page_size=PAGE_SIZE,
            )
            async for row in _fetch_report_pages(client, request):
                metrics_block, dims = _normalize_entry(row)
                prepared = (
                    _prepare_product_metric_row(
                        identifiers, metrics_block, dims, granularity
                    )
                    if promotion_type == "PRODUCT"
                    else _prepare_live_metric_row(
                        identifiers, metrics_block, dims, granularity
                    )
                )
                if prepared is None:
                    stage.invalidate()
                    logger.warning(
                        "gmvmax metrics row skipped missing campaign/stat time; "
                        "absence reconciliation disabled for window",
                        extra={"dimensions": dims},
                    )
                    continue
                prepared = dict(prepared)
                if (
                    campaign_id_scope
                    and prepared["campaign_id"] not in campaign_id_scope
                ):
                    stage.invalidate()
                    logger.warning(
                        "gmvmax campaign fact skipped outside requested campaign scope; "
                        "absence reconciliation disabled for window",
                        extra={
                            "campaign_id": prepared["campaign_id"],
                            "requested_campaign_ids": clean_campaign_ids,
                        },
                    )
                    continue
                if not stage.contains_time(prepared[time_column]):
                    stage.invalidate()
                    logger.warning(
                        "gmvmax campaign fact skipped outside requested window; "
                        "absence reconciliation disabled for window",
                        extra={
                            "campaign_id": prepared["campaign_id"],
                            "stat_time": prepared[time_column],
                            "window_start": window_start,
                            "window_end": window_end,
                        },
                    )
                    continue
                stage.add(prepared["campaign_id"], prepared[time_column])
                prepared["_report_day"] = (
                    prepared.get("stat_time_day") or window_start
                )
                _ensure_catalog_stub(
                    session,
                    identifiers,
                    prepared["campaign_id"],
                    promotion_type,
                    seen_campaigns,
                )
                prepared_rows.append(prepared)
                rows_synced += 1
        # Reaching this line proves the async page generator exhausted without
        # an upstream/pagination exception.
        stage.mark_pagination_complete()
        reconciliation_stages.append(stage)

    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    for prepared in prepared_rows:
        # Hourly windows are issued one advertiser-local day at a time.  This
        # avoids guessing the timezone of a naive ``stat_time_hour``.
        report_day = prepared.pop("_report_day")
        is_final, settled_at = settlement_metadata(
            report_day,
            source_observed_at=source_observed_at,
            advertiser_timezone=identifiers.advertiser_timezone,
        )
        prepared.update(
            {
                "source_observed_at": source_observed_at,
                "ingested_at": ingested_at,
                "is_final": is_final,
                "settled_at": settled_at,
                "updated_at": ingested_at,
            }
        )

    # Reconcile before upserting so an official status-only/zero row cannot
    # inherit stale metrics through null-preserving update semantics.
    for stage in reconciliation_stages:
        stage.reconcile(session)

    if session.bind.dialect.name == "mysql":
        for i in range(0, len(prepared_rows), UPSERT_CHUNK_SIZE):
            chunk = prepared_rows[i : i + UPSERT_CHUNK_SIZE]
            _bulk_upsert_mysql(session, model, chunk, METRIC_UPDATE_FIELDS[model])
    else:
        for prepared in prepared_rows:
            _upsert(session, model, prepared, METRIC_UPDATE_FIELDS[model])

    session.flush()
    # Core/MySQL upserts bypass ORM state tracking.  Expire cached facts so a
    # caller querying in the same transaction observes the official revision.
    session.expire_all()
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
