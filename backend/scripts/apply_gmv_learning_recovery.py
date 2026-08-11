import asyncio
import json
from decimal import Decimal

from sqlalchemy import text

from app.data.db import SessionLocal
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxBidRecommendRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
)
from app.services.ttb_client_factory import build_ttb_gmvmax_client


CAMPAIGNS = {
    "1732408101147349662": "1870397475273922",
    "1732450234518246046": "1870397489237154",
}
ERRONEOUS_LOW_SPEND_REBUILDS = ("1870407108876657",)
ADVERTISER_ID = "7642678413060538386"
STORE_ID = "7494654726488884894"

LEARNING_POLICY = {
    "daily_budget_pacing": False,
    "window_stop_enabled": False,
    "data_conflict_protective_pause_enabled": False,
    "fresh_campaign_controlled_test_enabled": False,
    "learning_min_run_minutes": 1440,
    "learning_target_minutes": 4320,
    "learning_exit_orders": 20,
    "learning_emergency_min_spend_cents": 3000,
    "controlled_test_min_budget_cents": 1000,
    "controlled_test_max_budget_cents": 5000,
    "controlled_test_delivery_probe_minutes": 120,
    "controlled_test_performance_min_budget_cents": 1500,
    "controlled_test_performance_min_minutes": 360,
    "controlled_test_performance_max_minutes": 1440,
    "controlled_test_budget_floor_cents": 5000,
    "budget_scale_max_raise_pct": "0.20",
    "budget_scale_cooldown_minutes": 1440,
    "roas_freeze_minutes": 4320,
}


def migrate_policy(db) -> None:
    strategy_rows = db.execute(
        text("select id, config_json from gmv_strategy_configs where id in (70, 71) for update")
    ).mappings().all()
    for row in strategy_rows:
        config = row["config_json"] or {}
        if isinstance(config, str):
            config = json.loads(config)
        config = dict(config)
        smart_guard = dict(config.get("smart_guard") or {})
        smart_guard.update(LEARNING_POLICY)
        config["smart_guard"] = smart_guard
        creative_guard = dict(config.get("creative_guard") or {})
        creative_guard["no_spend_reset"] = {
            **dict(creative_guard.get("no_spend_reset") or {}),
            "enabled": False,
        }
        creative_guard["product_card_reset"] = {
            **dict(creative_guard.get("product_card_reset") or {}),
            "enabled": True,
            "recreate": True,
            "disable_old_strategy": True,
            "protect_good_campaign": True,
            "min_product_card_spend_share": "0.70",
            "require_video_starvation": True,
            "max_video_spend_share": "0.20",
        }
        config["creative_guard"] = creative_guard
        db.execute(
            text("update gmv_strategy_configs set config_json=:config where id=:strategy_id"),
            {"config": json.dumps(config, ensure_ascii=False), "strategy_id": int(row["id"])},
        )

    runtime_rows = db.execute(
        text(
            """
            select strategy_id, runtime_json
              from gmv_campaign_realtime_state
             where strategy_id in (70, 71)
             for update
            """
        )
    ).mappings().all()
    for row in runtime_rows:
        runtime = row["runtime_json"] or {}
        if isinstance(runtime, str):
            runtime = json.loads(runtime)
        runtime = dict(runtime)
        runtime["smart_guard_state"] = {
            "controlled_test": {
                "active": False,
                "status": "MIGRATED_TO_STABLE_LEARNING",
                "rebuild_pending": False,
            },
            "paused_until": None,
            "policy_version": "stable_learning_v1",
        }
        db.execute(
            text(
                """
                update gmv_campaign_realtime_state
                   set runtime_json=:runtime_json, paused_until=null,
                       last_action='HOLD',
                       last_reason='smart_guard: migrated to stable GMV Max learning policy',
                       state_version=state_version+1
                 where strategy_id=:strategy_id
                """
            ),
            {
                "runtime_json": json.dumps(runtime, ensure_ascii=False),
                "strategy_id": int(row["strategy_id"]),
            },
        )
    db.commit()


async def main() -> None:
    with SessionLocal() as db:
        migrate_policy(db)
        client = build_ttb_gmvmax_client(db, auth_id=3)
        try:
            if ERRONEOUS_LOW_SPEND_REBUILDS:
                disabled = await client.campaign_status_update(
                    CampaignStatusUpdateRequest(
                        advertiser_id=ADVERTISER_ID,
                        campaign_ids=list(ERRONEOUS_LOW_SPEND_REBUILDS),
                        operation_status="DISABLE",
                    )
                )
                db.execute(
                    text(
                        """
                        update gmvmax_product_campaign_catalog
                           set operation_status='DISABLE', updated_at=utc_timestamp(6)
                         where campaign_id in ('1870407108876657')
                        """
                    )
                )
                db.execute(
                    text(
                        """
                        update gmv_strategy_configs
                           set enabled=case when id=70 then 1 when id=72 then 0 else enabled end
                         where id in (70,72)
                        """
                    )
                )
                db.commit()
                print(
                    {
                        "disabled_erroneous_rebuilds": list(ERRONEOUS_LOW_SPEND_REBUILDS),
                        "request_id": disabled.request_id,
                    }
                )
            for product_id, campaign_id in CAMPAIGNS.items():
                recommendation = await client.gmv_max_bid_recommend(
                    GMVMaxBidRecommendRequest(
                        advertiser_id=ADVERTISER_ID,
                        store_id=STORE_ID,
                        shopping_ads_type="PRODUCT",
                        optimization_goal="VALUE",
                        item_group_ids=[product_id],
                    )
                )
                roas = Decimal(str(recommendation.data.roas_bid))
                budget = Decimal(str(recommendation.data.budget))
                if not Decimal("0.5") <= roas <= Decimal("3.0"):
                    raise RuntimeError(f"unsafe recommended ROAS for {product_id}: {roas}")
                if not Decimal("20") <= budget <= Decimal("300"):
                    raise RuntimeError(f"unsafe recommended budget for {product_id}: {budget}")

                update = await client.gmv_max_campaign_update(
                    GMVMaxCampaignUpdateRequest(
                        advertiser_id=ADVERTISER_ID,
                        body=GMVMaxCampaignUpdateBody(
                            campaign_id=campaign_id,
                            budget=float(budget),
                            roas_bid=float(roas),
                        ),
                    )
                )
                start = await client.campaign_status_update(
                    CampaignStatusUpdateRequest(
                        advertiser_id=ADVERTISER_ID,
                        campaign_ids=[campaign_id],
                        operation_status="ENABLE",
                    )
                )
                db.execute(
                    text(
                        """
                        update gmvmax_product_campaign_catalog
                           set operation_status='ENABLE', budget_cents=:budget_cents,
                               roas_bid=:roas, updated_at=utc_timestamp(6)
                         where workspace_id=3 and auth_id=3 and campaign_id=:campaign_id
                        """
                    ),
                    {
                        "budget_cents": int(budget * 100),
                        "roas": str(roas),
                        "campaign_id": campaign_id,
                    },
                )
                db.execute(
                    text(
                        """
                        update gmv_campaign_realtime_state
                           set operation_status='ENABLE', paused_until=null,
                               last_action='START',
                               last_reason='smart_guard: stable learning started with official TikTok recommendation'
                         where strategy_id in (70, 71) and campaign_id=:campaign_id
                        """
                    ),
                    {"campaign_id": campaign_id},
                )
                db.commit()
                print(
                    {
                        "product_id": product_id,
                        "campaign_id": campaign_id,
                        "recommended_budget": str(budget),
                        "recommended_roas": str(roas),
                        "update_request_id": update.request_id,
                        "start_request_id": start.request_id,
                    }
                )
        finally:
            await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
