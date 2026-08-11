from __future__ import annotations

import unittest

from app.services.gmvmax_hermes_context import (
    select_campaign_signals,
    summarize_guard_events,
)


class HermesReportContextTests(unittest.TestCase):
    def test_campaign_selection_omits_inactive_zero_rows(self) -> None:
        selected = select_campaign_signals(
            [
                {"campaign_id": "spending", "status": "ENABLE", "cost": 12.0},
                {"campaign_id": "active", "status": "ENABLE", "cost": 0.0},
                {"campaign_id": "history", "status": "DISABLE", "cost": 0.0},
            ]
        )

        self.assertEqual({"spending", "active"}, {row["campaign_id"] for row in selected})

    def test_guard_rollup_preserves_distinct_material_identity(self) -> None:
        summary = summarize_guard_events(
            [
                {
                    "campaign_id": "campaign-1",
                    "creative_id": "video-1",
                    "item_group_id": "product-1",
                    "event_type": "CREATIVE_GUARD",
                    "action": "REMOVE",
                    "result": "SUCCESS",
                    "reason": "no_order",
                    "created_at": "2026-07-15 10:00:00",
                },
                {
                    "campaign_id": "campaign-1",
                    "creative_id": "video-2",
                    "item_group_id": "product-1",
                    "event_type": "CREATIVE_GUARD",
                    "action": "REMOVE",
                    "result": "SUCCESS",
                    "reason": "no_order",
                    "created_at": "2026-07-15 11:00:00",
                },
            ]
        )

        self.assertEqual(2, summary["distinct_creatives"])
        self.assertEqual(2, summary["groups"][0]["distinct_creatives"])
        self.assertEqual(2, len(summary["material_groups"]))


if __name__ == "__main__":
    unittest.main()
