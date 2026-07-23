from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.data.models.gmv_restructured import (
    GmvCampaignLivestream,
    GmvDurationMetricsHourly,
    PromotionTypeEnum,
)
from app.gmvmax.services.report_pagination import (
    NumberedPaginationInvariantError,
    NumberedPaginationStalledError,
    ReportPaginationState,
    iter_numbered_pages,
    numbered_page_has_more,
    report_page_has_more,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxDataset,
    GMVMaxIdentity,
    GMVMaxIdentityInfo,
    GMVMaxOccupiedAd,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxOccupiedListData,
    GMVMaxReportGetRequest,
    GMVMaxReportData,
    GMVMaxResponse,
    GMVMaxSessionListData,
    GMVMaxVideo,
    GMVMaxVideoGetRequest,
    PageInfo,
    TikTokBusinessGMVMaxClient,
    fetch_all_occupied_custom_shop_ads,
)
from app.services.gmvmax_creative_assets import (
    build_gmvmax_identity_filter,
    iter_gmvmax_video_entries,
)
from app.services.ttb_gmvmax import (
    _fetch_chunked_live_report_rows,
    sync_gmvmax_duration_metrics_hourly,
    sync_gmvmax_livestream_metrics_hourly,
)
from app.tasks.ttb_gmvmax_tasks import _resolve_identity_occupied_asset_type


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_numbered_iterator_rejects_empty_nonterminal_page():
    async def fetch(_page: int):
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[],
                page_info=PageInfo(page=1, page_size=50, total_page=2),
            )
        )

    with pytest.raises(NumberedPaginationInvariantError):
        async for _ in iter_numbered_pages(
            fetch,
            rows_from_data=lambda data: data.list,
            requested_page_size=50,
        ):
            pass


def test_report_page_rule_rejects_empty_nonterminal_page():
    data = SimpleNamespace(
        page_info=PageInfo(page=1, page_size=50, total_page=2)
    )
    with pytest.raises(NumberedPaginationInvariantError):
        report_page_has_more(data, current_page=1, rows=[])


@pytest.mark.parametrize("dimensions", [None, {}])
def test_report_state_requires_a_canonical_dimension_key(dimensions):
    state = ReportPaginationState(require_dimensions=True)
    row = SimpleNamespace(dimensions=dimensions)

    with pytest.raises(NumberedPaginationInvariantError):
        state.validate(page=1, rows=[row])


def test_numbered_page_rule_rejects_rows_with_zero_total_pages():
    data = SimpleNamespace(
        page_info=SimpleNamespace(
            page=1,
            page_size=50,
            total_page=0,
            total_number=0,
        )
    )

    with pytest.raises(NumberedPaginationInvariantError, match="total_page=0"):
        numbered_page_has_more(
            data,
            current_page=1,
            rows=[object()],
            requested_page_size=50,
        )


def test_numbered_page_rule_accepts_normal_nonempty_last_page():
    data = SimpleNamespace(
        page_info=PageInfo(
            page=2,
            page_size=50,
            total_page=2,
            total_number=51,
            has_more=False,
        )
    )

    assert (
        numbered_page_has_more(
            data,
            current_page=2,
            rows=[object()],
            requested_page_size=50,
            items_seen=51,
        )
        is False
    )


@pytest.mark.parametrize(
    "page_info",
    [
        {"page": True},
        {"page": 0},
        {"page": 1, "page_size": 0},
        {"page": 1, "total_page": -1},
        {"page": 1, "total_page": "invalid"},
        {"page": 1, "total_number": -1},
        {"page": 1, "has_more": 2},
        {"page": 1, "has_more": "maybe"},
    ],
)
def test_numbered_page_rule_rejects_invalid_metadata(page_info):
    with pytest.raises(NumberedPaginationInvariantError):
        numbered_page_has_more(
            {"page_info": page_info},
            current_page=1,
            rows=[],
            requested_page_size=50,
        )


def test_numbered_page_rule_rejects_impossible_counts_and_page_bounds():
    with pytest.raises(NumberedPaginationInvariantError, match="more rows"):
        numbered_page_has_more(
            {
                "page_info": {
                    "page": 1,
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 1,
                }
            },
            current_page=1,
            rows=[object(), object()],
            requested_page_size=50,
            items_seen=2,
        )

    with pytest.raises(NumberedPaginationInvariantError, match="beyond total_page"):
        numbered_page_has_more(
            {
                "page_info": {
                    "page": 2,
                    "page_size": 50,
                    "total_page": 1,
                }
            },
            current_page=2,
            rows=[],
            requested_page_size=50,
        )


def test_any_positive_continuation_signal_wins_conflicting_metadata():
    data = SimpleNamespace(
        page_info=PageInfo(
            page=1,
            page_size=50,
            total_number=50,
            total_page=1,
            has_more=True,
            has_next=False,
        )
    )
    assert numbered_page_has_more(
        data,
        current_page=1,
        rows=[object()],
        requested_page_size=50,
    )
    assert report_page_has_more(data, current_page=1, rows=[object()])


@pytest.mark.parametrize("response_page", [1, 3])
def test_numbered_page_rule_rejects_response_page_behind_or_ahead(
    response_page,
):
    data = SimpleNamespace(
        page_info=PageInfo(
            page=response_page,
            page_size=50,
            total_page=2,
        )
    )
    with pytest.raises(NumberedPaginationStalledError):
        numbered_page_has_more(
            data,
            current_page=2,
            rows=[object()],
            requested_page_size=50,
        )


@pytest.mark.parametrize("response_page", [1, 3])
def test_report_page_rule_rejects_response_page_behind_or_ahead(
    response_page,
):
    data = SimpleNamespace(
        page_info=PageInfo(
            page=response_page,
            page_size=50,
            total_page=2,
        )
    )
    with pytest.raises(NumberedPaginationStalledError):
        report_page_has_more(data, current_page=2, rows=[object()])


async def test_numbered_iterator_rejects_repeated_nonterminal_page():
    async def fetch(page: int):
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[{"id": "same"}],
                page_info=PageInfo(page=page, page_size=1, total_page=3),
            )
        )

    with pytest.raises(NumberedPaginationStalledError):
        async for _ in iter_numbered_pages(
            fetch,
            rows_from_data=lambda data: data.list,
            requested_page_size=1,
        ):
            pass


async def test_numbered_iterator_rejects_repeated_terminal_page():
    async def fetch(page: int):
        return SimpleNamespace(
            data=SimpleNamespace(
                list=[{"id": "same"}],
                page_info=PageInfo(
                    page=page,
                    page_size=1,
                    total_page=2,
                    total_number=2,
                    has_more=page == 1,
                ),
            )
        )

    with pytest.raises(NumberedPaginationStalledError):
        async for _ in iter_numbered_pages(
            fetch,
            rows_from_data=lambda data: data.list,
            requested_page_size=1,
        ):
            pass


async def test_numbered_iterator_does_not_infer_response_size_from_request():
    """A server-side page-size clamp must not silently truncate the result."""

    calls: list[int] = []

    async def fetch(page: int):
        calls.append(page)
        rows_by_page = {
            1: [{"id": f"row-{index}"} for index in range(50)],
            2: [{"id": "row-50"}],
            3: [],
        }
        return SimpleNamespace(
            data=SimpleNamespace(
                list=rows_by_page[page],
                # TikTok occasionally omits the effective response page_size.
                # total_number alone cannot prove the requested 1000-row page
                # was the final page because the service may have clamped it.
                page_info=SimpleNamespace(page=page, total_number=51),
            )
        )

    rows = []
    async for fetched_page in iter_numbered_pages(
        fetch,
        rows_from_data=lambda data: data.list,
        requested_page_size=1000,
        probe_on_missing_metadata=True,
    ):
        rows.extend(fetched_page.rows)

    assert len(rows) == 51
    assert calls == [1, 2, 3]


async def test_video_get_keeps_auth_code_path_when_identity_list_is_empty():
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_video_get(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                data=SimpleNamespace(
                    item_list=[GMVMaxVideo(item_id="auth-code-item")],
                    page_info=PageInfo(
                        page=request.page,
                        page_size=request.page_size,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    client = Client()
    rows = [
        row
        async for row in iter_gmvmax_video_entries(
            client,
            advertiser_id="adv",
            store_id="store",
            store_authorized_bc_id="bc",
            identities=[],
        )
    ]
    assert [row.item_id for row in rows] == ["auth-code-item"]
    assert client.requests[0].identity_list is None
    assert client.requests[0].need_auth_code_video is True


async def test_video_get_rejects_partial_overlap_inside_one_filter_chunk():
    class Client:
        async def gmv_max_video_get(self, request):
            rows = (
                [
                    GMVMaxVideo(item_id="shared", spu_id_list=["product-1"]),
                    GMVMaxVideo(item_id="first-only", spu_id_list=["product-1"]),
                ]
                if request.page == 1
                else [
                    GMVMaxVideo(item_id="shared", spu_id_list=["product-2"]),
                    GMVMaxVideo(item_id="second-only", spu_id_list=["product-2"]),
                ]
            )
            return SimpleNamespace(
                data=SimpleNamespace(
                    item_list=rows,
                    page_info=PageInfo(
                        page=request.page,
                        page_size=request.page_size,
                        total_page=2,
                        total_number=3,
                    ),
                )
            )

    with pytest.raises(NumberedPaginationStalledError, match="stable item key"):
        async for _ in iter_gmvmax_video_entries(
            Client(),
            advertiser_id="adv",
            store_id="store",
            store_authorized_bc_id="bc",
            identities=[],
            item_group_ids=["product-1"],
        ):
            pass


async def test_video_get_merges_spu_associations_across_legal_filter_chunks():
    class Client:
        async def gmv_max_video_get(self, request):
            return SimpleNamespace(
                data=SimpleNamespace(
                    item_list=[
                        GMVMaxVideo(
                            item_id="shared",
                            spu_id_list=list(request.spu_id_list or []),
                        )
                    ],
                    page_info=PageInfo(
                        page=request.page,
                        page_size=request.page_size,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    product_ids = [f"product-{index}" for index in range(51)]
    rows = [
        row
        async for row in iter_gmvmax_video_entries(
            Client(),
            advertiser_id="adv",
            store_id="store",
            store_authorized_bc_id="bc",
            identities=[],
            item_group_ids=product_ids,
        )
    ]

    assert len(rows) == 1
    assert rows[0].item_id == "shared"
    assert rows[0].spu_id_list == product_ids


def test_identity_filter_preserves_conditional_official_fields():
    entries = [
        GMVMaxIdentity.model_validate(
            {
                "identity_info": {
                    "identity_id": "bc-user",
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_shop_id": "shop-auth",
                },
                "identity_authorized_bc_id": "bc-auth",
            }
        ),
        GMVMaxIdentity(
            identity_info=GMVMaxIdentityInfo(
                identity_id="shop-user",
                identity_type="TTS_TT",
            )
        ),
    ]
    assert build_gmvmax_identity_filter(entries, store_id="store") == [
        {
            "identity_id": "bc-user",
            "identity_type": "BC_AUTH_TT",
            "identity_authorized_bc_id": "bc-auth",
            "identity_authorized_shop_id": "shop-auth",
        },
        {
            "identity_id": "shop-user",
            "identity_type": "TTS_TT",
            "store_id": "store",
        },
    ]


def test_official_request_models_reject_over_limit_payloads():
    with pytest.raises(ValidationError):
        GMVMaxVideoGetRequest(
            advertiser_id="a",
            store_id="s",
            store_authorized_bc_id="b",
            identity_list=[{"identity_id": str(i)} for i in range(21)],
        )
    with pytest.raises(ValidationError):
        GMVMaxCampaignGetRequest(
            advertiser_id="a",
            filtering=GMVMaxCampaignFiltering(
                gmv_max_promotion_types=["PRODUCT"],
                store_ids=[str(i) for i in range(11)],
            ),
        )
    with pytest.raises(ValidationError):
        GMVMaxReportGetRequest(
            advertiser_id="a",
            store_ids=["s1", "s2"],
            start_date="2026-01-01",
            end_date="2026-01-01",
            metrics=["cost"],
            dimensions=["campaign_id"],
        )
    with pytest.raises(ValidationError):
        GMVMaxOccupiedCustomShopAdsListRequest(
            advertiser_id="a",
            store_id="s",
            occupied_asset_type="UNKNOWN",
            asset_ids=["asset"],
        )
    with pytest.raises(ValidationError):
        GMVMaxVideoGetRequest(
            advertiser_id="a",
            store_id="s",
            store_authorized_bc_id="b",
            sort_order="DESC",
        )


def test_session_model_normalizes_official_and_legacy_keys():
    official = GMVMaxSessionListData.model_validate(
        {"session_list": [{"session_id": "official"}]}
    )
    legacy = GMVMaxSessionListData.model_validate(
        {"list": [{"session_id": "legacy"}]}
    )
    assert official.list[0].session_id == "official"
    assert legacy.session_list[0].session_id == "legacy"


def test_identity_occupancy_type_is_derived_from_identity_list():
    data = SimpleNamespace(
        identity_list=[
            GMVMaxIdentity(
                identity_info=GMVMaxIdentityInfo(
                    identity_id="identity",
                    identity_type="BC_AUTH_TT",
                )
            )
        ]
    )
    assert (
        _resolve_identity_occupied_asset_type(
            data,
            identity_id="identity",
            requested_type=None,
        )
        == "IDENTITY_BC_AUTH_TT"
    )


def test_report_total_metrics_is_canonical_with_summary_compatibility():
    official = GMVMaxReportData.model_validate({"total_metrics": {"cost": "1"}})
    legacy = GMVMaxReportData.model_validate({"summary": {"cost": "2"}})
    assert official.summary == {"cost": "1"}
    assert legacy.total_metrics == {"cost": "2"}


async def test_report_filters_are_emitted_once_inside_filtering_json():
    import json

    client = TikTokBusinessGMVMaxClient(access_token="test")
    try:
        params = client._build_report_get_params(
            GMVMaxReportGetRequest(
                advertiser_id="adv",
                store_ids=["store"],
                start_date="2026-01-01",
                end_date="2026-01-01",
                metrics=["cost"],
                dimensions=["campaign_id"],
                campaign_ids=["campaign"],
                item_group_ids=["product"],
                room_ids=["room"],
                search_word="needle",
            )
        )
    finally:
        await client.aclose()
    filtering = json.loads(params["filtering"])
    assert filtering["campaign_ids"] == ["campaign"]
    assert filtering["item_group_ids"] == ["product"]
    assert filtering["room_ids"] == ["room"]
    assert filtering["search_word"] == "needle"
    assert "campaign_ids" not in params
    assert "item_group_ids" not in params
    assert "room_ids" not in params


async def test_occupancy_batch_calls_official_endpoint_one_id_at_a_time():
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_occupied_custom_shop_ads_list(self, request):
            self.requests.append(request)
            asset_id = request.asset_ids[0]
            return GMVMaxResponse(
                code=0,
                message="OK",
                request_id=asset_id,
                data=GMVMaxOccupiedListData(
                    occupied_custom_shop_ads=[
                        GMVMaxOccupiedAd(ad_id=f"ad-{asset_id}")
                    ]
                ),
            )

    client = Client()
    response = await fetch_all_occupied_custom_shop_ads(
        client,
        advertiser_id="adv",
        store_id="store",
        occupied_asset_type="SPU",
        asset_ids=["one", "two"],
    )
    assert [request.asset_ids for request in client.requests] == [["one"], ["two"]]
    assert len(response.data.occupied_custom_shop_ads) == 2
    assert [
        item.item_group_id
        for item in response.data.occupied_custom_shop_ads
    ] == ["one", "two"]


@pytest.mark.parametrize(
    "dataset,dimensions",
    [
        (
            GMVMaxDataset.LIVE_LIVESTREAM,
            ["campaign_id", "room_id", "stat_time_hour"],
        ),
        (
            GMVMaxDataset.LIVE_DURATION,
            ["campaign_id", "duration", "stat_time_hour"],
        ),
    ],
)
async def test_live_report_fetch_chunks_dates_ids_and_all_pages(
    dataset,
    dimensions,
):
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_report_get(self, request):
            self.requests.append(request)
            row_dimensions = {
                "campaign_id": request.campaign_ids[0],
                "stat_time_hour": f"{request.start_date} {request.page - 1:02d}:00:00",
            }
            if dataset is GMVMaxDataset.LIVE_LIVESTREAM:
                row_dimensions["room_id"] = request.room_ids[0]
            else:
                row_dimensions["duration"] = f"d-{request.page}"
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[
                        {
                            "dimensions": row_dimensions,
                            "metrics": {"cost": str(request.page)},
                        }
                    ],
                    page_info=PageInfo(
                        page=request.page,
                        page_size=1000,
                        total_page=2,
                        total_number=2,
                    ),
                )
            )

    client = Client()
    rows = await _fetch_chunked_live_report_rows(
        client,
        dataset=dataset,
        advertiser_id="adv",
        store_id="store",
        start_date="2026-01-01",
        end_date="2026-01-02",
        campaign_ids=[f"c{i}" for i in range(101)],
        room_ids=[f"r{i}" for i in range(101)],
        metrics=["cost"],
        dimensions=dimensions,
        max_window_days=1,
    )
    assert len(client.requests) == 16
    assert len(rows) == 16
    assert all(len(request.campaign_ids) <= 100 for request in client.requests)
    assert all(len(request.room_ids) <= 100 for request in client.requests)
    assert {request.page for request in client.requests} == {1, 2}


async def test_live_report_rejects_duplicate_dimensions_within_one_request_chunk():
    class Client:
        async def gmv_max_report_get(self, request):
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[
                        {
                            "dimensions": {
                                "campaign_id": "campaign-1",
                                "room_id": "room-1",
                                "stat_time_hour": "2026-01-01 00:00:00",
                            },
                            "metrics": {"cost": str(request.page)},
                        }
                    ],
                    page_info=PageInfo(
                        page=request.page,
                        page_size=1000,
                        total_page=2,
                        total_number=2,
                    ),
                )
            )

    with pytest.raises(NumberedPaginationStalledError, match="dimension key"):
        await _fetch_chunked_live_report_rows(
            Client(),
            dataset=GMVMaxDataset.LIVE_LIVESTREAM,
            advertiser_id="adv",
            store_id="store",
            start_date="2026-01-01",
            end_date="2026-01-01",
            campaign_ids=["campaign-1"],
            room_ids=["room-1"],
            metrics=["cost"],
            dimensions=["campaign_id", "room_id", "stat_time_hour"],
            max_window_days=1,
        )


@pytest.mark.parametrize("escaped_dimension", ["campaign_id", "room_id"])
async def test_live_report_rejects_rows_that_escape_filter_chunks(
    escaped_dimension,
):
    class Client:
        async def gmv_max_report_get(self, request):
            dimensions = {
                "campaign_id": "campaign-1",
                "room_id": "room-1",
                "stat_time_hour": "2026-01-01 00:00:00",
            }
            dimensions[escaped_dimension] = f"escaped-{escaped_dimension}"
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[{"dimensions": dimensions, "metrics": {"cost": "1"}}],
                    page_info=PageInfo(
                        page=1,
                        page_size=1000,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    with pytest.raises(RuntimeError, match="escaped its .* filter chunk"):
        await _fetch_chunked_live_report_rows(
            Client(),
            dataset=GMVMaxDataset.LIVE_LIVESTREAM,
            advertiser_id="adv",
            store_id="store",
            start_date="2026-01-01",
            end_date="2026-01-01",
            campaign_ids=["campaign-1"],
            room_ids=["room-1"],
            metrics=["cost"],
            dimensions=["campaign_id", "room_id", "stat_time_hour"],
            max_window_days=1,
        )


async def test_livestream_report_can_discover_rooms_from_campaign_only():
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_report_get(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[
                        {
                            "dimensions": {
                                "campaign_id": request.campaign_ids[0],
                                "room_id": "discovered-room",
                                "stat_time_hour": "2026-01-01 00:00:00",
                            },
                            "metrics": {"cost": "1"},
                        }
                    ],
                    page_info=PageInfo(
                        page=1,
                        page_size=1000,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    client = Client()
    rows = await _fetch_chunked_live_report_rows(
        client,
        dataset=GMVMaxDataset.LIVE_LIVESTREAM,
        advertiser_id="adv",
        store_id="store",
        start_date="2026-01-01",
        end_date="2026-01-01",
        campaign_ids=["campaign-1"],
        room_ids=[],
        metrics=["cost"],
        dimensions=["campaign_id", "room_id", "stat_time_hour"],
        max_window_days=1,
    )

    assert len(client.requests) == 1
    assert client.requests[0].room_ids is None
    assert len(rows) == 1
    assert rows[0][0]["room_id"] == "discovered-room"


async def test_livestream_discovery_is_available_to_duration_in_same_session(
    db_session,
):
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_report_get(self, request):
            self.requests.append(request)
            if "room_id" in list(request.dimensions or ()):
                assert request.room_ids is None
                row_dimensions = {
                    "campaign_id": "campaign-1",
                    "room_id": "discovered-room",
                    "stat_time_hour": "2026-01-01 00:00:00",
                }
            else:
                assert request.room_ids == ["discovered-room"]
                row_dimensions = {
                    "campaign_id": "campaign-1",
                    "duration": "0-10",
                    "stat_time_hour": "2026-01-01 00:00:00",
                }
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[
                        {
                            "dimensions": row_dimensions,
                            "metrics": {"cost": "1", "orders": "1"},
                        }
                    ],
                    page_info=PageInfo(
                        page=1,
                        page_size=1000,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    client = Client()
    campaign = SimpleNamespace(campaign_id="campaign-1", store_id="store-1")
    common = {
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv",
        "campaign": campaign,
        "start_date": "2026-01-01",
        "end_date": "2026-01-01",
    }

    discovered = await sync_gmvmax_livestream_metrics_hourly(
        db_session,
        client,
        **common,
    )
    duration = await sync_gmvmax_duration_metrics_hourly(
        db_session,
        client,
        **common,
    )

    relation = db_session.query(GmvCampaignLivestream).one()
    assert discovered == {"synced_rows": 1, "discovered_rooms": 1}
    assert relation.campaign_id == "campaign-1"
    assert relation.room_id == "discovered-room"
    assert relation.promotion_type is PromotionTypeEnum.LIVE
    assert duration == {"synced_rows": 1}
    assert db_session.query(GmvDurationMetricsHourly).count() == 1


async def test_duration_only_sync_discovers_rooms_before_first_report(
    db_session,
):
    class Client:
        def __init__(self):
            self.requests = []

        async def gmv_max_report_get(self, request):
            self.requests.append(request)
            if "room_id" in list(request.dimensions or ()):
                assert request.room_ids is None
                dimensions = {
                    "campaign_id": "campaign-duration-only",
                    "room_id": "duration-only-room",
                    "stat_time_hour": "2026-01-01 00:00:00",
                }
            else:
                assert request.room_ids == ["duration-only-room"]
                dimensions = {
                    "campaign_id": "campaign-duration-only",
                    "duration": "0-10",
                    "stat_time_hour": "2026-01-01 00:00:00",
                }
            return SimpleNamespace(
                data=SimpleNamespace(
                    list=[{"dimensions": dimensions, "metrics": {"cost": "1"}}],
                    page_info=PageInfo(
                        page=1,
                        page_size=1000,
                        total_page=1,
                        total_number=1,
                    ),
                )
            )

    client = Client()
    result = await sync_gmvmax_duration_metrics_hourly(
        db_session,
        client,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        campaign=SimpleNamespace(
            campaign_id="campaign-duration-only",
            store_id="store-1",
        ),
        start_date="2026-01-01",
        end_date="2026-01-01",
    )

    assert result == {"synced_rows": 1}
    assert len(client.requests) == 2
    relation = db_session.query(GmvCampaignLivestream).one()
    assert relation.room_id == "duration-only-room"
    assert db_session.query(GmvDurationMetricsHourly).count() == 1
