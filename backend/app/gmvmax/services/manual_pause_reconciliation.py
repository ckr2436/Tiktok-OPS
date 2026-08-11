"""Reconcile durable user pauses with newer official campaign observations.

An in-platform enable clears the manual override in the same mutation
transaction.  This module handles the other legitimate path: the user enables
the campaign directly in TikTok.  One ENABLE response is not sufficient
because campaign/get is eventually consistent after a successful pause.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxCampaignManualOverride,
    GMVMaxCampaignPauseIntent,
)


logger = logging.getLogger("gmv.gmvmax.manual_pause_reconciliation")

EXTERNAL_ENABLE_PROPAGATION_GRACE = timedelta(minutes=2)
EXTERNAL_ENABLE_MIN_CONFIRMATION_GAP = timedelta(minutes=1)
EXTERNAL_ENABLE_REQUIRED_OBSERVATIONS = 2


def _utc_naive(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_unambiguous_enable(
    operation_status: str | None,
    secondary_status: str | None,
) -> bool:
    return (
        str(operation_status or "").strip().upper() == "ENABLE"
        and str(secondary_status or "").strip().upper()
        == "CAMPAIGN_STATUS_ENABLE"
    )


def _reset_enable_candidate(row: GMVMaxCampaignManualOverride) -> None:
    row.external_enable_first_observed_at = None
    row.external_enable_last_observed_at = None
    row.external_enable_observation_count = 0


def reconcile_manual_pause_from_official_catalog(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str | None,
    campaign_id: str,
    operation_status: str | None,
    secondary_status: str | None,
    remote_modified_at: datetime | None,
    source_observed_at: datetime,
) -> str:
    """Observe one accepted official list snapshot and return its outcome.

    The override is released only when all of these facts hold:

    * the durable override is a completed user PAUSE, not a pending pause;
    * no active pause intent remains;
    * TikTok reports an unambiguous ENABLE state whose remote modification is
      newer than the pause;
    * the propagation grace has elapsed; and
    * two distinct official snapshots, at least one minute apart, agree.

    Strategy and Guard enable switches are deliberately not changed here.
    """

    observed_at = _utc_naive(source_observed_at)
    if observed_at is None:
        raise TypeError("source_observed_at must be a datetime")

    override = db.execute(
        select(GMVMaxCampaignManualOverride)
        .where(
            GMVMaxCampaignManualOverride.workspace_id == int(workspace_id),
            GMVMaxCampaignManualOverride.auth_id == int(auth_id),
            GMVMaxCampaignManualOverride.advertiser_id == str(advertiser_id),
            GMVMaxCampaignManualOverride.store_id
            == (str(store_id) if store_id else None),
            GMVMaxCampaignManualOverride.campaign_id == str(campaign_id),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if override is None or not bool(override.active):
        return "NO_ACTIVE_OVERRIDE"
    if str(override.override_type or "").upper() != "PAUSE":
        return "PAUSE_NOT_FINAL"

    pending_pause = db.execute(
        select(GMVMaxCampaignPauseIntent.id)
        .where(
            GMVMaxCampaignPauseIntent.workspace_id == int(workspace_id),
            GMVMaxCampaignPauseIntent.auth_id == int(auth_id),
            GMVMaxCampaignPauseIntent.advertiser_id == str(advertiser_id),
            GMVMaxCampaignPauseIntent.store_id
            == (str(store_id) if store_id else None),
            GMVMaxCampaignPauseIntent.campaign_id == str(campaign_id),
            GMVMaxCampaignPauseIntent.active_key.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if pending_pause is not None:
        return "PAUSE_INTENT_ACTIVE"

    if not _is_unambiguous_enable(operation_status, secondary_status):
        if int(override.external_enable_observation_count or 0) > 0:
            _reset_enable_candidate(override)
            db.flush()
        return "NOT_ENABLED"

    pause_started_at = _utc_naive(override.override_started_at) or _utc_naive(
        override.created_at
    )
    remote_modified = _utc_naive(remote_modified_at)
    if pause_started_at is None:
        return "PAUSE_START_UNKNOWN"
    if remote_modified is None or remote_modified <= pause_started_at:
        return "REMOTE_CHANGE_NOT_NEWER"
    if observed_at < pause_started_at + EXTERNAL_ENABLE_PROPAGATION_GRACE:
        return "WITHIN_PROPAGATION_GRACE"

    last_observed = _utc_naive(override.external_enable_last_observed_at)
    count = int(override.external_enable_observation_count or 0)
    if count <= 0 or last_observed is None:
        override.external_enable_first_observed_at = observed_at
        override.external_enable_last_observed_at = observed_at
        override.external_enable_observation_count = 1
        db.flush()
        return "ENABLE_CONFIRMATION_PENDING"
    if observed_at <= last_observed:
        return "DUPLICATE_OBSERVATION"
    if observed_at - last_observed < EXTERNAL_ENABLE_MIN_CONFIRMATION_GAP:
        return "CONFIRMATION_TOO_SOON"

    count += 1
    override.external_enable_last_observed_at = observed_at
    override.external_enable_observation_count = count
    if count < EXTERNAL_ENABLE_REQUIRED_OBSERVATIONS:
        db.flush()
        return "ENABLE_CONFIRMATION_PENDING"

    override.active = False
    override.resolved_at = observed_at
    override.resolution_type = "EXTERNAL_ENABLE_CONFIRMED"
    db.flush()
    logger.info(
        "cleared GMV Max manual pause after confirmed external enable",
        extra={
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id) if store_id else None,
            "campaign_id": str(campaign_id),
            "official_modified_at": remote_modified.isoformat(),
            "confirmed_at": observed_at.isoformat(),
        },
    )
    return "OVERRIDE_CLEARED"
