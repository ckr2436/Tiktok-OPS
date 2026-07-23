from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

import app.celery_app  # noqa: F401 - establish production task import order
from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.features.tenants.ttb.gmv_max import router_provider as gmvmax_router_provider
from app.services import (
    gmvmax_creative_guard,
    gmvmax_hermes_advisor,
    gmvmax_hermes_daily_report,
    gmvmax_smart_guard,
)


@pytest.fixture()
def guard_tables(db_session):
    db_session.execute(text("drop table if exists gmv_campaign_guard_events"))
    db_session.execute(text("drop table if exists gmv_campaign_realtime_state"))
    db_session.execute(
        text(
            """
            create table gmv_campaign_realtime_state (
                id integer primary key autoincrement,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                campaign_name varchar(255),
                promotion_type varchar(16),
                operation_status varchar(32),
                secondary_status varchar(128),
                strategy_id integer,
                report_start_date date,
                report_end_date date,
                latest_cost_cents integer,
                latest_gross_revenue_cents integer,
                latest_orders integer,
                last_report_at datetime,
                runtime_json text,
                state_version integer not null default 0,
                guard_status varchar(32),
                last_action varchar(32),
                last_reason varchar(512),
                last_checked_at datetime,
                updated_at datetime
            )
            """
        )
    )
    db_session.execute(
        text(
            """
            create table gmv_campaign_guard_events (
                id integer primary key autoincrement,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                strategy_id integer,
                event_type varchar(64) not null,
                action varchar(32) not null,
                reason varchar(512),
                result varchar(32) not null,
                request_json text,
                response_json text,
                created_at datetime
            )
            """
        )
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(text("drop table if exists gmv_campaign_guard_events"))
        db_session.execute(text("drop table if exists gmv_campaign_realtime_state"))
        db_session.commit()


@pytest.fixture()
def hermes_scope_tables(db_session):
    db_session.execute(text("drop table if exists gmv_hermes_ad_recommendations"))
    db_session.execute(text("drop table if exists gmv_hermes_ad_learning_samples"))
    db_session.execute(
        text(
            """
            create table gmv_hermes_ad_recommendations (
                id integer primary key autoincrement,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                recommendation_type varchar(64) not null,
                recommendation_json text
            )
            """
        )
    )
    db_session.execute(
        text(
            """
            create table gmv_hermes_ad_learning_samples (
                id integer primary key autoincrement,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                action varchar(32),
                cost_cents integer,
                gross_revenue_cents integer,
                orders integer,
                roi numeric,
                observed_at datetime not null
            )
            """
        )
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(text("drop table if exists gmv_hermes_ad_recommendations"))
        db_session.execute(text("drop table if exists gmv_hermes_ad_learning_samples"))
        db_session.commit()


def _catalog(
    *,
    advertiser_id: str = "adv-target",
    store_id: str | None = "store-target",
    campaign_name: str = "Target campaign",
) -> GmvmaxProductCampaignCatalog:
    return GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id="campaign-shared",
        campaign_name=campaign_name,
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        shopping_ads_type="PRODUCT",
    )


def _live_catalog(
    *,
    advertiser_id: str = "adv-target",
    store_id: str | None = "store-target",
) -> GmvmaxLiveCampaignCatalog:
    return GmvmaxLiveCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id="campaign-shared",
        campaign_name="Target live campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        shopping_ads_type="LIVE",
    )


def _strategy() -> GmvStrategyConfig:
    return GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id="campaign-shared",
        enabled=True,
        config_json={
            "hermes_enabled": True,
            "creative_guard": {"enabled": True},
            "creative_guard_state": {"origin": "strategy"},
        },
    )


def _smart_campaign(
    *,
    advertiser_id: str = "adv-target",
    store_id: str = "store-target",
    promotion_type: str = "PRODUCT",
) -> gmvmax_smart_guard.CatalogCampaign:
    return gmvmax_smart_guard.CatalogCampaign(
        workspace_id=1,
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id="campaign-shared",
        campaign_name="Target campaign",
        promotion_type=promotion_type,
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_value=10_000,
        roas_bid=Decimal("2"),
    )


def _scope() -> gmvmax_creative_guard.CampaignScope:
    return gmvmax_creative_guard.CampaignScope(
        strategy_id=1,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-target",
        store_id="store-target",
        campaign_id="campaign-shared",
        campaign_name="Target campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=10_000,
        roas_bid=Decimal("2"),
        config={},
        monitor_state={},
        smart_guard_state={},
    )


def test_creative_guard_scope_loader_does_not_read_other_advertiser_runtime(
    db_session,
    guard_tables,
) -> None:
    db_session.add_all([_strategy(), _catalog()])
    db_session.flush()
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                operation_status, secondary_status, runtime_json
            ) values (
                1, 2, 'adv-wrong', 'store-wrong', 'campaign-shared',
                'DISABLE', 'CAMPAIGN_STATUS_DISABLE',
                '{"creative_guard_state":{"origin":"wrong-runtime"}}'
            )
            """
        )
    )
    db_session.commit()

    scopes = gmvmax_creative_guard._load_scopes(db_session)

    assert len(scopes) == 1
    assert scopes[0].advertiser_id == "adv-target"
    assert scopes[0].store_id == "store-target"
    assert scopes[0].monitor_state == {"origin": "strategy"}


def test_campaign_enabled_check_does_not_read_other_store_runtime(
    db_session,
    guard_tables,
) -> None:
    db_session.add(_catalog())
    db_session.flush()
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                operation_status, secondary_status
            ) values (
                1, 2, 'adv-target', 'store-wrong', 'campaign-shared',
                'DISABLE', 'CAMPAIGN_STATUS_DISABLE'
            )
            """
        )
    )
    db_session.commit()

    assert gmvmax_creative_guard._campaign_is_currently_enabled(
        db_session,
        _scope(),
    )


def test_already_reset_campaign_requires_complete_scope(
    db_session,
    guard_tables,
) -> None:
    db_session.execute(
        text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                event_type, action, result
            ) values (
                99, 98, 'adv-wrong', 'store-wrong', 'campaign-shared',
                'CREATIVE_GUARD', 'RESET_CAMPAIGN', 'SUCCESS'
            )
            """
        )
    )
    db_session.commit()

    assert not gmvmax_creative_guard._already_reset_campaign(db_session, _scope())

    db_session.execute(
        text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                event_type, action, result
            ) values (
                1, 2, 'adv-target', 'store-target', 'campaign-shared',
                'CREATIVE_GUARD', 'RESET_CAMPAIGN', 'SUCCESS'
            )
            """
        )
    )
    db_session.commit()

    assert gmvmax_creative_guard._already_reset_campaign(db_session, _scope())


def test_daily_creative_summary_does_not_take_other_advertiser_catalog_name(
    db_session,
) -> None:
    report_date = date(2026, 7, 16)
    db_session.add_all(
        [
            _catalog(campaign_name="AAA target campaign"),
            _catalog(
                advertiser_id="adv-wrong",
                store_id="store-wrong",
                campaign_name="ZZZ wrong campaign",
            ),
            GmvmaxProductCreativeMetricsDaily(
                workspace_id=1,
                auth_id=2,
                advertiser_id="adv-target",
                store_id="store-target",
                campaign_id="campaign-shared",
                item_group_id="product-1",
                creative_id="creative-1",
                stat_time_day=report_date,
                creative_delivery_status="ENABLE",
                cost_cents=100,
                gross_revenue_cents=300,
                orders=1,
            ),
        ]
    )
    db_session.commit()

    summary = gmvmax_hermes_daily_report._load_creative_summary(
        db_session,
        {
            "workspace_id": 1,
            "auth_id": 2,
            "advertiser_id": "adv-target",
            "store_id": "store-target",
        },
        report_date,
    )

    assert len(summary) == 1
    assert summary[0]["campaign_name"] == "AAA target campaign"


def test_hermes_advisor_fails_closed_when_strategy_catalog_scope_is_ambiguous(
    db_session,
) -> None:
    db_session.add_all(
        [
            _strategy(),
            _catalog(),
            _catalog(advertiser_id="adv-other", store_id="store-other"),
        ]
    )
    db_session.commit()

    assert gmvmax_hermes_advisor._load_hermes_scopes(db_session) == []


def test_hermes_advisor_fails_closed_when_catalog_store_scope_is_missing(
    db_session,
) -> None:
    db_session.add_all([_strategy(), _catalog(store_id=None)])
    db_session.commit()

    assert gmvmax_hermes_advisor._load_hermes_scopes(db_session) == []


def test_creative_guard_fails_closed_for_ambiguous_catalog_and_raw_fallback(
    db_session,
    guard_tables,
) -> None:
    target = _catalog()
    wrong = _catalog(advertiser_id="adv-wrong", store_id="store-wrong")
    wrong.detail_raw_json = {"item_group_ids": ["wrong-product"]}
    db_session.add_all([_strategy(), target, wrong])
    db_session.commit()

    assert gmvmax_creative_guard._load_scopes(db_session) == []

    db_session.delete(target)
    db_session.commit()
    assert gmvmax_creative_guard._campaign_item_group_ids(db_session, _scope()) == []


def test_runtime_state_fallbacks_are_unique_and_full_scope(
    db_session,
    guard_tables,
) -> None:
    strategy = _strategy()
    strategy.config_json = {
        **dict(strategy.config_json or {}),
        "smart_guard_state": {"origin": "legacy"},
    }
    db_session.add(strategy)
    db_session.flush()
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                runtime_json, state_version, updated_at
            ) values
                (1, 2, 'adv-wrong-a', 'store-wrong-a', 'campaign-shared',
                 '{"smart_guard_state":{"origin":"wrong-a"}}', 1, '2026-07-17 01:00:00'),
                (1, 2, 'adv-wrong-b', 'store-wrong-b', 'campaign-shared',
                 '{"smart_guard_state":{"origin":"wrong-b"}}', 1, '2026-07-17 02:00:00')
            """
        )
    )
    db_session.commit()

    runtime = gmvmax_smart_guard._load_runtime_state(db_session, strategy)
    assert runtime["smart_guard_state"] == {"origin": "legacy"}

    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                runtime_json, state_version, updated_at
            ) values (
                1, 2, 'adv-target', 'store-target', 'campaign-shared',
                '{"smart_guard_state":{"origin":"target"}}', 1, '2026-07-17 03:00:00'
            )
            """
        )
    )
    db_session.commit()

    runtime = gmvmax_smart_guard._load_runtime_state(
        db_session,
        strategy,
        campaign=_smart_campaign(),
    )
    assert runtime["smart_guard_state"] == {"origin": "target"}

    gmvmax_smart_guard._set_smart_guard_state(strategy, {"origin": "persisted"})
    gmvmax_smart_guard._persist_runtime_state(
        db_session,
        strategy,
        campaign=_smart_campaign(),
        now=datetime(2026, 7, 17, 4, tzinfo=timezone.utc),
    )
    db_session.commit()
    rows = db_session.execute(
        text(
            """
            select advertiser_id, store_id, runtime_json
            from gmv_campaign_realtime_state
            order by advertiser_id
            """
        )
    ).mappings().all()
    runtime_by_scope = {
        (str(row["advertiser_id"]), str(row["store_id"])): json.loads(row["runtime_json"])
        for row in rows
    }
    assert runtime_by_scope[("adv-target", "store-target")]["smart_guard_state"] == {
        "origin": "persisted"
    }
    assert runtime_by_scope[("adv-wrong-a", "store-wrong-a")]["smart_guard_state"] == {
        "origin": "wrong-a"
    }
    assert runtime_by_scope[("adv-wrong-b", "store-wrong-b")]["smart_guard_state"] == {
        "origin": "wrong-b"
    }

    creative_scope = _scope()
    creative_scope.strategy_id = int(strategy.id)
    gmvmax_creative_guard._update_creative_guard_state(
        db_session,
        creative_scope,
        now=datetime(2026, 7, 17, 5, tzinfo=timezone.utc),
        interval_minutes=1,
        checked_creatives=3,
        action_count=0,
    )
    db_session.commit()
    wrong_runtime = db_session.execute(
        text(
            """
            select runtime_json
            from gmv_campaign_realtime_state
            where advertiser_id='adv-wrong-a' and store_id='store-wrong-a'
            """
        )
    ).scalar_one()
    target_runtime = db_session.execute(
        text(
            """
            select runtime_json
            from gmv_campaign_realtime_state
            where advertiser_id='adv-target' and store_id='store-target'
            """
        )
    ).scalar_one()
    assert "creative_guard_state" not in json.loads(wrong_runtime)
    assert json.loads(target_runtime)["creative_guard_state"]["checked_creatives"] == 3


def test_runtime_state_strategy_id_match_has_priority(
    db_session,
    guard_tables,
) -> None:
    strategy = _strategy()
    db_session.add(strategy)
    db_session.flush()
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, runtime_json, state_version, updated_at
            ) values
                (99, 98, 'adv-linked', 'store-linked', 'other-campaign',
                 :strategy_id, '{"smart_guard_state":{"origin":"strategy-id"}}',
                 1, '2026-07-17 01:00:00'),
                (1, 2, 'adv-target', 'store-target', 'campaign-shared',
                 null, '{"smart_guard_state":{"origin":"scope-fallback"}}',
                 1, '2026-07-17 02:00:00')
            """
        ),
        {"strategy_id": int(strategy.id)},
    )
    db_session.commit()

    runtime = gmvmax_smart_guard._load_runtime_state(
        db_session,
        strategy,
        campaign=_smart_campaign(),
    )

    assert runtime["smart_guard_state"] == {"origin": "strategy-id"}


def test_smart_guard_catalog_lookup_requires_one_product_or_live_scope(
    db_session,
) -> None:
    strategy = _strategy()
    product = _catalog()
    live = _live_catalog()
    db_session.add_all([strategy, product, live])
    db_session.commit()

    assert gmvmax_smart_guard._load_catalog_campaign(db_session, strategy) is None

    db_session.delete(live)
    db_session.commit()
    product.store_id = "0"
    db_session.commit()
    assert gmvmax_smart_guard._load_catalog_campaign(db_session, strategy) is None

    product.store_id = "store-target"
    db_session.commit()
    campaign = gmvmax_smart_guard._load_catalog_campaign(db_session, strategy)
    assert campaign is not None
    assert campaign.promotion_type == "PRODUCT"
    assert campaign.advertiser_id == "adv-target"
    assert campaign.store_id == "store-target"

    db_session.add(_catalog(advertiser_id="adv-other", store_id="store-other"))
    db_session.commit()
    assert gmvmax_smart_guard._load_catalog_campaign(db_session, strategy) is None


def test_smart_guard_reads_previous_state_and_campaign_dates_in_full_scope(
    db_session,
    guard_tables,
    monkeypatch,
) -> None:
    wrong = _catalog(advertiser_id="adv-wrong", store_id="store-wrong")
    wrong.create_time_utc = datetime(2026, 7, 1)
    db_session.add(wrong)
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                report_start_date, report_end_date, latest_cost_cents,
                latest_gross_revenue_cents, latest_orders, last_report_at
            ) values (
                1, 2, 'adv-target', 'store-wrong', 'campaign-shared',
                '2026-07-17', '2026-07-17', 10000, 20000, 10,
                '2026-07-17 01:00:00'
            )
            """
        )
    )
    db_session.commit()
    campaign = _smart_campaign()

    assert gmvmax_smart_guard._campaign_start_at_utc(db_session, campaign) is None
    assert gmvmax_smart_guard._campaign_created_at_utc(db_session, campaign) is None

    monkeypatch.setattr(
        gmvmax_smart_guard,
        "_campaign_report_date_range",
        lambda *_args, **_kwargs: (date(2026, 7, 17), date(2026, 7, 17)),
    )
    quality = gmvmax_smart_guard._assess_realtime_metrics_quality(
        db_session,
        strategy=_strategy(),
        campaign=campaign,
        metrics=gmvmax_smart_guard.RealtimeMetrics(
            cost_cents=100,
            gross_revenue_cents=200,
            orders=1,
            row_count=1,
            fetched_at=datetime(2026, 7, 17, 2, tzinfo=timezone.utc),
        ),
    )
    assert quality["valid"] is True
    assert quality["state"] == "fresh"


def test_smart_guard_catalog_writes_do_not_touch_other_advertiser(
    db_session,
    monkeypatch,
) -> None:
    db_session.add_all(
        [
            _strategy(),
            _catalog(),
            _catalog(advertiser_id="adv-wrong", store_id="store-wrong"),
        ]
    )
    db_session.commit()

    class _Response:
        def model_dump(self, **_kwargs):
            return {"ok": True}

    class _Client:
        async def campaign_status_update(self, _request):
            return _Response()

        async def gmv_max_campaign_update(self, _request):
            return _Response()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        gmvmax_smart_guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: _Client(),
    )
    campaign = _smart_campaign()

    class _Mutation:
        def assert_current(self, _db):
            return None

        def commit(self, db):
            db.commit()

    mutation = _Mutation()
    asyncio.run(
        gmvmax_smart_guard._apply_status_action_unlocked(
            db_session,
            campaign=campaign,
            action="PAUSE",
            mutation=mutation,
        )
    )
    asyncio.run(
        gmvmax_smart_guard._apply_campaign_adjustment_unlocked(
            db_session,
            campaign=campaign,
            adjustment={"budget": 123, "budget_cents": 12_300},
            mutation=mutation,
        )
    )
    db_session.commit()

    rows = db_session.execute(
        text(
            """
            select advertiser_id, store_id, operation_status, budget_cents
            from gmvmax_product_campaign_catalog
            where campaign_id='campaign-shared'
            order by advertiser_id
            """
        )
    ).mappings().all()
    by_advertiser = {str(row["advertiser_id"]): dict(row) for row in rows}
    assert by_advertiser["adv-target"]["operation_status"] == "DISABLE"
    assert int(by_advertiser["adv-target"]["budget_cents"]) == 12_300
    assert by_advertiser["adv-wrong"]["operation_status"] == "ENABLE"
    assert by_advertiser["adv-wrong"]["budget_cents"] is None


def test_hermes_advisor_reads_recommendations_samples_and_products_in_full_scope(
    db_session,
    hermes_scope_tables,
) -> None:
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.execute(
        text(
            """
            insert into gmv_hermes_ad_recommendations (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                recommendation_type, recommendation_json
            ) values
                (1, 2, 'adv-wrong', 'store-wrong', 'campaign-shared',
                 'GMVMAX_STRATEGY', '{"origin":"wrong"}'),
                (1, 2, 'adv-target', 'store-target', 'campaign-shared',
                 'GMVMAX_STRATEGY', '{"origin":"target"}')
            """
        )
    )
    db_session.execute(
        text(
            """
            insert into gmv_hermes_ad_learning_samples (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                action, cost_cents, gross_revenue_cents, orders, roi, observed_at
            ) values
                (1, 2, 'adv-wrong', 'store-wrong', 'campaign-shared',
                 'PAUSE', 9000, 0, 0, 0, :observed_at),
                (1, 2, 'adv-target', 'store-target', 'campaign-shared',
                 'START', 100, 300, 1, 3, :observed_at)
            """
        ),
        {"observed_at": observed_at},
    )
    db_session.execute(
        text(
            """
            insert into ttb_products (
                workspace_id, auth_id, product_id, store_id, effective_price
            ) values
                (1, 2, 'product-target', 'store-target', 12.34),
                (1, 2, 'product-wrong', 'store-wrong', 99.99)
            """
        )
    )
    db_session.execute(
        text(
            """
            insert into gmvmax_product_campaign_item_groups (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id, item_group_id
            ) values
                (1, 2, 'adv-target', 'store-target', 'campaign-shared', 'product-target'),
                (1, 2, 'adv-wrong', 'store-wrong', 'campaign-shared', 'product-wrong')
            """
        )
    )
    db_session.commit()
    scope = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-target",
        "store_id": "store-target",
        "campaign_id": "campaign-shared",
    }

    assert gmvmax_hermes_advisor._last_realtime_review(db_session, scope) == {
        "origin": "target"
    }
    stats = gmvmax_hermes_advisor._sample_stats(db_session, scope, hours=24)
    assert int(stats["samples"]) == 1
    assert int(stats["starts"]) == 1
    assert int(stats["pauses"]) == 0
    assert int(stats["latest_cost_cents"]) == 100
    price = gmvmax_hermes_advisor._price_basis(db_session, scope)
    assert price["item_group_id"] == "product-target"
    assert int(price["cents"]) == 1234


def test_remaining_guard_scope_writes_include_advertiser_and_store() -> None:
    def normalized(function) -> str:
        return re.sub(r"\s+", " ", inspect.getsource(function)).lower()

    creative_disable = normalized(gmvmax_creative_guard._mark_campaign_disabled_best_effort)
    assert creative_disable.count("and store_id=:store_id") >= 2
    duplicate_pause = normalized(gmvmax_creative_guard._persist_duplicate_pause_result)
    assert "and advertiser_id=:advertiser_id and store_id=:store_id" in duplicate_pause
    clone = normalized(gmvmax_creative_guard._clone_campaign_body)
    assert "and advertiser_id=:advertiser_id and store_id=:store_id" in clone
    reset = normalized(
        gmvmax_creative_guard._reset_campaign_for_product_card_unlocked
    )
    # Rebuilds now trust only the exact create response and never perform the
    # former ambiguous name-based catalog fallback.
    assert "campaign_name=:campaign_name" not in reset
    assert "execution_guard=mutation.assert_current" in reset

    cycle = normalized(gmvmax_smart_guard.run_smart_guard_cycle)
    data_hold_update = cycle.split("set guard_status='data_hold'", 1)[1].split("db.add(strategy)", 1)[0]
    assert "and advertiser_id=:advertiser_id" in data_hold_update
    assert "and store_id=:store_id" in data_hold_update

    product_conflicts = normalized(gmvmax_router_provider._ensure_campaign_products_available)
    assert (
        "gmvmaxproductcampaigncatalog.store_id "
        "== gmvmaxproductcampaignitemgroup.store_id"
    ) in product_conflicts
    assert (
        "gmvmaxproductcampaignitemgroup.advertiser_id == str(advertiser_id)"
    ) in product_conflicts
