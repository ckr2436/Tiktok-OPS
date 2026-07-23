from __future__ import annotations

"""Execute official creative boost sessions behind tenant and Guard fences."""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmvmax_creative_metrics import (
    GmvmaxProductCreativeMetricsDaily,
)
from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_heating import (
    update_heating_action_result,
)
from app.gmvmax.services.mutation_execution_lock import (
    GmvMaxMutationLease,
    gmvmax_mutation_lease,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxSessionCreateBody,
    GMVMaxSessionCreateRequest,
    GMVMaxSessionSettings,
    TikTokBusinessGMVMaxClient,
)


_ENABLED_OPERATION_STATUSES = {"ENABLE", "CAMPAIGN_STATUS_ENABLE"}
logger = logging.getLogger("gmv.services.gmvmax.heating_actions")


class CreativeHeatingActionBlocked(RuntimeError):
    """Raised when a creative session mutation cannot be proven safe."""


def _mutation_generation(value: Any) -> int:
    """Keep legacy test/extension fences compatible while production uses leases."""

    return int(getattr(value, "global_fencing_token", value))


def _assert_mutation_generation(value: Any, db: Session) -> None:
    assertion = getattr(value, "assert_current", None)
    if callable(assertion):
        assertion(db)


def _snapshot_campaign_state(campaign: Any | None) -> dict[str, Any]:
    if campaign is None:
        return {}
    return {
        "operation_status": getattr(campaign, "operation_status", None),
        "secondary_status": getattr(campaign, "secondary_status", None),
        "budget_cents": getattr(campaign, "budget_cents", None),
        "roas_bid": getattr(campaign, "roas_bid", None),
    }


def _extract_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
    return str(detail)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_boost_budget(
    campaign: Any,
    *,
    target_daily_budget: float | None,
    budget_delta: float | None,
) -> float:
    budget = _to_float(target_daily_budget)
    if budget is not None:
        return budget

    delta = _to_float(budget_delta)
    current_cents = getattr(campaign, "budget_cents", None)
    if current_cents is None:
        current_cents = getattr(campaign, "daily_budget_cents", None)
    current = float(current_cents) / 100 if current_cents is not None else None
    if current is None:
        current = _to_float(getattr(campaign, "budget", None))

    if current is not None and delta is not None:
        return current + delta
    if delta is not None and delta > 0:
        return delta
    raise ValueError("target_daily_budget or a positive budget_delta is required")


def _resolve_spu_id(heating: TTBGmvMaxCreativeHeating) -> str:
    for attr in ("item_group_id", "product_id"):
        value = getattr(heating, attr, None)
        if value:
            return str(value)
    raise ValueError("product_id/spu_id is required for creative boost session")


def _resolve_item_id(heating: TTBGmvMaxCreativeHeating) -> str:
    value = getattr(heating, "item_id", None) or getattr(
        heating, "creative_id", None
    )
    if not value:
        raise ValueError("item_id/video id is required for creative boost session")
    return str(value)


def _extract_session_id(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        value = payload.get("session_id")
        if value:
            return str(value)
        data = payload.get("data")
        if isinstance(data, Mapping) and data.get("session_id"):
            return str(data["session_id"])
    return None


def _promotion_type(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _assert_input_identity(campaign: Any, heating: TTBGmvMaxCreativeHeating) -> None:
    expected = {
        "workspace_id": int(heating.workspace_id),
        "auth_id": int(heating.auth_id),
        "advertiser_id": str(heating.advertiser_id),
        "campaign_id": str(heating.campaign_id),
    }
    actual = {
        "workspace_id": int(getattr(campaign, "workspace_id", 0) or 0),
        "auth_id": int(getattr(campaign, "auth_id", 0) or 0),
        "advertiser_id": str(getattr(campaign, "advertiser_id", "") or ""),
        "campaign_id": str(getattr(campaign, "campaign_id", "") or ""),
    }
    if actual != expected:
        raise CreativeHeatingActionBlocked(
            "creative heating campaign identity does not match its tenant scope"
        )


def _load_authoritative_campaign(
    db: Session,
    *,
    campaign: Any,
    heating: TTBGmvMaxCreativeHeating,
) -> Any:
    _assert_input_identity(campaign, heating)
    binding = db.execute(
        select(TTBBindingConfig)
        .where(TTBBindingConfig.workspace_id == int(heating.workspace_id))
        .where(TTBBindingConfig.auth_id == int(heating.auth_id))
        .limit(1)
    ).scalars().first()
    bound_advertiser = str(getattr(binding, "advertiser_id", "") or "").strip()
    bound_store = str(getattr(binding, "store_id", "") or "").strip()
    if (
        binding is None
        or not bound_advertiser
        or not bound_store
        or bound_advertiser != str(heating.advertiser_id)
    ):
        raise CreativeHeatingActionBlocked(
            "creative heating scope is not the current account binding"
        )

    promotion_type = _promotion_type(heating.promotion_type)
    if promotion_type == "PRODUCT":
        model = GmvmaxProductCampaignCatalog
    elif promotion_type == "LIVE":
        model = GmvmaxLiveCampaignCatalog
    else:
        raise CreativeHeatingActionBlocked("unsupported GMV Max promotion type")

    rows = list(
        db.execute(
            select(model)
            .where(model.workspace_id == int(heating.workspace_id))
            .where(model.auth_id == int(heating.auth_id))
            .where(model.advertiser_id == bound_advertiser)
            .where(model.store_id == bound_store)
            .where(model.campaign_id == str(heating.campaign_id))
            .order_by(model.updated_at.desc())
            .limit(2)
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        raise CreativeHeatingActionBlocked(
            "canonical GMV Max campaign scope is missing or ambiguous"
        )
    return rows[0]


def _creative_item_identity_exists(
    db: Session,
    *,
    campaign: Any,
    creative_id: str,
    item_group_id: str,
) -> bool:
    common = (
        int(campaign.workspace_id),
        int(campaign.auth_id),
        str(campaign.advertiser_id),
        str(campaign.store_id),
        str(campaign.campaign_id),
        str(item_group_id),
        str(creative_id),
    )
    daily_id = db.execute(
        select(GmvmaxProductCreativeMetricsDaily.id)
        .where(
            GmvmaxProductCreativeMetricsDaily.workspace_id == common[0],
            GmvmaxProductCreativeMetricsDaily.auth_id == common[1],
            GmvmaxProductCreativeMetricsDaily.advertiser_id == common[2],
            GmvmaxProductCreativeMetricsDaily.store_id == common[3],
            GmvmaxProductCreativeMetricsDaily.campaign_id == common[4],
            GmvmaxProductCreativeMetricsDaily.item_group_id == common[5],
            GmvmaxProductCreativeMetricsDaily.creative_id == common[6],
            GmvmaxProductCreativeMetricsDaily.source_observed_at.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if daily_id is not None:
        return True
    realtime_id = db.execute(
        select(GmvCreativeMetrics10Min.id)
        .join(
            GmvCreative10MinBatchManifest,
            (
                GmvCreative10MinBatchManifest.workspace_id
                == GmvCreativeMetrics10Min.workspace_id
            )
            & (
                GmvCreative10MinBatchManifest.auth_id
                == GmvCreativeMetrics10Min.auth_id
            )
            & (
                GmvCreative10MinBatchManifest.advertiser_id
                == GmvCreativeMetrics10Min.advertiser_id
            )
            & (
                GmvCreative10MinBatchManifest.store_id
                == GmvCreativeMetrics10Min.store_id
            )
            & (
                GmvCreative10MinBatchManifest.campaign_id
                == GmvCreativeMetrics10Min.campaign_id
            )
            & (
                GmvCreative10MinBatchManifest.stat_time_day
                == GmvCreativeMetrics10Min.stat_time_day
            )
            & (
                GmvCreative10MinBatchManifest.snapshot_at
                == GmvCreativeMetrics10Min.snapshot_at
            )
            & GmvCreative10MinBatchManifest.complete.is_(True),
        )
        .where(
            GmvCreativeMetrics10Min.workspace_id == common[0],
            GmvCreativeMetrics10Min.auth_id == common[1],
            GmvCreativeMetrics10Min.advertiser_id == common[2],
            GmvCreativeMetrics10Min.store_id == common[3],
            GmvCreativeMetrics10Min.campaign_id == common[4],
            GmvCreativeMetrics10Min.item_group_id == common[5],
            GmvCreativeMetrics10Min.creative_id == common[6],
            GmvCreativeMetrics10Min.source_observed_at.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    return realtime_id is not None


def _validate_mutation_scope(
    db: Session,
    *,
    campaign: Any,
    heating: TTBGmvMaxCreativeHeating,
    require_enabled: bool,
    item_group_id: str | None = None,
) -> Any:
    authoritative = _load_authoritative_campaign(
        db,
        campaign=campaign,
        heating=heating,
    )
    if not require_enabled:
        return authoritative

    if _promotion_type(heating.promotion_type) != "PRODUCT":
        raise CreativeHeatingActionBlocked(
            "creative boost sessions are supported only for PRODUCT campaigns"
        )
    operation_status = str(
        getattr(authoritative, "operation_status", "") or ""
    ).strip().upper()
    if operation_status not in _ENABLED_OPERATION_STATUSES:
        raise CreativeHeatingActionBlocked(
            "campaign is not officially enabled; creative boost is held"
        )
    # Local import avoids importing the tenant router package while Celery is
    # still registering the heating task.
    from app.features.tenants.ttb.gmv_max.control import (
        is_manual_pause_override_active,
    )

    if is_manual_pause_override_active(
        db,
        workspace_id=int(heating.workspace_id),
        auth_id=int(heating.auth_id),
        advertiser_id=str(authoritative.advertiser_id),
        store_id=str(authoritative.store_id),
        campaign_id=str(authoritative.campaign_id),
    ):
        raise CreativeHeatingActionBlocked(
            "manual campaign pause override blocks creative boost"
        )
    creative_id = str(heating.creative_id or "").strip()
    if not item_group_id or not creative_id or not _creative_item_identity_exists(
        db,
        campaign=authoritative,
        creative_id=creative_id,
        item_group_id=str(item_group_id),
    ):
        raise CreativeHeatingActionBlocked(
            "creative and item group do not share a canonical campaign identity"
        )
    return authoritative


@contextmanager
def _heating_mutation_fence(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
) -> Iterator[GmvMaxMutationLease]:
    """Run every session mutation behind the shared double fence."""

    try:
        with gmvmax_mutation_lease(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            owner_prefix="creative-heating",
            timeout=0.2,
        ) as mutation:
            try:
                yield mutation
                mutation.commit(db)
            except Exception:
                # Persist a failure audit only while the same execution still
                # owns both generations. Lost ownership always rolls back.
                try:
                    mutation.commit(db)
                except Exception:
                    db.rollback()
                raise
    except Exception:
        raise


def _json_dumps(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), ensure_ascii=False, default=str)


def _record_heating_audit(
    db: Session,
    *,
    campaign: Any,
    heating: TTBGmvMaxCreativeHeating,
    action: str,
    reason: str | None,
    result: str,
    request_payload: Mapping[str, Any] | None,
    response_payload: Mapping[str, Any] | None,
    error_message: str | None,
    performed_by: str,
    fencing_token: int,
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
) -> None:
    request_context = {
        "official_request": dict(request_payload or {}),
        "creative_id": str(heating.creative_id),
        "item_group_id": str(
            getattr(heating, "item_group_id", None)
            or getattr(heating, "product_id", None)
            or ""
        ),
        "performed_by": performed_by,
        "fencing_token": int(fencing_token),
        "before": dict(before_state),
    }
    response_context = {
        "official_response": dict(response_payload or {}),
        "after": dict(after_state),
    }
    db.execute(
        text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, event_type, action, reason, result,
                cost_cents, gross_revenue_cents, orders, roi,
                request_json, response_json, error_message, created_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                null, 'CREATIVE_HEATING', :action, :reason, :result,
                null, null, null, null,
                :request_json, :response_json, :error_message, :created_at
            )
            """
        ),
        {
            "workspace_id": int(heating.workspace_id),
            "auth_id": int(heating.auth_id),
            "advertiser_id": str(campaign.advertiser_id),
            "store_id": str(campaign.store_id),
            "campaign_id": str(campaign.campaign_id),
            "action": str(action),
            "reason": reason,
            "result": str(result),
            "request_json": _json_dumps(request_context),
            "response_json": _json_dumps(response_context),
            "error_message": error_message,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )


async def _persist_blocked_action(
    db: Session,
    *,
    campaign: Any,
    heating: TTBGmvMaxCreativeHeating,
    action_type: str,
    action_time: datetime,
    action_body: dict[str, Any],
    error: CreativeHeatingActionBlocked,
    performed_by: str,
    note: str | None,
    fencing_token: int,
    before_snapshot: Mapping[str, Any],
) -> None:
    message = str(error)
    await update_heating_action_result(
        db,
        heating_id=heating.id,
        status="FAILED",
        action_type=action_type,
        action_time=action_time,
        request_payload=action_body,
        response_payload=None,
        error_message=message,
    )
    _record_heating_audit(
        db,
        campaign=campaign,
        heating=heating,
        action=action_type,
        reason=note or message,
        result="HOLD",
        request_payload=action_body,
        response_payload=None,
        error_message=message,
        performed_by=performed_by,
        fencing_token=fencing_token,
        before_state=before_snapshot,
        after_state=before_snapshot,
    )
    db.flush()


async def stop_boost_creative_session(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    campaign: Any | None,
    heating: TTBGmvMaxCreativeHeating,
    note: str | None = None,
    performed_by: str = "system_auto_heating",
    before_state: Mapping[str, Any] | None = None,
):
    """Delete an official boost session; stopping remains allowed while paused."""

    if campaign is None:
        raise ValueError("campaign is required to stop boost creative session")
    session_id = _extract_session_id(getattr(heating, "last_action_response", None))
    if not session_id:
        raise ValueError("session_id is required to stop creative boost session")

    action_time = datetime.now(timezone.utc)
    before_snapshot = dict(before_state or _snapshot_campaign_state(campaign))
    action_body = {
        "advertiser_id": str(getattr(campaign, "advertiser_id", "") or ""),
        "campaign_id": str(getattr(campaign, "campaign_id", "") or ""),
        "store_id": str(getattr(campaign, "store_id", "") or ""),
        "session_id": session_id,
    }
    with _heating_mutation_fence(
        db,
        workspace_id=int(heating.workspace_id),
        auth_id=int(heating.auth_id),
    ) as mutation:
        fencing_token = _mutation_generation(mutation)
        try:
            authoritative = _validate_mutation_scope(
                db,
                campaign=campaign,
                heating=heating,
                require_enabled=False,
            )
        except CreativeHeatingActionBlocked as exc:
            await _persist_blocked_action(
                db,
                campaign=campaign,
                heating=heating,
                action_type="STOP_CREATIVE",
                action_time=action_time,
                action_body=action_body,
                error=exc,
                performed_by=performed_by,
                note=note,
                fencing_token=fencing_token,
                before_snapshot=before_snapshot,
            )
            raise

        action_body.update(
            {
                "advertiser_id": str(authoritative.advertiser_id),
                "campaign_id": str(authoritative.campaign_id),
                "store_id": str(authoritative.store_id),
            }
        )
        try:
            _assert_mutation_generation(mutation, db)
            response = await client.gmv_max_session_delete(
                advertiser_id=str(authoritative.advertiser_id),
                session_id=session_id,
            )
            _assert_mutation_generation(mutation, db)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None)
            response_payload = detail if isinstance(detail, dict) else None
            error_message = _extract_error_message(detail) if detail else str(exc)
            await update_heating_action_result(
                db,
                heating_id=heating.id,
                status="FAILED",
                action_type="STOP_CREATIVE",
                action_time=action_time,
                request_payload=action_body,
                response_payload=response_payload,
                error_message=error_message,
            )
            _record_heating_audit(
                db,
                campaign=authoritative,
                heating=heating,
                action="STOP_CREATIVE",
                reason=note,
                result="FAILED",
                request_payload=action_body,
                response_payload=response_payload,
                error_message=error_message,
                performed_by=performed_by,
                fencing_token=fencing_token,
                before_state=before_snapshot,
                after_state=before_snapshot,
            )
            db.flush()
            raise

        payload = response.data.model_dump(exclude_none=True)
        updated_row = await update_heating_action_result(
            db,
            heating_id=heating.id,
            status="CANCELLED",
            action_type="STOP_CREATIVE",
            action_time=action_time,
            request_payload=action_body,
            response_payload=payload,
            error_message=None,
        )
        after_snapshot = _snapshot_campaign_state(authoritative)
        _record_heating_audit(
            db,
            campaign=authoritative,
            heating=heating,
            action="STOP_CREATIVE",
            reason=note,
            result="SUCCESS",
            request_payload=action_body,
            response_payload=payload,
            error_message=None,
            performed_by=performed_by,
            fencing_token=fencing_token,
            before_state=before_snapshot,
            after_state=after_snapshot,
        )
        db.flush()
        return updated_row, response


async def apply_boost_creative_action(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    campaign: Any | None,
    heating: TTBGmvMaxCreativeHeating,
    mode: str | None = None,
    target_daily_budget: float | None = None,
    budget_delta: float | None = None,
    currency: str | None = None,
    max_duration_minutes: int | None = None,
    note: str | None = None,
    performed_by: str = "system_auto_heating",
    before_state: Mapping[str, Any] | None = None,
    after_state: Mapping[str, Any] | None = None,
):
    """Create one official boost session after all safety checks pass."""

    if campaign is None:
        raise ValueError("campaign is required to apply boost creative action")
    if str(mode or "").upper() in {"STOP", "STOP_CREATIVE", "STOP_BOOST"}:
        return await stop_boost_creative_session(
            db,
            client=client,
            campaign=campaign,
            heating=heating,
            note=note,
            performed_by=performed_by,
            before_state=before_state,
        )

    item_group_id = _resolve_spu_id(heating)
    item_id = _resolve_item_id(heating)
    action_time = datetime.now(timezone.utc)
    before_snapshot = dict(before_state or _snapshot_campaign_state(campaign))
    preliminary_body = {
        "advertiser_id": str(getattr(campaign, "advertiser_id", "") or ""),
        "campaign_id": str(getattr(campaign, "campaign_id", "") or ""),
        "store_id": str(getattr(campaign, "store_id", "") or ""),
        "session": {
            "bid_type": "CREATIVE_NO_BID",
            "product_list": [{"spu_id": item_group_id}],
            "item_id": item_id,
        },
    }

    with _heating_mutation_fence(
        db,
        workspace_id=int(heating.workspace_id),
        auth_id=int(heating.auth_id),
    ) as mutation:
        fencing_token = _mutation_generation(mutation)
        try:
            authoritative = _validate_mutation_scope(
                db,
                campaign=campaign,
                heating=heating,
                require_enabled=True,
                item_group_id=item_group_id,
            )
        except CreativeHeatingActionBlocked as exc:
            await _persist_blocked_action(
                db,
                campaign=campaign,
                heating=heating,
                action_type="APPLY_BOOST",
                action_time=action_time,
                action_body=preliminary_body,
                error=exc,
                performed_by=performed_by,
                note=note,
                fencing_token=fencing_token,
                before_snapshot=before_snapshot,
            )
            raise

        budget = _resolve_boost_budget(
            authoritative,
            target_daily_budget=target_daily_budget,
            budget_delta=budget_delta,
        )
        if budget <= 0:
            raise ValueError("creative boost budget must be greater than 0")

        session_payload: dict[str, Any] = {
            "bid_type": "CREATIVE_NO_BID",
            "product_list": [{"spu_id": item_group_id}],
            "item_id": item_id,
            "budget": budget,
        }
        if max_duration_minutes is not None:
            end_time = datetime.now(timezone.utc) + timedelta(
                minutes=int(max_duration_minutes)
            )
            session_payload["schedule_type"] = "SCHEDULE_START_END"
            session_payload["schedule_end_time"] = end_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            session_payload["schedule_type"] = "SCHEDULE_FROM_NOW"

        action_body: dict[str, Any] = {
            "advertiser_id": str(authoritative.advertiser_id),
            "campaign_id": str(authoritative.campaign_id),
            "store_id": str(authoritative.store_id),
            "session": session_payload,
        }
        action_metadata = {
            "mode": mode,
            "currency": currency,
            "note": note,
        }
        action_body["manual"] = {
            key: value for key, value in action_metadata.items() if value is not None
        }
        api_request = GMVMaxSessionCreateRequest(
            advertiser_id=str(authoritative.advertiser_id),
            body=GMVMaxSessionCreateBody(
                campaign_id=str(authoritative.campaign_id),
                store_id=str(authoritative.store_id),
                session=GMVMaxSessionSettings(**session_payload),
            ),
        )

        try:
            _assert_mutation_generation(mutation, db)
            response = await client.gmv_max_session_create(api_request)
            _assert_mutation_generation(mutation, db)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None)
            response_payload = detail if isinstance(detail, dict) else None
            error_message = _extract_error_message(detail) if detail else str(exc)
            await update_heating_action_result(
                db,
                heating_id=heating.id,
                status="FAILED",
                action_type="APPLY_BOOST",
                action_time=action_time,
                request_payload=action_body,
                response_payload=response_payload,
                error_message=error_message,
            )
            _record_heating_audit(
                db,
                campaign=authoritative,
                heating=heating,
                action="APPLY_BOOST",
                reason=note,
                result="FAILED",
                request_payload=action_body,
                response_payload=response_payload,
                error_message=error_message,
                performed_by=performed_by,
                fencing_token=fencing_token,
                before_state=before_snapshot,
                after_state=before_snapshot,
            )
            db.flush()
            raise

        payload = response.data.model_dump(exclude_none=True)
        updated_row = await update_heating_action_result(
            db,
            heating_id=heating.id,
            status="APPLIED",
            action_type="APPLY_BOOST",
            action_time=action_time,
            request_payload=action_body,
            response_payload=payload,
            error_message=None,
        )
        after_snapshot = dict(
            after_state or _snapshot_campaign_state(authoritative)
        )
        _record_heating_audit(
            db,
            campaign=authoritative,
            heating=heating,
            action="APPLY_BOOST",
            reason=note,
            result="SUCCESS",
            request_payload=action_body,
            response_payload=payload,
            error_message=None,
            performed_by=performed_by,
            fencing_token=fencing_token,
            before_state=before_snapshot,
            after_state=after_snapshot,
        )
        db.flush()
        return updated_row, response
