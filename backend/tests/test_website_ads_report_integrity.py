from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.data.models.gmv_restructured import GmvProductMetricsHourly, GmvProductOrderEvent
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBAdvertiserStoreLink
from app.data.models.website_ads import (
    WebsiteAdsActionLog,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsConversionGuardState,
    WebsiteAdsCreativeAsset,
    WebsiteAdsDailyReport,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsMetricHourly,
)
from app.data.models.workspaces import Workspace
# The tenant package owns the Website Ads service registration and must finish
# loading before importing monitor internals (matching the production app boot).
from app.features.tenants.ttb.website_ads import schemas as _website_ads_schemas  # noqa: F401
from app.features.tenants.ttb.website_ads import router as website_ads_router
from app.features.tenants.ttb.website_ads.schemas import StatusUpdateRequest
from app.services import (
    website_ads_asset_expansion,
    website_ads_daily_report,
    website_ads_monitor,
)
from app.services import website_ads_execution_lock
from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxGuardActionLease,
    release_guard_action_lease,
)
from app.services.website_ads_daily_report import (
    WebsiteAdsReportIncompleteError,
    generate_campaign_daily_report,
)
from app.services.website_ads_delivery_optimizer import sync_platform_review_results
from app.services.website_ads_conversion_guard import (
    _order_snapshot,
    _source_snapshot,
    evaluate_campaign_conversion_guard,
    resolve_website_ads_store_id,
)
from app.services.website_ads_hermes_planner import _asset_performance
from app.services.website_ads_monitor import run_website_ads_monitor_cycle


class _ImmediateWebsiteAdsLock:
    def __init__(self, **kwargs):
        self.key = kwargs["key"]
        self.acquired = False
        self.lost = False

    def acquire(self, **_kwargs):
        self.acquired = True
        return True

    def release(self):
        self.acquired = False
        return True


@pytest.fixture(autouse=True)
def _stub_website_ads_execution_lock(monkeypatch):
    monkeypatch.setattr(
        website_ads_execution_lock,
        "_LOCK_FACTORY",
        _ImmediateWebsiteAdsLock,
    )


def _next_id(db_session, model) -> int:
    value = db_session.execute(select(func.coalesce(func.max(model.id), 0))).scalar_one()
    return int(value) + 1


def _seed_scope(db_session) -> tuple[WebsiteAdsCampaign, WebsiteAdsAd]:
    workspace = Workspace(
        id=_next_id(db_session, Workspace),
        name="Website Ads Integrity",
        company_code="web-integrity",
    )
    provider = OAuthProviderApp(
        id=_next_id(db_session, OAuthProviderApp),
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add_all([workspace, provider])
    db_session.flush()
    account = OAuthAccountTTB(
        id=_next_id(db_session, OAuthAccountTTB),
        workspace_id=workspace.id,
        provider_app_id=provider.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    landing_page = WebsiteAdsLandingPage(
        workspace_id=workspace.id,
        external_id="landing-1",
        identifier="landing-1",
        title="Landing",
        landing_url="https://example.com/product",
    )
    db_session.add_all([account, landing_page])
    db_session.flush()
    campaign = WebsiteAdsCampaign(
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="advertiser-1",
        landing_page_id=landing_page.id,
        request_key="request-1",
        campaign_id="campaign-1",
        name="Campaign",
        local_status="ACTIVE",
        operation_status="ENABLE",
    )
    db_session.add(campaign)
    db_session.flush()
    ad_group = WebsiteAdsAdGroup(
        campaign_local_id=campaign.id,
        adgroup_id="adgroup-1",
        name="Ad group",
        pixel_id="pixel-1",
        targeting_json={},
        budget_mode="BUDGET_MODE_DAY",
        budget=Decimal("50"),
        bid_type="BID_TYPE_NO_BID",
        schedule_start_time="2026-07-17 00:00:00",
        operation_status="ENABLE",
    )
    db_session.add(ad_group)
    db_session.flush()
    ad = WebsiteAdsAd(
        campaign_local_id=campaign.id,
        adgroup_local_id=ad_group.id,
        ad_id="ad-1",
        ad_id_v2="ad-v2-1",
        name="Ad",
        video_id="video-1",
        identity_type="CUSTOMIZED_USER",
        identity_id="identity-1",
        landing_page_url="https://example.com/product",
        operation_status="ENABLE",
        guard_enabled=True,
        max_unprofitable_spend=Decimal("1"),
    )
    db_session.add(ad)
    db_session.flush()
    return campaign, ad


def _seed_additional_ads(
    db_session,
    *,
    campaign: WebsiteAdsCampaign,
    template: WebsiteAdsAd,
    count: int,
) -> list[WebsiteAdsAd]:
    ads: list[WebsiteAdsAd] = []
    for index in range(2, count + 2):
        ad = WebsiteAdsAd(
            campaign_local_id=campaign.id,
            adgroup_local_id=template.adgroup_local_id,
            ad_id=f"ad-{index}",
            ad_id_v2=f"ad-v2-{index}",
            name=f"Ad {index}",
            video_id=f"video-{index}",
            identity_type="CUSTOMIZED_USER",
            identity_id="identity-1",
            landing_page_url="https://example.com/product",
            operation_status="ENABLE",
            guard_enabled=True,
            max_unprofitable_spend=Decimal("1"),
        )
        db_session.add(ad)
        ads.append(ad)
    db_session.flush()
    return ads


def test_platform_review_snapshot_accepts_complete_multi_batch_out_of_order(
    db_session,
):
    campaign, first_ad = _seed_scope(db_session)
    ads = [
        first_ad,
        *_seed_additional_ads(
            db_session,
            campaign=campaign,
            template=first_ad,
            count=51,
        ),
    ]
    requested_batches: list[list[str]] = []

    class Api:
        async def get_campaigns(self, advertiser_id, campaign_ids):
            assert advertiser_id == campaign.advertiser_id
            assert campaign_ids == [str(campaign.campaign_id)]
            return {
                "data": {
                    "list": [
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "operation_status": "ENABLE",
                            "secondary_status": "CAMPAIGN_STATUS_ENABLE",
                        }
                    ]
                }
            }

        async def get_ads(self, advertiser_id, ad_ids):
            assert advertiser_id == campaign.advertiser_id
            requested_batches.append(list(ad_ids))
            return {
                "data": {
                    "list": [
                        {
                            "ad_id": ad_id,
                            "operation_status": "DISABLE",
                            "secondary_status": "STATUS_DELIVERY_OK",
                        }
                        for ad_id in reversed(ad_ids)
                    ]
                }
            }

    result = asyncio.run(
        sync_platform_review_results(
            db_session,
            api=Api(),
            advertiser_id=campaign.advertiser_id,
            ads=ads,
        )
    )

    assert [len(batch) for batch in requested_batches] == [50, 2]
    assert result == {
        "status": "COMPLETE",
        "snapshot_complete": True,
        "requested": 52,
        "returned": 52,
        "batches_fetched": 2,
        "missing_ad_ids": [],
        "unexpected_ad_ids": [],
        "requested_campaigns": 1,
        "returned_campaigns": 1,
        "campaign_batches_fetched": 1,
        "missing_campaign_ids": [],
        "unexpected_campaign_ids": [],
        "checked": 52,
        "rejected": 0,
        "terminal_ads": 0,
        "terminal_campaigns": 0,
    }
    db_session.expire_all()
    assert {
        str(status)
        for status in db_session.scalars(
            select(WebsiteAdsAd.operation_status).where(
                WebsiteAdsAd.campaign_local_id == campaign.id
            )
        ).all()
    } == {"DISABLE"}


def test_platform_review_snapshot_missing_id_is_incomplete_and_atomic(
    db_session,
):
    campaign, first_ad = _seed_scope(db_session)
    second_ad = _seed_additional_ads(
        db_session,
        campaign=campaign,
        template=first_ad,
        count=1,
    )[0]
    first_ad.raw_json = {"snapshot": "old"}
    db_session.add(first_ad)
    db_session.commit()

    class Api:
        async def get_campaigns(self, advertiser_id, campaign_ids):
            assert advertiser_id == campaign.advertiser_id
            assert campaign_ids == [str(campaign.campaign_id)]
            return {
                "data": {
                    "list": [
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "operation_status": "ENABLE",
                        }
                    ]
                }
            }

        async def get_ads(self, advertiser_id, ad_ids):
            assert set(ad_ids) == {str(first_ad.ad_id), str(second_ad.ad_id)}
            return {
                "data": {
                    "list": [
                        {
                            "ad_id": str(first_ad.ad_id),
                            "operation_status": "DISABLE",
                            "secondary_status": "STATUS_DELIVERY_OK",
                        }
                    ]
                }
            }

    result = asyncio.run(
        sync_platform_review_results(
            db_session,
            api=Api(),
            advertiser_id=campaign.advertiser_id,
            ads=[first_ad, second_ad],
        )
    )

    assert result["status"] == "INCOMPLETE"
    assert result["snapshot_complete"] is False
    assert result["requested"] == 2
    assert result["returned"] == 1
    assert result["missing_ad_ids"] == [str(second_ad.ad_id)]
    assert result["unexpected_ad_ids"] == []
    assert result["checked"] == 0
    assert result["rejected"] == 0
    db_session.expire_all()
    assert db_session.get(WebsiteAdsAd, first_ad.id).operation_status == "ENABLE"
    assert db_session.get(WebsiteAdsAd, first_ad.id).raw_json == {"snapshot": "old"}
    assert db_session.get(WebsiteAdsAd, second_ad.id).operation_status == "ENABLE"
    assert db_session.query(WebsiteAdsActionLog).count() == 0


def test_platform_review_duplicate_local_report_id_is_incomplete(db_session):
    campaign, first_ad = _seed_scope(db_session)
    second_ad = _seed_additional_ads(
        db_session,
        campaign=campaign,
        template=first_ad,
        count=1,
    )[0]
    second_ad.ad_id_v2 = str(first_ad.ad_id_v2)
    db_session.add(second_ad)
    db_session.commit()

    class Api:
        async def get_ads(self, *_args, **_kwargs):
            raise AssertionError("duplicate local IDs must fail before the API")

    result = asyncio.run(
        sync_platform_review_results(
            db_session,
            api=Api(),
            advertiser_id=campaign.advertiser_id,
            ads=[first_ad, second_ad],
        )
    )

    assert result["status"] == "INCOMPLETE"
    assert result["snapshot_complete"] is False
    assert result["duplicate_local_report_ids"] == [str(first_ad.ad_id_v2)]
    assert result["checked"] == 0


def test_daily_refresh_rejects_duplicate_local_report_id_before_api(
    db_session,
):
    campaign, first_ad = _seed_scope(db_session)
    second_ad = _seed_additional_ads(
        db_session,
        campaign=campaign,
        template=first_ad,
        count=1,
    )[0]
    second_ad.ad_id_v2 = str(first_ad.ad_id_v2)
    stale = WebsiteAdsMetricHourly(
        workspace_id=campaign.workspace_id,
        advertiser_id=campaign.advertiser_id,
        ad_local_id=first_ad.id,
        stat_hour=datetime(2026, 7, 17, 9),
        spend=Decimal("25"),
    )
    db_session.add_all([second_ad, stale])
    db_session.commit()

    class Api:
        async def report_ads(self, *_args, **_kwargs):
            raise AssertionError("duplicate report IDs must fail before the API")

    result = asyncio.run(
        website_ads_daily_report._refresh_campaign_day_metrics(
            db_session,
            api=Api(),
            campaign=campaign,
            report_date=date(2026, 7, 17),
        )
    )

    assert result["complete"] is False
    assert result["duplicate_local_report_ids"] == [str(first_ad.ad_id_v2)]
    db_session.refresh(stale)
    assert stale.spend == Decimal("25")


def test_platform_review_duplicate_official_id_is_incomplete_and_atomic(
    db_session,
):
    campaign, ad = _seed_scope(db_session)

    class Api:
        async def get_campaigns(self, advertiser_id, campaign_ids):
            assert advertiser_id == campaign.advertiser_id
            assert campaign_ids == [str(campaign.campaign_id)]
            return {
                "data": {
                    "list": [
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "operation_status": "ENABLE",
                        }
                    ]
                }
            }

        async def get_ads(self, advertiser_id, ad_ids):
            assert advertiser_id == campaign.advertiser_id
            assert ad_ids == [str(ad.ad_id)]
            row = {
                "ad_id": str(ad.ad_id),
                "operation_status": "DISABLE",
            }
            return {"data": {"list": [row, dict(row)]}}

    result = asyncio.run(
        sync_platform_review_results(
            db_session,
            api=Api(),
            advertiser_id=campaign.advertiser_id,
            ads=[ad],
        )
    )

    assert result["status"] == "INCOMPLETE"
    assert result["snapshot_complete"] is False
    assert result["duplicate_official_ad_ids"] == [str(ad.ad_id)]
    assert result["checked"] == 0
    db_session.refresh(ad)
    assert ad.operation_status == "ENABLE"


def test_monitor_and_daily_report_fail_closed_on_the_same_lock_key(db_session):
    requested_keys: list[str] = []

    class BusyLock:
        def __init__(self, **kwargs):
            requested_keys.append(str(kwargs["key"]))
            self.acquired = False
            self.lost = False

        def acquire(self, **_kwargs):
            return False

        def release(self):
            raise AssertionError("an unacquired lock must not be released")

    monitor_result = asyncio.run(
        website_ads_monitor.run_website_ads_monitor_cycle(
            db_session,
            _lock_factory=BusyLock,
        )
    )
    daily_result = asyncio.run(
        website_ads_daily_report.run_website_ads_daily_report_cycle(
            db_session,
            _lock_factory=BusyLock,
        )
    )
    expansion_result = asyncio.run(
        website_ads_asset_expansion.run_website_ads_asset_expansion_cycle(
            db_session,
            _lock_factory=BusyLock,
        )
    )

    assert len(set(requested_keys)) == 1
    assert monitor_result["status"] == "SKIPPED"
    assert monitor_result["reason"] == "EXECUTION_LOCK_UNAVAILABLE"
    assert monitor_result["decision_holds"] == 1
    assert daily_result["status"] == "SKIPPED"
    assert daily_result["reason"] == "EXECUTION_LOCK_UNAVAILABLE"
    assert daily_result["decision"] == "HOLD"
    assert expansion_result["status"] == "SKIPPED"
    assert expansion_result["reason"] == "EXECUTION_LOCK_UNAVAILABLE"
    assert expansion_result["decision"] == "HOLD"


def test_execution_fence_redelivery_advances_generation_and_rejects_old_release(
    db_session,
):
    first_owner = ""
    first_generation = 0
    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="same_delivery",
        workspace_id=1,
        lock_factory=_ImmediateWebsiteAdsLock,
    ) as first:
        assert first is not None
        first_owner = first.owner_token
        first_generation = first.fencing_token

    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="same_delivery",
        workspace_id=1,
        lock_factory=_ImmediateWebsiteAdsLock,
    ) as replacement:
        assert replacement is not None
        assert replacement.fencing_token > first_generation
        assert (
            release_guard_action_lease(
                db_session,
                lease_name=replacement.lease_name,
                owner_token=first_owner,
                fencing_token=first_generation,
            )
            is False
        )
        db_session.commit()
        db_session.expire_all()
        row = db_session.get(GMVMaxGuardActionLease, replacement.lease_name)
        assert row is not None
        assert row.owner_token == replacement.owner_token
        assert row.fencing_token == replacement.fencing_token


def test_execution_lease_discards_pending_business_writes_at_outer_boundary(
    db_session,
):
    campaign, _ad = _seed_scope(db_session)
    db_session.commit()
    db_session.add(WebsiteAdsActionLog(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        actor_type="TEST",
        action="PRE_LEASE_PENDING",
        result="PENDING",
    ))

    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="clean_boundary",
        workspace_id=int(campaign.workspace_id),
        lock_factory=_ImmediateWebsiteAdsLock,
    ) as lease:
        assert lease is not None
        assert (
            db_session.scalar(
                select(func.count()).select_from(WebsiteAdsActionLog).where(
                    WebsiteAdsActionLog.action == "PRE_LEASE_PENDING",
                )
            )
            == 0
        )


def test_durable_fence_catches_supersede_after_redis_verify_and_rolls_back(
    db_session,
):
    from app.data.db import SessionLocal
    from app.features.tenants.ttb.gmv_max.control import GMVMaxGuardActionLease

    campaign, _ad = _seed_scope(db_session)
    db_session.commit()
    workspace_id = int(campaign.workspace_id)
    replacement_owner = "replacement-owner"

    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="superseded_after_verify",
        workspace_id=workspace_id,
        lock_factory=_ImmediateWebsiteAdsLock,
    ) as lease:
        assert lease is not None
        # Redis can still report the cached owner as healthy immediately
        # before a replacement database generation is published.
        lease.assert_active()
        with SessionLocal() as replacement_db:
            row = replacement_db.get(GMVMaxGuardActionLease, lease.lease_name)
            assert row is not None
            row.owner_token = replacement_owner
            row.fencing_token = int(row.fencing_token) + 1
            row.acquired_at = datetime.now(timezone.utc).replace(tzinfo=None)
            row.expires_at = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(minutes=30)
            )
            replacement_generation = int(row.fencing_token)
            replacement_db.add(row)
            replacement_db.commit()

        db_session.add(WebsiteAdsActionLog(
            workspace_id=workspace_id,
            auth_id=int(campaign.auth_id),
            actor_type="TEST",
            action="MUST_ROLL_BACK",
            result="PENDING",
        ))
        with pytest.raises(
            website_ads_execution_lock.WebsiteAdsExecutionLockLost
        ):
            website_ads_execution_lock.assert_website_ads_execution_lock(
                db_session,
                required=True,
            )
        db_session.rollback()

    assert (
        db_session.scalar(
            select(func.count()).select_from(WebsiteAdsActionLog).where(
                WebsiteAdsActionLog.action == "MUST_ROLL_BACK",
            )
        )
        == 0
    )
    db_session.expire_all()
    row = db_session.get(
        GMVMaxGuardActionLease,
        website_ads_execution_lock._durable_lease_name(),
    )
    assert row is not None
    assert row.owner_token == replacement_owner
    assert int(row.fencing_token) == replacement_generation


def test_monitor_returns_hold_if_lock_ownership_is_lost(db_session):
    class LostAfterFirstCheckLock:
        def __init__(self, **_kwargs):
            self._held = False
            self._checks = 0

        @property
        def acquired(self):
            self._checks += 1
            return self._held and self._checks == 1

        @property
        def lost(self):
            return self._checks > 1

        def acquire(self, **_kwargs):
            self._held = True
            return True

        def release(self):
            self._held = False
            return True

    result = asyncio.run(
        website_ads_monitor.run_website_ads_monitor_cycle(
            db_session,
            _lock_factory=LostAfterFirstCheckLock,
        )
    )

    assert result["status"] == "HOLD"
    assert result["reason"] == "EXECUTION_LOCK_LOST"
    assert result["decision_holds"] == 1


def test_manual_ad_status_returns_busy_without_calling_official_api(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    db_session.commit()

    class BusyLock:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False

        def acquire(self, **_kwargs):
            return False

        def release(self):
            raise AssertionError("unacquired lock must not be released")

    monkeypatch.setattr(website_ads_execution_lock, "_LOCK_FACTORY", BusyLock)
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *_args, **_kwargs: str(campaign.advertiser_id),
    )

    async def _unexpected_api(*_args, **_kwargs):
        raise AssertionError("busy mutation must not call the official API")

    monkeypatch.setattr(website_ads_router, "_with_api", _unexpected_api)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            website_ads_router.update_ad_status(
                workspace_id=int(campaign.workspace_id),
                provider="tiktok-business",
                auth_id=int(campaign.auth_id),
                ad_local_id=int(ad.id),
                body=StatusUpdateRequest(
                    operation_status="DISABLE",
                    reason="test",
                ),
                db=db_session,
            )
        )

    assert exc_info.value.status_code == 423
    db_session.refresh(ad)
    assert ad.operation_status == "ENABLE"


def test_manual_campaign_status_stops_remote_sequence_after_lock_loss(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    adgroup = db_session.get(WebsiteAdsAdGroup, int(ad.adgroup_local_id))
    campaign.local_status = "PAUSED"
    campaign.operation_status = "DISABLE"
    adgroup.operation_status = "DISABLE"
    ad.operation_status = "DISABLE"
    db_session.add_all([campaign, adgroup, ad])
    db_session.commit()

    locks = []

    class VerifiableLock:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False
            locks.append(self)

        def acquire(self, **_kwargs):
            self.acquired = True
            return True

        def verify_ownership(self):
            return self.acquired and not self.lost

        def release(self):
            self.acquired = False
            return True

    calls: list[str] = []

    class Api:
        async def update_campaign_status(self, *_args, **_kwargs):
            calls.append("campaign")
            # Simulate ownership changing while this awaited API request was
            # in flight. The next official mutation must never start.
            locks[0].lost = True
            return {}

        async def update_adgroup_status(self, *_args, **_kwargs):
            calls.append("adgroup")
            return {}

        async def update_ad_status(self, *_args, **_kwargs):
            calls.append("ad")
            return {}

    async def _with_fake_api(_db, _workspace_id, _provider, _auth_id, callback):
        return await callback(Api())

    monkeypatch.setattr(
        website_ads_execution_lock,
        "_LOCK_FACTORY",
        VerifiableLock,
    )
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *_args, **_kwargs: str(campaign.advertiser_id),
    )
    monkeypatch.setattr(website_ads_router, "_with_api", _with_fake_api)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            website_ads_router.update_campaign_status(
                workspace_id=int(campaign.workspace_id),
                provider="tiktok-business",
                auth_id=int(campaign.auth_id),
                campaign_local_id=int(campaign.id),
                body=StatusUpdateRequest(
                    operation_status="ENABLE",
                    reason="test",
                ),
                db=db_session,
            )
        )

    assert exc_info.value.status_code == 409
    assert calls == ["campaign"]
    db_session.refresh(campaign)
    db_session.refresh(adgroup)
    db_session.refresh(ad)
    assert campaign.local_status == "PAUSED"
    assert campaign.operation_status == "DISABLE"
    assert adgroup.operation_status == "DISABLE"
    assert ad.operation_status == "DISABLE"


def test_manual_campaign_status_reloads_groups_and_ads_after_lock_wait(
    db_session,
    monkeypatch,
):
    campaign, existing_ad = _seed_scope(db_session)
    existing_group = db_session.get(
        WebsiteAdsAdGroup,
        int(existing_ad.adgroup_local_id),
    )
    campaign.local_status = "PAUSED"
    campaign.operation_status = "DISABLE"
    existing_group.operation_status = "DISABLE"
    existing_ad.operation_status = "DISABLE"
    db_session.add_all([campaign, existing_group, existing_ad])
    db_session.commit()

    campaign_id = int(campaign.id)
    workspace_id = int(campaign.workspace_id)
    auth_id = int(campaign.auth_id)
    advertiser_id = str(campaign.advertiser_id)
    inserted: dict[str, int] = {}

    class LockThatWaitedForAnotherWriter:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False

        def acquire(self, **_kwargs):
            new_group = WebsiteAdsAdGroup(
                campaign_local_id=campaign_id,
                adgroup_id="adgroup-created-during-wait",
                name="Created during wait",
                pixel_id="pixel-1",
                targeting_json={},
                budget_mode="BUDGET_MODE_DAY",
                budget=Decimal("20"),
                bid_type="BID_TYPE_NO_BID",
                schedule_start_time="2026-07-17 00:00:00",
                operation_status="DISABLE",
            )
            db_session.add(new_group)
            db_session.flush()
            new_ad = WebsiteAdsAd(
                campaign_local_id=campaign_id,
                adgroup_local_id=int(new_group.id),
                ad_id="ad-created-during-wait",
                ad_id_v2="ad-v2-created-during-wait",
                name="Created during wait",
                video_id="video-created-during-wait",
                identity_type="CUSTOMIZED_USER",
                identity_id="identity-1",
                landing_page_url="https://example.com/product",
                operation_status="DISABLE",
                guard_enabled=True,
            )
            db_session.add(new_ad)
            db_session.commit()
            inserted["group_id"] = int(new_group.id)
            inserted["ad_id"] = int(new_ad.id)
            self.acquired = True
            return True

        def verify_ownership(self):
            return self.acquired and not self.lost

        def release(self):
            self.acquired = False
            return True

    calls: list[tuple[str, list[str]]] = []

    class Api:
        async def update_campaign_status(
            self,
            _advertiser_id,
            remote_ids,
            _status,
        ):
            calls.append(("campaign", list(remote_ids)))
            return {}

        async def update_adgroup_status(
            self,
            _advertiser_id,
            remote_ids,
            _status,
        ):
            calls.append(("adgroups", list(remote_ids)))
            return {}

        async def update_ad_status(
            self,
            _advertiser_id,
            remote_ids,
            _status,
        ):
            calls.append(("ads", list(remote_ids)))
            return {}

    async def _with_fake_api(_db, _workspace_id, _provider, _auth_id, callback):
        return await callback(Api())

    monkeypatch.setattr(
        website_ads_execution_lock,
        "_LOCK_FACTORY",
        LockThatWaitedForAnotherWriter,
    )
    monkeypatch.setattr(
        website_ads_router,
        "_request_advertiser_id",
        lambda *_args, **_kwargs: advertiser_id,
    )
    monkeypatch.setattr(website_ads_router, "_with_api", _with_fake_api)

    result = asyncio.run(
        website_ads_router.update_campaign_status(
            workspace_id=workspace_id,
            provider="tiktok-business",
            auth_id=auth_id,
            campaign_local_id=campaign_id,
            body=StatusUpdateRequest(
                operation_status="ENABLE",
                reason="test",
            ),
            db=db_session,
        )
    )

    assert result["operation_status"] == "ENABLE"
    assert calls[0] == ("campaign", [str(campaign.campaign_id)])
    assert set(calls[1][1]) == {
        str(existing_group.adgroup_id),
        "adgroup-created-during-wait",
    }
    assert set(calls[2][1]) == {
        str(existing_ad.ad_id),
        "ad-created-during-wait",
    }
    assert (
        db_session.get(WebsiteAdsAdGroup, inserted["group_id"]).operation_status
        == "ENABLE"
    )
    assert (
        db_session.get(WebsiteAdsAd, inserted["ad_id"]).operation_status
        == "ENABLE"
    )


def test_replacement_stops_before_remote_create_when_lock_is_lost_during_await(
    db_session,
    monkeypatch,
):
    campaign, template_ad = _seed_scope(db_session)
    group = db_session.get(
        WebsiteAdsAdGroup,
        int(template_ad.adgroup_local_id),
    )
    product = db_session.get(
        WebsiteAdsLandingPage,
        int(campaign.landing_page_id),
    )
    plan = WebsiteAdsMediaPlan(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        landing_page_id=int(product.id),
        campaign_local_id=int(campaign.id),
        name="Plan",
        status="ACTIVE",
        daily_budget=Decimal("50"),
    )
    asset = WebsiteAdsCreativeAsset(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        landing_page_id=int(product.id),
        video_id="replacement-video",
        title="Replacement",
        analysis_status="READY",
        hermes_analysis_json={"opening_hook": "Hook"},
        is_active=True,
    )
    db_session.add_all([plan, asset])
    db_session.commit()

    locks = []

    class VerifiableLock:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False
            locks.append(self)

        def acquire(self, **_kwargs):
            self.acquired = True
            return True

        def verify_ownership(self):
            return self.acquired and not self.lost

        def release(self):
            self.acquired = False
            return True

    calls: list[str] = []

    class Api:
        async def list_all_identities(self, *_args, **_kwargs):
            calls.append("identities")
            return {}

        async def suggest_video_covers(self, *_args, **_kwargs):
            calls.append("covers")
            locks[0].lost = True
            return {"data": {"list": [{"id": "cover-1"}]}}

        async def create_ads(self, *_args, **_kwargs):
            calls.append("create_ads")
            return {}

        async def update_ad_status(self, *_args, **_kwargs):
            calls.append("update_ad_status")
            return {}

    monkeypatch.setattr(
        website_ads_asset_expansion,
        "assess_website_ads_creative_policy",
        lambda *_args, **_kwargs: {
            "eligible_for_automatic_launch": True,
        },
    )
    monkeypatch.setattr(
        website_ads_asset_expansion,
        "select_tiktok_video_identity",
        lambda *_args, **_kwargs: {
            "identity_type": "CUSTOMIZED_USER",
            "identity_id": "identity-1",
        },
    )

    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="test_replacement",
        workspace_id=int(campaign.workspace_id),
        lock_factory=VerifiableLock,
    ):
        with pytest.raises(
            website_ads_execution_lock.WebsiteAdsExecutionLockLost
        ):
            asyncio.run(
                website_ads_asset_expansion._create_ad_for_asset(
                    db_session,
                    api=Api(),
                    plan=plan,
                    campaign=campaign,
                    product=product,
                    group=group,
                    asset=asset,
                    expansion_mode="TEST",
                    score=1.0,
                    require_execution_lease=True,
                )
            )

    db_session.rollback()
    assert calls == ["identities", "covers"]
    assert (
        db_session.scalar(
            select(func.count()).select_from(WebsiteAdsAd).where(
                WebsiteAdsAd.video_id == "replacement-video",
            )
        )
        == 0
    )


def test_daily_report_rolls_back_if_ownership_is_lost_before_commit(
    db_session,
):
    campaign, _ad = _seed_scope(db_session)
    db_session.commit()

    class LoseBeforeCommitLock:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False
            self.verifications = 0

        def acquire(self, **_kwargs):
            self.acquired = True
            return True

        def verify_ownership(self):
            self.verifications += 1
            if self.verifications >= 4:
                self.lost = True
            return self.acquired and not self.lost

        def release(self):
            self.acquired = False
            return True

    class Api:
        async def report_ads(self, *_args, **_kwargs):
            return {
                "data": {"list": []},
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": 0,
                    "source_pages": [{}],
                },
            }

    with website_ads_execution_lock.website_ads_execution_lease(
        db_session,
        operation="test_daily_report",
        workspace_id=int(campaign.workspace_id),
        lock_factory=LoseBeforeCommitLock,
    ):
        with pytest.raises(
            website_ads_execution_lock.WebsiteAdsExecutionLockLost
        ):
            asyncio.run(
                generate_campaign_daily_report(
                    db_session,
                    api=Api(),
                    campaign=campaign,
                    report_date=date(2026, 7, 17),
                    final=True,
                    require_execution_lease=True,
                )
            )

    db_session.rollback()
    assert db_session.query(WebsiteAdsDailyReport).count() == 0
    assert db_session.query(WebsiteAdsActionLog).count() == 0


def test_asset_expansion_holds_when_ownership_is_lost_during_await(
    db_session,
    monkeypatch,
):
    campaign, _ad = _seed_scope(db_session)
    asset = WebsiteAdsCreativeAsset(
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
        landing_page_id=int(campaign.landing_page_id),
        video_id="asset-expansion-lock-loss",
        title="Expansion lock loss",
        analysis_status="READY",
        auto_launch_status="PENDING",
        is_active=True,
    )
    db_session.add(asset)
    db_session.commit()

    locks = []

    class VerifiableLock:
        def __init__(self, **_kwargs):
            self.acquired = False
            self.lost = False
            locks.append(self)

        def acquire(self, **_kwargs):
            self.acquired = True
            return True

        def verify_ownership(self):
            return self.acquired and not self.lost

        def release(self):
            self.acquired = False
            return True

    async def _lose_during_review(_db, _asset):
        locks[0].lost = True
        return {"status": "WAITING_HERMES", "created": []}

    monkeypatch.setattr(
        website_ads_asset_expansion.settings,
        "WEBSITE_ADS_ASSET_EXPANSION_ENABLED",
        True,
    )
    monkeypatch.setattr(
        website_ads_asset_expansion,
        "_expand_asset",
        _lose_during_review,
    )

    result = asyncio.run(
        website_ads_asset_expansion.run_website_ads_asset_expansion_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
            _lock_factory=VerifiableLock,
        )
    )

    assert result["status"] == "HOLD"
    assert result["reason"] == "EXECUTION_LOCK_LOST"
    assert result["decision"] == "HOLD"


def test_monitor_holds_all_decisions_when_official_ad_snapshot_is_incomplete(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    db_session.commit()
    calls: list[str] = []

    class Api:
        async def get_campaigns(self, advertiser_id, campaign_ids):
            calls.append("get_campaigns")
            assert advertiser_id == campaign.advertiser_id
            assert campaign_ids == [str(campaign.campaign_id)]
            return {
                "data": {
                    "list": [
                        {
                            "campaign_id": str(campaign.campaign_id),
                            "operation_status": "ENABLE",
                        }
                    ]
                }
            }

        async def get_ads(self, advertiser_id, ad_ids):
            calls.append("get_ads")
            assert advertiser_id == campaign.advertiser_id
            assert ad_ids == [str(ad.ad_id)]
            return {"data": {"list": []}}

        async def report_ads(self, *args, **kwargs):
            calls.append("report_ads")
            return {"data": {"list": []}}

        async def update_ad_status(self, *args, **kwargs):
            calls.append("pause")
            return {}

        async def aclose(self):
            calls.append("aclose")

    api = Api()
    monkeypatch.setattr(
        website_ads_monitor,
        "build_ttb_client",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(website_ads_monitor, "TikTokWebsiteAdsClient", lambda _client: api)

    result = asyncio.run(
        run_website_ads_monitor_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
        )
    )

    assert calls == ["get_campaigns", "get_ads", "aclose"]
    assert result["incomplete_scopes"] == 1
    assert result["incomplete_ad_snapshots"] == 1
    assert result["decision_holds"] == 1
    assert result["report_days"] == 0
    assert result["reviewed"] == 0
    assert result["paused"] == 0
    assert result["replacement_ads"] == 0
    assert result["groups_scaled"] == 0
    assert result["cross_channel_checked"] == 0
    assert result["errors"][0]["stage"] == "AD_SNAPSHOT_INCOMPLETE"
    assert result["errors"][0]["missing_ad_ids"] == [str(ad.ad_id)]
    db_session.refresh(ad)
    assert ad.operation_status == "ENABLE"
    assert ad.last_checked_at is None


def test_monitor_retires_deleted_campaign_before_reports_or_decisions(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    ad_group = db_session.get(WebsiteAdsAdGroup, int(ad.adgroup_local_id))
    db_session.commit()
    calls: list[str] = []

    class Api:
        async def get_campaigns(self, advertiser_id, campaign_ids):
            calls.append("get_campaigns")
            assert advertiser_id == campaign.advertiser_id
            assert campaign_ids == [str(campaign.campaign_id)]
            return {
                "data": {
                    "list": [
                        {
                            "advertiser_id": advertiser_id,
                            "campaign_id": str(campaign.campaign_id),
                            "operation_status": "DISABLE",
                            "secondary_status": "CAMPAIGN_STATUS_DELETE",
                        }
                    ]
                }
            }

        async def get_ads(self, advertiser_id, ad_ids):
            calls.append("get_ads")
            assert advertiser_id == campaign.advertiser_id
            assert ad_ids == [str(ad.ad_id)]
            return {
                "data": {
                    "list": [
                        {
                            "advertiser_id": advertiser_id,
                            "campaign_id": str(campaign.campaign_id),
                            "ad_id": str(ad.ad_id),
                            # TikTok can retain ENABLE here even though the
                            # parent campaign is terminal.
                            "operation_status": "ENABLE",
                            "secondary_status": "AD_STATUS_CAMPAIGN_DELETE",
                        }
                    ]
                }
            }

        async def report_ads(self, *args, **kwargs):
            raise AssertionError("terminal campaigns must not reach reporting")

        async def update_ad_status(self, *args, **kwargs):
            raise AssertionError("terminal campaigns must not reach mutations")

        async def aclose(self):
            calls.append("aclose")

    api = Api()
    monkeypatch.setattr(
        website_ads_monitor,
        "build_ttb_client",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        website_ads_monitor,
        "TikTokWebsiteAdsClient",
        lambda _client: api,
    )

    result = asyncio.run(
        run_website_ads_monitor_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
        )
    )

    assert calls == ["get_campaigns", "get_ads", "aclose"]
    assert result["scopes"] == 1
    assert result["ads"] == 1
    assert result["audit_checked"] == 1
    assert result["terminal_ads"] == 1
    assert result["terminal_campaigns"] == 1
    assert result["report_days"] == 0
    assert result["reviewed"] == 0
    assert result["paused"] == 0
    assert result["replacement_ads"] == 0
    assert result["groups_scaled"] == 0
    assert result["cross_channel_checked"] == 0
    assert result["errors"] == []

    db_session.expire_all()
    assert db_session.get(WebsiteAdsCampaign, campaign.id).local_status == "DELETED"
    assert db_session.get(WebsiteAdsCampaign, campaign.id).operation_status == "DELETE"
    assert db_session.get(WebsiteAdsAdGroup, ad_group.id).operation_status == "DELETE"
    retired_ad = db_session.get(WebsiteAdsAd, ad.id)
    assert retired_ad.operation_status == "DELETE"
    assert retired_ad.guard_enabled is False
    assert db_session.query(WebsiteAdsActionLog).count() == 0


def test_website_hour_parser_never_fabricates_missing_or_wrong_day():
    assert website_ads_monitor._parse_hour(None, expected_day="2026-07-17") is None
    assert website_ads_monitor._parse_hour(
        "2026-07-17 12:30:00",
        expected_day="2026-07-17",
    ) is None
    assert website_ads_monitor._parse_hour(
        "2026-07-18 12:00:00",
        expected_day="2026-07-17",
    ) is None
    assert website_ads_daily_report._parse_hour(None, date(2026, 7, 17)) is None
    assert website_ads_daily_report._parse_hour(
        "2026-07-18 12:00:00",
        date(2026, 7, 17),
    ) is None
    assert website_ads_daily_report._parse_hour(
        "2026-07-17 12:00:00",
        date(2026, 7, 17),
    ) == datetime(2026, 7, 17, 12)


def test_monitor_holds_entire_scope_when_any_current_report_hour_is_invalid(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    db_session.commit()
    decision_calls: list[str] = []

    class Api:
        async def report_ads(self, *args, **kwargs):
            return {
                "data": {
                    "list": [
                        {
                            "dimensions": {
                                "ad_id_v2": ad.ad_id_v2,
                                "stat_time_hour": "2026-07-17 12:00:00",
                            },
                            "metrics": {
                                "spend": "10",
                                "impressions": "200",
                                "clicks": "0",
                            },
                        },
                        {
                            "dimensions": {
                                "ad_id_v2": ad.ad_id_v2,
                                "stat_time_hour": "2026-07-18 00:00:00",
                            },
                            "metrics": {"spend": "999"},
                        },
                        {
                            "dimensions": {
                                "ad_id_v2": "escaped-ad",
                                "stat_time_hour": "2026-07-17 13:00:00",
                            },
                            "metrics": {"spend": "999"},
                        },
                    ]
                },
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": 3,
                    "source_pages": [{}],
                },
            }

        async def update_ad_status(self, *args, **kwargs):
            decision_calls.append("pause")
            return {}

        async def aclose(self):
            return None

    async def _audit(*args, **kwargs):
        return {"checked": 1, "rejected": 0, "snapshot_complete": True}

    async def _conversion_guard(*args, **kwargs):
        decision_calls.append("conversion_guard")
        return {"status": "DATA_HOLD"}

    async def _review(*args, **kwargs):
        decision_calls.append("ad_guard")
        return {"decision": "PAUSE", "reason": "test"}

    async def _race(*args, **kwargs):
        decision_calls.append("group_racing")
        return {"status": "SCALED"}

    async def _backfill(*args, **kwargs):
        decision_calls.append("creative_replacement")
        return {"created": 1, "errors": []}

    api = Api()
    monkeypatch.setattr(website_ads_monitor, "build_ttb_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(website_ads_monitor, "TikTokWebsiteAdsClient", lambda _client: api)
    monkeypatch.setattr(website_ads_monitor, "sync_platform_review_results", _audit)
    monkeypatch.setattr(
        website_ads_monitor,
        "_advertiser_local_now",
        lambda *args, **kwargs: datetime(2026, 7, 17, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        website_ads_monitor,
        "evaluate_campaign_conversion_guard",
        _conversion_guard,
    )
    monkeypatch.setattr(website_ads_monitor, "review_website_ad_guard_action", _review)
    monkeypatch.setattr(website_ads_monitor, "run_group_racing", _race)
    monkeypatch.setattr(website_ads_monitor, "backfill_campaign_creatives", _backfill)

    result = asyncio.run(
        run_website_ads_monitor_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
        )
    )

    assert result["invalid_report_rows"] == 2
    assert result["incomplete_scopes"] == 1
    assert result["decision_holds"] == 1
    assert result["reviewed"] == 0
    assert result["paused"] == 0
    assert decision_calls == []
    assert db_session.query(WebsiteAdsMetricHourly).count() == 0
    db_session.refresh(ad)
    assert ad.last_checked_at is None


def test_monitor_reconciles_absent_hourly_facts_only_after_complete_report(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    db_session.add(
        WebsiteAdsMetricHourly(
            workspace_id=campaign.workspace_id,
            advertiser_id=campaign.advertiser_id,
            ad_local_id=ad.id,
            stat_hour=datetime(2026, 7, 17, 9),
            spend=Decimal("25"),
        )
    )
    db_session.commit()

    class Api:
        async def report_ads(self, *args, **kwargs):
            return {
                "data": {"list": []},
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": 0,
                    "source_pages": [{}],
                },
            }

        async def aclose(self):
            return None

    async def _audit(*args, **kwargs):
        return {"checked": 1, "rejected": 0, "snapshot_complete": True}

    async def _conversion_guard(*args, **kwargs):
        return {"status": "DATA_HOLD"}

    async def _race(*args, **kwargs):
        return {"status": "UNCHANGED"}

    async def _backfill(*args, **kwargs):
        return {"created": 0, "errors": []}

    api = Api()
    monkeypatch.setattr(website_ads_monitor, "build_ttb_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(website_ads_monitor, "TikTokWebsiteAdsClient", lambda _client: api)
    monkeypatch.setattr(website_ads_monitor, "sync_platform_review_results", _audit)
    monkeypatch.setattr(
        website_ads_monitor,
        "_advertiser_local_now",
        lambda *args, **kwargs: datetime(2026, 7, 17, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        website_ads_monitor,
        "evaluate_campaign_conversion_guard",
        _conversion_guard,
    )
    monkeypatch.setattr(website_ads_monitor, "run_group_racing", _race)
    monkeypatch.setattr(website_ads_monitor, "backfill_campaign_creatives", _backfill)

    result = asyncio.run(
        run_website_ads_monitor_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
        )
    )

    assert result["incomplete_scopes"] == 0
    assert result["decision_holds"] == 0
    assert result["reconciled_absent_rows"] == 1
    assert db_session.query(WebsiteAdsMetricHourly).count() == 0


def test_monitor_does_not_partially_overwrite_incomplete_previous_day(
    db_session,
    monkeypatch,
):
    campaign, ad = _seed_scope(db_session)
    stale = WebsiteAdsMetricHourly(
        workspace_id=campaign.workspace_id,
        advertiser_id=campaign.advertiser_id,
        ad_local_id=ad.id,
        stat_hour=datetime(2026, 7, 16, 9),
        spend=Decimal("25"),
    )
    db_session.add(stale)
    db_session.commit()

    class Api:
        async def report_ads(
            self,
            advertiser_id,
            ad_ids,
            start_date,
            end_date,
            *,
            hourly,
        ):
            rows = []
            if start_date == "2026-07-16":
                rows = [
                    {
                        "dimensions": {
                            "ad_id_v2": ad.ad_id_v2,
                            "stat_time_hour": "2026-07-16 09:00:00",
                        },
                        "metrics": {"spend": "5"},
                    },
                    {
                        "dimensions": {
                            "ad_id_v2": ad.ad_id_v2,
                            "stat_time_hour": None,
                        },
                        "metrics": {"spend": "999"},
                    },
                ]
            return {
                "data": {"list": rows},
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": len(rows),
                    "source_pages": [{}],
                },
            }

        async def aclose(self):
            return None

    async def _audit(*args, **kwargs):
        return {"checked": 1, "rejected": 0, "snapshot_complete": True}

    async def _conversion_guard(*args, **kwargs):
        return {"status": "DATA_HOLD"}

    async def _race(*args, **kwargs):
        return {"status": "UNCHANGED"}

    async def _backfill(*args, **kwargs):
        return {"created": 0, "errors": []}

    api = Api()
    monkeypatch.setattr(website_ads_monitor, "build_ttb_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(website_ads_monitor, "TikTokWebsiteAdsClient", lambda _client: api)
    monkeypatch.setattr(website_ads_monitor, "sync_platform_review_results", _audit)
    monkeypatch.setattr(
        website_ads_monitor,
        "_advertiser_local_now",
        lambda *args, **kwargs: datetime(2026, 7, 17, 0, 30, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        website_ads_monitor,
        "evaluate_campaign_conversion_guard",
        _conversion_guard,
    )
    monkeypatch.setattr(website_ads_monitor, "run_group_racing", _race)
    monkeypatch.setattr(website_ads_monitor, "backfill_campaign_creatives", _backfill)

    result = asyncio.run(
        run_website_ads_monitor_cycle(
            db_session,
            workspace_id=int(campaign.workspace_id),
        )
    )

    db_session.refresh(stale)
    assert result["incomplete_report_days"] == 1
    assert result["decision_holds"] == 0
    assert result["reconciled_absent_rows"] == 0
    assert stale.spend == Decimal("25")


def test_daily_report_rejects_incomplete_hourly_response_before_decision(
    db_session,
):
    campaign, ad = _seed_scope(db_session)

    class Api:
        async def report_ads(self, *args, **kwargs):
            return {
                "data": {
                    "list": [
                        {
                            "dimensions": {
                                "ad_id_v2": ad.ad_id_v2,
                                "stat_time_hour": "2026-07-17 01:00:00",
                            },
                            "metrics": {"spend": "5"},
                        },
                        {
                            "dimensions": {
                                "ad_id_v2": "escaped-ad",
                                "stat_time_hour": "2026-07-17 00:00:00",
                            },
                            "metrics": {"spend": "10"},
                        }
                    ]
                },
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": 2,
                    "source_pages": [{}],
                },
            }

    with pytest.raises(WebsiteAdsReportIncompleteError):
        asyncio.run(
            generate_campaign_daily_report(
                db_session,
                api=Api(),
                campaign=campaign,
                report_date=date(2026, 7, 17),
                final=True,
            )
        )

    assert db_session.query(WebsiteAdsMetricHourly).count() == 0
    assert db_session.query(WebsiteAdsDailyReport).count() == 0


def test_daily_refresh_reconciles_zero_row_complete_report(db_session):
    campaign, ad = _seed_scope(db_session)
    db_session.add(
        WebsiteAdsMetricHourly(
            workspace_id=campaign.workspace_id,
            advertiser_id=campaign.advertiser_id,
            ad_local_id=ad.id,
            stat_hour=datetime(2026, 7, 17, 9),
            spend=Decimal("25"),
        )
    )
    db_session.flush()

    class Api:
        async def report_ads(self, *args, **kwargs):
            return {
                "data": {"list": []},
                "_metric_fidelity": "conversion",
                "_report_pagination": {
                    "chunks_fetched": 1,
                    "pages_fetched": 1,
                    "rows_returned": 0,
                    "source_pages": [{}],
                },
            }

    result = asyncio.run(
        website_ads_daily_report._refresh_campaign_day_metrics(
            db_session,
            api=Api(),
            campaign=campaign,
            report_date=date(2026, 7, 17),
        )
    )

    assert result["complete"] is True
    assert result["reconciled_absent_rows"] == 1
    assert db_session.query(WebsiteAdsMetricHourly).count() == 0


def test_gmv_product_signal_isolates_store_and_target_report_day(
    db_session,
    monkeypatch,
):
    campaign, _ = _seed_scope(db_session)
    product = db_session.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    product.product_id = "product-1"
    db_session.add(
        TTBAdvertiserStoreLink(
            workspace_id=int(campaign.workspace_id),
            auth_id=int(campaign.auth_id),
            advertiser_id=str(campaign.advertiser_id),
            store_id="store-target",
        )
    )
    report_date = date(2026, 7, 15)
    db_session.add_all(
        [
            GmvProductOrderEvent(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-target",
                campaign_id="gmv-target",
                item_group_id="product-1",
                order_time_hour=datetime(2026, 7, 15, 10),
                order_count=2,
                gross_revenue_cents=5000,
                cost_cents=1000,
                source="GMVMAX_PRODUCT_HOURLY",
            ),
            GmvProductOrderEvent(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-other",
                campaign_id="gmv-other",
                item_group_id="product-1",
                order_time_hour=datetime(2026, 7, 15, 11),
                order_count=99,
                gross_revenue_cents=999_999,
                cost_cents=99_999,
                source="GMVMAX_PRODUCT_HOURLY",
            ),
            GmvProductMetricsHourly(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-target",
                campaign_id="gmv-target",
                item_group_id="product-1",
                stat_time_hour=datetime(2026, 7, 15, 23),
            ),
            GmvProductMetricsHourly(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-target",
                campaign_id="gmv-target",
                item_group_id="product-1",
                stat_time_hour=datetime(2026, 7, 16, 20),
            ),
            GmvProductMetricsHourly(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-other",
                campaign_id="gmv-other",
                item_group_id="product-1",
                stat_time_hour=datetime(2026, 7, 17, 23),
            ),
        ]
    )
    db_session.commit()
    sync_calls: list[dict] = []
    monkeypatch.setattr(
        website_ads_daily_report,
        "sync_product_order_events_from_hourly",
        lambda _db, **kwargs: sync_calls.append(dict(kwargs)) or 0,
    )

    signal, freshness = website_ads_daily_report._gmv_product_signal(
        db_session,
        campaign=campaign,
        product_id="product-1",
        report_date=report_date,
    )

    assert sync_calls[0]["store_id"] == "store-target"
    assert signal["orders"] == 2
    assert signal["gross_revenue"] == 50.0
    assert signal["gmv_max_spend"] == 10.0
    assert freshness["gmv_product_latest_hour"] == "2026-07-15T23:00:00"
    assert freshness["gmv_product_day_complete"] is True
    assert freshness["gmv_product_store_id"] == "store-target"

    orders = _order_snapshot(
        db_session,
        campaign=campaign,
        product_id="product-1",
        store_id="store-target",
        lookback_start=datetime(2026, 7, 15),
    )
    assert orders["order_count"] == 2
    source = _source_snapshot(
        db_session,
        campaign=campaign,
        product_id="product-1",
        store_id="store-target",
        advertiser_now=datetime(2026, 7, 16, 21),
    )
    assert source["latest_hour"] == datetime(2026, 7, 16, 20)


def test_conversion_guard_fails_closed_for_zero_or_ambiguous_store_scope(
    db_session,
    monkeypatch,
):
    campaign, _ = _seed_scope(db_session)
    product = db_session.get(WebsiteAdsLandingPage, int(campaign.landing_page_id))
    product.product_id = "product-1"
    db_session.add(product)
    db_session.commit()

    assert resolve_website_ads_store_id(
        db_session,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
    ) is None

    db_session.add_all(
        [
            TTBAdvertiserStoreLink(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-a",
            ),
            TTBAdvertiserStoreLink(
                workspace_id=int(campaign.workspace_id),
                auth_id=int(campaign.auth_id),
                advertiser_id=str(campaign.advertiser_id),
                store_id="store-b",
            ),
        ]
    )
    db_session.commit()
    assert resolve_website_ads_store_id(
        db_session,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        advertiser_id=str(campaign.advertiser_id),
    ) is None

    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.sync_product_order_events_from_hourly",
        lambda *_args, **_kwargs: pytest.fail("ambiguous store must not start GMV sync"),
    )

    class Api:
        async def update_campaign_status(self, *_args, **_kwargs):
            pytest.fail("ambiguous store must not mutate the official campaign")

    result = asyncio.run(
        evaluate_campaign_conversion_guard(
            db_session,
            api=Api(),
            campaign=campaign,
        )
    )

    assert result == {
        "status": "DATA_HOLD",
        "reason": "STORE_SCOPE_NOT_UNIQUE",
    }
    state = db_session.scalar(
        select(WebsiteAdsConversionGuardState).where(
            WebsiteAdsConversionGuardState.campaign_local_id == int(campaign.id)
        )
    )
    assert state is not None
    assert state.status == "DATA_HOLD"
    assert state.state_json["reason"] == "STORE_SCOPE_NOT_UNIQUE"


def test_asset_performance_isolates_same_video_by_advertiser(db_session):
    target_campaign, target_ad = _seed_scope(db_session)
    target_asset = WebsiteAdsCreativeAsset(
        workspace_id=int(target_campaign.workspace_id),
        auth_id=int(target_campaign.auth_id),
        advertiser_id=str(target_campaign.advertiser_id),
        video_id=str(target_ad.video_id),
        title="Target asset",
    )
    other_landing = WebsiteAdsLandingPage(
        workspace_id=int(target_campaign.workspace_id),
        external_id="landing-other",
        identifier="landing-other",
        title="Other landing",
        landing_url="https://example.com/other",
    )
    db_session.add_all([target_asset, other_landing])
    db_session.flush()
    other_campaign = WebsiteAdsCampaign(
        workspace_id=int(target_campaign.workspace_id),
        auth_id=int(target_campaign.auth_id),
        advertiser_id="advertiser-other",
        landing_page_id=int(other_landing.id),
        request_key="request-other",
        campaign_id="campaign-other",
        name="Other campaign",
        local_status="ACTIVE",
        operation_status="ENABLE",
    )
    db_session.add(other_campaign)
    db_session.flush()
    other_group = WebsiteAdsAdGroup(
        campaign_local_id=int(other_campaign.id),
        adgroup_id="adgroup-other",
        name="Other group",
        pixel_id="pixel-other",
        targeting_json={},
        budget_mode="BUDGET_MODE_DAY",
        budget=Decimal("50"),
        bid_type="BID_TYPE_NO_BID",
        schedule_start_time="2026-07-17 00:00:00",
        operation_status="ENABLE",
    )
    db_session.add(other_group)
    db_session.flush()
    other_ad = WebsiteAdsAd(
        campaign_local_id=int(other_campaign.id),
        adgroup_local_id=int(other_group.id),
        ad_id="ad-other",
        name="Other ad",
        video_id=str(target_ad.video_id),
        identity_type="CUSTOMIZED_USER",
        identity_id="identity-other",
        landing_page_url="https://example.com/other",
        operation_status="ENABLE",
    )
    db_session.add(other_ad)
    db_session.flush()
    db_session.add_all(
        [
            WebsiteAdsMetricHourly(
                workspace_id=int(target_campaign.workspace_id),
                advertiser_id=str(target_campaign.advertiser_id),
                ad_local_id=int(target_ad.id),
                stat_hour=datetime(2026, 7, 17, 1),
                spend=Decimal("10"),
                impressions=100,
                clicks=10,
                conversions=Decimal("1"),
                conversion_value=Decimal("20"),
            ),
            WebsiteAdsMetricHourly(
                workspace_id=int(target_campaign.workspace_id),
                advertiser_id="advertiser-other",
                ad_local_id=int(other_ad.id),
                stat_hour=datetime(2026, 7, 17, 1),
                spend=Decimal("100"),
                impressions=1000,
                clicks=100,
                conversions=Decimal("10"),
                conversion_value=Decimal("200"),
            ),
        ]
    )
    db_session.commit()

    performance = _asset_performance(db_session, target_asset)

    assert performance["spend"] == 10.0
    assert performance["impressions"] == 100
    assert performance["clicks"] == 10
    assert performance["conversion_value"] == 20.0
