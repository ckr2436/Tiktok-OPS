import types
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.features.tenants.ttb.gmv_max import service
from app.features.tenants.ttb.gmv_max.schemas import (
    CreateCampaignRequest,
    GMVMaxCustomAnchorVideo,
    GMVMaxIdentityRequest,
    GMVMaxItemVideo,
    GMVMaxVideoInfo,
)

pytestmark = pytest.mark.anyio


class _FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeClient:
    async def aclose(self):  # noqa: D401
        """No-op close."""
        return None


def _noop(*_args, **_kwargs):
    return None


async def test_create_campaign_all_auto_selection(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    received = {}

    async def _fake_create(db, **kwargs):  # noqa: ANN001
        received.update(kwargs)
        return types.SimpleNamespace(campaign_id="c1")

    monkeypatch.setattr(service, "svc_create_campaign", _fake_create)

    payload = CreateCampaignRequest(
        campaign_name="cmp",
        store_id="1",
        budget=100,
        roas_bid=1.2,
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time=datetime.now(),
        product_specific_type="ALL",
        product_video_specific_type="AUTO_SELECTION",
    )

    db = _FakeSession()
    result = await service.create_gmvmax_campaign(
        db,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=_FakeClient(),
    )

    assert isinstance(result, types.SimpleNamespace)
    body = received.get("body")
    assert body.shopping_ads_type == "PRODUCT"
    assert body.optimization_goal == "VALUE"
    assert body.deep_bid_type == "VO_MIN_ROAS"
    assert body.product_specific_type == "ALL"
    assert body.item_group_ids is None
    assert body.product_video_specific_type == "AUTO_SELECTION"
    assert getattr(body, "item_list", None) is None
    assert db.committed is True


async def test_create_campaign_customized_products(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    received = {}

    async def _fake_create(db, **kwargs):  # noqa: ANN001
        received.update(kwargs)
        return types.SimpleNamespace(campaign_id="c2")

    monkeypatch.setattr(service, "svc_create_campaign", _fake_create)

    payload = CreateCampaignRequest(
        campaign_name="cmp2",
        store_id="1",
        budget=200,
        roas_bid=2.1,
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time=datetime.now(),
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["g1", "g2"],
        product_video_specific_type="AUTO_SELECTION",
    )

    db = _FakeSession()
    await service.create_gmvmax_campaign(
        db,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=_FakeClient(),
    )

    body = received.get("body")
    assert body.product_specific_type == "CUSTOMIZED_PRODUCTS"
    assert body.item_group_ids == ["g1", "g2"]
    assert getattr(body, "item_list", None) is None


async def test_create_campaign_custom_selection(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)

    received = {}

    async def _fake_create(db, **kwargs):  # noqa: ANN001
        received.update(kwargs)
        return types.SimpleNamespace(campaign_id="c3")

    monkeypatch.setattr(service, "svc_create_campaign", _fake_create)

    identity = GMVMaxIdentityRequest(identity_id="id1", identity_type="TT_USER")
    video_info = GMVMaxVideoInfo(video_id="v1")
    payload = CreateCampaignRequest(
        campaign_name="cmp3",
        store_id="1",
        budget=300,
        roas_bid=3.1,
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time=datetime.now(),
        product_specific_type="ALL",
        product_video_specific_type="CUSTOM_SELECTION",
        item_list=[
            GMVMaxItemVideo(
                identity_info=identity,
                item_id="vid1",
                spu_id_list=["s1"],
                video_info=video_info,
            )
        ],
        custom_anchor_video_list=[
            GMVMaxCustomAnchorVideo(
                identity_info=identity,
                item_id="vid1",
                spu_id_list=["s1"],
                video_info=video_info,
            )
        ],
    )

    db = _FakeSession()
    await service.create_gmvmax_campaign(
        db,
        workspace_id=1,
        provider="ttb",
        auth_id=1,
        advertiser_id="adv",
        payload=payload,
        client=_FakeClient(),
    )

    body = received.get("body")
    assert body.product_video_specific_type == "CUSTOM_SELECTION"
    assert body.item_list is not None and len(body.item_list) == 1
    assert body.custom_anchor_video_list is not None and len(body.custom_anchor_video_list) == 1


async def test_create_campaign_invalid_customized_products(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)
    monkeypatch.setattr(service, "svc_create_campaign", _noop)

    payload = CreateCampaignRequest(
        campaign_name="cmp4",
        store_id="1",
        budget=150,
        roas_bid=1.5,
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time=datetime.now(),
        product_specific_type="CUSTOMIZED_PRODUCTS",
    )

    with pytest.raises(HTTPException):
        await service.create_gmvmax_campaign(
            _FakeSession(),
            workspace_id=1,
            provider="ttb",
            auth_id=1,
            advertiser_id="adv",
            payload=payload,
            client=_FakeClient(),
        )
