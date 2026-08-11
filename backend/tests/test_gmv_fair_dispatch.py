from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

import app.celery_app  # noqa: F401 - establish production task import order
from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvMonitoringStrategy,
    PromotionTypeEnum,
)
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.gmvmax_sync_state import (
    GmvCreative10MinSyncState,
    GmvSyncSelectionCursor,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.gmvmax.services.sync_service import GmvMaxSyncService
from app.services import gmvmax_creative_media_cache
from app.tasks import ttb_gmvmax_tasks


def _next_id(db_session, model) -> int:
    return int(
        db_session.scalar(select(func.coalesce(func.max(model.id), 0))) or 0
    ) + 1


def _account_scope(db_session) -> tuple[int, int]:
    workspace = Workspace(
        id=_next_id(db_session, Workspace),
        name="GMV fair selection",
        company_code="GMVF",
    )
    provider = OAuthProviderApp(
        id=_next_id(db_session, OAuthProviderApp),
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.test/callback",
    )
    db_session.add_all([workspace, provider])
    db_session.flush()
    account = OAuthAccountTTB(
        id=_next_id(db_session, OAuthAccountTTB),
        workspace_id=int(workspace.id),
        provider_app_id=int(provider.id),
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.commit()
    return int(workspace.id), int(account.id)


def _strategy_row(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    limit: int,
) -> tuple[GmvMonitoringStrategy, MonitoringStrategy]:
    row = GmvMonitoringStrategy(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        store_id="store-fair",
        promotion_type=PromotionTypeEnum.PRODUCT,
        level="PRODUCT_HOURLY",
        category="GMVMAX",
        task_name="gmvmax.product_hourly",
        params_json={},
        enabled=True,
        interval_minutes=10,
        max_campaigns_per_run=limit,
    )
    db_session.add(row)
    db_session.commit()
    return row, MonitoringStrategy(
        id=int(row.id),
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        store_id="store-fair",
        level="PRODUCT_HOURLY",
        category="GMVMAX",
        task_name="gmvmax.product_hourly",
        interval_minutes=10,
        enabled=True,
        promotion_type=PromotionTypeEnum.PRODUCT.value,
        max_campaigns_per_run=limit,
        params_json={},
    )


def _campaign(
    *,
    workspace_id: int,
    auth_id: int,
    index: int,
) -> GmvCampaign:
    return GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        campaign_id=f"campaign-{index}",
        store_id="store-fair",
        promotion_type=PromotionTypeEnum.PRODUCT,
        status="ACTIVE",
        lifecycle_status="ACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 1) + timedelta(minutes=index),
    )


def _product_catalog(
    *,
    workspace_id: int,
    auth_id: int,
    index: int,
) -> GmvmaxProductCampaignCatalog:
    return GmvmaxProductCampaignCatalog(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        campaign_id=f"product-{index}",
        campaign_name=f"Product {index}",
        operation_status="ENABLE",
        store_id="store-fair",
        shopping_ads_type="PRODUCT",
    )


def test_campaign_cap_freezes_round_high_water_and_reaches_old_rows(
    db_session,
):
    workspace_id, auth_id = _account_scope(db_session)
    strategy_row, strategy = _strategy_row(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        limit=2,
    )
    campaigns = [
        _campaign(workspace_id=workspace_id, auth_id=auth_id, index=index)
        for index in range(1, 6)
    ]
    db_session.add_all(campaigns)
    db_session.commit()
    service = GmvMaxSyncService()

    first = service._select_active_campaigns(db_session, strategy)
    assert [row.campaign_id for row in first] == ["campaign-5", "campaign-4"]

    db_session.add_all(
        [
            _campaign(workspace_id=workspace_id, auth_id=auth_id, index=6),
            _campaign(workspace_id=workspace_id, auth_id=auth_id, index=7),
        ]
    )
    db_session.commit()

    second = service._select_active_campaigns(db_session, strategy)
    third = service._select_active_campaigns(db_session, strategy)

    assert [row.campaign_id for row in second] == ["campaign-3", "campaign-2"]
    assert [row.campaign_id for row in third] == ["campaign-1", "campaign-7"]
    assert {
        row.campaign_id
        for row in [*first, *second, *third]
    }.issuperset({f"campaign-{index}" for index in range(1, 6)})

    cursor_state = db_session.scalar(
        select(GmvSyncSelectionCursor).where(
            GmvSyncSelectionCursor.strategy_id == int(strategy_row.id)
        )
    )
    assert cursor_state is not None
    assert cursor_state.high_water_id > 0
    db_session.refresh(strategy_row)
    assert strategy_row.params_json == {}


def test_product_catalog_cap_uses_same_persistent_fair_round(
    db_session,
):
    workspace_id, auth_id = _account_scope(db_session)
    _, strategy = _strategy_row(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        limit=2,
    )
    db_session.add_all(
        [
            _product_catalog(
                workspace_id=workspace_id,
                auth_id=auth_id,
                index=index,
            )
            for index in range(1, 6)
        ]
    )
    db_session.commit()
    service = GmvMaxSyncService()

    first = service._select_product_catalog_campaigns(db_session, strategy)
    db_session.add(
        _product_catalog(
            workspace_id=workspace_id,
            auth_id=auth_id,
            index=6,
        )
    )
    db_session.commit()
    second = service._select_product_catalog_campaigns(db_session, strategy)
    third = service._select_product_catalog_campaigns(db_session, strategy)

    assert [row.campaign_id for row in first] == ["product-5", "product-4"]
    assert [row.campaign_id for row in second] == ["product-3", "product-2"]
    assert [row.campaign_id for row in third] == ["product-1", "product-6"]


def test_manual_sync_has_no_hidden_200_campaign_cap(monkeypatch):
    service = GmvMaxSyncService()
    observed_caps: list[int | None] = []

    def _sync(strategy, **kwargs):
        observed_caps.append(strategy.max_campaigns_per_run)
        return {"synced_rows": 0}

    monkeypatch.setattr(service, "_sync_product_metrics", _sync)
    result = service.sync_levels_for_account(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-fair",
        store_id="store-fair",
        levels=["PRODUCT"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
    )

    assert result["PRODUCT"]["synced_rows"] == 0
    assert observed_caps == [None, None]


def test_creative_10min_claim_rotates_even_without_metric_rows_and_backs_off(
    db_session,
):
    workspace_id, auth_id = _account_scope(db_session)
    catalogs = [
        _product_catalog(
            workspace_id=workspace_id,
            auth_id=auth_id,
            index=index,
        )
        for index in range(1, 4)
    ]
    db_session.add_all(catalogs)
    db_session.commit()

    first = ttb_gmvmax_tasks._iter_active_catalog_campaign_scopes(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        limit=1,
    )
    assert [row.campaign_id for row in first] == ["product-1"]
    first_state = db_session.scalar(
        select(GmvCreative10MinSyncState).where(
            GmvCreative10MinSyncState.campaign_id == "product-1"
        )
    )
    assert first_state is not None
    assert first_state.last_attempt_at is not None
    assert first_state.attempt_count == 1

    newcomer = _product_catalog(
        workspace_id=workspace_id,
        auth_id=auth_id,
        index=4,
    )
    newcomer.created_at = datetime(2030, 1, 1)
    db_session.add(newcomer)
    db_session.commit()

    ttb_gmvmax_tasks._record_creative_10min_result(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        campaign_id="product-1",
        store_id="store-fair",
        attempt_token=first[0].sync_attempt_token,
        success=False,
        error="upstream failed",
    )
    db_session.commit()
    db_session.refresh(first_state)
    assert first_state.last_status == "ERROR"
    assert first_state.consecutive_failures == 1
    assert first_state.next_attempt_at > first_state.last_error_at
    assert (
        first_state.next_attempt_at - first_state.last_error_at
        <= timedelta(minutes=120)
    )
    for _ in range(10):
        ttb_gmvmax_tasks._record_creative_10min_result(
            db_session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-fair",
            campaign_id="product-1",
            store_id="store-fair",
            attempt_token=first[0].sync_attempt_token,
            success=False,
            error="still failing",
        )
        db_session.commit()
    db_session.refresh(first_state)
    assert first_state.consecutive_failures == 11
    assert (
        first_state.next_attempt_at - first_state.last_error_at
        == timedelta(minutes=120)
    )

    second = ttb_gmvmax_tasks._iter_active_catalog_campaign_scopes(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        limit=1,
    )
    assert [row.campaign_id for row in second] == ["product-2"]
    ttb_gmvmax_tasks._record_creative_10min_result(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        campaign_id="product-2",
        store_id="store-fair",
        attempt_token=second[0].sync_attempt_token,
        success=True,
        rows=0,
    )
    db_session.commit()
    second_state = db_session.scalar(
        select(GmvCreative10MinSyncState).where(
            GmvCreative10MinSyncState.campaign_id == "product-2"
        )
    )
    assert second_state.last_status == "ZERO_ROWS"
    assert second_state.last_success_at is not None

    third = ttb_gmvmax_tasks._iter_active_catalog_campaign_scopes(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        limit=1,
    )
    assert [row.campaign_id for row in third] == ["product-3"]


def test_creative_10min_sweep_continues_after_one_dispatch_failure(
    db_session,
    monkeypatch,
):
    workspace_id, auth_id = _account_scope(db_session)
    db_session.add_all(
        [
            _product_catalog(
                workspace_id=workspace_id,
                auth_id=auth_id,
                index=index,
            )
            for index in range(1, 3)
        ]
    )
    db_session.commit()

    monkeypatch.setattr(ttb_gmvmax_tasks, "_db_session", lambda: db_session)
    monkeypatch.setattr(ttb_gmvmax_tasks, "_close_session", lambda session: None)
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_iter_sync_scopes",
        lambda session: [(workspace_id, auth_id, "adv-fair")],
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_campaign_sync_window",
        lambda *args, **kwargs: (date(2024, 1, 1), date(2024, 1, 2)),
    )
    published: list[str] = []

    def _send_task(name, *, kwargs, queue):
        published.append(str(kwargs["campaign_id"]))
        if len(published) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ttb_gmvmax_tasks.celery_app, "send_task", _send_task)

    result = ttb_gmvmax_tasks.task_gmvmax_sync_creative_metrics_10min.run()

    assert result["dispatch_failed"] == 1
    assert result["tasks"] == 1
    assert published == ["product-1", "product-2"]
    states = {
        row.campaign_id: row
        for row in db_session.scalars(
            select(GmvCreative10MinSyncState).order_by(
                GmvCreative10MinSyncState.campaign_id
            )
        ).all()
    }
    assert states["product-1"].last_status == "ERROR"
    assert states["product-1"].next_attempt_at > states["product-1"].last_error_at
    assert states["product-2"].last_status == "QUEUED"


class _ScalarResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _MediaClaimSession:
    def __init__(self):
        self.calls: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        return _ScalarResult()

    def commit(self):
        return None


def test_gmv_media_cache_claim_orders_by_oldest_due_not_status_or_newest():
    session = _MediaClaimSession()
    assert (
        gmvmax_creative_media_cache.claim_creative_media_cache_batch(
            session,
            limit=3,
        )
        == []
    )
    select_sql = " ".join(
        next(sql for sql in session.calls if "select id" in sql.lower()).split()
    ).lower()
    assert (
        "coalesce(media_cache_next_retry_at, updated_at, fetched_at, created_at) asc"
        in select_sql
    )
    assert "fetched_at desc" not in select_sql
    assert "case media_cache_status" not in select_sql


def test_creative_10min_sync_state_migration_is_sqlite_compatible(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0100_gmv_creative_10min_sync_state"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'sync-state.db'}")
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "gmv_creative_10min_sync_state" in inspector.get_table_names()
    assert "gmv_sync_selection_cursors" in inspector.get_table_names()
    unique_scopes = [
        list(item.get("column_names") or [])
        for item in inspector.get_unique_constraints(
            "gmv_creative_10min_sync_state"
        )
    ]
    assert [
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "campaign_id",
    ] in unique_scopes
    index_names = {
        str(item.get("name"))
        for item in inspector.get_indexes("gmv_creative_10min_sync_state")
    }
    assert "idx_gmv_creative_10min_sync_due" in index_names
    assert "idx_gmv_creative_10min_sync_attempt" in index_names
    cursor_unique = [
        list(item.get("column_names") or [])
        for item in inspector.get_unique_constraints(
            "gmv_sync_selection_cursors"
        )
    ]
    assert ["strategy_id", "cursor_key"] in cursor_unique

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.downgrade()
    assert (
        "gmv_creative_10min_sync_state"
        not in sa.inspect(engine).get_table_names()
    )
    assert "gmv_sync_selection_cursors" not in sa.inspect(engine).get_table_names()


def test_creative_10min_sync_state_migration_repairs_partial_indexes(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0100_gmv_creative_10min_sync_state"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'partial-sync-state.db'}")
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.execute(
            sa.text("drop index idx_gmv_creative_10min_sync_due")
        )
        connection.execute(
            sa.text("drop index idx_gmv_sync_selection_strategy")
        )
        migration.upgrade()

    inspector = sa.inspect(engine)
    attempt_indexes = {
        str(item.get("name"))
        for item in inspector.get_indexes("gmv_creative_10min_sync_state")
    }
    cursor_indexes = {
        str(item.get("name"))
        for item in inspector.get_indexes("gmv_sync_selection_cursors")
    }
    assert "idx_gmv_creative_10min_sync_due" in attempt_indexes
    assert "idx_gmv_sync_selection_strategy" in cursor_indexes
