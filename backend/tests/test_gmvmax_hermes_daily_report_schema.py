from __future__ import annotations

import unittest

from app.services.gmvmax_hermes_daily_report import _normalize_report_output


class HermesDailyReportSchemaTests(unittest.TestCase):
    def test_requires_markdown_report(self) -> None:
        self.assertEqual({}, _normalize_report_output({"recommendations": []}))

    def test_normalizes_recommendation_shape(self) -> None:
        parsed = _normalize_report_output(
            {
                "markdown_report": "# 日报",
                "recommendations": [
                    {
                        "scope": "product:1",
                        "priority": "high",
                        "action": "保持观察",
                        "reason": "订单归因仍在回传",
                        "unexpected": "ignored",
                    },
                    "invalid",
                ],
                "risk_flags": ["数据延迟"],
                "next_actions": ["等待完整归因窗口"],
            }
        )

        self.assertEqual("# 日报", parsed["markdown_report"])
        self.assertEqual(1, len(parsed["recommendations"]))
        self.assertNotIn("unexpected", parsed["recommendations"][0])

    def test_normalizes_structured_risks_and_actions_as_readable_text(self) -> None:
        parsed = _normalize_report_output(
            {
                "markdown_report": "# 日报",
                "risk_flags": [
                    {"level": "high", "flag": "整体 ROI 偏低", "detail": "样本仅 2 单"}
                ],
                "next_actions": [
                    {"timing": "明日开投前", "action": "校验素材与计划归属"}
                ],
            }
        )

        self.assertEqual("[high] 整体 ROI 偏低：样本仅 2 单", parsed["risk_flags"][0])
        self.assertEqual("明日开投前：校验素材与计划归属", parsed["next_actions"][0])


if __name__ == "__main__":
    unittest.main()
