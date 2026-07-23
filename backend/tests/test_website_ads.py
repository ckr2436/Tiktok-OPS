from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.features.tenants.ttb.website_ads.schemas import MediaPlanGenerateRequest, WebsiteAdLaunchRequest
from app.features.tenants.ttb.website_ads.router import (
    _extract_uploaded_video_id,
    _fail_upload_fingerprint,
    _media_plan_generation_recovery_reason,
    _reserve_upload_fingerprint,
    _upload_result,
)
from app.services.website_ads_hermes_planner import (
    _allocate_budgets,
    _audience_keyword_slice,
    _creative_selection_score,
    _creative_slots_for_group_budget,
    _canonical_product_name,
    _fallback_groups,
    _fill_creative_slots,
    _json_safe,
    _normalize_creative_policy,
    _search_general_interest_ids,
    _verified_interest_fallback_keywords,
    sync_creative_assets,
    sync_spark_creative_assets,
)
from app.services import website_ads_magento
from app.services.website_ads_magento import sync_landing_pages
from app.data.models.website_ads import (
    WebsiteAdsCreativeAsset,
    WebsiteAdsLandingPage,
    WebsiteAdsMagentoConnection,
)
from app.services.website_ads_monitor import _click_quality_guard_evidence, _report_days_for_local_now
from app.services.website_ads_media_cache import (
    LOCAL_CACHE_KEY,
    archive_stream,
    attach_archived_video,
    merge_tiktok_media_metadata,
    resolve_asset_media,
    unique_tiktok_file_name,
)
from app.services.website_ads_asset_expansion import _asset_queue_key, _clone_budget_allocation, _group_capacity
from app.services.website_ads_plan_launch import _spark_asset_delivery
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_api import TTBRateLimitBudgetError
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_tracking import build_tracking_url
from app.services.website_ads_products import _match_tokens
from app.services.website_ads_tiktok_contract import (
    TIKTOK_CONTIGUOUS_US_LOCATION_IDS,
    TIKTOK_US_EXCLUDED_LOCATION_IDS,
    WEBSITE_ADS_OPTIMIZATION_EVENT,
    compensate_created_campaign,
    enforce_website_ads_location_policy,
    enforce_website_ads_placement_policy,
    normalize_tiktok_call_to_action,
    normalize_website_sales_call_to_action,
    select_website_ads_pixel,
    select_tiktok_video_identity,
    website_ads_optimization_fields,
)
from app.tasks.website_ads_tasks import (
    _media_plan_generation_claim_action,
    recover_stale_upload_fingerprints,
)


def _payload() -> dict:
    return {
        "landing_page_id": 1,
        "campaign_name": "Website Sales",
        "adgroup_name": "US precise audience",
        "ad_name": "Video A",
        "pixel_id": "pixel-1",
        "identity_type": "CUSTOMIZED_USER",
        "identity_id": "identity-1",
        "video_id": "video-1",
        "ad_text": "Shop now",
        "daily_budget": 50,
        "targeting": {
            "location_ids": ["6252001"],
            "interest_category_ids": ["verified-interest"],
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
        },
    }


def test_magento_absence_reconciliation_requires_a_clean_explicit_snapshot(
    db_session,
    monkeypatch,
):
    connection = WebsiteAdsMagentoConnection(
        workspace_id=3,
        name="Magento",
        base_url="https://shop.example",
        access_token_cipher=b"cipher",
        key_version=1,
        is_enabled=True,
    )
    db_session.add(connection)
    db_session.flush()
    current = WebsiteAdsLandingPage(
        workspace_id=3,
        connection_id=connection.id,
        external_id="current",
        identifier="current",
        title="Old current",
        landing_url="https://shop.example/current",
        is_active=True,
    )
    stale = WebsiteAdsLandingPage(
        workspace_id=3,
        connection_id=connection.id,
        external_id="stale",
        identifier="stale",
        title="Stale",
        landing_url="https://shop.example/stale",
        is_active=True,
    )
    db_session.add_all([current, stale])
    db_session.commit()

    valid_current = {
        "id": "current",
        "identifier": "current",
        "title": "Current from Magento",
        "landing_url": "https://shop.example/current",
        "reference_price": "12.50",
        "currency": "usd",
        "is_active": "true",
    }
    payloads = [
        [valid_current],
        [valid_current, {"landing_url": "https://shop.example/missing-id"}],
        {
            "items": [valid_current],
            "page_info": {
                "current_page": 1,
                "total_pages": 2,
                "has_more": True,
            },
        },
        [valid_current],
    ]

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return Response(payloads.pop(0))

    monkeypatch.setattr(
        website_ads_magento,
        "decrypt_blob_to_text",
        lambda *_args, **_kwargs: "token",
    )
    monkeypatch.setattr(
        website_ads_magento,
        "auto_bind_content_product",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        website_ads_magento.httpx,
        "AsyncClient",
        lambda **_kwargs: Client(),
    )

    implicit = asyncio.run(sync_landing_pages(db_session, connection))
    db_session.refresh(stale)
    assert implicit["reconciliation_applied"] is False
    assert stale.is_active is True

    malformed = asyncio.run(
        sync_landing_pages(
            db_session,
            connection,
            complete_snapshot=True,
        )
    )
    db_session.refresh(stale)
    assert malformed["invalid_rows"] == 1
    assert malformed["reconciliation_applied"] is False
    assert malformed["disabled"] == 0
    assert stale.is_active is True

    partial = asyncio.run(
        sync_landing_pages(
            db_session,
            connection,
            complete_snapshot=True,
        )
    )
    db_session.refresh(stale)
    assert partial["complete_snapshot"] is False
    assert partial["reconciliation_applied"] is False
    assert stale.is_active is True

    complete = asyncio.run(
        sync_landing_pages(
            db_session,
            connection,
            complete_snapshot=True,
        )
    )
    db_session.refresh(current)
    db_session.refresh(stale)
    assert complete["invalid_rows"] == 0
    assert complete["complete_snapshot"] is True
    assert complete["reconciliation_applied"] is True
    assert complete["disabled"] == 1
    assert current.title == "Current from Magento"
    assert current.reference_price == Decimal("12.5000")
    assert stale.is_active is False


def test_advertiser_library_absence_reconciles_only_a_complete_snapshot(db_session):
    current = WebsiteAdsCreativeAsset(
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        video_id="video-current",
        title="Current",
        source="ADVERTISER_LIBRARY",
        is_active=True,
    )
    stale = WebsiteAdsCreativeAsset(
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        video_id="video-stale",
        title="Stale",
        source="ADVERTISER_LIBRARY",
        is_active=True,
    )
    other_source = WebsiteAdsCreativeAsset(
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        video_id="manual-video",
        title="Manual",
        source="MANUAL_UPLOAD",
        is_active=True,
    )
    db_session.add_all([current, stale, other_source])
    db_session.commit()

    payload = {"list": [{"video_id": "video-current", "file_name": "current.mp4"}]}
    sync_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        videos_payload=payload,
    )
    db_session.refresh(stale)
    assert stale.is_active is True

    sync_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        videos_payload=payload,
        complete_snapshot=True,
    )
    db_session.refresh(current)
    db_session.refresh(stale)
    db_session.refresh(other_source)
    assert current.is_active is True
    assert stale.is_active is False
    assert other_source.is_active is True


def test_malformed_advertiser_library_snapshot_does_not_deactivate_assets(db_session):
    stale = WebsiteAdsCreativeAsset(
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        video_id="video-stale",
        title="Stale",
        source="ADVERTISER_LIBRARY",
        is_active=True,
    )
    db_session.add(stale)
    db_session.commit()

    sync_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        videos_payload={
            "list": [
                {"video_id": "video-current", "file_name": "current.mp4"},
                "not-an-object",
                {"file_name": "missing-primary-key.mp4"},
            ]
        },
        complete_snapshot=True,
    )

    db_session.refresh(stale)
    assert stale.is_active is True


def test_spark_absence_reconciliation_requires_valid_complete_snapshot(db_session):
    stale = WebsiteAdsCreativeAsset(
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        video_id="spark-stale",
        title="Stale Spark",
        source="SPARK_AUTHORIZED_POST",
        is_active=True,
    )
    db_session.add(stale)
    db_session.commit()
    valid_item = {
        "item_info": {"item_id": "spark-current", "item_type": "VIDEO"},
        "auth_info": {"ad_auth_status": "AUTHORIZED"},
        "video_info": {},
    }

    sync_spark_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        spark_payloads=[{"list": [valid_item]}],
    )
    db_session.refresh(stale)
    assert stale.is_active is True

    sync_spark_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        spark_payloads=[
            {
                "list": [
                    valid_item,
                    "not-an-object",
                    {"item_info": {"item_type": "VIDEO"}},
                ]
            }
        ],
        complete_snapshot=True,
    )
    db_session.refresh(stale)
    assert stale.is_active is True

    sync_spark_creative_assets(
        db_session,
        workspace_id=3,
        auth_id=9,
        advertiser_id="advertiser-1",
        spark_payloads=[{"list": [valid_item]}],
        complete_snapshot=True,
    )
    db_session.refresh(stale)
    assert stale.is_active is False


def test_media_plan_generation_redelivery_resumes_only_the_same_task():
    assert _media_plan_generation_claim_action(
        status="PROCESSING",
        stored_task_id="task-1",
        request_task_id="task-1",
        redelivered=True,
    ) == "RESUME"
    assert _media_plan_generation_claim_action(
        status="PROCESSING",
        stored_task_id="task-1",
        request_task_id="task-1",
        redelivered=False,
    ) == "DEDUPLICATE"
    assert _media_plan_generation_claim_action(
        status="PROCESSING",
        stored_task_id="task-2",
        request_task_id="task-1",
        redelivered=True,
    ) == "DEDUPLICATE"


def test_media_plan_generation_claim_rejects_a_stale_task_token():
    assert _media_plan_generation_claim_action(
        status="GENERATING",
        stored_task_id="new-task",
        request_task_id="old-task",
        redelivered=True,
    ) == "DEDUPLICATE"
    assert _media_plan_generation_claim_action(
        status="GENERATING",
        stored_task_id="new-task",
        request_task_id="new-task",
        redelivered=False,
    ) == "CLAIM"


def test_media_plan_generation_recovers_a_finished_or_orphaned_task():
    assert _media_plan_generation_recovery_reason(
        status="PROCESSING",
        task_state="SUCCESS",
        age_seconds=1,
        has_task_id=True,
    ) == "TASK_FINISHED_WITHOUT_PLAN"
    assert _media_plan_generation_recovery_reason(
        status="PROCESSING",
        task_state="PENDING",
        age_seconds=(22 * 60),
        has_task_id=True,
    ) == "STALE_WITHOUT_ACTIVE_TASK"


def test_media_plan_generation_does_not_requeue_an_active_task():
    assert _media_plan_generation_recovery_reason(
        status="PROCESSING",
        task_state="PROGRESS",
        age_seconds=60,
        has_task_id=True,
    ) is None
    assert _media_plan_generation_recovery_reason(
        status="GENERATING",
        task_state="PENDING",
        age_seconds=60,
        has_task_id=True,
    ) is None


def test_product_identity_uses_sold_product_name_not_landing_page_offer_title():
    product = SimpleNamespace(
        content_name="MYUPONA Sleep Ease Gummies",
        title="Evening Wind-Down Guide",
    )
    assert _canonical_product_name(product) == "MYUPONA Sleep Ease Gummies"


def test_verified_interest_fallback_prefers_health_taxonomy_for_sleep_products():
    product = SimpleNamespace(
        title="Evening Wind-Down Guide",
        content_name="MYUPONA Sleep Ease Gummies",
        content_category="Supplements",
    )
    keywords = _verified_interest_fallback_keywords(
        product,
        {"interest_keywords": ["melatonin-free", "magnesium glycinate"]},
    )
    assert keywords[:5] == [
        "wellness",
        "healthy lifestyle",
        "fitness",
        "yoga",
        "online shopping",
    ]


def test_auto_expansion_queue_prioritizes_new_assets_with_a_managed_plan():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc).replace(tzinfo=None)
    pending = SimpleNamespace(
        id=2,
        auto_launch_status="PENDING",
        auto_launch_next_retry_at=None,
        updated_at=now,
    )
    waiting = SimpleNamespace(
        id=1,
        auto_launch_status="WAITING_CAMPAIGN",
        auto_launch_next_retry_at=None,
        updated_at=now,
    )
    rank = (50.0, 3, now)
    assert _asset_queue_key(pending, has_managed_plan=True, rank=rank) < _asset_queue_key(
        waiting,
        has_managed_plan=False,
        rank=(100.0, 5, now),
    )


def test_auto_expansion_capacity_uses_enabled_ads_only_while_campaign_is_live():
    assert _group_capacity(
        total_ads=8,
        enabled_ads=5,
        campaign_enabled=True,
        max_active_ads=6,
        max_total_ads=20,
    ) == 1
    assert _group_capacity(
        total_ads=5,
        enabled_ads=0,
        campaign_enabled=False,
        max_active_ads=6,
        max_total_ads=20,
    ) == 1


def test_auto_expansion_never_increases_the_media_plan_budget_for_a_clone():
    assert _clone_budget_allocation(
        plan_budget=Decimal("50"),
        group_budgets=[Decimal("26.25"), Decimal("23.75")],
    ) is None

    redistributed = _clone_budget_allocation(
        plan_budget=Decimal("100"),
        group_budgets=[Decimal("40"), Decimal("30"), Decimal("30")],
    )
    assert redistributed == {
        "donor_index": 0,
        "donor_budget_before": Decimal("40"),
        "donor_budget_after": Decimal("20"),
        "new_group_budget": Decimal("20.00"),
    }
    assert (
        redistributed["donor_budget_after"]
        + Decimal("30")
        + Decimal("30")
        + redistributed["new_group_budget"]
    ) == Decimal("100")

    with_headroom = _clone_budget_allocation(
        plan_budget=Decimal("120"),
        group_budgets=[Decimal("40"), Decimal("30"), Decimal("30")],
    )
    assert with_headroom["donor_index"] is None
    assert sum([Decimal("40"), Decimal("30"), Decimal("30"), with_headroom["new_group_budget"]]) == Decimal("120")

    assert _clone_budget_allocation(
        plan_budget=Decimal("80"),
        group_budgets=[Decimal("60"), Decimal("20")],
        donor_indices={1},
    ) is None


def test_tracking_url_preserves_existing_query_and_adds_tiktok_macros():
    url, params = build_tracking_url("https://shop.example/lp/body-balm?ref=existing")
    query = parse_qs(urlsplit(url).query)

    assert query["ref"] == ["existing"]
    assert query["campaign_id"] == ["__CAMPAIGN_ID__"]
    assert query["adgroup_id"] == ["__AID__"]
    assert query["ad_id"] == ["__CID__"]
    assert query["creative_id"] == ["__CID__"]
    assert query["campaign_name"] == ["__CAMPAIGN_NAME__"]
    assert query["adgroup_name"] == ["__AID_NAME__"]
    assert "ad_name" not in query
    assert query["creative_name"] == ["__CID_NAME__"]
    assert query["utm_medium"] == ["paid"]
    assert len(params) == 14


def test_tiktok_call_to_action_normalizes_model_labels_and_rejects_unknown_values():
    assert normalize_tiktok_call_to_action("Learn More") == "LEARN_MORE"
    assert normalize_tiktok_call_to_action("shop-now") == "SHOP_NOW"
    assert normalize_tiktok_call_to_action("Buy Now") == "SHOP_NOW"
    assert normalize_tiktok_call_to_action("invented action") == "SHOP_NOW"


def test_website_sales_call_to_action_defaults_to_shop_now_and_requires_education_evidence():
    assert normalize_website_sales_call_to_action(None) == "SHOP_NOW"
    assert normalize_website_sales_call_to_action("Learn More") == "SHOP_NOW"
    assert normalize_website_sales_call_to_action(
        "Learn More",
        rationale="[EDUCATIONAL_FUNNEL] The verified landing page is an educational pre-sell guide.",
    ) == "LEARN_MORE"
    assert normalize_website_sales_call_to_action("Order Now") == "ORDER_NOW"
    assert normalize_website_sales_call_to_action("Sign Up") == "SHOP_NOW"


def test_website_ads_placement_is_always_tiktok_only():
    assert enforce_website_ads_placement_policy(
        "PLACEMENT_TYPE_AUTOMATIC",
        ["PLACEMENT_TIKTOK", "PLACEMENT_PANGLE", "PLACEMENT_GLOBAL_APP_BUNDLE"],
    ) == ("PLACEMENT_TYPE_NORMAL", ["PLACEMENT_TIKTOK"])


def test_us_country_target_expands_to_contiguous_states_and_dc():
    locations = enforce_website_ads_location_policy(["6252001"])

    assert locations == list(TIKTOK_CONTIGUOUS_US_LOCATION_IDS)
    assert len(locations) == 49
    assert not (set(locations) & TIKTOK_US_EXCLUDED_LOCATION_IDS)


def test_alaska_and_hawaii_are_removed_from_explicit_targeting():
    locations = enforce_website_ads_location_policy(["5332921", "5879092", "5855797"])

    assert locations == ["5332921"]


def test_only_excluded_states_cannot_become_an_empty_target():
    with pytest.raises(ValueError, match="only Alaska or Hawaii"):
        enforce_website_ads_location_policy(["5879092", "5855797"])


def test_legacy_compliant_creative_remains_approved():
    result = assess_website_ads_creative_policy({
        "risks": ["No explicit call to action.", "Avoid extending this into treatment claims."],
        "spoken_claims": ["Melatonin-free gummies for my night routine."],
    })

    assert result["readiness"] == "APPROVED"
    assert result["eligible_for_automatic_launch"] is True
    assert result["risk_only"] is False


def test_legacy_unverified_doctor_and_dosage_claims_are_risk_labeled_but_submittable():
    result = assess_website_ads_creative_policy({
        "risks": [
            "The doctor-friend reference may imply medical endorsement.",
            "The stated serving-dose sentence is contradictory or malformed.",
        ],
        "spoken_claims": ["A doctor friend did not prescribe anything."],
    })

    assert result["readiness"] == "BLOCKED"
    assert result["eligible_for_automatic_launch"] is True
    assert result["risk_only"] is True
    assert result["submission_mode"] == "TIKTOK_PLATFORM_REVIEW"
    assert "implied medical endorsement" in result["flags"]


def test_structured_review_creative_is_risk_labeled_but_submittable():
    result = assess_website_ads_creative_policy({
        "policy_readiness": "REVIEW",
        "policy_flags": ["ingredient label is not legible"],
        "risks": [],
    })

    assert result == {
        "readiness": "REVIEW",
        "eligible_for_automatic_launch": True,
        "flags": ["ingredient label is not legible"],
        "risk_only": True,
        "submission_mode": "TIKTOK_PLATFORM_REVIEW",
    }


def test_creative_analysis_policy_is_normalized_without_a_stale_local_variable():
    normalized = _normalize_creative_policy({
        "creative_type": "product_demo",
        "policy_readiness": "REVIEW",
        "policy_flags": ["ingredient label is not legible"],
    })

    assert normalized["creative_type"] == "product_demo"
    assert normalized["policy_readiness"] == "REVIEW"
    assert normalized["policy_flags"] == ["ingredient label is not legible"]


def test_video_identity_prefers_available_bc_push_identity_over_auth_code():
    selected = select_tiktok_video_identity({
        "data": {
            "identity_list": [
                {"identity_id": "spark-1", "identity_type": "AUTH_CODE"},
                {
                    "identity_id": "bc-1",
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_bc_id": "business-center-1",
                    "available_status": "AVAILABLE",
                    "can_push_video": True,
                },
            ]
        }
    })
    assert selected == {
        "identity_id": "bc-1",
        "identity_type": "BC_AUTH_TT",
        "identity_authorized_bc_id": "business-center-1",
    }


def test_video_identity_rejects_auth_code_only_for_uploaded_video():
    with pytest.raises(ValueError, match="can push advertiser-library videos"):
        select_tiktok_video_identity({
            "identity_list": [{"identity_id": "spark-1", "identity_type": "AUTH_CODE"}]
        })


def test_failed_creation_deletes_partial_tiktok_campaign():
    class Api:
        def __init__(self):
            self.calls = []

        async def update_campaign_status(self, advertiser_id, campaign_ids, status):
            self.calls.append((advertiser_id, campaign_ids, status))
            return {"code": 0, "data": {"status": status}}

    api = Api()
    result = asyncio.run(compensate_created_campaign(
        api,
        advertiser_id="advertiser-1",
        campaign_id="campaign-1",
    ))
    assert result["operation_status"] == "DELETE"
    assert api.calls == [("advertiser-1", ["campaign-1"], "DELETE")]


def test_failed_creation_disables_campaign_when_delete_compensation_fails():
    class Api:
        def __init__(self):
            self.calls = []

        async def update_campaign_status(self, advertiser_id, campaign_ids, status):
            self.calls.append(status)
            if status == "DELETE":
                raise RuntimeError("temporary delete failure")
            return {"code": 0, "data": {"status": status}}

    api = Api()
    result = asyncio.run(compensate_created_campaign(
        api,
        advertiser_id="advertiser-1",
        campaign_id="campaign-1",
    ))
    assert result["operation_status"] == "DISABLE"
    assert result["delete_error"].startswith("RuntimeError")
    assert api.calls == ["DELETE", "DISABLE"]


def test_failed_creation_cleanup_is_idempotent_when_campaign_is_already_deleted():
    class Api:
        def __init__(self):
            self.calls = []

        async def update_campaign_status(self, advertiser_id, campaign_ids, status):
            self.calls.append(status)
            raise RuntimeError("This campaign has been deleted.")

    api = Api()
    result = asyncio.run(compensate_created_campaign(
        api,
        advertiser_id="advertiser-1",
        campaign_id="campaign-1",
    ))
    assert result["operation_status"] == "DELETE"
    assert result["delete_response"]["idempotent"] is True
    assert api.calls == ["DELETE"]


def test_media_plan_snapshots_are_json_serializable():
    payload = _json_safe({
        "updated_at": datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        "price": Decimal("12.99"),
        "nested": [{"created_at": datetime(2026, 7, 14, 9, 30)}],
    })
    assert payload["updated_at"] == "2026-07-14T10:00:00+00:00"
    assert payload["price"] == 12.99
    assert payload["nested"][0]["created_at"] == "2026-07-14T09:30:00"
    json.dumps(payload)


def test_content_product_matching_normalizes_product_variants():
    landing = _match_tokens("2 bottles MYUPONA Sleep Ease Gummies")
    content_product = _match_tokens("MYUPONA Sleep Easy Gummies")
    assert landing & content_product == {"sleep", "easy", "gummies"}


def test_cost_cap_requires_conversion_bid_price():
    payload = _payload()
    payload["bid_strategy"] = "COST_CAP"
    with pytest.raises(ValidationError):
        WebsiteAdLaunchRequest.model_validate(payload)


def test_lowest_cost_does_not_require_conversion_bid_price():
    request = WebsiteAdLaunchRequest.model_validate(_payload())
    assert request.bid_strategy == "LOWEST_COST"
    assert request.conversion_bid_price is None


def test_targeting_requires_a_location():
    payload = _payload()
    payload["targeting"] = {"location_ids": []}
    with pytest.raises(ValidationError):
        WebsiteAdLaunchRequest.model_validate(payload)


def test_bc_identity_requires_authorized_business_center():
    payload = _payload()
    payload["identity_type"] = "BC_AUTH_TT"
    with pytest.raises(ValidationError):
        WebsiteAdLaunchRequest.model_validate(payload)


class _RecordingApi:
    def __init__(self):
        self.calls = []

    async def _request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"code": 0, "data": {}}


def test_pixel_list_uses_official_page_size_limit():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.list_pixels("advertiser-1"))
    _, path, kwargs = api.calls[0]
    assert path == "/pixel/list/"
    assert kwargs["params"]["page_size"] == 20


def test_ad_status_lookup_requests_every_filtered_ad_in_the_batch():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.get_ads("advertiser-1", [f"ad-{index}" for index in range(43)]))
    _, path, kwargs = api.calls[0]
    assert path == "/ad/get/"
    assert kwargs["params"]["page"] == 1
    assert kwargs["params"]["page_size"] == 43
    filtering = json.loads(kwargs["params"]["filtering"])
    assert len(filtering["ad_ids"]) == 43
    assert filtering["primary_status"] == "STATUS_ALL"


def test_campaign_status_lookup_includes_deleted_campaigns():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.get_campaigns("advertiser-1", ["campaign-1"]))
    _, path, kwargs = api.calls[0]
    assert path == "/campaign/get/"
    assert kwargs["params"]["page"] == 1
    assert kwargs["params"]["page_size"] == 1
    assert json.loads(kwargs["params"]["filtering"]) == {
        "campaign_ids": ["campaign-1"],
        "primary_status": "STATUS_ALL",
    }


def test_upload_by_url_uses_json_body():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(
        client.upload_video_by_url(
            "advertiser-1",
            "https://cdn.example/video.mp4",
            "video.mp4",
        )
    )
    method, path, kwargs = api.calls[0]
    assert method == "POST"
    assert path == "/file/video/ad/upload/"
    assert kwargs["json_body"]["upload_type"] == "UPLOAD_BY_URL"
    assert "multipart_body" not in kwargs


def test_upload_by_file_uses_multipart_and_signature():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    file_object = object()
    asyncio.run(
        client.upload_video_file(
            "advertiser-1",
            "video.mp4",
            file_object,
            "md5-signature",
        )
    )
    method, path, kwargs = api.calls[0]
    assert method == "POST"
    assert path == "/file/video/ad/upload/"
    assert kwargs["multipart_body"]["upload_type"] == "UPLOAD_BY_FILE"
    assert kwargs["multipart_body"]["video_signature"] == "md5-signature"
    assert kwargs["multipart_files"]["video_file"][1] is file_object
    assert kwargs["request_timeout"] == settings.WEBSITE_ADS_VIDEO_UPLOAD_TIMEOUT_SECONDS


def test_video_archive_is_content_addressed_and_deduplicated_on_raid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBSITE_ADS_MEDIA_STORAGE_DIR", str(tmp_path))
    content = b"test-video-content" * 1024
    first = archive_stream(BytesIO(content), file_name="first.mp4", content_type="video/mp4")
    second = archive_stream(BytesIO(content), file_name="renamed.mp4", content_type="video/mp4")

    assert first.path == second.path
    assert first.path.is_file()
    assert first.path.read_bytes() == content
    assert first.path.parent == tmp_path / "uploads" / "sha256" / first.sha256[:2]
    assert not list((tmp_path / "incoming").glob("*.part"))


def test_tiktok_upload_name_includes_content_hash_and_stays_within_limit():
    name = unique_tiktok_file_name("x" * 150 + ".mp4", "a" * 64)
    assert name.endswith("-aaaaaaaaaa.mp4")
    assert len(name) <= 100


def test_tiktok_resync_preserves_permanent_media_cache_metadata():
    existing = {
        LOCAL_CACHE_KEY: {
            "video": {"path": "/data/gmv_ops/website_ads_media/assets/video.mp4"},
        },
        "old": "value",
    }
    merged = merge_tiktok_media_metadata(
        existing,
        {"video_id": "video-1"},
        video_url="https://cdn.example/new-video.mp4",
        cover_url="https://cdn.example/new-cover.jpg",
    )

    assert merged[LOCAL_CACHE_KEY]["video"] == existing[LOCAL_CACHE_KEY]["video"]
    assert merged[LOCAL_CACHE_KEY]["state"] == "PENDING"
    assert merged["_tiktok_media_source"] == {
        "video_url": "https://cdn.example/new-video.mp4",
        "cover_url": "https://cdn.example/new-cover.jpg",
    }
    assert "old" not in merged


def test_uploaded_asset_links_archive_into_permanent_asset_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBSITE_ADS_MEDIA_STORAGE_DIR", str(tmp_path))
    archived = archive_stream(BytesIO(b"video" * 2048), file_name="video.mp4", content_type="video/mp4")
    asset = SimpleNamespace(
        id=7,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-3",
        video_id="video-4",
        raw_json={},
        preview_url=None,
    )

    attach_archived_video(asset, archived)
    cached = resolve_asset_media(asset, "video")

    assert cached is not None
    assert cached[0].read_bytes() == archived.path.read_bytes()
    assert asset.preview_url.endswith("/creative-assets/7/video")


def test_upload_video_id_extraction_and_duplicate_response():
    payload = {"code": 0, "data": {"video_info": {"video_id": "video-existing"}}}
    assert _extract_uploaded_video_id(payload) == "video-existing"
    row = SimpleNamespace(status="UPLOADED", video_id="video-existing", response_json=payload)
    result = _upload_result(row, deduplicated=True)
    assert result["deduplicated"] is True
    assert result["skipped"] is True
    assert result["in_progress"] is False
    assert result["video_id"] == "video-existing"
    assert result["data"]["video_id"] == "video-existing"


def test_upload_fingerprint_reserves_the_same_content_once(db_session):
    values = {
        "workspace_id": 101,
        "auth_id": 202,
        "advertiser_id": "advertiser-1",
        "content_sha256": "a" * 64,
        "fingerprint_type": "FILE_SHA256",
        "file_name": "renamed-video.mp4",
        "file_size_bytes": 1234,
    }
    first, should_upload = _reserve_upload_fingerprint(db_session, **values)
    duplicate, should_upload_again = _reserve_upload_fingerprint(
        db_session,
        **{**values, "file_name": "same-content-different-name.mp4"},
    )
    assert should_upload is True
    assert should_upload_again is False
    assert duplicate.id == first.id


def test_upload_failure_persists_exception_type(db_session):
    row, _ = _reserve_upload_fingerprint(
        db_session,
        workspace_id=101,
        auth_id=202,
        advertiser_id="advertiser-1",
        content_sha256="b" * 64,
        fingerprint_type="FILE_SHA256",
        file_name="video.mp4",
        file_size_bytes=1234,
    )
    _fail_upload_fingerprint(db_session, row, TimeoutError())
    db_session.refresh(row)
    assert row.status == "FAILED"
    assert row.error_message == "TimeoutError: "


def test_stale_upload_recovery_prevents_permanent_in_progress_rows(db_session):
    row, _ = _reserve_upload_fingerprint(
        db_session,
        workspace_id=101,
        auth_id=202,
        advertiser_id="advertiser-1",
        content_sha256="c" * 64,
        fingerprint_type="FILE_SHA256",
        file_name="interrupted.mp4",
        file_size_bytes=1234,
    )
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    db_session.add(row)
    db_session.commit()

    assert recover_stale_upload_fingerprints(db_session) == 1
    db_session.refresh(row)
    assert row.status == "FAILED"
    assert row.error_message.startswith("UploadInterrupted:")


def test_report_ads_requests_official_video_metrics():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    result = asyncio.run(client.report_ads("advertiser-1", ["ad-v2-1"], "2026-07-14", "2026-07-14", hourly=True))
    _, path, kwargs = api.calls[0]
    metrics = json.loads(kwargs["params"]["metrics"])
    assert path == "/report/integrated/get/"
    assert result["_metric_fidelity"] == "conversion_video"
    assert "video_play_actions" in metrics
    assert "video_watched_2s" in metrics
    assert "video_watched_6s" in metrics
    assert "video_views_p100" in metrics


def test_report_ads_does_not_retry_metric_fallback_on_quota_error():
    class _QuotaApi:
        def __init__(self):
            self.calls = 0

        async def _request_json(self, *_args, **_kwargs):
            self.calls += 1
            raise TTBRateLimitBudgetError(
                "quota busy",
                code="LOCAL_RATE_LIMIT",
                payload={"retry_after_ms": 60_000},
            )

    api = _QuotaApi()
    with pytest.raises(TTBRateLimitBudgetError):
        asyncio.run(
            TikTokWebsiteAdsClient(api).report_ads(
                "advertiser-1",
                ["ad-v2-1"],
                "2026-07-14",
                "2026-07-14",
                hourly=True,
            )
        )

    assert api.calls == 1


class _PaginatedReportApi:
    def __init__(self):
        self.calls = []

    async def _request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        page = int(kwargs["params"]["page"])
        rows = {
            1: [
                {
                    "dimensions": {"ad_id_v2": "ad-1", "stat_time_hour": "2026-07-15 08:00:00"},
                    "metrics": {"spend": "1.25"},
                }
            ],
            2: [
                {
                    "dimensions": {"ad_id_v2": "ad-2", "stat_time_hour": "2026-07-15 08:00:00"},
                    "metrics": {"spend": "2.50"},
                }
            ],
        }
        return {
            "code": 0,
            "data": {
                "list": rows[page],
                "page_info": {
                    "page": page,
                    "page_size": 50,
                    "total_number": 2,
                    "total_page": 2,
                },
            },
        }


def test_report_ads_fetches_every_official_report_page():
    api = _PaginatedReportApi()
    client = TikTokWebsiteAdsClient(api)

    result = asyncio.run(
        client.report_ads(
            "advertiser-1",
            ["ad-1", "ad-2"],
            "2026-07-15",
            "2026-07-15",
            hourly=True,
        )
    )

    assert [call[2]["params"]["page"] for call in api.calls] == [1, 2]
    assert [call[2]["params"]["page_size"] for call in api.calls] == [1000, 1000]
    assert len(result["data"]["list"]) == 2
    assert result["data"]["page_info"]["total_number"] == 2
    assert result["_report_pagination"]["pages_fetched"] == 2
    assert result["_metric_fidelity"] == "conversion_video"


def test_previous_advertiser_day_is_finalized_twice_hourly_after_midnight():
    assert _report_days_for_local_now(datetime(2026, 7, 16, 1, 30)) == [
        "2026-07-16",
        "2026-07-15",
    ]
    assert _report_days_for_local_now(datetime(2026, 7, 16, 1, 31)) == ["2026-07-16"]
    assert _report_days_for_local_now(datetime(2026, 7, 16, 4, 0)) == ["2026-07-16"]


def test_suggested_cover_uses_video_cover_endpoint():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.suggest_video_covers("advertiser-1", "video-1"))
    method, path, kwargs = api.calls[0]
    assert method == "GET"
    assert path == "/file/video/suggestcover/"
    assert kwargs["params"] == {"advertiser_id": "advertiser-1", "video_id": "video-1"}


def test_spark_video_list_uses_advertiser_authorized_posts_endpoint():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.list_spark_videos("advertiser-1", page=2, page_size=100))
    method, path, kwargs = api.calls[0]
    assert method == "GET"
    assert path == "/tt_video/list/"
    assert kwargs["params"]["advertiser_id"] == "advertiser-1"
    assert json.loads(kwargs["params"]["item_types"]) == ["VIDEO"]
    assert kwargs["params"]["page"] == 2
    assert kwargs["params"]["page_size"] == 50


def test_spark_asset_uses_authorized_creator_identity_and_item_id():
    asset = SimpleNamespace(
        id=9,
        source="SPARK_AUTHORIZED_POST",
        video_id="post-1",
        raw_json={
            "item_info": {"item_id": "post-1"},
            "user_info": {"identity_id": "creator-1", "identity_type": "AUTH_CODE"},
            "auth_info": {"ad_auth_status": "AUTHORIZED", "auth_end_time": "2099-12-31 23:59:59"},
        },
    )

    assert _spark_asset_delivery(asset) == {
        "tiktok_item_id": "post-1",
        "identity_id": "creator-1",
        "identity_type": "AUTH_CODE",
    }


def test_spark_asset_rejects_revoked_authorization():
    asset = SimpleNamespace(
        id=9,
        source="SPARK_AUTHORIZED_POST",
        video_id="post-1",
        raw_json={
            "item_info": {"item_id": "post-1"},
            "user_info": {"identity_id": "creator-1", "identity_type": "AUTH_CODE"},
            "auth_info": {"ad_auth_status": "UNAUTHORIZED"},
        },
    )

    with pytest.raises(RuntimeError, match="not active"):
        _spark_asset_delivery(asset)


def test_targeting_search_sends_official_general_interest_parameters():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(
        client.search_targeting(
            "advertiser-1",
            "INTEREST_AND_BEHAVIOR",
            ["sleep support"],
            sub_targeting_types=["GENERAL_INTEREST"],
            language="en",
        )
    )
    method, path, kwargs = api.calls[0]
    assert method == "GET"
    assert path == "/targeting/search/"
    assert json.loads(kwargs["params"]["search_keywords"]) == ["sleep support"]
    assert json.loads(kwargs["params"]["sub_targeting_types"]) == ["GENERAL_INTEREST"]
    assert kwargs["params"]["language"] == "en"


def test_interest_catalog_uses_official_v2_tiktok_placement_contract():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(
        client.list_interest_categories(
            "advertiser-1",
            version=2,
            language="en",
            placements=["PLACEMENT_TIKTOK"],
        )
    )
    method, path, kwargs = api.calls[0]
    assert method == "GET"
    assert path == "/tool/interest_category/"
    assert kwargs["params"]["version"] == 2
    assert kwargs["params"]["language"] == "en"
    assert json.loads(kwargs["params"]["placements"]) == ["PLACEMENT_TIKTOK"]


def test_interest_lookup_is_per_keyword_deduplicated_and_optional():
    class TargetingApi:
        def __init__(self):
            self.calls = []

        async def search_targeting(self, advertiser_id, targeting_type, search_keywords, **kwargs):
            self.calls.append((advertiser_id, targeting_type, search_keywords, kwargs))
            if search_keywords == ["bad keyword"]:
                raise RuntimeError("temporary TikTok targeting failure")
            interest_id = "interest-1" if search_keywords == ["sleep"] else "interest-2"
            return {
                "data": {
                    "general_interest": {
                        "search_result": {"items": [{"id": interest_id}, {"id": "interest-1"}]}
                    }
                }
            }

    api = TargetingApi()
    result = asyncio.run(
        _search_general_interest_ids(api, "advertiser-1", ["sleep", "bad keyword", "wellness"])
    )
    assert result == ["interest-1", "interest-2"]
    assert [call[2] for call in api.calls] == [["sleep"], ["bad keyword"], ["wellness"]]
    assert all(call[3]["sub_targeting_types"] == ["GENERAL_INTEREST"] for call in api.calls)


def test_smart_plus_ad_status_uses_smart_id_field():
    api = _RecordingApi()
    client = TikTokWebsiteAdsClient(api)
    asyncio.run(client.update_smart_ad_status("advertiser-1", ["smart-ad-1"], "DISABLE"))
    method, path, kwargs = api.calls[0]
    assert method == "POST"
    assert path == "/smart_plus/ad/status/update/"
    assert kwargs["json_body"]["smart_plus_ad_ids"] == ["smart-ad-1"]
    assert "ad_ids" not in kwargs["json_body"]


def test_media_plan_requires_tiktok_adgroup_minimum_budget():
    with pytest.raises(ValidationError):
        MediaPlanGenerateRequest.model_validate({
            "landing_page_id": 1,
            "creative_asset_ids": [1],
            "daily_budget": 19.99,
        })


def test_media_plan_allows_hermes_to_select_from_full_asset_library():
    request = MediaPlanGenerateRequest.model_validate({
        "landing_page_id": 1,
        "daily_budget": 60,
    })
    assert request.creative_asset_ids is None


def test_media_plan_budget_allocation_preserves_total_and_minimum():
    budgets = _allocate_budgets(Decimal("70.00"), 3)
    assert sum(budgets) == Decimal("70.00")
    assert all(value >= Decimal("20.00") for value in budgets)


def test_media_plan_budget_allocation_supports_five_adgroups():
    budgets = _allocate_budgets(Decimal("130.00"), 5)
    assert sum(budgets) == Decimal("130.00")
    assert all(value >= Decimal("20.00") for value in budgets)


def test_fallback_media_plan_never_invents_creative_ids():
    class Asset:
        def __init__(self, asset_id):
            self.id = asset_id

    selected = [Asset(11), Asset(12), Asset(13), Asset(14)]
    groups = _fallback_groups(selected, 3)
    assert len(groups) == 3
    assert {asset_id for group in groups for asset_id in group["creative_asset_ids"]} <= {11, 12, 13, 14}


def test_two_group_plan_reuses_creatives_for_precise_audience_isolation():
    assets = [SimpleNamespace(id=value) for value in range(1, 9)]

    groups = _fallback_groups(assets, 2, creatives_per_group=3)

    assert [group["role"] for group in groups] == ["AUDIENCE_TEST", "AUDIENCE_TEST"]
    assert groups[0]["creative_asset_ids"] == [1, 2, 3]
    assert groups[1]["creative_asset_ids"] == [1, 2, 3]


def test_five_group_plan_keeps_the_creative_variable_fixed_across_audiences():
    assets = [SimpleNamespace(id=value) for value in range(1, 15)]

    groups = _fallback_groups(assets, 5, creatives_per_group=4)

    assert [group["role"] for group in groups] == ["AUDIENCE_TEST"] * 5
    assert all(group["creative_asset_ids"] == [1, 2, 3, 4] for group in groups)


def test_audience_keyword_slices_are_distinct_when_enough_keywords_exist():
    keywords = ["sleep", "wellness", "relaxation", "night routine", "supplements", "self care"]
    assert _audience_keyword_slice(keywords, 0) == ["sleep", "wellness"]
    assert _audience_keyword_slice(keywords, 1) == ["relaxation", "night routine"]
    assert _audience_keyword_slice(keywords, 2) == ["supplements", "self care"]


def test_creative_coverage_fills_model_output_that_is_too_narrow():
    selected = _fill_creative_slots(
        [1],
        [1, 2, 3],
        [1, 2, 3, 4, 5, 6],
        target=3,
    )
    challenger = _fill_creative_slots(
        [1],
        [4, 5, 6],
        [1, 2, 3, 4, 5, 6],
        target=3,
        excluded_ids=set(selected),
    )

    assert selected == [1, 2, 3]
    assert challenger == [4, 5, 6]


def test_creative_slots_scale_with_each_adgroup_budget():
    assert _creative_slots_for_group_budget(Decimal("20")) == 4
    assert _creative_slots_for_group_budget(Decimal("40")) == 5
    assert _creative_slots_for_group_budget(Decimal("70")) == 6


def test_website_ads_uses_view_content_optimization_contract():
    assert WEBSITE_ADS_OPTIMIZATION_EVENT == "ON_WEB_DETAIL"
    assert website_ads_optimization_fields() == {
        "optimization_goal": "CONVERT",
        "optimization_event": "ON_WEB_DETAIL",
        "billing_event": "OCPM",
    }


def test_website_ads_pixel_preflight_requires_active_view_content_event():
    payload = {
        "data": {
            "pixels": [
                {
                    "pixel_id": "purchase-only",
                    "activity_status": "ACTIVE",
                    "events": [{"optimization_event": "SHOPPING", "deprecated": False}],
                },
                {
                    "pixel_id": "view-content",
                    "activity_status": "ACTIVE",
                    "events": [{"optimization_event": "ON_WEB_DETAIL", "deprecated": False}],
                },
            ]
        }
    }
    selected = select_website_ads_pixel(payload, preferred_pixel_id="view-content")
    assert selected["pixel_id"] == "view-content"
    with pytest.raises(ValueError):
        select_website_ads_pixel({"data": {"pixels": [payload["data"]["pixels"][0]]}})


def test_click_quality_guard_uses_sample_thresholds_instead_of_runtime():
    poor_ctr = _click_quality_guard_evidence(
        spend=Decimal("0.90"), impressions=100, clicks=3,
        config={}, emergency_spend_threshold=Decimal("5"),
    )
    qualified = _click_quality_guard_evidence(
        spend=Decimal("1.20"), impressions=100, clicks=5,
        config={}, emergency_spend_threshold=Decimal("5"),
    )
    insufficient = _click_quality_guard_evidence(
        spend=Decimal("0.60"), impressions=80, clicks=1,
        config={}, emergency_spend_threshold=Decimal("5"),
    )

    assert poor_ctr["triggered"]
    assert "LOW_CTR" in poor_ctr["reasons"]
    assert not qualified["triggered"]
    assert not insufficient["triggered"]


def test_creative_selection_balances_results_exploration_and_loss_control():
    proven = _creative_selection_score(
        {"spend": 20, "impressions": 5000, "clicks": 120, "conversions": 4, "roas": 2.2},
        product_confidence=0.95,
        analysis_status="READY",
        production_origin="REAL_CREATOR",
        reference_price=20,
    )
    exploration = _creative_selection_score(
        {"spend": 0.5, "impressions": 120, "clicks": 3, "conversions": 0, "roas": 0},
        product_confidence=0.9,
        analysis_status="READY",
        production_origin="REAL_CREATOR",
        reference_price=20,
    )
    wasteful = _creative_selection_score(
        {"spend": 80, "impressions": 5000, "clicks": 80, "conversions": 0, "roas": 0},
        product_confidence=0.95,
        analysis_status="READY",
        production_origin="REAL_CREATOR",
        reference_price=20,
    )
    assert proven > exploration > wasteful


def test_creative_selection_prioritizes_real_creator_over_aigc():
    performance = {"spend": 1, "impressions": 300, "clicks": 12, "conversions": 0}
    real_creator = _creative_selection_score(
        performance,
        product_confidence=0.9,
        analysis_status="READY",
        production_origin="REAL_CREATOR",
        reference_price=20,
    )
    aigc = _creative_selection_score(
        performance,
        product_confidence=0.9,
        analysis_status="READY",
        production_origin="AIGC",
        reference_price=20,
    )

    assert real_creator > aigc
