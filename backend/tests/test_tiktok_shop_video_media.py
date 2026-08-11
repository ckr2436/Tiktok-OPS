from __future__ import annotations

import importlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import gmvmax_creative_media_cache as media_cache


router_module = importlib.import_module("app.features.tenants.tiktok_shop.router")


class _MappingResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _MappingResult(self.rows)


class _SequentialSession:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _MappingResult(self.result_sets.pop(0))


def _cache_row(tmp_path, **overrides):
    video = tmp_path / "video.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    row = {
        "id": 17,
        "workspace_id": 3,
        "auth_id": 9,
        "advertiser_id": "advertiser-1",
        "store_id": "shop-provider-1",
        "item_id": "76543210987654321",
        "local_preview_path": str(video),
        "local_cover_path": str(cover),
        "preview_content_type": "video/mp4",
        "cover_content_type": "image/jpeg",
        "media_cache_status": "READY",
        "media_cached_at": datetime(2026, 7, 21, 1, 2, 3),
        "updated_at": datetime(2026, 7, 21, 1, 2, 3),
    }
    row.update(overrides)
    return row


def test_shop_video_media_lookup_matches_tiktok_item_id_and_local_files(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(id=1, shop_id="shop-provider-1"),
    )
    matching = _cache_row(tmp_path)
    other_shop = _cache_row(tmp_path, id=18, store_id="shop-provider-2")
    session = _Session([matching, other_shop])

    result = router_module.lookup_shop_video_media(
        workspace_id=3,
        payload=router_module.VideoMediaLookupRequest(
            shop_id=1,
            video_ids=[matching["item_id"]],
        ),
        _=object(),
        db=session,
    )

    assert result == {
        "items": [
            {
                "video_id": matching["item_id"],
                "asset_id": 17,
                "cover_url": "/api/v1/tenants/3/tiktok-shop/video-media/17/cover?shop_id=1",
                "preview_url": "/api/v1/tenants/3/tiktok-shop/video-media/17/video?shop_id=1",
                "media_status": "READY",
                "cache_status": "READY",
                "cached_at": "2026-07-21T01:02:03",
            }
        ],
        "requested": 1,
        "matched": 1,
        "status_counts": {"READY": 1},
    }
    assert session.calls[0][1]["workspace_id"] == 3
    assert session.calls[0][1]["video_ids"] == [matching["item_id"]]


def test_shop_video_media_lookup_returns_explicit_unavailable_states(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(id=1, shop_id="shop-provider-1"),
    )
    expired = _cache_row(
        tmp_path,
        id=19,
        item_id="expired-video",
        local_preview_path=None,
        local_cover_path=None,
        media_cache_status="SOURCE_EXPIRED",
    )
    session = _Session([expired])

    result = router_module.lookup_shop_video_media(
        workspace_id=3,
        payload=router_module.VideoMediaLookupRequest(
            shop_id=1,
            video_ids=["expired-video", "not-returned", "0"],
        ),
        _=object(),
        db=session,
    )

    assert result["matched"] == 0
    assert result["status_counts"] == {
        "SOURCE_EXPIRED": 1,
        "NOT_IN_GMVMAX_LIBRARY": 1,
        "INVALID_VIDEO_ID": 1,
    }
    assert result["items"] == [
        {
            "video_id": "expired-video",
            "asset_id": 19,
            "cover_url": None,
            "preview_url": None,
            "media_status": "SOURCE_EXPIRED",
            "cache_status": "SOURCE_EXPIRED",
            "cached_at": "2026-07-21T01:02:03",
        },
        {
            "video_id": "not-returned",
            "asset_id": None,
            "cover_url": None,
            "preview_url": None,
            "media_status": "NOT_IN_GMVMAX_LIBRARY",
            "cache_status": None,
            "cached_at": None,
        },
        {
            "video_id": "0",
            "asset_id": None,
            "cover_url": None,
            "preview_url": None,
            "media_status": "INVALID_VIDEO_ID",
            "cache_status": None,
            "cached_at": None,
        },
    ]


def test_guard_feed_is_bounded_store_scoped_and_redacts_payloads(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(id=1, shop_id="shop-provider-1"),
    )
    created_at = datetime(2026, 7, 21, 2, 3, 4)
    event = {
        "id": 91,
        "advertiser_id": "adv-1",
        "campaign_id": "campaign-1",
        "event_type": "SMART_GUARD",
        "action": "PAUSE",
        "reason": "ROI below guard threshold",
        "result": "SUCCESS",
        "cost_cents": 500,
        "gross_revenue_cents": 100,
        "orders": 1,
        "roi": 0.2,
        "request_json": {"creative_id": "video-1", "secret": "do-not-return"},
        "response_json": {"request_id": "req-1", "token": "do-not-return"},
        "error_message": None,
        "created_at": created_at,
    }
    state = {
        "id": 7,
        "advertiser_id": "adv-1",
        "campaign_id": "campaign-1",
        "campaign_name": "Campaign one",
        "operation_status": "DISABLE",
        "secondary_status": "PAUSED",
        "guard_status": "PAUSED",
        "last_action": "PAUSE",
        "last_reason": "ROI below guard threshold",
        "latest_cost_cents": 500,
        "latest_gross_revenue_cents": 100,
        "latest_orders": 1,
        "latest_roi": 0.2,
        "source": "tiktok_report_today",
        "last_report_at": created_at,
        "last_checked_at": created_at,
        "updated_at": created_at,
    }
    session = _SequentialSession([[event], [state]])

    result = router_module.guard_feed(
        workspace_id=3,
        shop_id=1,
        before_id=None,
        limit=40,
        _=object(),
        db=session,
    )

    assert result["items"] == [
        {
            "id": 91,
            "advertiser_id": "adv-1",
            "campaign_id": "campaign-1",
            "creative_id": "video-1",
            "event_type": "SMART_GUARD",
            "action": "PAUSE",
            "reason": "ROI below guard threshold",
            "result": "SUCCESS",
            "operator": "系统",
            "cost_cents": 500,
            "gross_revenue_cents": 100,
            "orders": 1,
            "roi": 0.2,
            "official_request_id": "req-1",
            "error_message": None,
            "created_at": created_at,
        }
    ]
    assert result["states"][0]["campaign_id"] == "campaign-1"
    assert result["data_meta"]["refresh_interval_seconds"] == 60
    assert session.calls[0][1]["workspace_id"] == 3
    assert session.calls[0][1]["store_id"] == "shop-provider-1"
    assert session.calls[0][1]["row_limit"] == 41
    assert "secret" not in str(result)
    assert "token" not in str(result)


def test_shop_video_media_serve_rejects_asset_from_another_shop(tmp_path, monkeypatch):
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(id=1, shop_id="shop-provider-1"),
    )
    session = _Session([_cache_row(tmp_path, store_id="shop-provider-2")])

    with pytest.raises(HTTPException) as exc:
        router_module._serve_shop_video_media(
            session,
            workspace_id=3,
            shop_id=1,
            asset_id=17,
            kind="cover",
        )

    assert exc.value.status_code == 404
