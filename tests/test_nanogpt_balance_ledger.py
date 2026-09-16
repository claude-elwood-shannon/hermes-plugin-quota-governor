"""Unit tests for nanogpt-balance-ledger helpers."""

import datetime as dt
import importlib.util
import unittest

# Load module
spec = importlib.util.spec_from_file_location(
    "ledger", "scripts/nanogpt-balance-ledger.py"
)
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)

class TestNanogptBalanceLedger(unittest.TestCase):

    def test_week_start(self):
        monday = dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.timezone.utc)
        wk_monday = ledger.week_start(monday)
        self.assertEqual(wk_monday.weekday(), 0)
        self.assertEqual(wk_monday.hour, 0)
        self.assertEqual(wk_monday.tzinfo, dt.timezone.utc)

        thursday = dt.datetime(2026, 9, 23, 3, 30, tzinfo=dt.timezone.utc)
        wk = ledger.week_start(thursday)
        self.assertEqual(wk.weekday(), 0)
        self.assertEqual(wk, dt.datetime(2026, 9, 21, 0, 0, tzinfo=dt.timezone.utc))

        naive = dt.datetime(2026, 9, 20)
        wk_naive = ledger.week_start(naive)
        self.assertIsNone(wk_naive.tzinfo)

    def test_window_start_from_period(self):
        period_end = dt.datetime(2026, 10, 1, 5, 0, tzinfo=dt.timezone.utc)
        start = ledger.window_start_from_period(period_end.isoformat(), None)
        self.assertEqual(start, dt.datetime(2026, 9, 14, 0, 0, tzinfo=dt.timezone.utc))

        now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
        fallback = ledger.window_start_from_period(None, now)
        expected_fallback = ledger.week_start(now)
        self.assertEqual(fallback, expected_fallback)

        malformed = ledger.window_start_from_period("not-a-date", now)
        self.assertEqual(malformed, expected_fallback)

    def test_is_covered(self):
        covered_set = {"z-ai/glm-5.2", "qwen3.5-4b"}
        self.assertTrue(ledger.is_covered("z-ai/glm-5.2", covered_set))
        self.assertTrue(ledger.is_covered("Z-AI/glm-5.2", covered_set))
        self.assertTrue(ledger.is_covered("z-unknown/glm-5.2", covered_set))
        self.assertFalse(ledger.is_covered("unknown/model", covered_set))
        self.assertFalse(ledger.is_covered(None, covered_set))
        self.assertFalse(ledger.is_covered("z-ai/glm-5.2", None))
        self.assertFalse(ledger.is_covered("z-ai/glm-5.2", set()))

    def test_compute_level(self):
        def _compute(spent, max_spend, warn_frac):
            return ledger._compute_level(
                {"spent_usd": spent, "max_spend_usd": max_spend, "warn_fraction": warn_frac},
                float(max_spend), float(warn_frac)
            )
        self.assertEqual(_compute(0.3, 1.0, 0.5), "ok")
        self.assertEqual(_compute(0.8, 1.0, 0.5), "warn")
        self.assertEqual(_compute(1.0, 1.0, 0.5), "stop")
        self.assertEqual(_compute(0, 0, 0.5), "stop")
        self.assertEqual(_compute(0.1, 0, 0.5), "stop")

if __name__ == "__main__":
    unittest.main()
