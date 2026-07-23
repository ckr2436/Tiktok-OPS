from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.tiktok_business.gmvmax_client import GMVMaxCampaignUpdateBody
from app.services.gmvmax_smart_guard import (
    CatalogCampaign,
    RealtimeMetrics,
    _dynamic_cooldown_minutes,
    _fallback_controlled_test_budget,
    _normalize_roas_bid,
    _performance_test_plan,
    _review_two_stage_decision,
)


class GmvMaxRecoveryPolicyTests(unittest.TestCase):
    def test_tiktok_request_normalizes_roas_to_one_decimal(self) -> None:
        body = GMVMaxCampaignUpdateBody(campaign_id="campaign", roas_bid=2.25)
        self.assertEqual(2.3, body.roas_bid)

    def test_delivery_rebuild_can_round_roas_down(self) -> None:
        self.assertEqual(Decimal("2.2"), _normalize_roas_bid("2.25", rounding=ROUND_DOWN))

    def test_peak_fallback_uses_meaningful_part_of_safe_range(self) -> None:
        budget = _fallback_controlled_test_budget(
            guard={
                "peak_recovery_min_confidence": "0.35",
                "peak_recovery_min_multiplier": "1.15",
            },
            budget_bounds={"min_cents": 520, "max_cents": 1040},
            order_timing={"confidence": 0.60, "delivery_multiplier": 1.5},
        )
        self.assertGreaterEqual(budget, 800)
        self.assertLessEqual(budget, 1040)

    def test_peak_caps_non_hard_cooldown(self) -> None:
        cooldown = _dynamic_cooldown_minutes(
            guard={
                "dynamic_cooldown_enabled": True,
                "peak_recovery_enabled": True,
                "peak_recovery_min_confidence": "0.35",
                "peak_recovery_min_multiplier": "1.15",
                "peak_recovery_cooldown_cap_minutes": 20,
                "max_pause_cooldown_minutes": 360,
            },
            reason_key="window_no_order",
            base_minutes=45,
            metrics=RealtimeMetrics(cost_cents=2000),
            product_stats={"cost_cents": 2000, "orders": 0},
            failure_stats={"failure_count": 4},
            min_roi=Decimal("0.8"),
            no_order_spend_cents=500,
            order_timing={"confidence": 0.60, "delivery_multiplier": 1.5},
        )
        self.assertEqual(20, cooldown)

    def test_performance_test_has_enough_budget_for_evidence(self) -> None:
        plan = _performance_test_plan(
            guard={"controlled_test_performance_min_budget_cents": 800},
            threshold_context={"allowed_cpa_cents": 267, "recent_momentum": {}},
            campaign=SimpleNamespace(budget_value=2000),
            probe_spend_cents=50,
            probe_elapsed_minutes=5,
        )
        self.assertGreaterEqual(plan["budget_cents"], 800)

    def test_hermes_hold_falls_back_to_bounded_peak_test(self) -> None:
        now = datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)
        strategy = SimpleNamespace(
            config_json={"smart_guard": {}, "smart_guard_state": {}},
            cooldown_minutes=45,
        )
        campaign = CatalogCampaign(
            workspace_id=3,
            auth_id=3,
            advertiser_id="advertiser",
            store_id="store",
            campaign_id="campaign",
            campaign_name="campaign",
            promotion_type="PRODUCT",
            operation_status="DISABLE",
            secondary_status="CAMPAIGN_STATUS_DISABLE",
            budget_value=3800,
            roas_bid=Decimal("2.5"),
        )
        decision = {
            "action": "START",
            "reason": "cooldown complete",
            "requires_hermes_review": True,
            "controlled_test_required": True,
            "threshold_context": {
                "order_timing": {"confidence": 0.60, "delivery_multiplier": 1.5},
                "recovery_control": {
                    "eligible": True,
                    "test_budget_bounds": {"min_cents": 520, "max_cents": 1040},
                },
            },
        }
        hermes_hold = {
            "status": "reviewed",
            "decision": "HOLD",
            "confidence": "low",
            "reason": "insufficient evidence",
            "review_after_minutes": 15,
            "budget_multiplier": None,
            "test_budget_cents": None,
            "reviewed_at": now.isoformat(),
        }
        with patch(
            "app.services.gmvmax_smart_guard.review_smart_guard_action",
            new=AsyncMock(return_value=hermes_hold),
        ):
            reviewed = asyncio.run(
                _review_two_stage_decision(
                    strategy=strategy,
                    campaign=campaign,
                    metrics=RealtimeMetrics(fetched_at=now),
                    decision=decision,
                    now=now,
                )
            )

        self.assertEqual("START", reviewed["action"])
        self.assertGreaterEqual(reviewed["controlled_test_budget_cents"], 800)
        self.assertEqual("bounded_controlled_test", reviewed["hermes_review"]["policy_resolution"])


if __name__ == "__main__":
    unittest.main()
