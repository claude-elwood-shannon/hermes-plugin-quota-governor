#!/usr/bin/env python3
"""Tests for usd-stats.py (t_e3d17323): attribution, tag join, rendering.

Uses a temp kanban DB + temp ledger built to exercise:
  - session→run window join (before/after tolerance, profile match,
    nearest-run-wins)
  - per-row worker/aux split within one run
  - cron_* sessions kept OUT of task attribution
  - cost tag join via body_header_cost_tag (line + pipe styles, missing →
    untagged)
  - done vs crashed classification
  - markdown rendering sanity + JSON output

Run:
  python3 test_usd_stats.py
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
from datetime import datetime, timezone
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "usd_stats_t", os.path.join(HERE, "scripts", "usd-stats.py"))
usd = importlib.util.module_from_spec(_spec)
sys.modules["usd_stats_t"] = usd
_spec.loader.exec_module(usd)

# Real body_header_cost_tag (sibling proposer, same import pattern the
# other test files in this repo use).
_prop_spec = importlib.util.spec_from_file_location(
    "objective_proposer_usd_t",
    os.path.join(HERE, "scripts", "objective-proposer.py"))
_proposer_mod = importlib.util.module_from_spec(_prop_spec)
sys.modules["objective_proposer_usd_t"] = _proposer_mod
_prop_spec.loader.exec_module(_proposer_mod)

_run_id = [0]


def make_db(tmp: str, tasks, runs) -> str:
    """tasks: (id, body); runs: dict kwargs for task_runs row."""
    path = os.path.join(tmp, "kanban.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT, profile TEXT, status TEXT, outcome TEXT, "
        "started_at INTEGER, ended_at INTEGER);")
    conn.executemany("INSERT INTO tasks (id, body) VALUES (?, ?)", tasks)
    for r in runs:
        _run_id[0] += 1
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
            (_run_id[0], r["task_id"], r["profile"], r.get("status", "done"),
             r.get("outcome", "completed"), r["started_at"],
             r.get("ended_at")))
    conn.commit()
    conn.close()
    return path


def make_ledger(tmp: str, rows) -> str:
    path = os.path.join(tmp, "ledger.jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


class TestUsdStats(unittest.TestCase):
    def setUp(self):
        usd._proposer = _proposer_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    # ── attribution ───────────────────────────────────────────────────────

    def test_session_joins_matching_window(self):
        t0 = self.now - 3600
        db = make_db(self.tmp.name, [("t_a", "cost:tiny\n\nB\n")],
                     [dict(task_id="t_a", profile="pr-x", started_at=t0,
                           ended_at=t0 + 600)])
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.5,
                 request_count=10, session_id=_sid(t0 + 5), task="")])
        rows = usd.load_ledger(ledger, self.now - 7200, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(len(misses), 0)
        self.assertEqual(len(cron), 0)

    def test_profile_mismatch_misses(self):
        t0 = self.now - 3600
        db = make_db(self.tmp.name, [("t_a", "cost:tiny\n\nB\n")],
                     [dict(task_id="t_a", profile="pr-x", started_at=t0,
                           ended_at=t0 + 600)])
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-y", model="m1", cost=0.5,
                 request_count=10, session_id=_sid(t0 + 5), task="")])
        rows = usd.load_ledger(ledger, self.now - 7200, self.now + 60, [])
        runs, _ = usd.load_runs_and_tags(db)
        buckets, misses, _ = usd.attribute(rows, runs)
        self.assertEqual(len(buckets), 0)
        self.assertEqual(len(misses), 1)

    def test_nearest_run_wins(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [("t_a", "cost:tiny\n\nB\n")],
                     [dict(task_id="t_a", profile="pr-x", started_at=base,
                           ended_at=base + 600),
                      dict(task_id="t_a", profile="pr-x", started_at=base + 3600,
                           ended_at=base + 4200)])
        # session closer to the second run's start
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=1.0,
                 request_count=5, session_id=_sid(base + 3600 + 30), task="")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, _ = usd.load_runs_and_tags(db)
        buckets, _, _ = usd.attribute(rows, runs)
        self.assertEqual(list(buckets.keys()),
                         [max(buckets.keys())])  # only the nearer run gets the row

    def test_cron_sessions_never_attributed(self):
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.11,
                 request_count=36, session_id="cron_0b9a2b17116f_20260907_024951",
                 task="")])
        rows = usd.load_ledger(ledger, self.now - 7200, self.now + 60, [])
        runs, _ = usd.load_runs_and_tags(make_db(self.tmp.name, [], []))
        buckets, misses, cron = usd.attribute(rows, runs)
        self.assertEqual(len(cron), 1)
        self.assertEqual(len(buckets), 0)
        self.assertEqual(len(misses), 0)

    def test_worker_aux_split_within_one_run(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [("t_a", "cost:small\n\nB\n")],
                     [dict(task_id="t_a", profile="pr-x", started_at=base,
                           ended_at=base + 600)])
        sid = _sid(base + 5)
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.30,
                 request_count=20, session_id=sid, task=""),
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.07,
                 request_count=3, session_id=sid, task="approval"),
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.0002,
                 request_count=2, session_id=sid, task="title_generation")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        report = usd.build_report(buckets, runs, tags, misses, cron, len(rows))
        # worker rows only → one run, $0.30, 20 calls; aux separate
        self.assertEqual(report["ledger_rows_attributed_worker"], 1)
        self.assertEqual(report["ledger_rows_attributed_aux"], 2)
        self.assertEqual(report["categories"]["small"]["done"]["n"], 1)
        self.assertAlmostEqual(
            report["categories"]["small"]["done"]["usd_per_task"]["mean"], 0.30)
        self.assertIn("approval", report["aux_calls"])

    # ── task tag join ─────────────────────────────────────────────────────

    def test_tag_join_line_and_pipe(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [
            ("t_line", "cost:medium\n\nB\n"),
            ("t_pipe", "objective:OBJ-2 | auto_created:true | cost:tiny | model:fast\n\nB\n")],
            [dict(task_id="t_line", profile="pr-x", started_at=base,
                  ended_at=base + 60),
             dict(task_id="t_pipe", profile="pr-x", started_at=base + 3600,
                  ended_at=base + 3660)])
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=1.0,
                 request_count=9, session_id=_sid(base + 5), task=""),
            dict(ts=self.now, profile="pr-x", model="m1", cost=2.0,
                 request_count=8, session_id=_sid(base + 3600 + 5), task="")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        report = usd.build_report(buckets, runs, tags, misses, cron, len(rows))
        cats = report["categories"]
        self.assertEqual(cats["medium"]["done"]["n"], 1)
        self.assertEqual(cats["medium"]["done"]["usd_per_task"]["mean"], 1.0)
        self.assertEqual(cats["tiny"]["done"]["n"], 1)
        self.assertEqual(cats["tiny"]["done"]["usd_per_task"]["mean"], 2.0)
        self.assertNotIn("untagged", cats)

    def test_missing_tag_counts_as_untagged(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [("t_u", "no tags here\n\nB\n")],
                     [dict(task_id="t_u", profile="pr-x", started_at=base,
                           ended_at=base + 60)])
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.4,
                 request_count=3, session_id=_sid(base + 5), task="")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        report = usd.build_report(buckets, runs, tags, misses, cron, len(rows))
        self.assertEqual(report["categories"]["untagged"]["done"]["n"], 1)

    def test_crashed_vs_done(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [("t_c", "cost:small\n\nB\n")],
                     [dict(task_id="t_c", profile="pr-x", status="crashed",
                           outcome="crashed", started_at=base,
                           ended_at=base + 60)])
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.2,
                 request_count=4, session_id=_sid(base + 5), task="")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        report = usd.build_report(buckets, runs, tags, misses, cron, len(rows))
        self.assertEqual(report["categories"]["small"]["done"]["n"], 0)
        self.assertEqual(report["categories"]["small"]["crashed"]["n"], 1)

    # ── output ───────────────────────────────────────────────────────────

    def test_render_and_json(self):
        base = self.now - 7200
        db = make_db(self.tmp.name, [("t_a", "cost:tiny\n\nB\n")],
                     [dict(task_id="t_a", profile="pr-x", started_at=base,
                           ended_at=base + 60)])
        sid = _sid(base + 5)
        ledger = make_ledger(self.tmp.name, [
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.1,
                 request_count=5, session_id=sid, task=""),
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.02,
                 request_count=2, session_id=sid, task="approval"),
            dict(ts=self.now, profile="pr-x", model="m1", cost=0.3,
                 request_count=7, session_id="cron_j_20260907_010203", task="")])
        rows = usd.load_ledger(ledger, base - 1000, self.now + 60, [])
        runs, tags = usd.load_runs_and_tags(db)
        buckets, misses, cron = usd.attribute(rows, runs)
        report = usd.build_report(buckets, runs, tags, misses, cron, len(rows))
        self.assertEqual(report["ledger_rows_cron"], 1)
        self.assertEqual(report["cron_calls"], 7)
        md = usd.render_markdown(report)
        self.assertIn("tiny", md)
        self.assertIn("USD/call", md)
        self.assertIn("approval", md)
        self.assertIn("cron", md)


def _sid(ts: float) -> str:
    st = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
    return f"{st}_aabbcc"


if __name__ == "__main__":
    unittest.main(verbosity=2)
