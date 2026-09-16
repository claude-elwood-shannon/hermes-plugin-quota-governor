#!/usr/bin/python3.12
"""test_forecast_contract.py — contrato writer↔consumers del forecast (OBJ-24).

El bug OBJ-24: quota-forecast.py escribía los datos por provider al TOP
LEVEL (forecast["pr-ollama"]) mientras el header "providers" quedaba {}
— pero el gate (forecast_context) y budget_check.py leen SOLO
forecast["providers"], así que las reglas vivas nunca disparaban. Los
fixtures de gate/budget usaban el shape anidado y no lo capturaban.

Este test regula el shape del writer contra los readers REALES del repo
(sin fixtures paralelos): ejecuta quota-forecast.main() con history
fixture y alimenta la salida a quota_gate.forecast_context() y a
budget_check tal cual los consumen en producción.
"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = str(Path(__file__).resolve().parent)
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qf = _load("quota_forecast", f"{PLUGIN}/scripts/quota-forecast.py")
qg = _load("quota_gate", f"{PLUGIN}/scripts/quota-gate.py")
bc = _load("budget_check", f"{PLUGIN}/scripts/budget_check.py")


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_history(path, base=70.0, steps=4, dt=900):
    """History 6h con burn constante: +2% cada 15m para pr-ollama."""
    now = time.time()
    rows = []
    for i in range(steps):
        ts = now - dt * (steps - 1 - i)
        row = {"ts": iso(ts), "running": 1, "ready": 0,
               "blocked": 0, "triage": 0,
               "ollama_weekly_pct": base + i * 2.0,
               "nanogpt_weekly_pct": base,
               "opencode_weekly_pct": base}
        rows.append(row)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestWriterConsumerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = Path(self.tmp) / "metrics.jsonl"
        self.out = Path(self.tmp) / "forecast.json"
        qf.HISTORY = self.hist
        qf.OUT = self.out

    def _run_writer(self):
        make_history(self.hist)
        self.assertEqual(qf.main(), 0)
        return json.loads(self.out.read_text())

    def test_writer_nidifica_en_providers(self):
        """El writer ESCRIBE el shape que los consumers LEEN."""
        d = self._run_writer()
        self.assertTrue(d["enabled"])
        self.assertEqual(d["providers_ok"], 3)
        self.assertEqual(set(d["providers"]),
                         {"pr-ollama", "pr-nanogpt", "pr-opencode"})
        # y NO hay keys de provider sueltos en el top level
        for prov in ("pr-ollama", "pr-nanogpt", "pr-opencode"):
            self.assertNotIn(prov, d)
        og = d["providers"]["pr-ollama"]
        self.assertIn("pct_now", og)
        self.assertIn("eta_90_hours", og)

    def test_gate_forecast_context_ve_los_3_providers(self):
        """forecast_context() del gate itera providers REALES, no {}."""
        d = self._run_writer()
        # burn 2%/15min = 0.1333 %/min; pct 76 → eta_90 ≈ 2.6h.
        # hours_to_reset real (~122h) → umbral margen 120h → DISPARA.
        out = qg.forecast_context(d)
        self.assertIsNotNone(out)
        self.assertFalse(out["shutdown"])
        self.assertTrue(out["rules"])
        self.assertIn("max_workers=1", out["rules"][0])

    def test_budget_check_lee_free_quota_del_writer(self):
        """evaluate() de budget_check consume el forecast del writer."""
        d = self._run_writer()
        db = Path(self.tmp) / "kanban.db"
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, "
                     "assignee TEXT, body TEXT, status TEXT)")
        # una tarea small en pr-ollama: free ~24% → umbral 2.4% → cabe
        conn.execute("INSERT INTO tasks VALUES ('t_1','t','pr-ollama',"
                     "'objective:OBJ-24\ncost:small\n','ready')")
        conn.commit()
        conn.close()
        decisiones = bc.evaluate(db_path=str(db), forecast=d)
        # la tarea small CABE (2.1% <= 10% de ~24% libre): sin veto. Lo
        # que importa del contrato es que el provider SÍ es legible —
        # sin el fix, evaluate() veía 0 providers y nunca cruzaba nada.
        for dec in decisiones:
            self.assertIn(dec.get("profile"), d["providers"])

    def test_backtest_harness_lee_ambos_shapes_igual(self):
        """backtest-f2.forecast_providers() no cambia con el nuevo shape."""
        bf = _load("backtest_f2", f"{PLUGIN}/scripts/backtest-f2.py")
        d = self._run_writer()
        provs = bf.forecast_providers(d)
        self.assertEqual(set(provs),
                         {"pr-ollama", "pr-nanogpt", "pr-opencode"})
        self.assertEqual(provs["pr-ollama"]["pct_now"], 76.0)
        snap = bf.snapshot_record(d)
        self.assertIsNotNone(snap)
        self.assertEqual(len(snap["providers"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
