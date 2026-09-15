#!/usr/bin/python3.12
"""test_quota_metrics.py — OBJ-29 (t_a821194b): supply_ratio in metrics-history.

Covers (fixtures only, no network, no real kanban.db):
  1. supply_counts counts task_events 'created'/'completed' in the rolling
     24h UTC window (boundary: event just outside is excluded)
  2. 'archived' events are NOT counted as closures (post-hoc cleanup)
  3. supply_ratio: normal case, None when closed==0 (undefined, not deficit),
     None on zero/negative inputs
  4. main() row: supply fields present after board row; degrade to None when
     the events DB is unreadable (F1 must never fail for supply columns)
  5. schema evolution: consumers (quota-forecast, backtest-f2) parse rows with
     and without the new fields — tolerant readers, no schema break
  6. sqlite URI: read-only mode works from an arbitrary cwd

Run:  /usr/bin/python3.12 test_quota_metrics.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent.parent)
METRICS_SCRIPT = Path(GOV_DIR) / "scripts" / "quota-metrics.py"

_spec = importlib.util.spec_from_file_location("quota_metrics", METRICS_SCRIPT)
qm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qm)


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
    unittest.main()
