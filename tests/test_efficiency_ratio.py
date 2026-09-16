#!/usr/bin/python3.12
"""Tests for efficiency-ratio.py (P4). Fixtures only — no real board."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
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
        er._tbp = False  # verifier limited unless tick_body_parts available
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

    def add_task(self, tid, body, status="done", result="",
                 completed_at=None, comments=(), runs=()):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (tid, f"task {tid}", "pr-ollama", status, body, result,
             self.now - 3600 if completed_at is None else completed_at))
        for c in comments:
            con.execute("INSERT INTO task_comments VALUES (?,?)", (tid, c))
        for r in runs:
            con.execute("INSERT INTO task_runs VALUES (?,?)", (tid, r))
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


# NB: tests 6 (multi-part evidence) use the REAL tick_body_parts via the
# repo path candidate (here.parent / tick_body_parts.py) — available in the
# plugin repo checkout, so verifier=full there.


class TestEfficiencyRatio(Harness):

    def test_1_10_verified_2usd_ratio5(self):
        for i in range(10):
            self.add_task(
                f"t_aaaaaa0{i}",
                "objective:AUTODEV | cost:tiny\n\nsuccess: informe generado",
                result="informe generado: ok")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 2.0,
                         "consumer_id": "e8fbb"}])
        out = self.run_now()
        self.assertEqual(out["tareas_verificadas"], 10)
        self.assertEqual(out["base_mode"], "strict")
        self.assertEqual(out["ratio"], 5.0)
        self.assertEqual(out["veredicto"], "EXCELENTE")

    def test_2_2_verified_3usd_bajo(self):
        for i in range(2):
            self.add_task(
                f"t_aaaaaa1{i}",
                "objective:AUTODEV\n\nsuccess: mapa generado",
                result="mapa generado: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 3.0}])
        out = self.run_now()
        self.assertEqual(out["ratio"], 0.67)
        self.assertEqual(out["veredicto"], "BAJO")

    def test_3_zero_spend_sin_gasto(self):
        self.add_task("t_aaaaaa20",
                      "objective:AUTODEV\n\nsuccess: doc written",
                      result="doc written: done")
        self.add_trace([])
        out = self.run_now()
        self.assertIsNone(out["ratio"])
        self.assertEqual(out["veredicto"], "SIN GASTO")

    def test_4_done_without_criterion_not_counted(self):
        self.add_task("t_aaaaaa30", "objective:AUTODEV | cost:tiny",
                      result="todo bien")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 1.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertEqual(out["tareas_verificadas"], 0)

    def test_5_criterion_without_evidence_not_counted(self):
        self.add_task("t_aaaaaa40",
                      "objective:AUTODEV\n\nsuccess: audiolibro transcrito",
                      result="empezando")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 1.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_verificadas"], 0)

    def test_6_multipart_verified_via_tick_body_parts(self):
        body = ("objective:AUTODEV | cost:tiny | clase:C\n\n"
                "R1: extraer filas\nR2: resumir columnas")
        self.add_task("t_aaaaaa50", body,
                      result="R1: extraidas 10 filas. R2: resumidas 4 columnas")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 1.0}])
        out = self.run_now()
        # Verifier=full only when tick_body_parts imports; strict either way:
        if out["verifier"] == "full":
            self.assertEqual(out["tareas_verificadas"], 1)
        else:
            self.assertEqual(out["tareas_verificadas"], 0)

    def test_7_non_budget_objective_excluded(self):
        self.add_task("t_aaaaaa60",
                      "objective:OBJ-13\n\nsuccess: x generado",
                      result="x generado: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "OBJ-13", "costUsd": 5.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 0)
        self.assertEqual(out["gasto_strict_usd"], 0.0)
        self.assertEqual(out["base_mode"], "proxy-total-24h")

    def test_8_failopen_db_missing(self):
        os.environ["ER_KANBAN_DB"] = str(self.root / "nope.db")
        self.add_trace([{"ts_epoch_utc": self.now - 600, "costUsd": 1.0}])
        out = self.run_now()
        self.assertIsNone(out["ratio"])
        self.assertEqual(out["veredicto"], "N/A")
        self.assertIn("error", out)

    def test_9_weekly_is_total_over_total(self):
        for i in range(3):
            self.add_task(
                f"t_aaaaaa7{i}",
                "objective:AUTOREPAIR\n\nsuccess: bug cerrado",
                result="bug cerrado: ok")
        # 2h ago: inside 24h AND 7d; 3d ago: only 7d.
        self.add_trace([
            {"ts_epoch_utc": self.now - 2 * 3600, "objective": "AUTOREPAIR",
             "costUsd": 1.0},
            {"ts_epoch_utc": self.now - 3 * 86400, "objective": "AUTOREPAIR",
             "costUsd": 1.0},
        ])
        out = self.run_now()
        self.assertEqual(out["tareas_verificadas"], 3)
        self.assertEqual(out["ratio"], 3.0)         # 24h: 3 / $1 (solo la de 2h)
        self.assertEqual(out["ratio_7d"], 1.5)      # 7d: 3 / $2 (total/total)
        self.assertEqual(out["tareas_verificadas_7d"], 3)

    def test_10_append_line_kind_efficiency(self):
        self.add_task("t_aaaaaa80",
                      "objective:AUTODEV\n\nsuccess: chip ensamblado",
                      result="chip ensamblado: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 0.5}])
        entry = er.compute(now=self.now)
        er.append_metrics(entry, self.metrics)
        lines = [json.loads(l) for l in
                 self.metrics.read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "efficiency_ratio")
        self.assertEqual(lines[0]["tareas_verificadas"], 1)

    def test_main_writes_and_is_silent(self):
        self.add_task("t_aaaaaa90",
                      "objective:AUTODEV\n\nsuccess: sello puesto",
                      result="sello puesto: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 0.25}])
        buf = io_capture(er.main, ["--dry-run"])
        self.assertIn('"kind": "efficiency_ratio"', buf)
        self.assertFalse(self.metrics.exists(), "dry-run must not write")

    def test_tag_in_footer_counts_obj20(self):
        self.add_task("t_aaaaaa95",
                      "Un body largo sin header...\n\nobjective:AUTODEV\n"
                      "success: campana sonada",
                      result="campana sonada: done")
        self.add_trace([{"ts_epoch_utc": self.now - 600,
                         "objective": "AUTODEV", "costUsd": 1.0}])
        out = self.run_now()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertEqual(out["tareas_verificadas"], 1)


def io_capture(fn, argv):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(argv)
    assert rc == 0
    return buf.getvalue()


if __name__ == "__main__":
    sys.exit(unittest.main())
