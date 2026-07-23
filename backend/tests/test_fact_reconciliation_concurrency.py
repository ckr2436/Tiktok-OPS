from __future__ import annotations

from datetime import date, timedelta

from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
)
from app.gmvmax.services.fact_reconciliation import StagedFactKeySet


def _stage() -> StagedFactKeySet:
    return StagedFactKeySet(
        model=GmvmaxProductCampaignMetricsDaily,
        time_column="stat_time_day",
        range_start=date(2024, 1, 1),
        range_end_exclusive=date(2024, 1, 2),
        key_columns=("campaign_id", "stat_time_day"),
        scope_equals={
            "workspace_id": 1,
            "auth_id": 2,
            "advertiser_id": "adv-1",
            "store_id": "store-1",
        },
    )


def _fact(*, campaign_id: str, source_observed_at):
    return GmvmaxProductCampaignMetricsDaily(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id=campaign_id,
        stat_time_day=date(2024, 1, 1),
        cost_cents=100,
        source_observed_at=source_observed_at,
        is_final=False,
    )


def test_older_reconciliation_never_deletes_newer_sync_fact(db_session):
    stage = _stage()
    newer_observation = stage.reconciliation_started_at + timedelta(seconds=1)
    db_session.add(
        _fact(
            campaign_id="written-by-newer-sync",
            source_observed_at=newer_observation,
        )
    )
    db_session.flush()

    stage.mark_pagination_complete()

    assert stage.reconcile(db_session) == 0
    assert (
        db_session.query(GmvmaxProductCampaignMetricsDaily)
        .filter_by(campaign_id="written-by-newer-sync")
        .count()
        == 1
    )


def test_complete_reconciliation_deletes_older_and_legacy_absent_facts(
    db_session,
):
    stage = _stage()
    db_session.add_all(
        [
            _fact(
                campaign_id="older",
                source_observed_at=(
                    stage.reconciliation_started_at - timedelta(seconds=1)
                ),
            ),
            _fact(campaign_id="legacy", source_observed_at=None),
        ]
    )
    db_session.flush()

    stage.mark_pagination_complete()

    assert stage.reconcile(db_session) == 2
    assert (
        db_session.query(GmvmaxProductCampaignMetricsDaily).count()
        == 0
    )
