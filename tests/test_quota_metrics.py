#!/usr/bin/python3.12
"""test_quota_metrics.py — tests de quota-metrics.py (F1/OBJ-24 + OBJ-29).

Dos linajes fusionados bajo tests/ (hogar unico de coleccion, fc635d7):

- OBJ-24 (F1) + OBJ-26a: parseo de last-good (shapes reales de
  ollama/nanogpt/opencode), conteo del board, fila JSONL correcta,
  degradacion elegante cuando falta un provider o el board es ilegible,
  y campos del ledger per-request en la fila.
- OBJ-29 (t_a821194b): supply_counts / supply_ratio en metrics-history,
  campos de supply en la fila de main(), evolucion de schema (consumidores
  tolerantes), y sqlite URI read-only desde cwd arbitrario.

TODO con fixtures tmp: nunca toca board, last-good ni ledger reales.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = str(Path(__file__).resolve().parent.parent)
SPEC = importlib.util.spec_from_file_location(
    "quota_metrics", f"{PLUGIN}/scripts/quota-metrics.py")
qm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qm)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        qm.OUT = Path(self.tmp) / "metrics.jsonl"
        qm.LAST_GOOD_DIR = Path(self.tmp)
        self.db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?, ?)", [
            ("t_1", "running"), ("t_2", "running"), ("t_3", "ready"),
            ("t_4", "blocked"), ("t_5", "triage"), ("t_6", "done"),
        ])
        con.commit()
        con.close()
        qm.KANBAN_DB = self.db

    def write_lg(self, name, data):
        json.dump(data, open(Path(self.tmp) / f"{name}-last-good.json", "w"))


class TestCollect(Base):
    def test_row_shape_and_values(self):
        self.write_lg("ollama", {"session_pct": 4.4, "weekly_pct": 34.8})
        self.write_lg("nanogpt", {"state": "active", "weekly_tokens_pct": 77.13})
        self.write_lg("opencode_go", {"rolling_pct": 3.0, "weekly_pct": 74.0})
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["running"], 2)
        self.assertEqual(row["ready"], 1)
        self.assertEqual(row["blocked"], 1)
        self.assertEqual(row["triage"], 1)
        self.assertEqual(row["ollama_weekly_pct"], 34.8)
        self.assertEqual(row["nanogpt_weekly_pct"], 77.13)
        self.assertEqual(row["opencode_weekly_pct"], 74.0)
        self.assertEqual(row["providers_ok"], 3)

    def test_partial_providers(self):
        self.write_lg("ollama", {"session_pct": 10.0, "weekly_pct": 20.0})
        # nanogpt y opencode ausentes
        rc = qm.main()
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 1)
        self.assertIsNone(row.get("nanogpt_weekly_pct"))

    def test_no_providers_warns_but_records_board(self):
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 0)
        self.assertIn("NINGUN provider", buf.getvalue())

    def test_last_good_corrupto_no_rompe(self):
        (Path(self.tmp) / "ollama-last-good.json").write_text("{broken json")
        self.write_lg("nanogpt", {"weekly_tokens_pct": 5.0})
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 1)

    def test_request_ledger_fields_en_fila(self):
        # OBJ-26a follow-up: los acumuladores per-request llegan a la fila
        # cuando el merge all-homes tiene datos (override de homes fixture).
        home = tempfile.mkdtemp(prefix="qm-ledger-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        requests = Path(home, "quota-governor", "nanogpt-requests.jsonl")
        requests.parent.mkdir(parents=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(requests, "w") as fh:
            fh.write(json.dumps({"ts": ts, "costUsd": 4.87e-06,
                                 "paymentSource": "USD",
                                 "requestId": "req_x"}) + "\n")
        self.write_lg("ollama", {"session_pct": 1.0, "weekly_pct": 2.0})
        self.write_lg("nanogpt", {"weekly_tokens_pct": 3.0})
        prev = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES")
        os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = home
        try:
            qm.main()
        finally:
            if prev is None:
                os.environ.pop("QUOTA_GOVERNOR_PROFILE_HOMES", None)
            else:
                os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = prev
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertAlmostEqual(row["nanogpt_request_balance_usd"], 4.87e-06,
                               places=9)
        self.assertAlmostEqual(row["nanogpt_request_covered_usd"], 0.0,
                               places=9)

    def test_request_ledger_sin_datos_omite_campos(self):
        # Sin filas de capture en ningun home: campos ausentes (None), nunca
        # 0.0 disfrazado de "gasto cero". Override -> home vacio inexistente
        # ("" volveria a los defaults reales, que SI tienen datos en vivo).
        self.write_lg("ollama", {"session_pct": 1.0, "weekly_pct": 2.0})
        prev = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES")
        os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = os.path.join(
            self.tmp, "nothing-here")
        try:
            qm.main()
        finally:
            if prev is None:
                os.environ.pop("QUOTA_GOVERNOR_PROFILE_HOMES", None)
            else:
                os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = prev
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertNotIn("nanogpt_request_balance_usd", row)
        self.assertNotIn("nanogpt_request_covered_usd", row)

    def test_board_ilegible_mensaja_y_exit0(self):
        qm.KANBAN_DB = Path("/nonexistent/kanban.db")
        rc = qm.main()
        self.assertEqual(rc, 0)
        self.assertFalse(qm.OUT.exists())


def _mk_db(path, events):
    """Create a minimal task_events table and insert (kind, created_at) rows."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL,"
        " run_id INTEGER,"
        " kind TEXT NOT NULL,"
        " payload TEXT,"
        " created_at INTEGER NOT NULL)")
    con.executemany(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
        [("t_x", kind, ts) for kind, ts in events])
    con.commit()
    con.close()


class SupplyCounts(unittest.TestCase):
    def test_counts_window_and_excludes_outside(self):
        now = 1_000_000_000
        evs = [("created", now - 3600), ("created", now - 100),
               ("completed", now - 60), ("created", now - 90000)]  # fuera
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "k.db")
            _mk_db(db, evs)
            created, closed = qm.supply_counts(now_epoch=now, db_path=db)
        self.assertEqual((created, closed), (2, 1))

    def test_archived_not_counted_as_closed(self):
        now = 1_000_000_000
        evs = [("completed", now - 500), ("archived", now - 400),
               ("archived", now - 300)]
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "k.db")
            _mk_db(db, evs)
            created, closed = qm.supply_counts(now_epoch=now, db_path=db)
        self.assertEqual((created, closed), (0, 1))

    def test_empty_db_zero_zero(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "k.db")
            _mk_db(db, [])
            created, closed = qm.supply_counts(now_epoch=1_000_000_000,
                                               db_path=db)
        self.assertEqual((created, closed), (0, 0))

    def test_default_now_uses_time_time(self):
        # default now_epoch is time.time(): a window far in the past -> zeros
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "k.db")
            _mk_db(db, [("created", 1)])
            created, closed = qm.supply_counts(db_path=db)
        self.assertEqual((created, closed), (0, 0))


class SupplyRatio(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(qm.supply_ratio(26, 23), 1.130)

    def test_none_when_zero_closed(self):
        self.assertIsNone(qm.supply_ratio(5, 0))

    def test_none_when_negative_closed(self):
        self.assertIsNone(qm.supply_ratio(5, -1))

    def test_none_when_closed_none(self):
        self.assertIsNone(qm.supply_ratio(5, None))

    def test_exact_one(self):
        self.assertEqual(qm.supply_ratio(3, 3), 1.0)


class MainRow(unittest.TestCase):
    """main() with a monkeypatched KANBAN_DB: fixture board + events."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        db = str(Path(self.td.name) / "kanban.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (status TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?)",
                        [("running",), ("running",), ("ready",),
                         ("blocked",), ("triage",)])
        con.execute(
            "CREATE TABLE task_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
            " payload TEXT, created_at INTEGER NOT NULL)")
        now = time.time()
        con.executemany(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?,?,?)",
            [("t1", "created", now - 3600), ("t2", "completed", now - 1800)])
        con.commit()
        con.close()
        self.db = db
        self._orig_db = qm.KANBAN_DB
        self._orig_out = qm.OUT
        self._orig_lg = qm.LAST_GOOD_DIR
        self._orig_ledger = qm.NANOGPT_LEDGER
        qm.KANBAN_DB = Path(db)
        qm.OUT = Path(self.td.name) / "metrics-history.jsonl"
        # last-good + ledger: temp vacio (aislamiento total del perfil real;
        # sin este patch el bloque de balance cargaria el ledger real y
        # budget_context() podria sondear /api/check-balance — red prohibida)
        qm.LAST_GOOD_DIR = Path(self.td.name) / "last-good"
        qm.LAST_GOOD_DIR.mkdir()
        qm.NANOGPT_LEDGER = str(Path(self.td.name) / "missing-ledger.py")

    def tearDown(self):
        qm.KANBAN_DB = self._orig_db
        qm.OUT = self._orig_out
        qm.LAST_GOOD_DIR = self._orig_lg
        qm.NANOGPT_LEDGER = self._orig_ledger
        self.td.cleanup()

    def test_row_contains_supply_fields(self):
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[0])
        for k in ("ts", "running", "ready", "blocked", "triage",
                  "supply_created_24h", "supply_closed_24h", "supply_ratio"):
            self.assertIn(k, row)
        self.assertEqual(row["supply_created_24h"], 1)
        self.assertEqual(row["supply_closed_24h"], 1)
        self.assertEqual(row["supply_ratio"], 1.0)
        # legacy fields preserved
        self.assertEqual(row["running"], 2)
        self.assertEqual(row["providers_ok"], 0)

    def test_row_degrades_to_none_on_bad_events_db(self):
        # events table missing -> supply fields None, row still written
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE task_events")
        con.commit()
        con.close()
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[0])
        self.assertIsNone(row["supply_created_24h"])
        self.assertIsNone(row["supply_closed_24h"])
        self.assertIsNone(row["supply_ratio"])
        self.assertIn("running", row)  # the rest of the row survives

    def test_consecutive_appends(self):
        qm.main()
        qm.main()
        lines = qm.OUT.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("supply_ratio", line)


class SchemaEvolution(unittest.TestCase):
    """Consumers stay tolerant: rows with and without supply fields parse."""

    OLD_ROW = {"ts": "2026-09-10T02:00:00Z", "running": 3, "ready": 5,
               "blocked": 1, "triage": 9, "ollama_weekly_pct": 52.1,
               "nanogpt_weekly_pct": 100.0, "opencode_weekly_pct": 100.0,
               "providers_ok": 3}

    def test_old_row_without_supply_fields_still_valid(self):
        # forecast reader pattern: json.loads + dict.get
        d = json.loads(json.dumps(self.OLD_ROW))
        self.assertIsNone(d.get("supply_ratio"))
        self.assertEqual(d["providers_ok"], 3)

    def test_new_row_superset_of_old_keys(self):
        now = 1_000_000_000
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "k.db")
            _mk_db(db, [("created", now - 60), ("completed", now - 30)])
            created, closed = qm.supply_counts(now_epoch=now, db_path=db)
        row = dict(self.OLD_ROW)
        row["supply_created_24h"] = created
        row["supply_closed_24h"] = closed
        row["supply_ratio"] = qm.supply_ratio(created, closed)
        # every old key survives untouched -> no consumer break
        for k, v in self.OLD_ROW.items():
            self.assertEqual(row[k], v)

if __name__ == "__main__":
    unittest.main(verbosity=2)
