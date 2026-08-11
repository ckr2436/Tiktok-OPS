"""GMV Max creative metrics synchronization using TikTok report/get."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import case, func, or_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.gmvmax.services.campaign_report_sync import PAGE_SIZE, SyncIdentifiers
from app.gmvmax.services.fact_freshness import (
    settlement_metadata,
    utc_now_naive,
)
from app.gmvmax.services.fact_reconciliation import StagedFactKeySet
from app.gmvmax.services.report_pagination import (
    ReportPaginationState,
    chunk_report_filter_ids,
    report_page_has_more,
)
from app.gmvmax.services.gmvmax_value_parser import (
    money_to_cents,
    parse_stat_time_day,
    to_decimal,
    to_int,
)
from app.gmvmax.creative_status import canonicalize_creative_delivery_status
from app.services.gmvmax_creative_assets import (
    backfill_missing_creative_assets_for_scope,
    resolve_store_authorized_bc_id,
    sync_creative_assets_for_scope,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMAX_CREATIVE_METRICS,
    GMVMaxReportData,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_gmvmax import fetch_gmvmax_current_creative_statuses

logger = logging.getLogger("gmv.gmvmax.creative_report_sync")

MAX_RETRIES = 3
UPSERT_CHUNK_SIZE = 1000

CREATIVE_UPDATE_FIELDS = [
    "creative_delivery_status",
    "cost_cents",
    "net_cost_cents",
    "orders",
    "gross_revenue_cents",
    "roi",
    "cost_per_order",
    "impressions",
    "clicks",
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "conversion_rate",
    "ad_video_view_rate_2s",
    "ad_video_view_rate_6s",
    "ad_video_view_rate_p25",
    "ad_video_view_rate_p50",
    "ad_video_view_rate_p75",
    "ad_video_view_rate_p100",
    "raw_metrics_json",
    "source_observed_at",
    "ingested_at",
    "is_final",
    "settled_at",
    "updated_at",
]

_NULL_PRESERVING_FIELDS = {
    "creative_delivery_status",
    "cost_cents",
    "orders",
    "gross_revenue_cents",
    "roi",
    "cost_per_order",
    "impressions",
    "clicks",
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "conversion_rate",
    "ad_video_view_rate_2s",
    "ad_video_view_rate_6s",
    "ad_video_view_rate_p25",
    "ad_video_view_rate_p50",
    "ad_video_view_rate_p75",
    "ad_video_view_rate_p100",
    "raw_metrics_json",
}


def _official_daily_report_windows(
    start_date: date,
    end_date: date,
) -> list[tuple[date, date]]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        window_end = min(end_date, current + timedelta(days=29))
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _normalize_entry(entry: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(entry, Mapping):
        return dict(entry.get("metrics") or {}), dict(entry.get("dimensions") or {})
    return dict(getattr(entry, "metrics", {}) or {}), dict(getattr(entry, "dimensions", {}) or {})


async def _fetch_report_pages(client: TikTokBusinessGMVMaxClient, request: GMVMaxReportGetRequest):
    page = 1
    max_pages = 200
    pagination_state = ReportPaginationState(require_dimensions=True)
    while True:
        if page > max_pages:
            raise RuntimeError(f"GMV Max creative report pagination exceeded {max_pages} pages")
        request.page = page
        request.page_size = PAGE_SIZE
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response: GMVMaxResponse[GMVMaxReportData] = await client.gmv_max_report_get(
                    request,
                    inject_promotion_types=False,
                )
                break
            except Exception:  # noqa: BLE001
                if attempt >= MAX_RETRIES:
                    logger.exception("gmvmax creative report/get failed after retries")
                    raise
                await asyncio.sleep(2 ** (attempt - 1))

        data = response.data or GMVMaxReportData()
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


def _prepare_row(
    identifiers: SyncIdentifiers,
    metrics: Mapping[str, Any],
    dimensions: Mapping[str, Any],
    *,
    fallback_stat_time_day: date,
    fallback_campaign_id: str | None,
    fallback_item_group_id: str | None,
) -> Mapping[str, Any] | None:
    campaign_id = dimensions.get("campaign_id") or fallback_campaign_id
    item_group_id = dimensions.get("item_group_id") or fallback_item_group_id
    creative_id = dimensions.get("item_id") or dimensions.get("creative_id")
    if not campaign_id or not item_group_id or not creative_id:
        logger.warning(
            "gmvmax creative row skipped missing dimensions",
            extra={"dimensions": dict(dimensions), "campaign_id": campaign_id, "item_group_id": item_group_id},
        )
        return None

    stat_time_day = (
        parse_stat_time_day(
            dimensions.get("stat_time_day")
            or dimensions.get("date")
            or dimensions.get("stat_time")
        )
        or fallback_stat_time_day
    )

    creative_delivery_status = canonicalize_creative_delivery_status(
        metrics.get("creative_delivery_status")
    )
    raw_metrics = dict(metrics)
    if creative_delivery_status is not None:
        raw_metrics["creative_delivery_status"] = creative_delivery_status

    return {
        "workspace_id": identifiers.workspace_id,
        "auth_id": identifiers.auth_id,
        "advertiser_id": str(identifiers.advertiser_id),
        "store_id": str(identifiers.store_id),
        "campaign_id": str(campaign_id),
        "item_group_id": str(item_group_id),
        "creative_id": str(creative_id),
        "stat_time_day": stat_time_day,
        "creative_delivery_status": creative_delivery_status,
        "cost_cents": money_to_cents(metrics.get("cost")),
        "net_cost_cents": money_to_cents(metrics.get("net_cost")),
        "orders": to_int(metrics.get("orders")),
        "gross_revenue_cents": money_to_cents(metrics.get("gross_revenue")),
        "roi": to_decimal(metrics.get("roi")),
        "cost_per_order": to_decimal(metrics.get("cost_per_order")),
        "impressions": to_int(metrics.get("impressions")),
        "clicks": to_int(metrics.get("clicks")),
        "product_impressions": to_int(metrics.get("product_impressions")),
        "product_clicks": to_int(metrics.get("product_clicks")),
        "product_click_rate": to_decimal(metrics.get("product_click_rate"), scale=6),
        "ad_click_rate": to_decimal(metrics.get("ad_click_rate"), scale=6),
        "ad_conversion_rate": to_decimal(metrics.get("ad_conversion_rate"), scale=6),
        "conversion_rate": to_decimal(metrics.get("conversion_rate"), scale=6),
        "ad_video_view_rate_2s": to_decimal(metrics.get("ad_video_view_rate_2s"), scale=6),
        "ad_video_view_rate_6s": to_decimal(metrics.get("ad_video_view_rate_6s"), scale=6),
        "ad_video_view_rate_p25": to_decimal(metrics.get("ad_video_view_rate_p25"), scale=6),
        "ad_video_view_rate_p50": to_decimal(metrics.get("ad_video_view_rate_p50"), scale=6),
        "ad_video_view_rate_p75": to_decimal(metrics.get("ad_video_view_rate_p75"), scale=6),
        "ad_video_view_rate_p100": to_decimal(metrics.get("ad_video_view_rate_p100"), scale=6),
        "raw_metrics_json": raw_metrics,
    }


def _bulk_upsert(session: Session, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    stmt = mysql_insert(GmvmaxProductCreativeMetricsDaily).values(rows)
    assignments = {}
    for field in CREATIVE_UPDATE_FIELDS:
        incoming_value = (
            or_(
                GmvmaxProductCreativeMetricsDaily.is_final,
                stmt.inserted[field],
            )
            if field == "is_final"
            else func.coalesce(
                stmt.inserted[field],
                getattr(GmvmaxProductCreativeMetricsDaily, field),
            )
            if field == "settled_at" or field in _NULL_PRESERVING_FIELDS
            else stmt.inserted[field]
        )
        # Once a daily fact has crossed the advertiser-timezone settlement
        # boundary it is immutable. A later rolling realtime/backfill request
        # may still contain that day, but it cannot rewrite the frozen row.
        assignments[field] = case(
            (
                GmvmaxProductCreativeMetricsDaily.is_final.is_(True),
                getattr(GmvmaxProductCreativeMetricsDaily, field),
            ),
            else_=incoming_value,
        )
    stmt = stmt.on_duplicate_key_update(
        **assignments,
    )
    session.execute(stmt)


def _prepared_row_key(row: Mapping[str, Any]) -> tuple[str, str, str, date]:
    return (
        str(row.get("campaign_id") or ""),
        str(row.get("item_group_id") or ""),
        str(row.get("creative_id") or ""),
        row["stat_time_day"],
    )


def _merge_current_status_rows(
    prepared_rows: list[Mapping[str, Any]],
    status_entries: Sequence[Mapping[str, Any]],
    *,
    identifiers: SyncIdentifiers,
    report_date: date,
    fallback_campaign_id: str | None,
    fallback_item_group_id: str | None,
) -> list[Mapping[str, Any]]:
    """Merge current statuses without replacing dated performance metrics."""

    rows_by_key = {_prepared_row_key(row): dict(row) for row in prepared_rows}
    for entry in status_entries:
        metrics, dimensions = _normalize_entry(entry)
        prepared = _prepare_row(
            identifiers,
            metrics,
            dimensions,
            fallback_stat_time_day=report_date,
            fallback_campaign_id=fallback_campaign_id,
            fallback_item_group_id=fallback_item_group_id,
        )
        if prepared is None:
            continue
        key = _prepared_row_key(prepared)
        existing = rows_by_key.get(key)
        if existing is None:
            rows_by_key[key] = dict(prepared)
            continue

        status = prepared.get("creative_delivery_status")
        if status:
            existing["creative_delivery_status"] = status
            raw_metrics = dict(existing.get("raw_metrics_json") or {})
            raw_metrics["creative_delivery_status"] = status
            existing["raw_metrics_json"] = raw_metrics

    return list(rows_by_key.values())


async def sync_product_creative_metrics(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    identifiers: SyncIdentifiers,
    campaign_ids: Sequence[str],
    item_group_ids: Sequence[str],
    start_date: date,
    end_date: date,
    include_current_statuses: bool = False,
    refresh_creative_assets: bool = True,
    backfill_missing_creative_assets: bool = False,
) -> int:
    clean_campaign_ids = list(dict.fromkeys(str(item) for item in campaign_ids if item))
    clean_item_group_ids = list(
        dict.fromkeys(str(item) for item in item_group_ids if item)
    )
    if not clean_campaign_ids or not clean_item_group_ids:
        return 0

    rows_synced = 0
    prepared_rows: list[Mapping[str, Any]] = []
    reconciliation_stages: list[StagedFactKeySet] = []
    campaign_id_chunks = chunk_report_filter_ids(clean_campaign_ids)
    item_group_id_chunks = chunk_report_filter_ids(clean_item_group_ids)
    fallback_campaign_id = clean_campaign_ids[0] if len(clean_campaign_ids) == 1 else None
    fallback_item_group_id = clean_item_group_ids[0] if len(clean_item_group_ids) == 1 else None
    for window_start, window_end in _official_daily_report_windows(
        start_date,
        end_date,
    ):
        stage = StagedFactKeySet(
            model=GmvmaxProductCreativeMetricsDaily,
            time_column="stat_time_day",
            range_start=window_start,
            range_end_exclusive=window_end + timedelta(days=1),
            key_columns=(
                "campaign_id",
                "item_group_id",
                "creative_id",
                "stat_time_day",
            ),
            scope_equals={
                "workspace_id": identifiers.workspace_id,
                "auth_id": identifiers.auth_id,
                "advertiser_id": str(identifiers.advertiser_id),
                "store_id": str(identifiers.store_id),
            },
            scope_in={
                "campaign_id": clean_campaign_ids,
                "item_group_id": clean_item_group_ids,
            },
        )
        for campaign_id_chunk in campaign_id_chunks:
            for item_group_id_chunk in item_group_id_chunks:
                request = GMVMaxReportGetRequest(
                    advertiser_id=str(identifiers.advertiser_id),
                    store_ids=[str(identifiers.store_id)],
                    start_date=window_start.isoformat(),
                    end_date=window_end.isoformat(),
                    metrics=list(GMVMAX_CREATIVE_METRICS),
                    dimensions=[
                        "campaign_id",
                        "item_group_id",
                        "item_id",
                        "stat_time_day",
                    ],
                    campaign_ids=campaign_id_chunk,
                    item_group_ids=item_group_id_chunk,
                    filtering=GMVMaxReportFiltering(
                        campaign_ids=campaign_id_chunk,
                        item_group_ids=item_group_id_chunk,
                    ),
                    page=1,
                    page_size=PAGE_SIZE,
                )
                async for row in _fetch_report_pages(client, request):
                    metrics, dimensions = _normalize_entry(row)
                    explicit_day = parse_stat_time_day(
                        dimensions.get("stat_time_day")
                        or dimensions.get("date")
                        or dimensions.get("stat_time")
                    )
                    if explicit_day is None and window_start != window_end:
                        # A multi-day response cannot be assigned to one day
                        # without corrupting the daily fact table. Keep absence
                        # reconciliation disabled for the affected window and
                        # skip the malformed row entirely. A single-day request
                        # may still use its exact requested day as a fallback.
                        stage.invalidate()
                        logger.warning(
                            "gmvmax creative row skipped without stat_time_day "
                            "for a multi-day window; absence reconciliation "
                            "disabled for window",
                            extra={
                                "window_start": window_start,
                                "window_end": window_end,
                                "campaign_id": dimensions.get("campaign_id"),
                                "item_group_id": dimensions.get("item_group_id"),
                                "creative_id": (
                                    dimensions.get("item_id")
                                    or dimensions.get("creative_id")
                                ),
                            },
                        )
                        continue
                    prepared = _prepare_row(
                        identifiers,
                        metrics,
                        dimensions,
                        fallback_stat_time_day=window_start,
                        fallback_campaign_id=(
                            campaign_id_chunk[0]
                            if len(campaign_id_chunk) == 1
                            else fallback_campaign_id
                        ),
                        fallback_item_group_id=(
                            item_group_id_chunk[0]
                            if len(item_group_id_chunk) == 1
                            else fallback_item_group_id
                        ),
                    )
                    if prepared is None:
                        stage.invalidate()
                        continue
                    if (
                        prepared["campaign_id"] not in campaign_id_chunk
                        or prepared["item_group_id"] not in item_group_id_chunk
                    ):
                        stage.invalidate()
                        logger.warning(
                            "gmvmax creative row skipped outside requested filter chunk; "
                            "absence reconciliation disabled for window",
                            extra={
                                "campaign_id": prepared["campaign_id"],
                                "item_group_id": prepared["item_group_id"],
                                "window_start": window_start,
                                "window_end": window_end,
                            },
                        )
                        continue
                    if not stage.contains_time(prepared["stat_time_day"]):
                        stage.invalidate()
                        logger.warning(
                            "gmvmax creative row skipped outside requested window; "
                            "absence reconciliation disabled for window",
                            extra={
                                "campaign_id": prepared["campaign_id"],
                                "item_group_id": prepared["item_group_id"],
                                "stat_time_day": prepared["stat_time_day"],
                                "window_start": window_start,
                                "window_end": window_end,
                            },
                        )
                        continue
                    has_performance_metrics = any(
                        metrics.get(metric_name) is not None
                        for metric_name in GMVMAX_CREATIVE_METRICS
                        if metric_name != "creative_delivery_status"
                    )
                    # A status-only row is inventory, not proof that a dated
                    # performance fact still exists. Leave it out of the staged
                    # fact key-set so stale metrics are cleared first.
                    if has_performance_metrics:
                        stage.add(*_prepared_row_key(prepared))
                    prepared_rows.append(prepared)
                    rows_synced += 1
        stage.mark_pagination_complete()
        reconciliation_stages.append(stage)

    if include_current_statuses and start_date == end_date:
        status_entries = await fetch_gmvmax_current_creative_statuses(
            client,
            advertiser_id=str(identifiers.advertiser_id),
            store_id=str(identifiers.store_id),
            campaign_ids=clean_campaign_ids,
            item_group_ids=clean_item_group_ids,
            report_date=end_date,
        )
        prepared_rows = _merge_current_status_rows(
            prepared_rows,
            status_entries,
            identifiers=identifiers,
            report_date=end_date,
            fallback_campaign_id=fallback_campaign_id,
            fallback_item_group_id=fallback_item_group_id,
        )
        rows_synced = len(prepared_rows)

    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    enriched_rows: list[Mapping[str, Any]] = []
    for prepared in prepared_rows:
        enriched = dict(prepared)
        is_final, settled_at = settlement_metadata(
            enriched["stat_time_day"],
            source_observed_at=source_observed_at,
            advertiser_timezone=identifiers.advertiser_timezone,
        )
        enriched.update(
            {
                "source_observed_at": source_observed_at,
                "ingested_at": ingested_at,
                "is_final": is_final,
                "settled_at": settled_at,
                "updated_at": ingested_at,
            }
        )
        enriched_rows.append(enriched)
    prepared_rows = enriched_rows

    # Delete omitted non-final facts before status-only rows are inserted. This
    # clears stale metrics instead of preserving them via COALESCE.
    for stage in reconciliation_stages:
        stage.reconcile(session)

    if session.bind.dialect.name == "mysql":
        for index in range(0, len(prepared_rows), UPSERT_CHUNK_SIZE):
            _bulk_upsert(session, prepared_rows[index : index + UPSERT_CHUNK_SIZE])
    else:
        for prepared in prepared_rows:
            existing = (
                session.query(GmvmaxProductCreativeMetricsDaily)
                .filter_by(
                    workspace_id=prepared["workspace_id"],
                    auth_id=prepared["auth_id"],
                    advertiser_id=prepared["advertiser_id"],
                    store_id=prepared["store_id"],
                    campaign_id=prepared["campaign_id"],
                    item_group_id=prepared["item_group_id"],
                    creative_id=prepared["creative_id"],
                    stat_time_day=prepared["stat_time_day"],
                )
                .one_or_none()
            )
            if existing:
                if existing.is_final:
                    continue
                for field in CREATIVE_UPDATE_FIELDS:
                    if field == "is_final":
                        setattr(
                            existing,
                            field,
                            bool(getattr(existing, field) or prepared.get(field)),
                        )
                        continue
                    if (
                        field == "settled_at" or field in _NULL_PRESERVING_FIELDS
                    ) and prepared.get(field) is None:
                        continue
                    setattr(existing, field, prepared.get(field))
            else:
                session.add(GmvmaxProductCreativeMetricsDaily(**prepared))
    session.flush()

    creative_refs = [
        {
            "creative_id": str(row.get("creative_id") or ""),
            "item_group_id": str(row.get("item_group_id") or ""),
        }
        for row in prepared_rows
        if str(row.get("creative_id") or "") not in {"", "-1", "0"}
    ]
    # Report refresh and video-library discovery have very different latency
    # and freshness requirements.  Interactive/report-only callers can skip
    # the paginated identity + video/get scan; the dedicated creative refresh
    # path and the realtime creative worker remain responsible for inventory.
    if creative_refs and (refresh_creative_assets or backfill_missing_creative_assets):
        store_authorized_bc_id = resolve_store_authorized_bc_id(
            session,
            workspace_id=int(identifiers.workspace_id),
            auth_id=int(identifiers.auth_id),
            advertiser_id=str(identifiers.advertiser_id),
            store_id=str(identifiers.store_id),
        )
        if store_authorized_bc_id:
            try:
                sync_assets = (
                    sync_creative_assets_for_scope
                    if refresh_creative_assets
                    else backfill_missing_creative_assets_for_scope
                )
                asset_result = await sync_assets(
                    session,
                    client,
                    workspace_id=int(identifiers.workspace_id),
                    auth_id=int(identifiers.auth_id),
                    advertiser_id=str(identifiers.advertiser_id),
                    store_id=str(identifiers.store_id),
                    store_authorized_bc_id=str(store_authorized_bc_id),
                    creative_refs=creative_refs,
                    item_group_ids=clean_item_group_ids,
                )
                logger.info(
                    "gmvmax creative assets enriched after daily metrics",
                    extra={
                        "workspace_id": identifiers.workspace_id,
                        "auth_id": identifiers.auth_id,
                        "advertiser_id": identifiers.advertiser_id,
                        "store_id": identifiers.store_id,
                        "campaign_ids": clean_campaign_ids[:5],
                        "mode": "full" if refresh_creative_assets else "missing_only",
                        "asset_result": asset_result,
                    },
                )
            except Exception:  # noqa: BLE001 - asset enrichment must not break metrics sync
                logger.warning(
                    "gmvmax creative asset enrichment failed after daily metrics",
                    exc_info=True,
                    extra={
                        "workspace_id": identifiers.workspace_id,
                        "auth_id": identifiers.auth_id,
                        "advertiser_id": identifiers.advertiser_id,
                        "store_id": identifiers.store_id,
                        "campaign_ids": clean_campaign_ids[:5],
                    },
                )
    return rows_synced


__all__ = ["sync_product_creative_metrics"]
