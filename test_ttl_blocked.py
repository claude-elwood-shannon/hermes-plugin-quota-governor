"""Tests P1 — TTL de blocked con autorremediación y triage automático.

Cubre los 8 criterios del brief:
 1. "user needed to set task id"  -> R1 unblock <15m
 2. model_override inválido       -> R2 limpia y reasigna -> unblock
 3. dependencia resuelta          -> R3 unblock
 4. crash loop 3x mismo error     -> triage directo con historial
 5. [human-gate] en body          -> TTL no toca
 6. DIRECCION-STOP activo         -> clasifica pero no actúa
 7. >30m no-clasificable          -> triage con comentario
 8. idempotencia: ya en triage no se re-mueve
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import ttl_blocked as m  # noqa: E402


class TtlBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "kanban.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,
            status TEXT, model_override TEXT, provider_override TEXT,
            body TEXT, created_at REAL);
        """)
        self._extra_schema(con)
        con.commit()
        con.close()

    def _extra_schema(self, con):
        con.executescript("""
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, kind TEXT, payload TEXT, created_at REAL);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, error TEXT);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        """)

    def tearDown(self):
        self.tmp.cleanup()

    def add_blocked(self, tid, *, reason="algo", age_s=0, override=None,
                    provider="ollama-cloud", body="objective:OBJ-44 | clase:C",
                    title="t", events=None, runs=None, links=None):
        now = time.time()
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                    (tid, title, "pr-ollama", "blocked", override, provider,
                     body, now - 3600))
        con.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?,?,?,?)",
                    (tid, "blocked", json.dumps({"reason": reason}), now - age_s))
        for e in (events or []):
            con.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?)", (tid, e[0], e[1], now - e[2]))
        for err, age in (runs or []):
            con.execute("INSERT INTO task_runs (task_id, error) VALUES (?,?)",
                        (tid, err))
        for p in (links or []):
            con.execute("INSERT INTO task_links VALUES (?,?)", (p, tid))
        con.commit()
        con.close()

    def run_ttl(self, execute):
        return m.run(execute=execute, db_path=self.db, root=self.root)


class TestTtl(TtlBase):

    def test_1_self_generated_id_unblocks(self):
        """(1) 'user needed to set task id' -> R1 unblock en <15m."""
        self.add_blocked("t_r1", reason="user needed to set task id", age_s=6 * 60)
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_r1"]
        self.assertEqual(acts[0].action, "remediated")
        self.assertIn("R1", acts[0].detail)
        # unblock + comment llamados
        called = [c.args for c in cli.call_args_list]
        self.assertTrue(any("unblock" in c for c in called))
        self.assertTrue(any("comment" in c for c in called))

    def test_2_poisoned_override_reassigned(self):
        """(2) model_override inválido -> R2 limpia, reasigna pin gate, unblock."""
        self.add_blocked("t_r2", reason="Model override is unavailable (HTTP 404)",
                         age_s=6 * 60, override="gpt-oss:20b")
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_r2"]
        self.assertEqual(acts[0].action, "remediated")
        self.assertIn("R2", acts[0].detail)
        called = [c.args for c in cli.call_args_list]
        self.assertTrue(any("set-model" in c for c in called))

    def test_3_resolved_dependency_unblocks(self):
        """(3) dependencia done -> R3 unblock."""
        now = time.time()
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                    ("t_padre", "padre", "pr-ollama", "done", None, None,
                     "x", now - 7200))
        con.commit()
        con.close()
        self.add_blocked("t_r3", reason="waiting on parent task", age_s=6 * 60,
                         links=["t_padre"])
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_r3"]
        self.assertEqual(acts[0].action, "remediated")
        self.assertIn("R3", acts[0].detail)

    def test_4_crash_loop_goes_triage(self):
        """(4) crash loop 3x mismo error -> triage directo con historial."""
        err = "worker exited cleanly (rc=0) without terminal call"
        self.add_blocked("t_r4", reason=err, age_s=2 * 60,
                         runs=[(err, 10), (err, 9), (err, 8)])
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_r4"]
        self.assertEqual(acts[0].action, "triaged")
        called = [c.args for c in cli.call_args_list]
        # move a triage vía doble-block (canal oficial CLI)
        self.assertTrue(any("block" in c for c in called))
        self.assertTrue(any("comment" in c for c in called))     # comentario estructurado

    def test_5_human_gate_untouched(self):
        """(5) [human-gate] en body -> el TTL no actúa."""
        self.add_blocked("t_hg", reason="espera aprobación del usuario",
                         age_s=45 * 60, body="espera aprobación [human-gate]")
        res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_hg"]
        self.assertEqual(acts[0].action, "skip-human-gate")

    def test_6_stop_signal_no_mutation(self):
        """(6) DIRECCION-STOP activo -> clasifica pero no actúa."""
        self.add_blocked("t_stop", reason="Scratch artifact unavailable", age_s=40 * 60)
        (self.root / "quota-governor").mkdir(parents=True, exist_ok=True)
        (self.root / "quota-governor" / "STOP").write_text("x")
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_stop"]
        self.assertEqual(acts[0].action, "skipped-stop")
        cli.assert_not_called()

    def test_7_over_30m_unclassifiable_triage(self):
        """(7) >30m sin clasificación posible -> triage 'no-clasificable'."""
        self.add_blocked("t_old", reason="", age_s=35 * 60)  # sin razón registrada
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_old"]
        self.assertEqual(acts[0].action, "triaged")
        called = [c for c in cli.call_args_list]
        # el comentario de triage lleva el formato estructurado
        comment_calls = [c for c in called if "comment" in c[0]]
        self.assertTrue(comment_calls)
        body = comment_calls[0].args[2]
        self.assertIn("[TTL-BLOCKED]", body)
        self.assertIn("no-clasificable", body)

    def test_8_idempotent_triage(self):
        """(8) tarea ya en triage no se re-mueve (nunca entra al escaneo)."""
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                    ("t_tri", "t", "pr-ollama", "triage", None, None, "x",
                     time.time() - 3600))
        con.commit()
        con.close()
        res = self.run_ttl(execute=True)
        self.assertFalse([r for r in res if r.task_id == "t_tri"])

    def test_9_window_0_5m_only_classifies(self):
        """Ventana 0-5m: solo clasificación, ninguna mutación."""
        self.add_blocked("t_fresh", reason="Scratch artifact unavailable", age_s=2 * 60)
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=True)
        acts = [r for r in res if r.task_id == "t_fresh"]
        self.assertEqual(acts[0].action, "classify")
        cli.assert_not_called()

    def test_10_dry_run_no_mutation(self):
        """Sin --execute: clasifica/remedia 'en papel' pero no llama CLI."""
        self.add_blocked("t_dry", reason="user needed to set task id", age_s=6 * 60)
        with mock.patch.object(m, "_cli") as cli:
            cli.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            res = self.run_ttl(execute=False)
        acts = [r for r in res if r.task_id == "t_dry"]
        self.assertIn(acts[0].action, ("remediated", "classify"))
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
