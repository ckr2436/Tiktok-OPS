from __future__ import annotations

import unittest

from app.services.gmvmax_hermes_advisor import (
    _realtime_review_required,
    _sanitize_realtime_review,
)


class HermesRealtimeReviewTests(unittest.TestCase):
    def test_rejects_unknown_decision(self) -> None:
        self.assertEqual({}, _sanitize_realtime_review({"decision": "PAUSE"}))

    def test_clamps_and_filters_parameter_overrides(self) -> None:
        review = _sanitize_realtime_review(
            {
                "decision": "revise",
                "confidence": 0.94,
                "reason": "bounded correction",
                "parameter_overrides": {
                    "monitor_interval_minutes": 99,
                    "pause_cooldown_minutes": 500,
                    "min_spend_cents": 1,
                    "min_roi": 9,
                    "budget": 999999,
                    "operation": "PAUSE",
                },
            }
        )

        self.assertEqual("REVISE", review["decision"])
        self.assertEqual("high", review["confidence"])
        self.assertEqual(
            {
                "monitor_interval_minutes": 5,
                "pause_cooldown_minutes": 60,
                "min_spend_cents": 300,
                "min_roi": 1.2,
            },
            review["parameter_overrides"],
        )

    def test_review_is_only_requested_for_material_risk(self) -> None:
        self.assertFalse(
            _realtime_review_required(
                {"stats_24h": {"samples": 1, "latest_cost_cents": 5000, "latest_orders": 0}}
            )
        )
        self.assertTrue(
            _realtime_review_required(
                {
                    "allowed_cpa_cents": 1000,
                    "stats_24h": {"samples": 3, "latest_cost_cents": 1200, "latest_orders": 0},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
