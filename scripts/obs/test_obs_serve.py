#!/usr/bin/python3.12
"""test_obs_serve.py — OBJ-27: the hyperespace server (obs-serve.py).

The server is the STABLE URL of the hiperespacio (F5d): static files from
obs/portal/, on-demand regeneration when the portal is not built yet, a
respawn contract owned by obs-serve-cron.sh. This suite covers the five
faces the card asks for:

  1. routes: GET / -> index, GET /consumo, GET /board (content-verified,
     including the on-demand build when the portal was never generated)
  2. 404s: unknown pages, nested paths and traversal never serve
  3. cache headers: no-store + explicit content type/length on 200s
  4. respawn: the alive probe never double-binds (idempotent, PID file
     untouched, our server still the one answering)
  5. respawn cycle: --check 1 when down -> server up -> --check 0 ->
     --stop SIGTERMs the recorded PID for real

Fixtures only: no network beyond 127.0.0.1 ephemeral ports, no live board,
no real home (synthetic HERMES_HOME tempdir, like every obs test suite).
Run: /usr/bin/python3.12 test_obs_serve.py  (or pytest).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
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


pb = _load("portal_build_serve_test", OBS / "portal-build.py")
sv = _load("obs_serve_test", OBS / "obs-serve.py")


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
        self.tmp = tempfile.mkdtemp(prefix="obs-serve-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.now = time.time()

    def home(self):
        return self.tmp

    # -- shared fixtures (same shape as the F5 suite) ------------------------

    def seed_trace(self):
        _write_jsonl(
            Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl", [
                {"ts_epoch_utc": self.now - 3600, "consumer_class": "worker",
                 "consumer_id": "req_a", "cause": "request",
                 "model": "glm-5.3", "provider": "custom", "costUsd": 0.002,
                 "requestId": "req_a", "objective": "OBJ-27",
                 "source": "nanogpt-requests"},
                {"ts_epoch_utc": self.now - 7200, "consumer_class": "cron-llm",
                 "consumer_id": "fire_1", "cause": "cron-fire",
                 "model": "glm-5.2", "provider": None, "costUsd": 0.004,
                 "requestId": None, "objective": "unattributed",
                 "source": "usage-audit"},
            ])

    def seed_board(self):
        _seed_db(Path(self.tmp) / "kanban.db",
                 [("t_a", "alpha", "done", "pr-ollama", "objective:OBJ-27",
                   self.now - 3 * 86400, self.now - 3600),
                  ("t_b", "beta live", "running", "pr-ollama",
                   "objective:OBJ-27", self.now - 7200, None)],
                 [("t_a", "created", self.now - 3 * 86400),
                  ("t_a", "claimed", self.now - 2 * 86400),
                  ("t_a", "completed", self.now - 3600),
                  ("t_b", "created", self.now - 7200),
                  ("t_b", "claimed", self.now - 7100)])

    def seed_forecast(self):
        fc = {"providers": {"pr-ollama": {"pct_now": 52.6,
                                          "eta_90_hours": 90.0,
                                          "confidence": 3}},
              "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
              "hours_to_reset": 81.5, "providers_ok": 1,
              "window_hours": 6, "alpha": 0.3}
        p = Path(self.tmp) / "quota-governor" / "forecast.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(fc), encoding="utf-8")

    def seed_metrics(self):
        _write_jsonl(
            Path(self.tmp) / "quota-governor" / "metrics-history.jsonl",
            [{"ts": "2026-09-09T10:00:00Z", "supply_ratio": 1.2,
              "supply_created_24h": 21, "supply_closed_24h": 13,
              "nanogpt_balance_usd": 27.0, "nanogpt_budget_level": "warn"}])

    def seed_all(self):
        self.seed_trace()
        self.seed_board()
        self.seed_forecast()
        self.seed_metrics()

    # -- server plumbing ------------------------------------------------------

    def serve(self):
        """Start make_server on an ephemeral 127.0.0.1 port; returns the URL."""
        srv = sv.make_server(0, hermes_home=self.home())
        port = srv.server_address[1]
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return f"http://127.0.0.1:{port}"

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8")


# ---------------------------------------------------------------------------
# 1. routes: /, /consumo, /board (+ the rest of PAGES, with on-demand build)
# ---------------------------------------------------------------------------

class TestRoutes(Base):
    def test_root_consumo_board_and_all_pages(self):
        self.seed_all()
        # portal NEVER pre-generated: the first GET must build it on demand
        base = self.serve()
        # GET / -> index
        status, headers, body = self.get(f"{base}/")
        self.assertEqual(status, 200)
        self.assertIn("hiperespacio — Overview", body)
        # the on-demand regeneration actually landed next to the trace
        self.assertTrue(
            (pb.portal_dir(self.home()) / "index.html").exists())
        # GET /consumo — seeded request ids reach the page
        status, _, body = self.get(f"{base}/consumo")
        self.assertEqual(status, 200)
        self.assertIn("hiperespacio — Consumo", body)
        self.assertIn("req_a", body)
        self.assertIn("OBJ-27", body)
        # GET /board — seeded kanban tasks reach the page
        status, _, body = self.get(f"{base}/board")
        self.assertEqual(status, 200)
        self.assertIn("hiperespacio — Board", body)
        self.assertIn("beta live", body)     # active task, board page
        self.assertIn("t_a", body)           # closed in 24h
        # the remaining pages answer too (query strings are stripped)
        for name, marker in (("providers", "hiperespacio — Providers"),
                             ("alarms", "<h1>Alarmas &amp; Salud</h1>"),
                             ("docs", "hiperespacio — Docs vivos")):
            status, _, body = self.get(f"{base}/{name}?x=1")
            self.assertEqual(status, 200)
            self.assertIn(marker, body)


# ---------------------------------------------------------------------------
# 2. 404s: unknown, nested and traversal paths never serve
# ---------------------------------------------------------------------------

class Test404(Base):
    def test_unknown_nested_and_traversal_are_404(self):
        self.seed_all()
        base = self.serve()
        for path in ("/nope.html", "/nope", "/nope/", "/a/b.html",
                     "/../etc/passwd", "/index.html.extra"):
            try:
                self.get(f"{base}{path}")
                raised = False
            except urllib.error.HTTPError as e:
                raised = e.code == 404
            self.assertTrue(raised, f"{path} must 404, not serve")


# ---------------------------------------------------------------------------
# 3. cache headers: no-store + explicit type/length on every 200
# ---------------------------------------------------------------------------

class TestCacheHeaders(Base):
    def test_200s_carry_no_store_and_content_type(self):
        self.seed_all()
        pb.write_portal(hermes_home=self.home())   # static path (not on-demand)
        base = self.serve()
        for name in ("", "index.html", "consumo.html", "board.html"):
            status, headers, body = self.get(f"{base}/{name}")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertEqual(headers.get("Content-Type"),
                             "text/html; charset=utf-8")
            self.assertEqual(headers.get("Content-Length"),
                             str(len(body.encode("utf-8"))))


# ---------------------------------------------------------------------------
# 4. respawn: alive port is never double-bound (idempotent respawn contract)
# ---------------------------------------------------------------------------

class TestRespawnIdempotent(Base):
    def test_alive_port_never_double_binds(self):
        self.seed_all()
        pb.write_portal(hermes_home=self.home())
        base = self.serve()
        # the cron supervisor's probe: --check on a live port must pass
        port = base.rsplit(":", 1)[1]
        self.assertEqual(sv.main(["--check", "--port", port]), 0)
        # a respawn tick while alive: exit 0, no second bind, PID file
        # untouched, and OUR server is still the one answering
        self.assertEqual(sv.main(["--port", port]), 0)
        pid_file = pb.portal_dir(self.home()).parent / "obs-serve.pid"
        self.assertFalse(pid_file.exists())
        status, _, body = self.get(f"{base}/")
        self.assertEqual(status, 200)
        self.assertIn("hiperespacio — Overview", body)


# ---------------------------------------------------------------------------
# 5. respawn cycle: --check 1 down -> up -> --check 0 -> --stop kills the PID
# ---------------------------------------------------------------------------

class TestRespawnCycle(Base):
    def test_check_down_up_then_stop_kills_pid(self):
        self.seed_all()
        # down: nothing bound on the probe port -> --check must fail
        self.assertEqual(sv.main(["--check", "--port", "0"]), 1)
        # the real daemon, exactly the way the cron tick leaves it:
        # obs-serve.py subprocess on a free port, PID file written by it
        probe = sv.make_server(0, hermes_home=self.home())
        port = probe.server_address[1]  # pick a free port, then release it
        probe.server_close()
        child = subprocess.Popen(
            [sys.executable, str(OBS / "obs-serve.py"),
             "--port", str(port), "--refresh", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        self.addCleanup(child.wait)
        pid_file = pb.portal_dir(self.home()).parent / "obs-serve.pid"
        for _ in range(50):  # wait until the daemon wrote its PID file
            if child.poll() is not None:
                self.fail("obs-serve daemon died during startup")
            if pid_file.exists() and sv.port_alive(port):
                break
            time.sleep(0.2)
        else:
            self.fail("obs-serve daemon never became ready")
        self.assertEqual(int(pid_file.read_text()), child.pid)
        # alive: --check passes, then --stop SIGTERMs the recorded PID
        self.assertEqual(sv.main(["--check", "--port", str(port)]), 0)
        self.assertEqual(sv.main(["--stop"]), 0)
        child.wait(timeout=10)  # reaped: SIGTERM delivered by --stop
        self.assertEqual(child.returncode, 0)  # graceful SIGTERM shutdown
        self.assertFalse(pid_file.exists())    # daemon unlinked its own PID
        # after the stop the probe reports the port dead again
        self.assertEqual(sv.main(["--check", "--port", str(port)]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)