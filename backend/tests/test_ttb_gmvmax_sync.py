from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.data.models.gmv_restructured import GmvCampaign
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaign,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignListData,
    GMVMaxResponse,
    PageInfo,
)
from app.services.ttb_gmvmax import sync_gmvmax_campaigns


class _DummyCampaignClient:
    def __init__(self, responses: dict[str, list[GMVMaxCampaignListData]]):
        self._responses = responses
        self._counters: dict[str, int] = {key: 0 for key in responses}
        self.requests: list[GMVMaxCampaignGetRequest] = []
        # 记录每次请求的 primary_status，方便验证参数
        self.primary_status_calls: list[str | None] = []

    async def gmv_max_campaign_get(
        self, request: GMVMaxCampaignGetRequest
    ) -> GMVMaxResponse[GMVMaxCampaignListData]:
        self.requests.append(request)
        primary = request.filtering.primary_status if request.filtering else None
        self.primary_status_calls.append(primary)
        idx = self._counters.get(primary or "STATUS_NOT_DELETE", 0)
        payloads = self._responses.get(primary or "STATUS_NOT_DELETE", [])
        response = payloads[idx] if idx < len(payloads) else GMVMaxCampaignListData()
        self._counters[primary or "STATUS_NOT_DELETE"] = idx + 1
        return GMVMaxResponse[GMVMaxCampaignListData](
            code=0, message="OK", data=response
        )

    async def get_gmvmax_campaign_info(self, advertiser_id: str, campaign_id: str):
        return SimpleNamespace()


def test_sync_gmvmax_campaigns_filters_by_primary_status_only(db_session):
    not_deleted_page = GMVMaxCampaignListData(
        list=[
            GMVMaxCampaign(
                campaign_id="camp-A",
                operation_status="ENABLE",
                secondary_status="CAMPAIGN_STATUS_ENABLE",
                store_id="store-1",
            ),
            GMVMaxCampaign(
                campaign_id="camp-B",
                operation_status="DISABLE",
                secondary_status="CAMPAIGN_STATUS_DISABLE",
                store_id="store-1",
            ),
        ],
        page_info=PageInfo(page=1, total_page=1),
    )

    deleted_page = GMVMaxCampaignListData(
        list=[
            GMVMaxCampaign(
                campaign_id="camp-C",
                operation_status="ENABLE",
                secondary_status="CAMPAIGN_STATUS_DELETE",
                store_id="store-1",
            )
        ],
        page_info=PageInfo(page=1, total_page=1),
    )

    client = _DummyCampaignClient(
        {
            "STATUS_NOT_DELETE": [not_deleted_page],
            "STATUS_DELETE": [deleted_page],
        }
    )

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
        )
    )
    db_session.commit()

    # 第一轮必须“不传 primary_status”（None），第二轮显式传 STATUS_DELETE。
    # 不能把 STATUS_NOT_DELETE 当成枚举值传给 TikTok。
    assert client.primary_status_calls == [None, "STATUS_DELETE"]

    campaigns = db_session.query(GmvCampaign).order_by(GmvCampaign.campaign_id).all()
    assert len(campaigns) == 3

    by_id = {row.campaign_id: row for row in campaigns}

    assert by_id["camp-A"].operation_status == "ENABLE"
    assert by_id["camp-A"].secondary_status == "CAMPAIGN_STATUS_ENABLE"
    assert by_id["camp-A"].lifecycle_status == "ACTIVE"
    assert by_id["camp-A"].is_deleted is False

    assert by_id["camp-B"].operation_status == "DISABLE"
    assert by_id["camp-B"].secondary_status == "CAMPAIGN_STATUS_DISABLE"
    assert by_id["camp-B"].lifecycle_status == "INACTIVE"
    assert by_id["camp-B"].is_deleted is False

    assert by_id["camp-C"].operation_status == "ENABLE"
    assert by_id["camp-C"].secondary_status == "CAMPAIGN_STATUS_DELETE"
    assert by_id["camp-C"].lifecycle_status == "DELETED"
    assert by_id["camp-C"].is_deleted is True

    assert all(row.lifecycle_status != "DELETED" for row in campaigns if row.secondary_status != "CAMPAIGN_STATUS_DELETE")


def test_bound_store_resolves_from_page_context(db_session, monkeypatch):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: "store-1")

    page = GMVMaxCampaignListData(
        list=[GMVMaxCampaign(campaign_id="camp-page-context")],
        page_info=PageInfo(page=1, total_page=1),
        links={"advertiser_to_stores": {"adv": ["store-1"]}},
        stores=[{"store_id": "store-1"}],
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": []})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
        )
    )
    db_session.commit()

    campaigns = db_session.query(GmvCampaign).all()
    assert len(campaigns) == 1
    assert campaigns[0].campaign_id == "camp-page-context"
    assert campaigns[0].store_id == "store-1"


def test_bound_store_missing_everywhere_skips_campaign(db_session, monkeypatch):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: "store-1")

    page = GMVMaxCampaignListData(
        list=[GMVMaxCampaign(campaign_id="camp-no-store")],
        page_info=PageInfo(page=1, total_page=1),
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": []})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
        )
    )
    db_session.commit()

    assert db_session.query(GmvCampaign).count() == 0


def test_bound_store_resolves_mismatch_and_skips(db_session, monkeypatch):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: "store-1")

    page = GMVMaxCampaignListData(
        list=[GMVMaxCampaign(campaign_id="camp-other-store")],
        page_info=PageInfo(page=1, total_page=1),
        links={"advertiser_to_stores": {"adv": ["store-2"]}},
        stores=[{"store_id": "store-2"}],
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": []})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
        )
    )
    db_session.commit()

    assert db_session.query(GmvCampaign).count() == 0


def test_sync_gmvmax_campaigns_forces_bound_store_filter(db_session, monkeypatch):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: "store-1")

    page = GMVMaxCampaignListData(
        list=[],
        page_info=PageInfo(page=1, total_page=1),
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": [page]})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            store_ids=["store-x"],
        )
    )

    assert len(client.requests) == 2
    assert all(req.filtering.store_ids == ["store-1"] for req in client.requests)


def test_cross_store_campaigns_filtered_out(db_session, monkeypatch):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: "store-1")

    page = GMVMaxCampaignListData(
        list=[
            GMVMaxCampaign(campaign_id="camp-bound", store_id="store-1"),
            GMVMaxCampaign(campaign_id="camp-other", store_id="store-2"),
        ],
        page_info=PageInfo(page=1, total_page=1),
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": []})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
        )
    )
    db_session.commit()

    campaigns = db_session.query(GmvCampaign).order_by(GmvCampaign.campaign_id).all()
    assert [row.campaign_id for row in campaigns] == ["camp-bound"]
    assert campaigns[0].store_id == "store-1"


def test_sync_gmvmax_campaigns_without_bound_store_respects_filters(
    db_session, monkeypatch
):
    monkeypatch.setattr("app.services.ttb_gmvmax._get_bound_store_id", lambda *_, **__: None)

    page = GMVMaxCampaignListData(
        list=[],
        page_info=PageInfo(page=1, total_page=1),
    )

    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": [page]})

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            store_ids=["store-2"],
        )
    )

    assert len(client.requests) == 2
    assert all(req.filtering.store_ids == ["store-2"] for req in client.requests)
