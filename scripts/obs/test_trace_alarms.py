#!/usr/bin/python3.12
"""test_trace_alarms.py — OBJ-27 F2: consumption alarms over the trace.

Covers (fixtures only, no network):
  1. crash loop: fires only with >=3 claims and zero completions in 24h
  2. crash loop: retries that completed are NOT a loop; old events and
     other sources do not count
  3. burn: eta_90 < 1h (board off) and eta_90 < reset-2h (reduce workers)
  4. burn: healthy margin and exhausted providers stay silent
  5. unattributed: >20% of spend alarms; healthy share and zero total are
     silent
  6. run_checks composes all three; empty stdout when sources are missing
     or healthy (watchdog pattern); corrupted sources stay silent
  7. privacy: no absolute host paths in the repo module (portability)

Run:  /usr/bin/python3.12 test_trace_alarms.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3  # noqa: F401  (kept for fixture parity with F1 tests)
import sys
import tempfile
import time
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent.parent.parent)
ALARMS_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "trace-alarms.py")

_spec = importlib.util.spec_from_file_location("trace_alarms", ALARMS_SCRIPT)
alarms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alarms)

NOW = 1789001175.0  # fixed epoch: tests are deterministic, never wall-clock


def _trace_row(ts, source="task-events", cause="claimed", tid="t_x",
               costUsd=None, objective="OBJ-27"):
    return {"ts_epoch_utc": ts, "consumer_class": "worker",
            "consumer_id": tid, "cause": cause, "model": None,
            "provider": None, "tokens_in": None, "tokens_out": None,
            "costUsd": costUsd, "requestId": None, "objective": objective,
            "source": source, "otel": {}}


def _trace_event(ts, cause, tid):
    return _trace_row(ts, source="task-events", cause=cause, tid=tid)


def _fc(providers, hours_to_reset=95.0):
    return {"generated_at": "2026-09-10T00:00:00Z",
            "providers": providers,
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "hours_to_reset": hours_to_reset}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alarms-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def _write_trace(self, rows):
        path = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _write_forecast(self, fc):
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fc), encoding="utf-8")


class TestCrashLoop(Base):
    def test_fires_at_three_claims_without_completed(self):
        rows = [_trace_event(NOW - 3600 * i, "claimed", "t_loop")
                for i in range(1, 4)]
        self._write_trace(rows)
        out = alarms.check_crash_loop(rows, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("ALARM crash-loop", out[0])
        self.assertIn("t_loop", out[0])
        self.assertIn("3 claimed", out[0])

    def test_two_claims_do_not_fire(self):
        rows = [_trace_event(NOW - 3600 * i, "claimed", "t_ok")
                for i in range(1, 3)]
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_completed_inside_window_is_not_a_loop(self):
        rows = [_trace_event(NOW - 3600 * i, "claimed", "t_retry")
                for i in range(1, 5)]
        rows.append(_trace_event(NOW - 1800, "completed", "t_retry"))
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_old_claims_outside_window_do_not_fire(self):
        # 3 claims but 2 days old -> outside the 24h window
        rows = [_trace_event(NOW - 86400 - 3600 * i, "claimed", "t_old")
                for i in range(1, 4)]
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_events_without_timestamp_are_excluded(self):
        rows = [_trace_event(None, "claimed", "t_nots") for _ in range(4)]
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_non_task_events_sources_do_not_count(self):
        rows = [_trace_row(NOW - 600, source="usage-audit",
                           cause="cron-fire", tid="f1")
                for _ in range(4)]
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_future_events_do_not_count(self):
        rows = [_trace_event(NOW + 400, "claimed", "t_future")
                for _ in range(3)]
        self.assertEqual(alarms.check_crash_loop(rows, now=NOW), [])

    def test_multiple_loops_are_one_line_each(self):
        rows = []
        for tid in ("t_a", "t_b"):
            rows += [_trace_event(NOW - 3600 * i, "claimed", tid)
                     for i in range(1, 4)]
        out = alarms.check_crash_loop(rows, now=NOW)
        self.assertEqual(len(out), 2)


class TestBurn(Base):
    def test_board_off_under_one_hour(self):
        fc = _fc({"pr-ollama": {"pct_now": 95.0, "eta_90_hours": 0.5,
                                "eta_100_hours": 1.0, "confidence": 3}})
        out = alarms.check_burn(fc)
        self.assertEqual(len(out), 1)
        self.assertIn("ALARM burn pr-ollama", out[0])
        self.assertIn("board off", out[0])

    def test_reduce_workers_below_reset_margin(self):
        # eta_90=10h < reset 20h - 2h colchon -> reduce workers
        fc = _fc({"pr-ollama": {"pct_now": 90.0, "eta_90_hours": 10.0,
                                "eta_100_hours": 12.0, "confidence": 3}},
                 hours_to_reset=20.0)
        out = alarms.check_burn(fc)
        self.assertEqual(len(out), 1)
        self.assertIn("reducir workers", out[0])

    def test_healthy_margin_stays_silent(self):
        # eta_90=105.9h vs reset 95.8h -> OK (live-calibrated case)
        fc = _fc({"pr-ollama": {"pct_now": 52.1, "eta_90_hours": 105.86,
                                "eta_100_hours": 133.79, "confidence": 3}},
                 hours_to_reset=95.8)
        self.assertEqual(alarms.check_burn(fc), [])

    def test_eta_just_at_margin_stays_silent(self):
        # eta_90 == reset - 2h exactly -> NOT below the margin
        fc = _fc({"pr-x": {"eta_90_hours": 18.0}}, hours_to_reset=20.0)
        self.assertEqual(alarms.check_burn(fc), [])

    def test_exhausted_provider_is_skipped(self):
        fc = _fc({"pr-nanogpt": {"pct_now": 100.0, "eta_90_hours": None,
                                 "eta_100_hours": None, "confidence": 2}})
        self.assertEqual(alarms.check_burn(fc), [])

    def test_negative_eta_is_skipped(self):
        fc = _fc({"pr-x": {"eta_90_hours": -1.0}})
        self.assertEqual(alarms.check_burn(fc), [])

    def test_without_reset_only_board_off_rule_applies(self):
        fc = {"providers": {"pr-x": {"eta_90_hours": 5.0}}}
        self.assertEqual(alarms.check_burn(fc), [])
        fc = {"providers": {"pr-x": {"eta_90_hours": 0.5}}}
        self.assertEqual(len(alarms.check_burn(fc)), 1)


class TestUnattributed(Base):
    def test_over_twenty_percent_alarms(self):
        rows = [_trace_row(NOW - 100, source="nanogpt-requests",
                           cause="request", tid="req_1", costUsd=0.80,
                           objective="unattributed"),
                _trace_row(NOW - 90, source="task-events", cause="claimed",
                           tid="t_1", costUsd=0.20, objective="OBJ-27")]
        out = alarms.check_unattributed(rows)
        self.assertEqual(len(out), 1)
        self.assertIn("ALARM unattributed", out[0])
        self.assertIn("80.0%", out[0])

    def test_healthy_share_stays_silent(self):
        rows = [_trace_row(NOW - 100, source="nanogpt-requests",
                           cause="request", tid="req_1", costUsd=0.10,
                           objective="unattributed"),
                _trace_row(NOW - 90, source="task-events", cause="claimed",
                           tid="t_1", costUsd=0.90, objective="OBJ-27")]
        self.assertEqual(alarms.check_unattributed(rows), [])

    def test_exactly_twenty_percent_stays_silent(self):
        rows = [_trace_row(NOW - 100, source="nanogpt-requests",
                           cause="request", tid="req_1", costUsd=0.2,
                           objective="unattributed"),
                _trace_row(NOW - 90, source="task-events", cause="claimed",
                           tid="t_1", costUsd=0.8, objective="OBJ-27")]
        self.assertEqual(alarms.check_unattributed(rows), [])

    def test_zero_total_spend_is_silent(self):
        rows = [_trace_row(NOW - 100, cause="claimed", tid="t_1",
                           costUsd=None)]
        self.assertEqual(alarms.check_unattributed(rows), [])

    def test_empty_trace_is_silent(self):
        self.assertEqual(alarms.check_unattributed([]), [])


class TestRunChecks(Base):
    def test_all_sources_missing_is_silent(self):
        self.assertEqual(alarms.run_checks(hermes_home=self.home(),
                                           now=NOW), [])

    def test_healthy_sources_are_silent(self):
        self._write_trace([
            _trace_event(NOW - 7200, "claimed", "t_1"),
            _trace_event(NOW - 3600, "completed", "t_1"),
            _trace_row(NOW - 600, source="nanogpt-requests",
                       cause="request", tid="req_1", costUsd=0.90,
                       objective="OBJ-27"),
            _trace_row(NOW - 500, source="usage-audit", cause="cron-fire",
                       tid="f1", costUsd=0.10, objective="unattributed"),
        ])
        self._write_forecast(_fc({"pr-ollama": {
            "pct_now": 52.1, "eta_90_hours": 105.86,
            "eta_100_hours": 133.79, "confidence": 3}}))
        self.assertEqual(alarms.run_checks(hermes_home=self.home(),
                                           now=NOW), [])

    def test_fires_each_alarm_once_when_all_anomalies_present(self):
        rows = [_trace_event(NOW - 3600 * i, "claimed", "t_loop")
                for i in range(1, 4)]
        rows += [_trace_row(NOW - 600, source="nanogpt-requests",
                            cause="request", tid="req_1", costUsd=0.80,
                            objective="unattributed"),
                 _trace_row(NOW - 500, source="task-events", cause="claimed",
                            tid="t_2", costUsd=0.20, objective="OBJ-27")]
        self._write_trace(rows)
        self._write_forecast(_fc({"pr-ollama": {
            "pct_now": 95.0, "eta_90_hours": 0.5, "eta_100_hours": 1.0,
            "confidence": 3}}))
        out = alarms.run_checks(hermes_home=self.home(), now=NOW)
        joined = "\n".join(out)
        self.assertIn("ALARM crash-loop", joined)
        self.assertIn("ALARM burn pr-ollama", joined)
        self.assertIn("ALARM unattributed", joined)
        self.assertEqual(len(out), 3)

    def test_corrupt_trace_stays_silent(self):
        path = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._write_forecast(_fc({"pr-x": {"eta_90_hours": 105.0}}))
        self.assertEqual(alarms.run_checks(hermes_home=self.home(),
                                           now=NOW), [])

    def test_corrupt_forecast_stays_silent(self):
        self._write_trace([_trace_event(NOW - 600, "claimed", "t_1")])
        path = Path(self.tmp) / "quota-governor" / "forecast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(alarms.run_checks(hermes_home=self.home(),
                                           now=NOW), [])

    def test_main_prints_one_line_per_alarm(self):
        rows = [_trace_event(NOW - 3600 * i, "claimed", "t_loop")
                for i in range(1, 4)]
        self._write_trace(rows)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = alarms.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().count("ALARM crash-loop"), 1)

    def test_main_silent_when_healthy(self):
        import io
        import contextlib
        self._write_trace([_trace_event(NOW - 600, "claimed", "t_1")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = alarms.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(ALARMS_SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data/git", "host"):
            self.assertNotIn(needle, src,
                             f"host path leaked into trace-alarms.py: "
                             f"{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
