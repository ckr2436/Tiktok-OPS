"""Fail-closed recovery for incomplete Product GMV Max campaign creation.

This module never submits a campaign create request.  It may only discover an
already-created campaign through TikTok read APIs, pause that exact campaign,
and quarantine its local automation state for an explicit same-intent resume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
)
from app.features.tenants.ttb.gmv_max._helpers import ensure_ttb_auth_in_workspace
from app.features.tenants.ttb.gmv_max.service import (
    mark_gmvmax_create_intent,
    reconcile_gmvmax_create_intent,
)
from app.gmvmax.services.mutation_execution_lock import (
    GmvMaxMutationBusy,
    GmvMaxMutationFenceLost,
    gmvmax_mutation_lease,
)
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_client_factory import build_ttb_gmvmax_client

logger = logging.getLogger("gmv.gmvmax.create_intent_recovery")

RECOVERABLE_CREATE_INTENT_STATES = frozenset(
    {"SUBMITTING", "UNKNOWN", "REMOTE_CREATED", "FINALIZING"}
)
_CREATIVE_REBUILD_SOURCE = "creative_guard_rebuild"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_creative_rebuild_intent(intent: GmvmaxCampaignCreateIntent) -> bool:
    request = intent.request_json or {}
    if not isinstance(request, Mapping):
        return False
    automation = request.get("automation")
    return (
        isinstance(automation, Mapping)
        and str(automation.get("source") or "") == _CREATIVE_REBUILD_SOURCE
    )


def _skip_generic_recovery_for_creative_rebuild(
    db: Session,
    intent: GmvmaxCampaignCreateIntent,
) -> bool:
    if not _is_creative_rebuild_intent(intent):
        return False
    result = intent.result_json or {}
    workflow = (
        result.get("rebuild_workflow")
        if isinstance(result, Mapping)
        else None
    )
    phase = (
        str(workflow.get("phase") or "").upper()
        if isinstance(workflow, Mapping)
        else ""
    )
    # A replacement whose safety pause already failed belongs to the generic
    # fail-closed scanner too; pausing it outranks automatic finalization.
    if phase == "QUARANTINE_PENDING":
        return False
    source_strategy = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == int(intent.workspace_id),
            GmvStrategyConfig.auth_id == int(intent.auth_id),
            GmvStrategyConfig.campaign_id
            == str(intent.replacement_campaign_id or ""),
        )
        .first()
    )
    source_catalog_count = (
        db.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.workspace_id
            == int(intent.workspace_id),
            GmvmaxProductCampaignCatalog.auth_id == int(intent.auth_id),
            GmvmaxProductCampaignCatalog.advertiser_id
            == str(intent.advertiser_id),
            GmvmaxProductCampaignCatalog.store_id == str(intent.store_id),
            GmvmaxProductCampaignCatalog.campaign_id
            == str(intent.replacement_campaign_id or ""),
        )
        .count()
    )
    source_campaign_id_count = (
        db.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.workspace_id
            == int(intent.workspace_id),
            GmvmaxProductCampaignCatalog.auth_id == int(intent.auth_id),
            GmvmaxProductCampaignCatalog.campaign_id
            == str(intent.replacement_campaign_id or ""),
        )
        .count()
    )
    # While the source strategy is still enabled, Creative Guard owns the
    # resumable workflow. If the user disables/deletes it, generic recovery
    # regains ownership and only quarantines any remotely created replacement.
    updated_at = intent.updated_at
    if updated_at is not None and updated_at.tzinfo is not None:
        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    fresh_owner_heartbeat = bool(
        updated_at is not None
        and updated_at >= _utcnow_naive() - timedelta(minutes=5)
    )
    return bool(
        source_strategy is not None
        and source_strategy.enabled
        and source_catalog_count == 1
        and source_campaign_id_count == 1
        and fresh_owner_heartbeat
    )


def _load_recoverable_intent(
    db: Session,
    *,
    intent_id: int,
    workspace_id: int,
    auth_id: int,
    generic_claimed: bool = False,
) -> GmvmaxCampaignCreateIntent | None:
    intent = (
        db.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.id == int(intent_id),
            GmvmaxCampaignCreateIntent.workspace_id == int(workspace_id),
            GmvmaxCampaignCreateIntent.auth_id == int(auth_id),
            GmvmaxCampaignCreateIntent.state.in_(
                sorted(RECOVERABLE_CREATE_INTENT_STATES)
            ),
        )
        .first()
    )
    # Creative Guard owns a richer PREPARED/finalization state machine. The
    # generic scanner must never turn that resumable workflow into a permanent
    # operator-only quarantine before Creative Guard gets to resume it.
    if (
        intent is not None
        and not generic_claimed
        and _skip_generic_recovery_for_creative_rebuild(db, intent)
    ):
        return None
    return intent


def _get_or_create_disabled_strategy(
    db: Session,
    *,
    intent: GmvmaxCampaignCreateIntent,
    campaign_id: str,
    remote_pause_confirmed: bool,
    reason: str,
) -> GmvStrategyConfig:
    strategy = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == int(intent.workspace_id),
            GmvStrategyConfig.auth_id == int(intent.auth_id),
            GmvStrategyConfig.campaign_id == str(campaign_id),
        )
        .first()
    )
    if strategy is None:
        strategy = GmvStrategyConfig(
            workspace_id=int(intent.workspace_id),
            auth_id=int(intent.auth_id),
            campaign_id=str(campaign_id),
            enabled=False,
            cooldown_minutes=30,
            min_runtime_minutes_before_first_change=30,
            config_json={},
        )
    config = (
        dict(strategy.config_json)
        if isinstance(strategy.config_json, Mapping)
        else {}
    )
    config["creation_quarantine"] = {
        "enabled": True,
        "source": "create_intent_recovery",
        "reason": str(reason)[:1000],
        "remote_pause_confirmed": bool(remote_pause_confirmed),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    strategy.enabled = False
    strategy.config_json = config
    db.add(strategy)
    return strategy


def _mark_catalog_disabled(
    db: Session,
    *,
    row: GmvmaxProductCampaignCatalog,
) -> None:
    observed_at = _utcnow_naive()
    row.operation_status = "DISABLE"
    row.secondary_status = "CAMPAIGN_STATUS_DISABLE"
    row.list_synced_at = observed_at
    row.detail_synced_at = observed_at
    row.modify_time_utc = observed_at
    row.updated_at = observed_at
    db.add(row)


async def recover_one_gmvmax_create_intent(
    db: Session,
    *,
    intent_id: int,
    workspace_id: int,
    auth_id: int,
    client: TikTokBusinessGMVMaxClient,
    mutation: Any,
) -> dict[str, Any]:
    """Reconcile and quarantine one intent under an existing mutation lease."""

    intent = _load_recoverable_intent(
        db,
        intent_id=int(intent_id),
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
    )
    if intent is None:
        return {"status": "stale"}

    original_state = str(intent.state or "").upper()
    row = await reconcile_gmvmax_create_intent(
        db,
        client=client,
        intent=intent,
        execution_guard=lambda current_db: mutation.assert_current(current_db),
        require_official_confirmation=True,
    )
    if row is None:
        # Rotate unresolved UNKNOWN intents to the back of the bounded scan so
        # a long-lived ambiguous outcome cannot starve every later intent.
        intent.updated_at = _utcnow_naive()
        db.add(intent)
        mutation.commit(db)
        return {"status": "pending", "state": original_state}

    campaign_id = str(row.campaign_id)
    reconciled_result = dict(intent.result_json or {})
    reconciled_result.update(
        {
            "campaign_id": campaign_id,
            "reconciled": True,
            "recovery_source": "smart_guard_create_intent_scan",
        }
    )
    mark_gmvmax_create_intent(
        db,
        workspace_id=int(intent.workspace_id),
        auth_id=int(intent.auth_id),
        advertiser_id=str(intent.advertiser_id),
        store_id=str(intent.store_id),
        idempotency_key=str(intent.idempotency_key),
        state="REMOTE_CREATED",
        campaign_id=campaign_id,
        result_json=reconciled_result,
    )
    # Persist the recovered immutable campaign identity before any status
    # mutation.  A crash after this point will retry only DISABLE, never CREATE.
    mutation.commit(db)

    try:
        mutation.assert_current(db)
        response = await client.campaign_status_update(
            CampaignStatusUpdateRequest(
                advertiser_id=str(intent.advertiser_id),
                campaign_ids=[campaign_id],
                operation_status="DISABLE",
            )
        )
        mutation.assert_current(db)
    except GmvMaxMutationFenceLost:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        retry_intent = _load_recoverable_intent(
            db,
            intent_id=int(intent_id),
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            generic_claimed=True,
        )
        if retry_intent is None:
            raise
        _get_or_create_disabled_strategy(
            db,
            intent=retry_intent,
            campaign_id=campaign_id,
            remote_pause_confirmed=False,
            reason="Recovered campaign could not yet be confirmed paused.",
        )
        retry_result = dict(retry_intent.result_json or {})
        retry_result.update(
            {
                "campaign_id": campaign_id,
                "recovery_pause_pending": True,
                "recovery_source": "smart_guard_create_intent_scan",
            }
        )
        mark_gmvmax_create_intent(
            db,
            workspace_id=int(retry_intent.workspace_id),
            auth_id=int(retry_intent.auth_id),
            advertiser_id=str(retry_intent.advertiser_id),
            store_id=str(retry_intent.store_id),
            idempotency_key=str(retry_intent.idempotency_key),
            state="REMOTE_CREATED",
            campaign_id=campaign_id,
            result_json=retry_result,
            error_json={
                "recovery_pause_error_type": type(exc).__name__,
                "recovery_pause_error": str(exc)[:1000],
            },
        )
        mutation.commit(db)
        logger.warning(
            "recovered GMV Max create intent is still awaiting remote pause",
            exc_info=True,
            extra={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "intent_id": int(intent_id),
                "campaign_id": campaign_id,
            },
        )
        return {
            "status": "pause_pending",
            "state": "REMOTE_CREATED",
            "campaign_id": campaign_id,
        }

    intent = _load_recoverable_intent(
        db,
        intent_id=int(intent_id),
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        generic_claimed=True,
    )
    if intent is None:
        # Another fenced owner completed the intent while the status request
        # was in flight.  Do not overwrite its terminal state.
        db.rollback()
        return {"status": "stale", "campaign_id": campaign_id}
    row = (
        db.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.workspace_id == int(intent.workspace_id),
            GmvmaxProductCampaignCatalog.auth_id == int(intent.auth_id),
            GmvmaxProductCampaignCatalog.advertiser_id
            == str(intent.advertiser_id),
            GmvmaxProductCampaignCatalog.store_id == str(intent.store_id),
            GmvmaxProductCampaignCatalog.campaign_id == campaign_id,
        )
        .first()
    )
    if row is not None:
        _mark_catalog_disabled(db, row=row)
    _get_or_create_disabled_strategy(
        db,
        intent=intent,
        campaign_id=campaign_id,
        remote_pause_confirmed=True,
        reason=(
            "Campaign creation was recovered after an interrupted request; "
            "the original create intent must finish initialization."
        ),
    )
    response_request_id = str(getattr(response, "request_id", None) or "")
    quarantine_result = dict(intent.result_json or {})
    quarantine_result.update(
        {
            "campaign_id": campaign_id,
            "recovery_source": "smart_guard_create_intent_scan",
            "remote_pause_confirmed": True,
            "pause_request_id": response_request_id or None,
        }
    )
    mark_gmvmax_create_intent(
        db,
        workspace_id=int(intent.workspace_id),
        auth_id=int(intent.auth_id),
        advertiser_id=str(intent.advertiser_id),
        store_id=str(intent.store_id),
        idempotency_key=str(intent.idempotency_key),
        state="QUARANTINED",
        campaign_id=campaign_id,
        result_json=quarantine_result,
        error_json={
            "recovery_reason": (
                "Recovered an official campaign after its create/finalization "
                "request was interrupted."
            ),
            "original_state": original_state,
        },
    )
    mutation.commit(db)
    return {
        "status": "quarantined",
        "state": "QUARANTINED",
        "campaign_id": campaign_id,
    }


async def recover_incomplete_gmvmax_create_intents(
    db: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Scan a bounded oldest-first batch without issuing campaign create POSTs."""

    batch_limit = max(
        1,
        min(
            50,
            int(
                limit
                or getattr(
                    settings,
                    "GMVMAX_CREATE_INTENT_RECOVERY_BATCH_SIZE",
                    10,
                )
            ),
        ),
    )
    source_expression = (
        GmvmaxCampaignCreateIntent.request_json["automation"]["source"]
        .as_string()
    )
    # Keyset-page until the safety scanner has found its bounded batch. Each
    # page is fully buffered before the per-row strategy/catalog queries, which
    # avoids issuing nested SQL while a MySQL streaming cursor is still open.
    candidates: list[GmvmaxCampaignCreateIntent] = []
    last_updated_at: datetime | None = None
    last_id = 0
    while len(candidates) < batch_limit:
        auto_query = db.query(GmvmaxCampaignCreateIntent).filter(
            GmvmaxCampaignCreateIntent.state.in_(
                sorted(RECOVERABLE_CREATE_INTENT_STATES)
            ),
            source_expression == _CREATIVE_REBUILD_SOURCE,
        )
        if last_updated_at is not None:
            auto_query = auto_query.filter(
                or_(
                    GmvmaxCampaignCreateIntent.updated_at > last_updated_at,
                    and_(
                        GmvmaxCampaignCreateIntent.updated_at == last_updated_at,
                        GmvmaxCampaignCreateIntent.id > last_id,
                    ),
                )
            )
        page = (
            auto_query.order_by(
                GmvmaxCampaignCreateIntent.updated_at.asc(),
                GmvmaxCampaignCreateIntent.id.asc(),
            )
            .limit(100)
            .all()
        )
        if not page:
            break
        for intent in page:
            if not _skip_generic_recovery_for_creative_rebuild(db, intent):
                candidates.append(intent)
                if len(candidates) >= batch_limit:
                    break
        last_updated_at = page[-1].updated_at
        last_id = int(page[-1].id)
        if len(page) < 100:
            break
    remaining = batch_limit - len(candidates)
    if remaining > 0:
        normal_candidates = (
            db.query(GmvmaxCampaignCreateIntent)
            .filter(
                GmvmaxCampaignCreateIntent.state.in_(
                    sorted(RECOVERABLE_CREATE_INTENT_STATES)
                ),
                or_(
                    source_expression.is_(None),
                    source_expression != _CREATIVE_REBUILD_SOURCE,
                ),
            )
            .order_by(
                GmvmaxCampaignCreateIntent.updated_at.asc(),
                GmvmaxCampaignCreateIntent.id.asc(),
            )
            .limit(remaining)
            .all()
        )
        candidates.extend(normal_candidates)
    summary: dict[str, Any] = {
        "candidates": len(candidates),
        "checked": 0,
        "quarantined": 0,
        "pending": 0,
        "pause_pending": 0,
        "stale": 0,
        "busy": 0,
        "errors": 0,
    }

    for candidate in candidates:
        intent_id = int(candidate.id)
        workspace_id = int(candidate.workspace_id)
        auth_id = int(candidate.auth_id)
        client: TikTokBusinessGMVMaxClient | None = None
        try:
            with gmvmax_mutation_lease(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                owner_prefix="create-intent-recovery",
                timeout=0.1,
            ) as mutation:
                intent = _load_recoverable_intent(
                    db,
                    intent_id=intent_id,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                )
                if intent is None:
                    summary["stale"] += 1
                    continue
                ensure_ttb_auth_in_workspace(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                )
                client = build_ttb_gmvmax_client(
                    db,
                    auth_id=auth_id,
                    timeout=float(
                        getattr(
                            settings,
                            "GMVMAX_CREATE_INTENT_RECOVERY_TIMEOUT_SECONDS",
                            20.0,
                        )
                    ),
                )
                result = await recover_one_gmvmax_create_intent(
                    db,
                    intent_id=intent_id,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    client=client,
                    mutation=mutation,
                )
                summary["checked"] += 1
                outcome = str(result.get("status") or "errors")
                if outcome in summary:
                    summary[outcome] += 1
                else:
                    summary["errors"] += 1
        except GmvMaxMutationBusy:
            db.rollback()
            summary["busy"] += 1
        except GmvMaxMutationFenceLost:
            db.rollback()
            raise
        except Exception:  # noqa: BLE001
            db.rollback()
            summary["errors"] += 1
            logger.exception(
                "GMV Max create intent recovery failed",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "intent_id": intent_id,
                },
            )
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "GMV Max create intent recovery client close failed",
                        exc_info=True,
                    )
    return summary


__all__ = [
    "RECOVERABLE_CREATE_INTENT_STATES",
    "recover_incomplete_gmvmax_create_intents",
    "recover_one_gmvmax_create_intent",
]
