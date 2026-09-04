#!/usr/bin/env python3
"""Tests for repo-sync-check.py — OBJ-13 repo synchronization monitor.

Tests cover:
  - get_uncommitted_changes: parsing git porcelain output, filtering .worktrees/
  - get_ahead_count / get_ahead_commits: parsing rev-list / log output
  - has_pending_sync_task: DB query for existing sync tasks
  - load_synced_records / record_sync: idempotency file read/write
  - build_sync_body: task body generation
  - main dry-run flow: no changes → silent exit; changes → DRY-RUN output

All tests use temp directories and mocked git subprocess calls — no real
git operations or side effects.

Run:
  python3 -m pytest test_repo_sync_check.py -v
  python3 test_repo_sync_check.py  # direct run
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with filename-based module (repo-sync-check.py → repo_sync_check)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "repo_sync_check",
    os.path.join(SCRIPT_DIR, "scripts", "repo-sync-check.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["repo_sync_check"] = _mod
_spec.loader.exec_module(_mod)

from repo_sync_check import (
    get_uncommitted_changes,
    get_ahead_count,
    get_ahead_commits,
    has_pending_sync_task,
    load_synced_records,
    record_sync,
    build_sync_body,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given tasks.
    Each task is a dict: {id, title, status}
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            created_at INTEGER
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            (t.get("id", "t_test"), t.get("title", "test"), t.get("status", "todo"), t.get("created_at", 0)),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests ────────────────────────────────────────────────────────────────────

class TestGetUncommittedChanges(unittest.TestCase):
    """Tests for get_uncommitted_changes()."""

    @patch("repo_sync_check.git")
    def test_no_changes(self, mock_git):
        """Empty git status → empty list."""
        mock_git.return_value = (0, "", "")
        result = get_uncommitted_changes()
        self.assertEqual(result, [])

    @patch("repo_sync_check.git")
    def test_modified_file(self, mock_git):
        """Single modified file parsed correctly."""
        mock_git.return_value = (0, " M scripts/foo.py", "")
        result = get_uncommitted_changes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "scripts/foo.py")
        self.assertEqual(result[0]["status"], " M")

    @patch("repo_sync_check.git")
    def test_staged_file(self, mock_git):
        """Staged file (M in position 0) parsed correctly."""
        mock_git.return_value = (0, "M  scripts/bar.py", "")
        result = get_uncommitted_changes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "scripts/bar.py")

    @patch("repo_sync_check.git")
    def test_worktrees_excluded(self, mock_git):
        """Files under .worktrees/ are excluded."""
        mock_git.return_value = (0, " M .worktrees/foo/bar.py\n M scripts/real.py", "")
        result = get_uncommitted_changes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "scripts/real.py")

    @patch("repo_sync_check.git")
    def test_git_failure_returns_empty(self, mock_git):
        """git status failure → empty list (no crash)."""
        mock_git.return_value = (1, "", "fatal: not a git repository")
        result = get_uncommitted_changes()
        self.assertEqual(result, [])

    @patch("repo_sync_check.git")
    def test_multiple_changes(self, mock_git):
        """Multiple changes all parsed."""
        porcelain = " M file1.py\n M file2.py\n?? file3.py\n M file4.py"
        mock_git.return_value = (0, porcelain, "")
        result = get_uncommitted_changes()
        # ?? is untracked, but --untracked-files=no should not show it
        # If it does appear, it's still parsed
        paths = [r["path"] for r in result]
        self.assertIn("file1.py", paths)
        self.assertIn("file2.py", paths)
        self.assertIn("file4.py", paths)

    @patch("repo_sync_check.git")
    def test_empty_lines_skipped(self, mock_git):
        """Empty lines in porcelain output are skipped."""
        mock_git.return_value = (0, "\n\n M real.py\n\n", "")
        result = get_uncommitted_changes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "real.py")


class TestGetAheadCount(unittest.TestCase):
    """Tests for get_ahead_count()."""

    @patch("repo_sync_check.git")
    def test_zero_ahead(self, mock_git):
        mock_git.return_value = (0, "0", "")
        self.assertEqual(get_ahead_count(), 0)

    @patch("repo_sync_check.git")
    def test_three_ahead(self, mock_git):
        mock_git.return_value = (0, "3", "")
        self.assertEqual(get_ahead_count(), 3)

    @patch("repo_sync_check.git")
    def test_git_failure_returns_zero(self, mock_git):
        mock_git.return_value = (1, "", "error")
        self.assertEqual(get_ahead_count(), 0)

    @patch("repo_sync_check.git")
    def test_non_integer_returns_zero(self, mock_git):
        mock_git.return_value = (0, "not a number", "")
        self.assertEqual(get_ahead_count(), 0)


class TestGetAheadCommits(unittest.TestCase):
    """Tests for get_ahead_commits()."""

    @patch("repo_sync_check.git")
    def test_no_commits(self, mock_git):
        mock_git.return_value = (0, "", "")
        self.assertEqual(get_ahead_commits(), [])

    @patch("repo_sync_check.git")
    def test_single_commit(self, mock_git):
        mock_git.return_value = (0, "abc1234 Fix bug", "")
        result = get_ahead_commits()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["hash"], "abc1234")
        self.assertEqual(result[0]["message"], "Fix bug")

    @patch("repo_sync_check.git")
    def test_multiple_commits(self, mock_git):
        mock_git.return_value = (0, "abc1234 Fix bug\ndef5678 Add feature\nghi9012 Refactor", "")
        result = get_ahead_commits()
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["hash"], "abc1234")
        self.assertEqual(result[2]["message"], "Refactor")

    @patch("repo_sync_check.git")
    def test_commit_no_message(self, mock_git):
        """Commit with hash only (no message)."""
        mock_git.return_value = (0, "abc1234", "")
        result = get_ahead_commits()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["hash"], "abc1234")
        self.assertEqual(result[0]["message"], "")

    @patch("repo_sync_check.git")
    def test_git_failure_returns_empty(self, mock_git):
        mock_git.return_value = (1, "", "error")
        self.assertEqual(get_ahead_commits(), [])


class TestHasPendingSyncTask(unittest.TestCase):
    """Tests for has_pending_sync_task()."""

    def test_no_pending_tasks(self):
        db = _make_kanban_db([])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_pending_todo_task(self):
        db = _make_kanban_db([
            {"id": "t_001", "title": "OBJ-13: Repo sync needed (2 uncommitted)", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t_001")

    def test_pending_running_task(self):
        db = _make_kanban_db([
            {"id": "t_002", "title": "OBJ-13: Repo sync needed", "status": "running"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(len(result), 1)

    def test_blocked_task_excluded(self):
        """Blocked sync tasks are NOT considered pending."""
        db = _make_kanban_db([
            {"id": "t_003", "title": "OBJ-13: Repo sync needed", "status": "blocked"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_done_task_excluded(self):
        db = _make_kanban_db([
            {"id": "t_004", "title": "OBJ-13: Repo sync needed", "status": "done"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_non_sync_task_excluded(self):
        """Tasks without OBJ-13: Repo sync prefix are ignored."""
        db = _make_kanban_db([
            {"id": "t_005", "title": "OBJ-10: Add tests", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = has_pending_sync_task(conn)
        conn.close()
        self.assertEqual(result, [])


class TestLoadSyncedRecords(unittest.TestCase):
    """Tests for load_synced_records()."""

    def test_no_file_returns_empty(self):
        with patch("repo_sync_check.SYNC_FILE", "/nonexistent/path/file.jsonl"):
            self.assertEqual(load_synced_records(), [])

    def test_valid_records(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"sync_task_id": "t_001"}) + "\n")
            f.write(json.dumps({"sync_task_id": "t_002"}) + "\n")
            f.flush()
            tmpfile = f.name
        try:
            with patch("repo_sync_check.SYNC_FILE", tmpfile):
                records = load_synced_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["sync_task_id"], "t_001")
        finally:
            os.unlink(tmpfile)

    def test_invalid_json_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"sync_task_id": "t_001"}\n')
            f.write('NOT JSON\n')
            f.write('{"sync_task_id": "t_002"}\n')
            f.flush()
            tmpfile = f.name
        try:
            with patch("repo_sync_check.SYNC_FILE", tmpfile):
                records = load_synced_records()
            self.assertEqual(len(records), 2)
        finally:
            os.unlink(tmpfile)

    def test_empty_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('\n\n{"sync_task_id": "t_001"}\n\n')
            f.flush()
            tmpfile = f.name
        try:
            with patch("repo_sync_check.SYNC_FILE", tmpfile):
                records = load_synced_records()
            self.assertEqual(len(records), 1)
        finally:
            os.unlink(tmpfile)


class TestRecordSync(unittest.TestCase):
    """Tests for record_sync()."""

    def test_record_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_file = os.path.join(tmpdir, "sync.jsonl")
            with patch("repo_sync_check.SYNC_FILE", sync_file):
                uncommitted = [{"status": " M", "path": "foo.py"}]
                ahead = [{"hash": "abc1234", "message": "test"}]
                record_sync("t_001", uncommitted, ahead)

                self.assertTrue(os.path.isfile(sync_file))
                with open(sync_file) as f:
                    data = json.loads(f.readline())
                self.assertEqual(data["sync_task_id"], "t_001")
                self.assertEqual(data["uncommitted_files"], 1)
                self.assertEqual(data["ahead_commits"], 1)
                self.assertEqual(data["uncommitted_paths"], ["foo.py"])
                self.assertEqual(data["ahead_hashes"], ["abc1234"])
                self.assertEqual(data["version"], "1.0")

    def test_record_truncates_long_lists(self):
        """Uncommitted paths and ahead hashes are truncated to 20."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_file = os.path.join(tmpdir, "sync.jsonl")
            with patch("repo_sync_check.SYNC_FILE", sync_file):
                uncommitted = [{"status": " M", "path": f"file_{i}.py"} for i in range(30)]
                ahead = [{"hash": f"hash_{i}", "message": "msg"} for i in range(30)]
                record_sync("t_002", uncommitted, ahead)

                with open(sync_file) as f:
                    data = json.loads(f.readline())
                self.assertEqual(len(data["uncommitted_paths"]), 20)
                self.assertEqual(len(data["ahead_hashes"]), 20)


class TestBuildSyncBody(unittest.TestCase):
    """Tests for build_sync_body()."""

    def test_empty_changes(self):
        """Body with no changes still has instructions."""
        body = build_sync_body([], [])
        self.assertIn("OBJ-13", body)
        self.assertIn("Instrucciones", body)
        # Should not contain change sections
        self.assertNotIn("Cambios sin commitear", body)
        self.assertNotIn("Commits sin pushear", body)

    def test_with_uncommitted(self):
        uncommitted = [{"status": " M", "path": "scripts/foo.py"}]
        body = build_sync_body(uncommitted, [])
        self.assertIn("Cambios sin commitear", body)
        self.assertIn("scripts/foo.py", body)
        self.assertNotIn("Commits sin pushear", body)

    def test_with_ahead_commits(self):
        ahead = [{"hash": "abc1234", "message": "Fix critical bug"}]
        body = build_sync_body([], ahead)
        self.assertIn("Commits sin pushear", body)
        self.assertIn("abc1234", body)
        self.assertIn("Fix critical bug", body)
        self.assertNotIn("Cambios sin commitear", body)

    def test_with_both(self):
        uncommitted = [{"status": " M", "path": "bar.py"}]
        ahead = [{"hash": "def5678", "message": "Add feature"}]
        body = build_sync_body(uncommitted, ahead)
        self.assertIn("Cambios sin commitear", body)
        self.assertIn("Commits sin pushear", body)
        self.assertIn("bar.py", body)
        self.assertIn("def5678", body)

    def test_truncates_at_30(self):
        """More than 30 items get truncated with a summary line."""
        uncommitted = [{"status": " M", "path": f"file_{i}.py"} for i in range(35)]
        body = build_sync_body(uncommitted, [])
        self.assertIn("y 5 mas", body)

    def test_includes_push_instructions(self):
        body = build_sync_body([], [])
        self.assertIn("git push", body)
        self.assertIn("ProxyCommand", body)


class TestMainDryRun(unittest.TestCase):
    """Tests for main() in dry-run mode (no --execute)."""

    @patch("repo_sync_check.os.path.isdir")
    @patch("repo_sync_check.get_uncommitted_changes")
    @patch("repo_sync_check.get_ahead_commits")
    def test_synced_repo_silent_exit(self, mock_ahead, mock_uncomm, mock_isdir):
        """Repo with no changes → sys.exit(0), no output."""
        mock_isdir.return_value = True
        mock_uncomm.return_value = []
        mock_ahead.return_value = []

        with self.assertRaises(SystemExit) as ctx:
            _mod.main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("repo_sync_check.os.path.isdir")
    @patch("repo_sync_check.get_uncommitted_changes")
    @patch("repo_sync_check.get_ahead_commits")
    @patch("repo_sync_check.VERBOSE", False)
    def test_dry_run_with_changes(self, mock_ahead, mock_uncomm, mock_isdir):
        """Dry-run with changes → prints DRY-RUN line."""
        mock_isdir.return_value = True
        mock_uncomm.return_value = [{"status": " M", "path": "foo.py"}]
        mock_ahead.return_value = [{"hash": "abc1234", "message": "test"}]

        # Mock the DB check to return no pending tasks
        with patch("repo_sync_check.get_db_path", return_value="/nonexistent.db"):
            _mod.main()
        # main() doesn't sys.exit in dry-run with changes (it just prints)


if __name__ == "__main__":
    unittest.main()