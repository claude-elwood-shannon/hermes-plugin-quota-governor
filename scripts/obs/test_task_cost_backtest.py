#!/usr/bin/python3.12
"""test_task_cost_backtest.py — OBJ-35 P3 tests (hermetic).

Covers: evaluate() pairing (estimate precedes outcome), error math,
in-band coverage, verdict aggregation, small-class goal flag, idempotent
daily verdict, tolerant degradation, seal -> train -> backtest flow.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


bt = _load("task_cost_backtest", HERE / "task-cost-backtest.py")


class TestEvaluate(unittest.TestCase):
    def test_error_math_and_in_band(self):
        tasks = [
            {"task_id": "t1", "attributed": True, "costUsd": 0.10,
             "cost_class": "small", "objective": "OBJ-1"},
            {"task_id": "t2", "attributed": True, "costUsd": 0.50,
             "cost_class": "small", "objective": "OBJ-1"},
            {"task_id": "t3", "attributed": False, "costUsd": None},
        ]
        ests = [
            {"task_id": "t1", "ts": 100.0,
             "estimate": {"p50": 0.12, "p90": 0.20, "stage": "class"}},
            # two predictions: earliest must win
            {"task_id": "t2", "ts": 200.0,
             "estimate": {"p50": 0.60, "p90": 0.80, "stage": "class"}},
            {"task_id": "t2", "ts": 100.0,
             "estimate": {"p50": 0.40, "p90": 0.55, "stage": "class"}},
        ]
        out = bt.evaluate(tasks, ests)
        self.assertEqual(len(out), 2)
        by = {e["task_id"]: e for e in out}
        # t1: |0.10-0.12|/0.10 = 20%, in band (0.10 <= 0.20)
        self.assertAlmostEqual(by["t1"]["error_pct"], 20.0)
        self.assertTrue(by["t1"]["in_band"])
        # t2: earliest prediction (ts=100) used -> p50=0.40, cap=0.55
        self.assertAlmostEqual(by["t2"]["predicted"], 0.40)
        self.assertTrue(by["t2"]["in_band"])  # 0.50 <= 0.55

    def test_unpaired_rows_ignored(self):
        tasks = [{"task_id": "t9", "attributed": True, "costUsd": 1.0,
                  "cost_class": "small", "objective": "OBJ-1"}]
        self.assertEqual(bt.evaluate(tasks, []), [])


class TestVerdict(unittest.TestCase):
    def test_empty(self):
        v = bt.build_verdict([], "2026-09-10")
        self.assertEqual(v["n"], 0)

    def test_aggregates(self):
        errs = [
            {"error_pct": 10.0, "in_band": True, "cost_class": "small"},
            {"error_pct": 30.0, "in_band": False, "cost_class": "small"},
            {"error_pct": 50.0, "in_band": False, "cost_class": "medium"},
            {"error_pct": 20.0, "in_band": True, "cost_class": "small"},
        ]
        v = bt.build_verdict(errs, "2026-09-10")
        self.assertEqual(v["n"], 4)
        self.assertAlmostEqual(v["mape_pct"], 27.5)
        self.assertAlmostEqual(v["in_band_pct"], 50.0)
        self.assertTrue(v["ok_small"])  # p50 of [10,20,30] = 20 < 30

    def test_small_goal_needs_n3(self):
        errs = [{"error_pct": 90.0, "in_band": False,
                 "cost_class": "small"}]
        v = bt.build_verdict(errs, "2026-09-10")
        self.assertNotIn("ok_small", v)


class TestRunIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "l.jsonl"
        self.old = os.environ.get("QUOTA_TASK_COST_LEDGER")
        os.environ["QUOTA_TASK_COST_LEDGER"] = str(self.ledger)

    def tearDown(self):
        if self.old is not None:
            os.environ["QUOTA_TASK_COST_LEDGER"] = self.old
        else:
            os.environ.pop("QUOTA_TASK_COST_LEDGER", None)
        self.tmp.cleanup()

    def test_full_flow_seal_train_backtest(self):
        # simulate: estimate captured, then task closed with real cost
        lines = [
            {"kind": "estimate", "task_id": "tA", "ts": 100.0,
             "estimate": {"p50": 0.10, "p90": 0.15, "stage": "class",
                          "model": "m", "n": 5}},
            {"kind": "task", "task_id": "tA", "attributed": True,
             "costUsd": 0.11, "cost_class": "small", "objective": "OBJ-1",
             "model": "m", "duration_s": 600, "completed_at": 200.0},
        ]
        with open(self.ledger, "w") as fh:
            for l in lines:
                fh.write(json.dumps(l) + "\n")
        now = 1789056000.0  # 2026-09-10 UTC
        res1 = bt.run(now=now)
        self.assertTrue(res1["new"])
        self.assertEqual(res1["verdict"]["n"], 1)
        # second run same day: no new verdict
        res2 = bt.run(now=now + 60)
        self.assertFalse(res2["new"])
        # ledger has exactly one est-day line
        n_est_day = sum(
            1 for l in self.ledger.read_text().splitlines()
            if json.loads(l).get("kind") == "est-day")
        self.assertEqual(n_est_day, 1)

    def test_missing_ledger_ok(self):
        res = bt.run(now=1789056000.0)
        self.assertEqual(res["verdict"]["n"], 0)
        self.assertFalse(res["new"])


if __name__ == "__main__":
    unittest.main()
