from __future__ import annotations

import unittest
from decimal import Decimal

from app.services.gmvmax_hermes_memory import classify_policy_memory


class HermesMemoryPolicyTests(unittest.TestCase):
    def test_repeated_success_across_independent_days_is_validated(self) -> None:
        status, confidence = classify_policy_memory(
            independent_days=4,
            successes=3,
            failures=1,
            orders=8,
            weighted_roi=Decimal("1.25"),
            weighted_target=Decimal("1.2"),
        )
        self.assertEqual("VALIDATED", status)
        self.assertGreaterEqual(confidence, Decimal("0.6"))

    def test_single_day_never_becomes_validated(self) -> None:
        status, _confidence = classify_policy_memory(
            independent_days=1,
            successes=50,
            failures=0,
            orders=100,
            weighted_roi=Decimal("5"),
            weighted_target=Decimal("1"),
        )
        self.assertEqual("CANDIDATE", status)

    def test_repeated_failure_retires_memory(self) -> None:
        status, _confidence = classify_policy_memory(
            independent_days=5,
            successes=0,
            failures=5,
            orders=0,
            weighted_roi=Decimal("0"),
            weighted_target=Decimal("1"),
        )
        self.assertEqual("RETIRED", status)
