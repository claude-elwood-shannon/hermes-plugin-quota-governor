"""Unit tests for scripts/quota-gate.py — importlib recipe, no network.

Covers the four test groups required by OBJ-44 semilla 23 (t_a1f17288):

1. select_provider: highest-availability wins, all-exhausted → None,
   pr-openrouter never a candidate (parked).
2. select_provider: provider with non-empty error is excluded; others
   evaluated normally (no fail-open).
3. compute_zombie_check: running task older than 45 min → has_zombie True;
   young running task → False.  Uses a temp SQLite DB, deterministic now.
4. bottleneck_to_max_cost / bottleneck_to_max_workers: monotonicity and
   edge limits at 0 and 100.

Run:
  python3 -m pytest tests/test_quota_gate.py -q
  python3 tests/test_quota_gate.py   # direct unittest
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

# ── Import via file path (verified recipe — 0.03 s, no side effects) ─────────
_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts"
)
_SPEC = importlib.util.spec_from_file_location(
    "quota_gate",
    os.path.join(_SCRIPT_DIR, "quota-gate.py"),
)
qg = importlib.util.module_from_spec(_SPEC)
sys.modules["quota_gate"] = qg
_SPEC.loader.exec_module(qg)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _provider(profile="pr-ollama", availability=80.0, bottleneck=20.0,
              error="", provider="ollama-cloud", model="glm-5.2",
              bottleneck_window="session"):
    return {
        "profile": profile,
        "provider": provider,
        "model": model,
        "availability": availability,
        "bottleneck_pct": bottleneck,
        "bottleneck_window": bottleneck_window,
        "error": error,
        "raw": {},
    }


def _create_zombie_db(path):
    """Create a minimal kanban.db with the columns compute_zombie_check reads."""
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            started_at REAL,
            last_heartbeat_at REAL
        );
        """
    )
    con.commit()
    return con


def _insert_task(con, id, title="t", assignee="w", status="running",
                 started_at=None, last_heartbeat_at=None):
    con.execute(
        "INSERT INTO tasks (id, title, assignee, status, started_at, "
        "last_heartbeat_at) VALUES (?,?,?,?,?,?)",
        (id, title, assignee, status, started_at, last_heartbeat_at),
    )
    con.commit()


# ── Test Group 1: select_provider — availability, exhaustion, parked ─────────

class TestSelectProviderAvailability(unittest.TestCase):

    def test_highest_availability_wins(self):
        """The provider with the highest availability is selected."""
        providers = [
            _provider(profile="pr-ollama", availability=40),
            _provider(profile="pr-nanogpt", availability=80),
        ]
        result = qg.select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_all_exhausted_returns_none(self):
        """When every provider has availability 0, recommendation is None."""
        providers = [
            _provider(profile="pr-ollama", availability=0),
            _provider(profile="pr-nanogpt", availability=0),
        ]
        result = qg.select_provider(providers)
        self.assertIsNone(result)

    def test_openrouter_never_candidate(self):
        """pr-openrouter is parked → never enters the candidate set,
        even with the highest availability."""
        providers = [
            _provider(profile="pr-ollama", availability=40),
            _provider(profile="pr-nanogpt", availability=50),
            _provider(profile="pr-openrouter", availability=100,
                      provider="openrouter"),
        ]
        result = qg.select_provider(providers, parked={"pr-openrouter"})
        self.assertIsNotNone(result)
        self.assertNotEqual(result["profile"], "pr-openrouter")


# ── Test Group 2: select_provider — error handling ───────────────────────────

class TestSelectProviderError(unittest.TestCase):

    def test_errored_provider_excluded(self):
        """A provider with non-empty error is excluded; the other provider
        is selected normally (no fail-open to the errored one)."""
        providers = [
            _provider(profile="pr-ollama", availability=90, error="timeout"),
            _provider(profile="pr-nanogpt", availability=50, error=""),
        ]
        result = qg.select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_all_errored_returns_none(self):
        """When every provider has an error, no candidate survives."""
        providers = [
            _provider(profile="pr-ollama", availability=90, error="500"),
            _provider(profile="pr-nanogpt", availability=80, error="403"),
        ]
        result = qg.select_provider(providers)
        self.assertIsNone(result)

    def test_error_does_not_block_others(self):
        """One errored provider does not affect evaluation of the rest."""
        providers = [
            _provider(profile="pr-ollama", availability=70, error=""),
            _provider(profile="pr-nanogpt", availability=30, error="dns"),
        ]
        result = qg.select_provider(providers)
        self.assertEqual(result["profile"], "pr-ollama")


# ── Test Group 3: compute_zombie_check ───────────────────────────────────────

class TestComputeZombieCheck(unittest.TestCase):

    def test_old_running_task_is_zombie(self):
        """A running task with started_at > 45 min ago → has_zombie True."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = _create_zombie_db(db_path)
            now = 1_000_000_000.0  # deterministic
            old_started = now - (46 * 60)  # 46 min ago → zombie
            _insert_task(con, "t_old", started_at=old_started)
            con.close()

            result = qg.compute_zombie_check(
                kanban_db_path=db_path, now=now
            )
            self.assertTrue(result["has_zombie"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["tasks"][0]["id"], "t_old")
        finally:
            os.unlink(db_path)

    def test_young_running_task_not_zombie(self):
        """A running task with started_at < 45 min ago → has_zombie False."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = _create_zombie_db(db_path)
            now = 1_000_000_000.0
            young_started = now - (10 * 60)  # 10 min ago → not zombie
            _insert_task(con, "t_young", started_at=young_started)
            con.close()

            result = qg.compute_zombie_check(
                kanban_db_path=db_path, now=now
            )
            self.assertFalse(result["has_zombie"])
            self.assertEqual(result["count"], 0)
        finally:
            os.unlink(db_path)

    def test_non_running_task_ignored(self):
        """A completed task with an old started_at is NOT a zombie."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = _create_zombie_db(db_path)
            now = 1_000_000_000.0
            old_started = now - (120 * 60)  # 2h ago but completed
            _insert_task(con, "t_done", status="done",
                         started_at=old_started)
            con.close()

            result = qg.compute_zombie_check(
                kanban_db_path=db_path, now=now
            )
            self.assertFalse(result["has_zombie"])
        finally:
            os.unlink(db_path)

    def test_heartbeat_preferred_over_started_at(self):
        """When last_heartbeat_at is present, it overrides started_at.
        A task with an old started_at but a recent heartbeat is NOT a zombie."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = _create_zombie_db(db_path)
            now = 1_000_000_000.0
            old_started = now - (120 * 60)  # 2h ago
            recent_hb = now - (5 * 60)      # 5 min ago
            _insert_task(con, "t_hb", started_at=old_started,
                         last_heartbeat_at=recent_hb)
            con.close()

            result = qg.compute_zombie_check(
                kanban_db_path=db_path, now=now
            )
            self.assertFalse(result["has_zombie"])
        finally:
            os.unlink(db_path)

    def test_missing_db_fail_open(self):
        """No kanban.db file → fail open (has_zombie False, no exception)."""
        result = qg.compute_zombie_check(
            kanban_db_path="/nonexistent/path/kanban.db", now=1_000_000.0
        )
        self.assertFalse(result["has_zombie"])
        self.assertEqual(result["count"], 0)


# ── Test Group 4: bottleneck_to_max_cost / bottleneck_to_max_workers ─────────

class TestBottleneckToMaxCost(unittest.TestCase):

    def test_edge_zero(self):
        self.assertEqual(qg.bottleneck_to_max_cost(0), "any")

    def test_edge_hundred(self):
        self.assertEqual(qg.bottleneck_to_max_cost(100), "micro")

    def test_monotonic_decreasing(self):
        """Higher bottleneck → same or stricter cost tier (never looser)."""
        tier_order = {"any": 0, "medium": 1, "small": 2, "tiny": 3, "micro": 4}
        prev_tier = -1
        for b in range(0, 101):
            tier = qg.bottleneck_to_max_cost(b)
            self.assertGreaterEqual(tier_order[tier], prev_tier,
                f"bottleneck {b}: tier {tier} is looser than previous")
            prev_tier = tier_order[tier]


class TestBottleneckToMaxWorkers(unittest.TestCase):

    def test_edge_zero(self):
        self.assertEqual(qg.bottleneck_to_max_workers(0), 2)

    def test_edge_hundred(self):
        self.assertEqual(qg.bottleneck_to_max_workers(100), 0)

    def test_monotonic_decreasing(self):
        """Higher bottleneck → same or fewer workers (never more)."""
        prev = 999
        for b in range(0, 101):
            w = qg.bottleneck_to_max_workers(b)
            self.assertLessEqual(w, prev,
                f"bottleneck {b}: workers {w} > previous {prev}")
            prev = w


if __name__ == "__main__":
    unittest.main()