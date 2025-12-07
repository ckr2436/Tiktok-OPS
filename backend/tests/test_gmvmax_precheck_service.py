import types
import pytest

from app.features.tenants.ttb.gmv_max import service
from app.features.tenants.ttb.gmv_max.schemas import GMVMaxPrecheckRequest
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCustomAnchorVideo,
    GMVMaxIdentity,
    GMVMaxIdentityInfo,
    GMVMaxOccupiedAd,
    GMVMaxOccupiedListData,
    GMVMaxStoreAdUsageCheckData,
    GMVMaxVideo,
    GMVMaxVideoInfo,
)


class _Resp:
    def __init__(self, data=None, request_id: str | None = None):
        self.data = data
        self.request_id = request_id or "req"


class _StoreEntry:
    def __init__(self, **kwargs):
        self._data = kwargs

    def model_dump(self, exclude_none=False):
        return dict(self._data)


class FakeGMVClient:
    def __init__(
        self,
        *,
        store_list=None,
        usage=None,
        occupancy=None,
        identities=None,
        videos=None,
        anchors=None,
    ):
        self._store_list = store_list or []
        self._usage = usage
        self._occupancy = occupancy
        self._identities = identities or []
        self._videos = videos or []
        self._anchors = anchors or []
        self.calls = types.SimpleNamespace(
            store_list=0,
            usage=0,
            occupancy=0,
            identities=0,
            videos=0,
            anchors=0,
        )

    async def gmv_max_store_list(self, request):  # noqa: ANN001
        self.calls.store_list += 1
        return _Resp(types.SimpleNamespace(store_list=self._store_list), request_id="store")

    async def gmv_max_store_shop_ad_usage_check(self, request):  # noqa: ANN001
        self.calls.usage += 1
        return _Resp(self._usage, request_id="usage")

    async def gmv_max_occupied_custom_shop_ads_list(self, request):  # noqa: ANN001
        self.calls.occupancy += 1
        self.last_occupancy_request = request
        return _Resp(self._occupancy, request_id="occupancy")

    async def gmv_max_identity_get(self, request):  # noqa: ANN001
        self.calls.identities += 1
        return _Resp(types.SimpleNamespace(identity_list=self._identities), request_id="identity")

    async def gmv_max_video_get(self, request):  # noqa: ANN001
        self.calls.videos += 1
        return _Resp(types.SimpleNamespace(list=self._videos), request_id="video")

    async def gmv_max_custom_anchor_video_list_get(self, request):  # noqa: ANN001
        self.calls.anchors += 1
        return _Resp(
            types.SimpleNamespace(custom_anchor_video_list=self._anchors), request_id="anchor"
        )

    async def aclose(self):  # noqa: D401
        """No-op close."""
        return None


class FakeTTBClient:
    def __init__(self, products=None, recommend=None):
        self._products = products or []
        self._recommend = recommend or {}
        self.calls = types.SimpleNamespace(iter_products=0, recommend=0)

    async def iter_products(self, **kwargs):  # noqa: ANN001
        self.calls.iter_products += 1
        for product in self._products:
            yield product

    async def recommend_gmvmax_bid(self, **kwargs):  # noqa: ANN001
        self.calls.recommend += 1
        return dict(self._recommend)

    async def aclose(self):  # noqa: D401
        """No-op close."""
        return None


pytestmark = pytest.mark.anyio


async def test_precheck_basic_flow(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    store_entry = _StoreEntry(store_id="1", is_gmv_max_available=True)
    usage = GMVMaxStoreAdUsageCheckData(
        promote_all_products_allowed=True, is_running_custom_shop_ads=False
    )
    identities = [
        GMVMaxIdentity(
            identity_info=GMVMaxIdentityInfo(identity_id="i1", identity_type="USER", user_name="u"),
            product_gmv_max_available=True,
        ),
        GMVMaxIdentity(
            identity_info=GMVMaxIdentityInfo(identity_id="i2"),
            product_gmv_max_available=False,
        ),
    ]
    videos = [GMVMaxVideo(item_id="vid_item", video_info=GMVMaxVideoInfo(video_id="v1", preview_url="p"))]
    anchors = [GMVMaxCustomAnchorVideo(custom_anchor_video_id="c1", video_info=GMVMaxVideoInfo(video_id="av1"))]
    products = [
        {"status": "AVAILABLE", "item_group_id": "g1", "gmv_max_ads_status": "UNOCCUPIED"},
        {"status": "AVAILABLE", "item_group_id": "g2", "gmv_max_ads_status": "OCCUPIED"},
    ]

    gmv_client = FakeGMVClient(
        store_list=[store_entry], usage=usage, identities=identities, videos=videos, anchors=anchors
    )
    ttb_client = FakeTTBClient(products=products, recommend={"roas_bid": 1.5, "budget": 100})

    payload = GMVMaxPrecheckRequest(
        store_id="1",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
    )

    result = await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    assert result.is_gmv_max_available is True
    assert result.needs_exclusive_auth is False
    assert result.promote_all_products_allowed is True
    assert result.has_running_custom_shop_ads is False
    assert result.unoccupied_item_group_ids == ["g1"]
    assert result.occupied_item_group_ids == ["g2"]
    assert [i.identity_id for i in result.available_identities] == ["i1"]
    assert result.available_videos[0].video_id == "v1"
    assert result.available_custom_anchor_videos[0].video_id == "av1"
    assert result.recommended_roas_bid == 1.5
    assert result.recommended_budget == 100


async def test_precheck_needs_exclusive_auth(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)
    store_entry = _StoreEntry(
        store_id="1",
        is_gmv_max_available=True,
        exclusive_authorized_advertiser_info={"advertiser_id": "other"},
    )

    gmv_client = FakeGMVClient(
        store_list=[store_entry],
        usage=GMVMaxStoreAdUsageCheckData(
            promote_all_products_allowed=True, is_running_custom_shop_ads=False
        ),
    )
    ttb_client = FakeTTBClient()
    payload = GMVMaxPrecheckRequest(
        store_id="1",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
    )

    result = await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    assert result.needs_exclusive_auth is True
    assert result.current_authorized_advertiser_id == "other"


async def test_precheck_occupied_shop_ads(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)
    store_entry = _StoreEntry(store_id="1", is_gmv_max_available=True)
    usage = GMVMaxStoreAdUsageCheckData(promote_all_products_allowed=False, is_running_custom_shop_ads=True)
    occupancy = GMVMaxOccupiedListData(
        occupied_custom_shop_ads=[
            GMVMaxOccupiedAd(ad_id="a1", campaign_id="c1", item_group_id="g1")
        ]
    )
    gmv_client = FakeGMVClient(
        store_list=[store_entry], usage=usage, occupancy=occupancy, identities=[], videos=[]
    )
    ttb_client = FakeTTBClient()
    payload = GMVMaxPrecheckRequest(
        store_id="1",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
    )

    result = await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    assert result.promote_all_products_allowed is False
    assert result.has_running_custom_shop_ads is True
    assert result.occupied_custom_shop_ads[0].ad_id == "a1"


async def test_precheck_short_circuits_when_store_unavailable(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    gmv_client = FakeGMVClient(store_list=[])
    ttb_client = FakeTTBClient()

    payload = GMVMaxPrecheckRequest(
        store_id="missing",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
    )

    result = await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    assert result.is_gmv_max_available is False
    assert result.needs_exclusive_auth is False
    assert result.promote_all_products_allowed is False
    assert result.has_running_custom_shop_ads is False
    assert result.unoccupied_item_group_ids == []
    assert result.occupied_item_group_ids == []
    assert result.available_identities == []
    assert result.available_videos == []
    assert result.available_custom_anchor_videos == []
    assert result.recommended_roas_bid is None
    assert result.recommended_budget is None
    assert result.store_usage is None
    assert result.identities == []
    assert result.occupancy is None
    assert result.request_ids == {
        "store_usage": None,
        "identities": None,
        "occupancy": None,
    }

    assert gmv_client.calls.store_list == 1
    assert gmv_client.calls.usage == 0
    assert gmv_client.calls.occupancy == 0
    assert gmv_client.calls.identities == 0
    assert gmv_client.calls.videos == 0
    assert gmv_client.calls.anchors == 0
    assert ttb_client.calls.iter_products == 0
    assert ttb_client.calls.recommend == 0


async def test_precheck_short_circuits_when_store_disabled(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    store_entry = _StoreEntry(store_id="1", is_gmv_max_available=False)
    gmv_client = FakeGMVClient(store_list=[store_entry])
    ttb_client = FakeTTBClient()

    payload = GMVMaxPrecheckRequest(
        store_id="1",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
    )

    result = await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    assert result.is_gmv_max_available is False
    assert result.needs_exclusive_auth is False
    assert result.promote_all_products_allowed is False
    assert result.has_running_custom_shop_ads is False
    assert gmv_client.calls.store_list == 1
    assert gmv_client.calls.usage == 0
    assert gmv_client.calls.occupancy == 0
    assert gmv_client.calls.identities == 0
    assert gmv_client.calls.videos == 0
    assert gmv_client.calls.anchors == 0
    assert ttb_client.calls.iter_products == 0
    assert ttb_client.calls.recommend == 0


async def test_precheck_occupancy_request_only_spu_ids(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    store_entry = _StoreEntry(store_id="1", is_gmv_max_available=True)
    usage = GMVMaxStoreAdUsageCheckData(promote_all_products_allowed=True, is_running_custom_shop_ads=True)
    occupancy = GMVMaxOccupiedListData(occupied_custom_shop_ads=[])
    gmv_client = FakeGMVClient(store_list=[store_entry], usage=usage, occupancy=occupancy)
    ttb_client = FakeTTBClient(products=[])

    payload = GMVMaxPrecheckRequest(
        store_id="1",
        store_authorized_bc_id="bc",
        advertiser_id="adv",
        item_group_ids=["spu1", "spu2"],
        identity_id="identity_should_not_be_used",
    )

    await service.gmvmax_precheck(
        None,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=gmv_client,
        ttb_client=ttb_client,
    )

    req = getattr(gmv_client, "last_occupancy_request", None)
    assert req is not None
    assert getattr(req, "occupied_asset_type", None) == "SPU"
    assert getattr(req, "asset_ids", None) == ["spu1", "spu2"]
