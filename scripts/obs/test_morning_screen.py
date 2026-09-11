#!/usr/bin/python3.12
"""test_morning_screen.py — OBJ-27 F1: deterministic consumption screen.

Covers (fixtures only, no network):
  1. build_trace_screen aggregates totals, by objective, by class, by source
  2. unattributed objective is shown explicitly (the gap, not hidden)
  3. build_forecast_screen renders provider pct + ETA
  4. build_board_screen renders status counts + active tasks
  5. build_screen composes all three; empty when every source is missing
  6. privacy: no absolute host paths in the repo module (portability)

Run:  /usr/bin/python3.12 test_morning_screen.py  (or pytest)
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

GOV_DIR = str(Path(__file__).resolve().parent.parent.parent)
SCREEN_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "morning-screen.py")

_spec = importlib.util.spec_from_file_location("morning_screen", SCREEN_SCRIPT)
screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen)


def _write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="screen-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def _seed_trace(self):
        _write_jsonl(Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl", [
            {"ts_epoch_utc": 1788998186.0, "consumer_class": "worker",
             "consumer_id": "t_1", "cause": "claimed", "model": None,
             "provider": None, "tokens_in": None, "tokens_out": None,
             "costUsd": None, "requestId": None, "objective": "OBJ-27",
             "source": "task-events", "otel": {}},
            {"ts_epoch_utc": 1788998200.0, "consumer_class": "worker",
             "consumer_id": "t_1", "cause": "completed", "model": None,
             "provider": None, "tokens_in": None, "tokens_out": None,
             "costUsd": None, "requestId": None, "objective": "OBJ-27",
             "source": "task-events", "otel": {}},
            {"ts_epoch_utc": 1788998300.0, "consumer_class": "cron-llm",
             "consumer_id": "f1", "cause": "cron-fire",
             "model": "glm-5.3-flash", "provider": None,
             "tokens_in": 1000, "tokens_out": 100, "costUsd": 0.0002,
             "requestId": None, "objective": "unattributed",
             "source": "usage-audit", "otel": {}},
        ])

    def _seed_forecast(self):
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "generated_at": "2026-09-10T00:00:00Z",
            "providers": {
                "pr-ollama": {"pct_now": 52.1, "eta_90_hours": 105.86,
                              "eta_100_hours": 133.79, "confidence": 3},
                "pr-nanogpt": {"pct_now": 100.0, "eta_90_hours": None,
                               "eta_100_hours": None, "confidence": 2},
            },
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "hours_to_reset": 95.8,
        }))

    def _seed_board(self):
        db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                    "assignee TEXT, body TEXT, completed_at REAL)")
        # done 24h is wall-clock relative (the screen compares against
        # time.time()): the fixture must be relative, never a fixed epoch.
        recent = time.time() - 60
        con.execute("INSERT INTO tasks VALUES "
                    "('t_1','F0 trace','done','pr-ollama','objective:OBJ-27', ?)",
                    (recent,))
        con.execute("INSERT INTO tasks VALUES "
                    "('t_2','F5 matrix','running','pr-ollama','objective:OBJ-30 | cost:small', NULL)")
        con.execute("INSERT INTO tasks VALUES "
                    "('t_3','privacy','blocked','pr-ollama','objective:OBJ-22', NULL)")
        con.commit()
        con.close()


class TestTraceScreen(Base):
    def test_aggregates_totals_and_objectives(self):
        self._seed_trace()
        out = screen.build_trace_screen(hermes_home=self.home())
        self.assertIn("CONSUMO", out)
        self.assertIn("3 lineas", out)
        self.assertIn("OBJ-27", out)
        self.assertIn("unattributed", out)
        self.assertIn("worker=2", out)
        self.assertIn("cron-llm=1", out)
        self.assertIn("task-events=2", out)
        self.assertIn("usage-audit=1", out)

    def test_unattributed_gap_is_explicit(self):
        self._seed_trace()
        out = screen.build_trace_screen(hermes_home=self.home())
        self.assertIn("SIN ETIQUETA (hueco)", out)

    def test_empty_trace_returns_empty(self):
        self.assertEqual(screen.build_trace_screen(hermes_home=self.home()), "")


class TestForecastScreen(Base):
    def test_renders_providers_and_eta(self):
        self._seed_forecast()
        out = screen.build_forecast_screen(hermes_home=self.home())
        self.assertIn("FORECAST", out)
        self.assertIn("pr-ollama", out)
        self.assertIn("52.1%", out)
        self.assertIn("105.9h", out)
        self.assertIn("pr-nanogpt", out)
        self.assertIn("100.0%", out)
        self.assertIn("reset semanal", out)

    def test_missing_forecast_returns_empty(self):
        self.assertEqual(screen.build_forecast_screen(hermes_home=self.home()), "")


class TestBoardScreen(Base):
    def test_renders_counts_and_active(self):
        self._seed_board()
        out = screen.build_board_screen(hermes_home=self.home())
        self.assertIn("BOARD", out)
        self.assertIn("total 3", out)
        self.assertIn("done=1", out)
        self.assertIn("running=1", out)
        self.assertIn("blocked=1", out)
        self.assertIn("t_2 [running]", out)
        self.assertIn("t_3 [blocked]", out)

    def test_renders_done_24h_and_supply_ratio_placeholder(self):
        self._seed_board()
        out = screen.build_board_screen(hermes_home=self.home())
        self.assertIn("done 24h: 1", out)
        self.assertIn("t_1", out)
        self.assertIn("supply_ratio diario: n/d", out)

    def test_missing_db_returns_empty(self):
        self.assertEqual(screen.build_board_screen(hermes_home=self.home()), "")


class TestVerdict(Base):
    def test_verdict_ok_when_eta_above_margin(self):
        self._seed_forecast()
        out = screen.build_forecast_screen(hermes_home=self.home())
        self.assertIn("VEREDICTO", out)
        self.assertIn("pr-ollama: eta_90=105.9h vs reset 95.8h -> OK", out)

    def test_verdict_board_off_when_eta_under_1h(self):
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "providers": {"pr-ollama": {"pct_now": 95.0,
                                        "eta_90_hours": 0.5,
                                        "eta_100_hours": 1.0,
                                        "confidence": 3}},
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "hours_to_reset": 95.0,
        }))
        out = screen.build_forecast_screen(hermes_home=self.home())
        self.assertIn("BOARD OFF", out)

    def test_verdict_reduce_workers_when_eta_below_margin(self):
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "providers": {"pr-ollama": {"pct_now": 90.0,
                                        "eta_90_hours": 10.0,
                                        "eta_100_hours": 12.0,
                                        "confidence": 3}},
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "hours_to_reset": 20.0,
        }))
        out = screen.build_forecast_screen(hermes_home=self.home())
        self.assertIn("max_workers=1, cap cost", out)


class TestAlerts(Base):
    def test_no_anomaly_says_sin_incidencias(self):
        # trace with attributed cost (unattributed < 20%) + healthy forecast
        _write_jsonl(Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl", [
            {"ts_epoch_utc": 1788998186.0, "consumer_class": "worker",
             "consumer_id": "t_1", "cause": "claimed", "model": None,
             "provider": None, "tokens_in": None, "tokens_out": None,
             "costUsd": 0.001, "requestId": None, "objective": "OBJ-27",
             "source": "task-events", "otel": {}},
            {"ts_epoch_utc": 1788998200.0, "consumer_class": "cron-llm",
             "consumer_id": "f1", "cause": "cron-fire",
             "model": "glm-5.3-flash", "provider": None,
             "tokens_in": 1000, "tokens_out": 100, "costUsd": 0.0002,
             "requestId": None, "objective": "OBJ-27",
             "source": "usage-audit", "otel": {}},
        ])
        self._seed_forecast()
        out = screen.build_alerts_screen(hermes_home=self.home())
        self.assertIn("sin incidencias", out)

    def test_unattributed_over_threshold_alerts(self):
        # all cost is unattributed -> >20% -> alert
        self._seed_trace()
        out = screen.build_alerts_screen(hermes_home=self.home())
        self.assertIn("unattributed", out)

    def test_crash_loop_alerts(self):
        db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, "
                    "task_id TEXT, started_at REAL, outcome TEXT)")
        now = time.time()
        for i in range(3):
            con.execute("INSERT INTO task_runs (task_id, started_at, outcome) "
                        "VALUES ('t_x', ?, 'crashed')", (now - 100,))
        con.commit()
        con.close()
        out = screen.build_alerts_screen(hermes_home=self.home())
        self.assertIn("loop de crashes", out)

    def test_burn_alert(self):
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "providers": {"pr-ollama": {"pct_now": 95.0,
                                        "eta_90_hours": 0.5,
                                        "eta_100_hours": 1.0,
                                        "confidence": 3}},
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "hours_to_reset": 95.0,
        }))
        out = screen.build_alerts_screen(hermes_home=self.home())
        self.assertIn("burn pr-ollama", out)


class TestCompose(Base):
    def test_composes_all_sources(self):
        self._seed_trace()
        self._seed_forecast()
        self._seed_board()
        out = screen.build_screen(hermes_home=self.home())
        self.assertIn("CONSUMO", out)
        self.assertIn("FORECAST", out)
        self.assertIn("BOARD", out)

    def test_empty_when_all_sources_missing(self):
        self.assertEqual(screen.build_screen(hermes_home=self.home()), "")


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCREEN_SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data", Path.home().name):
            self.assertNotIn(needle, src,
                             f"host path leaked into morning-screen.py: {needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
