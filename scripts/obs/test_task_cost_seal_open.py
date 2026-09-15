#!/usr/bin/python3.12
"""test_task_cost_seal_open.py — OBJ-METRICS P3 deferred seal (hermetic).

Covers: only open tasks get sealed, idempotence against existing
kind=estimate rows, --dry-run writes nothing, no-body tasks skipped,
tolerant degradation on a missing board/ledger.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


so = _load("task_cost_seal_open", HERE / "task-cost-seal-open.py")


def _fake_db(path: Path, tasks: list) -> None:
    """Minimal kanban.db stand-in with the columns the seal pass reads."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, completed_at INTEGER, "
        "model_override TEXT, body TEXT)")
    for t in tasks:
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?)",
            (t["id"], t["status"], t.get("completed_at"),
             t.get("model_override"), t.get("body")))
    con.commit()
    con.close()


def _ledger_lines(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]


class SealOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "train.jsonl"
        self.db = Path(self.tmp.name) / "kanban.db"
        _fake_db(self.db, [
            {"id": "t_open", "status": "running", "completed_at": None,
             "model_override": "m-fast",
             "body": "objective:OBJ-1 | cost:micro\nwork"},
            {"id": "t_ready", "status": "ready", "completed_at": None,
             "model_override": None,
             "body": "objective:OBJ-2 | cost:small\nmore"},
            {"id": "t_done", "status": "done", "completed_at": 100,
             "model_override": None, "body": "objective:OBJ-1 | cost:micro"},
            {"id": "t_nobody", "status": "ready", "completed_at": None,
             "model_override": None, "body": "   "},
        ])
        self.old_env = {k: os.environ.get(k) for k in
                        ("QUOTA_TASK_COST_LEDGER",
                         "QUOTA_TASK_COST_KANBAN_DB")}
        os.environ["QUOTA_TASK_COST_LEDGER"] = str(self.ledger)
        os.environ["QUOTA_TASK_COST_KANBAN_DB"] = str(self.db)

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_seals_only_open_tasks_without_estimate(self):
        sealed = so.seal_open(ledger=self.ledger, kanban_db=self.db,
                              now=500.0)
        ids = {s["task_id"] for s in sealed}
        self.assertEqual(ids, {"t_open", "t_ready"})  # done + no-body out
        rows = _ledger_lines(self.ledger)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["kind"], "estimate")
            self.assertEqual(r["ts"], 500.0)
            self.assertIsInstance(r["estimate"]["p50"], (int, float, type(None)))
        by = {r["task_id"]: r for r in rows}
        self.assertEqual(by["t_open"]["estimate"]["model"], "m-fast")

    def test_idempotent_second_pass(self):
        so.seal_open(ledger=self.ledger, kanban_db=self.db, now=500.0)
        again = so.seal_open(ledger=self.ledger, kanban_db=self.db,
                             now=600.0)
        self.assertEqual(again, [])
        self.assertEqual(len(_ledger_lines(self.ledger)), 2)

    def test_respects_existing_estimate_row(self):
        self.ledger.write_text(json.dumps(
            {"kind": "estimate", "task_id": "t_open", "ts": 1.0,
             "estimate": {"p50": 0.01, "p90": 0.02, "stage": "class"}}
        ) + "\n", encoding="utf-8")
        sealed = so.seal_open(ledger=self.ledger, kanban_db=self.db,
                              now=500.0)
        self.assertEqual({s["task_id"] for s in sealed}, {"t_ready"})
        self.assertEqual(len(_ledger_lines(self.ledger)), 2)  # +1, no edit

    def test_dry_run_writes_nothing(self):
        sealed = so.seal_open(dry_run=True, ledger=self.ledger,
                              kanban_db=self.db, now=500.0)
        self.assertEqual({s["task_id"] for s in sealed},
                         {"t_open", "t_ready"})
        self.assertFalse(self.ledger.exists())

    def test_missing_sources_degrade_to_empty(self):
        missing = Path(self.tmp.name) / "nope.db"
        self.assertEqual(
            so.seal_open(ledger=Path(self.tmp.name) / "nope.jsonl",
                         kanban_db=missing), [])


if __name__ == "__main__":
    unittest.main()
