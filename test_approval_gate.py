#!/usr/bin/python3.12
"""Tests for approval_gate.py + approval-ready-fix.py (P5). Fixtures only."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _HERE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ag = _load("approval_gate", "scripts/approval_gate.py")
af = _load("approval_ready_fix", "scripts/approval-ready-fix.py")

FULL_BODY = """objective:OBJ-31 | cost:tiny | auto_created:true
approval-ready

## Paquete de approval

- Presupuesto estimado: $0.10 (pr-ollama, gpt-oss:20b)
- Modelo asignado: gpt-oss:20b
- Perfil: pr-ollama
- Fecha estimada de entrega: 2 dias
- Success criterion: informe generado en docs/ con 3 secciones
- Clase: C
- Objetivo: objective:OBJ-31

[APPROVAL: pending]
"""


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "kanban.db"
        os.environ["AG_KANBAN_DB"] = str(self.db)
        os.environ["AF_KANBAN_DB"] = str(self.db)
        os.environ["AG_HERMES_ROOT"] = str(self.root)
        os.environ["AF_HERMES_ROOT"] = str(self.root)
        os.environ["AF_LOG"] = str(self.root / "approval-fixes.jsonl")
        os.environ["AG_STUB_CLI"] = "simulate"  # no hermes CLI in fixtures
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
            "assignee TEXT, status TEXT, body TEXT, created_at REAL);"
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT,"
            " run_id INTEGER, kind TEXT, payload TEXT, created_at REAL);")
        con.commit()
        con.close()

    def add(self, tid, title, body, status="triage", assignee="pr-ollama",
            created_at=1.0):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                    (tid, title, assignee, status, body, created_at))
        con.commit()
        con.close()

    def tearDown(self):
        for k in ("AG_KANBAN_DB", "AF_KANBAN_DB", "AG_HERMES_ROOT",
                  "AF_HERMES_ROOT", "AF_LOG", "AG_STUB_CLI"):
            os.environ.pop(k, None)


class TestApprovalGate(Harness):

    def test_1_tag_and_stamp_detection(self):
        self.add("t_b0000001", "OBJ-31 propuesta", FULL_BODY)
        pend = ag.fetch_pending(self.db)
        self.assertEqual([t["id"] for t in pend], ["t_b0000001"])
        self.assertEqual(ag.package_missing(FULL_BODY), [])

    def test_2_package_missing_detected(self):
        body = FULL_BODY.replace("- Perfil: pr-ollama\n", "") \
                        .replace("- Fecha estimada de entrega: 2 dias\n", "")
        self.assertEqual(sorted(ag.package_missing(body)),
                         ["fecha", "perfil"])

    def test_3_verdict_si_moves_to_ready(self):
        self.add("t_b0000002", "OBJ-31 propuesta", FULL_BODY)
        res = ag.apply_verdict(self.db, "t_b0000002", "si")
        # specify+promote go through the hermes CLI (absent in fixtures);
        # the stamp + CAS must still have applied.
        self.assertTrue(res["applied"], res)
        con = sqlite3.connect(self.db)
        body = con.execute("SELECT body FROM tasks WHERE id=?",
                           ("t_b0000002",)).fetchone()[0]
        con.close()
        self.assertIn("[APPROVAL: approved ", body)
        self.assertNotIn("[APPROVAL: pending]", body)

    def test_4_verdict_no_rejects(self):
        self.add("t_b0000003", "OBJ-31 propuesta", FULL_BODY)
        res = ag.apply_verdict(self.db, "t_b0000003", "no")
        self.assertTrue(res["applied"], res)
        con = sqlite3.connect(self.db)
        body = con.execute("SELECT body FROM tasks WHERE id=?",
                           ("t_b0000003",)).fetchone()[0]
        status = con.execute("SELECT status FROM tasks WHERE id=?",
                             ("t_b0000003",)).fetchone()[0]
        con.close()
        self.assertIn("[APPROVAL: rejected ", body)
        # archive is a CLI call (absent in fixtures) -> status stays triage
        # in the fixture; the CAS + stamp are the module's responsibility.
        self.assertEqual(status, "triage")

    def test_5_verdict_condicion(self):
        self.add("t_b0000004", "OBJ-31 propuesta", FULL_BODY)
        res = ag.apply_verdict(self.db, "t_b0000004", "condicion",
                               note="sin tocar config.yaml")
        self.assertTrue(res["applied"], res)
        con = sqlite3.connect(self.db)
        body = con.execute("SELECT body FROM tasks WHERE id=?",
                           ("t_b0000004",)).fetchone()[0]
        con.close()
        self.assertIn("[APPROVAL: approved-with ", body)
        self.assertIn("[condicion: sin tocar config.yaml]", body)

    def test_6_verdict_on_wrong_status_refused(self):
        self.add("t_b0000005", "OBJ-31 propuesta", FULL_BODY,
                 status="ready")
        res = ag.apply_verdict(self.db, "t_b0000005", "si")
        self.assertFalse(res["applied"])
        self.assertIn("not triage", res["reason"])

    def test_7_already_rejected_refused(self):
        body = FULL_BODY.replace("[APPROVAL: pending]",
                                 "[APPROVAL: rejected 2026-09-13T00:00:00Z]")
        self.add("t_b0000006", "OBJ-31 propuesta", body)
        res = ag.apply_verdict(self.db, "t_b0000006", "si")
        self.assertFalse(res["applied"])
        self.assertIn("already rejected", res["reason"])

    def test_8_dedup_groups_same_signature(self):
        self.add("t_b0000007", "OBJ-31 hardening clasificador", FULL_BODY,
                 created_at=1.0)
        self.add("t_b0000008", "OBJ-31 hardening clasificador", FULL_BODY,
                 created_at=2.0)
        groups = ag.dedup_scan(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keep"], "t_b0000007")
        self.assertEqual(groups[0]["duplicates"], ["t_b0000008"])

    def test_9_dedup_ignores_different_criterion(self):
        other = FULL_BODY.replace("informe generado en docs/ con 3 secciones",
                                  "audiolibro transcrito en 4 capítulos")
        other = other.replace("OBJ-31", "OBJ-32")
        self.add("t_b0000009", "OBJ-31 hardening clasificador", FULL_BODY)
        self.add("t_b0000010", "OBJ-32 otra cosa", other)
        self.assertEqual(ag.dedup_scan(self.db), [])

    def test_10_incomplete_package_not_tagged_by_fix(self):
        body = FULL_BODY.replace("objective:OBJ-31 | cost:tiny", "") \
                        .replace("- Clase: C\n", "")
        self.add("t_b0000011", "propuesta incompleta", body)
        rc = af.main(["--execute"])
        self.assertEqual(rc, 0)
        con = sqlite3.connect(self.db)
        new_body = con.execute("SELECT body FROM tasks WHERE id=?",
                               ("t_b0000011",)).fetchone()[0]
        con.close()
        self.assertNotRegex(new_body, r"(?im)^\s*approval-ready\s*$")
        self.assertIn("[APPROVAL: pending]", new_body)  # stamp preserved

    def test_11_fix_fills_missing_fields(self):
        body = FULL_BODY.replace("- Perfil: pr-ollama\n", "") \
                        .replace("- Fecha estimada de entrega: 2 dias\n", "")
        self.add("t_b0000012", "OBJ-31 propuesta", body)
        af.main(["--execute"])
        con = sqlite3.connect(self.db)
        new_body = con.execute("SELECT body FROM tasks WHERE id=?",
                               ("t_b0000012",)).fetchone()[0]
        con.close()
        self.assertEqual(ag.package_missing(new_body), [])

    def test_12_fix_archives_duplicates(self):
        self.add("t_b0000013", "OBJ-31 hardening clasificador", FULL_BODY,
                 created_at=1.0)
        self.add("t_b0000014", "OBJ-31 hardening clasificador", FULL_BODY,
                 created_at=2.0)
        af.main(["--execute"])
        # archive is CLI-driven (absent in fixture): dedup still REPORTS
        # both candidates in the log for the operator.
        log = Path(os.environ["AF_LOG"])
        self.assertTrue(log.exists())
        recs = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertTrue(any(r.get("action") == "duplicate-archived"
                            for r in recs))

    def test_13_dry_run_writes_nothing(self):
        body = FULL_BODY.replace("- Perfil: pr-ollama\n", "")
        self.add("t_b0000015", "OBJ-31 propuesta", body)
        af.main(["--dry-run"])
        con = sqlite3.connect(self.db)
        new_body = con.execute("SELECT body FROM tasks WHERE id=?",
                               ("t_b0000015",)).fetchone()[0]
        con.close()
        self.assertEqual(new_body, body)  # unchanged
        self.assertFalse(Path(os.environ["AF_LOG"]).exists())

    def test_14_non_triage_never_touched(self):
        self.add("t_b0000016", "OBJ-31 propuesta",
                 FULL_BODY.replace("- Perfil: pr-ollama\n", ""),
                 status="ready")
        af.main(["--execute"])
        con = sqlite3.connect(self.db)
        new_body = con.execute("SELECT body FROM tasks WHERE id=?",
                               ("t_b0000016",)).fetchone()[0]
        con.close()
        self.assertNotIn("perfil: pr-ollama", new_body.lower().replace(
            "- perfil:", "perfil:"))


if __name__ == "__main__":
    sys.exit(unittest.main())
