from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendRequest,
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignCreateRequest,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxCustomAnchorVideoListGetRequest,
    GMVMaxExclusiveAuthorizationCreateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
    GMVMaxSessionCreateBody,
    GMVMaxSessionCreateRequest,
    GMVMaxSessionListRequest,
    GMVMaxSessionSettings,
    GMVMaxSessionUpdateBody,
    GMVMaxSessionUpdateRequest,
    GMVMaxVideoGetRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_api import TTBApiError


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


def _assert_headers(headers: Mapping[str, str]) -> None:
    assert headers["Access-Token"] == "token"
    assert headers["Content-Type"].startswith("application/json")


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
        _assert_headers(request.headers)
        return httpx.Response(200, json=response_body)

    return handler, recorded


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_obj, method_name, expected_path, expected_query",
    [
        (
            GMVMaxCampaignGetRequest(
                advertiser_id="123",
                filtering=GMVMaxCampaignFiltering(gmv_max_promotion_types=["PRODUCT_GMV_MAX"]),
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
            GMVMaxCampaignInfoRequest(advertiser_id="123", campaign_id="c1"),
            "gmv_max_campaign_info",
            "/open_api/v1.3/campaign/gmv_max/info/",
            {"advertiser_id": "123", "campaign_id": "c1"},
        ),
        (
            GMVMaxSessionListRequest(advertiser_id="1", campaign_id="2", page=2),
            "gmv_max_session_list",
            "/open_api/v1.3/campaign/gmv_max/session/list/",
            {"advertiser_id": "1", "campaign_id": "2", "page": "2"},
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
                "asset_ids": "spu1",
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
                "spu_id_list": "spu1",
                "page": "1",
            },
        ),
        (
            GMVMaxCustomAnchorVideoListGetRequest(
                advertiser_id="1", campaign_id="cmp", page_size=10
            ),
            "gmv_max_custom_anchor_video_list_get",
            "/open_api/v1.3/gmv_max/custom_anchor_video_list/get/",
            {"advertiser_id": "1", "campaign_id": "cmp", "page_size": "10"},
        ),
        (
            GMVMaxExclusiveAuthorizationGetRequest(advertiser_id="1", store_id="s"),
            "gmv_max_exclusive_authorization_get",
            "/open_api/v1.3/gmv_max/exclusive_authorization/get/",
            {"advertiser_id": "1", "store_id": "s"},
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
                "item_group_ids": "ig",
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
                filtering=GMVMaxReportFiltering(gmv_max_promotion_types=["PRODUCT_GMV_MAX"]),
                page_size=50,
            ),
            "gmv_max_report_get",
            "/open_api/v1.3/gmv_max/report/get/",
            {
                "advertiser_id": "1",
                "store_ids": "s",
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "metrics": "metric",
                "dimensions": "dimension",
                "page_size": "50",
                "filtering": json.dumps(
                    {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX"]},
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
@pytest.mark.parametrize(
    "request_obj, method_name, expected_path, expected_query, expected_body",
    [
        (
            GMVMaxCampaignCreateRequest(
                advertiser_id="1",
                body=GMVMaxCampaignCreateBody(
                    store_id="s",
                    shopping_ads_type="PRODUCT",
                    optimization_goal="VALUE",
                    campaign_name="name",
                ),
            ),
            "gmv_max_campaign_create",
            "/open_api/v1.3/campaign/gmv_max/create/",
            {"advertiser_id": "1"},
            {
                "store_id": "s",
                "shopping_ads_type": "PRODUCT",
                "optimization_goal": "VALUE",
                "campaign_name": "name",
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
            {"campaign_id": "c", "campaign_name": "updated"},
        ),
        (
            GMVMaxSessionCreateRequest(
                advertiser_id="1",
                body=GMVMaxSessionCreateBody(
                    campaign_id="c",
                    store_id="s",
                    session=GMVMaxSessionSettings(budget=10.0),
                    product_list=[],
                ),
            ),
            "gmv_max_session_create",
            "/open_api/v1.3/campaign/gmv_max/session/create/",
            {"advertiser_id": "1"},
            {
                "campaign_id": "c",
                "store_id": "s",
                "session": {"budget": 10.0},
                "product_list": [],
            },
        ),
        (
            GMVMaxSessionUpdateRequest(
                advertiser_id="1",
                body=GMVMaxSessionUpdateBody(
                    campaign_id="c",
                    session_id="sid",
                    session=GMVMaxSessionSettings(schedule_type="SCHEDULE_FROM_NOW"),
                ),
            ),
            "gmv_max_session_update",
            "/open_api/v1.3/campaign/gmv_max/session/update/",
            {"advertiser_id": "1"},
            {
                "campaign_id": "c",
                "session_id": "sid",
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
            {"store_id": "s", "store_authorized_bc_id": "bc"},
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
    for key, value in expected_query.items():
        assert qs[key] == value
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
                filtering=GMVMaxCampaignFiltering(gmv_max_promotion_types=["PRODUCT_GMV_MAX"]),
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
                body=GMVMaxCampaignCreateBody(
                    store_id="s",
                    shopping_ads_type="PRODUCT",
                    optimization_goal="VALUE",
                    campaign_name="name",
                ),
            ),
            "gmv_max_campaign_create",
        ),
        (
            GMVMaxCampaignUpdateRequest(
                advertiser_id="1",
                body=GMVMaxCampaignUpdateBody(campaign_id="c"),
            ),
            "gmv_max_campaign_update",
        ),
        (
            GMVMaxSessionCreateRequest(
                advertiser_id="1",
                body=GMVMaxSessionCreateBody(
                    campaign_id="c",
                    store_id="s",
                    session=GMVMaxSessionSettings(budget=10.0),
                    product_list=[],
                ),
            ),
            "gmv_max_session_create",
        ),
        (
            GMVMaxSessionUpdateRequest(
                advertiser_id="1",
                body=GMVMaxSessionUpdateBody(campaign_id="c", session_id="sid"),
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
            GMVMaxCustomAnchorVideoListGetRequest(advertiser_id="1"),
            "gmv_max_custom_anchor_video_list_get",
        ),
        (
            GMVMaxExclusiveAuthorizationGetRequest(advertiser_id="1", store_id="s"),
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
