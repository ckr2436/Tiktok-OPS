import asyncio
from types import SimpleNamespace

from app.features.tenants.ttb.gmv_max import router_provider
from app.providers.tiktok_business.gmvmax_client import PageInfo


def _build_stub_context():
    binding = router_provider.GMVMaxAccountBinding(
        account=None,
        advertiser_id="adv-123",
        store_id="store-999",
    )
    return router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        advertiser_id="adv-123",
        store_id="store-999",
        binding=binding,
        client=SimpleNamespace(gmv_max_campaign_get=lambda *_, **__: None),
    )


def test_list_campaigns_accepts_explicit_advertiser_id(monkeypatch):
    stub_context = _build_stub_context()
    calls = []

    async def fake_call(func, *args, **kwargs):  # noqa: ANN001
        calls.append((func, args, kwargs))
        data = type("Payload", (), {"list": [], "page_info": PageInfo()})()
        return type("Resp", (), {"data": data, "request_id": "stub"})()

    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

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
    assert response.request_id == "stub"
    _, args, _ = calls[-1]
    request = args[0]
    assert str(request.advertiser_id) == "123"


def test_list_campaigns_uses_context_advertiser(monkeypatch):
    stub_context = _build_stub_context()
    calls = []

    async def fake_call(func, *args, **kwargs):  # noqa: ANN001
        calls.append((func, args, kwargs))
        data = type("Payload", (), {"list": [], "page_info": PageInfo()})()
        return type("Resp", (), {"data": data, "request_id": "stub"})()

    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)

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
    _, args, _ = calls[-1]
    request = args[0]
    assert str(request.advertiser_id) == "adv-123"
