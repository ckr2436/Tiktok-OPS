"""Utilities for cleaning up GMV Max campaign metric and snapshot tables.

The cleanup runs in batches to avoid wide locking and is intended to be invoked
from a scheduled Celery task. Retention windows are configurable so the task can
be tuned per-environment.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxLiveCampaignMetricsDaily,
    GmvmaxLiveCampaignMetricsHourly,
    GmvmaxProductCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
)
from app.data.models.gmvmax_campaign_snapshots import (
    GmvmaxLiveCampaignSnapshotBatch,
    GmvmaxProductCampaignSnapshotBatch,
)
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest

logger = logging.getLogger("gmv.gmvmax.cleanup")


def _delete_in_batches(
    session: Session,
    model,
    *,
    cutoff_value,
    cutoff_column: Callable[[object], object],
    batch_size: int = 10_000,
) -> int:
    deleted_total = 0
    while True:
        ids = (
            session.execute(
                select(model.id)
                .where(cutoff_column(model) < cutoff_value)
                .order_by(cutoff_column(model), model.id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        deleted = (
            session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        )
        session.commit()
        deleted_total += int(deleted)
    return deleted_total


def cleanup_campaign_tables(
    session: Session,
    *,
    now: datetime | None = None,
    hourly_retention_days: int = 90,
    daily_retention_days: int = 730,
    snapshot_retention_days: int = 90,
    creative_10min_retention_days: int = 90,
) -> dict:
    """Delete expired campaign facts, snapshots, and creative batch watermarks."""

    started = time.monotonic()
    clock = now or datetime.utcnow()
    hourly_cutoff = clock - timedelta(days=hourly_retention_days)
    daily_cutoff = (clock.date() if isinstance(clock, datetime) else date.today()) - timedelta(
        days=daily_retention_days
    )
    snapshot_cutoff = clock - timedelta(days=snapshot_retention_days)
    creative_10min_cutoff = clock - timedelta(
        days=creative_10min_retention_days
    )

    deleted_hourly_prod = _delete_in_batches(
        session,
        GmvmaxProductCampaignMetricsHourly,
        cutoff_value=hourly_cutoff,
        cutoff_column=lambda model: model.stat_time_hour,
    )
    deleted_hourly_live = _delete_in_batches(
        session,
        GmvmaxLiveCampaignMetricsHourly,
        cutoff_value=hourly_cutoff,
        cutoff_column=lambda model: model.stat_time_hour,
    )
    deleted_daily_prod = _delete_in_batches(
        session,
        GmvmaxProductCampaignMetricsDaily,
        cutoff_value=daily_cutoff,
        cutoff_column=lambda model: model.stat_time_day,
    )
    deleted_daily_live = _delete_in_batches(
        session,
        GmvmaxLiveCampaignMetricsDaily,
        cutoff_value=daily_cutoff,
        cutoff_column=lambda model: model.stat_time_day,
    )
    deleted_snapshots_prod = _delete_in_batches(
        session,
        GmvmaxProductCampaignSnapshotBatch,
        cutoff_value=snapshot_cutoff,
        cutoff_column=lambda model: model.snapshot_at,
    )
    deleted_snapshots_live = _delete_in_batches(
        session,
        GmvmaxLiveCampaignSnapshotBatch,
        cutoff_value=snapshot_cutoff,
        cutoff_column=lambda model: model.snapshot_at,
    )
    # Remove manifests first so a crash between the two bounded sweeps leaves
    # orphaned detail rows fail-closed rather than readable without a watermark.
    # Both tables use the exact same snapshot cutoff.
    deleted_creative_10min_manifests = _delete_in_batches(
        session,
        GmvCreative10MinBatchManifest,
        cutoff_value=creative_10min_cutoff,
        cutoff_column=lambda model: model.snapshot_at,
    )
    deleted_creative_10min_metrics = _delete_in_batches(
        session,
        GmvCreativeMetrics10Min,
        cutoff_value=creative_10min_cutoff,
        cutoff_column=lambda model: model.snapshot_at,
    )

    elapsed = time.monotonic() - started
    summary = {
        "hourly_prod": deleted_hourly_prod,
        "hourly_live": deleted_hourly_live,
        "daily_prod": deleted_daily_prod,
        "daily_live": deleted_daily_live,
        "snapshots_prod": deleted_snapshots_prod,
        "snapshots_live": deleted_snapshots_live,
        "creative_10min_manifests": deleted_creative_10min_manifests,
        "creative_10min_metrics": deleted_creative_10min_metrics,
        "elapsed_seconds": elapsed,
    }
    logger.info(
        "gmvmax campaign cleanup finished",
        extra={
            "summary": summary,
            "hourly_cutoff": hourly_cutoff.isoformat(),
            "daily_cutoff": daily_cutoff.isoformat(),
            "snapshot_cutoff": snapshot_cutoff.isoformat(),
            "creative_10min_cutoff": creative_10min_cutoff.isoformat(),
        },
    )
    return summary


__all__ = ["cleanup_campaign_tables"]
