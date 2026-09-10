#!/usr/bin/python3.12
"""test_task_cost_train.py — OBJ-35 P1 tests (hermetic, /usr/bin/python3.12).

Mirrors test_trace.py conventions: hermetic tmp homes, no live sources.
Covers: objective/cost/clase tag parsing, session join, cost estimation
(catalog), shared-session flag, idempotency, aggregation by
(objective, cost_class, model), fail-open on missing sources.
"""
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

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tct = _load("task_cost_train", HERE / "task-cost-train.py")


def _make_profile_home(base: Path, profile: str, usage_rows: list):
    """Create profiles/<profile>/state.db with session_model_usage rows."""
    prof = base / "profiles" / profile
    prof.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(prof / "state.db")
    con.execute(
        "CREATE TABLE session_model_usage ("
        " session_id TEXT, model TEXT, task TEXT, api_call_count INTEGER,"
        " input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, reasoning_tokens INTEGER,"
        " billing_provider TEXT, estimated_cost_usd REAL,"
        " cost_source TEXT, last_seen TEXT)")
    for r in usage_rows:
        con.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("session_id"), r.get("model"), r.get("task"),
             r.get("api_call_count", 0), r.get("input_tokens", 0),
             r.get("output_tokens", 0), r.get("cache_read_tokens", 0),
             r.get("reasoning_tokens", 0),
             r.get("billing_provider", "unknown"),
             r.get("estimated_cost_usd", 0.0),
             r.get("cost_source", "none"), r.get("last_seen")))
    con.commit()
    con.close()


def _make_kanban(base: Path, tasks: list, events=None):
    """Create a kanban.db with tasks (+ optional task_events) rows."""
    p = base / "kanban.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT,"
        " assignee TEXT, created_at REAL, completed_at REAL,"
        " session_id TEXT)")
    con.execute(
        "CREATE TABLE task_events (task_id TEXT, kind TEXT,"
        " created_at REAL)")
    con.execute(
        "CREATE TABLE task_runs (task_id TEXT, id INTEGER)")
    for t in tasks:
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
            (t["id"], t.get("body"), t.get("assignee"),
             t.get("created_at"), t.get("completed_at"),
             t.get("session_id")))
    for e in (events or []):
        con.execute("INSERT INTO task_events VALUES (?,?,?)",
                    (e[0], e[1], e[2]))
    for t in tasks:
        if t.get("runs"):
            con.execute("INSERT INTO task_runs VALUES (?,?)",
                        (t["id"], 1))
    con.commit()
    con.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ledger = self.base / "task-cost-train.jsonl"
        self._old = {}
        for var in ("QUOTA_TASK_COST_PROFILE_HOMES", "QUOTA_TASK_COST_KANBAN_DB",
                    "QUOTA_TASK_COST_LEDGER"):
            self._old[var] = os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self._old.items():
            if val is not None:
                os.environ[var] = val
        self.tmp.cleanup()

    def set_env(self):
        os.environ["QUOTA_TASK_COST_PROFILE_HOMES"] = str(self.base / "profiles")
        # profile homes env points at profiles ROOT in tests: patch the
        # scanner via per-profile dirs directly
        os.environ["QUOTA_TASK_COST_PROFILE_HOMES"] = "pr-ollama"
        os.environ["QUOTA_TASK_COST_KANBAN_DB"] = str(
            self.base / "kanban.db")
        os.environ["QUOTA_TASK_COST_LEDGER"] = str(self.ledger)
        # redirect profiles base for the test scanner
        os.environ["QUOTA_TASK_COST_TEST_BASE"] = str(self.base)

    def collect(self):
        return tct.collect(now=1789056000.0)


# Patch the profiles base so tests are hermetic
_orig_profile_state_dbs = tct.profile_state_dbs


def _test_profile_state_dbs():
    raw = os.environ.get("QUOTA_TASK_COST_PROFILE_HOMES", "").strip()
    base = os.environ.get("QUOTA_TASK_COST_TEST_BASE", "")
    if base and raw:
        out = []
        for name in raw.split(os.pathsep):
            p = Path(base) / "profiles" / name.strip() / "state.db"
            if p.exists():
                out.append(p)
        return out
    return _orig_profile_state_dbs()


tct.profile_state_dbs = _test_profile_state_dbs


class TestTags(Base):
    BODY = ("objective:OBJ-27 | cost:small | privacy:low | clase:B\n"
            "cuerpo")
    BODY_MULTILINE = ("objective:OBJ-27\ncost:small\nclase:B\ncuerpo")

    def test_parse_objective(self):
        self.assertEqual(tct.parse_objective(self.BODY), "OBJ-27")
        self.assertEqual(tct.parse_objective(self.BODY_MULTILINE), "OBJ-27")

    def test_parse_cost_class(self):
        self.assertEqual(tct.parse_cost_class(self.BODY), "small")
        self.assertEqual(tct.parse_cost_class(self.BODY_MULTILINE), "small")

    def test_parse_clase(self):
        self.assertEqual(tct.parse_clase(self.BODY), "B")
        self.assertEqual(tct.parse_clase(self.BODY_MULTILINE), "B")

    def test_missing_tags(self):
        self.assertIsNone(tct.parse_cost_class("sin tags"))
        self.assertIsNone(tct.parse_clase("sin tags"))
        self.assertEqual(tct.parse_objective("sin tags"), "unattributed")


class TestCollect(Base):
    def _scenario(self):
        _make_profile_home(self.base, "pr-ollama", [
            # worker session: task IS NULL -> the join target
            {"session_id": "s1", "model": "deepseek-v4-flash",
             "task": None, "api_call_count": 30,
             "input_tokens": 57242, "output_tokens": 17548,
             "cache_read_tokens": 1191808, "reasoning_tokens": 0,
             "billing_provider": "ollama-cloud",
             "last_seen": "2026-09-10T16:56:13+00:00"},
            # housekeeping rows are excluded
            {"session_id": "s1", "model": "glm-5.3-flash",
             "task": "title_generation", "api_call_count": 1,
             "input_tokens": 244, "output_tokens": 112,
             "cache_read_tokens": 0, "reasoning_tokens": 95,
             "billing_provider": "custom",
             "last_seen": "2026-09-10T16:56:13+00:00"},
            # different session, must NOT join to s1's task
            {"session_id": "s2", "model": "qwen3.8-flash",
             "task": None, "api_call_count": 5,
             "input_tokens": 1000, "output_tokens": 200,
             "cache_read_tokens": 0, "reasoning_tokens": 0,
             "billing_provider": "opencode-go",
             "last_seen": "2026-09-10T15:00:00+00:00"},
        ])
        kanban = _make_kanban(self.base, [
            {"id": "t_join", "body": self.BODY_CONCRETE,
             "assignee": "pr-ollama", "created_at": 1789050000.0,
             "completed_at": 1789051800.0, "session_id": "s1"},
            {"id": "t_nosess", "body": "objective:OBJ-99 | cost:tiny",
             "assignee": "pr-ollama", "created_at": 1789050000.0,
             "completed_at": 1789050600.0, "session_id": None},
            {"id": "t_nobody", "body": None,
             "assignee": "pr-ollama", "created_at": 1789050000.0,
             "completed_at": 1789050300.0, "session_id": "s2"},
        ], events=[("t_join", "completed", 1789051800.0),
                   ("t_join", "crashed", 1789050100.0)])
        return kanban

    BODY_CONCRETE = ("objective:OBJ-27 | cost:small | clase:B\n"
                     "cuerpo de prueba")

    def setUp(self):
        super().setUp()
        self._scenario()
        self.set_env()

    def test_first_pass_appends_rows(self):
        res = self.collect()
        self.assertEqual(res["appended"], 2)  # t_join + t_nosess
        self.assertEqual(res["no_body"], 1)

    def test_row_content(self):
        self.collect()
        rows = [json.loads(l) for l in
                self.ledger.read_text().strip().splitlines()]
        by_id = {r["task_id"]: r for r in rows}
        r = by_id["t_join"]
        self.assertEqual(r["objective"], "OBJ-27")
        self.assertEqual(r["cost_class"], "small")
        self.assertEqual(r["clase"], "B")
        self.assertEqual(r["duration_s"], 1800)
        self.assertEqual(r["crashes"], 1)
        self.assertTrue(r["attributed"])
        self.assertEqual(r["model"], "deepseek-v4-flash")
        self.assertEqual(r["cost_source"], "estimated-catalog")
        self.assertGreater(r["costUsd"], 0.0)
        # housekeeping row must be excluded from the join
        self.assertEqual(r["usage_rows"], 1)
        # unattributed task still recorded, cost None (gap shown)
        r2 = by_id["t_nosess"]
        self.assertFalse(r2["attributed"])
        self.assertIsNone(r2["costUsd"])
        self.assertEqual(r2["objective"], "OBJ-99")

    def test_idempotent_second_pass(self):
        self.collect()
        res = self.collect()
        self.assertEqual(res["appended"], 0)
        n = len(self.ledger.read_text().strip().splitlines())
        self.assertEqual(n, 2)

    def test_deepseek_peak_doubling(self):
        # 01:00-04:00 UTC weekday -> x2 peak prices on deepseek
        self.base.joinpath("profiles/pr-ollama/state.db").unlink()
        _make_profile_home(self.base, "pr-ollama", [
            {"session_id": "spe", "model": "deepseek-v4-flash",
             "task": None, "api_call_count": 1,
             "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_tokens": 0, "reasoning_tokens": 0,
             "billing_provider": "ollama-cloud",
             "last_seen": "2026-09-10T02:00:00+00:00"},  # Thu 02:00 UTC
        ])
        # fresh db file for this test's second kanban scenario
        self.base.joinpath("kanban.db").unlink()
        _make_kanban(self.base, [
            {"id": "t_peak", "body": "objective:OBJ-1 | cost:small",
             "assignee": "pr-ollama", "created_at": 1789040000.0,
             "completed_at": 1789041000.0, "session_id": "spe"},
        ])
        self.collect()
        rows = [json.loads(l) for l in
                self.ledger.read_text().strip().splitlines()]
        r = next(x for x in rows if x["task_id"] == "t_peak")
        self.assertAlmostEqual(r["costUsd"], 0.44, places=3)  # 0.22 * 2

    def test_shared_session_flag(self):
        self.base.joinpath("kanban.db").unlink()
        _make_kanban(self.base, [
            {"id": "t_sh1", "body": "objective:OBJ-1 | cost:tiny",
             "assignee": "pr-ollama", "created_at": 1789040000.0,
             "completed_at": 1789041000.0, "session_id": "s1"},
            {"id": "t_sh2", "body": "objective:OBJ-1 | cost:tiny",
             "assignee": "pr-ollama", "created_at": 1789040000.0,
             "completed_at": 1789042000.0, "session_id": "s1"},
        ])
        self.collect()
        rows = [json.loads(l) for l in
                self.ledger.read_text().strip().splitlines()]
        for r in rows:
            if r["task_id"] in ("t_sh1", "t_sh2"):
                self.assertTrue(r["shared_session"])
                self.assertEqual(r["session_tasks"], 2)


class TestEstimator(Base):
    def test_median_and_p90(self):
        import statistics
        vals = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0]
        est = tctd.estimate(vals)
        self.assertAlmostEqual(est["p50"], statistics.median(vals))
        # sorted: [1,1,2,3,3,4,5,5,6,9]; pos=9*0.9=8.1 -> v[8]+0.1*(v[9]-v[8])
        # = 6 + 0.1*(9-6) = 6.3 (numpy 'linear' convention)
        self.assertAlmostEqual(est["p90"], 6.3)

    def test_estimate_groups_by_key(self):
        rows = [
            {"objective": "OBJ-1", "cost_class": "small",
             "model": "m1", "attributed": True, "costUsd": 0.10},
            {"objective": "OBJ-1", "cost_class": "small",
             "model": "m1", "attributed": True, "costUsd": 0.30},
            {"objective": "OBJ-2", "cost_class": "tiny",
             "model": "m2", "attributed": True, "costUsd": 0.02},
        ]
        table = tctd.build_table(rows)
        self.assertIn(("OBJ-1", "small", "m1"), table)
        g = table[("OBJ-1", "small", "m1")]
        self.assertEqual(g["n"], 2)
        self.assertAlmostEqual(g["p50"], 0.20)
        self.assertAlmostEqual(g["p90"], 0.28)

    def test_robust_to_unattributed(self):
        rows = [{"objective": "OBJ-1", "cost_class": "small",
                 "model": "m1", "attributed": False, "costUsd": None}]
        table = tctd.build_table(rows)
        self.assertEqual(table, {})


# task-cost-estimator loaded after patching env; import into module names
tctd = _load("task_cost_estimator", HERE / "task-cost-estimator.py")

if __name__ == "__main__":
    unittest.main()
