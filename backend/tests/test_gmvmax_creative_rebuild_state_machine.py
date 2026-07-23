from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.features.tenants.ttb.gmv_max import service as create_service
from app.providers.tiktok_business.gmvmax_client import GMVMaxCampaignCreateBody
from app.services import gmvmax_creative_guard as guard


pytestmark = pytest.mark.anyio


class _Mutation:
    global_fencing_token = 1

    def __init__(self) -> None:
        self.commits = 0
        self.assertions = 0

    def assert_current(self, db) -> None:  # noqa: ANN001
        _ = db
        self.assertions += 1

    def commit(self, db) -> None:  # noqa: ANN001
        self.assert_current(db)
        db.commit()
        self.commits += 1


def _seed_source_scope(db_session) -> guard.CampaignScope:
    source = GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id="campaign-old",
        enabled=True,
        target_roi=Decimal("1.2"),
        cooldown_minutes=30,
        min_runtime_minutes_before_first_change=30,
        config_json={
            "creative_guard": {
                "enabled": True,
                "product_card_reset": {
                    "enabled": True,
                    "recreate": True,
                    "disable_old_strategy": True,
                },
            }
        },
    )
    db_session.add(source)
    db_session.add(
        GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-old",
            campaign_name="Old campaign",
            operation_status="ENABLE",
            secondary_status="CAMPAIGN_STATUS_ENABLE",
            shopping_ads_type="PRODUCT",
            product_specific_type="CUSTOMIZED_PRODUCTS",
            optimization_goal="VALUE",
            budget_cents=20_000,
            detail_raw_json={
                "campaign_id": "campaign-old",
                "campaign_name": "Old campaign",
                "store_id": "store-1",
                "item_group_ids": ["product-1"],
            },
        )
    )
    db_session.add(
        GmvmaxProductCampaignItemGroup(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-old",
            item_group_id="product-1",
        )
    )
    db_session.commit()
    db_session.refresh(source)
    return guard.CampaignScope(
        strategy_id=int(source.id),
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-old",
        campaign_name="Old campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=Decimal("1.2"),
        config={
            "product_card_reset": {
                "enabled": True,
                "recreate": True,
                "disable_old_strategy": True,
            }
        },
        monitor_state={},
        smart_guard_state={},
    )


def _replacement_body() -> GMVMaxCampaignCreateBody:
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(days=1)
    return GMVMaxCampaignCreateBody(
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        shopping_ads_type="PRODUCT",
        optimization_goal="VALUE",
        deep_bid_type="VO_MIN_ROAS",
        campaign_name="Creative guard replacement",
        budget=200,
        roas_bid=1.2,
        schedule_type="SCHEDULE_START_END",
        schedule_start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
        schedule_end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
        product_video_specific_type="AUTO_SELECTION",
    )


def _product_card_metric() -> guard.CreativeMetric:
    return guard.CreativeMetric(
        creative_id="-1",
        item_group_id="product-1",
        status="DELIVERING",
        cost_cents=1_000,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=100,
        product_clicks=2,
        ad_click_rate=Decimal("0.02"),
        product_click_rate=Decimal("0.02"),
        ad_conversion_rate=Decimal("0"),
        roi=Decimal("0"),
    )


def _rebuild_intent(db_session) -> GmvmaxCampaignCreateIntent:
    return (
        db_session.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.workspace_id == 1,
            GmvmaxCampaignCreateIntent.auth_id == 2,
            GmvmaxCampaignCreateIntent.replacement_campaign_id
            == "campaign-old",
        )
        .one()
    )


def _seed_prepared_rebuild_intent(
    db_session,
    scope: guard.CampaignScope,
    *,
    phase: str = "PREPARED",
    state: str = "PREPARED",
    campaign_id: str | None = None,
    historical_creatives: list[tuple[str, str | None]] | None = None,
) -> GmvmaxCampaignCreateIntent:
    proposed = guard._durable_rebuild_create_request(
        db_session,
        scope,
        _replacement_body(),
        historical_creatives=historical_creatives or [],
    )
    prepared = create_service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=int(scope.workspace_id),
        provider="tiktok-business",
        auth_id=int(scope.auth_id),
        advertiser_id=str(scope.advertiser_id),
        payload=proposed,
        store_authorized_bc_id="bc-1",
    )
    guard._mark_rebuild_phase(
        db_session,
        scope,
        prepared.frozen_payload,
        phase=phase,
        state=state,
        campaign_id=campaign_id,
    )
    db_session.commit()
    return _rebuild_intent(db_session)


@pytest.fixture(autouse=True)
def _isolate_state_machine_dependencies(monkeypatch):
    monkeypatch.setattr(
        create_service,
        "ensure_ttb_auth_in_workspace",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(create_service, "_ensure_provider", lambda value: value)
    monkeypatch.setattr(
        create_service,
        "_get_advertiser_timezone",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        guard,
        "assert_gmvmax_mutation_current",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        guard,
        "_assert_creative_guard_mutation_allowed",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        guard,
        "_manual_pause_override_active",
        lambda *args, **kwargs: False,
    )

    def _catalog_row(db, scope, campaign_id):  # noqa: ANN001
        return (
            db.query(GmvmaxProductCampaignCatalog)
            .filter(
                GmvmaxProductCampaignCatalog.workspace_id
                == int(scope.workspace_id),
                GmvmaxProductCampaignCatalog.auth_id == int(scope.auth_id),
                GmvmaxProductCampaignCatalog.advertiser_id
                == str(scope.advertiser_id),
                GmvmaxProductCampaignCatalog.store_id == str(scope.store_id),
                GmvmaxProductCampaignCatalog.campaign_id == str(campaign_id),
            )
            .one_or_none()
        )

    def _mark_disabled(db, scope, *, campaign_id=None):  # noqa: ANN001
        target = str(campaign_id or scope.campaign_id)
        row = _catalog_row(db, scope, target)
        if row is None:
            return False
        row.operation_status = "DISABLE"
        row.secondary_status = "CAMPAIGN_STATUS_DISABLE"
        if target == str(scope.campaign_id):
            scope.operation_status = "DISABLE"
            scope.secondary_status = "CAMPAIGN_STATUS_DISABLE"
        db.add(row)
        db.flush()
        return True

    def _mark_enabled(db, scope, *, campaign_id):  # noqa: ANN001
        target = str(campaign_id)
        row = _catalog_row(db, scope, target)
        if row is None:
            return False
        row.operation_status = "ENABLE"
        row.secondary_status = "CAMPAIGN_STATUS_ENABLE"
        if target == str(scope.campaign_id):
            scope.operation_status = "ENABLE"
            scope.secondary_status = "CAMPAIGN_STATUS_ENABLE"
        db.add(row)
        db.flush()
        return True

    # Realtime state is maintained by a production-only SQL table that is not
    # mapped into the isolated SQLite metadata. Keep the state-machine tests
    # focused on the durable catalog/intent checkpoints.
    monkeypatch.setattr(guard, "_mark_campaign_disabled_best_effort", _mark_disabled)
    monkeypatch.setattr(guard, "_mark_campaign_enabled_best_effort", _mark_enabled)


def _install_preflight(monkeypatch) -> None:
    async def _prepare(*args, **kwargs):
        return _replacement_body(), []

    monkeypatch.setattr(guard, "_prepare_recreated_campaign_body", _prepare)


async def test_prepared_checkpoint_is_committed_before_any_old_disable(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    _install_preflight(monkeypatch)
    monkeypatch.setattr(
        guard,
        "_rebuild_schedule_has_safety_margin",
        lambda *args, **kwargs: True,
    )

    class _StopAtOldDisable(RuntimeError):
        pass

    class _Client:
        def __init__(self) -> None:
            self.observed: tuple[str, str] | None = None
            self.closed = False

        async def campaign_status_update(self, request):  # noqa: ANN001
            assert request.campaign_ids == ["campaign-old"]
            assert request.operation_status == "DISABLE"

            # A separate connection must be able to see the checkpoint before
            # the first official write is allowed to leave this process.
            from app.data.db import SessionLocal

            observer = SessionLocal()
            try:
                intent = (
                    observer.query(GmvmaxCampaignCreateIntent)
                    .filter(
                        GmvmaxCampaignCreateIntent.workspace_id == 1,
                        GmvmaxCampaignCreateIntent.auth_id == 2,
                        GmvmaxCampaignCreateIntent.replacement_campaign_id
                        == "campaign-old",
                    )
                    .one()
                )
                self.observed = (
                    str(intent.state),
                    guard._intent_rebuild_phase(intent),
                )
            finally:
                observer.close()
            raise _StopAtOldDisable("fault injected before old DISABLE")

        async def aclose(self) -> None:
            self.closed = True

    client = _Client()
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: client,
    )

    with pytest.raises(_StopAtOldDisable):
        await guard._reset_campaign_for_product_card_unlocked(
            db_session,
            scope,
            _product_card_metric(),
            {"action": "RESET_CAMPAIGN", "reason": "test"},
            mutation=_Mutation(),
        )

    assert client.observed == ("PREPARED", "OLD_PAUSE_PENDING")
    assert client.closed is True
    intent = _rebuild_intent(db_session)
    assert intent.state == "PREPARED"
    assert guard._intent_rebuild_phase(intent) == "OLD_PAUSE_PENDING"


async def test_expired_pure_prepared_safety_window_never_enables_old(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    _install_preflight(monkeypatch)
    monkeypatch.setattr(
        guard,
        "_rebuild_schedule_has_safety_margin",
        lambda *args, **kwargs: False,
    )

    class _Client:
        def __init__(self) -> None:
            self.statuses: list[tuple[list[str], str]] = []

        async def campaign_status_update(self, request):  # noqa: ANN001
            self.statuses.append(
                (list(request.campaign_ids), str(request.operation_status))
            )
            return SimpleNamespace(request_id="status-1")

        async def aclose(self) -> None:
            return None

    client = _Client()
    mutation = _Mutation()
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: client,
    )

    with pytest.raises(
        guard.CreativeGuardAutomationHold,
        match="safety window expired",
    ):
        await guard._reset_campaign_for_product_card_unlocked(
            db_session,
            scope,
            _product_card_metric(),
            {"action": "RESET_CAMPAIGN", "reason": "test"},
            mutation=mutation,
        )

    intent = _rebuild_intent(db_session)
    db_session.refresh(intent)
    assert intent.state == "FAILED_TERMINAL"
    assert guard._intent_rebuild_phase(intent) == "SAFETY_WINDOW_EXPIRED_UNSENT"
    assert client.statuses == []
    old = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.campaign_id == "campaign-old",
        )
        .one()
    )
    assert old.operation_status == "ENABLE"

    request = guard._request_from_rebuild_intent(intent, scope)
    replayed = await guard._compensate_old_campaign_after_unsent_rebuild(
        db_session,
        client,
        scope,
        request,
        mutation=mutation,
        cause=RuntimeError("duplicate compensation delivery"),
    )
    assert replayed is False
    assert client.statuses == []


async def test_expired_old_paused_rebuild_compensates_exactly_once(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    _install_preflight(monkeypatch)
    safety_margin = {"available": True}
    monkeypatch.setattr(
        guard,
        "_rebuild_schedule_has_safety_margin",
        lambda *args, **kwargs: safety_margin["available"],
    )

    class _Crash(BaseException):
        pass

    async def _crash_after_old_pause(*args, **kwargs):
        raise _Crash("simulated process loss after OLD_PAUSED checkpoint")

    monkeypatch.setattr(
        guard,
        "_create_rebuild_after_occupancy_converges",
        _crash_after_old_pause,
    )

    class _Client:
        def __init__(self) -> None:
            self.statuses: list[tuple[list[str], str]] = []

        async def campaign_status_update(self, request):  # noqa: ANN001
            self.statuses.append(
                (list(request.campaign_ids), str(request.operation_status))
            )
            return SimpleNamespace(request_id=f"status-{len(self.statuses)}")

        async def aclose(self) -> None:
            return None

    client = _Client()
    mutation = _Mutation()
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: client,
    )

    with pytest.raises(_Crash):
        await guard._reset_campaign_for_product_card_unlocked(
            db_session,
            scope,
            _product_card_metric(),
            {"action": "RESET_CAMPAIGN", "reason": "test"},
            mutation=mutation,
        )

    intent = _rebuild_intent(db_session)
    db_session.refresh(intent)
    assert intent.state == "PREPARED"
    assert guard._intent_rebuild_phase(intent) == "OLD_PAUSED"
    assert client.statuses == [(["campaign-old"], "DISABLE")]

    safety_margin["available"] = False
    scope.config["dry_run"] = True
    with pytest.raises(
        guard.CreativeGuardAutomationHold,
        match="safety window expired",
    ):
        await guard._reset_campaign_for_product_card_unlocked(
            db_session,
            scope,
            _product_card_metric(),
            {
                "action": "RESET_CAMPAIGN",
                "reason": "creative_guard:rebuild_recovery",
            },
            mutation=mutation,
        )

    db_session.refresh(intent)
    assert intent.state == "FAILED_TERMINAL"
    assert guard._intent_rebuild_phase(intent) == "COMPENSATED"
    assert client.statuses == [
        (["campaign-old"], "DISABLE"),
        (["campaign-old"], "ENABLE"),
    ]
    request = guard._request_from_rebuild_intent(intent, scope)
    replayed = await guard._compensate_old_campaign_after_unsent_rebuild(
        db_session,
        client,
        scope,
        request,
        mutation=mutation,
        cause=RuntimeError("duplicate compensation delivery"),
    )
    assert replayed is False
    assert client.statuses == [
        (["campaign-old"], "DISABLE"),
        (["campaign-old"], "ENABLE"),
    ]


async def test_finalizing_recovery_ignores_later_dry_run_for_official_exclusion(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    intent = _seed_prepared_rebuild_intent(
        db_session,
        scope,
        phase="FINALIZING",
        state="FINALIZING",
        campaign_id="campaign-new",
        historical_creatives=[("creative-old", "product-1")],
    )
    scope.config["dry_run"] = True
    replacement = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-new",
        campaign_name="Creative guard replacement",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
        shopping_ads_type="PRODUCT",
        product_specific_type="CUSTOMIZED_PRODUCTS",
        optimization_goal="VALUE",
        budget_cents=20_000,
        detail_raw_json={
            "campaign_id": "campaign-new",
            "store_id": "store-1",
            "item_group_ids": ["product-1"],
        },
    )
    db_session.add(replacement)
    db_session.commit()

    class _Client:
        def __init__(self) -> None:
            self.statuses: list[tuple[list[str], str]] = []
            self.creative_requests = []

        async def campaign_status_update(self, request):  # noqa: ANN001
            self.statuses.append(
                (list(request.campaign_ids), str(request.operation_status))
            )
            return SimpleNamespace(request_id=f"status-{len(self.statuses)}")

        async def gmv_max_creative_status_update(self, request):  # noqa: ANN001
            self.creative_requests.append(request)
            return SimpleNamespace(request_id="creative-remove-1")

        async def aclose(self) -> None:
            return None

    client = _Client()
    mutation = _Mutation()
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: client,
    )
    async def _recovered_replacement(*args, **kwargs):
        return replacement

    monkeypatch.setattr(
        guard,
        "_create_rebuild_after_occupancy_converges",
        _recovered_replacement,
    )
    monkeypatch.setattr(
        guard,
        "apply_approved_plan_defaults_to_strategy",
        lambda *args, **kwargs: None,
    )

    async def _official_exclusion(
        db,
        recovery_scope,
        *,
        new_campaign_id,
        creatives,
    ):
        assert recovery_scope.config["dry_run"] is False
        items, _ = guard._historical_exclusion_item_list(creatives)
        request = guard.GMVMaxCreativeStatusUpdateRequest(
            advertiser_id=str(recovery_scope.advertiser_id),
            body=guard.GMVMaxCreativeStatusUpdateBody(
                campaign_id=str(new_campaign_id),
                item_list=items,
                action="REMOVE",
            ),
        )
        mutation.assert_current(db)
        await client.gmv_max_creative_status_update(request)
        mutation.assert_current(db)
        return {"requested": len(items), "excluded": len(items)}

    monkeypatch.setattr(
        guard,
        "_exclude_historical_removed_creatives",
        _official_exclusion,
    )

    _, response = await guard._reset_campaign_for_product_card_unlocked(
        db_session,
        scope,
        _product_card_metric(),
        {
            "action": "RESET_CAMPAIGN",
            "reason": "creative_guard:rebuild_recovery",
        },
        mutation=mutation,
    )

    assert response["new_campaign_enabled"] is True
    assert len(client.creative_requests) == 1
    creative_request = client.creative_requests[0]
    assert creative_request.body.action == "REMOVE"
    assert creative_request.body.campaign_id == "campaign-new"
    assert creative_request.body.item_list[0].item_id == "creative-old"
    db_session.refresh(intent)
    assert intent.state == "SUCCEEDED"


async def test_failed_replacement_disable_stays_remote_created_until_retry_quarantines(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    _install_preflight(monkeypatch)
    monkeypatch.setattr(
        guard,
        "_rebuild_schedule_has_safety_margin",
        lambda *args, **kwargs: True,
    )
    mutation = _Mutation()
    monkeypatch.setattr(
        guard,
        "active_gmvmax_mutation_lease",
        lambda db: mutation,
    )

    async def _create_replacement(*args, **kwargs):
        row = GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-new",
            campaign_name="Creative guard replacement",
            operation_status="ENABLE",
            secondary_status="CAMPAIGN_STATUS_ENABLE",
            shopping_ads_type="PRODUCT",
            product_specific_type="CUSTOMIZED_PRODUCTS",
            optimization_goal="VALUE",
            budget_cents=20_000,
            detail_raw_json={
                "campaign_id": "campaign-new",
                "campaign_name": "Creative guard replacement",
                "store_id": "store-1",
                "item_group_ids": ["product-1"],
            },
        )
        db_session.add(row)
        db_session.flush()
        return row

    monkeypatch.setattr(
        guard,
        "_create_rebuild_after_occupancy_converges",
        _create_replacement,
    )

    class _FailingPauseClient:
        def __init__(self) -> None:
            self.old_disable_calls = 0
            self.new_disable_calls = 0
            self.closed = False

        async def campaign_status_update(self, request):  # noqa: ANN001
            assert request.operation_status == "DISABLE"
            campaign_id = str(request.campaign_ids[0])
            if campaign_id == "campaign-old":
                self.old_disable_calls += 1
                return SimpleNamespace(request_id="old-pause")
            assert campaign_id == "campaign-new"
            self.new_disable_calls += 1
            raise RuntimeError("replacement DISABLE failed")

        async def aclose(self) -> None:
            self.closed = True

    failing_client = _FailingPauseClient()
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: failing_client,
    )

    with pytest.raises(RuntimeError, match="replacement DISABLE failed"):
        await guard._reset_campaign_for_product_card_unlocked(
            db_session,
            scope,
            _product_card_metric(),
            {"action": "RESET_CAMPAIGN", "reason": "test"},
            mutation=mutation,
        )

    intent = _rebuild_intent(db_session)
    db_session.refresh(intent)
    assert intent.state == "REMOTE_CREATED"
    assert guard._intent_rebuild_phase(intent) == "QUARANTINE_PENDING"
    assert failing_client.old_disable_calls == 1
    assert failing_client.new_disable_calls == 2
    assert failing_client.closed is True
    replacement_strategy = (
        db_session.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.campaign_id == "campaign-new")
        .one()
    )
    assert (
        replacement_strategy.config_json["creation_quarantine"]["state"]
        == "QUARANTINE_PENDING"
    )

    class _RecoveryClient:
        def __init__(self) -> None:
            self.disable_calls = 0
            self.closed = False

        async def campaign_status_update(self, request):  # noqa: ANN001
            assert request.campaign_ids == ["campaign-new"]
            assert request.operation_status == "DISABLE"
            self.disable_calls += 1
            return SimpleNamespace(request_id="recovery-pause")

        async def aclose(self) -> None:
            self.closed = True

    recovery_client = _RecoveryClient()

    @contextmanager
    def _lease(*args, **kwargs):
        yield mutation

    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _lease)
    monkeypatch.setattr(
        guard,
        "build_ttb_gmvmax_client",
        lambda *args, **kwargs: recovery_client,
    )

    recovered = await guard._retry_rebuild_quarantine(
        db_session,
        scope,
        intent,
    )

    assert recovered is True
    assert recovery_client.disable_calls == 1
    assert recovery_client.closed is True
    db_session.refresh(intent)
    assert intent.state == "QUARANTINED"
    assert guard._intent_rebuild_phase(intent) == "QUARANTINED"
    db_session.refresh(replacement_strategy)
    assert (
        replacement_strategy.config_json["creation_quarantine"]["state"]
        == "QUARANTINED"
    )


async def test_finalizing_reentry_upserts_one_strategy_and_one_product_relation(
    db_session,
):
    scope = _seed_source_scope(db_session)

    first = guard._upsert_replacement_strategy(
        db_session,
        scope,
        "campaign-new",
        quarantine_state="FINALIZING",
    )
    guard._copy_campaign_item_groups(
        db_session,
        scope,
        "campaign-new",
        item_group_ids=["product-1"],
    )
    db_session.commit()

    second = guard._upsert_replacement_strategy(
        db_session,
        scope,
        "campaign-new",
        quarantine_state="FINALIZING",
    )
    guard._copy_campaign_item_groups(
        db_session,
        scope,
        "campaign-new",
        item_group_ids=["product-1"],
    )
    db_session.commit()

    assert second.id == first.id
    assert (
        db_session.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == 1,
            GmvStrategyConfig.auth_id == 2,
            GmvStrategyConfig.campaign_id == "campaign-new",
        )
        .count()
        == 1
    )
    assert (
        db_session.query(GmvmaxProductCampaignItemGroup)
        .filter(
            GmvmaxProductCampaignItemGroup.workspace_id == 1,
            GmvmaxProductCampaignItemGroup.auth_id == 2,
            GmvmaxProductCampaignItemGroup.campaign_id == "campaign-new",
            GmvmaxProductCampaignItemGroup.item_group_id == "product-1",
        )
        .count()
        == 1
    )


async def test_recovery_candidates_include_enabled_strategy_when_guard_disabled(
    db_session,
):
    scope = _seed_source_scope(db_session)
    intent = _seed_prepared_rebuild_intent(db_session, scope)
    strategy = (
        db_session.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.id == int(scope.strategy_id))
        .one()
    )
    strategy.config_json = {
        "creative_guard_enabled": False,
        "creative_guard": {"enabled": False},
    }
    db_session.add(strategy)
    db_session.commit()

    candidates = guard._rebuild_recovery_candidates(db_session)

    assert len(candidates) == 1
    candidate_scope, candidate_intent, strategy_enabled = candidates[0]
    assert int(candidate_intent.id) == int(intent.id)
    assert candidate_scope.campaign_id == "campaign-old"
    assert candidate_scope.config["enabled"] is False
    assert strategy_enabled is True


async def test_revoked_strategy_terminalizes_without_enabling_campaign(
    monkeypatch,
    db_session,
):
    scope = _seed_source_scope(db_session)
    intent = _seed_prepared_rebuild_intent(
        db_session,
        scope,
        phase="OLD_PAUSED",
        state="COMPENSATION_PENDING",
    )
    strategy = (
        db_session.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.id == int(scope.strategy_id))
        .one()
    )
    strategy.enabled = False
    source_catalog = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.campaign_id == "campaign-old",
        )
        .one()
    )
    source_catalog.operation_status = "DISABLE"
    source_catalog.secondary_status = "CAMPAIGN_STATUS_DISABLE"
    db_session.add_all([strategy, source_catalog])
    db_session.commit()

    mutation = _Mutation()

    @contextmanager
    def _lease(*args, **kwargs):
        yield mutation

    monkeypatch.setattr(guard, "gmvmax_mutation_lease", _lease)
    candidates = guard._rebuild_recovery_candidates(db_session)
    assert len(candidates) == 1
    recovery_scope, candidate_intent, strategy_enabled = candidates[0]
    assert strategy_enabled is False

    terminalized = guard._terminalize_rebuild_after_permission_revoked(
        db_session,
        recovery_scope,
        candidate_intent,
    )

    assert terminalized is True
    db_session.refresh(intent)
    db_session.refresh(source_catalog)
    assert intent.state == "FAILED_TERMINAL"
    assert guard._intent_rebuild_phase(intent) == "PERMISSION_REVOKED"
    assert source_catalog.operation_status == "DISABLE"
    assert mutation.commits == 1


async def test_more_than_one_page_of_orphans_does_not_starve_valid_rebuild(
    db_session,
):
    scope = _seed_source_scope(db_session)
    for index in range(101):
        db_session.add(
            GmvmaxCampaignCreateIntent(
                workspace_id=1,
                auth_id=2,
                advertiser_id=f"orphan-advertiser-{index}",
                store_id=f"orphan-store-{index}",
                idempotency_key=f"orphan-{index:04d}",
                client_payload_sha256="a" * 64,
                payload_sha256="b" * 64,
                official_request_id=f"orphan-request-{index:04d}",
                campaign_name=f"Orphan {index}",
                replacement_campaign_id=f"missing-campaign-{index}",
                state="PREPARED",
                request_json={
                    "advertiser_id": f"orphan-advertiser-{index}",
                    "store_id": f"orphan-store-{index}",
                    "campaign_name": f"Orphan {index}",
                    "idempotency_key": f"orphan-{index:04d}",
                    "request_id": f"orphan-request-{index:04d}",
                    "replacement_campaign_id": f"missing-campaign-{index}",
                    "automation": {"source": "creative_guard_rebuild"},
                },
            )
        )
    db_session.commit()
    valid_intent = _seed_prepared_rebuild_intent(db_session, scope)

    candidates = guard._rebuild_recovery_candidates(db_session)

    assert any(
        int(candidate_intent.id) == int(valid_intent.id)
        and strategy_enabled
        for _, candidate_intent, strategy_enabled in candidates
    )


async def test_held_prepared_batch_rotates_without_touching_remote_heartbeat(
    db_session,
):
    intent_ids: list[int] = []
    for index in range(101):
        campaign_id = f"campaign-fair-{index:03d}"
        strategy = GmvStrategyConfig(
            workspace_id=1,
            auth_id=2,
            campaign_id=campaign_id,
            enabled=True,
            config_json={"creative_guard": {"enabled": True}},
        )
        catalog = GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id=campaign_id,
            campaign_name=f"Campaign {index}",
            operation_status="ENABLE",
            secondary_status="CAMPAIGN_STATUS_ENABLE",
            shopping_ads_type="PRODUCT",
            product_specific_type="CUSTOMIZED_PRODUCTS",
            detail_raw_json={"item_group_ids": [f"product-{index}"]},
        )
        intent = GmvmaxCampaignCreateIntent(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            idempotency_key=f"fair-{index:04d}",
            client_payload_sha256="a" * 64,
            payload_sha256="b" * 64,
            official_request_id=f"fair-request-{index:04d}",
            campaign_name=f"Replacement {index}",
            replacement_campaign_id=campaign_id,
            state="PREPARED",
            request_json={
                "advertiser_id": "advertiser-1",
                "store_id": "store-1",
                "campaign_name": f"Replacement {index}",
                "idempotency_key": f"fair-{index:04d}",
                "request_id": f"fair-request-{index:04d}",
                "replacement_campaign_id": campaign_id,
                "automation": {"source": "creative_guard_rebuild"},
            },
        )
        db_session.add_all([strategy, catalog, intent])
        db_session.flush()
        intent_ids.append(int(intent.id))
    db_session.commit()

    first_batch = guard._rebuild_recovery_candidates(db_session)
    first_ids = {int(intent.id) for _, intent, _ in first_batch}
    assert len(first_batch) == 100
    assert intent_ids[-1] not in first_ids

    for scope, intent, _ in first_batch:
        assert guard._rotate_rebuild_recovery_candidate(
            db_session,
            scope,
            intent,
        )

    second_batch = guard._rebuild_recovery_candidates(db_session)
    second_ids = {int(intent.id) for _, intent, _ in second_batch}
    assert intent_ids[-1] in second_ids

    remote_scope, remote_intent, _ = second_batch[0]
    remote_intent.state = "REMOTE_CREATED"
    db_session.add(remote_intent)
    db_session.commit()
    db_session.refresh(remote_intent)
    remote_updated_at = remote_intent.updated_at
    assert (
        guard._rotate_rebuild_recovery_candidate(
            db_session,
            remote_scope,
            remote_intent,
        )
        is False
    )
    db_session.refresh(remote_intent)
    assert remote_intent.updated_at == remote_updated_at
