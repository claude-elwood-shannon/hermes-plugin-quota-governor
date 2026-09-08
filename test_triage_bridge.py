#!/usr/bin/env python3
"""Tests for triage-bridge.py — OBJ-21: automatic triage->todo promotion.

triage-bridge promotes triage tasks to todo when their body already carries
the standard ledger header (objective: and cost: present) WITHOUT an LLM.
It delegates the actual promotion to the hermes core's
specify_triage_task(), which enforces the human-approval gate check.

The SWEEP-FIX (t_9b508127, Sep 7) fixed a bug where triage-bridge
re-promoted tasks with a pending human approval gate, causing an infinite
re-dispatch loop. Those human-gate decisions live in the core's
specify_triage_task(); this suite exercises the bridge's own logic and
regression-guards the "not re-promoted" behaviour by mocking that core
call to reject, then asserting the task is NOT promoted.

Tests cover:
  - Candidate filtering (objective: + cost: in body)
  - Basic promotion: triage task with both tags -> specify_triage_task called
  - Gate rejection (SWEEP-FIX regression): specify_triage_task returns False
    -> task NOT promoted, 'specify-rejected' logged
  - Missing tags: task without objective or cost -> NOT a candidate
  - Idempotency: already-promoted (non-triage) task -> never re-selected
  - Kill switch: PROMOTE-STOP present -> skipped
  - Missing DB -> silent exit
  - MAX_PER_TICK cap: at most 3 promotions per tick

All tests use temp dirs and a mocked `hermes_cli.kanban_db.specify_triage_task`
— no real DB, no live board state, no writes outside the sandbox.

Run:
  /usr/bin/python3.12 -m pytest test_triage_bridge.py -v
  /usr/bin/python3.12 test_triage_bridge.py  # direct run
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with filename-based module (triage-bridge.py -> triage_bridge)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "triage_bridge",
    os.path.join(SCRIPT_DIR, "scripts", "triage-bridge.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["triage_bridge"] = _mod
_spec.loader.exec_module(_mod)

from triage_bridge import main  # noqa: E402

MAX_PER_TICK = _mod.MAX_PER_TICK


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given triage/relevant tasks.

    Each task is a dict: {id, title, body, status, created_at}.
    Mirrors the schema columns triage-bridge.py actually reads.
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
            created_at INTEGER
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                t.get("id", "t_test"),
                t.get("title", "Test task"),
                t.get("body", ""),
                t.get("status", "triage"),
                t.get("created_at", 0),
            ),
        )
    conn.commit()
    conn.close()
    return path


def _full_body(title: str = "OBJ-X: Some objective") -> str:
    """A body that carries the standard objective: + cost: ledger header."""
    return f"{title}\nobjective:OBJ-X\ncost:tiny\n\nSome details."


class _BridgeIsolationMixin(unittest.TestCase):
    """TestCase base that redirects every global path to a temp sandbox.

    triage-bridge.py touches real state through module globals:
      KANBAN_DB   -> the live ~/.hermes/kanban.db
      LEDGER      -> ~/.hermes/quota-governor/triage-bridge-ledger.jsonl
      KILL_SWITCH -> ~/.hermes/quota-governor/PROMOTE-STOP
      HERMES_SRC  -> where main() imports hermes_cli from

    Tests never touch real state, so every one of these is redirected to
    a temp dir and HERMES_SRC points at an empty sandbox (the actual
    `hermes_cli.kanban_db` module is injected into sys.modules instead).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._state_dir = self._tmp_path / "quota-governor"
        self._state_dir.mkdir(exist_ok=True)
        self._patchers = []
        # The script's globals are Path objects (HERMES_HOME / ...), so the
        # patch replacements must be Path instances too.
        self._patchers.append(patch.object(_mod, "KANBAN_DB",
                              self._tmp_path / "kanban.db"))
        self._patchers.append(patch.object(_mod, "LEDGER",
                              self._state_dir / "triage-bridge-ledger.jsonl"))
        self._patchers.append(patch.object(_mod, "KILL_SWITCH",
                              self._state_dir / "PROMOTE-STOP"))
        self._patchers.append(patch.object(_mod, "HERMES_SRC",
                              self._tmp_path / "hermes-src"))
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _fake_core_specify(self, return_value):
        """Inject a fake hermes_cli.kanban_db.specify_triage_task.

        main() builds candidates, then does
            sys.path.insert(0, HERMES_SRC)
            from hermes_cli.kanban_db import specify_triage_task
        If the module is already in sys.modules the from-import resolves
        there without touching the (empty) HERMES_SRC sandbox. Returns the
        MagicMock so tests can assert call args.
        """
        kb = types.ModuleType("hermes_cli.kanban_db")
        kb.specify_triage_task = MagicMock(return_value=return_value)
        parent = types.ModuleType("hermes_cli")
        parent.kanban_db = kb
        sys.modules["hermes_cli"] = parent
        sys.modules["hermes_cli.kanban_db"] = kb
        self.addCleanup(lambda: sys.modules.pop("hermes_cli", None))
        self.addCleanup(lambda: sys.modules.pop("hermes_cli.kanban_db", None))
        return kb.specify_triage_task

    def _ledger_lines(self):
        """Read the (patched) ledger file; empty list if absent."""
        path = _mod.LEDGER
        if not os.path.isfile(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]


# ── Tests: candidate filtering & basic promotion ────────────────────────────

class TestBasicPromotion(_BridgeIsolationMixin):
    """Task with objective: + cost: tags in triage -> promoted to todo."""

    def test_promotes_triage_task_with_both_tags(self):
        db = _make_kanban_db([{
            "id": "t_prop",
            "title": "OBJ-21: Promote me",
            "body": _full_body("OBJ-21: Promote me"),
            "status": "triage",
        }])
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(True)
            try:
                main()
            finally:
                os.unlink(db)

        specify.assert_called_once()
        args = specify.call_args
        # args[0] is the fresh write-connection main() opened; args[1] is
        # the task id being promoted, and author is passed by keyword.
        self.assertEqual(args.args[1], "t_prop")
        self.assertEqual(args.kwargs.get("author"), "triage-bridge")

        actions = [e["action"] for e in self._ledger_lines()]
        self.assertIn("promoted", actions)

    def test_candidate_requiring_gate_is_not_promoted(self):
        """Task whose body carries the ledger header BUT whose promotion is
        rejected by the core's human-gate check is NOT promoted.

        SWEEP-FIX regression (t_9b508127): specify_triage_task rejects the
        task (e.g. block_loop needs_input / 'obtener aprobacion del usuario'
        pending). The bridge must NOT force it to todo — it records
        'specify-rejected' and moves on.
        """
        db = _make_kanban_db([{
            "id": "t_gated",
            "title": "OBJ-21: Needs human approval",
            "body": _full_body("OBJ-21: Needs human approval")
                + "\nobtener aprobacion del usuario antes de proceder",
            "status": "triage",
        }])
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(False)  # gate blocks
            try:
                main()
            finally:
                os.unlink(db)

        specify.assert_called_once()
        lines = self._ledger_lines()
        self.assertEqual(
            [e["action"] for e in lines],
            ["specify-rejected"],
            "Human-gated task must be logged as rejected, never promoted",
        )
        for e in lines:
            self.assertNotEqual(e["action"], "promoted")

    def test_missing_tags_not_candidate(self):
        """Task without objective: or cost: is NOT promoted (and not even
        handed to specify_triage_task)."""
        db = _make_kanban_db([
            {"id": "t_no_cost", "title": "No cost", "body": "objective:OBJ-X",
             "status": "triage"},
            {"id": "t_no_obj", "title": "No objective", "body": "cost:tiny",
             "status": "triage"},
            {"id": "t_neither", "title": "Neither", "body": "just text",
             "status": "triage"},
        ])
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(True)
            try:
                main()
            finally:
                os.unlink(db)

        specify.assert_not_called()
        self.assertEqual(self._ledger_lines(), [])

    def test_non_triage_task_not_selected(self):
        """Already-promoted task (status != triage) is never re-promoted.

        Idempotency: the SELECT only reads status='triage', so a task that
        already moved to todo/running/done is out of scope for this tick.
        """
        db = _make_kanban_db([{
            "id": "t_todo",
            "title": "Already promoted",
            "body": _full_body(),
            "status": "todo",  # not triage -> excluded by SELECT
        }])
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(True)
            try:
                main()
            finally:
                os.unlink(db)

        specify.assert_not_called()
        self.assertEqual(self._ledger_lines(), [])


# ── Tests: kill switch / missing DB / cap ───────────────────────────────────

class TestGuardrails(_BridgeIsolationMixin):
    """Kill switch, missing DB, and per-tick flood cap."""

    def test_kill_switch_skips(self):
        """PROMOTE-STOP present -> skipped, no promotion attempted."""
        open(_mod.KILL_SWITCH, "w").close()
        db = _make_kanban_db([{
            "id": "t_prom",
            "title": "Would promote",
            "body": _full_body(),
            "status": "triage",
        }])
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(True)
            try:
                main()
            finally:
                os.unlink(db)

        specify.assert_not_called()
        lines = self._ledger_lines()
        self.assertEqual(lines[0]["reason"], "PROMOTE-STOP activo")
        self.assertEqual(lines[0]["action"], "skipped")

    def test_missing_db_silent_exit(self):
        """KANBAN_DB absent -> main returns without touching the ledger."""
        # KANBAN_DB already points at a nonexistent path in the sandbox.
        specify = self._fake_core_specify(True)
        main()
        specify.assert_not_called()
        self.assertEqual(self._ledger_lines(), [])

    def test_max_per_tick_cap(self):
        """More than MAX_PER_TICK candidates -> only MAX_PER_TICK promoted."""
        tasks = [{
            "id": f"t_{i}",
            "title": f"Task {i}",
            "body": _full_body(f"Task {i}"),
            "status": "triage",
        } for i in range(MAX_PER_TICK + 2)]
        db = _make_kanban_db(tasks)
        with patch.object(_mod, "KANBAN_DB", Path(db)):
            specify = self._fake_core_specify(True)
            try:
                main()
            finally:
                os.unlink(db)

        self.assertEqual(specify.call_count, MAX_PER_TICK,
                         "Must not promote more than MAX_PER_TICK per tick")
        promoted = [e for e in self._ledger_lines() if e["action"] == "promoted"]
        self.assertEqual(len(promoted), MAX_PER_TICK)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
