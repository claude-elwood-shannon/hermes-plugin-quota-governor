#!/usr/bin/env python3
"""Tests for scripts/weekly-progress.py (OBJ-08).

Covers:
  - §4 status derivation: not_started / in_progress / complete /
    awaiting_human_verification (OBJ-01) / needs_attention (lost tasks)
  - archived-with-completed_at counts as done (board-cleanup convention);
    archived-without-completed_at does NOT
  - header-only tag parsing (prose mentions below the header are not links)
  - idempotency: re-running --execute is byte-identical modulo the timestamp
    and never duplicates entries
  - dry-run writes nothing
  - weekly window classification (--week 2026-37) + report content
  - quota rollup from model-cost-ledger + latest burn-ledger window % per
    provider; tolerant of a torn/invalid JSONL line
  - missing/unreadable kanban DB -> exit 1, no partial writes

Run:
  /usr/bin/python3.12 -m pytest test_weekly_progress.py -v
  /usr/bin/python3.12 test_weekly_progress.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "weekly_progress", os.path.join(SCRIPT_DIR, "scripts", "weekly-progress.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["weekly_progress"] = _mod
_spec.loader.exec_module(_mod)

import weekly_progress as wp  # noqa: E402

UTC = timezone.utc


def _ts(y, mo, d, h=12):
    return int(datetime(y, mo, d, h, tzinfo=UTC).timestamp())


def _make_db(path: Path, tasks):
    """tasks: list of (id, status, header, completed_at|None, created_at, started_at)."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, status TEXT, body TEXT,
        created_at INTEGER, started_at INTEGER, completed_at INTEGER)""")
    for tid, status, header, ca, cr, sa in tasks:
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (tid, f"title {tid}", status, f"{header}\n\n## Cuerpo\nmenciona "
             f"objective:OBJ-99 en prosa (no debe contar)", cr, sa, ca))
    conn.commit()
    conn.close()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "kanban.db"
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.reports = self.tmp / "reports"
        self.cost = self.state / "model-cost-ledger.jsonl"
        self.burn = self.state / "burn-ledger.jsonl"
        self.doc = self.tmp / "objectives.md"
        self.doc.write_text(
            "## 2. Objetivos\n\n"
            "### OBJ-01: Verificar loop\n\n### OBJ-02: Costes\n\n"
            "### OBJ-08: Pipeline\n\n### OBJ-19: Privacidad\n",
            encoding="utf-8")
        # Override module-level paths (main() resolves globals at call time).
        self._orig = {}
        for name, val in (("KANBAN_DB", self.db), ("STATE_DIR", self.state),
                          ("PROGRESS_FILE", self.state / "objective-progress.json"),
                          ("REPORT_DIR", self.reports), ("COST_LEDGER", self.cost),
                          ("BURN_LEDGER", self.burn), ("OBJECTIVES_DOC", self.doc)):
            self._orig[name] = getattr(wp, name)
            setattr(wp, name, val)

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(wp, name, val)

    def read_progress(self):
        return json.loads(wp.PROGRESS_FILE.read_text(encoding="utf-8"))


class TestStatusDerivation(Sandbox):
    def _load(self, tasks):
        _make_db(self.db, tasks)
        return wp.load_task_objectives(self.db)

    def test_complete_vs_lost_vs_open(self):
        by = self._load([
            ("t_1", "done", "objective:OBJ-02, cost:small", _ts(2026, 9, 2), _ts(2026, 9, 1), _ts(2026, 9, 2)),
            ("t_2", "archived", "objective:OBJ-02", _ts(2026, 9, 3), _ts(2026, 8, 20), _ts(2026, 9, 3)),   # was done, cleaned
            ("t_3", "running", "objective:OBJ-08", None, _ts(2026, 9, 7), _ts(2026, 9, 8)),
            ("t_4", "archived", "objective:OBJ-03", None, _ts(2026, 8, 1), None),                            # gave_up -> lost
            ("t_5", "done", "objective:OBJ-01, cost:tiny", _ts(2026, 9, 4), _ts(2026, 9, 1), _ts(2026, 9, 4)),
        ])
        prog = wp.build_progress(by, wp.declared_objectives(self.doc))
        self.assertEqual(prog["OBJ-02"]["status"], "complete")
        self.assertEqual(prog["OBJ-02"]["tasks_completed"], 2)  # done + archived-with-ts
        self.assertEqual(prog["OBJ-08"]["status"], "in_progress")
        self.assertEqual(prog["OBJ-08"]["tasks_open"], 1)
        self.assertEqual(prog["OBJ-03"]["status"], "needs_attention")
        self.assertEqual(prog["OBJ-03"]["tasks_lost"], 1)
        # §4: OBJ-01 needs human verification even with all tasks done
        self.assertEqual(prog["OBJ-01"]["status"], "awaiting_human_verification")
        # declared in doc but zero tagged tasks
        self.assertEqual(prog["OBJ-19"]["status"], "not_started")
        self.assertEqual(prog["OBJ-19"]["tasks_total"], 0)

    def test_header_only_tagging(self):
        # header tag counts, prose mention below the blank line does not:
        # _make_db puts "objective:OBJ-99" in every body's prose section.
        by = self._load([
            ("t_1", "done", "objective:OBJ-02, cost:small", _ts(2026, 9, 2), _ts(2026, 9, 1), None),
        ])
        self.assertIn("OBJ-02", by)
        self.assertNotIn("OBJ-99", by)

    def _load_simple(self, tasks):
        _make_db(self.db, tasks)
        return wp.load_task_objectives(self.db)

    def test_prose_mention_not_a_link(self):
        by = self._load_simple([
            ("t_1", "done", "cost:small  # sin tag objetivo en header", _ts(2026, 9, 2), _ts(2026, 9, 1), None),
        ])
        # body prose says objective:OBJ-99 but header has no tag -> untracked
        self.assertNotIn("OBJ-99", by)

    def test_placeholder_objx_ignored(self):
        _make_db(self.db, [("t_1", "done", "objective:OBJ-X", _ts(2026, 9, 2), _ts(2026, 9, 1), None)])
        self.assertEqual(wp.load_task_objectives(self.db), {})

    def test_last_task_created_iso(self):
        by = self._load_simple([
            ("t_1", "done", "objective:OBJ-02", _ts(2026, 9, 3), _ts(2026, 9, 1, 8), _ts(2026, 9, 2)),
            ("t_2", "done", "objective:OBJ-02", _ts(2026, 9, 5), _ts(2026, 9, 2, 8), _ts(2026, 9, 4)),
        ])
        prog = wp.build_progress(by, [])
        self.assertEqual(prog["OBJ-02"]["last_task_created"], "2026-09-02T08:00:00Z")


class TestCli(Sandbox):
    def _seed(self):
        _make_db(self.db, [
            ("t_1", "done", "objective:OBJ-02", _ts(2026, 9, 8), _ts(2026, 9, 7), _ts(2026, 9, 8)),
            ("t_2", "running", "objective:OBJ-08", None, _ts(2026, 9, 7), _ts(2026, 9, 8)),
        ])
        self.cost.write_text(
            json.dumps({"ts": _ts(2026, 9, 8), "profile": "pr-opencode",
                        "model": "qwen3.8-flash", "cost": 0.5}) + "\n"
            + "{torn line trunc\n"  # invalid -> must be skipped, not crash
            + json.dumps({"ts": _ts(2026, 8, 1), "profile": "pr-opencode",
                          "model": "qwen3.8-flash", "cost": 99}) + "\n",
            encoding="utf-8")
        self.burn.write_text(
            json.dumps({"provider": "opencode-go", "window": "weekly",
                        "window_pct": 40.0, "ts": _ts(2026, 9, 7)}) + "\n"
            + json.dumps({"provider": "opencode-go", "window": "weekly",
                          "window_pct": 53.0, "ts": _ts(2026, 9, 8)}) + "\n"
            + json.dumps({"provider": "ollama-cloud", "window": "weekly",
                          "window_pct": 23.6, "ts": _ts(2026, 9, 8)}) + "\n",
            encoding="utf-8")

    def test_dry_run_writes_nothing(self):
        self._seed()
        rc = wp.main(["--week", "2026-37"])
        self.assertEqual(rc, 0)
        self.assertFalse(wp.PROGRESS_FILE.exists())
        self.assertFalse(self.reports.exists())

    def test_execute_idempotent_and_content(self):
        self._seed()
        rc = wp.main(["--execute", "--week", "2026-37"])
        self.assertEqual(rc, 0)
        first = wp.PROGRESS_FILE.read_text(encoding="utf-8")
        rep = self.reports / "2026-37-weekly-summary.md"
        self.assertTrue(rep.exists())
        rc = wp.main(["--execute", "--week", "2026-37"])
        self.assertEqual(rc, 0)
        second = wp.PROGRESS_FILE.read_text(encoding="utf-8")
        # byte-identical modulo the generation timestamp; no duplicated keys
        strip = lambda s: s.split('"generated_at"')[0] + s.split('"generated_at"')[1].split("\n", 1)[1]
        self.assertEqual(strip(first), strip(second))
        doc = json.loads(second)
        objs = [k for k in doc if k.startswith("OBJ-")]
        self.assertEqual(len(objs), len(set(objs)))
        self.assertEqual(doc["OBJ-02"]["status"], "complete")
        self.assertEqual(doc["OBJ-08"]["status"], "in_progress")
        text = rep.read_text(encoding="utf-8")
        self.assertIn("2026-W37", text)
        self.assertIn("t_1", text)          # completed in window
        self.assertNotIn("t_2 —", text)     # running task not listed as done
        self.assertIn("$0.50", text)        # only in-window cost
        self.assertNotIn("$99", text)
        self.assertIn("opencode-go: 53.0%", text)   # latest tick per provider
        self.assertIn("ollama-cloud: 23.6%", text)

    def test_bad_week_arg(self):
        self._seed()
        self.assertEqual(wp.main(["--week", "37"]), 2)

    def test_missing_db_exits_1(self):
        rc = wp.main(["--execute", "--week", "2026-37"])
        self.assertEqual(rc, 1)
        self.assertFalse(wp.PROGRESS_FILE.exists())

    def test_progress_only_skips_report(self):
        self._seed()
        rc = wp.main(["--execute", "--progress-only", "--week", "2026-37"])
        self.assertEqual(rc, 0)
        self.assertTrue(wp.PROGRESS_FILE.exists())
        self.assertEqual(list(self.reports.glob("*")) if self.reports.exists() else [], [])


class TestWeekWindow(Sandbox):
    def test_boundaries(self):
        y, w, start, end = wp.parse_week("2026-37", datetime(2026, 9, 8, tzinfo=UTC))
        self.assertEqual((y, w), (2026, 37))
        self.assertEqual(start.isoformat(), "2026-09-07T00:00:00+00:00")  # Monday
        self.assertEqual((end - start).days, 7)

    def test_completed_outside_window_not_listed(self):
        _make_db(self.db, [
            ("t_old", "done", "objective:OBJ-02", _ts(2026, 8, 30), _ts(2026, 8, 29), _ts(2026, 8, 30)),
        ])
        by = wp.load_task_objectives(self.db)
        prog = wp.build_progress(by, [])
        _, _, start, end = wp.parse_week("2026-37", datetime(2026, 9, 8, tzinfo=UTC))
        secs = wp.week_sections(by, prog, start, end)
        self.assertEqual(secs["done_window"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
