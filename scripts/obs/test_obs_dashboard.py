#!/usr/bin/python3.12
"""test_obs_dashboard.py — OBJ-32 v0: the consultable dashboard page.

Covers (fixtures only, no network, no server bind):
  1. build_html composes KPIs + spend tables + forecast verdicts + board
  2. the unattributed gap is explicit and red over threshold (house rule)
  3. provider_verdicts maps morning-screen's gate rule to badge states
  4. daily_series buckets local-day spend for the sparkline (old rows out)
  5. sparkline renders inline SVG; degenerate inputs never crash
  6. empty home -> banner, still valid page (watchdog-friendly)
  7. write_dashboard lands next to the trace by default
  8. privacy: no absolute host paths in the module (portability)
  9. make_server binds 127.0.0.1 ONLY and serves the generated page

Run:  /usr/bin/python3.12 test_obs_dashboard.py  (or pytest)
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
import urllib.error
import urllib.request
from pathlib import Path

GOV_DIR = os.environ.get("QUOTA_GOVERNOR_REPO",
                         str(Path(__file__).resolve().parents[2]))
DASH_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "obs-dashboard.py")

_spec = importlib.util.spec_from_file_location("obs_dashboard", DASH_SCRIPT)
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)


def _write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def seed_all(self, eta90=90.0):
        now = time.time()
        _write_jsonl(
            Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl", [
                {"ts_epoch_utc": now - 7200, "consumer_class": "worker",
                 "consumer_id": "t1", "cause": "claimed", "objective":
                     "OBJ-32", "source": "task-events", "costUsd": None},
                {"ts_epoch_utc": now - 3600, "consumer_class": "cron-llm",
                 "consumer_id": "f1", "cause": "cron-fire", "objective":
                     "unattributed", "source": "usage-audit",
                 "costUsd": 0.004},
                {"ts_epoch_utc": now - 60, "consumer_class": "worker",
                 "consumer_id": "r1", "cause": "request", "objective":
                     "unattributed", "source": "nanogpt-requests",
                 "costUsd": 0.001},
            ])
        _write_jsonl(
            Path(self.tmp) / "quota-governor" / "metrics-history.jsonl",
            [{"ts": "x", "supply_ratio": 1.61, "supply_created_24h": 21,
              "supply_closed_24h": 13, "nanogpt_balance_usd": 27.82,
              "nanogpt_budget_level": "warn"}])
        fc = {"providers": {"pr-ollama": {"pct_now": 52.6,
                                          "eta_90_hours": eta90,
                                          "confidence": 3}},
              "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
              "hours_to_reset": 81.5}
        p = Path(self.tmp) / "quota-governor" / "forecast.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(fc), encoding="utf-8")
        db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                    "assignee TEXT, body TEXT, completed_at REAL)")
        con.execute("INSERT INTO tasks VALUES "
                    "('t_9','dash','done','pr-ollama','objective:OBJ-32',?)",
                    (now - 60,))
        con.execute("INSERT INTO tasks VALUES "
                    "('t_8','live','running','pr-ollama','x',NULL)")
        con.commit()
        con.close()
        return now


class TestPage(Base):
    def test_composes_all_sections(self):
        self.seed_all()
        page = dash.build_html(hermes_home=self.home())
        for needle in ("Gasto real (trace)", "supply_ratio 24h", "1.61",
                       "Saldo NanoGPT", "27.82", "Done 24h", "t_9",
                       "Gasto — por clase", "Gasto — por objetivo",
                       "Forecast — burn y veredicto", "pr-ollama",
                       "reset semanal", "Board", "t_8", "running"):
            self.assertIn(needle, page)

    def test_verdict_ok_reaches_the_page(self):
        self.seed_all(eta90=90.0)   # 90h > 81.5h reset -> OK
        page = dash.build_html(hermes_home=self.home())
        self.assertIn(">OK</span>", page)
        self.assertNotIn("BOARD OFF", page)

    def test_gap_is_explicit_and_marked(self):
        self.seed_all()
        page = dash.build_html(hermes_home=self.home())
        self.assertIn("hueco", page)
        self.assertIn("100%", page)             # gap share in the KPI
        self.assertIn('class="gap"', page)      # red styling applied

    def test_sparkline_svg_present(self):
        self.seed_all()
        page = dash.build_html(hermes_home=self.home())
        self.assertIn("<svg", page)
        self.assertIn("polyline", page)
        self.assertNotIn("http://", page)       # self-contained, no CDN
        self.assertNotIn("<script", page)       # no JS

    def test_empty_home_shows_banner(self):
        page = dash.build_html(hermes_home=self.home())
        self.assertIn("sin fuentes", page)
        self.assertIn("</html>", page)


class TestVerdicts(unittest.TestCase):
    def _fc(self, **kw):
        return {"providers": {"p": dict(pct_now=50.0, eta_90_hours=None,
                                        **kw)}}

    def test_ok(self):
        v = dash.provider_verdicts(
            {"providers": {"p": {"pct_now": 50, "eta_90_hours": 100.0}},
             "hours_to_reset": 81.5})
        self.assertEqual(v[0]["status"], "ok")

    def test_reduce(self):
        v = dash.provider_verdicts(
            {"providers": {"p": {"pct_now": 90, "eta_90_hours": 10.0}},
             "hours_to_reset": 20.0})
        self.assertEqual(v[0]["status"], "reduce")

    def test_off(self):
        v = dash.provider_verdicts(
            {"providers": {"p": {"pct_now": 95, "eta_90_hours": 0.5}}})
        self.assertEqual(v[0]["status"], "off")

    def test_unknown_eta_still_listed(self):
        v = dash.provider_verdicts(
            {"providers": {"p": {"pct_now": 100, "eta_90_hours": None}}})
        self.assertEqual(v[0]["status"], "unknown")
        self.assertEqual(v[0]["name"], "p")

    def test_summary_lines_skipped(self):
        v = dash.provider_verdicts(
            {"providers": {"p": {"pct_now": 50, "eta_90_hours": 100.0}},
             "hours_to_reset": 81.5})
        self.assertEqual([x["name"] for x in v], ["p"])


class TestSeries(unittest.TestCase):
    def test_daily_series_buckets_local_days(self):
        now = time.time()
        rows = [{"ts_epoch_utc": now, "costUsd": 0.5},
                {"ts_epoch_utc": now - 86400 * 20, "costUsd": 9.0},
                {"ts_epoch_utc": None, "costUsd": 1.0}]
        s = dash.daily_series(rows, now=now)
        self.assertEqual(len(s), dash.DAYS)
        self.assertAlmostEqual(s[-1][1], 0.5)
        self.assertEqual(s[0][1], 0.0)  # 20-day-old row is outside the window

    def test_sparkline_edges(self):
        self.assertEqual(dash.sparkline([]), "")
        self.assertIn("polyline", dash.sparkline([0.0]))
        self.assertIn("polyline", dash.sparkline([5.0]))
        self.assertIn("polyline", dash.sparkline(["x", None, 3]))


class TestWrite(Base):
    def test_default_path_next_to_trace(self):
        now = self.seed_all()
        p = dash.write_dashboard(hermes_home=self.home(), now=now)
        self.assertEqual(p, Path(self.tmp) / "quota-governor" / "obs"
                         / "dashboard.html")
        self.assertIn("<!doctype html>", p.read_text(encoding="utf-8"))

    def test_out_override(self):
        now = self.seed_all()
        target = Path(self.tmp) / "elsewhere" / "page.html"
        p = dash.write_dashboard(hermes_home=self.home(), out=target,
                                 now=now)
        self.assertEqual(p, target)
        self.assertTrue(target.exists())


class TestServer(Base):
    def test_binds_localhost_and_serves(self):
        self.seed_all()
        srv = dash.make_server(0, hermes_home=self.home())  # ephemeral port
        port = srv.server_address[1]
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        import threading
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5) as r:
                body = r.read().decode("utf-8")
                self.assertEqual(r.status, 200)
                self.assertIn("La casa — observabilidad", body)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/nope", timeout=5)
                raised = False
            except urllib.error.HTTPError as e:
                raised = e.code == 404
            self.assertTrue(raised)
        finally:
            srv.shutdown()
            srv.server_close()


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(DASH_SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data", "host"):
            self.assertNotIn(needle, src,
                             f"host path leaked into obs-dashboard.py: "
                             f"{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
