#!/usr/bin/env python3
"""test_cost_tag_fix.py — tests for OBJ-02 deterministic cost: tag enforcement.

Tests the cost-tag-fix.py backstop that injects/normalises the `cost:<category>`
header tag on auto_created tasks so the USD calibration (model-cost-ledger ×
kanban runs) can group rows by category.

Coverage (per task t_9a2c6eab acceptance criteria):
  - detection of missing tag            (inject + heuristic default)
  - normalisation of non-standard forms (Cost: small / coste:micro / cost: Small)
  - idempotence                         (canonical tag → no-op; run twice)
  - dry-run vs --execute                (default must not mutate the DB)
  - only non-archived tasks             (archived + running protected)
  - prose mentions ignored              (header-anchored, not LIKE '%cost%')
  - JSONL fix log                       (append-only, one line per applied fix)

All tests use temp DBs and temp log files — no side effects on the real board.

Run:
  /usr/bin/python3.12 test_cost_tag_fix.py
  /usr/bin/python3.12 -m pytest test_cost_tag_fix.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Import hyphenated module the same way the sibling tests do.
_spec = importlib.util.spec_from_file_location(
    "cost_tag_fix",
    os.path.join(SCRIPT_DIR, "scripts", "cost-tag-fix.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cost_tag_fix"] = _mod
_spec.loader.exec_module(_mod)

from cost_tag_fix import (
    analyze_body,
    apply_fix_to_body,
    find_fixable_tasks,
    has_auto_created_header,
    infer_default_cost,
    main,
    normalize_cost_value,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_db(tasks: list) -> str:
    """Create a temp kanban.db.  Each task: {id, title, body, status}."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT)"
    )
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, ?)",
            (
                t.get("id", "t_test"),
                t.get("title", "test task"),
                t.get("body", ""),
                t.get("status", "todo"),
            ),
        )
    conn.commit()
    conn.close()
    return path


def _body(path: str, task_id: str) -> str:
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return row[0]


def _cost_line(body: str) -> str:
    """Return the first header line that looks like a cost tag, or ''."""
    for ln in body.splitlines():
        if not ln.strip():
            break
        if ln.strip().lower().startswith(("cost:", "coste:", "cost :")) or \
           ln.strip().lower().startswith("cost:"):
            return ln.strip()
    return ""


GOOD_HEADER = "objective:OBJ-02\nauto_created:true\ncost:small\nprivacy:public\n\nDo the work.\n"


# ── Tests: header detection (prose must not count) ──────────────────────────

class TestHasAutoCreatedHeader(unittest.TestCase):

    def test_true_for_header_tag(self):
        self.assertTrue(has_auto_created_header(GOOD_HEADER))

    def test_false_for_prose_only(self):
        """'auto_created' mentioned mid-description, no tag header → False."""
        body = "This task talks about auto_created:true in prose.\nMore text."
        self.assertFalse(has_auto_created_header(body))

    def test_false_for_empty(self):
        self.assertFalse(has_auto_created_header(""))
        self.assertFalse(has_auto_created_header(None))

    def test_case_and_spacing_tolerant(self):
        self.assertTrue(has_auto_created_header("Auto_Created: true\n\nbody"))


# ── Tests: analyze_body decision matrix ─────────────────────────────────────

class TestAnalyzeBody(unittest.TestCase):

    def test_canonical_tag_is_noop(self):
        self.assertEqual(analyze_body(GOOD_HEADER)[0], "ok")

    def test_missing_tag_triggers_inject(self):
        body = "objective:OBJ-X\nauto_created:true\nprivacy:public\n\ntext"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "inject")
        self.assertEqual(idx, 1)          # inserted after auto_created line
        self.assertIn(value, {"tiny", "small"})

    def test_non_auto_task_skipped(self):
        body = "manual task, no tags\n\ncost:small might even appear in prose"
        self.assertEqual(analyze_body(body)[0], "skip")

    def test_prose_cost_mention_not_a_tag(self):
        """Header has auto_created but 'cost: small' only in the prose body."""
        body = "objective:OBJ-02\nauto_created:true\n\nThe regex cost: small is discussed.\n"
        action, _, _ = analyze_body(body)
        self.assertEqual(action, "inject")

    def test_normalization_capitalized(self):
        body = "auto_created:true\nCost: small\n\nx"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "normalize")
        self.assertEqual(value, "small")

    def test_normalization_spanish(self):
        body = "auto_created:true\ncoste:micro\n\nx"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "normalize")
        self.assertEqual(value, "micro")

    def test_normalization_space_after_colon(self):
        body = "auto_created:true\ncost: Small\n\nx"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "normalize")
        self.assertEqual(value, "small")

    def test_normalization_alias_large(self):
        body = "auto_created:true\ncost:large\n\nx"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "normalize")
        self.assertEqual(value, "complex")

    def test_unmappable_value_normalizes_to_default(self):
        body = "auto_created:true\ncost:banana\n\nx"
        action, idx, value = analyze_body(body)
        self.assertEqual(action, "normalize")
        self.assertIsNone(value)  # caller resolves via infer_default_cost


# ── Tests: normalization + defaults ─────────────────────────────────────────

class TestNormalizeValue(unittest.TestCase):

    def test_canonical_pass_through(self):
        for v in ("micro", "tiny", "small", "medium", "complex"):
            self.assertEqual(normalize_cost_value(v), v)

    def test_aliases(self):
        self.assertEqual(normalize_cost_value("large"), "complex")
        self.assertEqual(normalize_cost_value("XL"), "complex")
        self.assertEqual(normalize_cost_value("lite"), "micro")

    def test_unknown(self):
        self.assertIsNone(normalize_cost_value("banana"))
        self.assertIsNone(normalize_cost_value(""))


class TestInferDefault(unittest.TestCase):

    def test_heavy_keywords_give_small(self):
        for text in ("implement a parser", "refactor the código module",
                     "medium-sized feature", "complex migration"):
            self.assertEqual(infer_default_cost(text), "small", text)

    def test_light_default_tiny(self):
        self.assertEqual(infer_default_cost("rename a variable"), "tiny")


# ── Tests: apply_fix_to_body purity ─────────────────────────────────────────

class TestApplyFix(unittest.TestCase):

    def test_inject_after_auto_line(self):
        body = "objective:OBJ-X\nauto_created:true\nprivacy:public\n\ntext"
        new = apply_fix_to_body(body, "inject", 1, "tiny")
        self.assertEqual(new.splitlines()[:4],
                         ["objective:OBJ-X", "auto_created:true", "cost:tiny",
                          "privacy:public"])

    def test_normalize_replaces_line(self):
        body = "auto_created:true\nCost: Small\n\ntext"
        new = apply_fix_to_body(body, "normalize", 1, "small")
        self.assertEqual(new.splitlines()[1], "cost:small")

    def test_trailing_newline_preserved(self):
        body = "auto_created:true\n\ntext\n"
        new = apply_fix_to_body(body, "inject", 0, "tiny")
        self.assertTrue(new.endswith("\n"))

    def test_result_is_idempotent(self):
        body = "objective:OBJ-X\nauto_created:true\n\nx"
        once = apply_fix_to_body(body, "inject", 1, "tiny")
        self.assertEqual(analyze_body(once)[0], "ok")


# ── Tests: find_fixable_tasks + status protection ───────────────────────────

class TestFindFixableTasks(unittest.TestCase):

    def _conn(self, tasks):
        db = _make_db(tasks)
        conn = sqlite3.connect(db)
        return conn, db

    def test_detects_missing_and_skips_ok(self):
        conn, db = self._conn([
            {"id": "a", "body": "auto_created:true\n\nx", "status": "todo"},
            {"id": "b", "body": GOOD_HEADER, "status": "ready"},
        ])
        plans = find_fixable_tasks(conn)
        conn.close()
        self.assertEqual([p["id"] for p in plans], ["a"])

    def test_archived_and_running_protected(self):
        conn, db = self._conn([
            {"id": "arch", "body": "auto_created:true\n\nx", "status": "archived"},
            {"id": "run", "body": "auto_created:true\n\nx", "status": "running"},
            {"id": "live", "body": "auto_created:true\n\nx", "status": "triage"},
        ])
        plans = find_fixable_tasks(conn)
        conn.close()
        self.assertEqual([p["id"] for p in plans], ["live"])

    def test_prose_only_task_untouched(self):
        body = "Task about auto_created:true mentioning cost: small in prose."
        conn, db = self._conn([{"id": "prose", "body": body, "status": "todo"}])
        plans = find_fixable_tasks(conn)
        conn.close()
        self.assertEqual(plans, [])


# ── Tests: main() dry-run vs execute ────────────────────────────────────────

class TestMainFlow(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="qg-cost-tag-test-")
        self.log = os.path.join(self.tmpdir, "fixes.jsonl")
        self.db = _make_db([
            {"id": "t_missing", "title": "Implement something heavy",
             "body": "objective:OBJ-X\nauto_created:true\nprivacy:public\n\nImplement the thing.",
             "status": "todo"},
            {"id": "t_nonstd", "title": "tiny chore",
             "body": "auto_created:true\nCoste: micro\n\ntext",
             "status": "ready"},
            {"id": "t_ok", "title": "done right",
             "body": GOOD_HEADER, "status": "triage"},
        ])

    def test_dry_run_default_does_not_mutate(self):
        rc = main(["--db", self.db, "--log", self.log])
        self.assertEqual(rc, 0)
        self.assertEqual(_body(self.db, "t_missing"),
                         "objective:OBJ-X\nauto_created:true\nprivacy:public\n\nImplement the thing.")
        self.assertFalse(os.path.exists(self.log))

    def test_explicit_dry_run_flag_same(self):
        rc = main(["--dry-run", "--db", self.db, "--log", self.log])
        self.assertEqual(rc, 0)
        self.assertIn("Coste: micro", _body(self.db, "t_nonstd"))

    def test_execute_applies_inject_and_normalize(self):
        rc = main(["--execute", "--db", self.db, "--log", self.log])
        self.assertEqual(rc, 0)
        # heavy keyword 'implement' → cost:small injected after auto line
        body = _body(self.db, "t_missing")
        self.assertIn("\ncost:small\n", body)
        self.assertEqual(body.splitlines()[:3],
                         ["objective:OBJ-X", "auto_created:true", "cost:small"])
        # 'Coste: micro' normalised in place to 'cost:micro'
        self.assertEqual(_body(self.db, "t_nonstd").splitlines()[1], "cost:micro")
        # already-canonical task untouched
        self.assertEqual(_body(self.db, "t_ok"), GOOD_HEADER)

    def test_execute_is_idempotent(self):
        main(["--execute", "--db", self.db, "--log", self.log])
        snapshot = {t: _body(self.db, t) for t in ("t_missing", "t_nonstd", "t_ok")}
        rc = main(["--execute", "--db", self.db, "--log", self.log])
        self.assertEqual(rc, 0)
        for t, b in snapshot.items():
            self.assertEqual(_body(self.db, t), b, f"{t} changed on 2nd run")

    def test_jsonl_log_append_only(self):
        main(["--execute", "--db", self.db, "--log", self.log])
        with open(self.log) as f:
            records = [json.loads(l) for l in f]
        self.assertEqual(len(records), 2)  # inject + normalize, ok task silent
        self.assertEqual({r["task_id"] for r in records}, {"t_missing", "t_nonstd"})
        for r in records:
            self.assertIn("ts", r)
            self.assertIn(r["action"], ("inject", "normalize"))
            self.assertIn(r["cost"], ("micro", "tiny", "small", "medium", "complex"))
        # second run adds nothing (idempotent → no log growth)
        main(["--execute", "--db", self.db, "--log", self.log])
        with open(self.log) as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_missing_db_silent_zero(self):
        rc = main(["--db", os.path.join(self.tmpdir, "nope.db"),
                   "--log", self.log])
        self.assertEqual(rc, 0)

    def test_running_task_never_mutated_even_if_fixable(self):
        db = _make_db([{"id": "t_run", "title": "x",
                        "body": "auto_created:true\n\nwork", "status": "running"}])
        main(["--execute", "--db", db, "--log", self.log])
        self.assertEqual(_body(db, "t_run"), "auto_created:true\n\nwork")


if __name__ == "__main__":
    unittest.main(verbosity=2)
