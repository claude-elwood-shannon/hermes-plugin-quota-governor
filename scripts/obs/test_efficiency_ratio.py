#!/usr/bin/python3.12
"""test_efficiency_ratio.py — offline suite for efficiency-ratio.py (OBJ-METRICS).

Pins the CURRENT behaviour of the P4 ratio (fixtures only, no network, no
live state) so evidence-verification fixes stop being blind edits:

  1. verdict ladder: None=SIN GASTO, >=5 EXCELENTE, >=2 OK, >=0.5 BAJO, else CRITICO
  2. declared_criterion: success tag anywhere in body; prose (EN/ES) in first 12 lines
  3. criterion_evidenced: completion word AND anchor-first coverage
     (anchor-bearing criteria need >= ceil(anchors/3) anchor hits —
     paths/test names/commands, basename match allowed; anchor-free
     criteria need >= ceil(words/3) word hits) — honesty rule intact
  4. budget_objectives: approved_objectives table extends legacy AUTODEV/AUTOREPAIR
  5. spend side: read_trace (corrupt lines skipped, missing file raises),
     objective_of_row (direct tag / t_xxx join / unattributed), window_spend
  6. task side verified_done_tasks: honesty rule (no declaration -> never
     counts), evidence channels (result + run summaries + comments),
     include_runs=False, multi-part via tick_body_parts, verifier=limited
  7. compute(): strict base, proxy-total-24h fallback, SIN GASTO (spend==0),
     CRITICO (spend>0 / 0 verified), corrupt-line tolerance, missing trace
     -> N/A fail-open, 24h vs 7d windows
  8. main(): appends ONE line to metrics-history.jsonl, tolerates corrupt
     pre-existing content, never aborts (exit 0)

Run:  /usr/bin/python3.12 test_efficiency_ratio.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent.parent.parent)
SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "efficiency-ratio.py")

_spec = importlib.util.spec_from_file_location("efficiency_ratio", SCRIPT)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

T1 = "t_deadbeef"           # matches TASK_ID_RE (t_[0-9a-f]{8})
T2 = "t_cafe1234"
T3 = "t_0badc0de"

BODY_VERIFIED = (
    "objective:AUTODEV\n"
    "cost:micro\n"
    "\n"
    "## Trabajo\n"
    "Construir la suite offline del ratio.\n"
    "\n"
    "success: ratio suite tests pass with rc=0 and OK line\n"
)
RESULT_VERIFIED = "Suite done: the ratio tests pass and unittest printed OK."

# Same program, no `success:` tag and no prose criterion -> honesty rule:
# even with a completion-word-carrying result, it must NEVER count.
BODY_NO_CRITERION = (
    "objective:AUTODEV\n"
    "cost:micro\n"
    "\n"
    "## Trabajo\n"
    "Refactor del informe sin criterio declarado.\n"
)
RESULT_LIE = "Todo hecho, done, finished, perfecto."

BODY_MULTIPART = (
    "objective:AUTODEV\n"
    "\n"
    "## Trabajo\n"
    "R1: construir fixtures.\n"
    "R2: fijar el veredicto.\n"
)
RESULT_BOTH_PARTS = "R1 hechas las fixtures. R2 done."
RESULT_ONE_PART = "R1 hechas las fixtures."

BODY_NOT_BUDGET = (
    "objective:OBJ-OTHER\n"
    "\n"
    "success: anything at all\n"
)


def task_row(tid, body, result, completed_at, status="done"):
    return {"id": tid, "body": body, "result": result, "status": status,
            "completed_at": completed_at}


def make_db(path, tasks=(), runs=(), comments=(), objectives=()):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT, "
                "result TEXT, status TEXT, completed_at INTEGER)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_id TEXT, summary TEXT)")
    con.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, task_id TEXT, body TEXT)")
    con.executemany(
        "INSERT INTO tasks (id, body, result, status, completed_at) "
        "VALUES (:id, :body, :result, :status, :completed_at)", tasks)
    con.executemany("INSERT INTO task_runs (task_id, summary) VALUES (?, ?)",
                    [(r["task_id"], r["summary"]) for r in runs])
    con.executemany("INSERT INTO task_comments (task_id, body) VALUES (?, ?)",
                    [(c["task_id"], c["body"]) for c in comments])
    if objectives:
        con.execute("CREATE TABLE approved_objectives (id TEXT PRIMARY KEY, "
                    "status TEXT DEFAULT 'active')")
        con.executemany("INSERT INTO approved_objectives (id) VALUES (?)",
                        [(o,) for o in objectives])
    con.commit()
    con.close()


def write_trace(path, lines, corrupt=None):
    with open(path, "w", encoding="utf-8") as fh:
        if corrupt:
            for c in corrupt:
                fh.write(c + "\n")
        for r in lines:
            fh.write(json.dumps(r) + "\n")


def cost_row(ts, usd, objective=None, consumer_id="cron"):
    r = {"ts_epoch_utc": ts, "consumer_class": "worker",
         "consumer_id": consumer_id, "cause": "model-call", "costUsd": usd}
    if objective is not None:
        r["objective"] = objective
    return r


class ErBase(unittest.TestCase):
    """Env-isolated fixture: ER_KANBAN_DB / ER_TRACE / ER_METRICS point at
    a throwaway tempdir; module caches are wiped per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eff-ratio-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = Path(self.tmp) / "kanban.db"
        self.trace = Path(self.tmp) / "trace.jsonl"
        self.metrics = Path(self.tmp) / "metrics-history.jsonl"
        envs = {"ER_KANBAN_DB": self.db, "ER_TRACE": self.trace,
                "ER_METRICS": self.metrics}
        for k, v in envs.items():
            os.environ[k] = str(v)
            self.addCleanup(os.environ.pop, k, None)
        # module-level caches would otherwise leak objectives across fixtures
        for cache in (er._objectives_cache, er._OBJECTIVES_RES_CACHE):
            saved = dict(cache)
            cache.clear()
            self.addCleanup(cache.clear)
            self.addCleanup(cache.update, saved)
        self.now = time.time()
        self.w24 = er.WINDOWS["24h"]
        self.w7d = er.WINDOWS["7d"]

    def fresh_db(self, *a, **kw):
        make_db(str(self.db), *a, **kw)

    def fresh_trace(self, lines, corrupt=None):
        write_trace(self.trace, lines, corrupt)

    def metrics_entry(self):
        return er.compute(now=self.now)


# ---------------------------------------------------------------------------
# Pure-logic units
# ---------------------------------------------------------------------------

class TestVerdictLadder(unittest.TestCase):
    def test_ladder_exact_boundaries(self):
        self.assertEqual(er.verdict_for(None), "SIN GASTO")
        self.assertEqual(er.verdict_for(5.0), "EXCELENTE")
        self.assertEqual(er.verdict_for(4.99), "OK")
        self.assertEqual(er.verdict_for(2.0), "OK")
        self.assertEqual(er.verdict_for(1.99), "BAJO")
        self.assertEqual(er.verdict_for(0.5), "BAJO")
        self.assertEqual(er.verdict_for(0.49), "CRITICO")
        self.assertEqual(er.verdict_for(0.0), "CRITICO")


class TestDeclaredCriterion(ErBase):
    def test_tag_anywhere_in_body(self):
        kind, crit = er.declared_criterion(BODY_VERIFIED)
        self.assertEqual(kind, "tag")
        self.assertIn("ratio suite tests", crit)

    def test_prose_es_within_header(self):
        body = ("objective:AUTODEV\n"
                "Criterio de éxito: imprimir la palabra OK\n")
        kind, crit = er.declared_criterion(body)
        self.assertEqual(kind, "prose")
        self.assertIn("imprimir", crit)

    def test_prose_beyond_12_lines_not_found(self):
        body = "\n".join(["## Trabajo"] * 12 +
                         ["success criterion: printed OK report"])
        kind, crit = er.declared_criterion(body)
        self.assertIsNone(kind)
        self.assertIsNone(crit)

    def test_no_declaration(self):
        self.assertEqual(er.declared_criterion(BODY_NO_CRITERION),
                         (None, None))
        self.assertEqual(er.declared_criterion(""), (None, None))


class TestCriterionEvidence(ErBase):
    # A realistic ~24-unit criterion in the house style: concrete artifacts
    # (test path + repo path) plus a tail of process instructions.
    LONG_CRITERION = (
        "cd /data/git/hermes-plugin-quota-governor && python3 -m pytest "
        "tests/test_repo_sync_drift.py -q termina con rc=0 y el commit de "
        "la suite aparece en git log; reportar el resumen de pytest con su "
        "conteo y el hash del commit en el cierre.")

    def test_evidenced_with_completion_and_third_coverage(self):
        # anchor-free: 6 words, 2 hits >= ceil(6/3)
        self.assertTrue(er.criterion_evidenced(
            "efficiency ratio tests pass rc=0",
            "The efficiency ratio tests are done."))

    def test_tokens_without_completion_word_not_enough(self):
        self.assertFalse(er.criterion_evidenced(
            "efficiency ratio tests pass rc=0",
            "the efficiency ratio tests"))

    def test_below_third_token_coverage(self):
        # 4 words -> need >= 2 hits; "alpha done" gives 1
        self.assertFalse(er.criterion_evidenced(
            "alpha bravo charlie delta", "alpha done"))

    def test_tokenless_criterion_never_counts(self):
        self.assertFalse(er.criterion_evidenced("sí ok", "done done done"))

    # --- t_7aaa897c regression: long anchor-bearing criteria ---------------

    def test_long_criterion_with_anchors_and_honest_summary_verified(self):
        # Real shape (t_748fcdfd): the summary names the tested path, the
        # pass count, the commit hash and the touched file. Old matcher
        # required 14+ raw-token hits -> always CRITICO artifact.
        summary = ("Suite tests/test_repo_sync_drift.py en verde (12 passed)"
                   " y entregable comiteado de verdad en 22f4de4. Arreglé el"
                   " test del escenario drift y la suite completa acabó done.")
        self.assertTrue(er.criterion_evidenced(self.LONG_CRITERION, summary))

    def test_anchor_match_via_basename(self):
        # Full path quoted in criterion, basename only in the summary; the
        # criterion asks to report the test count, so the honest summary
        # carries it (word channel) plus the named artifact (anchor).
        crit = ("cd /data/git/hermes-plugin-quota-governor && python3 -m "
                "pytest tests/test_providers.py -q pasa rc=0 y reportar el "
                "resumen con el conteo de tests.")
        summary = ("test_providers.py done: 12 tests passed en la suite, "
                   "commit creado en el repo.")
        self.assertTrue(er.criterion_evidenced(crit, summary))

    def test_anchors_present_but_zero_anchor_hits_not_verified(self):
        # Summary talks about the work in prose only: no path, no filename.
        summary = ("Refactor del informe terminado y pruebas hechas con el "
                   "comando, commit creado y todo done.")
        self.assertFalse(er.criterion_evidenced(self.LONG_CRITERION, summary))

    def test_word_hits_only_still_need_threshold(self):
        # Anchor-free criterion, exactly 1/3 coverage -> pass boundary.
        crit = "alpha bravo charlie delta echo foxtrot"   # 6 words
        self.assertTrue(er.criterion_evidenced(crit, "alpha bravo done"))
        self.assertFalse(er.criterion_evidenced(crit, "alpha done"))

    def test_zero_token_overlap_never_verified_even_with_completion(self):
        self.assertFalse(er.criterion_evidenced(
            self.LONG_CRITERION,
            "Everything finished, all work done and completed successfully."))

    def test_diacritic_folding_matches_accented_forms(self):
        # 'número' folds to 'numero' on both sides (t_7aaa897c ghost tokens)
        self.assertTrue(er.criterion_evidenced(
            "reportar el número exacto de tests pasados en el resumen",
            "número de tests: 8, suite done."))
        self.assertFalse(er.criterion_evidenced(
            "reportar el número exacto de tests fallidos en el resumen",
            "tests done: 0 failed, 8 passed."))



class TestBudgetObjectives(ErBase):
    def test_missing_table_degrades_to_legacy_pair(self):
        self.fresh_db()
        self.assertEqual(er.budget_objectives(self.db),
                         er.LEGACY_BUDGET_OBJECTIVES)
        self.assertFalse(er.is_budget_task("objective:OBJ-METRICS x", self.db))

    def test_table_extends_legacy_pair(self):
        self.fresh_db(objectives=["OBJ-METRICS"])
        ids = er.budget_objectives(self.db)
        self.assertIn("OBJ-METRICS", ids)
        self.assertIn("AUTODEV", ids)
        self.assertTrue(er.is_budget_task("objective:OBJ-METRICS\n", self.db))

    def test_case_insensitive_footer_tag_counts(self):
        self.fresh_db()
        body = ("## Contexto\nalgo\n\n"
                "footer: objective:autorepair (added late)")
        self.assertTrue(er.is_budget_task(body, self.db))


# ---------------------------------------------------------------------------
# Spend side
# ---------------------------------------------------------------------------

class TestSpendSide(ErBase):
    def test_read_trace_skips_corrupt_lines(self):
        self.fresh_trace([cost_row(self.now - 60, 0.5, objective="AUTODEV")],
                         corrupt=["not json {{{", ""])
        rows = er.read_trace(self.trace)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["costUsd"], 0.5)

    def test_read_trace_missing_file_raises(self):
        with self.assertRaises(OSError):
            er.read_trace(self.trace)

    def test_objective_of_row_direct_join_unattributed(self):
        self.fresh_db(tasks=[task_row(T1, BODY_VERIFIED, RESULT_VERIFIED,
                                      int(self.now))])
        cache = {}
        direct = cost_row(self.now, 1.0, objective="AUTODEV")
        self.assertEqual(er.objective_of_row(direct, self.db, cache), "AUTODEV")
        joined = cost_row(self.now, 1.0, consumer_id=T1)
        self.assertEqual(er.objective_of_row(joined, self.db, cache), "AUTODEV")
        unattr = cost_row(self.now, 1.0, consumer_id="cron")
        self.assertEqual(er.objective_of_row(unattr, self.db, cache),
                         "unattributed")

    def test_window_spend_filters_ts_and_type(self):
        self.fresh_db()
        rows = [
            cost_row(self.now - 60, 0.5, objective="AUTODEV"),
            cost_row(self.now - 60, 0.25, objective="unattributed"),
            cost_row(self.now - 86400 * 30, 100.0, objective="AUTODEV"),
            cost_row(self.now - 60, "0.75", objective="AUTODEV"),
        ]
        strict, total = er.window_spend(rows, self.w24, self.now, self.db)
        self.assertAlmostEqual(strict, 0.5, places=6)
        self.assertAlmostEqual(total, 0.75, places=6)


# ---------------------------------------------------------------------------
# Task side
# ---------------------------------------------------------------------------

class TestTaskSide(ErBase):
    def test_honesty_done_without_declaration_never_counts(self):
        self.fresh_db(tasks=[
            task_row(T1, BODY_NO_CRITERION, RESULT_LIE, int(self.now - 60)),
            task_row(T2, BODY_NOT_BUDGET, "cualquier cosa done",
                     int(self.now - 60)),
            task_row(T3, BODY_VERIFIED, RESULT_VERIFIED, int(self.now - 60)),
        ])
        verified, budget, verifier = er.verified_done_tasks(
            self.db, self.w24, self.now)
        self.assertEqual(verified, [T3])          # T1: no declaration, T2: not budget
        self.assertEqual(sorted(budget), sorted([T1, T3]))
        self.assertIn(verifier, ("full", "limited"))

    def test_evidence_channels_result_runs_comments(self):
        self.fresh_db(tasks=[
            task_row(T1, BODY_VERIFIED, None, int(self.now - 60)),
            task_row(T2, BODY_VERIFIED, "", int(self.now - 60)),
        ], runs=[{"task_id": T1, "summary": RESULT_VERIFIED}],
            comments=[{"task_id": T2, "body": RESULT_VERIFIED}])
        verified, _, _ = er.verified_done_tasks(self.db, self.w24, self.now)
        self.assertEqual(sorted(verified), sorted([T1, T2]))
        strict_only, _, _ = er.verified_done_tasks(
            self.db, self.w24, self.now, include_runs=False)
        self.assertEqual(strict_only, [])        # ER_RUNS_TABLES=0 path

    def test_multipart_all_evidenced_verified(self):
        self.fresh_db(tasks=[task_row(T1, BODY_MULTIPART, RESULT_BOTH_PARTS,
                                      int(self.now - 60))])
        verified, budget, verifier = er.verified_done_tasks(
            self.db, self.w24, self.now)
        self.assertEqual(verifier, "full")       # repo ships tick_body_parts.py
        self.assertEqual(verified, [T1])
        self.assertEqual(budget, [T1])

    def test_multipart_pending_part_not_verified(self):
        self.fresh_db(tasks=[task_row(T1, BODY_MULTIPART, RESULT_ONE_PART,
                                      int(self.now - 60))])
        verified, budget, _ = er.verified_done_tasks(self.db, self.w24,
                                                     self.now)
        self.assertEqual(verified, [])           # R2 declared, never evidenced
        self.assertEqual(budget, [T1])

    def test_verifier_limited_without_tick_body_parts(self):
        saved = er._tbp
        er._tbp = False                          # simulate unavailable module
        self.addCleanup(setattr, er, "_tbp", saved)
        self.fresh_db(tasks=[task_row(T1, BODY_MULTIPART, RESULT_BOTH_PARTS,
                                      int(self.now - 60))])
        verified, budget, verifier = er.verified_done_tasks(
            self.db, self.w24, self.now)
        self.assertEqual(verifier, "limited")
        self.assertEqual(verified, [])           # conservative: parts unverifiable
        self.assertEqual(budget, [T1])


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------

class TestCompute(ErBase):
    def test_normal_strict_base_ratio_and_verdict(self):
        self.fresh_db(tasks=[
            task_row(T1, BODY_VERIFIED, RESULT_VERIFIED, int(self.now - 60)),
            task_row(T2, BODY_VERIFIED, RESULT_VERIFIED, int(self.now - 60)),
            task_row(T3, BODY_NO_CRITERION, RESULT_LIE, int(self.now - 60)),
        ])
        self.fresh_trace([
            cost_row(self.now - 3600, 0.4, objective="AUTODEV"),
            cost_row(self.now - 7200, 0.4, consumer_id=T1),
            cost_row(self.now - 60, 0.2, objective="unattributed"),
        ])
        out = self.metrics_entry()
        self.assertEqual(out["kind"], "efficiency_ratio")
        self.assertEqual(out["window"], "24h")
        self.assertEqual(out["tareas_verificadas"], 2)
        self.assertEqual(out["tareas_budget_done_24h"], 3)
        self.assertAlmostEqual(out["gasto_strict_usd"], 0.8, places=6)
        self.assertAlmostEqual(out["gasto_total_usd"], 1.0, places=6)
        self.assertEqual(out["base_mode"], "strict")
        self.assertAlmostEqual(out["ratio"], 2.5, places=6)
        self.assertEqual(out["veredicto"], "OK")
        self.assertAlmostEqual(out["ratio_7d"], 2.5, places=6)
        self.assertEqual(out["veredicto_7d"], "OK")

    def test_spending_none_both_bases_sin_gasto(self):
        self.fresh_db(tasks=[task_row(T1, BODY_VERIFIED, RESULT_VERIFIED,
                                      int(self.now - 60))])
        self.fresh_trace([])                     # file exists, zero cost lines
        out = self.metrics_entry()
        self.assertIsNone(out["ratio"])
        self.assertEqual(out["veredicto"], "SIN GASTO")
        self.assertEqual(out["base_mode"], "none")
        self.assertIsNone(out["ratio_7d"])
        self.assertEqual(out["veredicto_7d"], "SIN GASTO")

    def test_spend_zero_verified_critico(self):
        # honesty rule + ladder: budget done with NO declared success counts
        # 0 verified -> ratio 0.0 -> CRITICO
        self.fresh_db(tasks=[task_row(T1, BODY_NO_CRITERION, RESULT_LIE,
                                      int(self.now - 60))])
        self.fresh_trace([cost_row(self.now - 60, 1.0, objective="AUTODEV")])
        out = self.metrics_entry()
        self.assertEqual(out["tareas_budget_done_24h"], 1)
        self.assertEqual(out["tareas_verificadas"], 0)
        self.assertAlmostEqual(out["ratio"], 0.0, places=6)
        self.assertEqual(out["veredicto"], "CRITICO")

    def test_proxy_total_fallback_when_strict_zero(self):
        self.fresh_db(tasks=[task_row(T1, BODY_VERIFIED, RESULT_VERIFIED,
                                      int(self.now - 60))])
        self.fresh_trace([
            cost_row(self.now - 60, 0.25, objective="unattributed",
                     consumer_id="usage-audit"),
            cost_row(self.now - 120, 0.25, objective="unattributed",
                     consumer_id="nanogpt-requests"),
        ])
        out = self.metrics_entry()
        self.assertAlmostEqual(out["gasto_strict_usd"], 0.0, places=6)
        self.assertEqual(out["base_mode"], "proxy-total-24h")
        self.assertAlmostEqual(out["gasto_usd"], 0.5, places=6)
        self.assertAlmostEqual(out["ratio"], 2.0, places=6)
        self.assertEqual(out["veredicto"], "OK")
        # quirk pinned: the 7d window reuses the same mode label
        self.assertEqual(out["base_mode_7d"], "proxy-total-24h")

    def test_corrupt_trace_line_does_not_abort(self):
        self.fresh_db(tasks=[
            task_row(T1, BODY_VERIFIED, RESULT_VERIFIED, int(self.now - 60)),
            task_row(T2, BODY_VERIFIED, RESULT_VERIFIED, int(self.now - 60)),
        ])
        self.fresh_trace(
            [cost_row(self.now - 3600, 0.4, objective="AUTODEV"),
             cost_row(self.now - 7200, 0.4, objective="AUTODEV")],
            corrupt=["corrupt{{{line", "{broken json"])
        out = self.metrics_entry()
        self.assertAlmostEqual(out["ratio"], 2.5, places=6)
        self.assertEqual(out["veredicto"], "OK")

    def test_missing_trace_fails_open_na(self):
        self.fresh_db(tasks=[])                  # no trace file written
        out = self.metrics_entry()
        self.assertIsNone(out["ratio"])
        self.assertEqual(out["veredicto"], "N/A")
        self.assertIn("cannot compute", out["error"])

    def test_7d_window_includes_older_events(self):
        old = self.now - 4 * 86400
        self.fresh_db(tasks=[task_row(T1, BODY_VERIFIED, RESULT_VERIFIED,
                                      int(old))])
        self.fresh_trace([cost_row(old, 0.5, objective="AUTODEV")])
        out = self.metrics_entry()
        self.assertEqual(out["tareas_verificadas"], 0)
        self.assertIsNone(out["ratio"])
        self.assertEqual(out["veredicto"], "SIN GASTO")
        self.assertEqual(out["tareas_verificadas_7d"], 1)
        self.assertAlmostEqual(out["ratio_7d"], 2.0, places=6)
        self.assertEqual(out["veredicto_7d"], "OK")


# ---------------------------------------------------------------------------
# main() end-to-end
# ---------------------------------------------------------------------------

class TestMain(ErBase):
    def _capture_stdout(self, fn):
        import io
        import sys
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            rc = fn()
        finally:
            sys.stdout = old
        return rc, buf.getvalue()

    def test_main_appends_single_written_line_with_base_mode(self):
        self.fresh_db(tasks=[task_row(T1, BODY_VERIFIED, RESULT_VERIFIED,
                                      int(time.time() - 60))])
        self.fresh_trace([cost_row(time.time() - 60, 0.5,
                                   objective="unattributed")])
        rc = er.main([])
        self.assertEqual(rc, 0)
        lines = self.metrics.read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["kind"], "efficiency_ratio")
        self.assertEqual(entry["base_mode"], "proxy-total-24h")
        self.assertIn("proxy-total-24h", lines[0])  # literal, on the written line
        self.assertEqual(entry["veredicto"], "OK")

    def test_main_tolerates_corrupt_inputs_and_existing_metrics(self):
        self.fresh_db(tasks=[
            task_row(T1, BODY_VERIFIED, RESULT_VERIFIED, int(time.time() - 60)),
            task_row(T2, BODY_VERIFIED, RESULT_VERIFIED, int(time.time() - 60)),
        ])
        t = time.time()
        self.fresh_trace([cost_row(t - 60, 0.4, objective="AUTODEV"),
                          cost_row(t - 120, 0.4, objective="AUTODEV")],
                         corrupt=["}{ not json"])
        self.metrics.write_text("garbage-first-line\n", encoding="utf-8")
        rc = er.main([])
        self.assertEqual(rc, 0)
        lines = self.metrics.read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)          # garbage kept, one line appended
        entry = json.loads(lines[1])
        self.assertAlmostEqual(entry["ratio"], 2.5, places=6)

    def test_main_dry_run_writes_nothing_and_prints(self):
        self.fresh_db(tasks=[])
        self.fresh_trace([])
        rc, out = self._capture_stdout(lambda: er.main(["--dry-run"]))
        self.assertEqual(rc, 0)
        self.assertIn("efficiency_ratio", out)
        self.assertFalse(self.metrics.exists())

    def test_main_fail_open_when_trace_missing(self):
        self.fresh_db(tasks=[])                  # trace file never created
        rc, out = self._capture_stdout(lambda: er.main([]))
        self.assertEqual(rc, 0)
        self.assertIn("cannot compute", out)
        lines = self.metrics.read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(json.loads(lines[0])["veredicto"], "N/A")


if __name__ == "__main__":
    unittest.main(verbosity=2)
