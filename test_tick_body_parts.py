#!/usr/bin/python3.12
"""test_tick_body_parts.py — OBJ-39-REBELION: done-verification vs body +
successors from bodies. Fixtures only: no network, no real kanban.db, no
real CLI (create_task/comment_task monkeypatched). Companion to the
tick-cola-viva test; same conventions (fixed epoch, temp sqlite fixture).

Run: /usr/bin/python3.12 test_tick_body_parts.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

GOV_DIR = Path(__file__).resolve().parent
SCRIPT = GOV_DIR / "scripts" / "tick_body_parts.py"

_spec = importlib.util.spec_from_file_location("tick_body_parts", SCRIPT)
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

NOW = 1789001175.0  # fixed epoch: deterministic, never wall-clock

MARATON_BODY = (
    "objective:OBJ-38 | cost:tiny (todo local, cero USD) | privacy:high | "
    "clase:C (tareas de prueba sobre el worker local)\n\n"
    "Protocolo termico...\n"
    "R1 (esta): clasificar los 10 bodies kanban mas recientes -> JSON.\n"
    "R2: resumir en 3 lineas cada uno de los 5 skills del repo.\n"
    "R3: extraer campos estructurados del trace.jsonl.\n"
    "R4: generar el changelog de la semana del repo.\n"
    "R5: revisar los logs vllm.err.log y clasificar errores.\n"
    "R6+: si el usuario no ha vuelto y hay cuota, repetir o parar.\n\n"
    "Criterio: >= 4 rondas completadas con datos.\n"
)

FIRST_ROUND_SUMMARY = ("First round of classification completed. "
                       "Temperature and thermal metrics recorded.")


def _mk_db(path, tasks, done=None, runs=None, comments=None):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, "
                "assignee TEXT, body TEXT, result TEXT, completed_at REAL, "
                "created_at REAL)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT, summary TEXT)")
    con.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, task_id TEXT, body TEXT)")
    all_tasks = list(tasks)
    for d in (done or []):
        all_tasks.append({**d, "status": "done"})
    for t in all_tasks:
        con.execute("INSERT INTO tasks (id, title, status, assignee, body, "
                    "result, completed_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (t["id"], t.get("title"), t.get("status"),
                     t.get("assignee"), t.get("body"), t.get("result"),
                     t.get("completed_at"), t.get("created_at", 0)))
    for tid, s in (runs or []):
        con.execute("INSERT INTO task_runs (task_id, summary) VALUES (?,?)",
                    (tid, s))
    for tid, c in (comments or []):
        con.execute("INSERT INTO task_comments (task_id, body) VALUES (?,?)",
                    (tid, c))
    con.commit()
    con.close()


def _maraton_done(completed=NOW - 3600, result=None):
    return {"id": "t_maraton", "title": "maraton vLLM nocturno",
            "assignee": "pr-vllm", "body": MARATON_BODY, "result": result,
            "completed_at": completed}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bodyparts-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        self.addCleanup(os.environ.pop, "HERMES_KANBAN_DB", None)
        os.environ.pop("HERMES_KANBAN_DB", None)
        os.environ["HERMES_HOME"] = self.tmp
        self.db = str(Path(self.tmp) / "kanban.db")
        self.calls = {"create": [], "comment": []}
        bp.create_task = self._fake_create
        bp.comment_task = self._fake_comment

    def _fake_create(self, title, body, assignee):
        self.calls["create"].append((title, body, assignee))
        return f"t_new_{len(self.calls['create'])}"

    def _fake_comment(self, task_id, text):
        self.calls["comment"].append((task_id, text))
        return True

    def _ledger(self):
        p = bp.ledger_path(self.tmp)
        try:
            return [json.loads(l) for l in p.read_text().splitlines()
                    if l.strip()]
        except OSError:
            return []


class TestParseParts(unittest.TestCase):
    def test_parse_maraton(self):
        parts = bp.parse_parts(MARATON_BODY)
        self.assertEqual(sorted(parts), [1, 2, 3, 4, 5, 6])
        self.assertIn("R2:", parts[2])

    def test_fase_paso_step_round(self):
        body = "Fase 1: a\nFase 2: b\nPaso 1: c\nStep 2: d\nRound 3: e"
        self.assertEqual(sorted(bp.parse_parts(body)), [1, 2, 3])

    def test_bare_numbered_lists_are_not_parts(self):
        self.assertEqual(bp.parse_parts("1. a\n2. b\n3. c"), {})

    def test_guard_too_many_parts(self):
        body = "\n".join(f"R{i}: x" for i in range(1, 30))
        self.assertEqual(bp.parse_parts(body), {})

    def test_singular_part_is_not_multi_part(self):
        self.assertEqual(bp.parse_parts("R1: only part"),
                         {1: "R1: only part"})
        self.assertEqual(sorted(bp.parse_parts("R1: a\nR2: b")), [1, 2])


class TestEvidence(unittest.TestCase):
    def test_marathon_incident_case(self):
        """The exact confession case: run summary says 'First round', body
        declares R1-R6 -> output evidence must be {1}."""
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in(FIRST_ROUND_SUMMARY, declared)
        self.assertEqual(out, {1})

    def test_spanish_ordinal(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("Primera ronda ejecutada con exito.", declared)
        self.assertEqual(out, {1})

    def test_range_mention_evidences_whole_range(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R2-R6 ejecutadas", declared)
        self.assertEqual(out, {2, 3, 4, 5, 6})

    def test_range_with_negation_evidences_nothing(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R2-R6 pendientes para el proximo turno",
                              declared)
        self.assertEqual(out, set())

    def test_negated_mention_without_completion_is_not_evidence(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R3 y R4 quedan para manana", declared)
        self.assertEqual(out, set())

    def test_negated_mention_rescued_by_completion_word(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R3 y R4 pendientes al inicio, ya hechas",
                              declared)
        self.assertEqual(out, {3, 4})

    def test_skipped_is_negation(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R5 skipped: nothing interesting", declared)
        self.assertEqual(out, set())

    def test_plain_program_line_in_output_still_evidences(self):
        # Output channels are trusted: a plain "R4: done" line evidences.
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R4: done, changelog generated", declared)
        self.assertEqual(out, {4})

    def test_body_evidence_requires_completion_word(self):
        declared = bp.parse_parts(MARATON_BODY)
        # Program lines never self-evidence...
        self.assertEqual(bp.evidenced_in_body(
            "R2: resumir skills\nR3: extraer campos", declared), set())
        # ...but a restated completion does (successor convention).
        self.assertEqual(bp.evidenced_in_body(
            "R1 (ya hecha por el turno anterior)", declared), {1})

    def test_unrelated_numbers_ignored(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("R8 and R9 done elsewhere", declared)
        self.assertEqual(out, set())

    def test_waiver_comments(self):
        declared = bp.parse_parts(MARATON_BODY)
        waived = bp.waived_parts(
            "[done-verify-skip] R5: log vacio, nada que clasificar",
            declared)
        self.assertEqual(waived, {5})

    def test_pending_parts_union(self):
        declared = bp.parse_parts(MARATON_BODY)
        out = bp.evidenced_in("First round completed", declared)
        body = bp.evidenced_in_body("R2 (completada antes)", declared)
        waived = bp.waived_parts("[done-verify-skip] R4: n/a", declared)
        self.assertEqual(bp.pending_parts(declared, out, body, waived),
                         [3, 5, 6])


class TestPartsLabel(unittest.TestCase):
    def test_runs_and_singles(self):
        self.assertEqual(bp.parts_label([2, 3, 4, 5, 6]), "R2-R6")
        self.assertEqual(bp.parts_label([2, 5]), "R2, R5")
        self.assertEqual(bp.parts_label([2, 3, 5]), "R2-R3, R5")
        self.assertEqual(bp.parts_label([]), "")


class TestScan(Base):
    def test_incident_full_scan(self):
        """Body R1-R6, one run 'First round...' -> pending R2-R6,
        successor flag off, class C on."""
        _mk_db(self.db, [],
               done=[_maraton_done()],
               runs=[("t_maraton", FIRST_ROUND_SUMMARY)])
        findings = bp.scan(self.db, now=NOW)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["declared"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(f["evidenced"], [1])
        self.assertEqual(f["pending"], [2, 3, 4, 5, 6])
        self.assertFalse(f["has_successor"])
        self.assertTrue(f["clase_c"])

    def test_all_parts_evidenced_no_pending(self):
        _mk_db(self.db, [],
               done=[_maraton_done(
                   result="R1 done. R2-R6 ejecutadas y documentadas.")])
        findings = bp.scan(self.db, now=NOW)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["pending"], [])

    def test_open_successor_blocks(self):
        _mk_db(self.db, [
            {"id": "t_succ", "status": "ready", "assignee": "pr-vllm",
             "title": "Partes pendientes de t_maraton (R2-R6)",
             "body": "SUCESOR AUTO-DERIVADO del body de t_maraton",
             "created_at": NOW}],
               done=[_maraton_done()],
               runs=[("t_maraton", "First round completed.")])
        findings = bp.scan(self.db, now=NOW)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["has_successor"])

    def test_waiver_in_comment_removes_part(self):
        _mk_db(self.db, [],
               done=[_maraton_done()],
               runs=[("t_maraton", "First round completed.")],
               comments=[("t_maraton",
                          "[done-verify-skip] R5: log vacio, nada que ver")])
        findings = bp.scan(self.db, now=NOW)
        self.assertEqual(findings[0]["pending"], [2, 3, 4, 6])

    def test_non_multipart_task_ignored(self):
        _mk_db(self.db, [],
               done=[{"id": "t_simple", "title": "simple",
                      "assignee": "pr-ollama",
                      "body": "objective:OBJ-x | cost:tiny | clase:C\n"
                              "una sola cosa", "result": None,
                      "completed_at": NOW - 60}])
        self.assertEqual(bp.scan(self.db, now=NOW), [])

    def test_old_done_outside_window_ignored(self):
        _mk_db(self.db, [],
               done=[_maraton_done(completed=NOW - 72 * 3600)],
               runs=[("t_maraton", "First round completed.")])
        self.assertEqual(bp.scan(self.db, now=NOW), [])

    def test_pending_without_successor_count(self):
        _mk_db(self.db, [],
               done=[_maraton_done()],
               runs=[("t_maraton", "First round completed.")])
        self.assertEqual(bp.pending_without_successor_count(self.db, now=NOW),
                         1)


class TestCascade(Base):
    def _incident_board(self):
        _mk_db(self.db, [],
               done=[_maraton_done()],
               runs=[("t_maraton", FIRST_ROUND_SUMMARY)])

    def _step(self, execute):
        return bp.cascade_step(self.db, bp.ledger_path(self.tmp),
                               execute=execute, now=NOW)

    def _mark_successor_created(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks (id, title, status, assignee, body, "
                    "result, completed_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    ("t_new_1", "Partes pendientes", "ready", "pr-vllm",
                     "SUCESOR AUTO-DERIVADO del body de t_maraton", None,
                     None, NOW))
        con.commit()
        con.close()

    def test_dry_run_reports_without_mutating(self):
        self._incident_board()
        msg = self._step(execute=False)
        self.assertIsNotNone(msg)
        self.assertIn("t_maraton", msg)
        self.assertIn("R2-R6", msg)
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(self.calls["comment"], [])
        ledger = self._ledger()
        self.assertEqual(ledger[0]["action"], "body-parts-successor")
        self.assertTrue(ledger[0]["dry"])

    def test_execute_creates_successor_and_audits_parent(self):
        self._incident_board()
        msg = self._step(execute=True)
        self.assertIsNotNone(msg)
        self.assertEqual(len(self.calls["create"]), 1)
        title, body, assignee = self.calls["create"][0]
        self.assertIn("t_maraton", title)
        self.assertIn("R2-R6", title)
        self.assertIn("clase:C", body)
        self.assertIn("R2: resumir", body)          # verbatim part line
        self.assertIn("done-verify-skip", body)     # escape hatch documented
        self.assertEqual(assignee, "pr-vllm")       # inherited assignee
        self.assertEqual(len(self.calls["comment"]), 1)
        self.assertEqual(self.calls["comment"][0][0], "t_maraton")
        self.assertIn("[done-verify]", self.calls["comment"][0][1])
        ledger = self._ledger()
        self.assertEqual(ledger[0]["action"], "body-parts-successor")
        self.assertIn("R2-R6", ledger[0]["pending"])

    def test_second_tick_with_open_successor_is_silent(self):
        """Idempotency: after the successor exists, the cascade must not
        create a second one (detect-log only, None on stdout)."""
        self._incident_board()
        self._step(execute=True)
        self._mark_successor_created()
        msg = self._step(execute=True)
        self.assertIsNone(msg)
        self.assertEqual(len(self.calls["create"]), 1)  # still one
        ledger = self._ledger()
        self.assertEqual(ledger[-1]["action"], "body-parts-detect")

    def test_non_clase_c_gets_audit_comment_not_successor(self):
        body = MARATON_BODY.replace("clase:C", "clase:B")
        _mk_db(self.db, [],
               done=[{"id": "t_b", "title": "multi B", "assignee": "x",
                      "body": body, "result": None,
                      "completed_at": NOW - 60}],
               runs=[("t_b", "First round completed.")])
        msg = self._step(execute=True)
        self.assertIsNotNone(msg)
        self.assertEqual(self.calls["create"], [])
        self.assertEqual(len(self.calls["comment"]), 1)
        self.assertIn("[done-verify]", self.calls["comment"][0][1])

    def test_audit_comment_only_once(self):
        body = MARATON_BODY.replace("clase:C", "clase:B")
        _mk_db(self.db, [],
               done=[{"id": "t_b", "title": "multi B", "assignee": "x",
                      "body": body, "result": None,
                      "completed_at": NOW - 60}],
               runs=[("t_b", "First round completed.")],
               comments=[("t_b", "[done-verify] ya auditado antes")])
        msg = self._step(execute=True)
        self.assertIsNone(msg)  # audited already; nothing to say
        self.assertEqual(self.calls["comment"], [])

    def test_no_pending_no_action(self):
        _mk_db(self.db, [],
               done=[_maraton_done(result="R1 done. R2-R6 ejecutadas.")])
        msg = self._step(execute=True)
        self.assertIsNone(msg)
        self.assertEqual(self.calls["create"], [])

    def test_empty_board_silent(self):
        msg = self._step(execute=True)
        self.assertIsNone(msg)


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in ("/home/", "/data", Path.home().name):
            self.assertNotIn(needle, src,
                             f"host path leaked into tick_body_parts.py: "
                             f"{needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
