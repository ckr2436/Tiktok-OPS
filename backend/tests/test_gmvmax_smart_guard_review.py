from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.gmvmax_smart_guard import (
    CatalogCampaign,
    RealtimeMetrics,
    _active_campaign_conflict_backoff,
    _is_active_campaign_conflict,
    _prepare_two_stage_decision,
    _review_two_stage_decision,
)
from app.services.ttb_api import TTBBusinessError


class SmartGuardActiveCampaignConflictTests(unittest.TestCase):
    def test_recognizes_active_gmv_max_conflict(self) -> None:
        conflict = TTBBusinessError(
            "This product is already part of an active GMV Max campaign.",
            code=40002,
        )
        other = TTBBusinessError("Invalid request.", code=40002)

        self.assertTrue(_is_active_campaign_conflict(conflict))
        self.assertFalse(_is_active_campaign_conflict(other))

    def test_conflict_retry_uses_bounded_exponential_backoff(self) -> None:
        strategy = SimpleNamespace(
            config_json={
                "smart_guard_state": {
                    "last_decision": {
                        "decision_phase": "START_CONFLICT_HOLD",
                        "start_conflict_count": 2,
                    }
                }
            }
        )

        count, minutes = _active_campaign_conflict_backoff(
            strategy,
            guard={
                "active_campaign_conflict_retry_minutes": 15,
                "max_pause_cooldown_minutes": 45,
            },
        )

        self.assertEqual(3, count)
        self.assertEqual(45, minutes)


class SmartGuardCooldownReviewTests(unittest.TestCase):
    def _strategy(self) -> SimpleNamespace:
        return SimpleNamespace(
            cooldown_minutes=30,
            config_json={
                "smart_guard": {
                    "hermes_action_review_enabled": True,
                    "protection_pause_minutes": 15,
                    "max_pause_cooldown_minutes": 360,
                },
                "smart_guard_state": {},
            },
        )

    def _campaign(self) -> CatalogCampaign:
        return CatalogCampaign(
            workspace_id=3,
            auth_id=3,
            advertiser_id="advertiser",
            store_id="store",
            campaign_id="campaign",
            campaign_name="campaign",
            promotion_type="PRODUCT",
            operation_status="DISABLE",
            secondary_status="CAMPAIGN_STATUS_DISABLE",
            budget_value=2000,
            roas_bid=None,
        )

    def _review(self, *, now: datetime, deadline: datetime, review_after: int) -> dict:
        strategy = self._strategy()
        campaign = self._campaign()
        prepared = _prepare_two_stage_decision(
            strategy=strategy,
            campaign=campaign,
            decision={
                "action": "HOLD",
                "reason": "smart_guard: cooldown active",
                "paused_until": deadline.isoformat(),
                "threshold_context": {},
                "decision_phase": "COOLDOWN",
            },
            now=now,
        )
        hermes_review = {
            "status": "reviewed",
            "decision": "REVISE",
            "confidence": "high",
            "reason": "recheck later",
            "review_after_minutes": review_after,
            "reviewed_at": now.isoformat(),
        }
        with patch(
            "app.services.gmvmax_smart_guard.review_smart_guard_action",
            new=AsyncMock(return_value=hermes_review),
        ):
            return asyncio.run(
                _review_two_stage_decision(
                    strategy=strategy,
                    campaign=campaign,
                    metrics=RealtimeMetrics(fetched_at=now),
                    decision=prepared,
                    now=now,
                )
            )

    def test_review_cannot_roll_cooldown_deadline_forward(self) -> None:
        now = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(minutes=30)

        reviewed = self._review(now=now, deadline=deadline, review_after=45)

        self.assertEqual(deadline.isoformat(), reviewed["paused_until"])

    def test_review_can_shorten_cooldown_deadline(self) -> None:
        now = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(minutes=30)

        reviewed = self._review(now=now, deadline=deadline, review_after=10)

        self.assertEqual((now + timedelta(minutes=10)).isoformat(), reviewed["paused_until"])


if __name__ == "__main__":
    unittest.main()
