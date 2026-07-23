from __future__ import annotations

import importlib
from datetime import date, timedelta
from types import SimpleNamespace

from app.data.models.tiktok_shop import (
    TikTokShopVideoDailyMetric,
    TikTokShopVideoOverviewDailyMetric,
)


router_module = importlib.import_module("app.features.tenants.tiktok_shop.router")


def test_video_analytics_uses_stable_primary_key_tiebreaker(monkeypatch, db_session):
    report_date = date(2026, 7, 19)
    rows = [
        TikTokShopVideoDailyMetric(
            workspace_id=3,
            account_id=1,
            shop_row_id=1,
            report_date=report_date,
            video_id=f"video-{index}",
            gmv=0,
            views=0,
            sku_orders=0,
            items_sold=0,
            avg_customers=0,
        )
        for index in range(1, 7)
    ]
    db_session.add_all(rows)
    db_session.commit()
    expected_ids = sorted((int(row.id) for row in rows), reverse=True)

    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(timezone_name="Etc/GMT+8"),
    )

    pages = []
    for page in range(1, 4):
        result = router_module.analytics(
            workspace_id=3,
            dataset="videos",
            shop_id=1,
            start_date=report_date,
            end_date_exclusive=report_date + timedelta(days=1),
            product_id=None,
            channel=None,
            page=page,
            page_size=2,
            _=object(),
            db=db_session,
        )
        pages.append([int(item["id"]) for item in result["items"]])
        assert result["total"] == 6

    flattened = [row_id for page_ids in pages for row_id in page_ids]
    assert flattened == expected_ids
    assert len(flattened) == len(set(flattened))


def test_video_overview_exposes_freshness_without_returning_raw_payload(
    monkeypatch, db_session
):
    report_date = date(2026, 7, 21)
    db_session.add(
        TikTokShopVideoOverviewDailyMetric(
            workspace_id=3,
            account_id=1,
            shop_row_id=1,
            report_date=report_date,
            currency="USD",
            gmv=23.27,
            avg_customers=0,
            product_impressions=1000,
            product_clicks=30,
            sku_orders=2,
            click_through_rate=None,
            raw_json={
                "gmv": {"amount": "23.27", "currency": "USD"},
                "_gmv_ops_meta": {
                    "source": "shop_and_product_video_channels",
                    "provisional": True,
                    "latest_available_date": "2026-07-20",
                    "provider_request_id": "req-fallback",
                    "ctr_definition": "product_clicks_divided_by_video_views",
                },
            },
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        router_module,
        "_get_shop",
        lambda *_args, **_kwargs: SimpleNamespace(timezone_name="America/New_York"),
    )
    monkeypatch.setattr(router_module, "shop_today", lambda _shop: report_date)

    result = router_module.analytics(
        workspace_id=3,
        dataset="video-overview",
        shop_id=1,
        start_date=report_date,
        end_date_exclusive=report_date + timedelta(days=1),
        product_id=None,
        channel=None,
        page=1,
        page_size=10,
        _=object(),
        db=db_session,
    )

    assert result["data_meta"]["dataset_freshness"] == "realtime_aggregate"
    assert result["data_meta"]["stable_through_date"] == "2026-07-20"
    assert result["items"][0]["data_source"] == "shop_and_product_video_channels"
    assert result["items"][0]["is_provisional"] is True
    assert result["items"][0]["click_through_rate"] is None
    assert "raw_json" not in result["items"][0]
