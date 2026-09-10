#!/usr/bin/python3.12
"""test_tick_cola_viva.py — OBJ-30b-IMPL: mecanismo cola viva (constitucion del vuelo).

Covers (fixtures only, no network, no real kanban.db, no real CLI):
  1. STOP signal active -> skipped, no action
  2. live_workers > 0 -> skipped (board busy)
  3. session >= 80% -> skipped (quota gate)
  4. weekly >= 80% -> skipped (quota gate)
  5. ready task with no assignee -> assigned a profile (step 1)
  6. ready tasks with assignee -> queue-alive, no new work (step 2)
  7. no ready -> structural class-C successor of most recent done (step 3)
  8. successor idempotency: parent with an open successor is skipped
  9. nothing legitimate -> cola seca legitima, no filler (step 4)
  10. max 1 action per tick
  11. privacy: no absolute host paths in the repo module (portability)

The mutation functions (create_task / assign_task) are monkeypatched to
record calls instead of shelling out — the fixture board is a temp sqlite
DB, and the ledger lives under a temp HERMES_HOME.

Run:  /usr/bin/python3.12 test_tick_cola_viva.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent)
SCRIPT = os.path.join(GOV_DIR, "scripts", "tick-cola-viva.py")

_spec = importlib.util.spec_from_file_location("tick_cola_viva", SCRIPT)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

NOW = 1789001175.0  # fixed epoch: deterministic, never wall-clock

CLASE_C_DONE = (
    "objective:OBJ-35 | cost:small | privacy:low | clase:C\n"
    "Sucesor estructural: backfill de la tabla de entrenamiento."
)


def _mk_db(path, tasks, done=None):
    """Create a minimal tasks table. tasks = list of dicts (id,title,status,
    assignee,body). done is a convenience list merged into tasks as done."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "assignee TEXT, body TEXT, completed_at REAL, created_at REAL)")
    all_tasks = list(tasks)
    for d in (done or []):
        all_tasks.append({**d, "status": "done"})
    for t in all_tasks:
        con.execute(
            "INSERT INTO tasks (id, title, status, assignee, body, completed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["title"], t["status"], t.get("assignee"),
             t.get("body"), t.get("completed_at"), t.get("created_at", 0)))
    con.commit()
    con.close()


def _ready(id="t_ready", assignee=None, title="OBJ-27: tarea lista"):
    return {"id": id, "title": title, "status": "ready",
            "assignee": assignee, "body": "objective:OBJ-27"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="colaviva-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        self.addCleanup(os.environ.pop, "HERMES_KANBAN_DB", None)
        os.environ.pop("HERMES_KANBAN_DB", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.db = str(Path(self.tmp) / "kanban.db")
        # Record mutations instead of shelling out.
        self.calls = {"create": [], "assign": []}
        cv.create_task = self._fake_create
        cv.assign_task = self._fake_assign

    def _fake_create(self, title, body, assignee):
        self.calls["create"].append((title, body, assignee))
        return f"t_new_{len(self.calls['create'])}"

    def _fake_assign(self, task_id, assignee):
        self.calls["assign"].append((task_id, assignee))
        return True

    def home(self):
        return self.tmp

    def _ledger(self):
        p = cv.ledger_path(self.home())
        try:
            return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except OSError:
            return []

    def _run(self, **kw):
        kw.setdefault("hermes_home", self.home())
        kw.setdefault("execute", True)
        kw.setdefault("now", NOW)
        kw.setdefault("session_pct", 2.6)
        kw.setdefault("weekly_pct", 54.7)
        kw.setdefault("live_workers", 0)
        return cv.run(**kw)


class TestStopSignal(Base):
    def test_stop_signal_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        cv.stop_file_path(self.home()).parent.mkdir(parents=True, exist_ok=True)
        cv.stop_file_path(self.home()).write_text("spending limit")
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("STOP", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])


class TestQuotaGates(Base):
    def test_live_workers_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(live_workers=1)
        self.assertEqual(len(out), 1)
        self.assertIn("en vuelo", out[0])
        self.assertEqual(self.calls["assign"], [])

    def test_session_high_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(session_pct=85.0)
        self.assertEqual(len(out), 1)
        self.assertIn("sesion al 85.0%", out[0])
        self.assertEqual(self.calls["assign"], [])

    def test_weekly_high_skips(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(weekly_pct=90.0)
        self.assertEqual(len(out), 1)
        self.assertIn("weekly al 90.0%", out[0])
        self.assertEqual(self.calls["assign"], [])


class TestCascade(Base):
    def test_step1_assigns_ready_without_assignee(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("asignado t_ready", out[0])
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])
        self.assertEqual(self.calls["create"], [])

    def test_step2_queue_alive_when_ready_assigned(self):
        _mk_db(self.db, [_ready(assignee="pr-ollama")])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("ready con assignee", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])

    def test_step3_structural_successor(self):
        _mk_db(self.db, [],
               done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                      "assignee": "pr-ollama", "body": CLASE_C_DONE,
                      "completed_at": NOW - 3600}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("sucesor estructural de t_done", out[0])
        self.assertEqual(len(self.calls["create"]), 1)
        title, body, assignee = self.calls["create"][0]
        self.assertIn("t_done", title)
        self.assertIn("clase:C", body)
        self.assertEqual(assignee, "pr-ollama")

    def test_step3_skips_parent_with_open_successor(self):
        # t_done already has an open successor referencing it -> skipped.
        _mk_db(self.db, [
            {"id": "t_succ", "title": "Sucesor estructural de t_done",
             "status": "ready", "assignee": "pr-ollama",
             "body": "Sucesor estructural de t_done (ya abierto)"}],
            done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                   "assignee": "pr-ollama", "body": CLASE_C_DONE,
                   "completed_at": NOW - 3600}])
        out = self._run()
        # t_succ is ready WITH assignee -> step2 queue-alive fires first.
        self.assertEqual(len(out), 1)
        self.assertIn("ready con assignee", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_step3_skips_parent_with_open_successor_no_ready(self):
        # t_done has an open successor in 'todo' (not ready) -> step3 must
        # skip it (idempotency) and fall to step4.
        _mk_db(self.db, [
            {"id": "t_succ", "title": "Sucesor estructural de t_done",
             "status": "todo", "assignee": "pr-ollama",
             "body": "Sucesor estructural de t_done (ya abierto)"}],
            done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                   "assignee": "pr-ollama", "body": CLASE_C_DONE,
                   "completed_at": NOW - 3600}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("cola seca legitima", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_step4_stop_when_nothing_legitimate(self):
        _mk_db(self.db, [])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertIn("cola seca legitima", out[0])
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["assign"], [])

    def test_max_one_action_per_tick(self):
        # ready-without-assignee AND a done task: only step1 fires.
        _mk_db(self.db, [_ready(assignee=None)],
               done=[{"id": "t_done", "title": "OBJ-35 backfill predictor",
                      "assignee": "pr-ollama", "body": CLASE_C_DONE,
                      "completed_at": NOW - 3600}])
        out = self._run()
        self.assertEqual(len(out), 1)
        self.assertEqual(self.calls["assign"], [("t_ready", "pr-ollama")])
        self.assertEqual(self.calls["create"], [])


class TestDryRun(Base):
    def test_dry_run_prints_but_does_not_mutate(self):
        _mk_db(self.db, [_ready(assignee=None)])
        out = self._run(execute=False)
        self.assertEqual(len(out), 1)
        self.assertIn("DRY:", out[0])
        self.assertEqual(self.calls["assign"], [])
        self.assertEqual(self.calls["create"], [])


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data", Path.home().name):
            self.assertNotIn(needle, src,
                             f"host path leaked into tick-cola-viva.py: {needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
