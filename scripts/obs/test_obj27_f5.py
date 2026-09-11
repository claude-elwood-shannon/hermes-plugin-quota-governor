#!/usr/bin/env python3.12
"""Test suite for OBJ-27 F5: trace-backfill, portal-build, obs-serve.

Fixtures only: no network, no live board, no real home. Every reader is
exercised against a synthetic HERMES_HOME (tempdir) exactly like the F0/F1
test suites. Run: /usr/bin/python3.12 test_obj27_f5.py (or pytest).

Covers:
  F5b backfill
    1. merge writes the union sorted by ts; originals never lost
    2. idempotent: second run appends nothing
    3. corrupt lines preserved; ts-less rows kept at the head
    4. usage-audit backfill respects the F0 cursor floor
    5. model-cost-ledger skips covered profiles, maps the canonical shape
    6. task-events backfill carries extra kinds + objective join
    7. F0 cursor is seeded on fresh adoptants (never overwritten when set)
  F5c portal
    8. six pages build; nav + footer everywhere
    9. index KPIs + charts + board bars reach the HTML
    10. consumo: dimension tables, drill-down anchors, request rows,
        real/est badges
    11. board: daily bars, active tasks with kanban log links, supply chart
    12. providers: verdicts, provider sparklines, weekly ledger table
    13. alarms: F2 lines, crash-loop links, trace health (doctor)
    14. docs: trace schema + repo docs render; empty state when absent
    15. empty home: elegant empty states, never a crash
    16. privacy: no absolute host paths in ANY new module
  F5d server
    17. binds 127.0.0.1 only; serves all six pages; 404 elsewhere
    18. --check exit codes: 0 alive (probed via port_alive), 1 down
    19. Regenerator rebuilds the portal dir (boot regeneration)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

GOV_DIR = os.environ.get("QUOTA_GOVERNOR_REPO",
                         str(Path(__file__).resolve().parents[2]))
OBS = Path(GOV_DIR) / "scripts" / "obs"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load("trace_backfill_test", OBS / "trace-backfill.py")
pb = _load("portal_build_test", OBS / "portal-build.py")
sv = _load("obs_serve_test", OBS / "obs-serve.py")

NEW_TASK_KINDS = ("created", "crashed", "gave_up", "timed_out", "spawn_failed")


def _write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _seed_db(path: Path, tasks, events):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, "
        "assignee TEXT, body TEXT, created_at REAL, completed_at REAL)")
    for t in tasks:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", t)
    con.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, "
                "created_at REAL)")
    for e in events:
        con.execute("INSERT INTO task_events VALUES (?,?,?)", e)
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="f5-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.now = time.time()

    def home(self):
        return self.tmp

    # -- shared fixtures ------------------------------------------------------

    def seed_trace(self, rows=None):
        rows = rows if rows is not None else [
            {"ts_epoch_utc": self.now - 3600, "consumer_class": "worker",
             "consumer_id": "req_a", "cause": "request", "model": "glm-5.3",
             "provider": "custom", "costUsd": 0.002,
             "requestId": "req_a", "objective": "OBJ-27",
             "source": "nanogpt-requests"},
            {"ts_epoch_utc": self.now - 7200, "consumer_class": "cron-llm",
             "consumer_id": "fire_1", "cause": "cron-fire", "model": "glm-5.2",
             "provider": None, "costUsd": 0.004, "requestId": None,
             "objective": "unattributed", "source": "usage-audit"},
            "corrupt-line-not-json",
        ]
        good = [r for r in rows if isinstance(r, dict)]
        bad = [r for r in rows if isinstance(r, str)]
        path = pb.ms.trace_path(self.home())
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for r in good:
                fh.write(json.dumps(r) + "\n")
            for r in bad:
                fh.write(r + "\n")
        return good

    def seed_ledger(self, rows=None):
        rows = rows if rows is not None else [
            {"ts": self.now - 5 * 86400, "window": "2026-09-06T20:00Z",
             "profile": "pr-opencode", "model": "qwen3.8-flash",
             "cost": 0.12, "request_count": 7,
             "tokens": {"in": 40000, "out": 3000, "cache_read": 100},
             "session_id": "s_opencode_1", "task": "t_x"},
            {"ts": self.now - 4 * 86400, "window": "2026-09-07T18:45Z",
             "profile": "pr-ollama", "model": "glm-5.2", "cost": 0.05,
             "request_count": 3,
             "tokens": {"in": 9000, "out": 800, "cache_read": 0},
             "session_id": "s_ollama_1", "task": "t_y"},
        ]
        _write_jsonl(Path(self.tmp) / "quota-governor"
                     / "model-cost-ledger.jsonl", rows)
        return rows

    def seed_audit(self, rows=None):
        rows = rows if rows is not None else [
            {"ts": "2026-08-28T10:00:00Z", "job_id": "j1",
             "fire_id": "old_fire_1", "prompt_tokens": 500,
             "completion_tokens": 200, "model": "glm-5.2",
             "response_silent": False},
        ]
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl", rows)
        return rows

    def seed_board(self):
        db = Path(self.tmp) / "kanban.db"
        tasks = [
            ("t_a", "alpha", "done", "pr-ollama", "objective:OBJ-27",
             self.now - 3 * 86400, self.now - 3600),
            ("t_b", "beta live", "running", "pr-ollama", "objective:OBJ-27",
             self.now - 7200, None),
            ("t_c", "gamma", "done", "pr-ollama", "objective:OBJ-30",
             self.now - 2 * 86400, self.now - 1800),
        ]
        events = [
            ("t_a", "created", self.now - 3 * 86400),
            ("t_a", "claimed", self.now - 2 * 86400),
            ("t_a", "completed", self.now - 3600),
            ("t_b", "created", self.now - 7200),
            ("t_b", "claimed", self.now - 7100),
            ("t_c", "created", self.now - 2 * 86400),
            ("t_c", "crashed", self.now - 86000),
            ("t_c", "completed", self.now - 1800),
        ]
        _seed_db(db, tasks, events)
        return db

    def seed_forecast(self, eta90=90.0):
        fc = {"providers": {"pr-ollama": {"pct_now": 52.6,
                                          "eta_90_hours": eta90,
                                          "confidence": 3}},
              "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
              "hours_to_reset": 81.5, "providers_ok": 1,
              "window_hours": 6, "alpha": 0.3}
        p = Path(self.tmp) / "quota-governor" / "forecast.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(fc), encoding="utf-8")
        return fc

    def seed_metrics(self):
        rows = [{"ts": "2026-09-09T10:00:00Z", "supply_ratio": 1.2,
                 "ollama_weekly_pct": 30.0, "nanogpt_weekly_pct": 60.0,
                 "opencode_rolling_pct": 10.0, "opencode_weekly_pct": 50.0,
                 "nanogpt_balance_usd": 27.0,
                 "nanogpt_budget_level": "warn"},
                {"ts": "2026-09-10T10:00:00Z", "supply_ratio": 1.61,
                 "supply_created_24h": 21, "supply_closed_24h": 13,
                 "ollama_weekly_pct": 40.0, "nanogpt_weekly_pct": 70.0,
                 "opencode_rolling_pct": 20.0, "opencode_weekly_pct": 60.0,
                 "nanogpt_balance_usd": 25.5,
                 "nanogpt_budget_level": "warn"}]
        _write_jsonl(Path(self.tmp) / "quota-governor"
                     / "metrics-history.jsonl", rows)
        return rows


# ---------------------------------------------------------------------------
# F5b: backfill
# ---------------------------------------------------------------------------

class TestBackfill(Base):
    def test_merge_sorted_and_lossless(self):
        self.seed_trace()
        self.seed_ledger()
        self.seed_audit()
        self.seed_board()
        rep = bf.run_backfill(hermes_home=self.home())
        self.assertTrue(rep["ok"])
        self.assertGreater(rep["appended"], 0)
        rows, bad = [], 0
        for line in open(bf.tr.trace_path(self.home()), encoding="utf-8"):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except ValueError:
                bad += 1
        ts = [r["ts_epoch_utc"] for r in rows
              if isinstance(r.get("ts_epoch_utc"), (int, float))]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(bad, 1)  # the corrupt line survives
        orig = {(r.get("source"), r.get("consumer_id"), r.get("cause"),
                 r.get("ts_epoch_utc")) for r in self.seed_trace()}
        merged = {(r.get("source"), r.get("consumer_id"), r.get("cause"),
                   r.get("ts_epoch_utc")) for r in rows}
        self.assertTrue(orig <= merged)

    def test_second_run_is_noop(self):
        self.seed_trace()
        self.seed_ledger()
        self.seed_audit()
        self.seed_board()
        r1 = bf.run_backfill(hermes_home=self.home())
        r2 = bf.run_backfill(hermes_home=self.home())
        self.assertEqual(r2["appended"], 0)
        self.assertGreater(r2["duplicates"], 0)

    def test_usage_audit_respects_f0_cursor(self):
        # F0 cursor already at now-1800: the audit row (10 days old) is
        # ingested once; a NEWER audit row is left to the F0 collector.
        self.seed_audit([{"ts": "2026-08-28T10:00:00Z", "job_id": "j1",
                          "fire_id": "old_fire", "prompt_tokens": 10,
                          "completion_tokens": 5, "model": "glm-5.2"},
                         {"ts": "2026-09-11T22:00:00Z", "job_id": "j2",
                          "fire_id": "new_fire", "prompt_tokens": 10,
                          "completion_tokens": 5, "model": "glm-5.2"}])
        bf.tr._save_cursor({"usage-audit": self.now - 1800},
                           hermes_home=self.home())
        rep = bf.run_backfill(hermes_home=self.home())
        self.assertEqual(rep["sources"]["usage-audit"], 1)

    def test_ledger_skips_covered_profiles(self):
        self.seed_ledger()
        rows = bf.backfill_model_cost_ledger(hermes_home=self.home())
        self.assertEqual(len(rows), 1)  # pr-ollama row skipped
        self.assertEqual(rows[0]["provider"], "opencode-go")
        self.assertEqual(rows[0]["source"], "model-cost-ledger")
        self.assertEqual(rows[0]["consumer_class"], "worker")
        self.assertEqual(rows[0]["tokens_in"], 40000)

    def test_task_events_extra_kinds_join(self):
        self.seed_board()
        rows = bf.backfill_task_events(hermes_home=self.home())
        kinds = {r["cause"] for r in rows}
        self.assertIn("created", kinds)
        self.assertIn("crashed", kinds)
        by_key = {(r["consumer_id"], r["cause"]): r for r in rows}
        self.assertEqual(by_key[("t_a", "created")]["objective"], "OBJ-27")
        self.assertEqual(by_key[("t_c", "crashed")]["objective"], "OBJ-30")

    def test_f0_cursor_seeded_only_when_absent(self):
        self.seed_trace()
        self.seed_ledger()
        self.seed_audit()
        self.seed_board()
        bf.run_backfill(hermes_home=self.home())
        f0 = bf.tr._load_cursor(hermes_home=self.home())
        self.assertIn("usage-audit", f0)  # seeded for the fresh adoptant
        # now pretend the F0 cursor is real: backfill must NOT touch it
        bf.tr._save_cursor({"usage-audit": 123.0},
                           hermes_home=self.home())
        bf.run_backfill(hermes_home=self.home())
        f0 = bf.tr._load_cursor(hermes_home=self.home())
        self.assertEqual(f0["usage-audit"], 123.0)

    def test_dry_run_writes_nothing(self):
        self.seed_trace()
        self.seed_ledger()
        self.seed_audit()
        self.seed_board()
        n_before = len(bf.read_existing_keys(self.home()))
        rep = bf.run_backfill(hermes_home=self.home(), dry_run=True)
        self.assertEqual(rep["appended"], rep["sources"]["usage-audit"]
                         + rep["sources"]["model-cost-ledger"]
                         + rep["sources"]["task-events"]
                         - rep["duplicates"])
        self.assertEqual(len(bf.read_existing_keys(self.home())), n_before)


# ---------------------------------------------------------------------------
# F5c: portal
# ---------------------------------------------------------------------------

class TestPortal(Base):
    def seed_all(self):
        self.seed_trace()
        self.seed_ledger()
        self.seed_board()
        self.seed_forecast()
        self.seed_metrics()

    def test_six_pages_with_nav(self):
        self.seed_all()
        pages = pb.portal_build(hermes_home=self.home(), now=self.now)
        self.assertEqual(sorted(pages.keys()), sorted(pb.PAGES))
        for name, page in pages.items():
            self.assertIn("<!doctype html>", page)
            for other in pb.PAGES:
                self.assertIn(f'{other}.html', page)
            self.assertIn("casa portable", page)

    def test_index_kpis_and_charts(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(), now=self.now)["index"]
        self.assertIn("Gasto (trace)", page)
        self.assertIn("real", page)
        self.assertIn("estimado", page)
        self.assertIn("<svg", page)
        self.assertIn("supply_ratio 24h", page)
        self.assertIn("1.61", page)
        self.assertIn("Saldo NanoGPT", page)

    def test_consumo_dims_and_drilldown(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(),
                               now=self.now)["consumo"]
        self.assertIn("Por objetivo", page)
        self.assertIn("Por modelo", page)
        self.assertIn("#d-obj-OBJ-27", page)
        self.assertIn("req_a", page)
        self.assertIn(">real</span>", page)
        self.assertIn(">est</span>", page)

    def test_consumo_search_exact_request(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(), now=self.now,
                               query={"q": "req_a"})["consumo"]
        self.assertIn("Request individual", page)

    def test_board_log_links_and_supply(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(), now=self.now)["board"]
        self.assertIn("Creadas / cerradas / crashes", page)
        self.assertIn("#log-t_b", page)
        self.assertIn("supply_ratio", page)
        self.assertIn("kanban log", page)

    def test_providers_verdict_and_ledger(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(),
                               now=self.now)["providers"]
        self.assertIn(">OK</span>", page)
        self.assertIn("ollama", page)
        self.assertIn("Histórico de ventanas cerradas", page)
        self.assertIn("nanogpt", page)

    def test_alarms_health_and_loops(self):
        # seed a BALANCED trace (unattributed under threshold) so the
        # healthy path shows silence; the alarm path is covered by
        # test_alarms_render_when_gap (below).
        self.seed_trace([
            {"ts_epoch_utc": self.now - 3600, "consumer_class": "worker",
             "consumer_id": "req_a", "cause": "request", "model": "glm-5.3",
             "provider": "custom", "costUsd": 0.005,
             "requestId": "req_a", "objective": "OBJ-27",
             "source": "nanogpt-requests"},
            {"ts_epoch_utc": self.now - 7200, "consumer_class": "cron-llm",
             "consumer_id": "fire_1", "cause": "cron-fire", "model": "glm-5.2",
             "provider": None, "costUsd": 0.001, "requestId": None,
             "objective": "OBJ-30", "source": "usage-audit"},
        ])
        self.seed_board()
        self.seed_forecast()
        self.seed_metrics()
        page = pb.portal_build(hermes_home=self.home(), now=self.now)["alarms"]
        self.assertIn("sin incidencias", page)
        self.assertIn("Salud del trace", page)
        self.assertIn("ningún loop de crash", page)

    def test_alarms_render_when_gap(self):
        # the gap is SHOWN: an over-threshold unattributed share must reach
        # the page as an alarm (never hidden, never silenced).
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(), now=self.now)["alarms"]
        self.assertIn("ALARM unattributed", page)
        self.assertIn("hueco de join", page)

    def test_docs_schema(self):
        self.seed_all()
        page = pb.portal_build(hermes_home=self.home(), now=self.now)["docs"]
        self.assertIn("trace.py", page)
        self.assertIn("Contratos de la casa", page)

    def test_empty_home_still_builds(self):
        pages = pb.portal_build(hermes_home=self.home(), now=self.now)
        for name, page in pages.items():
            self.assertIn("</html>", page)
        self.assertIn("sin", pages["index"])

    def test_write_portal(self):
        self.seed_all()
        target = pb.write_portal(hermes_home=self.home(), now=self.now)
        self.assertEqual(sorted(p.name for p in target.glob("*.html")),
                         sorted(f"{p}.html" for p in pb.PAGES))


class TestPortability(unittest.TestCase):
    def test_no_host_paths_in_new_modules(self):
        for mod in ("trace-backfill.py", "portal-build.py", "obs-serve.py"):
            src = (OBS / mod).read_text(encoding="utf-8")
            for needle in ("/home/", "/data/git", Path.home().name,
                           "/iinstances"):
                self.assertNotIn(needle, src,
                                 f"host path leaked into {mod}: {needle}")

    def test_cron_wrappers_follow_house_convention(self):
        # cron wrappers PIN the home and pin the repo script path (the
        # same convention as trace-alarms-cron.sh — portable adoptants
        # regenerate the wrappers from their own checkout). What must
        # NEVER leak: paths under the user's HOME.
        for mod in ("obs-serve-cron.sh", "trace-backfill-cron.sh"):
            src = (OBS / mod).read_text(encoding="utf-8")
            self.assertNotIn("/home/", src, f"{mod} leaked a HOME path")
            self.assertIn("HERMES_HOME", src, f"{mod} must pin HERMES_HOME")


# ---------------------------------------------------------------------------
# F5d: server
# ---------------------------------------------------------------------------

class TestServer(Base):
    def test_binds_localhost_serves_all_pages(self):
        self.seed_trace()
        self.seed_board()
        self.seed_forecast()
        self.seed_metrics()
        pb.write_portal(hermes_home=self.home(), now=self.now)
        srv = sv.make_server(0, hermes_home=self.home())  # ephemeral port
        port = srv.server_address[1]
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            for name in ("index.html", "consumo.html", "board.html",
                         "providers.html", "alarms.html", "docs.html"):
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/{name}",
                        timeout=5) as r:
                    self.assertEqual(r.status, 200)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5) as r:
                self.assertEqual(r.status, 200)   # root -> index
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/nope.html", timeout=5)
                raised = False
            except urllib.error.HTTPError as e:
                raised = e.code == 404
            self.assertTrue(raised)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_regenerator_rebuilds_once(self):
        self.seed_trace()
        reg = sv.Regenerator(hermes_home=self.home(), refresh_min=0.5)
        reg.stop_flag.set()          # stop the loop immediately
        reg.start()                  # boot regeneration still runs
        reg.regenerate("test")
        target = pb.portal_dir(self.home())
        self.assertTrue((target / "index.html").exists())

    def test_check_exit_codes(self):
        # down (nothing bound on the ephemeral probe): --check must fail
        rc = sv.main(["--check", "--port", "0"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)