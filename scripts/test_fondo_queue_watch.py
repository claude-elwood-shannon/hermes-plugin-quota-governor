#!/usr/bin/python3.12
"""test_fondo_queue_watch.py — OBJ-30b: anti-parada mechanism during budget windows.

Covers (fixtures only, no network, no real kanban.db, no real CLI):
  1. no window active -> silent (no action, no state)
  2. window active + queue non-empty -> silent, resets empty-since
  3. window active + queue empty < grace -> silent, starts the clock
  4. window active + queue empty > grace -> cascade fires
  5. cascade order: step1 (next phase) -> step2 (structural successor) ->
     step3 (assign triage) -> step4 (annotate + STOP)
  6. step1 only fires when the window body lists a Cartera/Fases phase
  7. step2 only fires when there is a recently closed task
  8. step3 only fires when there is a ready-able class-C triage task
  9. step4 (STOP) fires when nothing legitimate exists — and never creates
     filler
  10. max 1 action per tick
  11. privacy: no absolute host paths in the repo module (portability)

The mutation functions (create_task / assign_task / comment_task) are
monkeypatched to record calls instead of shelling out — the fixture board is
a temp sqlite DB, and the state/ledger live under a temp HERMES_HOME.

Run:  /usr/bin/python3.12 test_fondo_queue_watch.py  (or pytest)
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

GOV_DIR = "REPO"
SCRIPT = os.path.join(GOV_DIR, "scripts", "fondo-queue-watch.py")

_spec = importlib.util.spec_from_file_location("fondo_queue_watch", SCRIPT)
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)

NOW = 1789001175.0  # fixed epoch: deterministic, never wall-clock

WINDOW_BODY = (
    "objective:OBJ-30 | cost:small | privacy:low | clase:A CON APROBACION\n"
    "## Contrato de la ventana (vinculante)\n"
    "PRESUPUESTO: 3.00 USD ...\n"
    "## Cartera de la ventana\n"
    "1. OBJ-27 F1 — INFORME DE LA MANANA\n"
    "2. Sucesores estructurales de F0\n"
    "3. OBJ-29 supply_ratio en metrics-history\n"
)

# Window body WITHOUT a Cartera section — forces the cascade past step1.
WINDOW_BODY_NO_CARTERA = (
    "objective:OBJ-30 | cost:small | privacy:low | clase:A CON APROBACION\n"
    "## Contrato de la ventana (vinculante)\n"
    "PRESUPUESTO: 3.00 USD ...\n"
    "## Mandato\n"
    "Liderar e innovar en ausencia del usuario.\n"
)


def _mk_db(path, tasks, done=None, triage=None):
    """Create a minimal tasks table. tasks = list of dicts (id,title,status,
    assignee,body). done/triage are convenience lists merged into tasks."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "assignee TEXT, body TEXT, completed_at REAL, created_at REAL)")
    all_tasks = list(tasks)
    for d in (done or []):
        all_tasks.append({**d, "status": "done"})
    for t in (triage or []):
        all_tasks.append({**t, "status": "triage"})
    for t in all_tasks:
        con.execute(
            "INSERT INTO tasks (id, title, status, assignee, body, completed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["title"], t["status"], t.get("assignee"),
             t.get("body"), t.get("completed_at"), t.get("created_at", 0)))
    con.commit()
    con.close()


def _window(id="t_win", status="running", assignee="pr-ollama", body=WINDOW_BODY):
    return {"id": id, "title": "OBJ-30a: ventana fondo", "status": status,
            "assignee": assignee, "body": body}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fqw-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.db = str(Path(self.tmp) / "kanban.db")
        # Record mutations instead of shelling out.
        self.calls = {"create": [], "assign": [], "comment": []}
        fw.create_task = self._fake_create
        fw.assign_task = self._fake_assign
        fw.comment_task = self._fake_comment

    def _fake_create(self, title, body, assignee):
        self.calls["create"].append((title, body, assignee))
        return f"t_new_{len(self.calls['create'])}"

    def _fake_assign(self, task_id, assignee):
        self.calls["assign"].append((task_id, assignee))
        return True

    def _fake_comment(self, task_id, body):
        self.calls["comment"].append((task_id, body))
        return True

    def home(self):
        return self.tmp

    def _state(self):
        p = fw.state_file_path(self.home())
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _ledger(self):
        p = fw.ledger_path(self.home())
        try:
            return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except OSError:
            return []


class TestNoWindow(Base):
    def test_no_window_is_silent(self):
        _mk_db(self.db, [{"id": "t_1", "title": "x", "status": "ready",
                          "assignee": "pr-ollama", "body": "objective:OBJ-27"}])
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(out, [])
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["comment"], [])


class TestQueueNonEmpty(Base):
    def test_queue_nonempty_resets_and_stays_silent(self):
        _mk_db(self.db, [_window(), {"id": "t_1", "title": "x", "status": "ready",
                                     "assignee": "pr-ollama", "body": "objective:OBJ-27"}])
        # Pre-seed a stale empty-since to prove it resets.
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 9999, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(out, [])
        self.assertIsNone(self._state().get("empty_since"))
        self.assertEqual(self.calls["create"], [])


class TestQueueEmptyGrace(Base):
    def test_first_empty_tick_starts_clock(self):
        _mk_db(self.db, [_window()])
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(out, [])
        self.assertIsNotNone(self._state().get("empty_since"))
        self.assertEqual(self.calls["create"], [])

    def test_within_grace_stays_silent(self):
        _mk_db(self.db, [_window()])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 600, "window": "t_win"})  # 10 min
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(out, [])
        self.assertEqual(self.calls["create"], [])


class TestCascade(Base):
    def test_step1_promotes_next_phase(self):
        _mk_db(self.db, [_window()])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})  # 60 min
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("promoted-next-phase", out[0])
        self.assertEqual(len(self.calls["create"]), 1)
        title, body, assignee = self.calls["create"][0]
        self.assertIn("OBJ-27 F1", title)
        self.assertEqual(assignee, "pr-ollama")

    def test_step1_skips_phase_already_on_board(self):
        # The phase task exists but is NOT in the ready/running queue (todo),
        # so the queue is empty but step1 finds nothing new to promote.
        single_cartera = (
            "objective:OBJ-30 | cost:small | privacy:low\n"
            "## Contrato de la ventana (vinculante)\n"
            "PRESUPUESTO: 3.00 USD ...\n"
            "## Cartera de la ventana\n"
            "1. OBJ-27 F1 — INFORME DE LA MANANA\n"
        )
        _mk_db(self.db, [_window(body=single_cartera),
                         {"id": "t_f1", "title": "OBJ-27 F1 — INFORME DE LA MANANA",
                          "status": "todo", "assignee": "pr-ollama",
                          "body": "objective:OBJ-27"}])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        # No phase to promote -> falls to step2 (no done) -> step3 (no triage)
        # -> step4 STOP.
        self.assertEqual(len(out), 1)
        self.assertIn("STOP", out[0])
        self.assertEqual(self.calls["create"], [])

    def test_step2_structural_successor(self):
        _mk_db(self.db, [_window(body=WINDOW_BODY_NO_CARTERA)],
               done=[{"id": "t_done", "title": "OBJ-27 F0 trace",
                      "assignee": "pr-ollama", "body": "objective:OBJ-27",
                      "completed_at": NOW - 3600}])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("structural-successor", out[0])
        self.assertEqual(len(self.calls["create"]), 1)
        self.assertIn("Test/hardening: OBJ-27 F0 trace", self.calls["create"][0][0])

    def test_step3_assigns_triage(self):
        _mk_db(self.db, [_window(body=WINDOW_BODY_NO_CARTERA)],
               triage=[{"id": "t_tri", "title": "OBJ-19: Documentar matriz de routing",
                        "assignee": None,
                        "body": "objective:OBJ-19 | cost:small | privacy:low\n"
                                "Documentar matriz de routing por privacidad"}])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("assigned-triage", out[0])
        self.assertEqual(self.calls["assign"], [("t_tri", "pr-ollama")])
        self.assertEqual(self.calls["create"], [])

    def test_step4_stop_when_nothing_legitimate(self):
        _mk_db(self.db, [_window(body=WINDOW_BODY_NO_CARTERA)])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("STOP", out[0])
        self.assertIn("cola seca", out[0])
        # No filler created.
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["assign"], [])
        # The window task is annotated.
        self.assertEqual(len(self.calls["comment"]), 1)
        self.assertEqual(self.calls["comment"][0][0], "t_win")

    def test_max_one_action_per_tick(self):
        # Window with a phase AND a done task AND a triage task: only step1 fires.
        _mk_db(self.db, [_window()],
               done=[{"id": "t_done", "title": "OBJ-27 F0 trace",
                      "assignee": "pr-ollama", "body": "objective:OBJ-27",
                      "completed_at": NOW - 3600}],
               triage=[{"id": "t_tri", "title": "OBJ-19: Documentar matriz",
                        "assignee": None,
                        "body": "objective:OBJ-19 | cost:small\nDocumentar matriz"}])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=True, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(self.calls["create"]), 1)  # only step1


class TestDryRun(Base):
    def test_dry_run_prints_but_does_not_mutate(self):
        _mk_db(self.db, [_window()])
        fw._write_state(fw.state_file_path(self.home()),
                        {"empty_since": NOW - 3600, "window": "t_win"})
        out = fw.run(hermes_home=self.home(), execute=False, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("DRY:", out[0])
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["comment"], [])


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data/git", "host"):
            self.assertNotIn(needle, src,
                             f"host path leaked into fondo-queue-watch.py: "
                             f"{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
