from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.features.tenants.ttb.gmv_max import router_provider
from app.services.gmvmax_hermes_creative_ranker import rank_creative_candidates


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _RecordingDb:
    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, params):
        sql = " ".join(str(statement).split())
        self.statements.append((sql, dict(params)))
        if "from gmvmax_creative_asset_cache a" in sql:
            return _Rows(
                [
                    {
                        "id": 17,
                        "workspace_id": 3,
                        "auth_id": 9,
                        "advertiser_id": "advertiser-1",
                        "store_id": "store-1",
                        "item_id": "creative-1",
                        # The requested second SPU must be projected even though
                        # the compatibility column stores the first SPU.
                        "item_group_id": "product-b",
                        "video_id": "video-1",
                        "title": "Creative 1",
                        "local_preview_path": None,
                        "local_cover_path": None,
                        "preview_content_type": None,
                        "cover_content_type": None,
                        "media_cache_status": "PENDING",
                        "duration": 10,
                        "identity_id": "identity-1",
                        "identity_type": "TT_USER",
                        "identity_name": "Identity 1",
                        "identity_authorized_bc_id": None,
                        "identity_authorized_shop_id": None,
                        "can_change_anchor": "false",
                        "fetched_at": datetime(2026, 7, 17),
                        "updated_at": datetime(2026, 7, 17),
                        "cost_cents": 0,
                        "gross_revenue_cents": 0,
                        "orders": 0,
                        "clicks": 0,
                        "impressions": 0,
                        "product_clicks": 0,
                        "product_impressions": 0,
                        "ad_video_view_rate_2s": 0,
                        "ad_video_view_rate_6s": 0,
                        "ad_video_view_rate_p100": 0,
                    }
                ]
            )
        if "from gmvmax_manual_creative_uploads" in sql:
            return _Rows([])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_creative_asset_product_filter_uses_relation_and_projects_requested_spu(
    monkeypatch,
):
    db = _RecordingDb()
    monkeypatch.setattr(
        router_provider,
        "_validate_bound_scope",
        lambda *_args, **_kwargs: ("advertiser-1", "store-1"),
    )
    monkeypatch.setattr(
        router_provider,
        "_load_historical_removed_creatives",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_today",
        lambda *_args, **_kwargs: datetime(2026, 7, 17).date(),
    )
    monkeypatch.setattr(
        router_provider,
        "_product_price_for_hermes",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        router_provider,
        "rank_creative_candidates",
        lambda candidates, **_kwargs: (
            list(candidates),
            {
                "model": "TEST",
                "status": "ok",
                "evaluated": len(candidates),
                "recommended": 0,
                "has_proven_winners": False,
            },
        ),
    )

    response = asyncio.run(
        router_provider.list_gmvmax_creative_assets_route(
            workspace_id=3,
            provider="tiktok-business",
            auth_id=9,
            store_id="store-1",
            advertiser_id="advertiser-1",
            campaign_id=None,
            item_group_id=None,
            item_group_ids=["product-c", "product-b"],
            refresh=False,
            lookback_days=30,
            page=1,
            page_size=24,
            offset=None,
            context=SimpleNamespace(db=db),
        )
    )

    asset_sql, params = db.statements[0]
    assert "exists ( select 1 from gmvmax_creative_asset_products ap" in asset_sql
    assert "ap.item_group_id in" in asset_sql
    assert "item_group_id in" in asset_sql
    assert "a.updated_at desc, a.item_id asc" in asset_sql
    assert params["item_group_ids"] == ["product-c", "product-b"]
    assert params["metric_start_date"] == datetime(2026, 6, 17).date()
    assert response["items"][0]["item_group_id"] == "product-b"


def test_absent_creative_or_product_relation_is_never_selectable():
    base = {
        "item_id": "creative-1",
        "video_id": "video-1",
        "identity_id": "identity-1",
        "identity_type": "TT_USER",
    }

    absent_scope = router_provider._creative_asset_candidate_from_row(
        {**base, "cache_active": "false", "partition_active": 1}
    )
    absent_partition = router_provider._creative_asset_candidate_from_row(
        {**base, "cache_active": "true", "partition_active": 0}
    )

    assert absent_scope["selectable"] is False
    assert "完整素材列表" in absent_scope["not_selectable_reason"]
    assert absent_partition["selectable"] is False
    assert "所选商品" in absent_partition["not_selectable_reason"]


def test_hermes_ranking_uses_item_id_as_the_final_tie_break():
    ranked, _ = rank_creative_candidates(
        [
            {"item_id": "creative-b", "selectable": True, "metrics": {}},
            {"item_id": "creative-a", "selectable": True, "metrics": {}},
        ],
        product_price=10,
    )

    assert [item["item_id"] for item in ranked] == ["creative-a", "creative-b"]
