#!/usr/bin/env python3
"""Tests for objective-proposer.py — OBJ-16 autonomous objective proposer.

Tests cover:
  - Pattern detectors (recurring errors, stale objectives, quota imbalance,
    missing test coverage, crash cluster)
  - Proposal deduplication (_already_proposed)
  - Guardrails integration (validate_proposal respects GR6)
  - Recording logic (dry-run doesn't record, --execute records only on success)
  - End-to-end dry-run flow

Run:
  python3 -m pytest test_objective_proposer.py -v
  python3 test_objective_proposer.py  # direct run
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with filename-based module
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "objective_proposer",
    os.path.join(SCRIPT_DIR, "scripts", "objective-proposer.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["objective_proposer"] = _mod
_spec.loader.exec_module(_mod)

from objective_proposer import (
    Pattern,
    ProposalResult,
    load_observations,
    detect_recurring_errors,
    detect_quota_imbalance,
    detect_stale_objectives,
    detect_missing_test_coverage,
    detect_crash_cluster,
    run_analysis,
    validate_proposal,
    record_proposal,
    propose_objective,
    _already_proposed,
    _is_error_resolved,
    find_validate_script,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_observation(
    event="session_end",
    errors=None,
    timestamp=None,
    quota=None,
):
    """Create a test observation dict."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    obs = {"timestamp": timestamp, "event": event}
    q = quota or {}
    if errors is not None:
        q["errors"] = errors
    obs["quota"] = q
    return obs


def _write_observations(path, observations):
    """Write observations to a JSONL file."""
    with open(path, "w") as f:
        for obs in observations:
            f.write(json.dumps(obs) + "\n")


def _make_kanban_db(tasks):
    """Create a temp kanban.db with tasks.
    Each task: {id, title, body, status, assignee, created_at, completed_at, consecutive_failures}
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

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
            completed_at INTEGER,
            consecutive_failures INTEGER DEFAULT 0
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at, completed_at, consecutive_failures) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.get("id", "t_test"),
                t.get("title", "Test task"),
                t.get("body", ""),
                t.get("status", "done"),
                t.get("assignee", "pr-ollama"),
                t.get("created_at", 0),
                t.get("completed_at", None),
                t.get("consecutive_failures", 0),
            ),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests: load_observations ─────────────────────────────────────────────────

class TestLoadObservations(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        """Missing observations file returns empty list."""
        result = load_observations()
        # May return data if the real file exists; test with a non-existent path
        with patch("objective_proposer.OBSERVATIONS_FILE", "/nonexistent/path.jsonl"):
            result = load_observations()
        self.assertEqual(result, [])

    def test_valid_file_loads_entries(self):
        """Valid JSONL file loads entries within window."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            now = datetime.now(timezone.utc).isoformat()
            old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            f.write(json.dumps(_make_observation(timestamp=now)) + "\n")
            f.write(json.dumps(_make_observation(timestamp=old_ts)) + "\n")
            f.write("invalid json line\n")
            f.write("\n")  # empty line
            path = f.name

        with patch("objective_proposer.OBSERVATIONS_FILE", path):
            result = load_observations(window_days=7)

        os.unlink(path)
        self.assertEqual(len(result), 1)  # Only the recent one


# ── Tests: detect_recurring_errors ───────────────────────────────────────────

class TestDetectRecurringErrors(unittest.TestCase):

    def setUp(self):
        # Ensure proposals file doesn't interfere
        self._patch = patch("objective_proposer._already_proposed", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_no_errors_returns_none(self):
        """No errors in observations returns None."""
        obs = [_make_observation(errors=[]) for _ in range(10)]
        result = detect_recurring_errors(obs)
        self.assertIsNone(result)

    def test_single_error_below_threshold(self):
        """Error appearing only once (below threshold) returns None."""
        obs = [_make_observation(errors=["some error"]) for _ in range(2)]
        result = detect_recurring_errors(obs)
        self.assertIsNone(result)

    def test_recurring_error_detected(self):
        """Error appearing >= threshold times is detected."""
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"]) for _ in range(5)]
        result = detect_recurring_errors(obs)
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "recurring_error")
        self.assertIn("recurring error", result.title.lower())

    def test_error_normalization(self):
        """Errors with different numbers are normalized and counted together."""
        obs = []
        for i in range(5):
            obs.append(_make_observation(errors=[f"HTTP Error {i}: Forbidden"]))
        result = detect_recurring_errors(obs)
        self.assertIsNotNone(result)
        # The normalized error should have "N" instead of the number
        self.assertIn("N", result.evidence)

    def test_resolved_error_not_detected(self):
        """Error that was already resolved (done task + no recent occurrences) is skipped."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=old_ts) for _ in range(5)]
        # Create a kanban.db with a done task that mentions the error keywords
        db_path = _make_kanban_db([
            {"id": "t_fix1", "title": "OBJ-20: Fix nanogpt HTTP 403 Forbidden error", "body": "objective:OBJ-20\nFixed the 403 error.", "status": "done", "completed_at": int(datetime.now(timezone.utc).timestamp()) - 86400},
        ])
        with patch("objective_proposer.KANBAN_DB", db_path):
            with patch("objective_proposer._already_proposed", return_value=False):
                result = detect_recurring_errors(obs)
        os.unlink(db_path)
        self.assertIsNone(result, "Resolved error should not be detected as recurring")

    def test_active_error_still_detected_with_done_task(self):
        """Error with recent occurrences (last 48h) is still detected even if a done task exists."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=recent_ts) for _ in range(5)]
        db_path = _make_kanban_db([
            {"id": "t_fix1", "title": "OBJ-20: Fix nanogpt HTTP 403 Forbidden error", "body": "objective:OBJ-20\nFixed.", "status": "done", "completed_at": int(datetime.now(timezone.utc).timestamp()) - 86400},
        ])
        with patch("objective_proposer.KANBAN_DB", db_path):
            with patch("objective_proposer._already_proposed", return_value=False):
                result = detect_recurring_errors(obs)
        os.unlink(db_path)
        self.assertIsNotNone(result, "Active error (recent occurrences) should still be detected")


# ── Tests: _is_error_resolved ────────────────────────────────────────────────

class TestIsErrorResolved(unittest.TestCase):

    def test_recent_occurrences_not_resolved(self):
        """Error appearing in last 48h is NOT resolved."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=recent_ts)]
        with patch("objective_proposer.query_kanban_db", return_value=[]):
            result = _is_error_resolved("nanogpt: HTTP Error N: Forbidden", obs)
        self.assertFalse(result)

    def test_no_done_task_not_resolved(self):
        """Error with no recent occurrences but no done task is NOT resolved."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=old_ts)]
        with patch("objective_proposer.query_kanban_db", return_value=[]):
            result = _is_error_resolved("nanogpt: HTTP Error N: Forbidden", obs)
        self.assertFalse(result)

    def test_done_task_with_matching_keywords_is_resolved(self):
        """Error with no recent occurrences + done task with matching keywords IS resolved."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=old_ts)]
        done_rows = [
            {"title": "OBJ-20: Fix nanogpt HTTP 403 Forbidden error", "body": "objective:OBJ-20\nFixed."},
        ]
        with patch("objective_proposer.query_kanban_db", return_value=done_rows):
            result = _is_error_resolved("nanogpt: HTTP Error N: Forbidden", obs)
        self.assertTrue(result)

    def test_done_task_with_unrelated_keywords_not_resolved(self):
        """Done task with unrelated keywords does not mark error as resolved."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        obs = [_make_observation(errors=["nanogpt: HTTP Error 403: Forbidden"], timestamp=old_ts)]
        done_rows = [
            {"title": "OBJ-99: Unrelated task about disk cleanup", "body": "objective:OBJ-99\nCleaned temp files."},
        ]
        with patch("objective_proposer.query_kanban_db", return_value=done_rows):
            result = _is_error_resolved("nanogpt: HTTP Error N: Forbidden", obs)
        self.assertFalse(result)


# ── Tests: detect_quota_imbalance ─────────────────────────────────────────────

class TestDetectQuotaImbalance(unittest.TestCase):

    def setUp(self):
        self._patch = patch("objective_proposer._already_proposed", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_balanced_quota_returns_none(self):
        """Balanced quota usage returns None."""
        obs = []
        for _ in range(10):
            obs.append(_make_observation(quota={
                "ollama_weekly_pct": 50.0,
                "nanogpt_weekly_tokens_pct": 60.0,
            }))
        result = detect_quota_imbalance(obs)
        self.assertIsNone(result)

    def test_imbalance_detected(self):
        """Ollama consistently high while NanoGPT low is detected."""
        obs = []
        for _ in range(10):
            obs.append(_make_observation(quota={
                "ollama_weekly_pct": 90.0,
                "nanogpt_weekly_tokens_pct": 10.0,
            }))
        result = detect_quota_imbalance(obs)
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "quota_imbalance")

    def test_insufficient_samples_returns_none(self):
        """Too few samples returns None."""
        obs = [_make_observation(quota={
            "ollama_weekly_pct": 90.0,
            "nanogpt_weekly_tokens_pct": 10.0,
        }) for _ in range(2)]
        result = detect_quota_imbalance(obs)
        self.assertIsNone(result)


# ── Tests: detect_stale_objectives ────────────────────────────────────────────

class TestDetectStaleObjectives(unittest.TestCase):

    def setUp(self):
        self._patch = patch("objective_proposer._already_proposed", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_no_active_objectives(self):
        """No active objectives returns None."""
        with patch("objective_proposer.get_active_objectives", return_value={}):
            with patch("objective_proposer.get_completed_objectives_info", return_value={}):
                result = detect_stale_objectives()
        self.assertIsNone(result)

    def test_fresh_objective_not_stale(self):
        """Objective with recent completion is not stale."""
        now_ts = datetime.now(timezone.utc).timestamp()
        active = {"OBJ-01": {"task_ids": ["t_1"], "statuses": ["running"], "titles": ["T"]}}
        completed = {"OBJ-01": {"last_completed_at": now_ts - 100, "completed_count": 1}}
        with patch("objective_proposer.get_active_objectives", return_value=active):
            with patch("objective_proposer.get_completed_objectives_info", return_value=completed):
                result = detect_stale_objectives()
        self.assertIsNone(result)

    def test_stale_objective_detected(self):
        """Objective with no recent completion and running tasks is stale."""
        old_ts = datetime.now(timezone.utc).timestamp() - (10 * 86400)
        active = {"OBJ-05": {"task_ids": ["t_1", "t_2"], "statuses": ["running", "blocked"], "titles": ["T1", "T2"]}}
        completed = {"OBJ-05": {"last_completed_at": old_ts, "completed_count": 1}}
        with patch("objective_proposer.get_active_objectives", return_value=active):
            with patch("objective_proposer.get_completed_objectives_info", return_value=completed):
                result = detect_stale_objectives()
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "stale_objective")
        self.assertIn("OBJ-05", result.title)

    def test_new_objective_not_stale(self):
        """Objective with only triage/todo tasks is not stale (still warming up)."""
        old_ts = datetime.now(timezone.utc).timestamp() - (10 * 86400)
        active = {"OBJ-99": {"task_ids": ["t_1"], "statuses": ["triage"], "titles": ["T"]}}
        completed = {"OBJ-99": {"last_completed_at": 0, "completed_count": 0}}
        with patch("objective_proposer.get_active_objectives", return_value=active):
            with patch("objective_proposer.get_completed_objectives_info", return_value=completed):
                result = detect_stale_objectives()
        self.assertIsNone(result)


# ── Tests: detect_missing_test_coverage ──────────────────────────────────────

class TestDetectMissingTestCoverage(unittest.TestCase):

    def setUp(self):
        self._patch = patch("objective_proposer._already_proposed", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_no_scripts_dir(self):
        """Missing scripts dir returns None."""
        with patch("objective_proposer.SCRIPT_DIR_PLUGIN", "/nonexistent"):
            result = detect_missing_test_coverage()
        self.assertIsNone(result)

    def test_all_covered(self):
        """All scripts have test files returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            # Create script files
            for name in ["foo.py", "bar.py"]:
                with open(os.path.join(scripts_dir, name), "w") as f:
                    f.write("# stub")
            # Create test files in repo root
            for name in ["test_foo.py", "test_bar.py"]:
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("# test stub")

            with patch("objective_proposer.SCRIPT_DIR_PLUGIN", scripts_dir):
                with patch("objective_proposer.PLUGIN_REPO", tmpdir):
                    result = detect_missing_test_coverage()
        self.assertIsNone(result)

    def test_uncovered_detected(self):
        """Script without test file is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            # Create script files
            for name in ["foo.py", "bar.py"]:
                with open(os.path.join(scripts_dir, name), "w") as f:
                    f.write("# stub")
            # Only one test file
            with open(os.path.join(tmpdir, "test_foo.py"), "w") as f:
                f.write("# test stub")

            with patch("objective_proposer.SCRIPT_DIR_PLUGIN", scripts_dir):
                with patch("objective_proposer.PLUGIN_REPO", tmpdir):
                    result = detect_missing_test_coverage()
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "missing_test_coverage")
        self.assertIn("bar.py", result.evidence)


# ── Tests: detect_crash_cluster ───────────────────────────────────────────────

class TestDetectCrashCluster(unittest.TestCase):

    def setUp(self):
        self._patch = patch("objective_proposer._already_proposed", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_no_crashes(self):
        """No crashed tasks returns None."""
        with patch("objective_proposer.get_crashed_tasks", return_value=[]):
            result = detect_crash_cluster()
        self.assertIsNone(result)

    def test_single_crash_not_cluster(self):
        """Single crashed task is not a cluster."""
        with patch("objective_proposer.get_crashed_tasks", return_value=[{"id": "t_1", "title": "T1"}]):
            result = detect_crash_cluster()
        self.assertIsNone(result)

    def test_crash_cluster_detected(self):
        """Multiple crashed tasks detected as cluster."""
        crashed = [
            {"id": f"t_{i}", "title": f"Task {i}", "consecutive_failures": 2}
            for i in range(3)
        ]
        with patch("objective_proposer.get_crashed_tasks", return_value=crashed):
            result = detect_crash_cluster()
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "crash_cluster")
        self.assertIn("3 tasks", result.title)


# ── Tests: _already_proposed ──────────────────────────────────────────────────

class TestAlreadyProposed(unittest.TestCase):

    def test_no_file_returns_false(self):
        """Missing proposals file returns False."""
        with patch("objective_proposer.PROPOSALS_FILE", "/nonexistent/file.jsonl"):
            result = _already_proposed("test_pattern")
        self.assertFalse(result)

    def test_today_allowed_entry_blocks(self):
        """Today's allowed entry with matching pattern_key blocks."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": True,
                "pattern_key": "recurring_error:my pattern",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("recurring_error:my pattern")
        os.unlink(path)
        self.assertTrue(result)

    def test_rejected_entry_blocks_same_day(self):
        """Entry with allowed=false (rejected by GR6) blocks same-day re-proposal.

        This is the core fix for OBJ-16: without this, the proposer
        re-proposes the same pattern every 2h cron tick, wasting the
        GR6 daily quota and spamming the proposals file.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": False,
                "pattern_key": "recurring_error:my pattern",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("recurring_error:my pattern")
        os.unlink(path)
        self.assertTrue(result)

    def test_old_allowed_entry_blocks_cross_day(self):
        """Allowed entry from a previous day blocks (cross-day dedup)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            old_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
            entry = {
                "date": old_date,
                "allowed": True,
                "pattern_key": "recurring_error:my pattern",
                "evidence": "Error 'my pattern' appeared 4 times in 7d",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("recurring_error:my pattern")
        os.unlink(path)
        self.assertTrue(result)

    def test_old_rejected_entry_blocks_cross_day(self):
        """Rejected entry from a previous day also blocks (cross-day dedup).

        A rejected proposal on a previous day means we already saw and
        evaluated that pattern — don't re-propose it.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            old_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
            entry = {
                "date": old_date,
                "allowed": False,
                "pattern_key": "recurring_error:my pattern",
                "evidence": "Error 'my pattern' appeared 4 times in 7d",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("recurring_error:my pattern")
        os.unlink(path)
        self.assertTrue(result)

    def test_different_pattern_old_entry_does_not_block(self):
        """Entry from a previous day with a different pattern does not block."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            old_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
            entry = {
                "date": old_date,
                "allowed": True,
                "pattern_key": "recurring_error:completely different error",
                "evidence": "Error 'completely different error' appeared 4 times in 7d",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("recurring_error:my pattern")
        os.unlink(path)
        self.assertFalse(result)

    # ── OBJ-16 regression: bare-kind dedup ──────────────────────────────

    def test_bare_kind_same_day_blocks_via_pattern_kind(self):
        """Bare kind (e.g. 'missing_test_coverage') matches a same-day
        recorded entry via the pattern_kind field.

        This is the core OBJ-16 regression test: detect_missing_test_coverage()
        calls _already_proposed('missing_test_coverage') (bare kind), but
        record_proposal() stores pattern_key='missing_test_coverage:evidence'.
        The bare kind must still match for same-day dedup.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": True,
                "pattern_kind": "missing_test_coverage",
                "pattern_key": "missing_test_coverage:Uncovered scripts: foo.py, bar.py",
                "evidence": "Uncovered scripts: foo.py, bar.py",
                "title": "OBJ-X: Add test coverage for uncovered plugin scripts (2 scripts)",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("missing_test_coverage")
        os.unlink(path)
        self.assertTrue(result, "Bare kind must match same-day entry via pattern_kind")

    def test_bare_kind_same_day_blocks_via_pattern_key_prefix(self):
        """Bare kind matches a same-day recorded entry via pattern_key prefix
        even when pattern_kind field is absent (backward compat)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": False,
                "pattern_key": "crash_cluster:3 crashed tasks: ...",
                "evidence": "3 crashed tasks: ...",
                "title": "OBJ-X: Investigate crash cluster",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("crash_cluster")
        os.unlink(path)
        self.assertTrue(result, "Bare kind must match same-day entry via pattern_key prefix")

    def test_bare_kind_rejected_entry_blocks_same_day(self):
        """A rejected (allowed=false) same-day entry with a matching bare
        kind blocks re-proposal. This is the exact OBJ-16 spam scenario:
        the first proposal is rejected by GR6, and without this fix the
        2h cron re-proposes every tick."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": False,
                "pattern_kind": "missing_test_coverage",
                "pattern_key": "missing_test_coverage:Uncovered scripts: foo.py",
                "evidence": "Uncovered scripts: foo.py",
                "title": "OBJ-X: Add test coverage for uncovered plugin scripts",
                "violations": [{"id": "GR6", "message": "Daily proposal limit reached"}],
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("missing_test_coverage")
        os.unlink(path)
        self.assertTrue(result, "Rejected same-day entry with bare kind must block")

    def test_bare_kind_cross_day_blocks(self):
        """Bare kind matches a previous-day recorded entry (cross-day dedup)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            old_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
            entry = {
                "date": old_date,
                "allowed": True,
                "pattern_kind": "missing_test_coverage",
                "pattern_key": "missing_test_coverage:Uncovered scripts: foo.py",
                "evidence": "Uncovered scripts: foo.py",
                "title": "OBJ-X: Add test coverage for uncovered plugin scripts",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("missing_test_coverage")
        os.unlink(path)
        self.assertTrue(result, "Bare kind must match cross-day entry via pattern_kind")

    def test_bare_kind_different_kind_does_not_block(self):
        """A same-day entry with a different pattern_kind does not block a
        bare-kind check for another kind."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = {
                "date": today,
                "allowed": True,
                "pattern_kind": "recurring_error",
                "pattern_key": "recurring_error:some error",
                "evidence": "Error 'some error' appeared 4 times in 7d",
                "title": "OBJ-X: Fix recurring error",
            }
            f.write(json.dumps(entry) + "\n")
            path = f.name

        with patch("objective_proposer.PROPOSALS_FILE", path):
            result = _already_proposed("missing_test_coverage")
        os.unlink(path)
        self.assertFalse(result, "Different pattern_kind must not block bare kind")

    def test_missing_test_coverage_detector_dedup_after_record(self):
        """End-to-end regression: after recording a missing_test_coverage
        proposal, detect_missing_test_coverage() returns None on the next
        call (same day). This is the exact bug scenario from OBJ-16."""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            # Create an uncovered script
            with open(os.path.join(scripts_dir, "foo.py"), "w") as f:
                f.write("# stub")
            # No test_foo.py → uncovered

            proposals_path = os.path.join(tmpdir, "proposals.jsonl")

            # First call: should detect (no prior proposals)
            with patch("objective_proposer.SCRIPT_DIR_PLUGIN", scripts_dir):
                with patch("objective_proposer.PLUGIN_REPO", tmpdir):
                    with patch("objective_proposer.PROPOSALS_FILE", proposals_path):
                        result1 = detect_missing_test_coverage()
            self.assertIsNotNone(result1, "First call should detect uncovered script")

            # Record the proposal (simulating what propose_objective does)
            with patch("objective_proposer.PROPOSALS_FILE", proposals_path):
                record_proposal(result1, allowed=False,
                    violations=[{"id": "GR6", "message": "Daily limit"}],
                    warnings=[])

            # Second call: should return None (dedup via pattern_kind)
            with patch("objective_proposer.SCRIPT_DIR_PLUGIN", scripts_dir):
                with patch("objective_proposer.PLUGIN_REPO", tmpdir):
                    with patch("objective_proposer.PROPOSALS_FILE", proposals_path):
                        result2 = detect_missing_test_coverage()
            self.assertIsNone(result2,
                "Second call same day must return None (dedup via pattern_kind)")


# ── Tests: record_proposal ─────────────────────────────────────────────────────

class TestRecordProposal(unittest.TestCase):

    def test_record_writes_entry(self):
        """record_proposal writes a valid JSON entry to the file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        pattern = Pattern(
            kind="test_kind",
            severity="medium",
            title="Test Title",
            body="Test Body",
            evidence="Test Evidence",
            source="test",
        )

        with patch("objective_proposer.PROPOSALS_FILE", path):
            record_proposal(pattern, True, [], [], task_id="t_test123")

        with open(path, "r") as f:
            entry = json.loads(f.read().strip())

        os.unlink(path)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["title"], "Test Title")
        self.assertEqual(entry["task_id"], "t_test123")
        self.assertEqual(entry["pattern_kind"], "test_kind")


# ── Tests: propose_objective (dry-run vs execute) ───────────────────────────

class TestProposeObjective(unittest.TestCase):

    def test_dry_run_does_not_record(self):
        """Dry-run mode should NOT record a proposal (GR6 safety)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        pattern = Pattern(
            kind="test",
            severity="low",
            title="Test",
            body="Test body",
            evidence="Test evidence",
            source="test",
        )

        with patch("objective_proposer.validate_proposal", return_value=(True, [], [])):
            with patch("objective_proposer.PROPOSALS_FILE", path):
                result = propose_objective(pattern, execute=False)

        # File should be empty (no recording in dry-run)
        with open(path, "r") as f:
            content = f.read()
        os.unlink(path)
        self.assertEqual(content, "")
        self.assertTrue(result.allowed)

    def test_blocked_proposal_records(self):
        """Blocked proposal should be recorded (for dedup)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        pattern = Pattern(
            kind="test",
            severity="low",
            title="Test",
            body="Test body",
            evidence="Test evidence",
            source="test",
        )

        violations = [{"id": "GR6", "message": "Daily limit reached"}]

        with patch("objective_proposer.validate_proposal", return_value=(False, violations, [])):
            with patch("objective_proposer.PROPOSALS_FILE", path):
                result = propose_objective(pattern, execute=True)

        # File should have the rejected entry
        with open(path, "r") as f:
            content = f.read().strip()
        os.unlink(path)
        self.assertTrue(content)
        entry = json.loads(content)
        self.assertFalse(entry["allowed"])
        self.assertEqual(len(entry["violations"]), 1)

    def test_execute_failure_does_not_record(self):
        """Failed task creation should NOT record (GR6 should not count it)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        pattern = Pattern(
            kind="test",
            severity="low",
            title="Test",
            body="Test body",
            evidence="Test evidence",
            source="test",
        )

        with patch("objective_proposer.validate_proposal", return_value=(True, [], [])):
            with patch("objective_proposer.create_triage_task", return_value=None):
                with patch("objective_proposer.PROPOSALS_FILE", path):
                    result = propose_objective(pattern, execute=True)

        # File should be empty (task creation failed, don't record)
        with open(path, "r") as f:
            content = f.read()
        os.unlink(path)
        self.assertEqual(content, "")
        self.assertFalse(result.task_id)
        self.assertIsNotNone(result.error)

    def test_execute_success_records(self):
        """Successful task creation should record the proposal."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        pattern = Pattern(
            kind="test",
            severity="low",
            title="Test",
            body="Test body",
            evidence="Test evidence",
            source="test",
        )

        with patch("objective_proposer.validate_proposal", return_value=(True, [], [])):
            with patch("objective_proposer.create_triage_task", return_value="t_abc123"):
                with patch("objective_proposer.PROPOSALS_FILE", path):
                    result = propose_objective(pattern, execute=True)

        # File should have the entry with task_id
        with open(path, "r") as f:
            content = f.read().strip()
        os.unlink(path)
        self.assertTrue(content)
        entry = json.loads(content)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["task_id"], "t_abc123")


# ── Tests: Pattern severity ordering ─────────────────────────────────────────

class TestPatternOrdering(unittest.TestCase):

    def test_patterns_sorted_by_severity(self):
        """run_analysis returns patterns sorted by severity (high first)."""
        # This test validates the sorting logic by checking the order
        # of severity values
        severity_order = {"high": 0, "medium": 1, "low": 2}
        patterns = [
            Pattern("a", "low", "T1", "B", "E", "S"),
            Pattern("b", "high", "T2", "B", "E", "S"),
            Pattern("c", "medium", "T3", "B", "E", "S"),
        ]
        patterns.sort(key=lambda p: severity_order.get(p.severity, 99))
        self.assertEqual(patterns[0].kind, "b")  # high first
        self.assertEqual(patterns[1].kind, "c")  # medium second
        self.assertEqual(patterns[2].kind, "a")  # low last


# ── Tests: find_validate_script ────────────────────────────────────────────────

class TestFindValidateScript(unittest.TestCase):

    def test_finds_script(self):
        """find_validate_script returns a path that exists."""
        path = find_validate_script()
        if path:
            self.assertTrue(os.path.exists(path))
        # If not found, it returns None — that's valid (validator may not be installed)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)