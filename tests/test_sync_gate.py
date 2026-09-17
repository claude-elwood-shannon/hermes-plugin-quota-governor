#!/usr/bin/env python3
"""Tests for scripts/sync_gate.py — OBJ-13 pre-commit syntax gate (t_cec89d77).

Regression for the 2026-09-15 incident: sync commit f92b3d2 committed
context-sync-corrupted WIP (a misplaced `from __future__` import) and
propagated it to every deployed copy; budget_check and fondo-queue-watch
crashed at 03:21:29 and the commit was reverted in 8bc198e. The gate must
reject ANY WIP containing a SyntaxError before a sync commit happens.

Covered:
  - check_file: py / sh pass & fail, non-code skips, missing skips,
    generated-dir skips, no .pyc pollution of the source tree
  - check_files: mixed batches, one broken file fails the whole batch
  - touched_files: porcelain parsing (renames, worktrees, deletes, untracked)
  - alert: BLOCKED output lists every broken file with its check name
  - main: end-to-end exit codes incl. the regression scenario
    (WIP with SyntaxError must NOT pass the sync gate)

All tests use temp files/dirs and mocked git calls — no repo mutation.

Run:
  python3 -m pytest tests/test_sync_gate.py -v
  python3 tests/test_sync_gate.py  # direct run
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

_spec = importlib.util.spec_from_file_location(
    "sync_gate",
    os.path.join(SCRIPT_DIR, "scripts", "sync_gate.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["sync_gate"] = _mod
_spec.loader.exec_module(_mod)

from sync_gate import (
    alert,
    check_file,
    check_files,
    main,
    touched_files,
)

# The exact 2026-09-15 failure mode: `from __future__` after code.
BROKEN_PY = "import os\n\nX = 1\n\nfrom __future__ import annotations\n"
GOOD_PY = "import os\n\nX = 1\n"
BROKEN_SH = "if [ true ]; then\n  echo unbalanced\n"
GOOD_SH = "#!/bin/bash\necho ok\n"


class _TempFilesTestCase(unittest.TestCase):
    """Base: temp workspace + redirected gate log (never the real one)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_patcher = mock.patch.object(
            _mod, "LOG_FILE", os.path.join(self._tmp.name, "gate.log")
        )
        self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)

    def write(self, name: str, content: str) -> str:
        path = os.path.join(self._tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path


class TestCheckFile(_TempFilesTestCase):
    """Single-file verdicts."""

    def test_good_py_passes(self):
        p = self.write("good.py", GOOD_PY)
        self.assertEqual(check_file(p), ("ok", "py_compile"))

    def test_broken_py_fails(self):
        """THE regression: misplaced __future__ import must be flagged."""
        p = self.write("broken.py", BROKEN_PY)
        status, detail = check_file(p)
        self.assertEqual(status, "fail")
        self.assertIn("__future__", detail)
        self.assertIn("SyntaxError", detail)

    def test_good_sh_passes(self):
        p = self.write("good.sh", GOOD_SH)
        self.assertEqual(check_file(p), ("ok", "bash -n"))

    def test_broken_sh_fails(self):
        p = self.write("bad.sh", BROKEN_SH)
        status, detail = check_file(p)
        self.assertEqual(status, "fail")
        # stderr text is locale-dependent (es/en); only assert it is present
        self.assertTrue(detail, "bash -n failure must carry an error detail")

    def test_non_code_skipped(self):
        for name in ("README.md", "COMMIT_MSG", "cfg.json", "tick.yaml"):
            p = self.write(name, "whatever\n")
            self.assertEqual(check_file(p), ("ok", "not-code"), name)

    def test_missing_file_skipped(self):
        """Deleted path (git status keeps it) has nothing to validate."""
        self.assertEqual(
            check_file(os.path.join(self._tmp.name, "gone.py")), ("ok", "missing")
        )

    def test_generated_dirs_skipped(self):
        for rel in ("__pycache__/x.py", ".worktrees/wt/x.py"):
            p = self.write(rel, GOOD_PY)
            self.assertEqual(check_file(p), ("ok", "generated-dir"), rel)

    def test_no_pyc_pollution(self):
        """Gate must not drop .pyc files next to the source (temp cfile)."""
        p = self.write("clean.py", GOOD_PY)
        check_file(p)
        leftovers = [f for f in os.listdir(self._tmp.name) if f.endswith(".pyc")]
        self.assertEqual(leftovers, [])


class TestCheckFiles(_TempFilesTestCase):
    """Batch verdicts: one broken file fails the whole batch."""

    def test_all_good_passes(self):
        paths = [self.write("a.py", GOOD_PY), self.write("b.sh", GOOD_SH)]
        all_ok, failures = check_files(paths)
        self.assertTrue(all_ok)
        self.assertEqual(failures, [])

    def test_one_broken_fails_batch(self):
        good = self.write("good.py", GOOD_PY)
        broken = self.write("broken.py", BROKEN_PY)
        all_ok, failures = check_files([good, broken])
        self.assertFalse(all_ok)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["path"], broken)
        self.assertEqual(failures[0]["check"], "py_compile")
        self.assertIn("__future__", failures[0]["error"])

    def test_failure_order_follows_input(self):
        b1 = self.write("b1.py", BROKEN_PY)
        b2 = self.write("b2.py", BROKEN_PY)
        _, failures = check_files([b1, b2])
        self.assertEqual([f["path"] for f in failures], [b1, b2])


class TestTouchedFiles(unittest.TestCase):
    """git status --porcelain parsing (mocked subprocess)."""

    def _run_mock(self, stdout, returncode=0):
        return mock.patch(
            "subprocess.run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout),
        )

    def test_parses_status_rename_worktree_delete(self):
        stdout = (
            " M scripts/a.py\n"
            "?? new.py\n"
            " D old.py\n"
            "R  old.py -> renamed.py\n"
            "?? .worktrees/wt/x.py\n"
        )
        with self._run_mock(stdout):
            self.assertEqual(
                touched_files(),
                ["scripts/a.py", "new.py", "old.py", "renamed.py"],
            )

    def test_git_failure_returns_empty(self):
        with self._run_mock("", returncode=128):
            self.assertEqual(touched_files(), [])

    def test_timeout_returns_empty(self):
        with mock.patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("git", 30)):
            self.assertEqual(touched_files(), [])


class TestAlert(_TempFilesTestCase):
    """Blocked-commit alert content."""

    def test_lists_every_broken_file(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            alert([
                {"path": "a.py", "check": "py_compile",
                 "error": 'File "a.py", line 8\nSyntaxError: from __future__ ...'},
                {"path": "t.sh", "check": "bash -n", "error": "line 3: unexpected EOF"},
            ])
        out = buf.getvalue()
        self.assertIn("SYNC_GATE: BLOCKED — 2 file(s)", out)
        self.assertIn("BROKEN: a.py [py_compile] File \"a.py\", line 8", out)
        self.assertIn("BROKEN: t.sh [bash -n] line 3: unexpected EOF", out)
        # multi-line error collapses to its first line
        self.assertNotIn("SyntaxError: from __future__ ...", out)


class TestMainCLI(_TempFilesTestCase):
    """End-to-end exit codes, including the regression scenario."""

    def test_passes_good_files(self):
        p1 = self.write("a.py", GOOD_PY)
        p2 = self.write("b.sh", GOOD_SH)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([p1, p2])
        self.assertEqual(rc, 0)
        self.assertIn("all checked files pass", buf.getvalue())

    def test_regression_broken_wip_cannot_pass(self):
        """t_cec89d77 acceptance: WIP with SyntaxError fails the sync gate."""
        p = self.write("wip.py", BROKEN_PY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([p])
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("SYNC_GATE: BLOCKED", out)
        self.assertIn("BROKEN:", out)
        self.assertIn("no commit, no propagation", out)

    def test_quiet_silent_on_success(self):
        p = self.write("a.py", GOOD_PY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--quiet", p])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_quiet_still_alerts_on_failure(self):
        p = self.write("wip.py", BROKEN_PY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--quiet", p])
        self.assertEqual(rc, 1)
        self.assertIn("SYNC_GATE: BLOCKED", buf.getvalue())

    def test_no_paths_clean_tree_passes(self):
        buf = io.StringIO()
        with mock.patch.object(_mod, "touched_files", return_value=[]):
            with contextlib.redirect_stdout(buf):
                rc = main([])
        self.assertEqual(rc, 0)
        self.assertIn("no files to validate", buf.getvalue())

    def test_git_status_mode_gates_touched_files(self):
        """Default mode validates git-status paths and blocks on a broken one."""
        broken = self.write("wip.py", BROKEN_PY)
        with mock.patch.object(_mod, "touched_files", return_value=[broken]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([])
        self.assertEqual(rc, 1)
        self.assertIn("SYNC_GATE: BLOCKED", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
