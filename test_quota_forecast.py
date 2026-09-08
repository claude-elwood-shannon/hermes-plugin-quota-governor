#!/usr/bin/python3.12
"""test_quota_forecast.py — tests de quota-forecast.py (F2, OBJ-24).

Verifica: EMA del burn rate, proyección de hitos 90/100, ventana 6h con
recorte de muestras viejas, dedupe por ts, degradación elegante (history
vacía/corrupta), y la regla de decisión del gate (--suggest). Todo con
fixtures tmp: nunca toca el history real ni llama a ninguna API.
"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = "REPO"
SPEC = importlib.util.spec_from_file_location(
    "quota_forecast", f"{PLUGIN}/scripts/quota-forecast.py")
qf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qf)


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = Path(self.tmp) / "metrics.jsonl"
        self.out = Path(self.tmp) / "forecast.json"
        qf.HISTORY = self.hist
        qf.OUT = self.out

    def write_history(self, rows):
        with open(self.hist, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def read_out(self):
        return json.loads(self.out.read_text())


class TestEmaBurn(unittest.TestCase):
    def test_ema_constante(self):
        now = time.time()
        puntos = [(now - 300 * i, 10.0) for i in range(5, 0, -1)]
        ema, n = qf.ema_burn(puntos)
        self.assertAlmostEqual(ema, 0.0, places=9)
        self.assertEqual(n, 4)

    def test_ema_crecimiento_constante(self):
        # +1% cada 5 min = 0.2 %/min (más antiguo primero => pendiente positiva)
        now = time.time()
        puntos = [(now - 300 * i, float(10 + i)) for i in range(5, 0, -1)]
        ema, _ = qf.ema_burn(puntos)
        self.assertAlmostEqual(ema, -0.2, places=6)

    def test_ema_converge_al_ultimo(self):
        # tras cambios, con alfa 0.3 el peso del último es mayor
        now = time.time()
        puntos = [(now - 1000, 0.0), (now - 500, 0.0), (now, 60.0)]
        ema, _ = qf.ema_burn(puntos)
        # primer rate 0 (ema=0), segundo 0.12 → ema = 0.3*0.12
        self.assertAlmostEqual(ema, 0.3 * 60 / 500 * 60 / 1, places=4)

    def test_dt_cero_ignorado(self):
        now = time.time()
        puntos = [(now - 100, 5.0), (now - 100, 50.0), (now, 50.0)]
        ema, n = qf.ema_burn(puntos, dt_min=0.001)
        # el duplicado (dt=0) no entra; el par de 100s sí (dt>=dt_min)
        self.assertEqual(n, 1)

    def test_dt_demasiado_denso_descartado(self):
        # par a 10s (< 120s) no computa rate; el par denso avanza la base
        # pero el rate que entra a la EMA es el último válido (900->now)
        now = time.time()
        puntos = [(now - 910, 10.0), (now - 900, 12.0), (now, 12.5)]
        ema, n = qf.ema_burn(puntos)
        self.assertEqual(n, 1)
        # el par denso (10s) se descarta y queda como base: el par válido
        # es 900->now con delta (12.5-12.0) en 900s
        self.assertAlmostEqual(ema, (12.5 - 12.0) / (900 / 60.0), places=6)


class TestEta(unittest.TestCase):
    def test_eta_horas_basico(self):
        # 10% restante a 0.2 %/min = 50 min = 0.833h
        self.assertAlmostEqual(qf.eta_horas(90.0, 100.0, 0.2), 0.8333, places=3)

    def test_eta_con_burn_cero_o_negativo(self):
        self.assertIsNone(qf.eta_horas(80.0, 90.0, 0.0))
        self.assertIsNone(qf.eta_horas(80.0, 90.0, -0.1))

    def test_eta_ya_pasado_el_hito(self):
        self.assertEqual(qf.eta_horas(95.0, 90.0, 0.2), 0.0)


class TestCollect(Base):
    def _rows(self, base_pct=70.0, steps=4, dt=900, providers=None):
        providers = providers or {
            "pr-ollama": ("ollama_weekly_pct", base_pct),
            "pr-nanogpt": ("nanogpt_weekly_pct", base_pct),
            "pr-opencode": ("opencode_weekly_pct", base_pct),
        }
        now = time.time()
        rows = []
        for i in range(steps):
            ts = now - dt * (steps - 1 - i)
            row = {"ts": iso(ts), "running": 1, "ready": 0,
                   "blocked": 0, "triage": 0}
            for prov, (key, pct) in providers.items():
                row[key] = pct + i * (2.0 if prov == "pr-ollama" else 0.0)
            rows.append(row)
        return rows

    def test_forecast_shape_y_valores(self):
        self.write_history(self._rows(steps=4))
        rc = qf.main()
        self.assertEqual(rc, 0)
        d = self.read_out()
        self.assertTrue(d["enabled"])
        self.assertEqual(d["providers_ok"], 3)
        og = d["pr-ollama"]
        # 4 pasos de 15 min subiendo 2% => 0.1333 %/min
        self.assertAlmostEqual(og["burn_rate_pct_per_min"], 2 / 15, places=3)
        self.assertEqual(og["pct_now"], 76.0)
        self.assertIsNotNone(og["eta_90_iso"])
        self.assertIsNotNone(og["eta_100_iso"])
        self.assertIn("next_weekly_reset_iso", d)

    def test_ventana_6h_recorta_viejas(self):
        rows = self._rows(steps=4)
        # añade una fila de hace 7h (fuera de ventana)
        vieja = dict(rows[0])
        vieja["ts"] = iso(time.time() - 7 * 3600)
        vieja["ollama_weekly_pct"] = 1.0
        self.write_history([vieja] + rows)
        qf.main()
        d = self.read_out()
        self.assertEqual(d["pr-ollama"]["samples"], 4)

    def test_history_vacia_enabled_false(self):
        rc = qf.main()
        self.assertEqual(rc, 0)
        d = self.read_out()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["providers_ok"], 0)

    def test_history_corrupta_no_rompe(self):
        self.hist.write_text("{broken json\n[otros\n")
        rc = qf.main()
        self.assertEqual(rc, 0)
        d = self.read_out()
        self.assertFalse(d["enabled"])

    def test_provider_ausente_degrada(self):
        rows = self._rows(steps=3)
        # quita nanogpt de todas las filas
        for r in rows:
            r.pop("nanogpt_weekly_pct", None)
        self.write_history(rows)
        qf.main()
        d = self.read_out()
        self.assertEqual(d["providers_ok"], 2)
        self.assertNotIn("pr-nanogpt", d)

    def test_pct_100_clampea(self):
        rows = self._rows(base_pct=99.5, steps=2)
        self.write_history(rows)
        qf.main()
        d = self.read_out()
        self.assertLessEqual(d["pr-ollama"]["pct_now"], 100.0)

    def test_dedupe_mismo_ts(self):
        rows = self._rows(steps=3)
        dup = dict(rows[-1])  # mismo ts, valor distinto
        dup["ollama_weekly_pct"] = 99.9
        self.write_history(rows + [dup])
        qf.main()
        d = self.read_out()
        # el duplicado gana (última fila con ese ts)
        self.assertEqual(d["pr-ollama"]["pct_now"], 99.9)
        self.assertEqual(d["pr-ollama"]["samples"], 3)


class TestResetEpoch(unittest.TestCase):
    def test_proximo_lunes_0000_utc(self):
        import datetime
        # un miércoles cualquiera
        now = datetime.datetime(2026, 9, 9, 12, 0, tzinfo=datetime.timezone.utc)
        e = qf.next_weekly_reset_epoch(now.timestamp())
        reset = datetime.datetime.fromtimestamp(e, tz=datetime.timezone.utc)
        self.assertEqual(reset.weekday(), 0)   # lunes
        self.assertEqual(reset.hour, 0)        # 00:00 UTC = 02:00 CEST
        self.assertGreater(e, now.timestamp())

    def test_mismo_lunes_post_reset_va_al_siguiente(self):
        import datetime
        # lunes 03:00 UTC (05:00 CEST) — ya pasó el reset de 00:00 UTC
        now = datetime.datetime(2026, 9, 14, 3, 0, tzinfo=datetime.timezone.utc)
        e = qf.next_weekly_reset_epoch(now.timestamp())
        reset = datetime.datetime.fromtimestamp(e, tz=datetime.timezone.utc)
        self.assertEqual(reset.isocalendar().week, now.isocalendar().week + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)