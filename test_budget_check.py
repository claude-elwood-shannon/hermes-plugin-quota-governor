#!/usr/bin/python3.12
"""test_budget_check.py — tests de budget_check.py (F3, OBJ-24).

Verifica: parseo del cost: tag, cálculo de cuota libre desde forecast,
criterio 10% de cuota libre (reassign / triage), exención de profiles
fuera de G1 y providers sin datos, y modo observador (dry-run sin mutar
el board real — DB fixture en tmp).
"""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

PLUGIN = "REPO"
SPEC = importlib.util.spec_from_file_location(
    "budget_check", f"{PLUGIN}/scripts/budget_check.py")
bc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bc)


def forecast_of(pcts):
    """Construye forecast F2 con pct_now dado por profile."""
    return {"enabled": True,
            "providers": {p: {"pct_now": pct, "eta_90_hours": None}
                          for p, pct in pcts.items()}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, "
                    "assignee TEXT, body TEXT, status TEXT)")
        con.commit()
        con.close()

    def add_task(self, tid, profile, cost_tag, status="ready",
                 title="t"):
        body = (f"objective:OBJ-24\ncost:{cost_tag}\n" if cost_tag else
                "objective:OBJ-24\n")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                    (tid, title, profile, body, status))
        con.commit()
        con.close()


class TestParse(unittest.TestCase):
    def test_parse_cost_tag(self):
        self.assertEqual(bc.parse_cost_tag("hola\ncost:small\n"), "small")
        self.assertEqual(bc.parse_cost_tag("cost:tiny"), "tiny")
        self.assertIsNone(bc.parse_cost_tag("sin tag"))
        self.assertIsNone(bc.parse_cost_tag("cost:unknownx"))
        self.assertIsNone(bc.parse_cost_tag(None))

    def test_free_pct(self):
        fc = forecast_of({"pr-ollama": 60.0})  # 60% usado → 40% libre
        self.assertEqual(bc.provider_free_pct(fc, "pr-ollama"), 40.0)
        self.assertIsNone(bc.provider_free_pct(fc, "pr-nanogpt"))
        self.assertIsNone(bc.provider_free_pct({}, "pr-ollama"))

    def test_free_pct_clampea_negativos(self):
        fc = forecast_of({"pr-ollama": 110.0})  # pct_now=110 (corrupto)
        self.assertEqual(bc.provider_free_pct(fc, "pr-ollama"), 0.0)


class TestEvaluate(Base):
    def test_cabe_no_decision(self):
        # small (2.1%) vs libre 40% (pct_now=60): umbral 4% → cabe
        self.add_task("t_1", "pr-ollama", "small")
        fc = forecast_of({"pr-ollama": 60.0, "pr-nanogpt": 90.0})
        self.assertEqual(bc.evaluate(db_path=self.db, forecast=fc), [])

    def test_excede_reassign_al_mas_holgado(self):
        # medium (4%) vs libre 20% (pct_now=80): umbral 2% → excede;
        # nanogpt libre 80% (pct_now=20): umbral 8% → cabe → reassign
        self.add_task("t_1", "pr-ollama", "medium")
        fc = forecast_of({"pr-ollama": 80.0, "pr-nanogpt": 20.0})
        d = bc.evaluate(db_path=self.db, forecast=fc)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["verdict"], "reassign")
        self.assertEqual(d[0]["to_profile"], "pr-nanogpt")

    def test_excede_sin_alternativa_triage(self):
        # complex (24.8%): en ningún provider cabe (ambos con libre 40%:
        # umbral 4%) → triage
        self.add_task("t_1", "pr-ollama", "complex")
        fc = forecast_of({"pr-ollama": 60.0, "pr-nanogpt": 60.0})
        d = bc.evaluate(db_path=self.db, forecast=fc)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["verdict"], "triage")
        self.assertIn("sin alternativa", d[0]["reason"])

    def test_sin_datos_provider_exento(self):
        # provider no está en el forecast → no veto (cero falsos positivos)
        self.add_task("t_1", "pr-ollama", "complex")
        fc = forecast_of({"pr-nanogpt": 95.0})
        self.assertEqual(bc.evaluate(db_path=self.db, forecast=fc), [])

    def test_profile_fuera_g1_exento(self):
        self.add_task("t_1", "pr-openrouter", "complex")
        fc = forecast_of({"pr-ollama": 99.0, "pr-nanogpt": 99.0,
                          "pr-opencode": 99.0})
        self.assertEqual(bc.evaluate(db_path=self.db, forecast=fc), [])

    def test_sin_tag_usa_default_tiny(self):
        # tiny (0.5%) vs libre 0.5% (pct_now=99.5): umbral 0.05 → excede,
        # sin alt → triage
        self.add_task("t_1", "pr-opencode", None)
        fc = forecast_of({"pr-opencode": 99.5})
        d = bc.evaluate(db_path=self.db, forecast=fc)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["class"], "tiny")

    def test_status_no_elegible_exento(self):
        self.add_task("t_1", "pr-ollama", "complex", status="running")
        fc = forecast_of({"pr-ollama": 99.0})
        self.assertEqual(bc.evaluate(db_path=self.db, forecast=fc), [])

    def test_observe_no_mutar_board(self):
        self.add_task("t_1", "pr-ollama", "complex")
        fc = forecast_of({"pr-ollama": 60.0, "pr-nanogpt": 60.0})
        bc.evaluate(db_path=self.db, forecast=fc, enforce=False)
        con = sqlite3.connect(self.db)
        st = con.execute(
            "SELECT status, assignee FROM tasks WHERE id='t_1'").fetchone()
        con.close()
        self.assertEqual((st[0], st[1]), ("ready", "pr-ollama"))


class TestDryRunRealBoard(unittest.TestCase):
    def test_board_real_solo_lectura(self):
        # el board real existe: evaluate en modo observe no debe fallar
        # ni mutar (usa el forecast real, que puede estar vacío)
        decisions = bc.evaluate(forecast=bc.load_forecast(), enforce=False)
        self.assertIsInstance(decisions, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)