#!/usr/bin/env python3
"""test_triage_bridge.py — tests for OBJ-21 scripts/triage-bridge.py.

Deterministic triage->todo bridge: no LLM, header-tag gating, human-approval
filter (lesson t_9b508127 / SWEEP-FIX), 1 promotion per tick, JSONL ledger
idempotence, PROMOTE-STOP kill switch, dry-run by default.

Cases (per task acceptance criteria):
  - body con tags validas en header            -> would-promote / promote
  - sin cost:                                  -> kept-in-triage
  - cost invalido (large)                      -> kept-in-triage
  - kill switch / aprobacion humana            -> kept-in-triage (NUNCA promo)
  - ya promocionada (ledger)                   -> kept-in-triage (no duplicate)
  - cap 1/tick                                 -> segunda elegible cap-reached
  - PROMOTE-STOP activo                        -> skipped
  - dry-run no muta el board; --execute si     -> status todo + evento specified
  - mención de "aprobación" en el cuerpo (no header) NO bloquea

The execute path calls hermes core specify_triage_task(); the "full" test uses
the real core against a temp DB built with the core's own schema so the
auditable 'specified' event is verified end-to-end. If the core import fails
in an exotic environment, that single test skips.

Run:
  /usr/bin/python3.12 -m pytest test_triage_bridge.py -x
  /usr/bin/python3.12 test_triage_bridge.py            (unittest fallback)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "triage_bridge",
    os.path.join(SCRIPT_DIR, "scripts", "triage-bridge.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys_hack = __import__("sys")
sys_hack.modules["triage_bridge"] = _mod
_spec.loader.exec_module(_mod)

triage_bridge = _mod

# ── Fixtures ─────────────────────────────────────────────────────────────────

GOOD_BODY = """objective:OBJ-19, auto_created:true, cost:small, model:fast

## Objetivo
Documentar la matriz de routing por privacidad. Su version final marcara
el documento como DRAFT pendiente de aprobacion del usuario al entregarlo.
"""

NO_COST_BODY = """objective:OBJ-20, auto_created:true, model:fast

## Objetivo
Algo sin tag de coste.
"""

BAD_COST_BODY = """objective:OBJ-21, auto_created:true, cost:large, model:fast

## Objetivo
Coste fuera del vocabulario permitido.
"""

KILL_SWITCH_TITLE = "OBJ-23: Auto-promocion — kill switch activo hasta autorizacion del usuario"

APPROVAL_TITLE = "MULTI-PROV-10.4: Construir matriz y obtener aprobacion del usuario"

SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT,
  assignee TEXT,
  status TEXT,
  body TEXT,
  created_at INTEGER
);
"""


def make_db(tasks):
    """Create a temp kanban.db. tasks: list of {id,title,body,status}."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id,title,assignee,status,body,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (t["id"], t.get("title", "t"), t.get("assignee", "pr-test"),
             t.get("status", "triage"), t.get("body", ""), t.get("created_at", 1)),
        )
    conn.commit()
    conn.close()
    return path


class BridgeTestCase(unittest.TestCase):
    """Isolates LEDGER and KILL_SWITCH into a temp dir per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_ledger = triage_bridge.LEDGER
        self._orig_stop = triage_bridge.KILL_SWITCH
        triage_bridge.LEDGER = Path(self.tmp) / "triage-bridge.jsonl"
        triage_bridge.KILL_SWITCH = Path(self.tmp) / "PROMOTE-STOP"

    def tearDown(self):
        triage_bridge.LEDGER = self._orig_ledger
        triage_bridge.KILL_SWITCH = self._orig_stop

    def actions(self, decisions):
        return [(d.get("task"), d["action"]) for d in decisions]

    def by_action(self, decisions, action):
        return [d for d in decisions if d["action"] == action]


# ── Tag parsing ──────────────────────────────────────────────────────────────

class TestTagParsing(BridgeTestCase):
    def test_header_tags_extracted(self):
        obj, cost = triage_bridge.parse_tags(GOOD_BODY)
        self.assertEqual(obj, "OBJ-19")
        self.assertEqual(cost, "small")

    def test_prose_cost_mention_not_a_tag(self):
        # cost: appearing in body prose (after the blank line) must NOT count.
        body = "objective:OBJ-1, auto_created:true\n\nBlabla cost:small en el cuerpo.\n"
        obj, cost = triage_bridge.parse_tags(body)
        self.assertEqual(obj, "OBJ-1")
        self.assertIsNone(cost)

    def test_pipe_separated_header(self):
        body = "objective:OBJ-21 | cost:small | model:worker\n\nCuerpo.\n"
        obj, cost = triage_bridge.parse_tags(body)
        self.assertEqual(obj, "OBJ-21")
        self.assertEqual(cost, "small")


class TestEvaluate(BridgeTestCase):
    def test_valid_tags_promotable(self):
        ok, reason = triage_bridge.evaluate("OBJ-19: Docs", GOOD_BODY)
        self.assertTrue(ok, reason)

    def test_missing_cost_not_promotable(self):
        ok, _ = triage_bridge.evaluate("OBJ-20: Algo", NO_COST_BODY)
        self.assertFalse(ok)

    def test_invalid_cost_not_promotable(self):
        ok, reason = triage_bridge.evaluate("OBJ-21: Algo", BAD_COST_BODY)
        self.assertFalse(ok)
        self.assertIn("no valido", reason)

    def test_kill_switch_in_title_blocks(self):
        ok, reason = triage_bridge.evaluate(KILL_SWITCH_TITLE, GOOD_BODY)
        self.assertFalse(ok)
        self.assertIn("aprobacion humana", reason)

    def test_approval_gate_in_title_blocks(self):
        ok, _ = triage_bridge.evaluate(APPROVAL_TITLE, GOOD_BODY)
        self.assertFalse(ok)

    def test_header_gate_phrase_blocks(self):
        body = ("objective:OBJ-9, auto_created:true, cost:small\n"
                "gate: puerta de aprobacion humana\n\nCuerpo.\n")
        ok, _ = triage_bridge.evaluate("OBJ-9: Algo", body)
        self.assertFalse(ok)

    def test_body_approval_mention_does_not_block(self):
        # GOOD_BODY says "pendiente de aprobacion" in prose (after blank line):
        # that is a deliverable step, not a gate — must stay promotable.
        ok, reason = triage_bridge.evaluate("OBJ-19: Docs", GOOD_BODY)
        self.assertTrue(ok, reason)


# ── Bridge loop: dry-run / execute / cap / idempotence ──────────────────────

class TestDryRun(BridgeTestCase):
    def test_dry_run_would_promote_and_writes_no_ledger(self):
        db = make_db([{"id": "t_ok", "title": "OBJ-19: Docs", "body": GOOD_BODY}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.actions(decisions) == [("t_ok", "would-promote")]
        self.assertEqual(len(self.by_action(decisions, "would-promote")), 1)
        self.assertFalse(triage_bridge.LEDGER.exists())
        # board untouched
        row = sqlite3.connect(db).execute(
            "SELECT status FROM tasks WHERE id='t_ok'").fetchone()
        self.assertEqual(row[0], "triage")

    def test_no_tags_kept(self):
        body = "Sin tags de cabecera aqui.\n\nSolo prosa.\n"
        db = make_db([{"id": "t_no", "title": "feature random", "body": body}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(self.by_action(decisions, "kept-in-triage")[0]["task"], "t_no")

    def test_kill_switch_task_never_promoted(self):
        db = make_db([{"id": "t_ks", "title": KILL_SWITCH_TITLE, "body": GOOD_BODY}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(self.by_action(decisions, "would-promote"), [])
        self.assertEqual(len(self.by_action(decisions, "kept-in-triage")), 1)

    def test_cap_one_per_tick(self):
        db = make_db([
            {"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY, "created_at": 1},
            {"id": "t_b", "title": "OBJ-2: B", "body": GOOD_BODY, "created_at": 2},
        ])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(len(self.by_action(decisions, "would-promote")), 1)
        self.assertEqual(self.by_action(decisions, "would-promote")[0]["task"], "t_a")
        self.assertEqual(self.by_action(decisions, "cap-reached")[0]["task"], "t_b")

    def test_already_promoted_in_ledger_not_duplicated(self):
        triage_bridge.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        triage_bridge.LEDGER.write_text(json.dumps(
            {"ts": "x", "task": "t_a", "action": "promoted"}) + "\n")
        db = make_db([{"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(self.by_action(decisions, "would-promote"), [])
        kept = self.by_action(decisions, "kept-in-triage")
        self.assertIn("ya promocionada", kept[0]["reason"])

    def test_non_triage_rows_ignored(self):
        db = make_db([{"id": "t_done", "title": "OBJ-1", "body": GOOD_BODY,
                       "status": "done"}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(decisions, [])

    def test_kill_switch_file_skips_everything(self):
        triage_bridge.KILL_SWITCH.touch()
        db = make_db([{"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY}])
        decisions = triage_bridge.run(db_path=Path(db), execute=False)
        self.assertEqual(decisions[0]["action"], "skipped")
        self.assertNotIn("task", decisions[0])


class TestExecute(BridgeTestCase):
    """--execute path with the core promotion call stubbed, plus a real-core
    integration test when the hermes source tree is importable."""

    def test_execute_promotes_and_logs(self):
        calls = []
        orig = triage_bridge.promote_via_core
        triage_bridge.promote_via_core = (
            lambda db_path, task_id: calls.append(task_id) or True)
        try:
            db = make_db([
                {"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY, "created_at": 1},
                {"id": "t_b", "title": "OBJ-2: B", "body": GOOD_BODY, "created_at": 2},
            ])
            decisions = triage_bridge.run(db_path=Path(db), execute=True)
        finally:
            triage_bridge.promote_via_core = orig
        self.assertEqual(calls, ["t_a"])  # cap 1/tick
        self.assertEqual(len(self.by_action(decisions, "promoted")), 1)
        self.assertEqual(len(self.by_action(decisions, "cap-reached")), 1)
        ledger = [json.loads(l) for l in
                  triage_bridge.LEDGER.read_text().splitlines()]
        self.assertEqual(ledger[0]["action"], "promoted")
        self.assertEqual(ledger[0]["task"], "t_a")

    def test_execute_core_rejection_recorded(self):
        orig = triage_bridge.promote_via_core
        triage_bridge.promote_via_core = lambda db_path, task_id: False
        try:
            db = make_db([{"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY}])
            decisions = triage_bridge.run(db_path=Path(db), execute=True)
        finally:
            triage_bridge.promote_via_core = orig
        self.assertEqual(len(self.by_action(decisions, "specify-rejected")), 1)

    def test_execute_with_real_core_writes_specified_event(self):
        """End-to-end against hermes core specify_triage_task on a temp DB
        built with the core's own schema, verifying the auditable event.

        The core's cooperative write fence (HERMES_DELEGATED_CHILD_CONTEXT)
        is set for kanban workers but NOT for the production cron context
        (gateway-spawned) — drop the marker for the test and restore it."""
        if triage_bridge.HERMES_SRC not in sys_hack.path:
            sys_hack.path.insert(0, triage_bridge.HERMES_SRC)
        try:
            from hermes_cli import kanban_db as kdb
        except Exception:
            self.skipTest("fallo importando el nucleo hermes; entorno exotico")
        marker = "HERMES_DELEGATED_CHILD_CONTEXT"
        saved = os.environ.pop(marker, None)
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(kdb.SCHEMA_SQL)
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,body,created_at)"
                " VALUES ('t_live','OBJ-19: Docs','pr-test','triage',?,1)",
                (GOOD_BODY,))
            conn.commit()
            ok = triage_bridge.promote_via_core(Path(db), "t_live")
            self.assertTrue(ok)
            row = conn.execute(
                "SELECT status FROM tasks WHERE id='t_live'").fetchone()
            # specify_triage_task lands in todo; the core then flips a
            # parent-free task straight to ready in its own txn (by design).
            self.assertIn(row["status"], ("todo", "ready"))
            ev = conn.execute(
                "SELECT kind FROM task_events WHERE task_id='t_live'"
                " AND kind='specified'").fetchall()
            self.assertEqual(len(ev), 1)
            conn.close()
        finally:
            if saved is not None:
                os.environ[marker] = saved
            if os.path.exists(db):
                os.unlink(db)


# ── Ledger format ────────────────────────────────────────────────────────────

class TestLedger(BridgeTestCase):
    def test_ledger_is_one_json_per_line(self):
        db = make_db([{"id": "t_a", "title": "OBJ-1: A", "body": GOOD_BODY}])
        orig = triage_bridge.promote_via_core
        triage_bridge.promote_via_core = lambda db_path, task_id: True
        try:
            triage_bridge.run(db_path=Path(db), execute=True)
        finally:
            triage_bridge.promote_via_core = orig
        lines = triage_bridge.LEDGER.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        e = json.loads(lines[0])
        self.assertEqual(e["action"], "promoted")
        self.assertIn("ts", e)


if __name__ == "__main__":
    unittest.main(verbosity=2)
