from __future__ import annotations

from pathlib import Path

import httpx
import asyncio
import pytest

from app.services import gmvmax_creative_assets as creative_assets
from app.services import gmvmax_creative_media_cache as media_cache
from app.services import website_ads_media_cache as shared_media_cache
from app.features.tenants.ttb.gmv_max import router_provider


class _RecordingSession:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))


class _RowcountResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _MappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


def _row(**overrides):
    row = {
        "id": 17,
        "workspace_id": 3,
        "auth_id": 9,
        "advertiser_id": "advertiser-1",
        "store_id": "store-1",
        "item_id": "creative-1",
        "local_preview_path": None,
        "local_cover_path": None,
        "preview_content_type": None,
        "cover_content_type": None,
    }
    row.update(overrides)
    return row


def test_creative_media_urls_are_local_and_only_returned_for_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(tmp_path))
    row = _row()

    assert media_cache.creative_media_urls(row) == {"preview_url": None, "video_cover_url": None}

    directory = media_cache.asset_directory(row)
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "video.mp4"
    cover = directory / "cover.jpg"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")

    urls = media_cache.creative_media_urls(row)
    expected_base = "/api/v1/tenants/3/providers/tiktok-business/accounts/9/gmvmax/creative-assets/17"
    assert urls == {
        "preview_url": f"{expected_base}/video",
        "video_cover_url": f"{expected_base}/cover",
    }
    assert media_cache.resolve_creative_media(row, "video")[0] == video
    assert media_cache.resolve_creative_media(row, "cover")[0] == cover


def test_explicit_media_path_must_remain_inside_configured_raid_root(tmp_path, monkeypatch):
    root = tmp_path / "raid"
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(root))

    assert media_cache.resolve_creative_media(_row(local_preview_path=str(outside)), "video") is None

    inside = root / "manual" / "video.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"inside")
    resolved = media_cache.resolve_creative_media(
        _row(local_preview_path=str(inside), preview_content_type="video/mp4"),
        "video",
    )
    assert resolved == (Path(inside).resolve(), "video/mp4")


def test_media_route_releases_database_connection_before_streaming(tmp_path, monkeypatch):
    root = tmp_path / "raid"
    cover = root / "cached" / "cover.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"cover")
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(root))

    class Session:
        def __init__(self):
            self.rollbacks = 0

        def execute(self, _statement, params):
            assert params == {"asset_id": 17, "workspace_id": 3, "auth_id": 9}
            return _MappingResult(
                _row(local_cover_path=str(cover), cover_content_type="image/jpeg")
            )

        def rollback(self):
            self.rollbacks += 1

    session = Session()
    response = router_provider._serve_gmvmax_creative_media(
        session,
        workspace_id=3,
        auth_id=9,
        asset_id=17,
        kind="cover",
    )

    assert Path(response.path) == cover
    assert response.media_type == "image/jpeg"
    assert session.rollbacks == 1


def test_asset_upsert_only_requeues_when_tiktok_returns_a_different_source():
    session = _RecordingSession()
    creative_assets._upsert_asset(
        session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        store_id="store-1",
        payload={
            "item_id": "creative-1",
            "preview_url": "https://example.test/video",
            "video_cover_url": "https://example.test/cover",
            "raw_json": {},
        },
    )

    sql = " ".join(session.calls[0][0].split())
    status_position = sql.index("media_cache_status=case")
    source_position = sql.index("preview_url=coalesce")
    assert status_position < source_position
    assert "media_cache_status in ('QUEUED', 'PROCESSING')" in sql
    assert "values(preview_url) is not null" in sql
    assert "not (values(preview_url) <=> preview_url)" in sql
    assert "else media_cache_status" in sql


def test_asset_payload_preserves_complete_deduplicated_spu_list():
    payload = creative_assets._asset_payload_from_entry(
        {
            "item_id": "creative-1",
            "spu_id_list": ["product-a", "product-b", "product-a", "  "],
            "video_info": {"video_id": "video-1"},
            "identity_info": {
                "identity_id": "identity-1",
                "identity_type": "TT_USER",
            },
        }
    )

    assert payload is not None
    assert payload["item_group_id"] == "product-a"
    assert payload["spu_id_list"] == ["product-a", "product-b"]
    assert payload["raw_json"]["spu_id_list"] == ["product-a", "product-b"]


def test_asset_upsert_atomically_replaces_all_product_relations():
    session = _RecordingSession()
    creative_assets._upsert_asset(
        session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        store_id="store-1",
        payload={
            "item_id": "creative-1",
            "item_group_id": "product-a",
            "spu_id_list": ["product-a", "product-b", "product-a"],
            "raw_json": {
                "item_id": "creative-1",
                "spu_id_list": ["product-a", "product-b"],
            },
        },
    )

    assert len(session.calls) == 3
    cache_sql, cache_params = session.calls[0]
    delete_sql, delete_params = session.calls[1]
    insert_sql, insert_params = session.calls[2]
    assert "item_group_id=values(item_group_id)" in " ".join(cache_sql.split())
    assert cache_params["item_group_id"] == "product-a"
    assert "delete from gmvmax_creative_asset_products" in delete_sql
    assert delete_params["item_id"] == "creative-1"
    assert "insert into gmvmax_creative_asset_products" in insert_sql
    assert [row["item_group_id"] for row in insert_params] == [
        "product-a",
        "product-b",
    ]

    cleared = _RecordingSession()
    creative_assets._upsert_asset(
        cleared,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        store_id="store-1",
        payload={
            "item_id": "creative-1",
            "item_group_id": None,
            "spu_id_list": [],
            "raw_json": {"item_id": "creative-1", "spu_id_list": []},
        },
    )

    assert len(cleared.calls) == 2
    assert cleared.calls[0][1]["item_group_id"] is None
    assert "delete from gmvmax_creative_asset_products" in cleared.calls[1][0]


def test_complete_scope_reconciliation_tombstones_only_unseen_cache_rows():
    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), dict(params)))
            return _RowcountResult(2)

    session = Session()
    changed = creative_assets._deactivate_absent_scope_assets(
        session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        store_id="store-1",
        seen_item_ids={"creative-current"},
    )

    sql, params = session.calls[0]
    normalized_sql = " ".join(sql.split())
    assert changed == 2
    assert "json_set" in normalized_sql
    assert "'$._gmv_ops_sync.active', false" in normalized_sql
    assert "item_id not in" in normalized_sql
    assert params["seen_item_ids"] == ["creative-current"]
    assert params["workspace_id"] == 3
    assert params["auth_id"] == 9
    assert params["advertiser_id"] == "advertiser-1"
    assert params["store_id"] == "store-1"


def test_filtered_reconciliation_removes_only_target_product_relations():
    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), dict(params)))
            return _RowcountResult(1)

    session = Session()
    changed = creative_assets._reconcile_asset_product_partitions(
        session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        store_id="store-1",
        seen_by_item_group={
            "product-a": {"creative-a"},
            "product-b": set(),
        },
    )

    assert changed == 2
    assert len(session.calls) == 2
    first_sql, first_params = session.calls[0]
    second_sql, second_params = session.calls[1]
    assert "delete from gmvmax_creative_asset_products" in first_sql
    assert "item_id not in" in first_sql
    assert first_params["item_group_id"] == "product-a"
    assert first_params["seen_item_ids"] == ["creative-a"]
    assert "item_id not in" not in second_sql
    assert second_params["item_group_id"] == "product-b"


def test_official_pagination_failure_never_runs_creative_absence_reconciliation(
    monkeypatch,
):
    async def load_identities(*_args, **_kwargs):
        return []

    async def fail_pages(*_args, **_kwargs):
        raise RuntimeError("official pagination failed")
        yield  # pragma: no cover

    reconciled = []
    monkeypatch.setattr(creative_assets, "ensure_creative_asset_cache_table", lambda _session: None)
    monkeypatch.setattr(creative_assets, "load_gmvmax_identity_filter", load_identities)
    monkeypatch.setattr(creative_assets, "iter_gmvmax_video_entries", fail_pages)
    monkeypatch.setattr(
        creative_assets,
        "_deactivate_absent_scope_assets",
        lambda *_args, **_kwargs: reconciled.append("scope"),
    )
    monkeypatch.setattr(
        creative_assets,
        "_reconcile_asset_product_partitions",
        lambda *_args, **_kwargs: reconciled.append("partition"),
    )

    with pytest.raises(RuntimeError, match="pagination failed"):
        asyncio.run(
            creative_assets._sync_creative_assets_for_scope_unlocked(
                object(),
                object(),
                workspace_id=3,
                auth_id=9,
                advertiser_id="advertiser-1",
                store_id="store-1",
                store_authorized_bc_id="bc-1",
            )
        )

    assert reconciled == []


def test_media_download_does_not_retry_expired_or_invalid_sources():
    request = httpx.Request("GET", "https://example.test/video")
    forbidden = httpx.HTTPStatusError(
        "forbidden",
        request=request,
        response=httpx.Response(403, request=request),
    )
    unavailable = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )

    assert shared_media_cache._should_retry_media_download(forbidden) is False
    assert shared_media_cache._should_retry_media_download(ValueError("wrong content type")) is False
    assert shared_media_cache._should_retry_media_download(unavailable) is True
    assert shared_media_cache._should_retry_media_download(httpx.ReadTimeout("timeout")) is True


def test_expired_media_retry_uses_database_clock(monkeypatch, tmp_path):
    row = _row(
        preview_url="https://example.test/expired-video",
        video_cover_url=None,
        media_cache_attempts=12,
    )

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, dict(params or {})))
            if "select * from gmvmax_creative_asset_cache" in sql.lower():
                return _MappingResult(row)
            return _RowcountResult(1)

        def commit(self):
            return None

    async def expired_download(*_args, **_kwargs):
        request = httpx.Request("GET", "https://example.test/expired-video")
        raise httpx.HTTPStatusError(
            "forbidden",
            request=request,
            response=httpx.Response(403, request=request),
        )

    session = Session()
    monkeypatch.setattr(media_cache, "ensure_creative_asset_cache_table", lambda _db: None)
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(media_cache, "download_asset_file", expired_download)

    result = asyncio.run(media_cache.cache_creative_asset_media(session, 17))

    assert result["status"] == "SOURCE_EXPIRED"
    final_sql, final_params = session.calls[-1]
    normalized_sql = " ".join(final_sql.split()).lower()
    assert (
        "date_add(current_timestamp(6), interval :retry_delay_minutes minute)"
        in normalized_sql
    )
    assert final_params["retry_delay_minutes"] == 320
    assert "media_cache_next_retry_at" not in final_params


def test_cached_video_generates_cover_without_remote_cover_request(monkeypatch, tmp_path):
    root = tmp_path / "raid"
    video = root / "cached" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    row = _row(
        local_preview_path=str(video),
        preview_content_type="video/mp4",
        video_cover_url="https://example.test/temporary-cover",
        media_cache_attempts=0,
    )

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, dict(params or {})))
            if "select * from gmvmax_creative_asset_cache" in sql.lower():
                return _MappingResult(row)
            return _RowcountResult(1)

        def commit(self):
            return None

    generated = []
    downloaded = []

    def generate_cover(video_path, target_path):
        generated.append((Path(video_path), Path(target_path)))
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(b"cover")
        return {"path": str(target_path), "content_type": "image/jpeg"}

    async def download_cover(*args, **kwargs):
        downloaded.append((args, kwargs))
        raise AssertionError("remote cover must not be requested for a cached video")

    session = Session()
    monkeypatch.setattr(media_cache, "ensure_creative_asset_cache_table", lambda _db: None)
    monkeypatch.setattr(media_cache.settings, "GMVMAX_MEDIA_STORAGE_DIR", str(root))
    monkeypatch.setattr(media_cache, "generate_video_cover", generate_cover)
    monkeypatch.setattr(media_cache, "download_asset_file", download_cover)

    result = asyncio.run(media_cache.cache_creative_asset_media(session, 17))

    assert result == {
        "asset_id": 17,
        "status": "READY",
        "video_cached": True,
        "cover_cached": True,
        "errors": {},
    }
    assert generated and generated[0][0] == video
    assert downloaded == []
