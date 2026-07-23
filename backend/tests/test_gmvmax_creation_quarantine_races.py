from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignCatalog,
)
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.schemas import (
    StrategyUpdateRequest,
    UpdateCampaignRequest,
)


def _context(
    db,
    *,
    advertiser_id: str = "adv-current",
    store_id: str = "store-current",
    client=None,
) -> router_provider.GMVMaxRouteContext:
    binding = router_provider.GMVMaxAccountBinding(
        account=None,
        advertiser_id=advertiser_id,
        store_id=store_id,
    )
    return router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        binding=binding,
        client=client or SimpleNamespace(),
        db=db,
    )


def _catalog(
    *,
    model=GmvmaxProductCampaignCatalog,
    advertiser_id: str = "adv-current",
    store_id: str = "store-current",
    campaign_id: str = "campaign-1",
):
    return model(
        workspace_id=1,
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
        campaign_name="Scoped campaign",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
        shopping_ads_type=(
            "LIVE" if model is GmvmaxLiveCampaignCatalog else "PRODUCT"
        ),
    )


def _intent(
    *,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    idempotency_key: str,
    state: str = "FINALIZING",
    replacement_campaign_id: str | None = None,
) -> GmvmaxCampaignCreateIntent:
    return GmvmaxCampaignCreateIntent(
        workspace_id=1,
        auth_id=2,
        advertiser_id=advertiser_id,
        store_id=store_id,
        idempotency_key=idempotency_key,
        client_payload_sha256="a" * 64,
        payload_sha256="b" * 64,
        official_request_id=idempotency_key,
        campaign_name="Scoped create",
        campaign_id=campaign_id,
        replacement_campaign_id=replacement_campaign_id,
        state=state,
        request_json={
            "store_id": store_id,
            "idempotency_key": idempotency_key,
        },
    )


def _fake_lease(*, on_enter=None, on_commit=None, on_exit=None):
    @contextmanager
    def manager(_context):
        if on_enter:
            on_enter()
        mutation = SimpleNamespace(
            assert_current=lambda *_args, **_kwargs: None,
            commit=on_commit or (lambda *_args, **_kwargs: None),
        )
        try:
            yield mutation
        finally:
            if on_exit:
                on_exit()

    return manager


def test_campaign_action_source_requires_exact_advertiser_and_store(db_session):
    context = _context(db_session)
    db_session.add_all(
        [
            _catalog(store_id="store-other", campaign_id="campaign-scoped"),
            _catalog(
                advertiser_id="adv-other",
                store_id="store-current",
                campaign_id="campaign-scoped",
            ),
        ]
    )
    db_session.commit()

    assert (
        router_provider._load_campaign_action_source(
            context, "campaign-scoped"
        )
        is None
    )

    exact = _catalog(
        model=GmvmaxLiveCampaignCatalog,
        campaign_id="campaign-scoped",
    )
    db_session.add(exact)
    db_session.commit()

    loaded = router_provider._load_campaign_action_source(
        context, "campaign-scoped"
    )
    assert loaded is exact
    assert loaded.advertiser_id == "adv-current"
    assert loaded.store_id == "store-current"


def test_unfinished_create_intent_requires_exact_advertiser_and_store(db_session):
    campaign_id = "campaign-intent-scope"
    context = _context(db_session)
    db_session.add(
        GmvStrategyConfig(
            workspace_id=1,
            auth_id=2,
            campaign_id=campaign_id,
            enabled=False,
            config_json={},
        )
    )
    db_session.add_all(
        [
            _intent(
                advertiser_id="adv-current",
                store_id="store-other",
                campaign_id=campaign_id,
                idempotency_key="10000001",
            ),
            _intent(
                advertiser_id="adv-other",
                store_id="store-current",
                campaign_id=campaign_id,
                idempotency_key="10000002",
            ),
        ]
    )
    db_session.commit()

    router_provider._ensure_campaign_not_creation_quarantined(
        context, campaign_id
    )

    db_session.add(
        _intent(
            advertiser_id="adv-current",
            store_id="store-current",
            campaign_id=campaign_id,
            idempotency_key="10000003",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as caught:
        router_provider._ensure_campaign_not_creation_quarantined(
            context, campaign_id
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "GMVMAX_CREATE_QUARANTINED"
    assert caught.value.detail["creation_state"] == "FINALIZING"


def test_unfinished_replacement_intent_quarantines_replaced_campaign(db_session):
    replaced_campaign_id = "campaign-being-replaced"
    context = _context(db_session)
    db_session.add(
        GmvStrategyConfig(
            workspace_id=1,
            auth_id=2,
            campaign_id=replaced_campaign_id,
            enabled=False,
            config_json={},
        )
    )
    db_session.add(
        _intent(
            advertiser_id="adv-current",
            store_id="store-current",
            campaign_id="replacement-campaign",
            replacement_campaign_id=replaced_campaign_id,
            idempotency_key="10000005",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as caught:
        router_provider._ensure_campaign_not_creation_quarantined(
            context, replaced_campaign_id
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "GMVMAX_CREATE_QUARANTINED"
    assert caught.value.detail["campaign_id"] == replaced_campaign_id
    assert caught.value.detail["creation_state"] == "FINALIZING"


def test_local_only_strategy_write_reloads_and_commits_inside_mutation_lease(
    monkeypatch,
    db_session,
):
    campaign_id = "campaign-local-write"
    campaign = _catalog(campaign_id=campaign_id)
    strategy = GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id=campaign_id,
        enabled=False,
        config_json={},
    )
    db_session.add_all([campaign, strategy])
    db_session.commit()
    context = _context(db_session)

    events: list[str] = []
    lease_active = False
    original_commit = db_session.commit
    original_load = router_provider._load_campaign_action_source
    original_check = router_provider._ensure_campaign_not_creation_quarantined
    original_apply = router_provider._apply_local_strategy_update

    def guarded_session_commit():
        if "mutation_commit" not in events[-1:]:
            raise AssertionError("strategy writes must commit through the mutation lease")
        original_commit()

    db_session.commit = guarded_session_commit

    def enter():
        nonlocal lease_active
        lease_active = True
        events.append("lease_enter")

    def leave():
        nonlocal lease_active
        events.append("lease_exit")
        lease_active = False

    def commit(_db):
        assert lease_active
        events.append("mutation_commit")
        guarded_session_commit()

    def load(*args, **kwargs):
        assert lease_active
        events.append("campaign_reload")
        return original_load(*args, **kwargs)

    def check(*args, **kwargs):
        assert lease_active
        events.append("quarantine_check")
        return original_check(*args, **kwargs)

    def apply(*args, **kwargs):
        assert lease_active
        events.append("strategy_write")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        _fake_lease(on_enter=enter, on_commit=commit, on_exit=leave),
    )
    monkeypatch.setattr(router_provider, "_load_campaign_action_source", load)
    monkeypatch.setattr(
        router_provider,
        "_ensure_campaign_not_creation_quarantined",
        check,
    )
    monkeypatch.setattr(router_provider, "_apply_local_strategy_update", apply)

    response = asyncio.run(
        router_provider.update_gmvmax_strategy_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            campaign_id=campaign_id,
            payload=StrategyUpdateRequest(enabled=True),
            advertiser_id=None,
            context=context,
        )
    )

    assert response.status == "success"
    assert strategy.enabled is True
    assert events == [
        "lease_enter",
        "campaign_reload",
        "quarantine_check",
        "strategy_write",
        "mutation_commit",
        "lease_exit",
    ]


def test_finalizing_intent_blocks_local_strategy_write_inside_lease(
    monkeypatch,
    db_session,
):
    campaign_id = "campaign-finalizing"
    strategy = GmvStrategyConfig(
        workspace_id=1,
        auth_id=2,
        campaign_id=campaign_id,
        enabled=False,
        config_json={},
    )
    db_session.add_all(
        [
            _catalog(campaign_id=campaign_id),
            strategy,
            _intent(
                advertiser_id="adv-current",
                store_id="store-current",
                campaign_id=campaign_id,
                idempotency_key="10000004",
            ),
        ]
    )
    db_session.commit()
    context = _context(db_session)
    lease_active = False
    entered = 0

    def enter():
        nonlocal lease_active, entered
        lease_active = True
        entered += 1

    def leave():
        nonlocal lease_active
        lease_active = False

    original_check = router_provider._ensure_campaign_not_creation_quarantined

    def check(*args, **kwargs):
        assert lease_active
        return original_check(*args, **kwargs)

    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        _fake_lease(on_enter=enter, on_exit=leave),
    )
    monkeypatch.setattr(
        router_provider,
        "_ensure_campaign_not_creation_quarantined",
        check,
    )
    monkeypatch.setattr(
        router_provider,
        "_apply_local_strategy_update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("quarantined strategy must not be changed")
        ),
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            router_provider.update_gmvmax_strategy_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=2,
                campaign_id=campaign_id,
                payload=StrategyUpdateRequest(enabled=True),
                advertiser_id=None,
                context=context,
            )
        )

    assert entered == 1
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "GMVMAX_CREATE_QUARANTINED"
    assert caught.value.detail["creation_state"] == "FINALIZING"
    assert strategy.enabled is False


@pytest.mark.parametrize("entrypoint", ["action", "campaign_update", "strategy"])
def test_cross_store_campaign_is_not_addressable_by_mutation_routes(
    monkeypatch,
    db_session,
    entrypoint,
):
    campaign_id = "campaign-other-store"
    db_session.add(
        _catalog(store_id="store-other", campaign_id=campaign_id)
    )
    db_session.commit()
    context = _context(db_session)
    lease_entries = 0

    def enter():
        nonlocal lease_entries
        lease_entries += 1

    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        _fake_lease(on_enter=enter),
    )

    with pytest.raises(HTTPException) as caught:
        if entrypoint == "action":
            asyncio.run(
                router_provider.apply_gmvmax_campaign_action_provider(
                    workspace_id=1,
                    provider="tiktok-business",
                    auth_id=2,
                    campaign_id=campaign_id,
                    payload={"type": "enable"},
                    advertiser_id=None,
                    me=SimpleNamespace(
                        id=1,
                        email="owner@example.test",
                        display_name=None,
                        username="owner",
                    ),
                    context=context,
                )
            )
        elif entrypoint == "campaign_update":
            asyncio.run(
                router_provider.update_gmvmax_campaign_provider(
                    workspace_id=1,
                    provider="tiktok-business",
                    auth_id=2,
                    campaign_id=campaign_id,
                    payload=UpdateCampaignRequest(name="Blocked update"),
                    context=context,
                )
            )
        else:
            asyncio.run(
                router_provider.update_gmvmax_strategy_provider(
                    workspace_id=1,
                    provider="tiktok-business",
                    auth_id=2,
                    campaign_id=campaign_id,
                    payload=StrategyUpdateRequest(enabled=True),
                    advertiser_id=None,
                    context=context,
                )
            )

    assert caught.value.status_code == 404
    assert caught.value.detail == "Campaign not found"
    assert lease_entries == (1 if entrypoint == "strategy" else 0)
