from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError

from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxBidRecommendRequest,
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignCreateRequest,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxCreationCustomAnchorVideoListGetRequest,
    GMVMaxExclusiveAuthorizationCreateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxCampaignReportRequest,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxDataset,
    GMVMaxReportTimeRange,
    GMVMaxResponse,
    GMVMaxSessionCreateBody,
    GMVMaxSessionCreateRequest,
    GMVMaxSessionListRequest,
    GMVMaxSessionSettings,
    GMVMaxSessionUpdateBody,
    GMVMaxSessionUpdateRequest,
    GMVMaxStoreAdUsageCheckRequest,
    GMVMaxVideoGetRequest,
    build_gmv_max_report_request,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_api import TTBApiError, TTBBusinessError


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_database() -> None:  # pragma: no cover - isolate unit tests from DB side effects
    yield


@dataclass(slots=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes


async def _build_client(handler) -> TikTokBusinessGMVMaxClient:
    transport = httpx.MockTransport(handler)
    client = TikTokBusinessGMVMaxClient(access_token="token")
    original_headers = client._client.headers
    timeout = client._timeout
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        timeout=timeout, headers=original_headers, transport=transport
    )
    return client


def _extract_query(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    return {key: values if len(values) > 1 else values[0] for key, values in parse_qs(parsed.query).items()}


def _assert_headers(headers: Mapping[str, str], *, method: str) -> None:
    assert headers["Access-Token"] == "token"
    if method == "POST":
        assert headers["Content-Type"].startswith("application/json")
    else:
        # TikTok's GET endpoint contracts only require Access-Token.  httpx
        # correctly omits an entity Content-Type when no request body exists.
        assert "Content-Type" not in headers


def _official_create_body(**updates: Any) -> GMVMaxCampaignCreateBody:
    payload: dict[str, Any] = {
        "request_id": "123456789",
        "store_id": "s",
        "store_authorized_bc_id": "bc",
        "shopping_ads_type": "PRODUCT",
        "optimization_goal": "VALUE",
        "deep_bid_type": "VO_MIN_ROAS",
        "campaign_name": "name",
        "budget": 100.0,
        "roas_bid": 1.5,
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": "2026-07-18 00:00:00",
        "product_video_specific_type": "AUTO_SELECTION",
    }
    payload.update(updates)
    return GMVMaxCampaignCreateBody(**payload)


def test_store_usage_request_rejects_nonofficial_bc_parameter() -> None:
    with pytest.raises(ValidationError):
        GMVMaxStoreAdUsageCheckRequest(
            advertiser_id="1",
            store_id="shop",
            store_authorized_bc_id="must-not-be-sent",
        )


def test_bid_recommendation_enforces_official_conditional_scope() -> None:
    with pytest.raises(ValidationError):
        GMVMaxBidRecommendRequest(
            advertiser_id="1",
            store_id="shop",
            shopping_ads_type="LIVE",
            optimization_goal="VALUE",
        )
    with pytest.raises(ValidationError):
        GMVMaxBidRecommendRequest(
            advertiser_id="1",
            store_id="shop",
            shopping_ads_type="PRODUCT",
            optimization_goal="VALUE",
            identity_id="live-only",
        )


def _wrap_handler(expected_method: str, expected_path: str, *, response_body: Mapping[str, Any]) -> Any:
    recorded: List[RecordedRequest] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(
            RecordedRequest(
                method=request.method,
                url=str(request.url),
                headers=request.headers,
                body=request.content,
            )
        )
        assert request.method == expected_method
        assert urlparse(str(request.url)).path.endswith(expected_path)
        _assert_headers(request.headers, method=expected_method)
        return httpx.Response(200, json=response_body)

    return handler, recorded


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_obj, method_name, expected_path, expected_query",
    [
        (
            GMVMaxCampaignGetRequest(
                advertiser_id="123",
                filtering=GMVMaxCampaignFiltering(gmv_max_promotion_types=["PRODUCT"]),
                page=1,
                page_size=20,
            ),
            "gmv_max_campaign_get",
            "/open_api/v1.3/gmv_max/campaign/get/",
            {
                "advertiser_id": "123",
                "page": "1",
                "page_size": "20",
                "filtering": json.dumps(
                    {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX"]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
        (
            GMVMaxCampaignGetRequest(
                advertiser_id="456",
                filtering=GMVMaxCampaignFiltering(
                    gmv_max_promotion_types=["PRODUCT"],
                    **{"store_id": "store-123"},
                ),
            ),
            "gmv_max_campaign_get",
                "/open_api/v1.3/gmv_max/campaign/get/",
                {
                    "advertiser_id": "456",
                    "filtering": json.dumps(
                        {
                            "gmv_max_promotion_types": ["PRODUCT_GMV_MAX"],
                            "store_ids": ["store-123"],
                        },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
        (
            GMVMaxCampaignGetRequest(
                advertiser_id="789",
                filtering=GMVMaxCampaignFiltering(
                    gmv_max_promotion_types=["LIVE"],
                    store_ids=["one", "one", "two"],
                ),
            ),
            "gmv_max_campaign_get",
                "/open_api/v1.3/gmv_max/campaign/get/",
                {
                    "advertiser_id": "789",
                    "filtering": json.dumps(
                        {
                            "gmv_max_promotion_types": ["LIVE_GMV_MAX"],
                            "store_ids": ["one", "two"],
                        },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
        (
            GMVMaxCampaignInfoRequest(advertiser_id="123", campaign_id="c1"),
            "gmv_max_campaign_info",
            "/open_api/v1.3/campaign/gmv_max/info/",
            {"advertiser_id": "123", "campaign_id": "c1"},
        ),
        (
            GMVMaxSessionListRequest(advertiser_id="1", campaign_id="2"),
            "gmv_max_session_list",
            "/open_api/v1.3/campaign/gmv_max/session/list/",
            {"advertiser_id": "1", "campaign_id": "2"},
        ),
        (
            GMVMaxStoreAdUsageCheckRequest(
                advertiser_id="1",
                store_id="shop",
            ),
            "gmv_max_store_shop_ad_usage_check",
            "/open_api/v1.3/gmv_max/store/shop_ad_usage_check/",
            {
                "advertiser_id": "1",
                "store_id": "shop",
            },
        ),
        (
            GMVMaxIdentityGetRequest(
                advertiser_id="1", store_id="shop", store_authorized_bc_id="bc"
            ),
            "gmv_max_identity_get",
            "/open_api/v1.3/gmv_max/identity/get/",
            {"advertiser_id": "1", "store_id": "shop", "store_authorized_bc_id": "bc"},
        ),
        (
            GMVMaxOccupiedCustomShopAdsListRequest(
                advertiser_id="1",
                store_id="shop",
                occupied_asset_type="SPU",
                asset_ids=["spu1"],
            ),
            "gmv_max_occupied_custom_shop_ads_list",
            "/open_api/v1.3/gmv_max/occupied_custom_shop_ads/list/",
            {
                "advertiser_id": "1",
                "store_id": "shop",
                "occupied_asset_type": "SPU",
                "asset_ids": json.dumps(["spu1"], ensure_ascii=False),
            },
        ),
        (
            GMVMaxVideoGetRequest(
                advertiser_id="1",
                store_id="shop",
                store_authorized_bc_id="bc",
                spu_id_list=["spu1"],
                page=1,
            ),
            "gmv_max_video_get",
            "/open_api/v1.3/gmv_max/video/get/",
            {
                "advertiser_id": "1",
                "store_id": "shop",
                "store_authorized_bc_id": "bc",
                "spu_id_list": json.dumps(["spu1"], ensure_ascii=False),
                "page": "1",
            },
        ),
        (
            GMVMaxExclusiveAuthorizationGetRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_exclusive_authorization_get",
            "/open_api/v1.3/gmv_max/exclusive_authorization/get/",
            {"advertiser_id": "1", "store_id": "s", "store_authorized_bc_id": "bc"},
        ),
        (
            GMVMaxBidRecommendRequest(
                advertiser_id="1",
                store_id="s",
                shopping_ads_type="PRODUCT",
                optimization_goal="VALUE",
                item_group_ids=["ig"],
            ),
            "gmv_max_bid_recommend",
            "/open_api/v1.3/gmv_max/bid/recommend/",
            {
                "advertiser_id": "1",
                "store_id": "s",
                "shopping_ads_type": "PRODUCT",
                "optimization_goal": "VALUE",
                "item_group_ids": json.dumps(["ig"], ensure_ascii=False),
            },
        ),
        (
            GMVMaxBidRecommendRequest(
                advertiser_id="1",
                store_id="s",
                shopping_ads_type="PRODUCT",
                optimization_goal="VALUE",
            ),
            "gmv_max_bid_recommend",
            "/open_api/v1.3/gmv_max/bid/recommend/",
            {
                "advertiser_id": "1",
                "store_id": "s",
                "shopping_ads_type": "PRODUCT",
                "optimization_goal": "VALUE",
            },
        ),
        (
            GMVMaxBidRecommendRequest(
                advertiser_id="1",
                store_id="s",
                shopping_ads_type="LIVE",
                optimization_goal="VALUE",
                identity_id="identity-1",
            ),
            "gmv_max_bid_recommend",
            "/open_api/v1.3/gmv_max/bid/recommend/",
            {
                "advertiser_id": "1",
                "store_id": "s",
                "shopping_ads_type": "LIVE",
                "optimization_goal": "VALUE",
                "identity_id": "identity-1",
            },
        ),
        (
            GMVMaxReportGetRequest(
                advertiser_id="1",
                store_ids=["s"],
                start_date="2024-01-01",
                end_date="2024-01-02",
                metrics=["metric"],
                dimensions=["dimension"],
                filtering=GMVMaxReportFiltering(gmv_max_promotion_types=["PRODUCT"]),
                page_size=50,
            ),
            "gmv_max_report_get",
            "/open_api/v1.3/gmv_max/report/get/",
            {
                "advertiser_id": "1",
                "store_ids": json.dumps(["s"], ensure_ascii=False),
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "metrics": json.dumps(["metric"], ensure_ascii=False),
                "dimensions": json.dumps(["dimension"], ensure_ascii=False),
                "page_size": "50",
                "filtering": json.dumps(
                    {"gmv_max_promotion_types": ["PRODUCT"]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
    ],
)
async def test_gmvmax_client_get_requests(request_obj, method_name, expected_path, expected_query):
    handler, recorded = _wrap_handler(
        "GET",
        expected_path,
        response_body={"code": 0, "message": "OK", "data": {}},
    )
    client = await _build_client(handler)
    method = getattr(client, method_name)
    response: GMVMaxResponse[Any] = await method(request_obj)
    await client.aclose()
    assert response.code == 0
    assert response.message == "OK"
    assert recorded, "request not captured"
    qs = _extract_query(recorded[0].url)
    for key, expected in expected_query.items():
        assert key in qs
        assert qs[key] == expected


@pytest.mark.anyio
async def test_gmvmax_campaign_report_uses_get_endpoint():
    handler, recorded = _wrap_handler(
        "GET",
        "/open_api/v1.3/gmv_max/report/get/",
        response_body={"code": 0, "message": "OK", "data": {"report": {"list": []}}},
    )
    client = await _build_client(handler)
    request_obj = GMVMaxCampaignReportRequest(
        advertiser_id="1",
        campaign_ids=["cmp"],
        metrics=["cost"],
        dimensions=["campaign_id", "stat_time_day"],
        time_range=GMVMaxReportTimeRange(start_time="2024-01-01", end_time="2024-01-02"),
        filtering=GMVMaxReportFiltering(
            store_ids=["store"], gmv_max_promotion_types=["PRODUCT_GMV_MAX"]
        ),
        page=1,
        page_size=50,
    )

    await client.gmv_max_campaign_report(request_obj)
    await client.aclose()

    assert recorded, "request not captured"
    request = recorded[0]
    assert request.method == "GET"
    parsed = urlparse(request.url)
    assert parsed.path.endswith("/open_api/v1.3/gmv_max/report/get/")
    query = _extract_query(request.url)
    assert query["advertiser_id"] == "1"
    assert query["start_date"] == "2024-01-01"
    assert query["end_date"] == "2024-01-02"
    assert json.loads(query["dimensions"]) == ["campaign_id", "stat_time_day"]
    assert json.loads(query["metrics"]) == ["cost"]
    filtering = json.loads(query["filtering"])
    assert filtering["gmv_max_promotion_types"] == ["PRODUCT"]


@pytest.mark.anyio
async def test_gmvmax_campaign_create_never_retries_an_ambiguous_transport_error():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("remote outcome is unknown", request=request)

    client = await _build_client(handler)
    request = GMVMaxCampaignCreateRequest(
        advertiser_id="1",
        body=_official_create_body(
            campaign_name="one-logical-create",
        ),
    )

    with pytest.raises(httpx.ReadTimeout):
        await client.gmv_max_campaign_create(request)
    await client.aclose()

    assert attempts == 1


def test_build_report_request_strips_sentinel_campaign_ids():
    request = build_gmv_max_report_request(
        dataset=GMVMaxDataset.OVERVIEW,
        advertiser_id="adv",
        store_ids=["store"],
        start_date="2024-01-01",
        end_date="2024-01-02",
        metrics=["spend"],
        campaign_ids=["all", "1850141052477537"],
    )

    assert request.campaign_ids == ["1850141052477537"]
    assert request.filtering
    assert request.filtering.campaign_ids == ["1850141052477537"]


def test_build_report_request_rejects_only_sentinel_ids():
    with pytest.raises(ValueError):
        build_gmv_max_report_request(
            dataset=GMVMaxDataset.PRODUCT_PRODUCT,
            advertiser_id="adv",
            store_ids=["store"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            metrics=["spend"],
            campaign_ids=["all"],
        )


def test_report_request_enforces_official_hourly_window() -> None:
    with pytest.raises(ValidationError):
        GMVMaxReportGetRequest(
            advertiser_id="1",
            store_ids=["shop"],
            start_date="2026-07-17",
            end_date="2026-07-18",
            metrics=["cost"],
            dimensions=["campaign_id", "stat_time_hour"],
        )


def test_campaign_update_rejects_nonofficial_start_time() -> None:
    with pytest.raises(ValidationError):
        GMVMaxCampaignUpdateBody(
            campaign_id="campaign",
            schedule_start_time="2026-07-18 00:00:00",
        )


def test_session_update_requires_official_session_object() -> None:
    with pytest.raises(ValidationError):
        GMVMaxSessionUpdateBody(
            campaign_id="campaign",
            session_id="session",
            store_id="shop",
        )


@pytest.mark.parametrize(
    "dataset,item_group_ids,room_ids",
    [
        (GMVMaxDataset.PRODUCT_PRODUCT, None, None),
        (GMVMaxDataset.PRODUCT_DURATION, ["item"], None),
        (GMVMaxDataset.LIVE_LIVESTREAM, None, None),
        (GMVMaxDataset.LIVE_DURATION, None, ["room"]),
    ],
)
def test_non_campaign_report_datasets_omit_promotion_type_filter(
    dataset,
    item_group_ids,
    room_ids,
):
    request = build_gmv_max_report_request(
        dataset=dataset,
        advertiser_id="adv",
        store_ids=["store"],
        start_date="2024-01-01",
        end_date="2024-01-02",
        metrics=["cost"],
        campaign_ids=["campaign"],
        item_group_ids=item_group_ids,
        room_ids=room_ids,
    )

    assert request.gmv_max_promotion_types is None
    assert request.filtering is not None
    assert request.filtering.gmv_max_promotion_types is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_obj, method_name, expected_path, expected_query, expected_body",
    [
        (
            GMVMaxCampaignCreateRequest(
                advertiser_id="1",
                body=_official_create_body(),
            ),
            "gmv_max_campaign_create",
            "/open_api/v1.3/campaign/gmv_max/create/",
            {"advertiser_id": "1"},
                {
                    "advertiser_id": "1",
                    "request_id": "123456789",
                    "store_id": "s",
                    "store_authorized_bc_id": "bc",
                    "shopping_ads_type": "PRODUCT",
                    "optimization_goal": "VALUE",
                    "deep_bid_type": "VO_MIN_ROAS",
                    "campaign_name": "name",
                    "budget": 100.0,
                    "roas_bid": 1.5,
                    "schedule_type": "SCHEDULE_FROM_NOW",
                    "schedule_start_time": "2026-07-18 00:00:00",
                    "product_video_specific_type": "AUTO_SELECTION",
                },
        ),
        (
            GMVMaxCampaignUpdateRequest(
                advertiser_id="1",
                body=GMVMaxCampaignUpdateBody(campaign_id="c", campaign_name="updated"),
            ),
            "gmv_max_campaign_update",
            "/open_api/v1.3/campaign/gmv_max/update/",
            {"advertiser_id": "1"},
            {"advertiser_id": "1", "campaign_id": "c", "campaign_name": "updated"},
        ),
        (
            CampaignStatusUpdateRequest(
                advertiser_id="1",
                campaign_ids=["c"],
                operation_status="DISABLE",
            ),
            "campaign_status_update",
            "/open_api/v1.3/campaign/status/update/",
            {"advertiser_id": "1"},
            {
                "advertiser_id": "1",
                "campaign_ids": ["c"],
                "operation_status": "DISABLE",
            },
        ),
        (
            GMVMaxSessionCreateRequest(
                advertiser_id="1",
                body=GMVMaxSessionCreateBody(
                    campaign_id="c",
                    store_id="s",
                    session=GMVMaxSessionSettings(
                        bid_type="NO_BID",
                        product_list=[{"spu_id": "spu"}],
                        budget=10.0,
                    ),
                ),
            ),
            "gmv_max_session_create",
            "/open_api/v1.3/campaign/gmv_max/session/create/",
            {"advertiser_id": "1"},
            {
                "advertiser_id": "1",
                "campaign_id": "c",
                "store_id": "s",
                "session": {
                    "bid_type": "NO_BID",
                    "product_list": [{"spu_id": "spu"}],
                    "budget": 10.0,
                },
            },
        ),
        (
            GMVMaxSessionUpdateRequest(
                advertiser_id="1",
                body=GMVMaxSessionUpdateBody(
                    campaign_id="c",
                    session_id="sid",
                    store_id="s",
                    session=GMVMaxSessionSettings(schedule_type="SCHEDULE_FROM_NOW"),
                ),
            ),
            "gmv_max_session_update",
            "/open_api/v1.3/campaign/gmv_max/session/update/",
            {"advertiser_id": "1"},
            {
                "advertiser_id": "1",
                "campaign_id": "c",
                "session_id": "sid",
                "store_id": "s",
                "session": {"schedule_type": "SCHEDULE_FROM_NOW"},
            },
        ),
        (
            GMVMaxExclusiveAuthorizationCreateRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_exclusive_authorization_create",
            "/open_api/v1.3/gmv_max/exclusive_authorization/create/",
            {"advertiser_id": "1"},
            {
                "advertiser_id": "1",
                "store_id": "s",
                "store_authorized_bc_id": "bc",
            },
        ),
        (
            GMVMaxCreationCustomAnchorVideoListGetRequest(
                advertiser_id="1",
                store_id="s",
                store_authorized_bc_id="bc",
                identity_list=[
                    {"identity_id": "identity", "identity_type": "TT_USER"}
                ],
                page=2,
                page_size=50,
            ),
            "gmv_max_creation_custom_anchor_video_list_get",
            "/open_api/v1.3/gmv_max/creation/custom_anchor_video_list/get/",
            {},
            {
                "advertiser_id": "1",
                "store_id": "s",
                "store_authorized_bc_id": "bc",
                "creative_source": "CUSTOMIZED",
                "identity_list": [
                    {"identity_id": "identity", "identity_type": "TT_USER"}
                ],
                "page": 2,
                "page_size": 50,
            },
        ),
    ],
)
async def test_gmvmax_client_post_requests(
    request_obj, method_name, expected_path, expected_query, expected_body
):
    handler, recorded = _wrap_handler(
        "POST",
        expected_path,
        response_body={"code": 0, "message": "OK", "data": {}},
    )
    client = await _build_client(handler)
    method = getattr(client, method_name)
    response: GMVMaxResponse[Any] = await method(request_obj)
    await client.aclose()
    assert response.code == 0
    qs = _extract_query(recorded[0].url)
    assert qs == expected_query
    body = json.loads(recorded[0].body.decode()) if recorded[0].body else {}
    for key, value in expected_body.items():
        assert body[key] == value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_obj, method_name",
    [
        (
            GMVMaxCampaignGetRequest(
                advertiser_id="1",
                filtering=GMVMaxCampaignFiltering(gmv_max_promotion_types=["PRODUCT"]),
            ),
            "gmv_max_campaign_get",
        ),
        (
            GMVMaxCampaignInfoRequest(advertiser_id="1", campaign_id="c"),
            "gmv_max_campaign_info",
        ),
        (
            GMVMaxCampaignCreateRequest(
                advertiser_id="1",
                body=_official_create_body(),
            ),
            "gmv_max_campaign_create",
        ),
        (
            GMVMaxCampaignUpdateRequest(
                advertiser_id="1",
                body=GMVMaxCampaignUpdateBody(
                    campaign_id="c",
                    campaign_name="updated",
                ),
            ),
            "gmv_max_campaign_update",
        ),
        (
            GMVMaxSessionCreateRequest(
                advertiser_id="1",
                body=GMVMaxSessionCreateBody(
                    campaign_id="c",
                    store_id="s",
                    session=GMVMaxSessionSettings(
                        bid_type="NO_BID",
                        product_list=[{"spu_id": "spu"}],
                        budget=10.0,
                    ),
                ),
            ),
            "gmv_max_session_create",
        ),
        (
            GMVMaxSessionUpdateRequest(
                advertiser_id="1",
                body=GMVMaxSessionUpdateBody(
                    campaign_id="c",
                    session_id="sid",
                    store_id="s",
                    session=GMVMaxSessionSettings(budget=10.0),
                ),
            ),
            "gmv_max_session_update",
        ),
        (
            GMVMaxSessionListRequest(advertiser_id="1", campaign_id="c"),
            "gmv_max_session_list",
        ),
        (
            GMVMaxIdentityGetRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_identity_get",
        ),
        (
            GMVMaxOccupiedCustomShopAdsListRequest(
                advertiser_id="1", store_id="s", occupied_asset_type="SPU", asset_ids=["spu"]
            ),
            "gmv_max_occupied_custom_shop_ads_list",
        ),
        (
            GMVMaxVideoGetRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_video_get",
        ),
        (
            GMVMaxExclusiveAuthorizationGetRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_exclusive_authorization_get",
        ),
        (
            GMVMaxExclusiveAuthorizationCreateRequest(
                advertiser_id="1", store_id="s", store_authorized_bc_id="bc"
            ),
            "gmv_max_exclusive_authorization_create",
        ),
        (
            GMVMaxBidRecommendRequest(
                advertiser_id="1",
                store_id="s",
                shopping_ads_type="PRODUCT",
                optimization_goal="VALUE",
                item_group_ids=["ig"],
            ),
            "gmv_max_bid_recommend",
        ),
        (
            GMVMaxReportGetRequest(
                advertiser_id="1",
                store_ids=["s"],
                start_date="2024-01-01",
                end_date="2024-01-02",
                metrics=["metric"],
                dimensions=["dimension"],
            ),
            "gmv_max_report_get",
        ),
    ],
)
async def test_gmvmax_client_raises_for_business_error(request_obj, method_name):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 10001, "message": "error"})

    client = await _build_client(handler)
    method = getattr(client, method_name)
    with pytest.raises(TTBApiError):
        await method(request_obj)
    await client.aclose()


async def test_gmvmax_client_raises_for_specific_business_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40002, "message": "requires filters"})

    client = await _build_client(handler)
    request_obj = GMVMaxReportGetRequest(
        advertiser_id="1",
        store_ids=["s"],
        start_date="2024-01-01",
        end_date="2024-01-02",
        metrics=["metric"],
        dimensions=["dimension"],
    )

    with pytest.raises(TTBBusinessError):
        await client.gmv_max_report_get(request_obj)
    await client.aclose()
