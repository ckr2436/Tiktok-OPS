from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.core.deps import require_tenant_member
from app.data.db import get_db
from app.data.models import OAuthAccountTTB, OAuthProviderApp, Workspace
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
    WebsiteAdsLandingPage,
)
from app.features.tenants.ttb.website_ads import router as website_ads_router
from app.features.tenants.ttb.website_ads.schemas import (
    StatusUpdateRequest,
    WebsiteAdLaunchRequest,
)
from app.features.tenants.ttb.website_ads.scope import resolve_bound_advertiser_id
from app.services import website_ads_launch


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        (
            "get",
            "/website-ads/metadata",
            None,
        ),
        (
            "post",
            "/website-ads/targeting/interests",
            {
                "advertiser_id": "advertiser-outside-binding",
                "search_keywords": ["wellness"],
            },
        ),
        (
            "post",
            "/website-ads/targeting/locations",
            {
                "advertiser_id": "advertiser-outside-binding",
                "search_keyword": "New York",
            },
        ),
    ],
)
def test_remote_reads_reject_cross_advertiser_override(
    monkeypatch,
    method,
    path,
    json_body,
):
    app = FastAPI()
    app.include_router(website_ads_router.router)
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[require_tenant_member] = lambda: object()
    monkeypatch.setattr(
        website_ads_router,
        "resolve_account_binding",
        lambda *args, **kwargs: SimpleNamespace(advertiser_id="advertiser-bound"),
    )
    upstream = AsyncMock(side_effect=AssertionError("upstream API must not be called"))
    monkeypatch.setattr(website_ads_router, "_with_api", upstream)
    params = {
        "workspace_id": 1,
        "provider": "tiktok-business",
        "auth_id": 2,
    }
    if method == "get":
        params["advertiser_id"] = "advertiser-outside-binding"

    with TestClient(app) as client:
        response = client.request(method, path, params=params, json=json_body)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "WEBSITE_ADS_ADVERTISER_SCOPE_MISMATCH"
    upstream.assert_not_awaited()


def test_bound_advertiser_accepts_only_omitted_or_matching_request():
    assert resolve_bound_advertiser_id("advertiser-1") == "advertiser-1"
    assert resolve_bound_advertiser_id("advertiser-1", "advertiser-1") == "advertiser-1"
    with pytest.raises(HTTPException) as exc_info:
        resolve_bound_advertiser_id("advertiser-1", "advertiser-2")
    assert exc_info.value.status_code == 403


def _seed_launch_scope(db_session):
    workspace = Workspace(id=1, name="Tenant", company_code="T001")
    provider = OAuthProviderApp(
        id=1,
        provider="tiktok-business",
        name="TikTok Business",
        client_id="client",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.test/oauth/callback",
    )
    accounts = [
        OAuthAccountTTB(
            id=11,
            workspace_id=1,
            provider_app_id=1,
            alias="Account A",
            access_token_cipher=b"token-a",
            token_fingerprint=b"a" * 32,
        ),
        OAuthAccountTTB(
            id=22,
            workspace_id=1,
            provider_app_id=1,
            alias="Account B",
            access_token_cipher=b"token-b",
            token_fingerprint=b"b" * 32,
        ),
    ]
    landing = WebsiteAdsLandingPage(
        id=101,
        workspace_id=1,
        external_id="manual-101",
        identifier="product-101",
        title="Product",
        landing_url="https://example.test/product",
        is_active=True,
    )
    db_session.add_all([workspace, provider, *accounts, landing])
    db_session.flush()
    campaign = WebsiteAdsCampaign(
        workspace_id=1,
        auth_id=11,
        advertiser_id="advertiser-a",
        landing_page_id=101,
        request_key="shared-request",
        campaign_id="campaign-a",
        name="Existing Campaign",
        local_status="PAUSED",
        operation_status="DISABLE",
    )
    db_session.add(campaign)
    db_session.commit()
    return landing, campaign


def _launch_request(*, advertiser_id: str) -> WebsiteAdLaunchRequest:
    return WebsiteAdLaunchRequest(
        request_key="shared-request",
        advertiser_id=advertiser_id,
        landing_page_id=101,
        campaign_name="New Campaign",
        adgroup_name="Ad Group",
        ad_name="Ad",
        pixel_id="pixel-1",
        identity_type="CUSTOMIZED_USER",
        identity_id="identity-1",
        video_id="video-1",
        ad_text="Shop now",
        daily_budget=50,
        targeting={
            "location_ids": ["6252001"],
            "interest_category_ids": ["interest-1"],
        },
    )


def test_launch_idempotency_never_returns_another_auth_campaign(
    db_session,
    monkeypatch,
):
    _seed_launch_scope(db_session)
    monkeypatch.setattr(
        website_ads_launch,
        "resolve_account_binding",
        lambda db, workspace_id, provider, auth_id: SimpleNamespace(
            advertiser_id="advertiser-b" if int(auth_id) == 22 else "advertiser-a"
        ),
    )

    class PreflightReached(RuntimeError):
        pass

    monkeypatch.setattr(
        website_ads_launch,
        "build_ttb_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(PreflightReached()),
    )

    with pytest.raises(PreflightReached):
        asyncio.run(
            website_ads_launch.launch_website_ad(
                db_session,
                workspace_id=1,
                auth_id=22,
                provider="tiktok-business",
                request=_launch_request(advertiser_id="advertiser-b"),
            )
        )


def test_launch_same_auth_and_scope_returns_only_its_idempotent_campaign(
    db_session,
    monkeypatch,
):
    _, campaign = _seed_launch_scope(db_session)
    monkeypatch.setattr(
        website_ads_launch,
        "resolve_account_binding",
        lambda *args, **kwargs: SimpleNamespace(advertiser_id="advertiser-a"),
    )
    monkeypatch.setattr(
        website_ads_launch,
        "build_ttb_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("idempotent launch must not call TikTok")
        ),
    )

    result = asyncio.run(
        website_ads_launch.launch_website_ad(
            db_session,
            workspace_id=1,
            auth_id=11,
            provider="tiktok-business",
            request=_launch_request(advertiser_id="advertiser-a"),
        )
    )

    assert result == {
        "id": campaign.id,
        "campaign_id": "campaign-a",
        "status": "PAUSED",
        "idempotent": True,
    }


def test_local_reads_and_mutations_are_limited_to_current_advertiser(
    db_session,
    monkeypatch,
):
    _, campaign_a = _seed_launch_scope(db_session)
    campaign_b = WebsiteAdsCampaign(
        workspace_id=1,
        auth_id=11,
        advertiser_id="advertiser-b",
        landing_page_id=101,
        request_key="request-b",
        campaign_id="campaign-b",
        name="Bound Campaign",
        local_status="PAUSED",
        operation_status="DISABLE",
    )
    assets = [
        WebsiteAdsCreativeAsset(
            workspace_id=1,
            auth_id=11,
            advertiser_id="advertiser-a",
            video_id="video-a",
            title="Old advertiser asset",
        ),
        WebsiteAdsCreativeAsset(
            workspace_id=1,
            auth_id=11,
            advertiser_id="advertiser-b",
            video_id="video-b",
            title="Bound advertiser asset",
        ),
    ]
    db_session.add_all([campaign_b, *assets])
    db_session.flush()
    db_session.add_all(
        [
            WebsiteAdsActionLog(
                workspace_id=1,
                auth_id=11,
                actor_type="USER",
                action="OLD_ADVERTISER_ACTION",
                result="SUCCESS",
                request_json={"campaign_local_id": int(campaign_a.id)},
            ),
            WebsiteAdsActionLog(
                workspace_id=1,
                auth_id=11,
                actor_type="USER",
                action="BOUND_ADVERTISER_ACTION",
                result="SUCCESS",
                request_json={"campaign_local_id": int(campaign_b.id)},
            ),
            WebsiteAdsActionLog(
                workspace_id=1,
                auth_id=11,
                actor_type="LEGACY",
                action="UNATTRIBUTED_LEGACY_ACTION",
                result="SUCCESS",
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        website_ads_router,
        "resolve_account_binding",
        lambda *args, **kwargs: SimpleNamespace(advertiser_id="advertiser-b"),
    )

    campaign_result = website_ads_router.list_campaigns(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=11,
        page=1,
        page_size=20,
        start_date=None,
        end_date=None,
        db=db_session,
    )
    assert [item["campaign_id"] for item in campaign_result["items"]] == [
        "campaign-b"
    ]
    asset_result = website_ads_router.list_asset_library(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=11,
        landing_page_id=None,
        db=db_session,
    )
    assert [item["video_id"] for item in asset_result["items"]] == ["video-b"]
    action_result = website_ads_router.list_actions(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=11,
        page=1,
        page_size=30,
        limit=None,
        db=db_session,
    )
    assert [item["action"] for item in action_result["items"]] == [
        "BOUND_ADVERTISER_ACTION"
    ]
    assert action_result["total"] == 1

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            website_ads_router.update_campaign_status(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=11,
                campaign_local_id=int(campaign_a.id),
                body=StatusUpdateRequest(operation_status="ENABLE"),
                db=db_session,
            )
        )
    assert exc_info.value.status_code == 404


def test_launch_preflight_rejects_identity_and_video_from_other_advertiser():
    identity_payload = {
        "data": {
            "identity_list": [
                {
                    "identity_id": "identity-bound",
                    "identity_type": "CUSTOMIZED_USER",
                    "available_status": "AVAILABLE",
                    "can_push_video": True,
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="unavailable for the bound advertiser"):
        website_ads_launch._validate_advertiser_identity(
            identity_payload,
            identity_id="identity-other",
            identity_type="CUSTOMIZED_USER",
            identity_authorized_bc_id=None,
        )

    assert website_ads_launch._advertiser_video_ids(
        {"data": {"list": [{"video_id": "video-bound"}]}}
    ) == {"video-bound"}
    assert "video-other" not in website_ads_launch._advertiser_video_ids(
        {"data": {"list": [{"video_id": "video-bound"}]}}
    )
