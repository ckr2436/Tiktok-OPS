from datetime import datetime, timedelta
from decimal import Decimal

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.data.models.gmv_restructured import PromotionTypeEnum
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.gmvmax.services.campaign_mapper import map_gmvmax_campaign_info_to_model
from app.gmvmax.services.sync_service import GmvMaxSyncService


def test_map_gmvmax_campaign_info_to_model(db_session):
    info = {
        "campaign_id": "123",
        "campaign_name": "Test GMV Max",
        "shopping_ads_type": "PRODUCT",
        "operation_status": "DISABLE",
        "secondary_status": "campaign_status_enable",
        "optimization_goal": "GMV",
        "deep_bid_type": "COST",
        "roas_bid": "1.23456",
        "budget": "12.5",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": "2024-03-01T00:00:00Z",
        "schedule_end_time": "2024-03-10T00:00:00Z",
        "store_id": "store-1",
        "ext_updated_time": "2024-03-02 00:00:00",
        "currency": "USD",
    }
    synced_at = datetime(2024, 3, 5, 0, 0, 0)

    campaign = map_gmvmax_campaign_info_to_model(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        info=info,
        synced_at=synced_at,
        primary_status_hint="STATUS_NOT_DELETE",
    )
    db_session.add(campaign)
    db_session.commit()

    assert campaign.campaign_id == "123"
    assert campaign.name == "Test GMV Max"
    assert campaign.promotion_type == PromotionTypeEnum.PRODUCT
    assert campaign.operation_status == "DISABLE"
    assert campaign.bid_type == "COST"
    assert campaign.daily_budget_cents == 1250
    assert campaign.currency == "USD"
    assert campaign.roas_bid == Decimal("1.2346")
    assert campaign.schedule_start_time == datetime(2024, 3, 1, 0, 0, 0)
    assert campaign.schedule_end_time == datetime(2024, 3, 10, 0, 0, 0)
    assert campaign.ext_created_time == synced_at
    assert campaign.ext_updated_time == datetime(2024, 3, 2, 0, 0, 0)
    assert campaign.raw_json.get("promotion_days") is None
    assert not hasattr(campaign, "primary_status")
    assert campaign.secondary_status == "CAMPAIGN_STATUS_ENABLE"
    assert campaign.status == "ACTIVE"
    assert campaign.is_deleted is False
    assert campaign.deleted_at is None


def test_map_gmvmax_campaign_deleted_state(db_session):
    deleted_synced_at = datetime(2024, 4, 1, 0, 0, 0)
    deleted_payload = {
        "campaign_id": "del-1",
        "campaign_name": "Deleted GMV Max",
        "shopping_ads_type": "PRODUCT",
        "secondary_status": None,
        "operation_status": "DISABLE",
    }

    campaign = map_gmvmax_campaign_info_to_model(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        info=deleted_payload,
        synced_at=deleted_synced_at,
        primary_status_hint="STATUS_DELETE",
    )
    db_session.add(campaign)
    db_session.commit()

    assert not hasattr(campaign, "primary_status")
    assert campaign.secondary_status is None
    assert campaign.status == "DELETED"
    assert campaign.is_deleted is True
    assert campaign.deleted_at == deleted_synced_at

    later_sync = map_gmvmax_campaign_info_to_model(
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv",
        info=deleted_payload,
        synced_at=datetime(2024, 4, 2, 0, 0, 0),
        existing=campaign,
        primary_status_hint="STATUS_DELETE",
    )
    assert later_sync.deleted_at == deleted_synced_at


def test_select_active_campaigns_filters_and_limits(db_session):
    service = GmvMaxSyncService()
    base_time = datetime(2024, 1, 1, 12, 0, 0)
    # Active campaigns
    for i in range(35):
        db_session.add(
            TTBGmvMaxCampaign(
                workspace_id=1,
                auth_id=1,
                advertiser_id="adv",
                campaign_id=str(i),
                promotion_type=PromotionTypeEnum.PRODUCT,
                status="ACTIVE",
                operation_status="ENABLE",
                ext_updated_time=base_time + timedelta(minutes=i),
                is_deleted=False,
                store_id="store",
            )
        )
    # Inactive or filtered out
    db_session.add(
        TTBGmvMaxCampaign(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            campaign_id="inactive",
            promotion_type=PromotionTypeEnum.PRODUCT,
            status="INACTIVE",
            operation_status="ENABLE",
            ext_updated_time=base_time + timedelta(hours=1),
            is_deleted=False,
            store_id="store",
        )
    )
    db_session.add(
        TTBGmvMaxCampaign(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            campaign_id="deleted",
            promotion_type=PromotionTypeEnum.PRODUCT,
            status="DELETED",
            operation_status="DISABLE",
            ext_updated_time=base_time + timedelta(hours=2),
            is_deleted=True,
            store_id="store",
        )
    )
    db_session.commit()

    strategy = MonitoringStrategy(
        id=1,
        workspace_id=1,
        auth_id=None,
        advertiser_id=None,
        store_id=None,
        level="CREATIVE_10MIN",
        interval_minutes=10,
        enabled=True,
        promotion_type="PRODUCT",
        max_campaigns_per_run=None,
    )

    campaigns = service._select_active_campaigns(db_session, strategy)
    assert len(campaigns) == 30
    # Ordered by ext_updated_time desc, so the highest numeric campaign_id is first
    assert campaigns[0].campaign_id == "34"
    assert campaigns[-1].campaign_id == "5"
    assert all(c.operation_status in {None, "ENABLE"} for c in campaigns)
    assert all(c.status == "ACTIVE" for c in campaigns)
