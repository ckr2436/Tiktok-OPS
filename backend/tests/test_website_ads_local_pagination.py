from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.data.models import OAuthAccountTTB, OAuthProviderApp, Workspace
from app.data.models.website_ads import (
    WebsiteAdsCampaign,
    WebsiteAdsDailyReport,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsUploadFingerprint,
)
from app.features.tenants.ttb.website_ads import router as website_ads_router


def _seed_local_lists(db_session) -> None:
    db_session.add(
        Workspace(id=1, name="Tenant", company_code="PAGING")
    )
    db_session.add(
        OAuthProviderApp(
            id=1,
            provider="tiktok-business",
            name="TikTok Business",
            client_id="client",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.test/oauth/callback",
        )
    )
    db_session.add(
        OAuthAccountTTB(
            id=2,
            workspace_id=1,
            provider_app_id=1,
            alias="Account",
            access_token_cipher=b"token",
            token_fingerprint=b"x" * 32,
        )
    )
    landing = WebsiteAdsLandingPage(
        id=10,
        workspace_id=1,
        external_id="manual-10",
        identifier="product-10",
        title="Product",
        landing_url="https://example.test/product",
        is_active=True,
    )
    db_session.add(landing)
    db_session.flush()
    base_time = datetime(2026, 7, 17, 12, 0, 0)
    campaigns = [
        WebsiteAdsCampaign(
            id=20,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-bound",
            landing_page_id=10,
            request_key="request-bound",
            campaign_id="campaign-bound",
            name="Bound Campaign",
            created_at=base_time,
        ),
        WebsiteAdsCampaign(
            id=22,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-bound",
            landing_page_id=10,
            request_key="request-bound-22",
            campaign_id="campaign-bound-22",
            name="Bound Campaign 22",
            created_at=base_time,
        ),
        WebsiteAdsCampaign(
            id=23,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-bound",
            landing_page_id=10,
            request_key="request-bound-23",
            campaign_id="campaign-bound-23",
            name="Bound Campaign 23",
            created_at=base_time,
        ),
        WebsiteAdsCampaign(
            id=21,
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-other",
            landing_page_id=10,
            request_key="request-other",
            campaign_id="campaign-other",
            name="Other Campaign",
            created_at=base_time,
        ),
    ]
    db_session.add_all(campaigns)
    for index in range(5):
        db_session.add(
            WebsiteAdsMediaPlan(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-bound",
                landing_page_id=10,
                name=f"Plan {index}",
                daily_budget=Decimal("50"),
                created_at=base_time + timedelta(minutes=index),
            )
        )
        db_session.add(
            WebsiteAdsDailyReport(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-bound",
                campaign_local_id=20,
                landing_page_id=10,
                report_date=date(2026, 7, 10 + index),
                advertiser_timezone="UTC",
                metrics_json={},
                audience_performance_json=[],
                generated_at=base_time + timedelta(days=index),
            )
        )
        db_session.add(
            WebsiteAdsUploadFingerprint(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-bound",
                content_sha256=f"{index:064d}",
                file_name=f"video-{index}.mp4",
                status="UPLOADED",
                video_id=f"video-{index}",
                updated_at=base_time + timedelta(minutes=index),
            )
        )
    db_session.add_all(
        [
            WebsiteAdsMediaPlan(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-other",
                landing_page_id=10,
                name="Other plan",
                daily_budget=Decimal("50"),
            ),
            WebsiteAdsDailyReport(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-other",
                campaign_local_id=21,
                landing_page_id=10,
                report_date=date(2026, 7, 17),
                advertiser_timezone="UTC",
                metrics_json={},
                audience_performance_json=[],
                generated_at=base_time,
            ),
            WebsiteAdsUploadFingerprint(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-other",
                content_sha256="f" * 64,
                status="UPLOADED",
            ),
        ]
    )
    db_session.commit()


def test_media_plans_and_daily_reports_expose_complete_numbered_page_metadata(
    db_session,
    monkeypatch,
):
    _seed_local_lists(db_session)
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *args, **kwargs: "advertiser-bound",
    )

    plans = website_ads_router.list_media_plans(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        page=2,
        page_size=2,
        limit=None,
        db=db_session,
    )
    reports = website_ads_router.list_daily_reports(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        campaign_local_id=None,
        start_date=None,
        end_date=None,
        page=2,
        page_size=2,
        limit=None,
        db=db_session,
    )

    assert plans["total"] == 5
    assert plans["page"] == 2
    assert plans["page_size"] == 2
    assert len(plans["items"]) == 2
    assert reports["total"] == 5
    assert reports["page"] == 2
    assert reports["page_size"] == 2
    assert len(reports["items"]) == 2
    assert {item["campaign_name"] for item in reports["items"]} == {"Bound Campaign"}


def test_legacy_limit_is_reported_as_page_size_instead_of_hiding_total(
    db_session,
    monkeypatch,
):
    _seed_local_lists(db_session)
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *args, **kwargs: "advertiser-bound",
    )

    plans = website_ads_router.list_media_plans(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        page=1,
        page_size=None,
        limit=2,
        db=db_session,
    )
    reports = website_ads_router.list_daily_reports(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        campaign_local_id=None,
        start_date=None,
        end_date=None,
        page=1,
        page_size=None,
        limit=2,
        db=db_session,
    )

    assert (plans["page_size"], len(plans["items"]), plans["total"]) == (2, 2, 5)
    assert (reports["page_size"], len(reports["items"]), reports["total"]) == (2, 2, 5)


def test_video_upload_history_is_stably_paged_and_advertiser_scoped(
    db_session,
    monkeypatch,
):
    _seed_local_lists(db_session)
    monkeypatch.setattr(
        website_ads_router,
        "resolve_account_binding",
        lambda *args, **kwargs: SimpleNamespace(advertiser_id="advertiser-bound"),
    )

    result = website_ads_router.list_video_uploads(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        upload_ids=None,
        page=2,
        page_size=None,
        limit=2,
        db=db_session,
    )

    assert result["total"] == 5
    assert result["page"] == 2
    assert result["page_size"] == 2
    assert len(result["items"]) == 2


def test_campaign_pages_use_id_as_the_created_at_tie_break(
    db_session,
    monkeypatch,
):
    _seed_local_lists(db_session)
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *args, **kwargs: "advertiser-bound",
    )
    monkeypatch.setattr(
        website_ads_router,
        "_metric_summary",
        lambda *args, **kwargs: ({}, None),
    )
    monkeypatch.setattr(
        website_ads_router,
        "_landing_dict",
        lambda *args, **kwargs: {},
    )

    pages = [
        website_ads_router.list_campaigns(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            page=page,
            page_size=1,
            start_date=None,
            end_date=None,
            db=db_session,
        )
        for page in (1, 2, 3)
    ]

    assert [result["items"][0]["id"] for result in pages] == [23, 22, 20]
    assert {result["total"] for result in pages} == {3}
