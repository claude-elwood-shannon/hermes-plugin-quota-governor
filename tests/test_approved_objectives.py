#!/usr/bin/python3.12
"""Tests for approved_objectives (MEDIATOR 2026-09-14). Fixtures only."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "approved_objectives", _HERE / "scripts" / "approved_objectives.py")
ao = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ao)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "profiles/pr-ollama/quota-governor/obs").mkdir(parents=True)
        self.db = self.root / "kanban.db"
        self.trace = self.root / "profiles/pr-ollama/quota-governor/obs/trace.jsonl"
        os.environ["AO_HERMES_ROOT"] = str(self.root)
        os.environ["AO_KANBAN_DB"] = str(self.db)
        os.environ["AO_TRACE"] = str(self.trace)
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
            "assignee TEXT, status TEXT, body TEXT, created_at REAL);"
            "CREATE TABLE kanban_meta (k TEXT PRIMARY KEY, v TEXT);")
        con.commit()
        con.close()
        self.now = 1789600000.0  # any instant

    def tearDown(self):
        for k in ("AO_HERMES_ROOT", "AO_KANBAN_DB", "AO_TRACE"):
            os.environ.pop(k, None)

    def add_trace(self, entries):
        with open(self.trace, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")


class TestInventory(Harness):

    def test_1_table_created_with_3_seed(self):
        self.assertTrue(ao.ensure_table(self.db))
        rows = ao.list_objectives(self.db)
        self.assertEqual([r["id"] for r in rows],
                         ["OBJ-AUTODEV", "OBJ-CODEQUALITY", "OBJ-METRICS",
                          "OBJ-VLLM"])
        self.assertTrue(all(r["status"] == "active" for r in rows))
        self.assertEqual(sum(r["budget_daily"] for r in rows), 5.00)
        # idempotent
        self.assertTrue(ao.ensure_table(self.db))
        self.assertEqual(len(ao.list_objectives(self.db)), 4)

    def test_4_5_bridge_upsert_create_and_update(self):
        res = ao.upsert_objective(self.db, {
            "id": "OBJ-NEW", "name": "Nuevo objetivo", "budget_daily": 0.50,
            "description": "d", "success_criterion": "sc", "status": "active"},
            updated_by="mediator")
        self.assertTrue(res["ok"] and res["action"] == "created")
        res2 = ao.upsert_objective(self.db, {
            "id": "OBJ-NEW", "name": "Nuevo objetivo", "budget_daily": 0.75})
        self.assertTrue(res2["ok"] and res2["action"] == "updated")
        obj = ao.get_objective(self.db, "OBJ-NEW")
        self.assertEqual(obj["budget_daily"], 0.75)
        self.assertEqual(obj["updated_by"], "mediator")
        self.assertEqual(len(ao.list_objectives(self.db)), 5)  # no duplicate

    def test_upsert_rejects_bad_input(self):
        self.assertFalse(ao.upsert_objective(self.db, {"id": "X"}).get("ok"))
        self.assertFalse(ao.upsert_objective(
            self.db, {"id": "X", "name": "n", "budget_daily": "much"}).get("ok"))
        self.assertFalse(ao.upsert_objective(
            self.db, {"id": "X", "name": "n", "budget_daily": 1.0,
                      "status": "zombie"}).get("ok"))


class TestSpend(Harness):

    def test_6_spend_from_trace_and_implicit_reset(self):
        ao.ensure_table(self.db)
        day0 = ao.day_start_epoch_cest(ao.cest_day_of(self.now))
        self.add_trace([
            {"ts_epoch_utc": day0 + 3600, "objective": "OBJ-AUTODEV",
             "costUsd": 1.20},
            {"ts_epoch_utc": day0 + 7200, "objective": "OBJ-AUTODEV",
             "costUsd": 0.30},
            {"ts_epoch_utc": day0 - 100, "objective": "OBJ-AUTODEV",
             "costUsd": 9.99},                       # yesterday: excluded
            {"ts_epoch_utc": day0 + 400, "objective": "OBJ-METRICS",
             "costUsd": 0.15},
            {"ts_epoch_utc": day0 + 500, "objective": "OBJ-UNKNOWN",
             "costUsd": 5.0},                        # not in table: ignored
        ])
        res = ao.update_spend(self.db, now=self.now)
        by_id = {u["id"]: u for u in res["updated"]}
        self.assertAlmostEqual(by_id["OBJ-AUTODEV"]["spent_today"], 1.50)
        self.assertAlmostEqual(by_id["OBJ-METRICS"]["spent_today"], 0.15)
        self.assertAlmostEqual(by_id["OBJ-AUTODEV"]["spent_total"], 1.50)
        # next tick same day: total grows by delta only (0)
        res2 = ao.update_spend(self.db, now=self.now + 600)
        by_id2 = {u["id"]: u for u in res2["updated"]}
        self.assertAlmostEqual(by_id2["OBJ-AUTODEV"]["spent_total"], 1.50)
        # new CEST day: today sum 0.30 -> spent_today resets, total grows +0.30
        tomorrow = self.now + 86400
        self.add_trace([
            {"ts_epoch_utc": ao.day_start_epoch_cest(ao.cest_day_of(tomorrow)) + 600,
             "objective": "OBJ-AUTODEV", "costUsd": 0.30}])
        res3 = ao.update_spend(self.db, now=tomorrow)
        by_id3 = {u["id"]: u for u in res3["updated"]}
        self.assertAlmostEqual(by_id3["OBJ-AUTODEV"]["spent_today"], 0.30)
        self.assertAlmostEqual(by_id3["OBJ-AUTODEV"]["spent_total"], 1.80)


class TestBudgetGate(Harness):

    def setUp(self):
        super().setUp()
        ao.ensure_table(self.db)

    def test_7_exhausted_budget_not_dispatched(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE approved_objectives SET spent_today=3.0 "
                    "WHERE id='OBJ-AUTODEV'")
        con.commit(); con.close()
        allowed, reason = ao.budget_check(self.db, "OBJ-AUTODEV")
        self.assertFalse(allowed)
        self.assertIn("budget exhausted", reason)

    def test_8_non_active_not_dispatched(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE approved_objectives SET status='paused' "
                    "WHERE id='OBJ-VLLM'")
        con.commit(); con.close()
        allowed, reason = ao.budget_check(self.db, "OBJ-VLLM")
        self.assertFalse(allowed)
        self.assertIn("not active", reason)

    def test_9_unknown_objective_not_dispatched(self):
        allowed, reason = ao.budget_check(self.db, "OBJ-GHOST")
        self.assertFalse(allowed)
        self.assertIn("unknown objective", reason)

    def test_table_missing_failopen(self):
        os.environ["AO_KANBAN_DB"] = str(self.root / "empty.db")
        allowed, reason = ao.budget_check(self.root / "empty.db", "OBJ-X")
        os.environ["AO_KANBAN_DB"] = str(self.db)
        self.assertTrue(allowed)
        self.assertIn("fail-open", reason)


class TestLifecycle(Harness):

    def setUp(self):
        super().setUp()
        ao.ensure_table(self.db)

    def test_11_achieved_with_evidence_except_perpetual(self):
        # OBJ-VLLM criterion: script + >=10 ok entries + skill
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/vllm-invoke.py").write_text("x")
        for i in range(10):
            (self.root / "logs").mkdir(exist_ok=True)
            with open(self.root / "logs/vllm-invoke.jsonl", "a") as fh:
                fh.write(json.dumps({"exit_code": 0}) + "\n")
        (self.root / "profiles/pr-ollama/skills/vllm-delegate").mkdir(
            parents=True, exist_ok=True)
        (self.root / "profiles/pr-ollama/skills/vllm-delegate/SKILL.md"
         ).write_text("x")
        lines = ao.run_lifecycle(self.db, now=self.now)
        self.assertTrue(any("OBJ-VLLM achieved" in l for l in lines))
        obj = ao.get_objective(self.db, "OBJ-VLLM")
        self.assertEqual(obj["status"], "achieved")
        # perpetual never achieved
        autodev = ao.get_objective(self.db, "OBJ-AUTODEV")
        self.assertEqual(autodev["status"], "active")

    def test_12_13_paused_after_3_days_then_reactivate(self):
        day0 = ao.day_start_epoch_cest(ao.cest_day_of(self.now))
        self.add_trace([
            {"ts_epoch_utc": day0 + 3600, "objective": "OBJ-METRICS",
             "costUsd": 0.60}])  # >= budget 0.50 today
        # day 1
        ao.update_spend(self.db, now=day0 + 7200)
        ao.run_lifecycle(self.db, now=day0 + 7200)
        # day 2 and 3 also exhausted
        for offset in (1, 2):
            d = day0 + offset * 86400 + 3600
            self.add_trace([
                {"ts_epoch_utc": d, "objective": "OBJ-METRICS",
                 "costUsd": 0.55}])
            ao.update_spend(self.db, now=d + 3600)
            ao.run_lifecycle(self.db, now=d + 3600)
        obj = ao.get_objective(self.db, "OBJ-METRICS")
        self.assertEqual(obj["status"], "paused")
        # budget available again (no spend today) -> reactivated
        d4 = day0 + 3 * 86400 + 3600
        self.add_trace([])
        ao.update_spend(self.db, now=d4)
        lines = ao.run_lifecycle(self.db, now=d4)
        self.assertTrue(any("OBJ-METRICS reactivado" in l for l in lines))
        self.assertEqual(ao.get_objective(self.db, "OBJ-METRICS")["status"],
                         "active")


# --- bridge endpoints (2, 3) over a real HTTP round-trip --------------------

class TestBridgeEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "kanban.db"
        os.environ["AO_KANBAN_DB"] = str(db)
        spec = importlib.util.spec_from_file_location(
            "bridge", _HERE / "scripts" / "bridge" / "open-webui-bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)
        os.environ["BRIDGE_PLUGIN_REPO"] = str(_HERE)  # vllm script candidate
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE approved_objectives (id TEXT PRIMARY KEY, name TEXT"
            " NOT NULL, budget_daily REAL NOT NULL DEFAULT 0.0, description"
            " TEXT, status TEXT NOT NULL DEFAULT 'active', success_criterion"
            " TEXT, spent_today REAL DEFAULT 0.0, spent_total REAL DEFAULT"
            " 0.0, created_at REAL, updated_at REAL, updated_by TEXT);")
        con.execute("INSERT INTO approved_objectives (id,name,budget_daily,"
                    "status) VALUES ('OBJ-AUTODEV','Autodesarrollo',3.0,"
                    "'active')")
        con.commit()
        con.close()

        cls.httpd = cls.bridge.HTTPServer(("127.0.0.1", 0),
                                          cls.bridge.HermesBridge)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("AO_KANBAN_DB", None)
        cls.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())

    def _post(self, path, data):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(data).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_2_get_objectives(self):
        code, data = self._get("/objectives")
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["objectives"][0]["id"], "OBJ-AUTODEV")

    def test_3_get_objectives_filter(self):
        code, data = self._get("/objectives?status=paused")
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        code, data = self._get("/objectives?status=active")
        self.assertEqual(data["count"], 1)

    def test_4_post_creates(self):
        code, data = self._post("/update-objective", {
            "id": "OBJ-BRIDGE", "name": "Via bridge", "budget_daily": 0.25})
        self.assertEqual(code, 200)
        self.assertEqual(data["action"], "created")
        con = sqlite3.connect(os.environ["AO_KANBAN_DB"])
        row = con.execute("SELECT name, budget_daily, updated_by FROM "
                          "approved_objectives WHERE id='OBJ-BRIDGE'"
                          ).fetchone()
        con.close()
        self.assertEqual(row[0], "Via bridge")
        self.assertEqual(row[2], "mediator")

    def test_5_post_updates_existing(self):
        code, data = self._post("/update-objective", {
            "id": "OBJ-BRIDGE", "name": "Via bridge",
            "budget_daily": 0.40, "status": "paused"})
        self.assertEqual(data["action"], "updated")
        con = sqlite3.connect(os.environ["AO_KANBAN_DB"])
        row = con.execute("SELECT budget_daily, status, name FROM "
                          "approved_objectives WHERE id='OBJ-BRIDGE'"
                          ).fetchone()
        con.close()
        self.assertEqual((row[0], row[1]), (0.40, "paused"))


if __name__ == "__main__":
    sys.exit(unittest.main())
