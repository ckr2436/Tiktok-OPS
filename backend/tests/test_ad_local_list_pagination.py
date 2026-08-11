from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text

from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max._helpers import GMVMaxAccountBinding


def _context(db_session) -> router_provider.GMVMaxRouteContext:
    return router_provider.GMVMaxRouteContext(
        workspace_id=7,
        provider="tiktok-business",
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=GMVMaxAccountBinding(
            account=SimpleNamespace(),
            bc_id="bc-1",
            advertiser_id="adv-1",
            store_id="store-1",
        ),
        client=SimpleNamespace(),
        db=db_session,
    )


def _create_daily_report_tables(db_session) -> None:
    db_session.execute(text("drop table if exists gmv_hermes_ad_plan_defaults"))
    db_session.execute(text("drop table if exists gmv_hermes_ad_daily_reports"))
    db_session.execute(
        text(
            """
            create table gmv_hermes_ad_daily_reports (
                id integer primary key,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(128) not null,
                report_date date not null,
                advertiser_timezone varchar(64),
                report_type varchar(32),
                status varchar(32),
                input_json text,
                response_json text,
                report_markdown text,
                recommendation_json text,
                hermes_response_id varchar(255),
                prompt_tokens integer,
                completion_tokens integer,
                total_tokens integer,
                error_message text,
                created_at datetime,
                updated_at datetime
            )
            """
        )
    )
    db_session.execute(
        text(
            """
            create table gmv_hermes_ad_plan_defaults (
                id integer primary key,
                source_report_id integer not null,
                item_group_id varchar(128),
                effective_date date,
                status varchar(32),
                confidence varchar(32),
                params_json text,
                decision_json text,
                updated_at datetime
            )
            """
        )
    )
    now = datetime(2026, 7, 17, 12, 0, 0)
    for index in range(5):
        db_session.execute(
            text(
                """
                insert into gmv_hermes_ad_daily_reports (
                    id, workspace_id, auth_id, advertiser_id, store_id,
                    report_date, advertiser_timezone, report_type, status,
                    input_json, response_json, report_markdown,
                    recommendation_json, created_at, updated_at
                ) values (
                    :id, 7, 11, 'adv-1', 'store-1',
                    :report_date, 'UTC', 'DAILY', 'GENERATED',
                    '{}', '{}', :markdown, '{}', :created_at, :updated_at
                )
                """
            ),
            {
                "id": index + 1,
                "report_date": date(2026, 7, 10 + index),
                "markdown": f"Report {index}",
                "created_at": now + timedelta(days=index),
                "updated_at": now + timedelta(days=index),
            },
        )
    db_session.execute(
        text(
            """
            insert into gmv_hermes_ad_daily_reports (
                id, workspace_id, auth_id, advertiser_id, store_id,
                report_date, advertiser_timezone, report_type, status,
                input_json, response_json, report_markdown,
                recommendation_json, created_at, updated_at
            ) values (
                99, 7, 11, 'adv-other', 'store-1',
                '2026-07-17', 'UTC', 'DAILY', 'GENERATED',
                '{}', '{}', 'Other', '{}', '2026-07-17', '2026-07-17'
            )
            """
        )
    )
    db_session.commit()


def test_gmv_hermes_daily_reports_support_numbered_pages_and_legacy_limit(
    db_session,
):
    _create_daily_report_tables(db_session)

    page = asyncio.run(
        router_provider.list_hermes_daily_reports_provider(
            workspace_id=7,
            provider="tiktok-business",
            auth_id=11,
            store_id="store-1",
            advertiser_id="adv-1",
            page=2,
            page_size=None,
            limit=2,
            context=_context(db_session),
        )
    )

    assert page["total"] == 5
    assert page["page"] == 2
    assert page["page_size"] == 2
    assert len(page["list"]) == 2


def test_creative_metric_sort_key_covers_scope_and_business_dimensions():
    rows = [
        SimpleNamespace(
            id=2,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-b",
            item_group_id="product-a",
            creative_id="creative-a",
            stat_time_day=date(2026, 7, 17),
        ),
        SimpleNamespace(
            id=1,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-a",
            item_group_id="product-b",
            creative_id="creative-b",
            stat_time_day=date(2026, 7, 17),
        ),
        SimpleNamespace(
            id=3,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="campaign-a",
            item_group_id="product-a",
            creative_id="creative-b",
            stat_time_day=date(2026, 7, 16),
        ),
    ]

    sorted_rows = sorted(rows, key=router_provider._creative_metric_row_sort_key)

    assert [row.id for row in sorted_rows] == [3, 1, 2]


def test_serialized_creatives_are_stable_before_page_slicing():
    rows = [
        {
            "dimensions": {
                "campaign_id": "campaign-b",
                "product_id": "product-a",
                "creative_id": "creative-a",
                "shop_content_id": "creative-a",
                "stat_time_day": "2026-07-17",
            }
        },
        {
            "dimensions": {
                "campaign_id": "campaign-a",
                "product_id": "product-b",
                "creative_id": "creative-b",
                "shop_content_id": "creative-b",
                "stat_time_day": "2026-07-17",
            }
        },
    ]

    rows.sort(key=router_provider._serialized_creative_row_sort_key)

    assert rows[0]["dimensions"]["campaign_id"] == "campaign-a"
