#!/usr/bin/python3.12
"""test_quota_metrics.py — tests de quota-metrics.py (F1, OBJ-24).

Verifica: parseo de last-good (shapes reales de ollama/nanogpt/opencode),
conteo del board, fila JSONL correcta, y degradacion elegante cuando
falta un provider. TODO con fixtures tmp: nunca toca board ni last-good reales.
"""
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

PLUGIN = "REPO"
SPEC = importlib.util.spec_from_file_location(
    "quota_metrics", f"{PLUGIN}/scripts/quota-metrics.py")
qm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qm)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        qm.OUT = Path(self.tmp) / "metrics.jsonl"
        qm.LAST_GOOD_DIR = Path(self.tmp)
        self.db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?, ?)", [
            ("t_1", "running"), ("t_2", "running"), ("t_3", "ready"),
            ("t_4", "blocked"), ("t_5", "triage"), ("t_6", "done"),
        ])
        con.commit()
        con.close()
        qm.KANBAN_DB = self.db

    def write_lg(self, name, data):
        json.dump(data, open(Path(self.tmp) / f"{name}-last-good.json", "w"))


class TestCollect(Base):
    def test_row_shape_and_values(self):
        self.write_lg("ollama", {"session_pct": 4.4, "weekly_pct": 34.8})
        self.write_lg("nanogpt", {"state": "active", "weekly_tokens_pct": 77.13})
        self.write_lg("opencode_go", {"rolling_pct": 3.0, "weekly_pct": 74.0})
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["running"], 2)
        self.assertEqual(row["ready"], 1)
        self.assertEqual(row["blocked"], 1)
        self.assertEqual(row["triage"], 1)
        self.assertEqual(row["ollama_weekly_pct"], 34.8)
        self.assertEqual(row["nanogpt_weekly_pct"], 77.13)
        self.assertEqual(row["opencode_weekly_pct"], 74.0)
        self.assertEqual(row["providers_ok"], 3)

    def test_partial_providers(self):
        self.write_lg("ollama", {"session_pct": 10.0, "weekly_pct": 20.0})
        # nanogpt y opencode ausentes
        rc = qm.main()
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 1)
        self.assertIsNone(row.get("nanogpt_weekly_pct"))

    def test_no_providers_warns_but_records_board(self):
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 0)
        self.assertIn("NINGUN provider", buf.getvalue())

    def test_last_good_corrupto_no_rompe(self):
        (Path(self.tmp) / "ollama-last-good.json").write_text("{broken json")
        self.write_lg("nanogpt", {"weekly_tokens_pct": 5.0})
        rc = qm.main()
        self.assertEqual(rc, 0)
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertEqual(row["providers_ok"], 1)

    def test_request_ledger_fields_en_fila(self):
        # OBJ-26a follow-up: los acumuladores per-request llegan a la fila
        # cuando el merge all-homes tiene datos (override de homes fixture).
        home = tempfile.mkdtemp(prefix="qm-ledger-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        requests = Path(home, "quota-governor", "nanogpt-requests.jsonl")
        requests.parent.mkdir(parents=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(requests, "w") as fh:
            fh.write(json.dumps({"ts": ts, "costUsd": 4.87e-06,
                                 "paymentSource": "USD",
                                 "requestId": "req_x"}) + "\n")
        self.write_lg("ollama", {"session_pct": 1.0, "weekly_pct": 2.0})
        self.write_lg("nanogpt", {"weekly_tokens_pct": 3.0})
        prev = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES")
        os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = home
        try:
            qm.main()
        finally:
            if prev is None:
                os.environ.pop("QUOTA_GOVERNOR_PROFILE_HOMES", None)
            else:
                os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = prev
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertAlmostEqual(row["nanogpt_request_balance_usd"], 4.87e-06,
                               places=9)
        self.assertAlmostEqual(row["nanogpt_request_covered_usd"], 0.0,
                               places=9)

    def test_request_ledger_sin_datos_omite_campos(self):
        # Sin filas de capture en ningun home: campos ausentes (None), nunca
        # 0.0 disfrazado de "gasto cero". Override -> home vacio inexistente
        # ("" volveria a los defaults reales, que SI tienen datos en vivo).
        self.write_lg("ollama", {"session_pct": 1.0, "weekly_pct": 2.0})
        prev = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES")
        os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = os.path.join(
            self.tmp, "nothing-here")
        try:
            qm.main()
        finally:
            if prev is None:
                os.environ.pop("QUOTA_GOVERNOR_PROFILE_HOMES", None)
            else:
                os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = prev
        row = json.loads(qm.OUT.read_text().splitlines()[-1])
        self.assertNotIn("nanogpt_request_balance_usd", row)
        self.assertNotIn("nanogpt_request_covered_usd", row)

    def test_board_ilegible_mensaja_y_exit0(self):
        qm.KANBAN_DB = Path("/nonexistent/kanban.db")
        rc = qm.main()
        self.assertEqual(rc, 0)
        self.assertFalse(qm.OUT.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)