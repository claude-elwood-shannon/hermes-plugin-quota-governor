#!/usr/bin/env python3
"""Tests for diagnose-crash.py — OBJ-12 automatic crash diagnosis.

Tests cover:
  - get_crash_blocked_tasks: DB query for blocked tasks with gave_up events
  - get_crash_runs: DB query for failed runs
  - get_gave_up_events: DB query for circuit breaker events
  - get_worker_log: reading worker log files
  - load_diagnosed_ids: idempotency file parsing
  - record_diagnosis: idempotency file writing
  - build_diagnostic_body: task body generation
  - main dry-run flow: no tasks → silent exit; tasks → DRY-RUN output

The diagnose-crash.py script lives at ~/.hermes/scripts/diagnose-crash.py
(outside the plugin repo), so tests import it via absolute path. All DB
operations use temp databases — no side effects on the real kanban.db.

Run:
  python3 -m pytest test_diagnose_crash.py -v
  python3 test_diagnose_crash.py  # direct run
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# The script lives at ~/.hermes/scripts/diagnose-crash.py
SCRIPT_PATH = os.path.expanduser("~/.hermes/scripts/diagnose-crash.py")

# Import with filename-based module
import importlib.util

_spec = importlib.util.spec_from_file_location("diagnose_crash", SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["diagnose_crash"] = _mod
_spec.loader.exec_module(_mod)

from diagnose_crash import (
    get_crash_blocked_tasks,
    get_crash_runs,
    get_gave_up_events,
    get_worker_log,
    load_diagnosed_ids,
    record_diagnosis,
    build_diagnostic_body,
    MAX_DIAGNOSTICS_PER_TICK,
    CRASH_LOG_MAX_CHARS,
    DIAGNOSER_VERSION,
    DIAGNOSIS_WINDOW_HOURS,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks=None, runs=None, events=None) -> str:
    """Create a temp kanban.db with the given tasks, runs, and events."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            last_failure_error TEXT,
            created_by TEXT,
            created_at INTEGER,
            started_at INTEGER,
            completed_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            profile TEXT,
            status TEXT,
            outcome TEXT,
            error TEXT,
            summary TEXT,
            worker_pid INTEGER,
            started_at INTEGER,
            ended_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        )
    """)

    now = int(time.time())
    for t in (tasks or []):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, "
            "consecutive_failures, last_failure_error, created_by, "
            "created_at, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.get("id", "t_test"),
                t.get("title", "test task"),
                t.get("body", ""),
                t.get("assignee", "pr-ollama"),
                t.get("status", "blocked"),
                t.get("consecutive_failures", 3),
                t.get("last_failure_error", "crash"),
                t.get("created_by", None),
                t.get("created_at", now),
                t.get("started_at", now),
                t.get("completed_at", None),
            ),
        )
    for r in (runs or []):
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, outcome, "
            "error, summary, worker_pid, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.get("id", 1), r.get("task_id", "t_test"),
                r.get("profile", "pr-ollama"), r.get("status", "failed"),
                r.get("outcome", "crashed"), r.get("error", "segfault"),
                r.get("summary", None), r.get("worker_pid", 12345),
                r.get("started_at", now), r.get("ended_at", now + 60),
            ),
        )
    for e in (events or []):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                e.get("task_id", "t_test"),
                e.get("kind", "gave_up"),
                e.get("payload", json.dumps({"failures": 3})),
                e.get("created_at", now),
            ),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests: get_crash_blocked_tasks ───────────────────────────────────────────

class TestGetCrashBlockedTasks(unittest.TestCase):

    def test_finds_blocked_with_gave_up(self):
        """Blocked task with gave_up event → found."""
        db = _make_kanban_db(
            tasks=[{"id": "t_001", "consecutive_failures": 3, "status": "blocked"}],
            events=[{"task_id": "t_001", "kind": "gave_up"}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t_001")

    def test_skips_non_blocked(self):
        """Non-blocked tasks are excluded."""
        db = _make_kanban_db(
            tasks=[{"id": "t_002", "status": "running", "consecutive_failures": 3}],
            events=[{"task_id": "t_002", "kind": "gave_up"}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_zero_failures(self):
        """Tasks with 0 consecutive_failures are excluded."""
        db = _make_kanban_db(
            tasks=[{"id": "t_003", "status": "blocked", "consecutive_failures": 0}],
            events=[{"task_id": "t_003", "kind": "gave_up"}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_without_gave_up_event(self):
        """Blocked task without gave_up event → excluded (manual block)."""
        db = _make_kanban_db(
            tasks=[{"id": "t_004", "status": "blocked", "consecutive_failures": 3}],
            events=[{"task_id": "t_004", "kind": "blocked"}],  # not gave_up
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_self_created_diagnostic(self):
        """v1.1 recursion hardening: tasks created by diagnose-crash.py itself
        (created_by='diagnose-crash.py') are NEVER candidates."""
        db = _make_kanban_db(
            tasks=[{"id": "t_008", "status": "blocked", "consecutive_failures": 3,
                    "created_by": "diagnose-crash.py"}],
            events=[{"task_id": "t_008", "kind": "gave_up"}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_other_created_by_still_found(self):
        """created_by set to another creator → still a valid candidate."""
        db = _make_kanban_db(
            tasks=[{"id": "t_009", "status": "blocked", "consecutive_failures": 3,
                    "created_by": "objective-proposer.py"}],
            events=[{"task_id": "t_009", "kind": "gave_up"}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t_009")

    def test_multiple_crash_tasks(self):
        db = _make_kanban_db(
            tasks=[
                {"id": "t_005", "status": "blocked", "consecutive_failures": 3},
                {"id": "t_006", "status": "blocked", "consecutive_failures": 5},
            ],
            events=[
                {"task_id": "t_005", "kind": "gave_up"},
                {"task_id": "t_006", "kind": "gave_up"},
            ],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 2)

    def test_empty_db(self):
        db = _make_kanban_db()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_old_event_outside_window(self):
        """Events older than DIAGNOSIS_WINDOW_HOURS are excluded."""
        now = int(time.time())
        too_old = now - (DIAGNOSIS_WINDOW_HOURS * 3600) - 3600  # 1h past window
        db = _make_kanban_db(
            tasks=[{"id": "t_007", "status": "blocked", "consecutive_failures": 3}],
            events=[{"task_id": "t_007", "kind": "gave_up", "created_at": too_old}],
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = get_crash_blocked_tasks(conn)
        conn.close()
        self.assertEqual(result, [])


# ── Tests: get_crash_runs ────────────────────────────────────────────────────

class TestGetCrashRuns(unittest.TestCase):

    def _connect(self, db):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_finds_failed_runs(self):
        db = _make_kanban_db(
            tasks=[{"id": "t_001"}],
            runs=[
                {"id": 1, "task_id": "t_001", "outcome": "crashed"},
                {"id": 2, "task_id": "t_001", "outcome": "timed_out"},
            ],
        )
        conn = self._connect(db)
        result = get_crash_runs(conn, "t_001")
        conn.close()
        self.assertEqual(len(result), 2)

    def test_excludes_non_crash_outcomes(self):
        db = _make_kanban_db(
            tasks=[{"id": "t_002"}],
            runs=[
                {"id": 1, "task_id": "t_002", "outcome": "completed"},
                {"id": 2, "task_id": "t_002", "outcome": "crashed"},
            ],
        )
        conn = self._connect(db)
        result = get_crash_runs(conn, "t_002")
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["outcome"], "crashed")

    def test_no_runs(self):
        db = _make_kanban_db(tasks=[{"id": "t_003"}])
        conn = self._connect(db)
        result = get_crash_runs(conn, "t_003")
        conn.close()
        self.assertEqual(result, [])

    def test_limits_to_5(self):
        runs = [
            {"id": i, "task_id": "t_004", "outcome": "crashed"}
            for i in range(1, 10)
        ]
        db = _make_kanban_db(tasks=[{"id": "t_004"}], runs=runs)
        conn = self._connect(db)
        result = get_crash_runs(conn, "t_004")
        conn.close()
        self.assertEqual(len(result), 5)


# ── Tests: get_gave_up_events ────────────────────────────────────────────────

class TestGetGaveUpEvents(unittest.TestCase):

    def _connect(self, db):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_finds_events(self):
        db = _make_kanban_db(
            tasks=[{"id": "t_001"}],
            events=[
                {"task_id": "t_001", "kind": "gave_up", "payload": json.dumps({"failures": 3})},
            ],
        )
        conn = self._connect(db)
        result = get_gave_up_events(conn, "t_001")
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["payload"]["failures"], 3)

    def test_parses_json_payload(self):
        db = _make_kanban_db(
            tasks=[{"id": "t_002"}],
            events=[
                {"task_id": "t_002", "kind": "gave_up",
                 "payload": json.dumps({"failures": 5, "effective_limit": 3})},
            ],
        )
        conn = self._connect(db)
        result = get_gave_up_events(conn, "t_002")
        conn.close()
        self.assertEqual(result[0]["payload"]["failures"], 5)
        self.assertEqual(result[0]["payload"]["effective_limit"], 3)

    def test_handles_invalid_json_payload(self):
        db = _make_kanban_db(
            tasks=[{"id": "t_003"}],
            events=[
                {"task_id": "t_003", "kind": "gave_up", "payload": "NOT JSON"},
            ],
        )
        conn = self._connect(db)
        result = get_gave_up_events(conn, "t_003")
        conn.close()
        self.assertEqual(len(result), 1)
        # Invalid JSON leaves payload as the raw string
        self.assertEqual(result[0]["payload"], "NOT JSON")

    def test_no_events(self):
        db = _make_kanban_db(tasks=[{"id": "t_004"}])
        conn = self._connect(db)
        result = get_gave_up_events(conn, "t_004")
        conn.close()
        self.assertEqual(result, [])

    def test_limits_to_3(self):
        events = [
            {"task_id": "t_005", "kind": "gave_up", "payload": "{}"}
            for _ in range(5)
        ]
        db = _make_kanban_db(tasks=[{"id": "t_005"}], events=events)
        conn = self._connect(db)
        result = get_gave_up_events(conn, "t_005")
        conn.close()
        self.assertEqual(len(result), 3)


# ── Tests: get_worker_log ────────────────────────────────────────────────────

class TestGetWorkerLog(unittest.TestCase):

    def test_reads_existing_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("diagnose_crash.WORKER_LOGS_DIR", tmpdir):
                log_path = os.path.join(tmpdir, "t_001.log")
                with open(log_path, "w") as f:
                    f.write("line1\nline2\nTraceback...\n")
                result = get_worker_log("t_001")
                self.assertIn("Traceback", result)
                self.assertIn("line1", result)

    def test_missing_log_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("diagnose_crash.WORKER_LOGS_DIR", tmpdir):
                result = get_worker_log("t_nonexistent")
                self.assertIsNone(result)

    def test_empty_log_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("diagnose_crash.WORKER_LOGS_DIR", tmpdir):
                log_path = os.path.join(tmpdir, "t_002.log")
                with open(log_path, "w") as f:
                    f.write("")
                result = get_worker_log("t_002")
                self.assertIsNone(result)

    def test_truncates_long_log(self):
        """Logs longer than CRASH_LOG_MAX_CHARS are truncated to the tail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("diagnose_crash.WORKER_LOGS_DIR", tmpdir):
                log_path = os.path.join(tmpdir, "t_003.log")
                content = "A" * (CRASH_LOG_MAX_CHARS + 500)
                with open(log_path, "w") as f:
                    f.write(content)
                result = get_worker_log("t_003")
                self.assertEqual(len(result), CRASH_LOG_MAX_CHARS)
                # Should be the tail (last CRASH_LOG_MAX_CHARS chars)
                self.assertTrue(result.endswith("A" * 100))


# ── Tests: load_diagnosed_ids ────────────────────────────────────────────────

class TestLoadDiagnosedIds(unittest.TestCase):

    def test_no_file_returns_empty(self):
        with patch("diagnose_crash.DIAGNOSES_FILE", "/nonexistent/file.jsonl"):
            result = load_diagnosed_ids()
            self.assertEqual(result, set())

    def test_loads_ids(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"task_id": "t_001"}) + "\n")
            f.write(json.dumps({"task_id": "t_002"}) + "\n")
            f.flush()
            tmpfile = f.name
        try:
            with patch("diagnose_crash.DIAGNOSES_FILE", tmpfile):
                result = load_diagnosed_ids()
            self.assertEqual(result, {"t_001", "t_002"})
        finally:
            os.unlink(tmpfile)

    def test_invalid_json_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"task_id": "t_001"}\n')
            f.write('NOT JSON\n')
            f.write('{"task_id": "t_002"}\n')
            f.flush()
            tmpfile = f.name
        try:
            with patch("diagnose_crash.DIAGNOSES_FILE", tmpfile):
                result = load_diagnosed_ids()
            self.assertEqual(result, {"t_001", "t_002"})
        finally:
            os.unlink(tmpfile)

    def test_empty_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('\n\n{"task_id": "t_001"}\n\n')
            f.flush()
            tmpfile = f.name
        try:
            with patch("diagnose_crash.DIAGNOSES_FILE", tmpfile):
                result = load_diagnosed_ids()
            self.assertEqual(result, {"t_001"})
        finally:
            os.unlink(tmpfile)


# ── Tests: record_diagnosis ──────────────────────────────────────────────────

class TestRecordDiagnosis(unittest.TestCase):

    def test_record_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            diag_file = os.path.join(tmpdir, "diagnoses.jsonl")
            with patch("diagnose_crash.DIAGNOSES_FILE", diag_file):
                crash_info = {
                    "consecutive_failures": 3,
                    "trigger_outcome": "crashed",
                    "error": "segfault at 0x0",
                }
                record_diagnosis("t_001", "t_diag_001", crash_info)

                self.assertTrue(os.path.isfile(diag_file))
                with open(diag_file) as f:
                    data = json.loads(f.readline())
                self.assertEqual(data["task_id"], "t_001")
                self.assertEqual(data["diagnostic_task_id"], "t_diag_001")
                self.assertEqual(data["consecutive_failures"], 3)
                self.assertEqual(data["trigger_outcome"], "crashed")
                self.assertIn("segfault", data["error"])
                # Follow the deployed script's version instead of hardcoding,
                # so the test cannot drift again on a version bump.
                self.assertEqual(data["version"], DIAGNOSER_VERSION)

    def test_error_truncated_to_200(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            diag_file = os.path.join(tmpdir, "diagnoses.jsonl")
            with patch("diagnose_crash.DIAGNOSES_FILE", diag_file):
                crash_info = {"error": "X" * 500}
                record_diagnosis("t_002", "t_diag_002", crash_info)
                with open(diag_file) as f:
                    data = json.loads(f.readline())
                self.assertEqual(len(data["error"]), 200)


# ── Tests: build_diagnostic_body ─────────────────────────────────────────────

class TestBuildDiagnosticBody(unittest.TestCase):

    def test_body_contains_task_id(self):
        task = {"id": "t_crash", "title": "broken task", "consecutive_failures": 3,
                "last_failure_error": "segfault", "assignee": "pr-ollama", "body": "task body"}
        body = build_diagnostic_body(task, [], [], None)
        self.assertIn("t_crash", body)
        self.assertIn("OBJ-12", body)
        self.assertIn("segfault", body)
        self.assertIn("broken task", body)

    def test_body_with_runs(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 2,
                "last_failure_error": "err", "assignee": "pr-ollama", "body": ""}
        runs = [{"id": 1, "outcome": "crashed", "error": "segfault", "worker_pid": 123,
                 "started_at": int(time.time())}]
        body = build_diagnostic_body(task, runs, [], None)
        self.assertIn("Failed Runs", body)
        self.assertIn("crashed", body)
        self.assertIn("123", body)

    def test_body_with_gave_up_events(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 3,
                "last_failure_error": "err", "assignee": "pr-ollama", "body": ""}
        events = [{"kind": "gave_up", "payload": {"failures": 3, "effective_limit": 3,
                   "trigger_outcome": "crashed"}, "created_at": int(time.time())}]
        body = build_diagnostic_body(task, [], events, None)
        self.assertIn("Circuit Breaker", body)
        self.assertIn("failures=3", body)

    def test_body_with_worker_log(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 1,
                "last_failure_error": "err", "assignee": "pr-ollama", "body": ""}
        body = build_diagnostic_body(task, [], [], "Traceback: KeyError")
        self.assertIn("Worker Log", body)
        self.assertIn("Traceback", body)

    def test_body_without_worker_log(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 1,
                "last_failure_error": "err", "assignee": "pr-ollama", "body": ""}
        body = build_diagnostic_body(task, [], [], None)
        self.assertIn("No worker log file found", body)

    def test_body_includes_instructions(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 1,
                "last_failure_error": "err", "assignee": "pr-ollama", "body": ""}
        body = build_diagnostic_body(task, [], [], None)
        self.assertIn("Instructions", body)
        self.assertIn("root cause", body)

    def test_body_includes_original_body_excerpt(self):
        task = {"id": "t_001", "title": "test", "consecutive_failures": 1,
                "last_failure_error": "err", "assignee": "pr-ollama",
                "body": "Do something important"}
        body = build_diagnostic_body(task, [], [], None)
        self.assertIn("Do something important", body)
        self.assertIn("Original Task Body", body)


# ── Tests: constants ─────────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):

    def test_max_diagnostics_per_tick(self):
        self.assertEqual(MAX_DIAGNOSTICS_PER_TICK, 1)

    def test_crash_log_max_chars(self):
        self.assertEqual(CRASH_LOG_MAX_CHARS, 2000)

    def test_diagnosis_window_hours(self):
        self.assertEqual(DIAGNOSIS_WINDOW_HOURS, 168)  # 7 days


if __name__ == "__main__":
    unittest.main()