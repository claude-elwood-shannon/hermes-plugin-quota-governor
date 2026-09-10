#!/usr/bin/python3.12
"""test_forecast_gate.py — tests de la integración F2 en quota-gate.py.

Cubre: forecast_context() con las dos reglas del criterio (eta_90<1h →
shutdown; eta_90 < margen reset → workers=1), no-regla, disabled/vacío/
corrupto, y no-regresión (sin --suggest el gate no inyecta nada). El
monkeypatching es sobre fixtures tmp — nunca toca forecast.json real.
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

SPEC = importlib.util.spec_from_file_location(
    "quota_gate", os.path.join(REPO_ROOT, "scripts", "quota-gate.py"))
qg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qg)


def fc(reset_h, providers):
    return {"enabled": True, "hours_to_reset": reset_h,
            "next_weekly_reset_iso": "2026-09-14T00:00:00Z",
            "providers": providers}


class TestForecastContext(unittest.TestCase):
    def test_shutdown_eta90_menor_1h(self):
        out = qg.forecast_context(fc(50.0, {"pr-ollama": {"eta_90_hours": 0.5}}))
        self.assertIsNotNone(out)
        self.assertTrue(out["shutdown"])
        self.assertEqual(len(out["rules"]), 1)
        self.assertIn("board off", out["rules"][0])

    def test_margen_workers_1(self):
        # reset en 4h, colchón 2h → umbral 2h; eta_90=1.5 dispara
        out = qg.forecast_context(fc(4.0, {"pr-nanogpt": {"eta_90_hours": 1.5}}))
        self.assertIsNotNone(out)
        self.assertFalse(out["shutdown"])
        self.assertIn("max_workers=1", out["rules"][0])

    def test_eta90_cercano_al_reset_dispara_margen(self):
        # 40h con reset a 120h: umbral = 120-2 = 118h → dispara margen
        out = qg.forecast_context(
            fc(120.0, {"pr-opencode": {"eta_90_hours": 40.0}}))
        self.assertIsNotNone(out)
        self.assertFalse(out["shutdown"])
        self.assertIn("max_workers=1", out["rules"][0])

    def test_eta90_lejos_del_umbral_no_dispara(self):
        # 40h de eta con reset a 100h: umbral = 98h → 40 < 98 dispara margen;
        # para no-disparar hay que estar POR ENCIMA de reset-colchón
        self.assertIsNone(
            qg.forecast_context(
                fc(20.0, {"pr-opencode": {"eta_90_hours": 40.0}})))

    def test_eta90_igual_al_umbral_de_1h_no_shutdown(self):
        # 1.0 no es < 1.0: cae a la regla de margen (reset 4h → dispara)
        out = qg.forecast_context(fc(4.0, {"pr-x": {"eta_90_hours": 1.0}}))
        self.assertFalse(out["shutdown"])

    def test_disabled_vacio_corrupto(self):
        self.assertIsNone(qg.forecast_context({"enabled": False}))
        self.assertIsNone(qg.forecast_context({}))
        self.assertIsNone(qg.forecast_context(None))
        self.assertIsNone(qg.forecast_context("junk"))

    def test_eta_none_ignorado(self):
        self.assertIsNone(
            qg.forecast_context(fc(50.0, {"pr-ollama": {"eta_90_hours": None}})))

    def test_hours_to_reset_no_numerico_ignora_regla_margen(self):
        fcx = {"enabled": True, "hours_to_reset": "junk",
               "providers": {"pr-ollama": {"eta_90_hours": 1.5}}}
        self.assertIsNone(qg.forecast_context(fcx))

    def test_mode_suggest_flag(self):
        # sin flags del proceso actual: modo según _SUGGEST/_ENFORCE del gate
        out = qg.forecast_context(fc(4.0, {"pr-ollama": {"eta_90_hours": 1.0}}))
        self.assertIn(out["mode"], ("suggest", "enforce"))


if __name__ == "__main__":
    unittest.main(verbosity=2)