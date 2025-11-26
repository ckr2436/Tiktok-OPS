import asyncio
from types import SimpleNamespace

from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.schemas import (
    CreateCampaignRequest,
    UpdateCampaignRequest,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignInfoData,
    GMVMaxCampaignInfoRequest,
    PageInfo,
)


def _build_stub_context():
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

    return router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        advertiser_id="adv-123",
        store_id="store-999",
        binding=binding,
        client=SimpleNamespace(
            gmv_max_campaign_get=lambda *_, **__: None,
            gmv_max_campaign_info=lambda *_, **__: None,
        ),
        db=SimpleNamespace(query=lambda *_, **__: _Query(), flush=lambda: None),
    )


def test_list_campaigns_accepts_explicit_advertiser_id(monkeypatch):
    stub_context = _build_stub_context()

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
            advertiser_id="123",
            context=stub_context,
        )
    )
    assert response.items == []
    assert isinstance(response.page_info, PageInfo)
    assert response.page_info.page == 1
    assert response.page_info.page_size == 20


def test_list_campaigns_uses_context_advertiser(monkeypatch):
    stub_context = _build_stub_context()

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
            context=stub_context,
        )
    )
    assert response.items == []
    assert response.page_info.page == 1


def test_create_campaign_provider_invokes_service(monkeypatch):
    stub_context = _build_stub_context()
    calls: dict[str, object] = {}

    async def fake_create(db, *, workspace_id, provider, auth_id, advertiser_id, client, body):
        calls["create_args"] = (workspace_id, provider, auth_id, advertiser_id, body)
        return SimpleNamespace(campaign_id="cmp-new")

    async def fake_call(func, request):  # noqa: ANN001
        calls["info_request"] = request
        return SimpleNamespace(
            data=GMVMaxCampaignInfoData(campaign_id="cmp-new", campaign_name="new"),
            request_id="req-123",
        )

    monkeypatch.setattr(router_provider, "create_gmvmax_campaign", fake_create)
    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

    payload = CreateCampaignRequest(
        name="New Campaign",
        store_id="store-1",
        item_group_ids=["item-1"],
        promotion_type="PRODUCT",
        objective_type="VALUE",
        daily_budget=10.0,
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
    workspace_id, provider, auth_id, advertiser_id, body = calls["create_args"]
    assert workspace_id == 1
    assert provider == "tiktok-business"
    assert auth_id == 1
    assert advertiser_id == "adv-123"
    assert getattr(body, "campaign_name", None) == "New Campaign"


def test_update_campaign_provider_invokes_service(monkeypatch):
    stub_context = _build_stub_context()
    calls: dict[str, object] = {}

    async def fake_update(db, *, workspace_id, provider, auth_id, advertiser_id, client, body):
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
