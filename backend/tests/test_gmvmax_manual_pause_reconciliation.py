from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxCampaignManualOverride,
    create_or_get_campaign_pause_intent,
    set_manual_pause_override,
)
from app.gmvmax.services.manual_pause_reconciliation import (
    reconcile_manual_pause_from_official_catalog,
)


SCOPE = {
    "workspace_id": 7,
    "auth_id": 11,
    "advertiser_id": "adv-1",
    "store_id": "store-1",
    "campaign_id": "campaign-1",
}


def _pause(db_session, *, started_at: datetime) -> GMVMaxCampaignManualOverride:
    row = set_manual_pause_override(
        db_session,
        **SCOPE,
        actor="operator@example.com",
        reason="manual pause",
    )
    row.override_started_at = started_at.replace(tzinfo=None)
    db_session.commit()
    return row


def _observe(
    db_session,
    *,
    observed_at: datetime,
    remote_modified_at: datetime,
    operation_status: str = "ENABLE",
    secondary_status: str = "CAMPAIGN_STATUS_ENABLE",
) -> str:
    return reconcile_manual_pause_from_official_catalog(
        db_session,
        **SCOPE,
        operation_status=operation_status,
        secondary_status=secondary_status,
        remote_modified_at=remote_modified_at,
        source_observed_at=observed_at,
    )


def test_external_enable_requires_new_remote_change_and_two_snapshots(
    db_session,
) -> None:
    pause_at = datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc)
    row = _pause(db_session, started_at=pause_at)

    assert _observe(
        db_session,
        observed_at=pause_at + timedelta(minutes=3),
        remote_modified_at=pause_at,
    ) == "REMOTE_CHANGE_NOT_NEWER"
    assert row.external_enable_observation_count == 0

    remote_enable_at = pause_at + timedelta(minutes=1)
    first_snapshot = pause_at + timedelta(minutes=3)
    assert _observe(
        db_session,
        observed_at=first_snapshot,
        remote_modified_at=remote_enable_at,
    ) == "ENABLE_CONFIRMATION_PENDING"
    assert row.active is True
    assert row.external_enable_observation_count == 1

    assert _observe(
        db_session,
        observed_at=first_snapshot,
        remote_modified_at=remote_enable_at,
    ) == "DUPLICATE_OBSERVATION"
    assert row.external_enable_observation_count == 1

    second_snapshot = first_snapshot + timedelta(minutes=1)
    assert _observe(
        db_session,
        observed_at=second_snapshot,
        remote_modified_at=remote_enable_at,
    ) == "OVERRIDE_CLEARED"
    assert row.active is False
    assert row.resolution_type == "EXTERNAL_ENABLE_CONFIRMED"
    assert row.resolved_at == second_snapshot.replace(tzinfo=None)


def test_disable_between_enable_observations_resets_confirmation(
    db_session,
) -> None:
    pause_at = datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)
    row = _pause(db_session, started_at=pause_at)
    enabled_at = pause_at + timedelta(minutes=1)
    first = pause_at + timedelta(minutes=3)
    assert _observe(
        db_session,
        observed_at=first,
        remote_modified_at=enabled_at,
    ) == "ENABLE_CONFIRMATION_PENDING"

    assert _observe(
        db_session,
        observed_at=first + timedelta(minutes=1),
        remote_modified_at=pause_at,
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
    ) == "NOT_ENABLED"
    assert row.active is True
    assert row.external_enable_observation_count == 0


def test_active_pause_intent_blocks_external_enable_reconciliation(
    db_session,
) -> None:
    pause_at = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)
    row = _pause(db_session, started_at=pause_at)
    create_or_get_campaign_pause_intent(
        db_session,
        **SCOPE,
        actor="operator@example.com",
        reason="still queued",
    )
    db_session.commit()

    assert _observe(
        db_session,
        observed_at=pause_at + timedelta(minutes=5),
        remote_modified_at=pause_at + timedelta(minutes=1),
    ) == "PAUSE_INTENT_ACTIVE"
    assert row.active is True
    assert row.external_enable_observation_count == 0


def test_reconciliation_is_exactly_tenant_scoped(db_session) -> None:
    pause_at = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
    row = _pause(db_session, started_at=pause_at)
    outcome = reconcile_manual_pause_from_official_catalog(
        db_session,
        **{**SCOPE, "workspace_id": 8},
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        remote_modified_at=pause_at + timedelta(minutes=1),
        source_observed_at=pause_at + timedelta(minutes=5),
    )
    assert outcome == "NO_ACTIVE_OVERRIDE"
    assert row.active is True
    assert row.external_enable_observation_count == 0
