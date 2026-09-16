#!/usr/bin/python3.12
"""test_p2_desired3_screen.py — P2 desired=3 (t_acf726e6): the morning
screen must show the backlog-guard state (board screen line).

Covers:
  1. BOARD renders the backlog-guard line when the cola-viva ledger has
     backlog-guard entries (latest wins).
  2. BOARD without any backlog-guard entry omits the line (no fake data).
  3. Screen remains hermetic: fixture home, no host reads.

Run:  /usr/bin/python3.12 test_p2_desired3_screen.py  (or pytest)
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

GOV_DIR = str(Path(__file__).resolve().parent.parent)
SCREEN_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "morning-screen.py")

_spec = importlib.util.spec_from_file_location("morning_screen", SCREEN_SCRIPT)
screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen)


def _write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _seed_ledger(home: Path, entries: list):
    _write_jsonl(home / "quota-governor" / "cola-viva.jsonl", entries)


def _seed_board(home: Path):
    db = home / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "assignee TEXT, body TEXT, completed_at REAL)")
    con.execute("INSERT INTO tasks VALUES "
                "('t_1','F0 trace','done','pr-ollama','objective:OBJ-27', 1.0)")
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2screen-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp


class TestBacklogGuardLine(Base):
    def test_board_renders_latest_backlog_guard(self):
        _seed_board(Path(self.tmp))
        _seed_ledger(Path(self.tmp), [
            {"ts": 1789001000.0, "action": "backlog-guard",
             "ready_assigned": 2, "live_workers": 1, "backlog_total": 3,
             "verdict": "OK"},
            {"ts": 1789001100.0, "action": "backlog-guard",
             "ready_assigned": 0, "live_workers": 2, "backlog_total": 2,
             "verdict": "LOW"},
        ])
        out = screen.build_board_screen(hermes_home=self.home())
        self.assertIn("backlog-guard: total=2 min=3 [LOW]", out)
        self.assertIn("desired=3", out)

    def test_board_without_guard_entry_has_no_line(self):
        _seed_board(Path(self.tmp))
        out = screen.build_board_screen(hermes_home=self.home())
        self.assertNotIn("backlog-guard", out)
        self.assertIn("BOARD", out)

    def test_empty_ledger_omits_line(self):
        _seed_board(Path(self.tmp))
        ledger = Path(self.tmp) / "quota-governor" / "cola-viva.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("")
        out = screen.build_board_screen(hermes_home=self.home())
        self.assertNotIn("backlog-guard", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
