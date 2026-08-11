from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.features.tenants.ttb.gmv_max.control import set_manual_pause_override
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.gmvmax.services.report_pagination import NumberedPaginationStalledError
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignListData,
    GMVMaxResponse,
    PageInfo,
)
from app.services.ttb_gmvmax import sync_gmvmax_campaigns
from app.services import ttb_gmvmax


class _DummyCampaignClient:
    def __init__(self, responses: dict[str, list[GMVMaxCampaignListData]]):
        self._responses = responses
        self._counters: dict[str, int] = {key: 0 for key in responses}
        self.requests: list[GMVMaxCampaignGetRequest] = []
        # 记录每次请求的 primary_status，方便验证参数
        self.primary_status_calls: list[str | None] = []
        self.info_calls: list[str] = []

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

    async def gmv_max_campaign_info(self, request):  # noqa: ANN001
        self.info_calls.append(str(request.campaign_id))
        return GMVMaxResponse[GMVMaxCampaignInfoData](
            code=0,
            message="OK",
            data=GMVMaxCampaignInfoData(
                advertiser_id=request.advertiser_id,
                campaign_id=request.campaign_id,
            ),
        )


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

    campaigns = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .order_by(GmvmaxProductCampaignCatalog.campaign_id)
        .all()
    )
    assert len(campaigns) == 3

    by_id = {row.campaign_id: row for row in campaigns}

    assert by_id["camp-A"].operation_status == "ENABLE"
    assert by_id["camp-A"].secondary_status == "CAMPAIGN_STATUS_ENABLE"

    assert by_id["camp-B"].operation_status == "DISABLE"
    assert by_id["camp-B"].secondary_status == "CAMPAIGN_STATUS_DISABLE"

    assert by_id["camp-C"].operation_status == "ENABLE"
    assert by_id["camp-C"].secondary_status == "CAMPAIGN_STATUS_DELETE"


def test_realtime_catalog_sync_can_skip_per_campaign_info_enrichment(db_session):
    original_detail = {
        "campaign_id": "camp-realtime",
        "item_group_ids": ["product-1"],
        "product_specific_type": "CUSTOMIZED_PRODUCTS",
    }
    db_session.add(
        GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            campaign_id="camp-realtime",
            store_id="store-1",
            operation_status="DISABLE",
            secondary_status="CAMPAIGN_STATUS_DISABLE",
            shopping_ads_type="PRODUCT",
            detail_raw_json=original_detail,
        )
    )
    db_session.commit()
    client = _DummyCampaignClient(
        {
            "STATUS_NOT_DELETE": [
                GMVMaxCampaignListData(
                    list=[
                        GMVMaxCampaign(
                            campaign_id="camp-realtime",
                            operation_status="ENABLE",
                            secondary_status="CAMPAIGN_STATUS_ENABLE",
                            store_id="store-1",
                        )
                    ],
                    page_info=PageInfo(page=1, total_page=1),
                )
            ],
            "STATUS_DELETE": [
                GMVMaxCampaignListData(
                    list=[],
                    page_info=PageInfo(page=1, total_page=1),
                )
            ],
        }
    )

    result = asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            include_campaign_details=False,
        )
    )

    assert result["synced"] == 1
    assert client.info_calls == []
    row = db_session.query(GmvmaxProductCampaignCatalog).one()
    assert row.operation_status == "ENABLE"
    assert row.detail_raw_json == original_detail


def test_two_newer_official_syncs_resume_guard_after_external_enable(
    db_session, monkeypatch
):
    pause_at = datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc)
    override = set_manual_pause_override(
        db_session,
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv",
        store_id="store-1",
        campaign_id="camp-external-enable",
        actor="operator@example.com",
    )
    override.override_started_at = pause_at.replace(tzinfo=None)
    db_session.commit()

    monkeypatch.setattr(
        ttb_gmvmax,
        "_get_bound_store_id",
        lambda *_, **__: "store-1",
    )
    clock = {"now": pause_at + timedelta(minutes=3)}
    monkeypatch.setattr(
        ttb_gmvmax,
        "catalog_observation_now",
        lambda: clock["now"].replace(tzinfo=None),
    )

    def _client():
        return _DummyCampaignClient(
            {
                "STATUS_NOT_DELETE": [
                    GMVMaxCampaignListData(
                        list=[
                            GMVMaxCampaign(
                                campaign_id="camp-external-enable",
                                operation_status="ENABLE",
                                secondary_status="CAMPAIGN_STATUS_ENABLE",
                                store_id="store-1",
                                modify_time=(pause_at + timedelta(minutes=1)).isoformat(),
                            )
                        ],
                        page_info=PageInfo(page=1, total_page=1),
                    )
                ],
                "STATUS_DELETE": [
                    GMVMaxCampaignListData(
                        list=[],
                        page_info=PageInfo(page=1, total_page=1),
                    )
                ],
            }
        )

    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            _client(),
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            include_campaign_details=False,
        )
    )
    db_session.commit()
    assert override.active is True
    assert override.external_enable_observation_count == 1

    clock["now"] = pause_at + timedelta(minutes=4)
    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            _client(),
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            include_campaign_details=False,
        )
    )
    db_session.commit()
    assert override.active is False
    assert override.resolution_type == "EXTERNAL_ENABLE_CONFIRMED"


def test_sync_campaigns_probes_when_effective_page_size_is_omitted(db_session):
    active_pages = [
        GMVMaxCampaignListData(
            list=[GMVMaxCampaign(campaign_id="camp-page-1", store_id="store-1")],
            page_info=PageInfo(page=1, total_number=2),
        ),
        GMVMaxCampaignListData(
            list=[GMVMaxCampaign(campaign_id="camp-page-2", store_id="store-1")],
            page_info=PageInfo(page=2, total_number=2),
        ),
        GMVMaxCampaignListData(
            list=[],
            page_info=PageInfo(page=3, total_number=2),
        ),
    ]
    deleted_page = GMVMaxCampaignListData(
        list=[],
        page_info=PageInfo(page=1, total_page=1, total_number=0),
    )
    client = _DummyCampaignClient(
        {
            "STATUS_NOT_DELETE": active_pages,
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

    active_requests = [
        request.page
        for request in client.requests
        if request.filtering.primary_status is None
    ]
    assert active_requests == [1, 2, 3]
    assert {
        row.campaign_id
        for row in db_session.query(GmvmaxProductCampaignCatalog).all()
    } == {"camp-page-1", "camp-page-2"}


def test_sync_campaigns_rejects_partial_cross_page_overlap(db_session):
    active_pages = [
        GMVMaxCampaignListData(
            list=[
                GMVMaxCampaign(campaign_id="camp-1", store_id="store-1"),
                GMVMaxCampaign(campaign_id="camp-2", store_id="store-1"),
            ],
            page_info=PageInfo(
                page=1,
                page_size=100,
                total_number=3,
                total_page=2,
            ),
        ),
        GMVMaxCampaignListData(
            list=[
                GMVMaxCampaign(campaign_id="camp-2", store_id="store-1"),
                GMVMaxCampaign(campaign_id="camp-3", store_id="store-1"),
            ],
            page_info=PageInfo(
                page=2,
                page_size=100,
                total_number=3,
                total_page=2,
            ),
        ),
    ]
    client = _DummyCampaignClient(
        {
            "STATUS_NOT_DELETE": active_pages,
            "STATUS_DELETE": [],
        }
    )

    with pytest.raises(NumberedPaginationStalledError, match="stable item key"):
        asyncio.run(
            sync_gmvmax_campaigns(
                db_session,
                client,
                workspace_id=1,
                auth_id=1,
                advertiser_id="adv",
            )
        )


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

    campaigns = db_session.query(GmvmaxProductCampaignCatalog).all()
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

    assert db_session.query(GmvmaxProductCampaignCatalog).count() == 0


def test_bound_store_accepts_explicit_zero_placeholder_from_campaign_info(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.ttb_gmvmax._get_bound_store_id",
        lambda *_, **__: "store-1",
    )

    page = GMVMaxCampaignListData(
        list=[GMVMaxCampaign(campaign_id="camp-placeholder")],
        page_info=PageInfo(page=1, total_page=1),
    )
    client = _DummyCampaignClient({"STATUS_NOT_DELETE": [page], "STATUS_DELETE": []})

    async def _placeholder_campaign_info(request):  # noqa: ANN001
        return GMVMaxResponse[GMVMaxCampaignInfoData](
            code=0,
            message="OK",
            data=GMVMaxCampaignInfoData(
                advertiser_id=request.advertiser_id,
                campaign_id=request.campaign_id,
                store_id="0",
            ),
        )

    client.gmv_max_campaign_info = _placeholder_campaign_info

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

    campaign = db_session.query(GmvmaxProductCampaignCatalog).one()
    assert campaign.campaign_id == "camp-placeholder"
    assert campaign.store_id == "store-1"


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

    assert db_session.query(GmvmaxProductCampaignCatalog).count() == 0


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

    campaigns = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .order_by(GmvmaxProductCampaignCatalog.campaign_id)
        .all()
    )
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
