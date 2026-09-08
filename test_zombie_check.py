#!/usr/bin/env python3
"""test_zombie_check.py — OBJ-21: deterministic G3 zombie guard in quota-gate.py.

Covers compute_zombie_check() and its main() wiring (OBJ-21, t_e793b2b9):
guardrail G3 of the autonomous-task-creator prompt ("any running task
older than 45 minutes → [SILENT]") enforced deterministically by the
gate, instead of depending on the LLM reading the board correctly.

Key invariants under test:
  1. Fresh running task (< 45 min) → no zombie, wakeAgent unaffected.
  2. Running task started > 45 min ago → zombie via started_at.
  3. Age base prefers last_heartbeat_at: task started 2h ago but with a
     recent heartbeat is NOT a zombie; with a stale heartbeat it is.
  4. A DONE task with an old started_at is NOT counted (the false
     "87 min zombie" behind OBJ-21 measured a completed task's age).
  5. Running task with BOTH timestamps NULL → zombie (undated row).
  6. Missing kanban.db / DB error → fail open (no zombie) + warning.
  7. main() e2e: zombie present → wakeAgent False + zombie_guard warning;
     no zombie → wakeAgent True and behaviour unchanged.
  8. Output context always carries zombie_check (additive key).

Run:
  /usr/bin/python3.12 test_zombie_check.py
  /usr/bin/python3.12 -m pytest test_zombie_check.py -v
"""
from __future__ import annotations

import io
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Ensure the script dir is on the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

# Import the module (quota-gate.py has a hyphen, so import via importlib)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "quota_gate_zombie_check",
    os.path.join(SCRIPTS_DIR, "quota-gate.py"),
)
quota_gate = importlib.util.module_from_spec(_spec)
sys.modules["quota_gate_zombie_check"] = quota_gate
_spec.loader.exec_module(quota_gate)

from quota_gate_zombie_check import (
    compute_zombie_check,
    ZOMBIE_RUNNING_MINUTES,
    select_provider,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given tasks.

    Each task is a dict: {id, title, status, assignee, started_at,
    last_heartbeat_at, completed_at}.
    Mirrors the real tasks table schema (subset relevant to the guard).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # remove so sqlite creates fresh

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            created_at INTEGER,
            started_at INTEGER,
            last_heartbeat_at INTEGER,
            completed_at INTEGER
        )
    """)
    for task in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at, "
            "started_at, last_heartbeat_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.get("id", "t_test"),
                task.get("title", "Test task"),
                task.get("body", ""),
                task.get("status", "ready"),
                task.get("assignee", "pr-nanogpt"),
                0,
                task.get("started_at"),
                task.get("last_heartbeat_at"),
                task.get("completed_at"),
            ),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests: threshold constant ────────────────────────────────────────────────

class TestZombieThreshold(unittest.TestCase):

    def test_threshold_is_45_minutes(self):
        """G3 threshold (raised from 10 min on Sep 7 2026 — pitfall 17b)."""
        self.assertEqual(ZOMBIE_RUNNING_MINUTES, 45.0)

    def test_result_shape_without_zombie(self):
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_fresh", "status": "running",
             "started_at": now - 10 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertFalse(result["has_zombie"])
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["threshold_minutes"], 45.0)
            self.assertEqual(result["tasks"], [])
        finally:
            os.unlink(db)


# ── Tests: zombie detection ──────────────────────────────────────────────────

class TestZombieDetection(unittest.TestCase):

    def test_started_46_minutes_is_zombie(self):
        """Running task started > 45 min ago → zombie via started_at."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_zombie", "title": "Stuck worker", "status": "running",
             "started_at": now - 46 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertTrue(result["has_zombie"])
            self.assertEqual(result["count"], 1)
            t = result["tasks"][0]
            self.assertEqual(t["id"], "t_zombie")
            self.assertEqual(t["age_source"], "started_at")
            self.assertGreater(t["age_minutes"], 45.0)
        finally:
            os.unlink(db)

    def test_boundary_45_minutes_exactly_is_not_zombie(self):
        """Exactly at the threshold is NOT a zombie (strictly > rule)."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_edge", "status": "running",
             "started_at": now - 45 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertFalse(result["has_zombie"])
        finally:
            os.unlink(db)

    def test_one_second_over_threshold_is_zombie(self):
        """45 min + 1 second IS a zombie (strictly > rule, float-safe)."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_edge", "status": "running",
             "started_at": now - 45 * 60 - 1},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertTrue(result["has_zombie"])
        finally:
            os.unlink(db)

    def test_recent_heartbeat_overrides_old_start(self):
        """started 2h ago but heartbeat 10 min ago → ALIVE, not zombie."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_alive", "status": "running",
             "started_at": now - 120 * 60,
             "last_heartbeat_at": now - 10 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertFalse(result["has_zombie"])
        finally:
            os.unlink(db)

    def test_stale_heartbeat_is_zombie_via_heartbeat(self):
        """Heartbeat 50 min ago → zombie, source heartbeat (liveness wins)."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_dead_hb", "status": "running",
             "started_at": now - 60 * 60,
             "last_heartbeat_at": now - 50 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertTrue(result["has_zombie"])
            self.assertEqual(result["tasks"][0]["age_source"], "heartbeat")
        finally:
            os.unlink(db)

    def test_done_task_old_start_not_counted(self):
        """THE OBJ-21 regression: a COMPLETED task with an old started_at
        is NOT a zombie — only status='running' rows are examined."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_done_old", "title": "Finished long ago",
             "status": "done", "started_at": now - 87 * 60,
             "completed_at": now - 60 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertFalse(result["has_zombie"])
            self.assertEqual(result["count"], 0)
        finally:
            os.unlink(db)

    def test_null_both_timestamps_is_zombie(self):
        """Running row with no heartbeat and no started_at → zombie."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_undated", "status": "running"},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertTrue(result["has_zombie"])
            self.assertEqual(result["tasks"][0]["age_source"], "unknown")
        finally:
            os.unlink(db)

    def test_mixed_board_counts_only_zombies(self):
        """Fresh running + old running + done old → exactly 1 zombie."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_fresh", "status": "running", "started_at": now - 5 * 60},
            {"id": "t_old", "status": "running", "started_at": now - 90 * 60},
            {"id": "t_done", "status": "done", "started_at": now - 500 * 60},
        ])
        try:
            result = compute_zombie_check(db, now=now)
            self.assertTrue(result["has_zombie"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["tasks"][0]["id"], "t_old")
        finally:
            os.unlink(db)

    def test_sorted_by_age_desc_capped_at_five(self):
        """6 zombies → tasks list carries the 5 OLDEST, count is 6."""
        now = time.time()
        tasks = [
            {"id": f"t_old_{i}", "status": "running",
             "started_at": now - (50 + i) * 60}
            for i in range(6)
        ]
        db = _make_kanban_db(tasks)
        try:
            result = compute_zombie_check(db, now=now)
            self.assertEqual(result["count"], 6)
            self.assertEqual(len(result["tasks"]), 5)
            ages = [t["age_minutes"] for t in result["tasks"]]
            self.assertEqual(ages, sorted(ages, reverse=True))
            self.assertEqual(result["tasks"][0]["id"], "t_old_5")
        finally:
            os.unlink(db)


# ── Tests: fail-open behaviour ───────────────────────────────────────────────

class TestZombieFailOpen(unittest.TestCase):

    def test_missing_db_returns_no_zombie(self):
        warnings = []
        result = compute_zombie_check("/nonexistent/kanban.db",
                                      warnings=warnings)
        self.assertFalse(result["has_zombie"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(warnings, [])

    def test_corrupt_db_fail_open_with_warning(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.write(fd, b"this is not a sqlite database")
        os.close(fd)
        try:
            warnings = []
            result = compute_zombie_check(path, warnings=warnings)
            self.assertFalse(result["has_zombie"])
            self.assertTrue(any("zombie_check" in w for w in warnings))
        finally:
            os.unlink(path)

    def test_env_var_path_resolution(self):
        """compute_zombie_check() with no args uses HERMES_KANBAN_DB env."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_env_zombie", "status": "running",
             "started_at": now - 60 * 60},
        ])
        old = os.environ.get("HERMES_KANBAN_DB")
        os.environ["HERMES_KANBAN_DB"] = db
        try:
            result = compute_zombie_check(now=now)
            self.assertTrue(result["has_zombie"])
        finally:
            if old is None:
                os.environ.pop("HERMES_KANBAN_DB", None)
            else:
                os.environ["HERMES_KANBAN_DB"] = old
            os.unlink(db)

    def test_warnings_arg_optional(self):
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_ok", "status": "running", "started_at": now - 5 * 60},
        ])
        try:
            result = compute_zombie_check(db)  # no warnings arg
            self.assertFalse(result["has_zombie"])
        finally:
            os.unlink(db)


# ── Tests: main() wiring (e2e with mocked providers) ─────────────────────────

FROZEN_OLLAMA = {"session_pct": 40.0, "weekly_pct": 20.0, "activity_cost": 0.0}
FAKE_ENV = {
    "NANO_GPT_API_KEY": "k", "OPENROUTER_API_KEY": "k",
    "OPENCODE_GO_API_KEY": "k", "OLLAMA_API_KEY": "k",
}
EXPECTED_PROFILES = {"pr-ollama", "pr-nanogpt", "pr-opencode"}


def _run_gate_main(kanban_db, scratch):
    """Run the gate's real main() with live API calls mocked out.

    Returns the parsed JSON output dict.
    """
    frozen_ollama = dict(FROZEN_OLLAMA)
    env = {
        "HERMES_KANBAN_DB": kanban_db,
        "HERMES_HOME": scratch,
    }
    os.environ.pop("QUOTA_GATE_PRIVACY", None)

    buf = io.StringIO()
    ledger_frozen = lambda rolling_resets_at=None: (None, [])
    with patch.dict(os.environ, env, clear=False), \
         patch.object(quota_gate, "query_ollama", lambda: frozen_ollama), \
         patch.object(quota_gate, "get_existing_profiles",
                      lambda: set(EXPECTED_PROFILES)), \
         patch.object(quota_gate, "get_env",
                      lambda key: FAKE_ENV.get(key)), \
         patch.object(quota_gate, "model_cost_context", ledger_frozen,
                      create=True), \
         contextlib.redirect_stdout(buf):
        quota_gate.main()
    return json.loads(buf.getvalue().strip().splitlines()[-1])


class TestGateMainZombieWiring(unittest.TestCase):
    """The zombie guard must FORCE wakeAgent:false in main(), and the
    context must always carry zombie_check."""

    def _setup_gate_env(self, db):
        return patch.dict(os.environ, {
            "HERMES_KANBAN_DB": db,
            "HERMES_HOME": tempfile.gettempdir(),
        }, clear=False)

    def test_zombie_forces_wakeagent_false(self):
        """Board with a 90-min running task → wakeAgent False even though
        Ollama has plenty of quota; warning names the zombie_guard."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_stuck", "title": "Stuck", "status": "running",
             "assignee": "pr-ollama", "started_at": now - 90 * 60},
        ])
        try:
            with self._setup_gate_env(db), \
                 patch.object(quota_gate, "query_ollama",
                              lambda: dict(FROZEN_OLLAMA)), \
                 patch.object(quota_gate, "get_existing_profiles",
                              lambda: set(EXPECTED_PROFILES)), \
                 patch.object(quota_gate, "get_env",
                              lambda key: FAKE_ENV.get(key)), \
                 patch.object(quota_gate, "model_cost_context",
                              lambda rolling_resets_at=None: (None, []),
                              create=True), \
                 contextlib.redirect_stdout(io.StringIO()) as buf:
                quota_gate.main()
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertFalse(out["wakeAgent"])
            ctx = out["context"]
            self.assertTrue(ctx["zombie_check"]["has_zombie"])
            self.assertEqual(ctx["zombie_check"]["count"], 1)
            self.assertIn("zombie_guard", ctx["warning"])
            self.assertIn("t_stuck", ctx["warning"])
            # Recommendation stays visible for auditability…
            self.assertEqual(ctx["recommended_profile"], "pr-ollama")
        finally:
            os.unlink(db)

    def test_no_zombie_wakeagent_true_and_behaviour_unchanged(self):
        """Acceptance criterion 3: without zombies the gate behaves
        exactly as before — wakeAgent true, normal recommendation."""
        now = time.time()
        db = _make_kanban_db([
            {"id": "t_fresh", "status": "running",
             "assignee": "pr-ollama", "started_at": now - 10 * 60},
        ])
        try:
            with self._setup_gate_env(db), \
                 patch.object(quota_gate, "query_ollama",
                              lambda: dict(FROZEN_OLLAMA)), \
                 patch.object(quota_gate, "get_existing_profiles",
                              lambda: set(EXPECTED_PROFILES)), \
                 patch.object(quota_gate, "get_env",
                              lambda key: FAKE_ENV.get(key)), \
                 patch.object(quota_gate, "model_cost_context",
                              lambda rolling_resets_at=None: (None, []),
                              create=True), \
                 contextlib.redirect_stdout(io.StringIO()) as buf:
                quota_gate.main()
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertTrue(out["wakeAgent"])
            ctx = out["context"]
            self.assertEqual(ctx["recommended_profile"], "pr-ollama")
            # Additive key present, zeroed.
            self.assertFalse(ctx["zombie_check"]["has_zombie"])
            self.assertEqual(ctx["zombie_check"]["count"], 0)
            # No zombie_guard noise in the warning.
            self.assertNotIn("zombie_guard", ctx.get("warning") or "")
        finally:
            os.unlink(db)

    def test_no_db_wakeagent_true_fail_open(self):
        """Missing kanban.db → guard fails open, gate still wakes."""
        with tempfile.TemporaryDirectory() as scratch:
            env = {"HERMES_KANBAN_DB": os.path.join(scratch, "missing.db"),
                   "HERMES_HOME": scratch}
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(quota_gate, "query_ollama",
                              lambda: dict(FROZEN_OLLAMA)), \
                 patch.object(quota_gate, "get_existing_profiles",
                              lambda: set(EXPECTED_PROFILES)), \
                 patch.object(quota_gate, "get_env",
                              lambda key: FAKE_ENV.get(key)), \
                 patch.object(quota_gate, "model_cost_context",
                              lambda rolling_resets_at=None: (None, []),
                              create=True), \
                 contextlib.redirect_stdout(io.StringIO()) as buf:
                quota_gate.main()
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertTrue(out["wakeAgent"])
            self.assertFalse(out["context"]["zombie_check"]["has_zombie"])


# ── Tests: routing invariants (guard must not touch provider selection) ──────

class TestRoutingUnchangedByZombieGuard(unittest.TestCase):
    """compute_zombie_check is a pure read-only census + decision flag;
    select_provider knows nothing about it.  These tests pin that."""

    MOCK_PROVIDERS = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 80.0, "bottleneck_pct": 20.0,
         "bottleneck_window": "session", "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt",
         "model": "zai-org/glm-5.2", "availability": 60.0,
         "bottleneck_pct": 40.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]

    def test_select_provider_signature_untouched(self):
        """select_provider takes no zombie argument — routing identical
        whether or not a zombie exists (the guard acts on the OUTPUT,
        after selection)."""
        result = select_provider(self.MOCK_PROVIDERS, privacy_level=None)
        # OBJ-26: preference-first everywhere → pr-nanogpt (pref 0) wins
        # for public/no-privacy; the zombie guard still acts only on output.
        self.assertEqual(result["profile"], "pr-nanogpt")


if __name__ == "__main__":
    unittest.main(verbosity=2)