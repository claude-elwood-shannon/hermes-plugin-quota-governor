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

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
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
    find_live_sibling_workers,
    find_recent_sync_task,
    annotate_wip_files,
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


# ── Main-flow test isolation ────────────────────────────────────────────────
# main() runs in-process, so it inherits the unittest CLI in sys.argv
# (argparse then errors with SystemExit(2)) and touches REAL state
# (repo-watch.json, kanban.db, repo-sync-check.log). These helpers pin
# argv and redirect every path to a temp sandbox so tests can never
# create board tasks or write to real config/log files.

def _patch_main_isolation(patchers, tmpdir):
    """Register on a list the patchers that isolate main() from real state."""
    patchers.append(patch("sys.argv", ["repo-sync-check.py"]))
    patchers.append(patch("repo_sync_check.CONFIG_FILE",
                          os.path.join(tmpdir, "repo-watch.json")))
    patchers.append(patch("repo_sync_check.LOG_FILE",
                          os.path.join(tmpdir, "repo-sync-check.log")))
    patchers.append(patch("repo_sync_check.get_db_path",
                          return_value=os.path.join(tmpdir, "nonexistent.db")))
    # Deploy-drift check must not read the real repo/scripts or real
    # deploy dirs in main-flow tests: point it at an empty sandbox.
    patchers.append(patch("repo_sync_check.REPO_DIR", tmpdir))
    patchers.append(patch("repo_sync_check.DEPLOY_DIRS", []))
    # Defense in depth: even with isolation, never honor execute mode.
    patchers.append(patch.dict(os.environ, {"REPO_SYNC_EXECUTE": ""}))


class _LogIsolationMixin(unittest.TestCase):
    """TestCase base redirecting the script's LOG_FILE to a temp dir.

    log() writes unconditionally to the real
    ~/.hermes/logs/repo-sync-check.log; tests exercising failure paths
    (mocked git failures, corrupt configs) would otherwise append noise
    to the production ops log. Override setUp/tearDown but call super().
    """

    def setUp(self):
        self._log_tmp = tempfile.TemporaryDirectory()
        self._log_patcher = patch(
            "repo_sync_check.LOG_FILE",
            os.path.join(self._log_tmp.name, "repo-sync-check.log"),
        )
        self._log_patcher.start()
        super().setUp()

    def tearDown(self):
        self._log_patcher.stop()
        self._log_tmp.cleanup()
        super().tearDown()


# ── Tests ────────────────────────────────────────────────────────────────────

class TestGetUncommittedChanges(_LogIsolationMixin, unittest.TestCase):
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


class TestGetAheadCount(_LogIsolationMixin, unittest.TestCase):
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
                self.assertEqual(data["version"], "3.0")
                # Multi-repo: ledger records the repo/remote/branch identity
                self.assertEqual(data["repo"], _mod.REPO_DIR)
                self.assertEqual(data["remote"], "origin")
                self.assertEqual(data["pattern_key"], f"{_mod.REPO_DIR}@origin")

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

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = []
        _patch_main_isolation(self._patchers, self._tmp.name)
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_synced_repo_silent_exit(self):
        """Repo with no changes → sys.exit(0), no output."""
        with patch("repo_sync_check.os.path.isdir", return_value=True), \
             patch("repo_sync_check.get_uncommitted_changes", return_value=[]), \
             patch("repo_sync_check.get_ahead_commits", return_value=[]), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_dry_run_with_changes(self):
        """Dry-run with changes → prints DRY-RUN line."""
        with patch("repo_sync_check.os.path.isdir", return_value=True), \
             patch("repo_sync_check.get_uncommitted_changes",
                   return_value=[{"status": " M", "path": "foo.py"}]), \
             patch("repo_sync_check.get_ahead_commits",
                   return_value=[{"hash": "abc1234", "message": "test"}]), \
             patch("repo_sync_check.compute_desync_time", return_value=12345), \
             patch("repo_sync_check.VERBOSE", False), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            _mod.main()
        printed = out.getvalue()
        self.assertIn("DRY-RUN", printed)
        self.assertIn("foo.py", printed)
        self.assertIn("abc1234", printed)
        # main() doesn't sys.exit in dry-run with changes (it just prints)


# ── Multi-repo config tests ──────────────────────────────────────────────────

class TestLoadRepoConfigs(_LogIsolationMixin, unittest.TestCase):
    """Tests for load_repo_configs()."""

    def test_config_absent_falls_back_to_single_repo(self):
        """No config file → legacy single-repo config (REPO_DIR)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("repo_sync_check.CONFIG_FILE", os.path.join(tmpdir, "missing.json")):
                configs = _mod.load_repo_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["repo"], _mod.REPO_DIR)
        self.assertTrue(configs[0]["enabled"])
        self.assertEqual(configs[0]["remote"], "origin")

    def test_valid_multi_repo(self):
        """Valid config with 2+ repos → parsed with defaults applied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = os.path.join(tmpdir, "repo-watch.json")
            with open(cfg, "w") as f:
                json.dump([
                    {"repo": "/a/b", "enabled": True},
                    {"repo": "/c/d", "enabled": False, "assignee": "pr-x", "remote": "upstream"},
                    {"repo": "/e/f"},
                ], f)
            with patch("repo_sync_check.CONFIG_FILE", cfg):
                configs = _mod.load_repo_configs()
        self.assertEqual(len(configs), 3)
        self.assertEqual(configs[0]["assignee"], "auto")
        self.assertEqual(configs[0]["remote"], "origin")
        self.assertEqual(configs[1]["enabled"], False)
        self.assertEqual(configs[1]["assignee"], "pr-x")
        self.assertEqual(configs[1]["remote"], "upstream")
        self.assertEqual(configs[2]["enabled"], True)

    def test_corrupt_config_raises(self):
        """Corrupt JSON → ValueError (caller aborts, no tasks)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = os.path.join(tmpdir, "repo-watch.json")
            with open(cfg, "w") as f:
                f.write("{ not json !!!")
            with patch("repo_sync_check.CONFIG_FILE", cfg):
                with self.assertRaises(ValueError):
                    _mod.load_repo_configs()

    def test_non_array_config_raises(self):
        """Valid JSON but not an array → ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = os.path.join(tmpdir, "repo-watch.json")
            with open(cfg, "w") as f:
                json.dump({"repo": "/a/b"}, f)
            with patch("repo_sync_check.CONFIG_FILE", cfg):
                with self.assertRaises(ValueError):
                    _mod.load_repo_configs()

    def test_entry_without_repo_skipped(self):
        """Entries missing 'repo' are skipped, not fatal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = os.path.join(tmpdir, "repo-watch.json")
            with open(cfg, "w") as f:
                json.dump([
                    {"enabled": True},
                    {"repo": "/ok/repo"},
                ], f)
            with patch("repo_sync_check.CONFIG_FILE", cfg):
                configs = _mod.load_repo_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["repo"], "/ok/repo")


class TestMultiRepoHasPendingPerRepo(unittest.TestCase):
    """Per-repo board idempotency."""

    def test_pending_for_other_repo_is_not_pending_for_this_repo(self):
        """A pending task naming repo A must not block repo B."""
        db = _make_kanban_db([
            {"id": "t_a", "title": "OBJ-13: Repo sync myrepoA needed", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(len(has_pending_sync_task(conn, repo="/x/myrepoB")), 0)
            self.assertEqual(len(has_pending_sync_task(conn, repo="/x/myrepoA")), 1)
        finally:
            conn.close()

    def test_pending_for_same_repo_matches(self):
        """A pending task naming repo A blocks repo A regardless of timestamp."""
        db = _make_kanban_db([
            {"id": "t_a", "title": "OBJ-13: Repo sync myrepoA needed", "status": "running"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(len(has_pending_sync_task(conn, repo="/x/myrepoA")), 1)
        finally:
            conn.close()


class TestGatherDirtyRepos(_LogIsolationMixin, unittest.TestCase):
    """Multi-repo orchestration: dirty detection, disabled skip, pending surfacing."""

    def setUp(self):
        super().setUp()  # LOG_FILE redirect from _LogIsolationMixin
        self.patchers = []

    def tearDown(self):
        for p in self.patchers:
            try:
                p.stop()
            except Exception:
                pass
        super().tearDown()  # _LogIsolationMixin cleanup

    def _patch(self, target, **kw):
        p = patch(target, **kw)
        p.start()
        self.patchers.append(p)
        return p

    def _configs(self, entries):
        return [_mod._normalize_config(e) for e in entries]

    def test_two_dirty_repos_both_gathered_with_desync_priority(self):
        """2 dirty repos → both gathered; desync_time allows oldest-first pick."""
        self._patch("repo_sync_check._repo_needs_sync",
                    side_effect=[
                        ([{"status": " M", "path": "a.py"}], [], {"fresh_untracked": []}),
                        ([{"status": " M", "path": "b.py"}], [], {"fresh_untracked": []}),
                    ])
        self._patch("repo_sync_check.compute_desync_time",
                    side_effect=[100, 50])
        self._patch("repo_sync_check.os.path.isfile", return_value=False)

        dirty = _mod._gather_dirty_repos(
            self._configs([
                {"repo": "/r/repoA"},
                {"repo": "/r/repoB"},
            ]),
            db_path="/nonexistent.db",
        )
        self.assertEqual(len(dirty), 2)
        # repoB has the older desync (50 < 100)
        by_repo = {d["cfg"]["repo"]: d for d in dirty}
        self.assertEqual(by_repo["/r/repoA"]["desync_time"], 100)
        self.assertEqual(by_repo["/r/repoB"]["desync_time"], 50)

    def test_disabled_repo_skipped(self):
        """A repo disabled in config is not gathered."""
        self._patch("repo_sync_check._repo_needs_sync",
                    side_effect=[
                        ([{"status": " M", "path": "a.py"}], [], {"fresh_untracked": []}),  # only repoA called
                    ])
        self._patch("repo_sync_check.os.path.isfile", return_value=False)

        dirty = _mod._gather_dirty_repos(
            self._configs([
                {"repo": "/r/repoA", "enabled": True},
                {"repo": "/r/repoDisabled", "enabled": False},
            ]),
            db_path="/nonexistent.db",
        )
        self.assertEqual(len(dirty), 1)
        self.assertEqual(dirty[0]["cfg"]["repo"], "/r/repoA")

    def test_pending_is_surfaced(self):
        """A repo with an existing pending board task is flagged as pending."""
        self._patch("repo_sync_check._repo_needs_sync",
                    side_effect=[
                        ([{"status": " M", "path": "a.py"}], [], {"fresh_untracked": []}),
                    ])
        db = _make_kanban_db([{"id": "t_p", "title": "OBJ-13: Repo sync repoA needed", "status": "todo"}])
        self._patch("repo_sync_check.os.path.isfile", return_value=True)
        with patch("repo_sync_check.sqlite3.connect", return_value=sqlite3.connect(db)):
            dirty = _mod._gather_dirty_repos(
                self._configs([{"repo": "/r/repoA"}]),
                db_path=db,
            )
        self.assertEqual(len(dirty), 1)
        self.assertTrue(dirty[0]["pending"])
        self.assertEqual(dirty[0]["pending_id"], "t_p")


class TestSiblingWipSuppression(_LogIsolationMixin, unittest.TestCase):
    """Sibling-WIP suppression + dedupe (t_261f31e3).

    Regression: the scanner created two identical "Repo sync needed" cards 30
    min apart (t_f2ff57c5, t_ad90e6e4) for files that were the WIP of live
    task t_a8d38c10 (OBJ-42 capa 2, worker pr-ollama, heartbeats every ~60s).
    """

    NOW = 1_800_000_000.0

    def setUp(self):
        super().setUp()  # LOG_FILE redirect
        self.patchers = []

    def tearDown(self):
        for p in self.patchers:
            try:
                p.stop()
            except Exception:
                pass
        super().tearDown()

    def _patch(self, target, **kw):
        p = patch(target, **kw)
        p.start()
        self.patchers.append(p)
        return p

    @staticmethod
    def _db(tasks):
        """Temp kanban.db with full-column tasks; returns path."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, status TEXT,
                assignee TEXT, last_heartbeat_at REAL, worker_pid INTEGER,
                current_run_id INTEGER, created_at REAL, completed_at REAL
            )
        """)
        for t in tasks:
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    t.get("id"), t.get("title", "x"), t.get("status", "todo"),
                    t.get("assignee"), t.get("last_heartbeat_at"),
                    t.get("worker_pid"), t.get("current_run_id"),
                    t.get("created_at", 0), t.get("completed_at"),
                ),
            )
        conn.commit()
        conn.close()
        return path

    def _gather(self, db_tasks, db_path, uncommitted=None, ahead=None, desync=None):
        self._patch("repo_sync_check._repo_needs_sync",
                    side_effect=[(uncommitted or [{"status": " M", "path": "gateway/kanban_watchers.py"}],
                                  ahead or [], {"fresh_untracked": []})])
        self._patch("repo_sync_check.compute_desync_time", return_value=desync if desync is not None else self.NOW - 120)
        self._patch("repo_sync_check.time.time", return_value=self.NOW)
        return _mod._gather_dirty_repos(self._configs([{"repo": "/r/hermes-agent"}]), db_path=db_path)

    def _configs(self, entries):
        return [_mod._normalize_config(e) for e in entries]

    # -- find_live_sibling_workers ------------------------------------------

    def test_live_worker_fresh_heartbeat(self):
        db = self._db([{"id": "t_a8d38c10", "status": "running", "assignee": "pr-ollama",
                        "last_heartbeat_at": self.NOW - 60, "worker_pid": 862}])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            live = find_live_sibling_workers(conn, now=self.NOW)
        finally:
            conn.close()
        self.assertEqual([l["task_id"] for l in live], ["t_a8d38c10"])
        self.assertEqual(live[0]["assignee"], "pr-ollama")

    def test_stale_heartbeat_not_live(self):
        db = self._db([{"id": "t_dead", "status": "running", "last_heartbeat_at": self.NOW - 3600}])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(find_live_sibling_workers(conn, now=self.NOW), [])
        finally:
            conn.close()

    def test_done_and_heartbeatless_not_live(self):
        db = self._db([
            {"id": "t_done", "status": "done", "last_heartbeat_at": self.NOW - 10},
            {"id": "t_nohb", "status": "running", "last_heartbeat_at": None},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(find_live_sibling_workers(conn, now=self.NOW), [])
        finally:
            conn.close()

    def test_future_heartbeat_ignored(self):
        """Clock skew guard: heartbeat in the future is not 'live'."""
        db = self._db([{"id": "t_skew", "status": "running", "last_heartbeat_at": self.NOW + 999}])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(find_live_sibling_workers(conn, now=self.NOW), [])
        finally:
            conn.close()

    # -- find_recent_sync_task (dedupe window) -------------------------------

    def test_dedupe_matches_recent_card_any_status(self):
        db = self._db([
            {"id": "t_f2ff57c5",
             "title": "OBJ-13: Repo sync hermes-agent needed (2 uncommitted) [2026-09-12 22:31]",
             "status": "done", "created_at": self.NOW - 30 * 60},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            hit = find_recent_sync_task(conn, "/home/iinstances/.hermes/hermes-agent", now=self.NOW)
        finally:
            conn.close()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["task_id"], "t_f2ff57c5")
        self.assertEqual(hit["status"], "done")

    def test_dedupe_ignores_card_outside_window(self):
        db = self._db([
            {"id": "t_old", "title": "OBJ-13: Repo sync hermes-agent needed", "status": "done",
             "created_at": self.NOW - 3 * 3600},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertIsNone(find_recent_sync_task(conn, "/home/iinstances/.hermes/hermes-agent", now=self.NOW))
        finally:
            conn.close()

    def test_dedupe_ignores_other_repo(self):
        db = self._db([
            {"id": "t_qg", "title": "OBJ-13: Repo sync hermes-plugin-quota-governor needed (1 unpushed)",
             "status": "todo", "created_at": self.NOW - 5 * 60},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            self.assertIsNone(find_recent_sync_task(conn, "/home/iinstances/.hermes/hermes-agent", now=self.NOW))
        finally:
            conn.close()

    # -- annotate_wip_files ---------------------------------------------------

    def test_annotate_splits_by_untracked_age(self):
        changes = [
            {"status": " M", "path": "tracked.py", "age_seconds": None},
            {"status": "??", "path": "fresh.py", "age_seconds": 120},
            {"status": "??", "path": "old.py", "age_seconds": 7 * 3600},
            {"status": "??", "path": "unknown.py", "age_seconds": None},
        ]
        tracked, fresh, old = annotate_wip_files("/tmp", changes)
        self.assertEqual([c["path"] for c in tracked], ["tracked.py"])
        self.assertEqual([c["path"] for c in fresh], ["fresh.py"])
        self.assertEqual([c["path"] for c in old], ["old.py", "unknown.py"])

    # -- _gather_dirty_repos integration --------------------------------------

    def test_wip_of_live_worker_suppressed(self):
        """THE t_a8d38c10 replay: fresh uncommitted + live worker → no card."""
        db = self._db([{"id": "t_a8d38c10", "status": "running",
                        "last_heartbeat_at": self.NOW - 60}])
        dirty = self._gather(db_tasks=None, db_path=db)
        self.assertEqual(len(dirty), 1)
        self.assertTrue(dirty[0]["wip_suppressed"])
        self.assertFalse(dirty[0]["eligible"])

    def test_uncommitted_with_live_worker_but_old_desync_eligible(self):
        """Debt older than WIP_MAX_DESYNC_AGE predates the claim → alert."""
        db = self._db([{"id": "t_live", "status": "running",
                        "last_heartbeat_at": self.NOW - 60}])
        dirty = self._gather(db_tasks=None, db_path=db, desync=self.NOW - 5 * 3600)
        self.assertFalse(dirty[0]["wip_suppressed"])
        self.assertTrue(dirty[0]["eligible"])

    def test_unpushed_commits_not_suppressed_by_live_worker(self):
        """Ahead commits are deliberate work: WIP suppression must not eat them."""
        db = self._db([{"id": "t_live", "status": "running",
                        "last_heartbeat_at": self.NOW - 60}])
        dirty = self._gather(db_tasks=None, db_path=db,
                             uncommitted=[], ahead=[{"hash": "abc1234", "message": "feat"}],
                             desync=self.NOW - 60)
        self.assertFalse(dirty[0]["wip_suppressed"])
        self.assertTrue(dirty[0]["eligible"])

    def test_dedupe_window_blocks_realert_even_when_resolved(self):
        """Second identical alert 30 min after the first was handled → skip."""
        db = self._db([
            {"id": "t_live", "status": "running", "last_heartbeat_at": self.NOW - 3 * 3600},  # dead
            {"id": "t_ad90e6e4", "title": "OBJ-13: Repo sync hermes-agent needed (2 uncommitted)",
             "status": "done", "created_at": self.NOW - 30 * 60},
        ])
        dirty = self._gather(db_tasks=None, db_path=db)
        self.assertFalse(dirty[0]["wip_suppressed"])
        self.assertIsNotNone(dirty[0]["recent_sync"])
        self.assertFalse(dirty[0]["eligible"])

    def test_missing_desync_ts_suppresses_conservatively(self):
        """desync_time=None + live worker + uncommitted → suppress (safe side)."""
        db = self._db([{"id": "t_live", "status": "running",
                        "last_heartbeat_at": self.NOW - 60}])
        dirty = self._gather(db_tasks=None, db_path=db, desync=None)
        self.assertTrue(dirty[0]["wip_suppressed"])

    def test_no_db_fail_open(self):
        """Board DB unavailable → no suppression, no dedupe (create if needed)."""
        dirty = self._gather(db_tasks=None, db_path="/nonexistent/db")
        self.assertFalse(dirty[0]["wip_suppressed"])
        self.assertIsNone(dirty[0]["recent_sync"])
        self.assertTrue(dirty[0]["eligible"])


class TestMainMultiRepo(unittest.TestCase):
    """main() end-to-end multi-repo behavior."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = []
        _patch_main_isolation(self._patchers, self._tmp.name)
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _patch(self, target, **kw):
        p = patch(target, **kw)
        p.start()
        self._patchers.append(p)
        return p

    def test_two_dirty_dry_run_creates_one_for_oldest(self):
        """Dry-run over 2 dirty repos → DRY-RUN names only the oldest-desync one."""
        dirty = [
            {
                "cfg": _mod._normalize_config({"repo": "/r/repoA"}),
                "uncommitted": [{"status": " M", "path": "a.py"}],
                "ahead_commits": [],
                "desync_time": 200,
                "pending": False,
                "pending_id": None,
                "live_workers": [],
                "wip_suppressed": False,
                "recent_sync": None,
                "fresh_untracked": [],
                "eligible": True,
            },
            {
                "cfg": _mod._normalize_config({"repo": "/r/repoB"}),
                "uncommitted": [{"status": " M", "path": "b.py"}],
                "ahead_commits": [],
                "desync_time": 100,  # older → this wins
                "pending": False,
                "pending_id": None,
                "live_workers": [],
                "wip_suppressed": False,
                "recent_sync": None,
                "fresh_untracked": [],
                "eligible": True,
            },
        ]
        self._patch("repo_sync_check._gather_dirty_repos", return_value=dirty)
        self._patch("repo_sync_check.load_repo_configs", return_value=[
            _mod._normalize_config({"repo": "/r/repoA"}),
            _mod._normalize_config({"repo": "/r/repoB"}),
        ])
        out = io.StringIO()
        self._patch("sys.stdout", new=out)
        _mod.main()
        printed = out.getvalue()
        self.assertIn("repoB", printed)
        self.assertIn("DRY-RUN", printed)
        self.assertNotIn("would create sync task for repoA", printed)

    def test_corrupt_config_no_tasks(self):
        """Corrupt config → sys.exit(1) without creating any task."""
        mock_gather = MagicMock()
        self._patch("repo_sync_check._gather_dirty_repos", new=mock_gather)
        self._patch("repo_sync_check.load_repo_configs",
                    side_effect=ValueError("corrupt"))
        with self.assertRaises(SystemExit) as ctx:
            _mod.main()
        self.assertEqual(ctx.exception.code, 1)
        mock_gather.assert_not_called()

    def test_all_pending_skips_new_task(self):
        """If every dirty repo already has a pending task, nothing is created."""
        dirty = [
            {
                "cfg": _mod._normalize_config({"repo": "/r/repoA"}),
                "uncommitted": [{"status": " M", "path": "a.py"}],
                "ahead_commits": [],
                "desync_time": 100,
                "pending": True,
                "pending_id": "t_p",
                "live_workers": [],
                "wip_suppressed": False,
                "recent_sync": None,
                "fresh_untracked": [],
                "eligible": False,
            },
        ]
        self._patch("repo_sync_check._gather_dirty_repos", return_value=dirty)
        self._patch("repo_sync_check.load_repo_configs", return_value=[
            _mod._normalize_config({"repo": "/r/repoA"}),
        ])
        with self.assertRaises(SystemExit) as ctx:
            _mod.main()
        self.assertEqual(ctx.exception.code, 0)


# ── Deploy-drift tests (OBJ-06 / t_b8511377) ─────────────────────────────────

class TestMd5OfFile(_LogIsolationMixin, unittest.TestCase):
    """md5_of_file(): digest, missing file, unreadable file."""

    def test_digest_matches_known(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello\n")
            path = f.name
        try:
            import hashlib
            expected = hashlib.md5(b"hello\n").hexdigest()
            self.assertEqual(_mod.md5_of_file(path), expected)
        finally:
            os.unlink(path)

    def test_missing_file_returns_none(self):
        self.assertIsNone(_mod.md5_of_file("/nonexistent/x/y.txt"))

    def test_unreadable_returns_none_and_does_not_raise(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("secret")
            path = f.name
        try:
            os.chmod(path, 0)
            if os.access(path, os.R_OK):
                self.skipTest("running as root — chmod 0 still readable")
            self.assertIsNone(_mod.md5_of_file(path))
        finally:
            os.chmod(path, 0o644)
            os.unlink(path)


class TestCheckDeployDrift(_LogIsolationMixin, unittest.TestCase):
    """check_deploy_drift(): stale/no-deployed/subdir semantics."""

    def _sandbox(self):
        tmp = tempfile.mkdtemp(prefix="drift-test-")
        repo_scripts = os.path.join(tmp, "repo", "scripts")
        os.makedirs(repo_scripts)
        return tmp, repo_scripts

    def test_no_drift_when_all_deployed_copies_match(self):
        tmp, repo_scripts = self._sandbox()
        dep = os.path.join(tmp, "deploy"); os.makedirs(dep)
        open(os.path.join(repo_scripts, "a.py"), "w").write("v1")
        open(os.path.join(dep, "a.py"), "w").write("v1")
        with patch("repo_sync_check.DEPLOY_DIRS", [dep]):
            self.assertEqual(_mod.check_deploy_drift(repo_dir=os.path.dirname(repo_scripts)), [])

    def test_stale_copy_reported_with_md5s(self):
        tmp, repo_scripts = self._sandbox()
        dep = os.path.join(tmp, "deploy"); os.makedirs(dep)
        open(os.path.join(repo_scripts, "a.py"), "w").write("v2")
        open(os.path.join(dep, "a.py"), "w").write("v1")
        with patch("repo_sync_check.DEPLOY_DIRS", [dep]):
            drift = _mod.check_deploy_drift(repo_dir=os.path.dirname(repo_scripts))
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["script"], "a.py")
        self.assertEqual(drift[0]["repo_md5"], _mod.md5_of_file(os.path.join(repo_scripts, "a.py")))
        self.assertEqual(len(drift[0]["stale_copies"]), 1)
        self.assertEqual(drift[0]["stale_copies"][0]["md5"], _mod.md5_of_file(os.path.join(dep, "a.py")))
        self.assertIn("mtime", drift[0]["stale_copies"][0])

    def test_no_deployed_script_not_reported(self):
        """Script exists only in repo → no-deployed, NOT stale (criterion)."""
        tmp, repo_scripts = self._sandbox()
        dep = os.path.join(tmp, "deploy"); os.makedirs(dep)
        open(os.path.join(repo_scripts, "never-deployed.py"), "w").write("x")
        with patch("repo_sync_check.DEPLOY_DIRS", [dep]):
            drift = _mod.check_deploy_drift(repo_dir=os.path.dirname(repo_scripts))
        self.assertEqual(drift, [])

    def test_subdirs_ignored(self):
        tmp, repo_scripts = self._sandbox()
        os.makedirs(os.path.join(repo_scripts, "__pycache__"))
        with patch("repo_sync_check.DEPLOY_DIRS", []):
            self.assertEqual(_mod.check_deploy_drift(repo_dir=os.path.dirname(repo_scripts)), [])

    def test_missing_scripts_dir_returns_empty(self):
        with patch("repo_sync_check.DEPLOY_DIRS", []):
            self.assertEqual(_mod.check_deploy_drift(repo_dir="/nonexistent"), [])

    def test_one_stale_one_synced_copy(self):
        """A script with 2 deployed copies: only the diverging one is stale."""
        tmp, repo_scripts = self._sandbox()
        d1 = os.path.join(tmp, "d1"); os.makedirs(d1)
        d2 = os.path.join(tmp, "d2"); os.makedirs(d2)
        open(os.path.join(repo_scripts, "a.py"), "w").write("v2")
        open(os.path.join(d1, "a.py"), "w").write("v1")  # stale
        open(os.path.join(d2, "a.py"), "w").write("v2")  # synced
        with patch("repo_sync_check.DEPLOY_DIRS", [d1, d2]):
            drift = _mod.check_deploy_drift(repo_dir=os.path.dirname(repo_scripts))
        self.assertEqual(len(drift), 1)
        self.assertEqual(len(drift[0]["stale_copies"]), 1)
        self.assertEqual(drift[0]["stale_copies"][0]["dir"], d1)


class TestReportDeployDrift(_LogIsolationMixin, unittest.TestCase):
    """report_deploy_drift(): stdout alert format."""

    def test_alert_line_and_copies(self):
        drift = [{
            "script": "tick.sh",
            "repo_md5": "aaa",
            "repo_path": "/r/scripts/tick.sh",
            "stale_copies": [{"dir": "/d", "path": "/d/tick.sh", "md5": "bbb", "mtime": 0}],
        }]
        out = io.StringIO()
        with patch("sys.stdout", new=out):
            _mod.report_deploy_drift(drift)
        printed = out.getvalue()
        self.assertIn("DEPLOY_DRIFT", printed)
        self.assertIn("tick.sh", printed)
        self.assertIn("aaa", printed)
        self.assertIn("bbb", printed)
        self.assertIn("STALE", printed)
        self.assertIn("mtime", printed)


class TestMainDeployDrift(_LogIsolationMixin, unittest.TestCase):
    """main() integration: drift alert printed, no-deployed stays silent."""

    def _make_sandbox(self, tmp):
        """Create repo/scripts/stale.sh (repo copy v2) + deploy copy (v1).

        Returns (repo_root, dep): REPO_DIR must point at the repo ROOT
        (check_deploy_drift appends 'scripts/' itself).
        """
        repo_root = os.path.join(tmp, "repo")
        repo_scripts = os.path.join(repo_root, "scripts")
        os.makedirs(repo_scripts)
        dep = os.path.join(tmp, "deploy"); os.makedirs(dep)
        open(os.path.join(repo_scripts, "stale.sh"), "w").write("repo-v2")
        open(os.path.join(dep, "stale.sh"), "w").write("deploy-v1")
        return repo_root, dep

    def _run_main_in_sandbox(self, tmp):
        repo_root, dep = self._make_sandbox(tmp)
        out = io.StringIO()
        with patch("sys.argv", ["repo-sync-check.py"]), \
             patch("repo_sync_check.REPO_DIR", repo_root), \
             patch("repo_sync_check.DEPLOY_DIRS", [dep]), \
             patch("repo_sync_check.CONFIG_FILE", os.path.join(tmp, "repo-watch.json")), \
             patch("repo_sync_check.LOG_FILE", os.path.join(tmp, "log")), \
             patch("repo_sync_check.get_db_path", return_value=os.path.join(tmp, "db")), \
             patch("repo_sync_check.os.path.isdir", return_value=True), \
             patch("repo_sync_check.get_uncommitted_changes", return_value=[]), \
             patch("repo_sync_check.get_ahead_commits", return_value=[]), \
             patch("sys.stdout", new=out):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        return ctx.exception.code, out.getvalue()

    def test_drift_alert_printed_even_when_git_synced(self):
        code, printed = self._run_main_in_sandbox(tempfile.mkdtemp(prefix="drift-main-"))
        self.assertEqual(code, 0)
        self.assertIn("DEPLOY_DRIFT", printed)
        self.assertIn("stale.sh", printed)

    def test_no_deployed_does_not_break_silent_exit(self):
        tmp = tempfile.mkdtemp(prefix="drift-main2-")
        repo_root = os.path.join(tmp, "repo")
        repo_scripts = os.path.join(repo_root, "scripts")
        os.makedirs(repo_scripts)
        open(os.path.join(repo_scripts, "only-repo.sh"), "w").write("x")  # never deployed
        out = io.StringIO()
        with patch("sys.argv", ["repo-sync-check.py"]), \
             patch("repo_sync_check.REPO_DIR", repo_root), \
             patch("repo_sync_check.DEPLOY_DIRS", []), \
             patch("repo_sync_check.CONFIG_FILE", os.path.join(tmp, "repo-watch.json")), \
             patch("repo_sync_check.LOG_FILE", os.path.join(tmp, "log")), \
             patch("repo_sync_check.get_db_path", return_value=os.path.join(tmp, "db")), \
             patch("repo_sync_check.os.path.isdir", return_value=True), \
             patch("repo_sync_check.get_uncommitted_changes", return_value=[]), \
             patch("repo_sync_check.get_ahead_commits", return_value=[]), \
             patch("sys.stdout", new=out):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("DEPLOY_DRIFT", out.getvalue())
        self.assertNotIn("only-repo.sh", out.getvalue())


if __name__ == "__main__":
    unittest.main()