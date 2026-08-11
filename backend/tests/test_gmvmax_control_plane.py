from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.data.models.ttb_entities import (
    TTBAdvertiser,
    TTBAdvertiserStoreLink,
    TTBBCAdvertiserLink,
    TTBBindingConfig,
)
from app.data.models.gmv_restructured import (
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    GmvProductMetricsDaily,
    GmvProductMetricsHourly,
)
from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
    GmvmaxProductCampaignMetricsHourly,
)
from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max import control as control_plane
from app.features.tenants.ttb.gmv_max._helpers import GMVMaxAccountBinding
from app.features.tenants.ttb.gmv_max.control import (
    acquire_guard_action_lease,
    assert_guard_action_lease_current,
    build_manual_upload_url,
    clear_manual_override,
    ensure_private_manual_upload_directory,
    get_sync_schedule,
    is_manual_pause_override_active,
    record_task_ownership,
    release_guard_action_lease,
    set_manual_pause_override,
    sign_manual_upload,
    task_is_owned,
    upsert_sync_schedule,
    verify_manual_upload_signature,
)
from app.tasks import ttb_gmvmax_tasks
from app.services import gmvmax_creative_metrics


def _context(db_session) -> router_provider.GMVMaxRouteContext:
    return router_provider.GMVMaxRouteContext(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=GMVMaxAccountBinding(
            account=SimpleNamespace(),
            bc_id="bc-1",
            advertiser_id="adv-1",
            store_id="store-1",
        ),
        client=SimpleNamespace(),
        db=db_session,
    )


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/gmvmax/metrics",
            "headers": [],
            "query_string": query.encode(),
        }
    )


def test_metrics_and_hermes_reads_reject_scope_overrides(db_session):
    context = _context(db_session)
    metric_day = date(2026, 7, 17)

    with pytest.raises(HTTPException) as metrics_error:
        asyncio.run(
            router_provider._query_gmvmax_metrics(
                _request("level=overview"),
                workspace_id=7,
                provider="tiktok-business",
                auth_id=11,
                campaign_id=None,
                store_id="store-1",
                level="overview",
                start_date=metric_day,
                end_date=metric_day,
                advertiser_id="other-advertiser",
                campaign_ids=None,
                item_group_ids=None,
                context=context,
            )
        )
    assert metrics_error.value.status_code == 403

    with pytest.raises(HTTPException) as hermes_error:
        asyncio.run(
            router_provider.list_hermes_daily_reports_provider(
                workspace_id=7,
                provider="tiktok-business",
                auth_id=11,
                store_id="other-store",
                advertiser_id="adv-1",
                limit=14,
                context=context,
            )
        )
    assert hermes_error.value.status_code == 403


def test_task_ownership_is_exactly_scoped(db_session):
    record_task_ownership(
        db_session,
        task_id="task-1",
        workspace_id=7,
        auth_id=11,
        provider="tiktok_business",
        task_name="gmvmax.manual_sync_levels",
    )
    db_session.commit()

    assert task_is_owned(
        db_session,
        task_id="task-1",
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )
    assert not task_is_owned(
        db_session,
        task_id="task-1",
        workspace_id=8,
        auth_id=11,
        provider="tiktok-business",
    )
    assert not task_is_owned(
        db_session,
        task_id="task-1",
        workspace_id=7,
        auth_id=12,
        provider="tiktok-business",
    )


def test_sync_interval_and_manual_override_are_persistent(db_session):
    schedule = upsert_sync_schedule(
        db_session,
        workspace_id=7,
        auth_id=11,
        provider="tiktok_business",
        advertiser_id="adv-1",
        store_id="store-1",
        interval_minutes=20,
        actor_user_id=3,
    )
    db_session.commit()
    db_session.expire_all()

    loaded = get_sync_schedule(
        db_session,
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )
    assert loaded is not None
    assert loaded.id == schedule.id
    assert loaded.interval_minutes == 20
    assert loaded.advertiser_id == "adv-1"

    set_manual_pause_override(
        db_session,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="operator@example.com",
    )
    db_session.commit()
    assert is_manual_pause_override_active(
        db_session,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
    )
    clear_manual_override(
        db_session,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
    )
    db_session.commit()
    assert not is_manual_pause_override_active(
        db_session,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
    )


def test_binding_switch_moves_minute_schedule_without_changing_interval(db_session):
    context = _context(db_session)
    db_session.add(
        TTBBindingConfig(
            workspace_id=7,
            auth_id=11,
            bc_id="bc-1",
            advertiser_id="adv-1",
            store_id="store-1",
            auto_sync_products=False,
        )
    )
    upsert_sync_schedule(
        db_session,
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
        advertiser_id="adv-1",
        store_id="store-1",
        interval_minutes=20,
        actor_user_id=3,
    )
    db_session.commit()

    changed = router_provider._persist_auto_binding(
        context,
        candidate=router_provider.AutoBindingCandidate(
            advertiser_id="adv-2",
            store_id="store-1",
            store_authorized_bc_id="bc-1",
            authorization_status="EFFECTIVE",
        ),
        actor_user_id=3,
    )

    binding = db_session.query(TTBBindingConfig).one()
    schedule = get_sync_schedule(
        db_session,
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )
    assert changed is True
    assert binding.advertiser_id == "adv-2"
    assert binding.store_id == "store-1"
    assert schedule is not None
    assert schedule.advertiser_id == "adv-2"
    assert schedule.store_id == "store-1"
    assert schedule.interval_minutes == 20
    assert schedule.enabled is True


def test_binding_status_rejects_requested_advertiser_outside_saved_scope(db_session):
    db_session.add(
        TTBBindingConfig(
            workspace_id=7,
            auth_id=11,
            bc_id="bc-1",
            advertiser_id="adv-1",
            store_id="store-1",
            auto_sync_products=False,
        )
    )
    db_session.commit()

    result = router_provider._build_binding_status(
        db_session,
        workspace_id=7,
        auth_id=11,
        store_id="store-1",
        advertiser_id="adv-2",
        bc_id="bc-1",
    )

    assert result.binding_ready is False
    assert result.error_code == "binding_scope_mismatch"


def test_auto_binding_continues_after_cached_advertiser_loses_authorization(db_session):
    class _Client:
        async def gmv_max_exclusive_authorization_get(self, request):
            status = "EFFECTIVE" if request.advertiser_id == "adv-2" else "UNAUTHORIZED"
            return SimpleNamespace(
                request_id=f"auth-{request.advertiser_id}",
                data=SimpleNamespace(authorization_status=status, is_authorized=status == "EFFECTIVE"),
            )

        async def gmv_max_store_shop_ad_usage_check(self, request):
            return SimpleNamespace(
                request_id=f"usage-{request.advertiser_id}",
                data=SimpleNamespace(
                    promote_all_products_allowed=True,
                    is_running_custom_shop_ads=False,
                ),
            )

        async def gmv_max_store_list(self, request):
            store = SimpleNamespace(
                store_id="store-1",
                store_name="Store",
                store_authorized_bc_id="bc-1",
                advertiser_id=request.advertiser_id,
                is_gmv_max_available=True,
            )
            return SimpleNamespace(
                request_id=f"stores-{request.advertiser_id}",
                data=SimpleNamespace(store_list=[store]),
            )

    db_session.add_all(
        [
            TTBBindingConfig(
                workspace_id=7,
                auth_id=11,
                bc_id="bc-1",
                advertiser_id="adv-1",
                store_id="store-1",
                auto_sync_products=False,
            ),
            TTBAdvertiser(workspace_id=7, auth_id=11, advertiser_id="adv-1", bc_id="bc-1"),
            TTBAdvertiser(workspace_id=7, auth_id=11, advertiser_id="adv-2", bc_id="bc-1"),
            TTBBCAdvertiserLink(
                workspace_id=7,
                auth_id=11,
                bc_id="bc-1",
                advertiser_id="adv-1",
                relation_type="AUTHORIZED",
            ),
            TTBBCAdvertiserLink(
                workspace_id=7,
                auth_id=11,
                bc_id="bc-1",
                advertiser_id="adv-2",
                relation_type="AUTHORIZED",
            ),
            TTBAdvertiserStoreLink(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                relation_type="AUTHORIZER",
                store_authorized_bc_id="bc-1",
            ),
            TTBAdvertiserStoreLink(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-2",
                store_id="store-1",
                relation_type="AUTHORIZER",
                store_authorized_bc_id="bc-1",
            ),
        ]
    )
    db_session.commit()
    context = _context(db_session)
    context.client = _Client()

    result = asyncio.run(
        router_provider.auto_bind_gmvmax_account(
            7,
            "tiktok-business",
            11,
            router_provider.AutoBindingRequest(store_id="store-1", persist=True),
            me=SimpleNamespace(id=3),
            context=context,
        )
    )

    assert result.persisted is True
    assert result.binding_changed is True
    assert result.selected is not None
    assert result.selected.advertiser_id == "adv-2"
    assert {candidate.authorization_status for candidate in result.candidates} == {
        "EFFECTIVE",
        "UNAUTHORIZED",
    }
    binding = db_session.query(TTBBindingConfig).one()
    assert binding.advertiser_id == "adv-2"


def test_minute_beat_dispatches_due_persisted_account_schedule(
    db_session, monkeypatch
):
    db_session.add(
        TTBBindingConfig(
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-current",
            store_id="store-current",
        )
    )
    schedule = upsert_sync_schedule(
        db_session,
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
        advertiser_id="adv-stale",
        store_id="store-stale",
        interval_minutes=10,
        actor_user_id=None,
    )
    schedule.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=1
    )
    db_session.commit()
    sent: list[dict] = []

    monkeypatch.setattr(ttb_gmvmax_tasks, "_db_session", lambda: db_session)
    monkeypatch.setattr(ttb_gmvmax_tasks, "_close_session", lambda *_: None)
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_advertiser_report_day",
        lambda *_, **__: date(2026, 7, 17),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks.celery_app,
        "send_task",
        lambda name, **kwargs: sent.append({"name": name, **kwargs}),
    )

    result = ttb_gmvmax_tasks.dispatch_account_syncs_task.run()

    assert result["enqueued"] == 1
    assert sent[0]["name"] == "gmvmax.manual_sync_levels"
    assert sent[0]["kwargs"]["advertiser_id"] == "adv-current"
    assert sent[0]["kwargs"]["store_id"] == "store-current"
    assert sent[0]["kwargs"]["levels"] == [
        "OVERVIEW",
        "CAMPAIGN",
        "PRODUCT",
        "CREATIVE",
        "LIVESTREAM",
        "DURATION",
    ]
    assert sent[0]["kwargs"]["require_active_campaigns"] is True
    assert sent[0]["kwargs"]["refresh_creative_assets"] is False
    assert sent[0]["kwargs"]["refresh_catalog_details"] is False
    assert sent[0]["kwargs"]["start_date"] == "2026-07-17"
    assert sent[0]["kwargs"]["end_date"] == "2026-07-17"
    db_session.refresh(schedule)
    assert schedule.advertiser_id == "adv-current"
    assert schedule.store_id == "store-current"
    assert schedule.last_task_id == sent[0]["task_id"]
    assert schedule.next_run_at > datetime.now(timezone.utc).replace(tzinfo=None)
    assert task_is_owned(
        db_session,
        task_id=schedule.last_task_id,
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )


def test_dispatch_disables_schedule_without_current_binding_and_cleans_expired_owner(
    db_session, monkeypatch
):
    schedule = upsert_sync_schedule(
        db_session,
        workspace_id=8,
        auth_id=12,
        provider="tiktok-business",
        advertiser_id="adv-stale",
        store_id="store-stale",
        interval_minutes=10,
        actor_user_id=None,
    )
    schedule.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=1
    )
    expired = record_task_ownership(
        db_session,
        task_id="expired-owner",
        workspace_id=8,
        auth_id=12,
        provider="tiktok-business",
        task_name="gmvmax.manual_sync_levels",
    )
    expired.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=1
    )
    db_session.commit()

    monkeypatch.setattr(ttb_gmvmax_tasks, "_db_session", lambda: db_session)
    monkeypatch.setattr(ttb_gmvmax_tasks, "_close_session", lambda *_: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        ttb_gmvmax_tasks.celery_app,
        "send_task",
        lambda name, **kwargs: sent.append({"name": name, **kwargs}),
    )

    result = ttb_gmvmax_tasks.dispatch_account_syncs_task.run()

    db_session.refresh(schedule)
    assert result["disabled"] == 1
    assert result["expired_ownerships_deleted"] == 1
    assert schedule.enabled is False
    assert "ttb_binding_configs" in str(schedule.last_error)
    assert sent == []
    assert not task_is_owned(
        db_session,
        task_id="expired-owner",
        workspace_id=8,
        auth_id=12,
        provider="tiktok-business",
    )


def test_manual_sync_refreshes_catalog_before_metrics(monkeypatch):
    events: list[str] = []
    sync_kwargs: list[dict] = []

    class _Lock:
        def acquire(self, **_kwargs):
            events.append("lock_acquire")
            return True

        def release(self):
            events.append("lock_release")
            return True

    class _Db:
        def commit(self):
            events.append("db_commit")

        def rollback(self):
            events.append("db_rollback")

    class _SyncService:
        def __init__(self, **_kwargs):
            pass

        def sync_levels_for_account(self, **_kwargs):
            events.append("metrics")
            sync_kwargs.append(dict(_kwargs))
            return {"CAMPAIGN": {"rows": 1}}

    class _Fence:
        def assert_current(self, _db):
            events.append("fence_assert")

    async def _catalog_sync(*_args, **_kwargs):
        events.append("catalog")
        return {"campaigns": 1}

    monkeypatch.setattr(ttb_gmvmax_tasks, "_db_session", _Db)
    monkeypatch.setattr(ttb_gmvmax_tasks, "_close_session", lambda *_: None)
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "build_account_sync_lock",
        lambda **_kwargs: _Lock(),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "acquire_account_sync_fence",
        lambda *_args, **_kwargs: (
            events.append("fence_acquire") or _Fence()
        ),
    )
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "release_account_sync_fence",
        lambda *_args, **_kwargs: (
            events.append("fence_release") or True
        ),
    )
    monkeypatch.setattr(ttb_gmvmax_tasks, "sync_gmvmax_campaigns", _catalog_sync)
    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "_run_with_client",
        lambda db, auth_id, fn: asyncio.run(fn(SimpleNamespace())),
    )
    monkeypatch.setattr(ttb_gmvmax_tasks, "GmvMaxSyncService", _SyncService)

    result = ttb_gmvmax_tasks.manual_sync_levels_task.run(
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        levels=["CAMPAIGN"],
        start_date="2026-07-16",
        end_date="2026-07-17",
    )

    assert events == [
        "lock_acquire",
        "fence_acquire",
        "db_commit",
        "catalog",
        "fence_assert",
        "db_commit",
        "metrics",
        "db_rollback",
        "fence_release",
        "db_commit",
        "lock_release",
    ]
    assert result["catalog"] == {"campaigns": 1}
    assert result["require_active_campaigns"] is False
    assert sync_kwargs[0]["require_active_campaigns"] is False
    assert sync_kwargs[0]["refresh_creative_assets"] is False


def test_manual_sync_requeues_immediately_when_account_lock_is_busy(monkeypatch):
    acquire_kwargs: list[dict] = []

    class _BusyLock:
        def acquire(self, **kwargs):
            acquire_kwargs.append(dict(kwargs))
            return False

    class _RetryRaised(RuntimeError):
        pass

    retry_kwargs: list[dict] = []

    def _retry(**kwargs):
        retry_kwargs.append(dict(kwargs))
        raise _RetryRaised

    monkeypatch.setattr(
        ttb_gmvmax_tasks,
        "build_account_sync_lock",
        lambda **_kwargs: _BusyLock(),
    )
    monkeypatch.setattr(ttb_gmvmax_tasks.manual_sync_levels_task, "retry", _retry)

    with pytest.raises(_RetryRaised):
        ttb_gmvmax_tasks.manual_sync_levels_task.run(
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            levels=["CAMPAIGN"],
            start_date="2026-07-17",
            end_date="2026-07-17",
        )

    assert acquire_kwargs == [{"timeout": 0.0, "retry_interval": 0.2}]
    assert retry_kwargs[0]["countdown"] == 5
    assert "already running" in str(retry_kwargs[0]["exc"])


def test_sync_task_progress_reports_account_wait_without_raw_details():
    progress = router_provider._sync_task_progress(
        "RETRY",
        RuntimeError("GMV Max account sync already running"),
    )

    assert progress == {
        "phase": "WAITING_ACCOUNT_SYNC",
        "message": "同账户定时同步正在收尾，当前系列将在锁释放后自动开始…",
    }


def test_recent_expired_media_source_does_not_force_full_video_rescan(monkeypatch):
    class _Result:
        def mappings(self):
            return self

        def one(self):
            return {
                "matched": 1,
                "expired_sources": 1,
                "matched_age_seconds": 30,
                "scope_age_seconds": 30,
            }

    class _Session:
        def execute(self, *_args, **_kwargs):
            return _Result()

    monkeypatch.setattr(gmvmax_creative_metrics, "CREATIVE_ASSET_REFRESH_SECONDS", 600)

    refresh, reason = gmvmax_creative_metrics._creative_asset_refresh_needed(
        _Session(),
        workspace_id=3,
        auth_id=3,
        advertiser_id="adv-1",
        store_id="store-1",
        creative_refs=[{"creative_id": "video-1"}],
    )

    assert refresh is False
    assert reason == "expired_media_source_recently_probed"


def test_private_upload_directory_chain_is_0750(tmp_path, monkeypatch):
    upload_root = tmp_path / "manual_uploads"
    monkeypatch.setattr(control_plane, "MANUAL_UPLOAD_ROOT", upload_root)

    leaf = ensure_private_manual_upload_directory(7, 11, "store-1")

    assert leaf == upload_root / "7" / "11" / "store-1"
    for directory in (
        upload_root,
        upload_root / "7",
        upload_root / "7" / "11",
        leaf,
    ):
        assert directory.stat().st_mode & 0o777 == 0o750


def test_mutating_scope_routes_use_bound_scope_validator():
    for route in (
        router_provider.create_gmvmax_campaign_provider,
        router_provider.sync_advertiser_balance,
        router_provider.apply_gmvmax_campaign_action_provider,
        router_provider.update_gmvmax_strategy_provider,
        router_provider.preview_gmvmax_strategy_provider,
    ):
        assert "_validate_bound_scope" in route.__code__.co_names

    with pytest.raises(Exception) as exc_info:
        router_provider._validate_bound_scope(
            _context(SimpleNamespace()),
            advertiser_id="adv-outside",
            store_id="store-outside",
        )
    assert getattr(exc_info.value, "status_code", None) == 403


def test_manual_campaign_mutations_share_guard_lease():
    for route in (
        router_provider.apply_gmvmax_campaign_action_provider,
        router_provider.update_gmvmax_strategy_provider,
    ):
        assert "_manual_guard_mutation_lease" in route.__code__.co_names


def test_signed_upload_url_rejects_tampering_and_expiry():
    now = int(datetime.now(timezone.utc).timestamp())
    expires = now + 300
    signature = sign_manual_upload(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        upload_id="upload-1",
        expires=expires,
    )
    assert verify_manual_upload_signature(
        workspace_id=7,
        provider="tiktok_business",
        auth_id=11,
        upload_id="upload-1",
        expires=expires,
        signature=signature,
        now_epoch=now,
    )
    assert not verify_manual_upload_signature(
        workspace_id=8,
        provider="tiktok-business",
        auth_id=11,
        upload_id="upload-1",
        expires=expires,
        signature=signature,
        now_epoch=now,
    )
    assert not verify_manual_upload_signature(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        upload_id="upload-1",
        expires=now - 1,
        signature=signature,
        now_epoch=now,
    )
    url = build_manual_upload_url(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        upload_id="upload-1",
    )
    assert "/static/" not in url
    assert "expires=" in url and "signature=" in url


def test_guard_action_lease_is_durable_and_fenced(db_session):
    first = acquire_guard_action_lease(
        db_session,
        lease_name="guard",
        owner_token="worker-a",
        ttl_seconds=300,
    )
    db_session.commit()
    assert first == 1
    db_session.info["gmvmax_guard_owner_token"] = "worker-a"
    db_session.info["gmvmax_guard_fencing_token"] = first
    db_session.info["gmvmax_guard_redis_lock"] = SimpleNamespace(
        acquired=True,
        lost=False,
    )
    assert (
        assert_guard_action_lease_current(db_session, lease_name="guard")
        == first
    )
    db_session.commit()
    assert (
        acquire_guard_action_lease(
            db_session,
            lease_name="guard",
            owner_token="worker-b",
            ttl_seconds=300,
        )
        is None
    )
    db_session.rollback()
    assert release_guard_action_lease(
        db_session,
        lease_name="guard",
        owner_token="worker-a",
    )
    db_session.commit()
    second = acquire_guard_action_lease(
        db_session,
        lease_name="guard",
        owner_token="worker-b",
        ttl_seconds=300,
    )
    assert second == 2


def test_product_metrics_use_item_group_true_net_cost_and_real_pagination(
    db_session, monkeypatch
):
    metric_day = date(2026, 7, 17)
    db_session.add_all(
        [
            GmvProductMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                item_group_id="item-a",
                stat_time_day=metric_day,
                cost_cents=1000,
                net_cost_cents=0,
                gross_revenue_cents=5000,
                orders=1,
                impressions=100,
                clicks=10,
                source_observed_at=datetime(2026, 7, 17, 12, 0, 0),
            ),
            GmvProductMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                item_group_id="item-b",
                stat_time_day=metric_day,
                cost_cents=2000,
                net_cost_cents=None,
                gross_revenue_cents=4000,
                orders=2,
                impressions=200,
                clicks=20,
                source_observed_at=datetime(2026, 7, 17, 12, 0, 0),
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(router_provider, "_resolve_advertiser_today", lambda *_, **__: metric_day)
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "UTC",
    )

    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=product&page=1&page_size=1"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id="campaign-1",
            store_id="store-1",
            level="product",
            start_date=metric_day,
            end_date=metric_day,
            advertiser_id="adv-1",
            campaign_ids=["campaign-1"],
            item_group_ids=None,
            context=_context(db_session),
        )
    )

    report = result["report"]
    assert report["page_info"]["total_number"] == 2
    assert report["page_info"]["page_size"] == 1
    assert report["page_info"]["has_next"] is True
    assert len(report["list"]) == 1
    first = report["list"][0]
    assert first["dimensions"]["item_group_id"] == "item-a"
    assert first["dimensions"]["product_id"] == "item-a"
    assert first["metrics"]["cost"] == 10.0
    assert first["metrics"]["net_cost"] == 0.0
    assert report["summary"]["net_cost"] == 20.0
    assert report["summary"]["roi"] == 3.0


def test_creative_metrics_preserve_legacy_official_daily_history(
    db_session, monkeypatch
):
    """Observation metadata must not erase otherwise valid historical facts."""

    today = date(2026, 7, 17)
    historical_day = date(2026, 7, 15)
    db_session.add(
        GmvmaxProductCreativeMetricsDaily(
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            item_group_id="item-a",
            creative_id="creative-legacy",
            stat_time_day=historical_day,
            creative_delivery_status="DELIVERING",
            cost_cents=1234,
            net_cost_cents=1200,
            gross_revenue_cents=5678,
            orders=3,
            impressions=400,
            clicks=40,
            source_observed_at=None,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        router_provider, "_resolve_advertiser_today", lambda *_, **__: today
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "UTC",
    )

    context = _context(db_session)
    context.binding.bc_id = None
    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=creative&page=1&page_size=50"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id="campaign-1",
            store_id="store-1",
            level="creative",
            start_date=historical_day,
            end_date=historical_day,
            advertiser_id="adv-1",
            campaign_ids=["campaign-1"],
            item_group_ids=["item-a"],
            context=context,
        )
    )

    report = result["report"]
    assert report["page_info"]["total_number"] == 1
    assert report["list"][0]["dimensions"]["creative_id"] == "creative-legacy"
    assert report["list"][0]["metrics"]["cost"] == 12.34
    assert report["summary"]["cost"] == 12.34
    assert report["summary"]["gmv"] == 56.78


def test_overview_falls_back_to_daily_facts_without_fabricating_net_cost(
    db_session, monkeypatch
):
    metric_day = date(2026, 7, 17)
    db_session.add(
        GmvmaxProductCampaignMetricsDaily(
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-1",
            stat_time_day=metric_day,
            cost_cents=1000,
            net_cost_cents=0,
            gross_revenue_cents=5000,
            orders=2,
            source_observed_at=datetime(2026, 7, 17, 12, 0, 0),
        )
    )
    db_session.commit()
    monkeypatch.setattr(router_provider, "_resolve_advertiser_today", lambda *_, **__: metric_day)
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "UTC",
    )
    monkeypatch.setattr(router_provider, "_source_age_seconds", lambda *_, **__: 0)

    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=overview&page=1&page_size=50"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id=None,
            store_id="store-1",
            level="overview",
            start_date=metric_day,
            end_date=metric_day,
            advertiser_id="adv-1",
            campaign_ids=None,
            item_group_ids=None,
            context=_context(db_session),
        )
    )

    summary = result["report"]["summary"]
    assert summary["cost"] == 10.0
    assert summary["net_cost"] == 0.0
    assert summary["gmv"] == 50.0
    assert summary["roi"] == 5.0
    assert result["freshness"].source == "gmvmax_campaign_metrics_fallback"


def test_campaign_metrics_choose_official_daily_for_history_and_hourly_for_today(
    db_session, monkeypatch
):
    today = date(2026, 7, 17)
    yesterday = date(2026, 7, 16)
    observed_at = datetime(2026, 7, 17, 12, 0, 0)
    db_session.add_all(
        [
            GmvmaxProductCampaignMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                stat_time_day=yesterday,
                cost_cents=11140,
                net_cost_cents=11140,
                gross_revenue_cents=1243,
                orders=1,
                source_observed_at=observed_at,
            ),
            GmvmaxProductCampaignMetricsHourly(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                stat_time_hour=datetime(2026, 7, 16, 12),
                cost_cents=11141,
                net_cost_cents=11141,
                gross_revenue_cents=1243,
                orders=1,
            ),
            GmvmaxProductCampaignMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                stat_time_day=today,
                cost_cents=5000,
                net_cost_cents=5000,
                gross_revenue_cents=0,
                orders=0,
                source_observed_at=observed_at,
            ),
            GmvmaxProductCampaignMetricsHourly(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                stat_time_hour=datetime(2026, 7, 17, 8),
                cost_cents=1256,
                net_cost_cents=1256,
                gross_revenue_cents=0,
                orders=0,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        router_provider, "_resolve_advertiser_today", lambda *_, **__: today
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "America/New_York",
    )

    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=campaign&page=1&page_size=50"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id="campaign-1",
            store_id="store-1",
            level="campaign",
            start_date=yesterday,
            end_date=today,
            advertiser_id="adv-1",
            campaign_ids=["campaign-1"],
            item_group_ids=None,
            context=_context(db_session),
        )
    )

    rows = result["report"]["list"]
    assert [row["metrics"]["cost"] for row in rows] == [111.4, 12.56]
    assert result["report"]["summary"]["cost"] == 123.96


def test_product_metrics_exclude_unproven_daily_and_fall_back_to_hourly(
    db_session, monkeypatch
):
    today = date(2026, 7, 17)
    historical_day = date(2026, 7, 15)
    db_session.add_all(
        [
            GmvProductMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                item_group_id="item-a",
                stat_time_day=historical_day,
                cost_cents=14,
                net_cost_cents=14,
                gross_revenue_cents=0,
                orders=0,
                source_observed_at=None,
            ),
            GmvProductMetricsHourly(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                campaign_id="campaign-1",
                item_group_id="item-a",
                stat_time_hour=datetime(2026, 7, 15, 10),
                cost_cents=11140,
                net_cost_cents=11140,
                gross_revenue_cents=1243,
                orders=1,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        router_provider, "_resolve_advertiser_today", lambda *_, **__: today
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "America/New_York",
    )

    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=product&page=1&page_size=50"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id="campaign-1",
            store_id="store-1",
            level="product",
            start_date=historical_day,
            end_date=historical_day,
            advertiser_id="adv-1",
            campaign_ids=["campaign-1"],
            item_group_ids=["item-a"],
            context=_context(db_session),
        )
    )

    assert result["report"]["summary"]["cost"] == 111.4


def test_overview_uses_normalized_daily_history_and_hourly_today(
    db_session, monkeypatch
):
    today = date(2026, 7, 17)
    yesterday = date(2026, 7, 16)
    observed_at = datetime(2026, 7, 17, 12, 0, 0)
    db_session.add_all(
        [
            GmvOverviewMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                stat_time_day=yesterday,
                cost_cents=11140,
                net_cost_cents=11140,
                gross_revenue_cents=1243,
                orders=1,
                source_observed_at=observed_at,
                ingested_at=observed_at,
            ),
            GmvOverviewMetricsHourly(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                stat_time_hour=datetime(2026, 7, 16, 12),
                cost_cents=11141,
                net_cost_cents=11141,
                gross_revenue_cents=1243,
                orders=1,
                ingested_at=observed_at,
            ),
            GmvOverviewMetricsDaily(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                stat_time_day=today,
                cost_cents=5000,
                net_cost_cents=5000,
                gross_revenue_cents=0,
                orders=0,
                source_observed_at=observed_at,
                ingested_at=observed_at,
            ),
            GmvOverviewMetricsHourly(
                workspace_id=7,
                auth_id=11,
                advertiser_id="adv-1",
                store_id="store-1",
                stat_time_hour=datetime(2026, 7, 17, 8),
                cost_cents=1256,
                net_cost_cents=1256,
                gross_revenue_cents=0,
                orders=0,
                ingested_at=observed_at,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        router_provider, "_resolve_advertiser_today", lambda *_, **__: today
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_, **__: "America/New_York",
    )

    result = asyncio.run(
        router_provider._query_gmvmax_metrics(
            _request("level=overview&page=1&page_size=50"),
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            campaign_id=None,
            store_id="store-1",
            level="overview",
            start_date=yesterday,
            end_date=today,
            advertiser_id="adv-1",
            campaign_ids=None,
            item_group_ids=None,
            context=_context(db_session),
        )
    )

    assert result["report"]["summary"]["cost"] == 123.96
    assert "gmv_overview_metrics_daily" in result["freshness"].source
    assert "gmv_overview_metrics_hourly" in result["freshness"].source
