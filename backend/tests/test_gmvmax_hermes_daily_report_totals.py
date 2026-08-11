from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.services.gmvmax_hermes_daily_report import (
    _extract_guard_event_identity,
    _load_campaign_summary,
    _reconcile_report_summary,
    _report_evidence_fingerprint,
    _report_final_cutoff_reached,
    _report_generation_ready,
    _scope_report_date,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _CampaignSummaryDb:
    def __init__(self):
        self.statement = ""
        self.params = {}

    def execute(self, statement, params):
        self.statement = str(statement).lower()
        self.params = dict(params)
        return _Rows(
            [
                {
                    "campaign_id": "campaign-1",
                    "campaign_name": "软糖",
                    "operation_status": "ENABLE",
                    "secondary_status": "DELIVERING",
                    "budget_cents": 10000,
                    "roas_bid": 2.5,
                    "cost_cents": 1234,
                    "gross_revenue_cents": 4321,
                    "orders": 3,
                    "item_group_ids": "product-1,product-2",
                    "product_titles": "软糖 A | 软糖 B",
                }
            ]
        )


class HermesDailyReportTotalsTests(unittest.TestCase):
    def test_campaign_metrics_are_aggregated_before_item_group_join(self) -> None:
        db = _CampaignSummaryDb()

        rows = _load_campaign_summary(
            db,
            {
                "workspace_id": 3,
                "auth_id": 3,
                "advertiser_id": "advertiser-1",
                "store_id": "store-1",
            },
            date(2026, 7, 16),
        )

        self.assertEqual(12.34, rows[0]["cost"])
        self.assertEqual(43.21, rows[0]["gmv"])
        self.assertEqual(3, rows[0]["orders"])
        self.assertEqual(["product-1", "product-2"], rows[0]["item_group_ids"])

        sql = db.statement
        metrics_start = sql.index("from gmvmax_product_campaign_metrics_daily")
        metrics_end = sql.index(") m", metrics_start)
        products_start = sql.index("from gmvmax_product_campaign_item_groups")
        products_end = sql.index(") ig", products_start)
        self.assertLess(metrics_end, products_start)
        self.assertIn("group by", sql[metrics_start:metrics_end])
        self.assertNotIn(
            "gmvmax_product_campaign_item_groups",
            sql[metrics_start:metrics_end],
        )
        self.assertIn("group by", sql[products_start:products_end])
        self.assertNotIn(
            "gmvmax_product_campaign_metrics_daily",
            sql[products_start:products_end],
        )
        self.assertNotIn("sum(m.cost_cents)", sql)

    def test_overview_totals_override_incomplete_campaign_backfill(self) -> None:
        summary, quality = _reconcile_report_summary(
            [{"cost": 216.94, "gmv": 112.95, "orders": 12, "status": "DISABLE"}],
            {
                "source": "overview_daily",
                "cost_cents": 49946,
                "net_cost_cents": 48294,
                "gross_revenue_cents": 12252,
                "orders": 13,
            },
        )

        self.assertEqual(499.46, summary["cost"])
        self.assertEqual(482.94, summary["net_cost"])
        self.assertEqual(122.52, summary["gmv"])
        self.assertEqual(13, summary["orders"])
        self.assertFalse(quality["campaign_detail_complete"])
        self.assertEqual(282.52, quality["difference"]["cost"])

    def test_campaign_totals_are_explicit_fallback_without_overview(self) -> None:
        summary, quality = _reconcile_report_summary(
            [{"cost": 12.34, "gmv": 20.0, "orders": 2, "status": "ENABLE"}],
            None,
        )

        self.assertEqual(12.34, summary["cost"])
        self.assertEqual("campaign_daily_fallback", summary["summary_source"])
        self.assertFalse(quality["authoritative_totals"])

    def test_snapshot_is_visible_but_not_authoritative(self) -> None:
        summary, quality = _reconcile_report_summary(
            [{"cost": 12.0, "gmv": 0.0, "orders": 0, "status": "ENABLE"}],
            {
                "source": "overview_snapshot",
                "authoritative": False,
                "cost_cents": 1250,
                "net_cost_cents": 1100,
                "gross_revenue_cents": 0,
                "orders": 0,
            },
        )

        self.assertEqual(12.5, summary["cost"])
        self.assertFalse(quality["authoritative_totals"])
        self.assertFalse(quality["campaign_detail_complete"])

    def test_guard_event_identity_uses_decision_and_retest_payloads(self) -> None:
        identity = _extract_guard_event_identity(
            {
                "body": {"item_list": [{"item_id": "video-1", "spu_id_list": ["product-1"]}]},
                "decision": {"context": {"creative_id": "video-1", "item_group_id": "product-1"}},
                "retest": {"attempt": 2, "cooldown_minutes": 53, "time_bucket": "00-04"},
            }
        )

        self.assertEqual("video-1", identity["creative_id"])
        self.assertEqual("product-1", identity["item_group_id"])
        self.assertEqual(2, identity["retest_attempt"])
        self.assertEqual(53, identity["retest_cooldown_minutes"])

    def test_evidence_fingerprint_ignores_finalization_metadata(self) -> None:
        initial = {"summary": {"cost": 10}, "data_quality": {"report_finalized": False}}
        final = {"summary": {"cost": 10}, "data_quality": {"report_finalized": True}}
        self.assertEqual(_report_evidence_fingerprint(initial), _report_evidence_fingerprint(final))

    def test_force_always_reaches_final_cutoff(self) -> None:
        self.assertTrue(
            _report_final_cutoff_reached(datetime(2026, 7, 16, 0, 30), force=True)
        )

    def test_current_advertiser_day_cannot_generate(self) -> None:
        ready, reason, _ = _report_generation_ready(
            "America/New_York",
            date(2026, 7, 10),
            now=datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(ready)
        self.assertEqual("advertiser_day_not_closed", reason)

    def test_report_date_requires_advertiser_api_timezone(self) -> None:
        with self.assertRaisesRegex(Exception, "Advertiser timezone"):
            _scope_report_date(None, None)


if __name__ == "__main__":
    unittest.main()
