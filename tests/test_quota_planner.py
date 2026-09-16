"""Minimal tests for quota_planner.decide() — the quota decision engine.

quota_planner.py (root of the plugin) was uncovered by the test suite:
scripts/*.py all have a same-named test_*, core modules (quota_governor,
providers, health_checks, concurrency_guard) are imported by existing
tests, but nothing imported quota_planner.  These 3 tests exercise the
three-state decision heuristic (run / paying / stop).

Run:  python3 test_quota_planner.py
"""
import importlib
import importlib.util
import os
import sys
import unittest

GOV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "hermes_plugin_quota_governor"


def _load_planner():
    """Register the package by PATH under its canonical name and return
    the quota_planner module (it uses relative imports: .providers)."""
    if PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PKG,
            os.path.join(GOV_DIR, "__init__.py"),
            submodule_search_locations=[GOV_DIR],
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[PKG] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module(f"{PKG}.quota_planner")


class TestDecideWeeklyHardStop(unittest.TestCase):
    """Weekly > 90% → hard stop regardless of session status."""

    def setUp(self):
        self.planner = _load_planner()

    def test_weekly_over_90_stops_even_with_healthy_session(self):
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=10.0, ollama_weekly_pct=95.0)
        dec = self.planner.decide(snap, spending_limit=5.0)
        self.assertEqual(dec.action, "stop")
        self.assertEqual(dec.max_workers, 0)
        self.assertFalse(dec.should_spawn)
        self.assertIn("weekly quota critical", dec.reason)

    def test_weekly_over_90_stops_even_at_100_session(self):
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=100.0, ollama_weekly_pct=91.0)
        dec = self.planner.decide(snap, spending_limit=5.0)
        self.assertEqual(dec.action, "stop")
        self.assertIn("weekly", dec.reason)


class TestDecidePayAsYouGo(unittest.TestCase):
    """Session exhausted + rising cost → paying until spend limit."""

    def setUp(self):
        self.planner = _load_planner()

    def test_rising_cost_below_limit_is_paying(self):
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=100.0, ollama_weekly_pct=10.0,
            ollama_activity_cost=1.5)
        dec = self.planner.decide(snap, prev_activity_cost=0.5,
                                  spending_limit=5.0)
        self.assertEqual(dec.action, "paying")
        self.assertEqual(dec.max_workers, 1)
        self.assertEqual(dec.max_task_cost, "small")
        self.assertTrue(dec.should_spawn)
        self.assertIn("pay-as-you-go", dec.reason)
        self.assertNotEqual(dec.paying_warning, "")

    def test_cost_hits_spend_limit_stops(self):
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=100.0, ollama_weekly_pct=10.0,
            ollama_activity_cost=5.0)
        dec = self.planner.decide(snap, prev_activity_cost=4.0,
                                  spending_limit=5.0)
        self.assertEqual(dec.action, "stop")
        self.assertEqual(dec.max_workers, 0)
        self.assertIn("spending limit reached", dec.reason)


class TestDecideHealthy(unittest.TestCase):
    """Plenty of quota → run healthy, 2 workers on any task size."""

    def setUp(self):
        self.planner = _load_planner()

    def test_low_session_and_weekly_allows_3_workers_any(self):
        """P2 desired=3 (t_acf726e6): cuota sana = 3 workers, el mínimo
        operativo del backlog-guard (ready_assigned + running >= 3)."""
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=20.0, ollama_weekly_pct=20.0)
        dec = self.planner.decide(snap, spending_limit=5.0)
        self.assertEqual(dec.action, "run")
        self.assertEqual(dec.max_workers, 3)
        self.assertEqual(dec.max_task_cost, "any")
        self.assertTrue(dec.should_spawn)
        self.assertIn("quota healthy", dec.reason)

    def test_low_session_but_high_weekly_throttles_to_one_worker(self):
        snap = self.planner.QuotaSnapshot(
            ollama_session_pct=20.0, ollama_weekly_pct=60.0)
        dec = self.planner.decide(snap, spending_limit=5.0)
        self.assertEqual(dec.action, "run")
        self.assertEqual(dec.max_workers, 1)
        self.assertEqual(dec.max_task_cost, "any")


if __name__ == "__main__":
    unittest.main()
