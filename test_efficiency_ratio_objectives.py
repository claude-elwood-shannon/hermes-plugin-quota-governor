#!/usr/bin/env python3
"""test_efficiency_ratio_objectives.py — MEDIATOR t_4fa0a4b5 regression.

The budget moved from the legacy AUTODEV/AUTOREPAIR pair to the
approved_objectives TABLE in kanban.db.  The efficiency ratio must count
done tasks tagged `objective:<table-id>` (any row) and their strict spend,
while keeping the legacy names valid and non-budget tags excluded.

Fixture-only (no real board), same harness style as test_efficiency_ratio.
Run:  python3 test_efficiency_ratio_objectives.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "efficiency_ratio", _HERE / "scripts" / "obs" / "efficiency-ratio.py")
er = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(er)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profiles" / "pr-ollama"
        (self.profile / "quota-governor" / "obs").mkdir(parents=True)
        self.db = self.root / "kanban.db"
        self.trace = self.profile / "quota-governor" / "obs" / "trace.jsonl"
        self.metrics = self.profile / "quota-governor" / "metrics-history.jsonl"
        os.environ["ER_KANBAN_DB"] = str(self.db)
        os.environ["ER_TRACE"] = str(self.trace)
        os.environ["ER_METRICS"] = str(self.metrics)
        os.environ["ER_HERMES_ROOT"] = str(self.root)
        er._tbp = False  # verifier limited
        er._objectives_cache.clear()
        er._OBJECTIVES_RES_CACHE.clear()
        self.now = 1789500000.0
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
            "assignee TEXT, status TEXT, body TEXT, result TEXT, "
            "completed_at REAL);"
            "CREATE TABLE task_runs (task_id TEXT, summary TEXT);"
            "CREATE TABLE task_comments (task_id TEXT, body TEXT);")
        con.commit()
        con.close()

    def add_objectives(self, rows):
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS approved_objectives ("
            "id TEXT PRIMARY KEY, name TEXT, budget_daily REAL, "
            "description TEXT, status TEXT, success_criterion TEXT, "
            "spent_today REAL DEFAULT 0.0, spent_total REAL DEFAULT 0.0)")
        for oid, status in rows:
            con.execute(
                "INSERT INTO approved_objectives (id, name, status) "
                "VALUES (?,?,?)", (oid, oid, status))
        con.commit()
        con.close()

    def add_task(self, tid, body, status="done", result="", comments=()):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (tid, f"task {tid}", "pr-ollama", status, body, result,
             self.now - 3600))
        for c in comments:
            con.execute("INSERT INTO task_comments VALUES (?,?)", (tid, c))
        con.commit()
        con.close()

    def add_trace(self, entries):
        with open(self.trace, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def run_now(self):
        return er.compute(now=self.now)

    def tearDown(self):
        for k in ("ER_KANBAN_DB", "ER_TRACE", "ER_METRICS", "ER_HERMES_ROOT"):
            os.environ.pop(k, None)


class TestTableDrivenObjectives(Harness):

    def test_1_table_objective_counts_as_budget_task(self):
        self.add_objectives([("OBJ-CODEQUALITY", "active"),
                             ("OBJ-AUTODEV", "active")])
        self.add_task("t_c0de0001",
                      "objective:OBJ-CODEQUALITY | cost:tiny\n\n"
                      "success: funciones listadas y plan escrito",
                      result="R1: listadas 3 funciones >50 lineas y plan "
                             "escrito — done")
        self.add_trace([])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertEqual(out["tareas_verificadas"], 1)

    def test_2_strict_spend_join_via_table_id(self):
        self.add_objectives([("OBJ-METRICS", "active")])
        self.add_task("t_c0de0002",
                      "objective:OBJ-METRICS\n\nsuccess: backtest generado",
                      result="backtest generado: done, ratio 0.8")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "OBJ-METRICS", "costUsd": 0.15}])
        out = self.run_now()
        self.assertGreater(out["gasto_strict_usd"], 0.0)
        self.assertEqual(out["base_mode"], "strict")

    def test_3_strict_spend_join_via_task_id(self):
        self.add_objectives([("OBJ-SYSADMIN", "active")])
        self.add_task("t_c0de0003",
                      "objective:OBJ-SYSADMIN\n\nsuccess: ssh verificado",
                      result="ssh verificado y nvidia-smi capturado — done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "unattributed",
                         "consumer_id": "t_c0de0003", "costUsd": 0.10}])
        out = self.run_now()
        self.assertGreater(out["gasto_strict_usd"], 0.0)
        self.assertEqual(out["base_mode"], "strict")

    def test_4_non_table_objective_still_excluded(self):
        self.add_objectives([("OBJ-AUTODEV", "active")])
        self.add_task("t_c0de0004",
                      "objective:OBJ-13\n\nsuccess: x generado",
                      result="x generado: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "OBJ-13", "costUsd": 5.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 0)
        self.assertEqual(out["gasto_strict_usd"], 0.0)
        self.assertEqual(out["base_mode"], "proxy-total-24h")

    def test_5_missing_table_fails_open_to_legacy(self):
        # No approved_objectives table — legacy pair still counts.
        self.add_task("t_c0de0005",
                      "objective:AUTODEV\n\nsuccess: doc written",
                      result="doc written: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 1.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertGreater(out["gasto_strict_usd"], 0.0)

    def test_6_non_active_status_still_counts_for_spend(self):
        # achieved/paused objectives keep their historical spend visible.
        self.add_objectives([("OBJ-VLLM", "achieved")])
        self.add_task("t_c0de0006",
                      "objective:OBJ-VLLM\n\nsuccess: invoke registrado",
                      result="invoke registrado exit 0 — done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "OBJ-VLLM", "costUsd": 0.05}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertGreater(out["gasto_strict_usd"], 0.0)

    def test_7_untagged_task_never_counts(self):
        self.add_objectives([("OBJ-AUTODEV", "active")])
        self.add_task("t_c0de0007",
                      "body sin tag alguno", result="done")
        self.add_trace([])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 0)
        self.assertEqual(out["tareas_verificadas"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
