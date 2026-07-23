from datetime import datetime, timezone

from app.data.models.gmv_restructured import PromotionTypeEnum
from app.services.gmvmax_lifecycle import _derive_campaign_lifecycle
from app.services.ttb_gmvmax import upsert_campaign_from_api


def test_derive_campaign_lifecycle_mapping():
    assert _derive_campaign_lifecycle("ENABLE", "CAMPAIGN_STATUS_ENABLE") == (
        "ACTIVE",
        False,
    )
    assert _derive_campaign_lifecycle("DISABLE", "CAMPAIGN_STATUS_ENABLE") == (
        "ACTIVE",
        False,
    )

    assert _derive_campaign_lifecycle("DISABLE", "CAMPAIGN_STATUS_DISABLE") == (
        "INACTIVE",
        False,
    )
    assert _derive_campaign_lifecycle(
        "DISABLE", "CAMPAIGN_STATUS_PRODUCT_USED_BY_PRODUCT_GMV_MAX"
    ) == ("INACTIVE", False)

    assert _derive_campaign_lifecycle("ENABLE", "CAMPAIGN_STATUS_DELETE") == (
        "DELETED",
        True,
    )
    assert _derive_campaign_lifecycle("DISABLE", "CAMPAIGN_STATUS_DELETE") == (
        "DELETED",
        True,
    )

    assert _derive_campaign_lifecycle(None, None) == ("UNKNOWN", False)


def test_upsert_campaign_from_api_sets_lifecycle(db_session):
    active_payload = {
        "campaign_id": "life-1",
        "campaign_name": "Lifecycle Active",
        "shopping_ads_type": "PRODUCT",
        "operation_status": "ENABLE",
        "secondary_status": "CAMPAIGN_STATUS_ENABLE",
    }

    active = upsert_campaign_from_api(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        payload=active_payload,
        store_id_hint="store-1",
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    assert active.operation_status == "ENABLE"
    assert active.secondary_status == "CAMPAIGN_STATUS_ENABLE"

    deleted_payload = {
        "campaign_id": "life-2",
        "campaign_name": "Lifecycle Deleted",
        "shopping_ads_type": "PRODUCT",
        "operation_status": "DISABLE",
        "secondary_status": "CAMPAIGN_STATUS_DELETE",
    }

    deleted = upsert_campaign_from_api(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        payload=deleted_payload,
        store_id_hint="store-1",
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    assert deleted.operation_status == "DISABLE"
    assert deleted.secondary_status == "CAMPAIGN_STATUS_DELETE"
