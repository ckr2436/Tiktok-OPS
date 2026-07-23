import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.schemas import (
    CreateCampaignRequest,
    GmvMaxManualSyncRequest,
    StrategyUpdateRequest,
    UpdateCampaignRequest,
)
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignInfoRequest,
    PageInfo,
)


def _build_stub_context(db=None):
    binding = router_provider.GMVMaxAccountBinding(
        account=None,
        advertiser_id="adv-123",
        store_id="store-999",
    )

    class _Query:
        def filter(self, *_, **__):
            return self

        def count(self) -> int:
            return 0

        def order_by(self, *_, **__):
            return self

        def offset(self, *_, **__):
            return self

        def limit(self, *_, **__):
            return self

        def all(self):
            return []

        def first(self):
            return None

    if db is None:
        db = SimpleNamespace(
            query=lambda *_, **__: _Query(),
            flush=lambda: None,
            commit=lambda: None,
            rollback=lambda: None,
            add=lambda *_: None,
            execute=lambda *_: None,
            begin_nested=lambda: nullcontext(),
        )

    return router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        advertiser_id="adv-123",
        store_id="store-999",
        binding=binding,
        client=SimpleNamespace(
            campaign_status_update=lambda *_, **__: None,
            gmv_max_campaign_get=lambda *_, **__: None,
            gmv_max_campaign_info=lambda *_, **__: None,
        ),
        db=db,
    )


def _install_creation_quarantine_gate_stubs(
    monkeypatch,
    context,
    *,
    strategy,
):
    campaign = SimpleNamespace(
        campaign_id="cmp-gated",
        advertiser_id="adv-123",
        store_id="store-999",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
        is_deleted=False,
    )
    remote_calls: list[object] = []

    async def forbidden_remote_call(_func, request):  # noqa: ANN001
        remote_calls.append(request)
        raise AssertionError("creation quarantine must block the remote mutation")

    async def forbidden_product_check(*_args, **_kwargs):
        raise AssertionError("enable must stop before product or remote checks")

    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_args, **_kwargs: campaign,
    )
    monkeypatch.setattr(
        router_provider,
        "_get_local_strategy_config",
        lambda *_args, **_kwargs: strategy,
    )
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", forbidden_remote_call)
    monkeypatch.setattr(
        router_provider,
        "_ensure_campaign_products_available",
        forbidden_product_check,
    )
    monkeypatch.setattr(
        router_provider,
        "_log_action_entry",
        lambda *_args, **_kwargs: None,
    )
    return remote_calls


def _invoke_creation_quarantine_guarded_mutation(entrypoint, context):
    if entrypoint == "enable":
        return asyncio.run(
            router_provider.apply_gmvmax_campaign_action_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=1,
                campaign_id="cmp-gated",
                payload={"type": "enable"},
                advertiser_id=None,
                me=SimpleNamespace(
                    id=1,
                    email="admin@example.test",
                    display_name=None,
                    username="admin",
                ),
                context=context,
            )
        )
    return asyncio.run(
        router_provider.update_gmvmax_strategy_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            campaign_id="cmp-gated",
            payload=StrategyUpdateRequest(campaign={"roas_bid": 1.2}),
            advertiser_id=None,
            context=context,
        )
    )


def test_list_campaigns_accepts_explicit_advertiser_id(monkeypatch, db_session):
    stub_context = _build_stub_context(db_session)

    response = asyncio.run(
        router_provider.list_gmvmax_campaigns_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            gmv_max_promotion_types=None,
            store_ids=None,
            campaign_ids=None,
            campaign_name=None,
            primary_status=None,
            creation_filter_start_time=None,
            creation_filter_end_time=None,
            fields=None,
            page=None,
            page_size=None,
            advertiser_id="adv-123",
            include_deleted=False,
            performance_start_date=None,
            performance_end_date=None,
            context=stub_context,
        )
    )
    assert response.items == []
    assert isinstance(response.page_info, PageInfo)
    assert response.page_info.page == 1
    assert response.page_info.page_size == 20


def test_list_campaigns_uses_context_advertiser(monkeypatch, db_session):
    stub_context = _build_stub_context(db_session)

    response = asyncio.run(
        router_provider.list_gmvmax_campaigns_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            gmv_max_promotion_types=None,
            store_ids=None,
            campaign_ids=None,
            campaign_name=None,
            primary_status=None,
            creation_filter_start_time=None,
            creation_filter_end_time=None,
            fields=None,
            page=None,
            page_size=None,
            advertiser_id=None,
            include_deleted=False,
            performance_start_date=None,
            performance_end_date=None,
            context=stub_context,
        )
    )
    assert response.items == []
    assert response.page_info.page == 1


def test_campaign_sync_route_forces_path_campaign_scope(monkeypatch, db_session):
    context = _build_stub_context(db_session)
    captured = {}

    def _capture_sync(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(task_id="task-1", state="PENDING")

    monkeypatch.setattr(router_provider, "sync_gmvmax_manual", _capture_sync)
    response = asyncio.run(
        router_provider.sync_gmvmax_metrics_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            campaign_id="campaign-current",
            payload=GmvMaxManualSyncRequest(
                levels=["CAMPAIGN"],
                campaign_ids=["campaign-other"],
            ),
            advertiser_id=None,
            me=SimpleNamespace(id=1),
            context=context,
        )
    )

    assert response.task_id == "task-1"
    assert captured["payload"].campaign_ids == ["campaign-current"]


def test_campaign_metrics_route_forces_path_campaign_scope(monkeypatch, db_session):
    context = _build_stub_context(db_session)
    captured = {}
    expected = SimpleNamespace(rows=[])

    async def _capture_query(_request, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(router_provider, "_query_gmvmax_metrics", _capture_query)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/metrics",
            "headers": [],
            "query_string": b"level=campaign",
        }
    )
    response = asyncio.run(
        router_provider.query_gmvmax_metrics_provider(
            request=request,
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            campaign_id=12345,
            store_id=None,
            level="campaign",
            start_date=None,
            end_date=None,
            advertiser_id=None,
            campaign_ids=["campaign-other"],
            item_group_ids=None,
            context=context,
        )
    )

    assert response is expected
    assert captured["campaign_ids"] == ["12345"]


@pytest.mark.parametrize("entrypoint", ["enable", "strategy"])
def test_creation_quarantine_strategy_marker_blocks_mutation_before_remote(
    monkeypatch,
    entrypoint,
):
    context = _build_stub_context()
    strategy = SimpleNamespace(
        config_json={
            "creation_quarantine": {
                "enabled": True,
                "reason": "create finalization failed",
            }
        }
    )
    remote_calls = _install_creation_quarantine_gate_stubs(
        monkeypatch,
        context,
        strategy=strategy,
    )

    with pytest.raises(HTTPException) as exc_info:
        _invoke_creation_quarantine_guarded_mutation(entrypoint, context)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "GMVMAX_CREATE_QUARANTINED"
    assert exc_info.value.detail["creation_state"] == "QUARANTINED"
    assert remote_calls == []


@pytest.mark.parametrize("entrypoint", ["enable", "strategy"])
def test_creation_finalizing_intent_blocks_mutation_without_config_marker(
    monkeypatch,
    entrypoint,
):
    context = _build_stub_context()
    intent = SimpleNamespace(
        campaign_id="cmp-gated",
        state="FINALIZING",
    )

    class _IntentQuery:
        def filter(self, *_, **__):
            return self

        def order_by(self, *_, **__):
            return self

        def first(self):
            return intent

    context.db.query = lambda *_args, **_kwargs: _IntentQuery()
    remote_calls = _install_creation_quarantine_gate_stubs(
        monkeypatch,
        context,
        strategy=SimpleNamespace(config_json={}),
    )

    with pytest.raises(HTTPException) as exc_info:
        _invoke_creation_quarantine_guarded_mutation(entrypoint, context)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "GMVMAX_CREATE_QUARANTINED"
    assert exc_info.value.detail["creation_state"] == "FINALIZING"
    assert remote_calls == []


def test_create_campaign_provider_requires_idempotency_key():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider.create_gmvmax_campaign_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=1,
                payload=CreateCampaignRequest(
                    name="Missing Idempotency Key",
                    store_id="store-999",
                    item_group_ids=["item-1"],
                    budget=10,
                ),
                context=_build_stub_context(),
            )
        )

    assert exc_info.value.status_code == 422
    assert (
        exc_info.value.detail["code"]
        == "GMVMAX_CREATE_IDEMPOTENCY_KEY_REQUIRED"
    )


def test_create_campaign_provider_invokes_service(monkeypatch):
    stub_context = _build_stub_context()
    calls: dict[str, object] = {}
    strategy = SimpleNamespace(
        id=1,
        enabled=False,
        target_roi=None,
        min_roi=None,
        cooldown_minutes=30,
        min_runtime_minutes_before_first_change=10,
        config_json={},
    )

    async def fake_create(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        advertiser_id,
        client,
        payload,
        store_authorized_bc_id,
        client_payload_sha256,
        execution_guard,
    ):
        execution_guard()
        calls["create_args"] = (
            workspace_id,
            provider,
            auth_id,
            advertiser_id,
            payload,
            store_authorized_bc_id,
            client_payload_sha256,
        )
        return SimpleNamespace(campaign_id="cmp-new")

    async def fake_call(func, request):  # noqa: ANN001
        if isinstance(request, CampaignStatusUpdateRequest):
            calls.setdefault("status_updates", []).append(request.operation_status)
            return SimpleNamespace(data={}, request_id=f"status-{request.operation_status}")
        assert isinstance(request, GMVMaxCampaignInfoRequest)
        calls["info_request"] = request
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(campaign_id="cmp-new", campaign_name="new"),
            request_id="req-123",
        )

    async def fake_authorized(*_, **__):
        return "bc-1"

    monkeypatch.setattr(router_provider, "svc_create_campaign", fake_create)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(router_provider, "ensure_gmvmax_store_authorized", fake_authorized)
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda _db, request, **_kwargs: request,
    )
    monkeypatch.setattr(
        router_provider,
        "_get_or_create_local_strategy_config",
        lambda *_args, **_kwargs: strategy,
    )

    async def fake_inherit(*_, **__):
        return {"excluded": []}

    monkeypatch.setattr(
        router_provider, "_inherit_historical_creative_exclusions", fake_inherit
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    payload = CreateCampaignRequest(
        request_id="123456781",
        idempotency_key="123456781",
        name="New Campaign",
        store_id="store-999",
        item_group_ids=["item-1"],
        promotion_type="PRODUCT",
        objective_type="VALUE",
        budget=10.0,
        automation={
            "enabled": False,
            "hermes_enabled": False,
        },
    )

    response = asyncio.run(
        router_provider.create_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            payload=payload,
            context=stub_context,
        )
    )

    assert response.campaign.campaign_id == "cmp-new"
    assert response.request_id == "req-123"
    assert isinstance(calls["info_request"], GMVMaxCampaignInfoRequest)
    assert calls["info_request"].campaign_id == "cmp-new"
    (
        workspace_id,
        provider,
        auth_id,
        advertiser_id,
        body,
        store_bc_id,
        client_payload_sha256,
    ) = calls["create_args"]
    assert workspace_id == 1
    assert provider == "tiktok-business"
    assert auth_id == 1
    assert advertiser_id == "adv-123"
    assert getattr(body, "campaign_name", None) == "New Campaign"
    assert store_bc_id == "bc-1"
    expected_payload = payload.model_copy(update={"advertiser_id": "adv-123"})
    assert client_payload_sha256 == router_provider.gmvmax_create_payload_sha256(
        expected_payload,
        advertiser_id="adv-123",
    )
    assert calls["status_updates"] == ["DISABLE", "ENABLE"]
    assert strategy.enabled is False
    assert strategy.config_json["hermes_enabled"] is False
    assert strategy.config_json["smart_guard"]["hermes_enabled"] is False


def test_create_campaign_provider_falls_back_to_catalog_when_detail_lookup_fails(
    monkeypatch,
):
    stub_context = _build_stub_context()
    created_row = SimpleNamespace(
        campaign_id="cmp-new",
        campaign_name="New Campaign",
        store_id="store-999",
    )
    calls: dict[str, object] = {}

    async def fake_create(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        advertiser_id,
        client,
        payload,
        store_authorized_bc_id,
        client_payload_sha256,
        execution_guard,
    ):
        execution_guard()
        calls["client_payload_sha256"] = client_payload_sha256
        return created_row

    async def fake_call(func, request):  # noqa: ANN001
        if isinstance(request, CampaignStatusUpdateRequest):
            calls.setdefault("status_updates", []).append(request.operation_status)
            return SimpleNamespace(data={}, request_id=f"status-{request.operation_status}")
        assert isinstance(request, GMVMaxCampaignInfoRequest)
        calls["info_request"] = request
        raise HTTPException(status_code=503, detail="campaign detail unavailable")

    async def fake_authorized(*_, **__):
        return "bc-1"

    async def fake_inherit(*_, **__):
        return {"excluded": []}

    def fake_catalog_row_to_detail(row, promotion_type):  # noqa: ANN001
        calls["catalog_fallback"] = (row, promotion_type)
        return GMVMaxCampaignInfoData(
            campaign_id=row.campaign_id,
            campaign_name=row.campaign_name,
            store_id=row.store_id,
        )

    monkeypatch.setattr(router_provider, "svc_create_campaign", fake_create)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(router_provider, "ensure_gmvmax_store_authorized", fake_authorized)
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda _db, request, **_kwargs: request,
    )
    monkeypatch.setattr(
        router_provider,
        "_get_or_create_local_strategy_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=1,
            enabled=False,
            target_roi=None,
            min_roi=None,
            cooldown_minutes=30,
            min_runtime_minutes_before_first_change=10,
            config_json={},
        ),
    )
    monkeypatch.setattr(
        router_provider, "_inherit_historical_creative_exclusions", fake_inherit
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)
    monkeypatch.setattr(
        router_provider, "_catalog_row_to_detail", fake_catalog_row_to_detail
    )

    payload = CreateCampaignRequest(
        request_id="123456782",
        idempotency_key="123456782",
        name="New Campaign",
        store_id="store-999",
        item_group_ids=["item-1"],
        promotion_type="PRODUCT",
        objective_type="VALUE",
        budget=10.0,
    )

    response = asyncio.run(
        router_provider.create_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            payload=payload,
            context=stub_context,
        )
    )

    assert response.campaign.campaign_id == "cmp-new"
    assert response.campaign.store_id == "store-999"
    assert response.request_id is None
    assert isinstance(calls["info_request"], GMVMaxCampaignInfoRequest)
    assert calls["catalog_fallback"] == (created_row, "PRODUCT")
    assert calls["status_updates"] == ["DISABLE", "ENABLE"]
    expected_payload = payload.model_copy(update={"advertiser_id": "adv-123"})
    assert calls[
        "client_payload_sha256"
    ] == router_provider.gmvmax_create_payload_sha256(
        expected_payload,
        advertiser_id="adv-123",
    )


def test_create_campaign_provider_quarantines_a_postprocessing_failure(monkeypatch):
    stub_context = _build_stub_context()
    stub_context.client.campaign_status_update = lambda *_args, **_kwargs: None
    created_row = SimpleNamespace(
        campaign_id="cmp-quarantined",
        campaign_name="New Campaign",
        store_id="store-999",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
    )
    strategy = SimpleNamespace(
        id=1,
        enabled=True,
        target_roi=None,
        min_roi=None,
        cooldown_minutes=30,
        min_runtime_minutes_before_first_change=10,
        config_json={},
    )
    status_updates: list[str] = []

    async def fake_create(*_args, **_kwargs):
        return created_row

    async def fake_authorized(*_, **__):
        return "bc-1"

    async def fail_inherit(*_, **__):
        raise RuntimeError("historical exclusion failed")

    async def fake_call(_func, request):  # noqa: ANN001
        if hasattr(request, "operation_status"):
            status_updates.append(request.operation_status)
            return SimpleNamespace(data={}, request_id="pause-request")
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(
                campaign_id="cmp-quarantined",
                campaign_name="New Campaign",
                store_id="store-999",
            ),
            request_id="info-request",
        )

    monkeypatch.setattr(router_provider, "svc_create_campaign", fake_create)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda _db, request, **_kwargs: request,
    )
    monkeypatch.setattr(
        router_provider,
        "ensure_gmvmax_store_authorized",
        fake_authorized,
    )
    monkeypatch.setattr(
        router_provider,
        "_get_or_create_local_strategy_config",
        lambda *_args, **_kwargs: strategy,
    )
    monkeypatch.setattr(
        router_provider,
        "_inherit_historical_creative_exclusions",
        fail_inherit,
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    response = asyncio.run(
        router_provider.create_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            payload=CreateCampaignRequest(
                request_id="123456783",
                idempotency_key="123456783",
                name="New Campaign",
                store_id="store-999",
                item_group_ids=["item-1"],
                budget=10,
            ),
            context=stub_context,
        )
    )

    assert response.campaign.campaign_id == "cmp-quarantined"
    assert response.creation_status == "QUARANTINED"
    assert response.warnings[0]["code"] == "GMVMAX_CREATE_POSTPROCESS_QUARANTINED"
    assert status_updates == ["DISABLE", "DISABLE"]
    assert created_row.operation_status == "DISABLE"
    assert strategy.enabled is False
    assert strategy.config_json["creation_quarantine"]["enabled"] is True


def test_create_campaign_provider_does_not_refinalize_a_succeeded_intent(monkeypatch):
    stub_context = _build_stub_context()
    raw_payload = CreateCampaignRequest(
        request_id="123456789",
        idempotency_key="123456789",
        name="Existing Campaign",
        store_id="store-999",
        item_group_ids=["item-1"],
        budget=10,
    )
    scoped_payload = raw_payload.model_copy(update={"advertiser_id": "adv-123"})
    client_payload_sha256 = router_provider.gmvmax_create_payload_sha256(
        scoped_payload,
        advertiser_id="adv-123",
    )
    existing_intent = SimpleNamespace(
        state="SUCCEEDED",
        idempotency_key="123456789",
        client_payload_sha256=client_payload_sha256,
        request_json=scoped_payload.model_dump(mode="json", exclude_none=True),
    )
    created_row = SimpleNamespace(
        campaign_id="cmp-existing",
        campaign_name="Existing Campaign",
        store_id="store-999",
    )
    finalize_calls = 0
    service_calls = 0

    async def fake_create(*_args, payload, client_payload_sha256, **_kwargs):
        nonlocal service_calls
        service_calls += 1
        assert payload.campaign_name == "Existing Campaign"
        assert client_payload_sha256 == existing_intent.client_payload_sha256
        return created_row

    async def fake_authorized(*_, **__):
        return "bc-1"

    async def forbidden_finalize(*_, **__):
        nonlocal finalize_calls
        finalize_calls += 1
        raise AssertionError("a SUCCEEDED intent must not be finalized twice")

    async def fake_call(_func, request):
        assert isinstance(request, GMVMaxCampaignInfoRequest)
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(
                campaign_id="cmp-existing",
                campaign_name="Existing Campaign",
                store_id="store-999",
            ),
            request_id="info-request",
        )

    monkeypatch.setattr(router_provider, "svc_create_campaign", fake_create)
    monkeypatch.setattr(
        router_provider,
        "get_gmvmax_create_intent",
        lambda *_args, **_kwargs: existing_intent,
    )
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an existing intent must use its frozen request")
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "ensure_gmvmax_store_authorized",
        fake_authorized,
    )
    monkeypatch.setattr(
        router_provider,
        "_inherit_historical_creative_exclusions",
        forbidden_finalize,
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    response = asyncio.run(
        router_provider.create_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            payload=raw_payload,
            context=stub_context,
        )
    )

    assert response.campaign.campaign_id == "cmp-existing"
    assert response.creation_status == "SUCCEEDED"
    assert finalize_calls == 0
    assert service_calls == 1


def test_create_campaign_provider_rejects_same_key_with_different_client_payload(
    monkeypatch,
):
    stub_context = _build_stub_context()
    original_payload = CreateCampaignRequest(
        request_id="123456784",
        idempotency_key="123456784",
        name="Original Campaign",
        store_id="store-999",
        item_group_ids=["item-1"],
        budget=10,
    )
    scoped_original = original_payload.model_copy(update={"advertiser_id": "adv-123"})
    existing_intent = SimpleNamespace(
        state="UNKNOWN",
        idempotency_key="123456784",
        client_payload_sha256=router_provider.gmvmax_create_payload_sha256(
            scoped_original,
            advertiser_id="adv-123",
        ),
        request_json=scoped_original.model_dump(mode="json", exclude_none=True),
    )

    monkeypatch.setattr(
        router_provider,
        "get_gmvmax_create_intent",
        lambda *_args, **_kwargs: existing_intent,
    )
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "svc_create_campaign",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a conflicting client payload must not reach the service")
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a conflicting client payload must not apply new defaults")
        ),
    )

    conflicting_payload = original_payload.model_copy(update={"budget": 11})
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider.create_gmvmax_campaign_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=1,
                payload=conflicting_payload,
                context=stub_context,
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "GMVMAX_CREATE_IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("initial_state", ["UNKNOWN", "REMOTE_CREATED"])
def test_create_campaign_provider_resumes_frozen_intent_without_recreate(
    monkeypatch,
    initial_state,
):
    stub_context = _build_stub_context()
    raw_payload = CreateCampaignRequest(
        request_id="123456785",
        idempotency_key="123456785",
        name="Client Campaign Name",
        store_id="store-999",
        item_group_ids=["item-1"],
        budget=10,
    )
    scoped_raw_payload = raw_payload.model_copy(update={"advertiser_id": "adv-123"})
    client_payload_sha256 = router_provider.gmvmax_create_payload_sha256(
        scoped_raw_payload,
        advertiser_id="adv-123",
    )
    frozen_payload = scoped_raw_payload.model_copy(
        update={
            "campaign_name": "Frozen Approved Plan",
            "budget": 42,
            "automation": {
                "enabled": True,
                "monitor_interval_minutes": 7,
            },
        }
    )
    existing_intent = SimpleNamespace(
        state=initial_state,
        idempotency_key="123456785",
        client_payload_sha256=client_payload_sha256,
        request_json=frozen_payload.model_dump(mode="json", exclude_none=True),
    )
    created_row = SimpleNamespace(
        campaign_id="cmp-recovered",
        campaign_name="Frozen Approved Plan",
        store_id="store-999",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
    )
    strategy = SimpleNamespace(
        id=1,
        enabled=False,
        target_roi=None,
        min_roi=None,
        cooldown_minutes=30,
        min_runtime_minutes_before_first_change=10,
        config_json={},
    )
    calls: dict[str, object] = {
        "official_create": 0,
        "service": 0,
        "status_updates": [],
        "intent_states": [],
    }

    async def forbidden_official_create(*_args, **_kwargs):
        calls["official_create"] += 1
        raise AssertionError("an unfinished intent must never be created again")

    stub_context.client.gmv_max_campaign_create = forbidden_official_create

    async def fake_resume(
        _db,
        *,
        payload,
        client_payload_sha256,
        execution_guard,
        **_kwargs,
    ):
        calls["service"] += 1
        calls["service_payload"] = payload
        calls["service_payload_sha256"] = client_payload_sha256
        execution_guard(_db)
        if existing_intent.state == "UNKNOWN":
            existing_intent.state = "REMOTE_CREATED"
        return created_row

    async def fake_authorized(*_, **__):
        return "bc-1"

    async def fake_inherit(*_, **__):
        assert strategy.enabled is False
        assert strategy.config_json["creation_quarantine"]["enabled"] is True
        assert strategy.config_json["creation_quarantine"]["state"] == "FINALIZING"
        return {"excluded": []}

    async def fake_call(_func, request):  # noqa: ANN001
        if isinstance(request, CampaignStatusUpdateRequest):
            calls["status_updates"].append(request.operation_status)
            return SimpleNamespace(data={}, request_id=f"status-{request.operation_status}")
        assert isinstance(request, GMVMaxCampaignInfoRequest)
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(
                campaign_id="cmp-recovered",
                campaign_name="Frozen Approved Plan",
                store_id="store-999",
            ),
            request_id="info-recovered",
        )

    def fake_mark(_db, *, state, **_kwargs):
        existing_intent.state = state
        calls["intent_states"].append(state)
        return existing_intent

    def fake_commit(_db):
        quarantine = strategy.config_json.get("creation_quarantine")
        calls.setdefault("commit_snapshots", []).append(
            {
                "intent_state": existing_intent.state,
                "strategy_enabled": strategy.enabled,
                "quarantine": dict(quarantine) if quarantine else None,
            }
        )

    monkeypatch.setattr(router_provider, "svc_create_campaign", fake_resume)
    monkeypatch.setattr(
        router_provider,
        "get_gmvmax_create_intent",
        lambda *_args, **_kwargs: existing_intent,
    )
    monkeypatch.setattr(router_provider, "mark_gmvmax_create_intent", fake_mark)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=fake_commit,
            )
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "apply_approved_plan_defaults_to_create_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not reapply dynamic plan defaults")
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "ensure_gmvmax_store_authorized",
        fake_authorized,
    )
    monkeypatch.setattr(
        router_provider,
        "_get_or_create_local_strategy_config",
        lambda *_args, **_kwargs: strategy,
    )
    monkeypatch.setattr(
        router_provider,
        "_inherit_historical_creative_exclusions",
        fake_inherit,
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    response = asyncio.run(
        router_provider.create_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            payload=raw_payload,
            context=stub_context,
        )
    )

    service_payload = calls["service_payload"]
    assert service_payload.campaign_name == "Frozen Approved Plan"
    assert service_payload.budget == 42
    assert service_payload.automation["monitor_interval_minutes"] == 7
    assert calls["service_payload_sha256"] == client_payload_sha256
    assert calls["service"] == 1
    assert calls["official_create"] == 0
    assert calls["status_updates"] == ["DISABLE", "ENABLE"]
    assert calls["intent_states"] == ["FINALIZING", "SUCCEEDED"]
    assert len(calls["commit_snapshots"]) == 2
    finalizing_snapshot, succeeded_snapshot = calls["commit_snapshots"]
    assert finalizing_snapshot["intent_state"] == "FINALIZING"
    assert finalizing_snapshot["strategy_enabled"] is False
    assert finalizing_snapshot["quarantine"]["enabled"] is True
    assert finalizing_snapshot["quarantine"]["state"] == "FINALIZING"
    assert succeeded_snapshot == {
        "intent_state": "SUCCEEDED",
        "strategy_enabled": True,
        "quarantine": None,
    }
    assert strategy.enabled is True
    assert response.creation_status == "SUCCEEDED"
    assert response.campaign.campaign_id == "cmp-recovered"


def test_update_campaign_provider_invokes_service(monkeypatch):
    stub_context = _build_stub_context()
    calls: dict[str, object] = {}

    async def fake_update(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        advertiser_id,
        client,
        body,
        execution_guard,
    ):
        execution_guard()
        calls["update_args"] = (workspace_id, provider, auth_id, advertiser_id, body)
        return SimpleNamespace(campaign_id="cmp-updated")

    async def fake_call(func, request):  # noqa: ANN001
        calls["info_request"] = request
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(
                campaign_id="cmp-updated", campaign_name="updated"
            ),
            request_id="req-456",
        )

    monkeypatch.setattr(router_provider, "update_gmvmax_campaign", fake_update)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(
                assert_current=lambda *_: None,
                commit=lambda *_: None,
            )
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_: SimpleNamespace(
            campaign_id="cmp-updated",
            operation_status="ENABLE",
            is_deleted=False,
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "_get_local_strategy_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    payload = UpdateCampaignRequest(name="Updated", daily_budget=25.0)

    response = asyncio.run(
        router_provider.update_gmvmax_campaign_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=1,
            campaign_id="cmp-updated",
            payload=payload,
            context=stub_context,
        )
    )

    assert response.campaign.campaign_id == "cmp-updated"
    assert response.request_id == "req-456"
    assert isinstance(calls["info_request"], GMVMaxCampaignInfoRequest)
    assert calls["info_request"].campaign_id == "cmp-updated"
    workspace_id, provider, auth_id, advertiser_id, body = calls["update_args"]
    assert workspace_id == 1
    assert provider == "tiktok-business"
    assert auth_id == 1
    assert advertiser_id == "adv-123"
    assert getattr(body, "campaign_id", None) == "cmp-updated"
