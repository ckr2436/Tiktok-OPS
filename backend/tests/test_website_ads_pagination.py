from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.data.models.website_ads import WebsiteAdsActionLog
from app.features.tenants.ttb.website_ads import router as website_ads_router
from app.features.tenants.ttb.website_ads.router import list_actions
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.providers.tiktok_business.website_ads_pagination import (
    WebsiteAdsPaginationInvariantError,
    WebsiteAdsPaginationLimitError,
    WebsiteAdsPaginationStalledError,
    report_payload_has_complete_pagination,
)


def test_production_website_ads_consumers_never_use_single_page_asset_methods():
    app_root = Path(__file__).resolve().parents[1] / "app"
    forbidden = {"list_pixels", "list_identities", "list_videos"}
    offenders = []
    for path in app_root.rglob("*.py"):
        if path.name == "website_ads_client.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden:
                    offenders.append(f"{path.relative_to(app_root)}:{node.lineno}:{node.func.attr}")
    assert offenders == []


def test_every_production_action_log_write_includes_auth_id():
    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    writes = 0
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr if isinstance(target, ast.Attribute) else ""
            )
            if name != "WebsiteAdsActionLog":
                continue
            writes += 1
            if not any(keyword.arg == "auth_id" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(app_root)}:{node.lineno}")
    assert writes >= 25
    assert offenders == []


def test_action_log_query_is_auth_scoped_and_second_page_is_reachable(
    db_session,
    monkeypatch,
):
    now = datetime(2026, 7, 17, 12, 0, 0)
    db_session.add_all(
        [
            WebsiteAdsActionLog(
                workspace_id=7,
                auth_id=11,
                actor_type="TEST",
                action="PAUSE_AD",
                result="SUCCESS",
                created_at=now + timedelta(seconds=index),
            )
            for index in range(205)
        ]
        + [
            WebsiteAdsActionLog(
                workspace_id=7,
                auth_id=12,
                actor_type="OTHER_AUTH",
                action="PAUSE_AD",
                result="SUCCESS",
                created_at=now + timedelta(hours=1),
            ),
            WebsiteAdsActionLog(
                workspace_id=7,
                auth_id=None,
                actor_type="LEGACY_NULL",
                action="PAUSE_AD",
                result="SUCCESS",
                created_at=now + timedelta(hours=2),
            ),
            WebsiteAdsActionLog(
                workspace_id=8,
                auth_id=11,
                actor_type="OTHER_WORKSPACE",
                action="PAUSE_AD",
                result="SUCCESS",
                created_at=now + timedelta(hours=3),
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *args, **kwargs: "advertiser-bound",
    )
    monkeypatch.setattr(
        website_ads_router,
        "_scoped_action_query",
        lambda *, workspace_id, auth_id, advertiser_id: select(WebsiteAdsActionLog).where(
            WebsiteAdsActionLog.workspace_id == int(workspace_id),
            WebsiteAdsActionLog.auth_id == int(auth_id),
        ),
    )

    first = list_actions(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        page=1,
        page_size=200,
        limit=None,
        db=db_session,
    )
    second = list_actions(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        page=2,
        page_size=200,
        limit=None,
        db=db_session,
    )

    assert first["total"] == 205
    assert first["page"] == 1
    assert first["page_size"] == 200
    assert len(first["items"]) == 200
    assert second["total"] == 205
    assert second["page"] == 2
    assert len(second["items"]) == 5
    assert {item["actor_type"] for item in first["items"] + second["items"]} == {"TEST"}


class _ResourcePagesApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def _request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        page = int(kwargs["params"]["page"])
        return self.responses[(path, page)]


def _payload(list_key, rows, *, page, total_page, total_number=None, **page_info):
    return {
        "code": 0,
        "data": {
            list_key: rows,
            "page_info": {
                "page": page,
                "page_size": page_info.pop("page_size", 50),
                "total_page": total_page,
                "total_number": len(rows) if total_number is None else total_number,
                **page_info,
            },
        },
    }


def test_all_page_resource_methods_merge_every_page_and_keep_single_page_methods():
    responses = {
        ("/pixel/list/", 1): _payload(
            "pixels",
            [{"pixel_id": "pixel-1"}],
            page=1,
            total_page=2,
            total_number=2,
            page_size=20,
        ),
        ("/pixel/list/", 2): _payload(
            "pixels",
            [{"pixel_id": "pixel-2"}],
            page=2,
            total_page=2,
            total_number=2,
            page_size=20,
        ),
        # The identity contract explicitly says page_info can be inaccurate
        # without identity_type. The client therefore proves the end by an
        # empty page instead of trusting total_page=1.
        ("/identity/get/", 1): _payload(
            "identity_list",
            [{"identity_id": "identity-1"}],
            page=1,
            total_page=1,
            total_number=1,
        ),
        ("/identity/get/", 2): _payload(
            "identity_list",
            [],
            page=2,
            total_page=1,
            total_number=1,
        ),
        ("/file/video/ad/search/", 1): _payload(
            "list",
            [{"video_id": "video-1"}],
            page=1,
            total_page=2,
            total_number=2,
            page_size=100,
        ),
        ("/file/video/ad/search/", 2): _payload(
            "list",
            [{"video_id": "video-2"}],
            page=2,
            total_page=2,
            total_number=2,
            page_size=100,
        ),
        ("/tt_video/list/", 1): _payload(
            "list",
            [{"item_info": {"item_id": "spark-1"}}],
            page=1,
            total_page=2,
            total_number=2,
        ),
        ("/tt_video/list/", 2): _payload(
            "list",
            [{"item_info": {"item_id": "spark-2"}}],
            page=2,
            total_page=2,
            total_number=2,
        ),
    }
    api = _ResourcePagesApi(responses)
    client = TikTokWebsiteAdsClient(api)

    pixels = asyncio.run(client.list_all_pixels("advertiser-1"))
    identities = asyncio.run(client.list_all_identities("advertiser-1"))
    videos = asyncio.run(client.list_all_videos("advertiser-1"))
    spark_pages = asyncio.run(client.list_all_spark_videos("advertiser-1"))

    assert [row["pixel_id"] for row in pixels["data"]["pixels"]] == ["pixel-1", "pixel-2"]
    assert [row["identity_id"] for row in identities["data"]["identity_list"]] == ["identity-1"]
    assert [row["video_id"] for row in videos["data"]["list"]] == ["video-1", "video-2"]
    assert len(spark_pages) == 2
    assert [call[2]["params"]["page"] for call in api.calls] == [1, 2, 1, 2, 1, 2, 1, 2]
    identity_calls = [call for call in api.calls if call[1] == "/identity/get/"]
    assert [call[2]["params"]["page_size"] for call in identity_calls] == [100, 100]


def test_positive_continuation_signal_wins_over_stale_terminal_signal():
    responses = {
        ("/pixel/list/", 1): _payload(
            "pixels",
            [{"pixel_id": "pixel-1"}],
            page=1,
            total_page=1,
            total_number=2,
            page_size=20,
            has_next=True,
        ),
        ("/pixel/list/", 2): _payload(
            "pixels",
            [{"pixel_id": "pixel-2"}],
            page=2,
            total_page=2,
            total_number=2,
            page_size=20,
        ),
    }
    client = TikTokWebsiteAdsClient(_ResourcePagesApi(responses))

    result = asyncio.run(client.list_all_pixels("advertiser-1"))

    assert [row["pixel_id"] for row in result["data"]["pixels"]] == ["pixel-1", "pixel-2"]


@pytest.mark.parametrize(
    ("responses", "error_type"),
    [
        (
            {
                ("/pixel/list/", 1): _payload(
                    "pixels",
                    [{"pixel_id": "pixel-1"}],
                    page=2,
                    total_page=2,
                    total_number=2,
                    page_size=20,
                )
            },
            WebsiteAdsPaginationStalledError,
        ),
        (
            {
                ("/pixel/list/", 1): _payload(
                    "pixels",
                    [],
                    page=1,
                    total_page=2,
                    total_number=2,
                    page_size=20,
                )
            },
            WebsiteAdsPaginationInvariantError,
        ),
        (
            {
                ("/pixel/list/", 1): _payload(
                    "pixels",
                    [{"pixel_id": "pixel-1"}],
                    page=1,
                    total_page=2,
                    total_number=2,
                    page_size=20,
                ),
                ("/pixel/list/", 2): _payload(
                    "pixels",
                    [{"pixel_id": "pixel-1"}],
                    page=2,
                    total_page=3,
                    total_number=3,
                    page_size=20,
                ),
            },
            WebsiteAdsPaginationStalledError,
        ),
    ],
)
def test_resource_pagination_rejects_wrong_empty_or_repeated_nonterminal_pages(
    responses,
    error_type,
):
    client = TikTokWebsiteAdsClient(_ResourcePagesApi(responses))

    with pytest.raises(error_type):
        asyncio.run(client.list_all_pixels("advertiser-1"))


def test_resource_pagination_raises_instead_of_silently_stopping_at_cap():
    responses = {
        ("/pixel/list/", 1): _payload(
            "pixels",
            [{"pixel_id": "pixel-1"}],
            page=1,
            total_page=2,
            total_number=2,
            page_size=20,
        )
    }
    client = TikTokWebsiteAdsClient(_ResourcePagesApi(responses))

    with pytest.raises(WebsiteAdsPaginationLimitError):
        asyncio.run(client.list_all_pixels("advertiser-1", max_pages=1))


def test_resource_pagination_rejects_partial_cross_page_overlap_by_item_key():
    responses = {
        ("/pixel/list/", 1): _payload(
            "pixels",
            [{"pixel_id": "pixel-1"}, {"pixel_id": "pixel-2"}],
            page=1,
            total_page=2,
            total_number=3,
            page_size=20,
        ),
        ("/pixel/list/", 2): _payload(
            "pixels",
            [{"pixel_id": "pixel-2"}, {"pixel_id": "pixel-3"}],
            page=2,
            total_page=2,
            total_number=3,
            page_size=20,
        ),
    }
    client = TikTokWebsiteAdsClient(_ResourcePagesApi(responses))

    with pytest.raises(WebsiteAdsPaginationStalledError, match="stable item key"):
        asyncio.run(client.list_all_pixels("advertiser-1"))


def test_resource_pagination_rejects_rows_without_endpoint_item_key():
    responses = {
        ("/file/video/ad/search/", 1): _payload(
            "list",
            [{"display_name": "missing stable ID"}],
            page=1,
            total_page=1,
            total_number=1,
            page_size=100,
        )
    }
    client = TikTokWebsiteAdsClient(_ResourcePagesApi(responses))

    with pytest.raises(WebsiteAdsPaginationInvariantError, match="stable item key"):
        asyncio.run(client.list_all_videos("advertiser-1"))


class _ChunkedReportApi:
    def __init__(self, *, fail_chunk_prefix=None):
        self.calls = []
        self.fail_chunk_prefix = fail_chunk_prefix

    async def _request_json(self, method, path, **kwargs):
        params = kwargs["params"]
        page = int(params["page"])
        filtering = json.loads(params["filtering"])
        ad_ids = json.loads(filtering[0]["filter_value"])
        self.calls.append((method, path, kwargs, ad_ids))
        if self.fail_chunk_prefix and ad_ids[0] == self.fail_chunk_prefix:
            raise RuntimeError("chunk failed")
        selected_id = ad_ids[0] if page == 1 else ad_ids[-1]
        return _payload(
            "list",
            [
                {
                    "dimensions": {
                        "ad_id_v2": selected_id,
                        "stat_time_hour": f"2026-07-17 {page:02d}:00:00",
                    },
                    "metrics": {"spend": str(page)},
                }
            ],
            page=page,
            total_page=2,
            total_number=2,
        )


def test_report_ids_are_deduplicated_chunked_below_100_and_each_chunk_is_fully_paged():
    api = _ChunkedReportApi()
    client = TikTokWebsiteAdsClient(api)
    ad_ids = [f"ad-{index}" for index in range(205)] + ["ad-0", "ad-204"]

    result = asyncio.run(
        client.report_ads(
            "advertiser-1",
            ad_ids,
            "2026-07-17",
            "2026-07-17",
            hourly=True,
        )
    )

    assert [len(call[3]) for call in api.calls] == [99, 99, 99, 99, 7, 7]
    assert [call[2]["params"]["page"] for call in api.calls] == [1, 2, 1, 2, 1, 2]
    assert len(result["data"]["list"]) == 6
    assert result["_report_pagination"]["chunks_fetched"] == 3
    assert result["_report_pagination"]["pages_fetched"] == 6
    assert report_payload_has_complete_pagination(result) is True


@pytest.mark.parametrize(
    ("id_count", "expected_chunk_sizes"),
    [
        (100, [99, 99, 1, 1]),
        (198, [99, 99, 99, 99]),
    ],
)
def test_integrated_report_filter_values_are_strictly_below_100(
    id_count,
    expected_chunk_sizes,
):
    api = _ChunkedReportApi()
    client = TikTokWebsiteAdsClient(api)

    asyncio.run(
        client.report_ads(
            "advertiser-1",
            [f"ad-{index}" for index in range(id_count)],
            "2026-07-17",
            "2026-07-17",
            hourly=True,
        )
    )

    assert [len(call[3]) for call in api.calls] == expected_chunk_sizes
    assert max(len(call[3]) for call in api.calls) == 99


def test_report_chunk_failure_never_returns_a_partial_multi_chunk_result():
    api = _ChunkedReportApi(fail_chunk_prefix="ad-99")
    client = TikTokWebsiteAdsClient(api)

    with pytest.raises(RuntimeError, match="chunk failed"):
        asyncio.run(
            client.report_ads(
                "advertiser-1",
                [f"ad-{index}" for index in range(101)],
                "2026-07-17",
                "2026-07-17",
                hourly=True,
            )
        )


def test_report_completeness_evidence_rejects_missing_or_mismatched_metadata():
    assert report_payload_has_complete_pagination(
        {"data": {"list": []}}
    ) is False
    assert report_payload_has_complete_pagination(
        {
            "data": {"list": [{"dimensions": {}}]},
            "_report_pagination": {
                "chunks_fetched": 1,
                "pages_fetched": 1,
                "rows_returned": 0,
                "source_pages": [{}],
            },
        }
    ) is False


def test_report_pagination_rejects_non_object_rows():
    class Api:
        async def _request_json(self, method, path, **kwargs):
            return _payload(
                "list",
                ["not-an-object"],
                page=1,
                total_page=1,
                total_number=1,
            )

    with pytest.raises(
        WebsiteAdsPaginationInvariantError,
        match="non-object report row",
    ):
        asyncio.run(
            TikTokWebsiteAdsClient(Api()).report_ads(
                "advertiser-1",
                ["ad-1"],
                "2026-07-17",
                "2026-07-17",
                hourly=True,
            )
        )


def test_report_pagination_rejects_rows_without_canonical_dimensions():
    class Api:
        async def _request_json(self, method, path, **kwargs):
            return _payload(
                "list",
                [{"metrics": {"spend": "1"}}],
                page=1,
                total_page=1,
                total_number=1,
            )

    with pytest.raises(
        WebsiteAdsPaginationInvariantError,
        match="without canonical dimensions",
    ):
        asyncio.run(
            TikTokWebsiteAdsClient(Api()).report_ads(
                "advertiser-1",
                ["ad-1"],
                "2026-07-17",
                "2026-07-17",
                hourly=True,
            )
        )
