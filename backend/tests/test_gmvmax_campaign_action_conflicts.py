import types

import types

import pytest

from app.data.models.gmv_restructured import PromotionTypeEnum
from app.features.tenants.ttb.gmv_max import service
from app.services.ttb_api import TTBBusinessError


class _FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def refresh(self, _obj):  # noqa: D401
        """No-op refresh."""
        return None


class _FakeGMVClient:
    def __init__(self, usage=None):
        self._usage = usage

    async def gmv_max_store_shop_ad_usage_check(self, request):  # noqa: ANN001
        return types.SimpleNamespace(data=self._usage, request=request)

    async def aclose(self):  # noqa: D401
        """No-op close."""
        return None


class _FakeTTBClient:
    def __init__(self, products=None):
        self._products = products or []

    async def iter_products(self, **_kwargs):  # noqa: ANN001
        for product in self._products:
            yield product

    async def aclose(self):  # noqa: D401
        """No-op close."""
        return None


class _FakeCampaign:
    def __init__(self, *, promotion_type, store_id="1", raw_json=None, item_group_ids=None):
        self.promotion_type = promotion_type
        self.store_id = store_id
        self.raw_json = raw_json or {}
        self.item_group_ids = item_group_ids or []
        self.campaign_id = "cmp"


@pytest.fixture(autouse=True)
def _monkey_defaults(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)
    monkeypatch.setattr(service, "get_advertiser_id_for_account", lambda *a, **k: "adv")


pytestmark = pytest.mark.anyio


async def test_apply_all_conflict(monkeypatch):
    usage = types.SimpleNamespace(promote_all_products_allowed=False)
    monkeypatch.setattr(service, "_ensure_campaign", lambda *_a, **_k: _FakeCampaign(promotion_type=PromotionTypeEnum.PRODUCT, raw_json={"product_specific_type": "ALL"}))
    monkeypatch.setattr(service, "get_gmvmax_client_for_account", lambda *a, **k: _FakeGMVClient(usage=usage))
    called = False

    async def _fake_apply(*_a, **_k):  # noqa: ANN001
        nonlocal called
        called = True
        return types.SimpleNamespace()

    monkeypatch.setattr(service, "svc_apply_campaign_action", _fake_apply)

    db = _FakeSession()
    with pytest.raises(TTBBusinessError) as exc:
        await service.apply_campaign_action(
            db,
            workspace_id=1,
            provider="ttb",
            auth_id=1,
            campaign_id="c1",
            action="ENABLE",
            payload={},
            reason=None,
            performed_by="me",
        )

    assert exc.value.code == "GMVMAX_PROMOTE_ALL_CONFLICT"
    assert called is False
    assert db.committed is False


async def test_apply_custom_conflict(monkeypatch):
    campaign = _FakeCampaign(
        promotion_type=PromotionTypeEnum.PRODUCT,
        raw_json={"product_specific_type": "CUSTOMIZED_PRODUCTS"},
    )
    monkeypatch.setattr(service, "_ensure_campaign", lambda *_a, **_k: campaign)
    monkeypatch.setattr(service, "get_gmvmax_client_for_account", lambda *a, **k: _FakeGMVClient())
    monkeypatch.setattr(service, "get_item_group_ids_for_campaign", lambda *_a, **_k: ["g1", "g2"])
    ttb_client = _FakeTTBClient(
        products=[
            {"item_group_id": "g1", "gmv_max_ads_status": "OCCUPIED", "status": "AVAILABLE"},
            {"item_group_id": "g2", "gmv_max_ads_status": "UNOCCUPIED", "status": "AVAILABLE"},
        ]
    )
    monkeypatch.setattr(service, "build_ttb_client", lambda *_a, **_k: ttb_client)

    called = False

    async def _fake_apply(*_a, **_k):  # noqa: ANN001
        nonlocal called
        called = True
        return types.SimpleNamespace()

    monkeypatch.setattr(service, "svc_apply_campaign_action", _fake_apply)

    db = _FakeSession()
    with pytest.raises(TTBBusinessError) as exc:
        await service.apply_campaign_action(
            db,
            workspace_id=1,
            provider="ttb",
            auth_id=1,
            campaign_id="c2",
            action="START",
            payload={},
            reason=None,
            performed_by="me",
        )

    assert exc.value.code == "GMVMAX_PRODUCT_OCCUPIED"
    assert called is False
    assert db.committed is False


async def test_apply_custom_pass(monkeypatch):
    campaign = _FakeCampaign(
        promotion_type=PromotionTypeEnum.PRODUCT,
        raw_json={"product_specific_type": "CUSTOMIZED_PRODUCTS"},
    )
    monkeypatch.setattr(service, "_ensure_campaign", lambda *_a, **_k: campaign)
    monkeypatch.setattr(service, "get_gmvmax_client_for_account", lambda *a, **k: _FakeGMVClient())
    monkeypatch.setattr(service, "get_item_group_ids_for_campaign", lambda *_a, **_k: ["g1", "g2"])
    ttb_client = _FakeTTBClient(
        products=[
            {"item_group_id": "g1", "gmv_max_ads_status": "UNOCCUPIED", "status": "AVAILABLE"},
            {"item_group_id": "g2", "gmv_max_ads_status": "UNOCCUPIED", "status": "AVAILABLE"},
        ]
    )
    monkeypatch.setattr(service, "build_ttb_client", lambda *_a, **_k: ttb_client)

    called = False

    async def _fake_apply(*_a, **_k):  # noqa: ANN001
        nonlocal called
        called = True
        return types.SimpleNamespace(id="log")

    monkeypatch.setattr(service, "svc_apply_campaign_action", _fake_apply)

    db = _FakeSession()
    campaign_result, log_entry = await service.apply_campaign_action(
        db,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        campaign_id="c3",
        action="ENABLE",
        payload={},
        reason=None,
        performed_by="me",
    )

    assert called is True
    assert db.committed is True
    assert campaign_result is campaign
    assert getattr(log_entry, "id", None) == "log"


async def test_apply_non_product(monkeypatch):
    campaign = _FakeCampaign(promotion_type=PromotionTypeEnum.LIVE, raw_json={"product_specific_type": "ALL"})
    monkeypatch.setattr(service, "_ensure_campaign", lambda *_a, **_k: campaign)
    monkeypatch.setattr(service, "get_gmvmax_client_for_account", lambda *a, **k: _FakeGMVClient())

    called = False

    async def _fake_apply(*_a, **_k):  # noqa: ANN001
        nonlocal called
        called = True
        return types.SimpleNamespace(id="log")

    monkeypatch.setattr(service, "svc_apply_campaign_action", _fake_apply)

    db = _FakeSession()
    await service.apply_campaign_action(
        db,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        campaign_id="c4",
        action="ENABLE",
        payload={},
        reason=None,
        performed_by="me",
    )

    assert called is True
    assert db.committed is True
