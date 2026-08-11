from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.celery_app import celery_app
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.gmv_restructured import GmvStrategyConfig
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxCampaignPauseIntent,
    claim_campaign_pause_intent,
    complete_campaign_pause_intent,
    create_or_get_campaign_pause_intent,
    defer_campaign_pause_intent,
    due_campaign_pause_intent_ids,
    is_manual_pause_override_active,
    utcnow_naive,
)
from app.tasks import ttb_gmvmax_tasks


def _context(db_session) -> router_provider.GMVMaxRouteContext:
    return router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=SimpleNamespace(),
        client=SimpleNamespace(),
        db=db_session,
    )


def test_pause_intent_is_coalesced_claimed_deferred_and_recoverable(db_session):
    first, created = create_or_get_campaign_pause_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="owner",
    )
    second, created_again = create_or_get_campaign_pause_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="owner",
    )
    assert created is True
    assert created_again is False
    assert first.id == second.id
    db_session.commit()

    claimed = claim_campaign_pause_intent(
        db_session,
        intent_id=first.id,
        owner_token="worker-a",
    )
    assert claimed is not None
    assert claimed.status == "RUNNING"
    assert claimed.attempt_count == 1
    assert defer_campaign_pause_intent(
        db_session,
        intent_id=first.id,
        owner_token="worker-a",
        countdown_seconds=1,
        error="sync in progress",
    )
    db_session.commit()

    row = db_session.get(GMVMaxCampaignPauseIntent, first.id)
    row.next_attempt_at = utcnow_naive() - timedelta(seconds=1)
    db_session.commit()
    assert due_campaign_pause_intent_ids(db_session) == [first.id]
    claimed = claim_campaign_pause_intent(
        db_session,
        intent_id=first.id,
        owner_token="worker-b",
    )
    assert claimed is not None
    assert complete_campaign_pause_intent(
        db_session,
        intent_id=first.id,
        owner_token="worker-b",
        status="SUCCEEDED",
    )
    db_session.commit()
    db_session.refresh(row)
    assert row.status == "SUCCEEDED"
    assert row.active_key is None


def test_pause_conflict_persists_intent_and_returns_queued(monkeypatch, db_session):
    campaign = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        shopping_ads_type="PRODUCT",
    )
    strategy = GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id="campaign-1",
        enabled=True,
        config_json={"smart_guard": {"enabled": True}},
    )
    db_session.add_all([campaign, strategy])
    db_session.commit()
    sent: list[dict] = []
    lease_options: list[dict] = []

    @contextmanager
    def busy_lease(_context, **_kwargs):
        lease_options.append(_kwargs)
        raise HTTPException(
            status_code=409,
            detail={"code": "GMVMAX_MUTATION_INFLIGHT", "message": "syncing"},
        )
        yield  # pragma: no cover

    monkeypatch.setattr(router_provider, "_manual_guard_mutation_lease", busy_lease)
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, **kwargs: sent.append({"name": name, **kwargs}),
    )

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            campaign_id="campaign-1",
            payload={"type": "pause", "reason": "operator pause"},
            advertiser_id=None,
            me=SimpleNamespace(id=8, email="owner@example.test", username="owner", display_name="Owner"),
            context=_context(db_session),
        )
    )

    assert response.status == "queued"
    assert response.response["strategy_disabled"] is False
    assert lease_options == [{"priority_pause": True}]
    intent = db_session.query(GMVMaxCampaignPauseIntent).one()
    assert intent.status == "PENDING"
    assert intent.active_key
    assert is_manual_pause_override_active(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
    )
    db_session.refresh(strategy)
    assert strategy.enabled is True
    assert strategy.config_json["smart_guard"]["enabled"] is True
    assert sent == [
        {
            "name": "gmvmax.execute_campaign_pause_intent",
            "kwargs": {"intent_id": intent.id},
            "queue": "gmvmax_control",
        }
    ]


def test_smart_shutdown_conflict_disables_strategy_before_returning_queued(
    monkeypatch,
    db_session,
):
    campaign = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        shopping_ads_type="PRODUCT",
    )
    strategy = GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id="campaign-1",
        enabled=True,
        config_json={"smart_guard": {"enabled": True}},
    )
    db_session.add_all([campaign, strategy])
    db_session.commit()
    sent: list[dict] = []

    @contextmanager
    def busy_lease(*_args, **_kwargs):
        raise HTTPException(
            status_code=409,
            detail={"code": "GMVMAX_MUTATION_INFLIGHT", "message": "syncing"},
        )
        yield  # pragma: no cover

    monkeypatch.setattr(router_provider, "_manual_guard_mutation_lease", busy_lease)
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, **kwargs: sent.append({"name": name, **kwargs}),
    )

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            campaign_id="campaign-1",
            payload={"type": "pause", "disable_strategy": True},
            advertiser_id=None,
            me=SimpleNamespace(id=8, email="owner@example.test", username="owner", display_name="Owner"),
            context=_context(db_session),
        )
    )

    assert response.status == "queued"
    assert response.response["strategy_disabled"] is True
    db_session.refresh(strategy)
    assert strategy.enabled is False
    assert strategy.config_json["smart_guard"]["enabled"] is False
    assert len(sent) == 1


def test_system_pause_retry_does_not_recursively_publish_the_same_intent(
    monkeypatch,
    db_session,
):
    campaign = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        shopping_ads_type="PRODUCT",
    )
    db_session.add(campaign)
    intent, _ = create_or_get_campaign_pause_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="owner",
    )
    db_session.commit()
    sent: list[dict] = []

    @contextmanager
    def busy_lease(*_args, **_kwargs):
        raise HTTPException(
            status_code=409,
            detail={"code": "GMVMAX_MUTATION_INFLIGHT", "message": "syncing"},
        )
        yield  # pragma: no cover

    monkeypatch.setattr(router_provider, "_manual_guard_mutation_lease", busy_lease)
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, **kwargs: sent.append({"name": name, **kwargs}),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider.apply_gmvmax_campaign_action_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=2,
                campaign_id="campaign-1",
                payload={"type": "pause", "_system_pause_intent_id": intent.id},
                advertiser_id=None,
                me=SimpleNamespace(id=None, email="worker", username="worker", display_name="worker"),
                context=_context(db_session),
            )
        )

    assert exc_info.value.detail["code"] == "GMVMAX_MUTATION_INFLIGHT"
    assert sent == []


def test_pause_intent_worker_executes_owned_intent_once(monkeypatch, db_session):
    intent, _ = create_or_get_campaign_pause_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="owner",
        reason="pause now",
    )
    db_session.commit()
    calls: list[dict] = []

    class _Client:
        async def aclose(self):
            return None

    context = SimpleNamespace(
        advertiser_id="adv-1",
        store_id="store-1",
        client=_Client(),
    )

    def fake_build(*_args, **_kwargs):
        return context

    async def fake_apply(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="success")

    monkeypatch.setattr(router_provider, "_build_route_context", fake_build)
    monkeypatch.setattr(
        router_provider,
        "apply_gmvmax_campaign_action_provider",
        fake_apply,
    )

    result = ttb_gmvmax_tasks.execute_campaign_pause_intent_task.run(
        intent_id=intent.id
    )

    assert result == {"status": "succeeded", "intent_id": intent.id}
    assert calls[0]["payload"] == {
        "type": "pause",
        "reason": "pause now",
        "_system_pause_intent_id": intent.id,
    }
    db_session.expire_all()
    persisted = db_session.get(GMVMaxCampaignPauseIntent, intent.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.active_key is None


def test_pause_intent_worker_never_marks_a_requeued_pause_as_succeeded(monkeypatch, db_session):
    intent, _ = create_or_get_campaign_pause_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        actor="owner",
    )
    intent.attempt_count = 24
    db_session.commit()

    class _Client:
        async def aclose(self):
            return None

    monkeypatch.setattr(
        router_provider,
        "_build_route_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            advertiser_id="adv-1", store_id="store-1", client=_Client()
        ),
    )

    async def fake_apply(**_kwargs):
        raise HTTPException(
            status_code=409,
            detail={"code": "GMVMAX_MUTATION_INFLIGHT", "message": "syncing"},
        )

    monkeypatch.setattr(
        router_provider,
        "apply_gmvmax_campaign_action_provider",
        fake_apply,
    )

    # ``Task.run`` has no broker request context, so Celery re-raises the
    # original retryable HTTP exception here. A live worker turns it into a
    # one-second retry through ``self.retry``.
    with pytest.raises(HTTPException):
        ttb_gmvmax_tasks.execute_campaign_pause_intent_task.run(intent_id=intent.id)

    db_session.expire_all()
    persisted = db_session.get(GMVMaxCampaignPauseIntent, intent.id)
    assert persisted.status == "PENDING"
    assert persisted.active_key is not None
    assert persisted.attempt_count == 25
    assert ttb_gmvmax_tasks.execute_campaign_pause_intent_task.max_retries is None
