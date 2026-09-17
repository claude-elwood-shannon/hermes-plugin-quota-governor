#!/usr/bin/env python3
"""t_d8df248b — P3 presets+objectives endpoint tests (offline, hermetic).

Runs the bridge against a temp kanban.db with the P1 schema (P1 DDL verbatim)
and exercises GET /presets, POST /update-preset, extended POST
/update-objective and extended GET /objectives over real HTTP round-trips.
"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent
# dev loop: BRIDGE_P3_SOURCE points at a workspace copy of the bridge;
# default (tests/ in the repo checkout) is the repo copy itself.
_SRC = Path(os.environ.get("BRIDGE_P3_SOURCE")
            or (REPO / "scripts" / "bridge" / "open-webui-bridge.py"))
P1_DDL = (
    "CREATE TABLE adjustment_presets (\n"
    "    id TEXT PRIMARY KEY,\n"
    "    name TEXT,\n"
    "    description TEXT,\n"
    "    nice_step REAL,\n"
    "    nice_cap_high REAL,\n"
    "    nice_cap_low REAL,\n"
    "    budget_step_pct REAL,\n"
    "    budget_cap_high_pct REAL,\n"
    "    budget_cap_low_pct REAL,\n"
    "    eval_frequency_min INTEGER,\n"
    "    cooldown_min INTEGER,\n"
    "    trigger_eff_high REAL,\n"
    "    trigger_eff_low REAL,\n"
    "    consecutive_high INTEGER,\n"
    "    consecutive_low INTEGER,\n"
    "    is_system INTEGER,\n"
    "    is_active INTEGER,\n"
    "    created_at REAL,\n"
    "    updated_at REAL,\n"
    "    updated_by TEXT\n"
    ");"
)
AO_DDL = (
    "CREATE TABLE approved_objectives (\n"
    "    id TEXT PRIMARY KEY, name TEXT NOT NULL, budget_daily REAL NOT NULL"
    " DEFAULT 0.0, description TEXT, status TEXT NOT NULL DEFAULT 'active',"
    " success_criterion TEXT, spent_today REAL DEFAULT 0.0, spent_total REAL"
    " DEFAULT 0.0, created_at REAL, updated_at REAL, updated_by TEXT,"
    " exhausted_days INTEGER DEFAULT 0, last_exhausted_day TEXT,\n"
    "    nice INTEGER DEFAULT 0, focus_until REAL DEFAULT NULL,"
    " budget_baseline REAL DEFAULT 0, budget_adjustment_pct REAL DEFAULT 0,"
    " governance TEXT DEFAULT 'static', preset_id TEXT DEFAULT 'normal'\n"
    ");"
)


def _load_bridge():
    # isolated temp db FIRST so module-level _AO_DB points at it
    os.environ["AO_KANBAN_DB"] = os.environ["P3_TMP_DB"]
    spec = importlib.util.spec_from_file_location("bridge_p3", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class P3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "kanban.db"
        os.environ["P3_TMP_DB"] = str(cls.db)
        con = sqlite3.connect(cls.db)
        con.executescript(P1_DDL + ";" + AO_DDL + ";")
        con.execute("INSERT INTO adjustment_presets (id, name) VALUES"
                    " ('normal', 'normal'), ('aggressive', 'aggressive'),"
                    " ('conservative', 'conservative'), ('startup',"
                    " 'startup'), ('protected', 'protected')")
        con.commit()
        con.close()
        cls.bridge = _load_bridge()
        cls.httpd = cls.bridge.HTTPServer(("127.0.0.1", 0),
                                          cls.bridge.HermesBridge)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("AO_KANBAN_DB", None)
        os.environ.pop("P3_TMP_DB", None)
        cls.tmp.cleanup()

    def _get(self, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, data):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(data).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # ---- GET /presets ------------------------------------------------

    def test_1_presets_empty_then_seeded(self):
        # hermetic: drop+recreate empty (setUpClass seeds 5), then reseed
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE adjustment_presets")
        con.executescript(P1_DDL + ";")
        con.commit()
        con.close()
        code, data = self._get("/presets")
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["presets"], [])
        self._recreate_presets()
        code, data = self._get("/presets")
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 5)
        self.assertEqual([p["id"] for p in data["presets"]],
                         ["aggressive", "conservative", "normal",
                          "protected", "startup"])

    def test_2_presets_missing_table_503(self):
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE adjustment_presets")
        con.commit()
        con.close()
        try:
            code, data = self._get("/presets")
            self.assertEqual(code, 503)
            self.assertIn("error", data)
        finally:
            self._recreate_presets()

    def _recreate_presets(self):
        con = sqlite3.connect(self.db)
        con.executescript("DROP TABLE IF EXISTS adjustment_presets;"
                          + P1_DDL + ";")
        con.execute("INSERT INTO adjustment_presets (id, name) VALUES"
                    " ('normal', 'normal'), ('aggressive', 'aggressive'),"
                    " ('conservative', 'conservative'), ('startup',"
                    " 'startup'), ('protected', 'protected')")
        con.commit()
        con.close()

    # ---- POST /update-preset ----------------------------------------

    def test_3_preset_create_update(self):
        code, data = self._post("/update-preset", {"id": "turbo",
                                                   "name": "turbo"})
        self.assertEqual(code, 200)
        self.assertEqual(data["action"], "created")
        code, data = self._post("/update-preset", {
            "id": "turbo", "name": "turbo",
            "description": "fast lane",
            "nice_step": 5, "nice_cap_high": 15, "nice_cap_low": -10,
            "budget_step_pct": 0.25, "budget_cap_high_pct": 0.5,
            "budget_cap_low_pct": 0.4, "eval_frequency_min": 15,
            "cooldown_min": 30, "trigger_eff_high": 0.8,
            "trigger_eff_low": 0.3, "consecutive_high": 2,
            "consecutive_low": 3, "is_system": 0, "is_active": 1})
        self.assertEqual(code, 200)
        self.assertEqual(data["action"], "updated")
        p = data["preset"]
        self.assertEqual(p["description"], "fast lane")
        self.assertEqual(p["nice_step"], 5)
        self.assertEqual(p["eval_frequency_min"], 15)
        self.assertEqual(p["is_active"], 1)
        # untouched sibling preset stays null-typed
        code, data = self._get("/presets")
        row = [x for x in data["presets"] if x["id"] == "normal"]
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["nice_step"])

    def test_4_preset_validation(self):
        code, data = self._post("/update-preset", {"name": "no-id"})
        self.assertEqual(code, 400, str(data))
        code, data = self._post("/update-preset", {"id": "x",
                                                   "name": "x",
                                                   "nice_step": "fast"})
        self.assertEqual(code, 400, str(data))
        code, data = self._post("/update-preset", {"id": "x",
                                                   "name": "x",
                                                   "is_active": True})
        self.assertEqual(code, 400, str(data))
        # unknown/extra fields (governance no es columna de preset) se
        # ignoran: semantica provided-fields-only
        code, data = self._post("/update-preset", {"id": "x",
                                                   "name": "x",
                                                   "governance": "wild"})
        self.assertEqual(code, 200, str(data))
        self.assertNotIn("governance", data["preset"])

    # ---- POST /update-objective extended -----------------------------

    def test_5_objective_create_with_p1_fields(self):
        code, data = self._post("/update-objective", {
            "id": "OBJ-TEST", "name": "test", "budget_daily": 0.10,
            "nice": -5, "governance": "dynamic",
            "preset_id": "aggressive"})
        self.assertEqual(code, 200, str(data))
        self.assertEqual(data["action"], "created")
        o = data["objective"]
        self.assertEqual(o["nice"], -5)
        self.assertEqual(o["governance"], "dynamic")
        self.assertEqual(o["preset_id"], "aggressive")
        self.assertEqual(o["budget_baseline"], 0.10)
        self.assertEqual(o["budget_daily"], 0.10)

    def test_6_objective_update_fields_only(self):
        code, data = self._post("/update-objective", {
            "id": "OBJ-TEST", "name": "test", "budget_daily": 0.10,
            "nice": 10, "budget_baseline": 0.50,
            "budget_adjustment_pct": 0.2, "governance": "responsive",
            "preset_id": "conservative"})
        self.assertEqual(code, 200)
        o = data["objective"]
        self.assertEqual(o["nice"], 10)
        self.assertEqual(o["budget_baseline"], 0.50)
        self.assertEqual(o["budget_adjustment_pct"], 0.2)
        self.assertEqual(o["governance"], "responsive")
        self.assertEqual(o["preset_id"], "conservative")
        self.assertEqual(o["budget_daily"], 0.10)
        # legacy fields still work
        code, data = self._post("/update-objective", {
            "id": "OBJ-TEST", "name": "test2", "budget_daily": 0.20,
            "status": "paused"})
        self.assertEqual(data["objective"]["name"], "test2")
        self.assertEqual(data["objective"]["status"], "paused")
        self.assertEqual(data["objective"]["nice"], 10)

    def test_7_objective_validation(self):
        code, _ = self._post("/update-objective", {
            "id": "OBJ-BAD", "name": "n", "budget_daily": 0.1,
            "nice": 3.5})
        self.assertEqual(code, 400)
        code, _ = self._post("/update-objective", {
            "id": "OBJ-BAD", "name": "n", "budget_daily": 0.1,
            "nice": True})
        self.assertEqual(code, 400)
        code, _ = self._post("/update-objective", {
            "id": "OBJ-BAD", "name": "n", "budget_daily": 0.1,
            "budget_baseline": "x"})
        self.assertEqual(code, 400)
        code, _ = self._post("/update-objective", {
            "id": "OBJ-BAD", "name": "n", "budget_daily": 0.1,
            "governance": "chaos"})
        self.assertEqual(code, 400)
        code, _ = self._post("/update-objective", {
            "id": "OBJ-BAD", "name": "n", "budget_daily": 0.1,
            "preset_id": "ghost"})
        self.assertEqual(code, 400)

    def test_8_ensure_columns_alter(self):
        # fresh legacy-shaped table in a second db -> ensure adds 6 columns
        db2 = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(db2)
        con.execute(
            "CREATE TABLE approved_objectives (id TEXT PRIMARY KEY,"
            " name TEXT NOT NULL, budget_daily REAL NOT NULL DEFAULT 0.0,"
            " description TEXT, status TEXT NOT NULL DEFAULT 'active',"
            " success_criterion TEXT, spent_today REAL DEFAULT 0.0,"
            " spent_total REAL DEFAULT 0.0, created_at REAL, updated_at REAL,"
            " updated_by TEXT, exhausted_days INTEGER DEFAULT 0,"
            " last_exhausted_day TEXT)")
        con.commit()
        con.close()
        self.bridge._ao_ensure_columns(str(db2))
        cols = {r[1] for r in sqlite3.connect(db2).execute(
            "PRAGMA table_info(approved_objectives)")}
        self.assertTrue({"nice", "focus_until", "budget_baseline",
                         "budget_adjustment_pct", "governance",
                         "preset_id"} <= cols)

    # ---- GET /objectives extended ------------------------------------

    def test_9_objectives_expose_p1_columns(self):
        code, data = self._get("/objectives")
        self.assertEqual(code, 200)
        row = [o for o in data["objectives"] if o["id"] == "OBJ-TEST"][0]
        for col in ("nice", "focus_until", "budget_baseline",
                    "budget_adjustment_pct", "governance", "preset_id"):
            self.assertIn(col, row)

    # ---- openapi ------------------------------------------------------

    def test_10_openapi_new_ops(self):
        code, spec = self._get("/openapi.json")
        self.assertEqual(code, 200)
        for op in ("/presets", "/update-preset", "/update-objective",
                   "/objectives"):
            self.assertIn(op, spec["paths"], op)
        self.assertEqual(spec["paths"]["/presets"]["get"]["operationId"],
                         "list_presets")
        self.assertEqual(
            spec["paths"]["/update-preset"]["post"]["operationId"],
            "update_preset")


if __name__ == "__main__":
    unittest.main()
