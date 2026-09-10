#!/usr/bin/env python3
"""Tests for scripts/abandon-superseded.py (OBJ-08 / t_78acb6dc).

Covers the t_78acb6dc contract:
  - only archived-without-completed_at tasks are stampable, and only when a
    LATER superseding task (done / archived-with-completed_at) of the same
    objective relates by id-reference or normalized title
  - temporal guard: an EARLIER successful task never supersedes a later loss
    (a real orphan must stay unstamped)
  - tasks already carrying an abandoned: header line are never re-stamped
  - header-only stamp: prose mentions of abandoned: below the header don't count
  - stamp line is appended INSIDE the tag header (before the first blank line)
  - body preserved byte-for-byte except the inserted line (+ trailing newline)
  - superseder caps and id normalization
  - status guard: execute_plan only updates rows still status='archived'
  - idempotency: stamped tasks are not re-planned
  - dry-run writes nothing; log records are append-only JSONL

Run:
  /usr/bin/python3.12 -m pytest test_abandon_superseded.py -v
  /usr/bin/python3.12 test_abandon_superseded.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "abandon_superseded", os.path.join(SCRIPT_DIR, "scripts", "abandon-superseded.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["abandon_superseded"] = _mod
_spec.loader.exec_module(_mod)

import abandon_superseded as ab  # noqa: E402

UTC = timezone.utc


def _ts(y, mo, d, h=12):
    return int(datetime(y, mo, d, h, tzinfo=UTC).timestamp())


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "kanban.db"
        self.log = self.tmp / "stamps.jsonl"

    def tearDown(self):
        pass

    def _mk(self, tasks):
        """tasks: list of (id, title, status, body, created_at, completed_at)."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT, body TEXT,
            created_at INTEGER, started_at INTEGER, completed_at INTEGER)""")
        for tid, title, status, body, cr, ca in tasks:
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                         (tid, title, status, body, cr, cr, ca))
        conn.commit()
        conn.close()

    def _plan_ids(self):
        return [p["id"] for p in ab.find_candidates(str(self.db))]


class TestCandidateRule(Sandbox):
    def test_referencing_later_done_supersedes(self):
        # the OBJ-06 shape: lost task body references a later completed task
        self._mk([
            ("t_aaaaaaaa", "Fix X [intento 2]", "archived",
             "objective:OBJ-06\nref: t_bbbbbbbb (blocked)", _ts(2026, 8, 28), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nimplementacion", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), ["t_aaaaaaaa"])

    def test_referenced_by_later_done_supersedes(self):
        # later task references the lost one (OBJ-16 shape)
        self._mk([
            ("t_aaaaaaaa", "Fix spam", "archived",
             "objective:OBJ-16\nautocreado", _ts(2026, 9, 4), None),
            ("t_bbbbbbbb", "Fix spam v2", "done",
             "objective:OBJ-16\nreemplazo de t_aaaaaaaa perdida", _ts(2026, 9, 6),
             _ts(2026, 9, 7)),
        ])
        self.assertEqual(self._plan_ids(), ["t_aaaaaaaa"])

    def test_same_normalized_title_supersedes(self):
        # OBJ-19 shape: retry generations share the title
        self._mk([
            ("t_aaaaaaaa", "Documentar matriz", "archived",
             "objective:OBJ-19\ncost:small", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Documentar matriz — [reintento 3]", "archived",
             "objective:OBJ-19\ncost:small", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), ["t_aaaaaaaa"])

    def test_earlier_success_never_supersedes(self):
        # temporal guard: the successful task is OLDER than the loss -> real loss
        self._mk([
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nimplementacion", _ts(2026, 8, 20), _ts(2026, 8, 21)),
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\ngave_up", _ts(2026, 9, 1), None),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_no_related_task_is_real_loss(self):
        self._mk([
            ("t_aaaaaaaa", "Never done thing", "archived",
             "objective:OBJ-20\ngave_up", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Totally other work", "done",
             "objective:OBJ-20\notra cosa", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_objective_tag_in_footer_supersedes(self):
        # OBJ-20 bug: t_3cd3dd45 carries objective:OBJ-20 only in the footer
        # (below the first blank line). objective_of() must scan the WHOLE
        # body so the superseding task joins the OBJ-20 group and the lost
        # task t_1725897f is stamped. Footer scan must NOT suppress the stamp
        # (has_abandoned_stamp stays header-only).
        self._mk([
            ("t_1725897f", "Fix X", "archived",
             "objective:OBJ-20 cost:small provider:pr-ollama\n\nprose", _ts(2026, 8, 30), None),
            ("t_3cd3dd45", "Fix X", "archived",
             "Investigar 403\n\nnotas\nobjective:OBJ-20", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        plans = ab.find_candidates(str(self.db))
        self.assertEqual([p["id"] for p in plans], ["t_1725897f"])
        self.assertIn("t_3cd3dd45", plans[0]["superseded_by"])

    def test_footer_objective_crossref_without_matching_title_is_not_candidate(self):
        # Negative: a later completed footer-objective task that merely shares
        # the objective but is unrelated (different title, no id reference)
        # must NOT supersede the lost task.
        self._mk([
            ("t_aaaaaaaa", "Fix the thing", "archived",
             "objective:OBJ-20 cost:small\n\ntexto", _ts(2026, 8, 30), None),
            ("t_bbbbbbbb", "Totally unrelated work", "archived",
             "headline here\n\nobjective:OBJ-20 body", _ts(2026, 9, 2), _ts(2026, 9, 4)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_unrelated_objective_ignored(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\ngave_up", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-21\notro objetivo, mismo titulo", _ts(2026, 9, 2),
             _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_running_and_done_not_stampable(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "running",
             "objective:OBJ-06\nviva", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_archived_with_completed_at_not_stampable(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\nlimpiado", _ts(2026, 9, 1), _ts(2026, 9, 2)),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_already_stamped_not_replanned(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\nabandoned: superseded-by t_bbbbbbbb — manual\ngave_up",
             _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_prose_abandoned_mention_does_not_count(self):
        # a prose mention below the header must NOT suppress re-planning...
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\ngave_up\n\nse abandono la tarea (abandoned: x)",
             _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        # ...but the candidate still gets planned (stamp belongs in the HEADER)
        self.assertEqual(self._plan_ids(), ["t_aaaaaaaa"])

    def test_superseder_archived_without_ts_rejected(self):
        # archived-without-completed_at does NOT count as success (OBJ-19 chain
        # of mutual retries must not self-adopt)
        self._mk([
            ("t_aaaaaaaa", "Doc matriz", "archived",
             "objective:OBJ-19\ncost:small", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Documentar matriz", "archived",
             "objective:OBJ-19\ncost:small", _ts(2026, 9, 2), None),
        ])
        self.assertEqual(self._plan_ids(), [])

    def test_objective_x_ignored(self):
        self._mk([
            ("t_aaaaaaaa", "placeholder", "archived",
             "objective:OBJ-X\nx", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "placeholder", "done",
             "objective:OBJ-X\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        self.assertEqual(self._plan_ids(), [])


class TestStampContent(Sandbox):
    def test_stamp_in_header_and_body_intact(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\ncost:small\n\n## Cuerpo\ntexto", _ts(2026, 8, 28), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nimpl", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        plans = ab.find_candidates(str(self.db))
        self.assertEqual(len(plans), 1)
        p = plans[0]
        lines = p["new_body"].splitlines()
        end = ab.header_bounds(lines)
        self.assertTrue(any(ab.ABANDONED_TAG_RE.match(l) for l in lines[:end]),
                        "stamp must live inside the tag header")
        # stamp appended right after the existing header lines, and the blank
        # separator line that closed the header is preserved below it
        self.assertEqual(lines[2], p["stamp"])
        # original body preserved below the insertion point (blank + prose)
        self.assertEqual("\n".join(lines[3:]), "\n## Cuerpo\ntexto")
        self.assertIn("t_bbbbbbbb", p["stamp"])

    def test_no_header_blank_inserts_at_top_block(self):
        # body without a blank line: header is the whole body; stamp goes last
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-20\ncost:small", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "archived",
             "objective:OBJ-20\ncost:small", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        plans = ab.find_candidates(str(self.db))
        self.assertEqual(len(plans), 1)
        lines = plans[0]["new_body"].splitlines()
        self.assertEqual(ab.header_bounds(lines), len(lines))
        self.assertTrue(ab.ABANDONED_TAG_RE.match(lines[-1]))

    def test_norm_title_strips_attempt_suffixes(self):
        self.assertEqual(ab.norm_title("Fix X [intento 2]"),
                         ab.norm_title("fix x"))
        self.assertEqual(ab.norm_title("Fix X — reintento (fresh)"),
                         ab.norm_title("FIX X"))
        self.assertNotEqual(ab.norm_title("Fix X"), ab.norm_title("Fix Y"))

    def test_superseder_cap(self):
        ev = {"first": {"id": "t_1"}, "all": [f"t_{i:08d}" for i in range(20)]}
        stamp = ab.build_stamp_line("r", ev["all"])
        self.assertEqual(stamp.count("t_"), 8)


class TestExecute(Sandbox):
    def _mk_board(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-09\nprovider:pr-nanogpt\n\nbody", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-09\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])

    def test_dry_run_writes_nothing(self):
        self._mk_board()
        rc = ab.main(["--db", str(self.db), "--log", str(self.log)])
        self.assertEqual(rc, 0)
        self.assertFalse(self.log.exists())
        import sqlite3
        conn = sqlite3.connect(self.db)
        body = conn.execute(
            "SELECT body FROM tasks WHERE id='t_aaaaaaaa'").fetchone()[0]
        conn.close()
        self.assertNotIn("abandoned:", body)

    def test_execute_stamps_and_logs(self):
        self._mk_board()
        rc = ab.main(["--execute", "--db", str(self.db), "--log", str(self.log)])
        self.assertEqual(rc, 0)
        import sqlite3
        conn = sqlite3.connect(self.db)
        body = conn.execute(
            "SELECT body FROM tasks WHERE id='t_aaaaaaaa'").fetchone()[0]
        conn.close()
        self.assertIn("abandoned: superseded-by t_bbbbbbbb", body.splitlines()[2])
        rec = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(rec["task_id"], "t_aaaaaaaa")
        self.assertEqual(rec["superseded_by"], ["t_bbbbbbbb"])

    def test_execute_idempotent(self):
        self._mk_board()
        ab.main(["--execute", "--db", str(self.db), "--log", str(self.log)])
        # second run: already stamped -> no candidate, log untouched
        rc = ab.main(["--execute", "--db", str(self.db), "--log", str(self.log)])
        self.assertEqual(rc, 0)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_status_guard_rejects_non_archived(self):
        # task flips to 'done' between scan and write -> mini-CAS rejects
        self._mk_board()
        plans = ab.find_candidates(str(self.db))
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status='done' WHERE id='t_aaaaaaaa'")
        conn.commit()
        ok = ab.execute_plan(conn, plans[0])
        conn.close()
        self.assertFalse(ok)

    def test_only_filter(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-09\ngave_up", _ts(2026, 9, 1), None),
            ("t_cccccccc", "Fix Z", "archived",
             "objective:OBJ-16\ngave_up", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-09\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
            ("t_dddddddd", "Fix Z", "done",
             "objective:OBJ-16\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        rc = ab.main(["--execute", "--db", str(self.db),
                      "--log", str(self.log), "--only", "t_cccccccc"])
        self.assertEqual(rc, 0)
        import sqlite3
        conn = sqlite3.connect(self.db)
        b1 = conn.execute(
            "SELECT body FROM tasks WHERE id='t_aaaaaaaa'").fetchone()[0]
        b2 = conn.execute(
            "SELECT body FROM tasks WHERE id='t_cccccccc'").fetchone()[0]
        conn.close()
        self.assertNotIn("abandoned:", b1)
        self.assertIn("abandoned:", b2)

    def test_missing_db_exits_1(self):
        rc = ab.main(["--db", str(self.tmp / "nope.db")])
        self.assertEqual(rc, 1)


class TestDetectorIntegration(Sandbox):
    """The stamp must flip weekly-progress.py's lost accounting."""

    def test_stamped_task_not_lost_for_weekly_progress(self):
        self._mk([
            ("t_aaaaaaaa", "Fix X", "archived",
             "objective:OBJ-06\ngave_up\nabandoned: superseded-by t_bbbbbbbb — r",
             _ts(2026, 8, 28), None),
            ("t_bbbbbbbb", "Fix X", "done",
             "objective:OBJ-06\nimpl", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        wp = self._load_wp()
        by = wp.load_task_objectives(self.db)
        prog = wp.build_progress(by, [])
        self.assertEqual(prog["OBJ-06"]["status"], "complete")
        self.assertEqual(prog["OBJ-06"]["tasks_lost"], 0)
        # stamped abandoned task counts as resolved, not completed (kept
        # separate from the real done task)
        self.assertEqual(prog["OBJ-06"]["tasks_completed"], 1)

    def test_unstamped_loss_still_needs_attention(self):
        self._mk([
            ("t_aaaaaaaa", "Real orphan", "archived",
             "objective:OBJ-20\ngave_up", _ts(2026, 9, 1), None),
            ("t_bbbbbbbb", "Other work", "done",
             "objective:OBJ-20\nx", _ts(2026, 9, 2), _ts(2026, 9, 3)),
        ])
        wp = self._load_wp()
        by = wp.load_task_objectives(self.db)
        prog = wp.build_progress(by, [])
        self.assertEqual(prog["OBJ-20"]["status"], "needs_attention")
        self.assertEqual(prog["OBJ-20"]["tasks_lost"], 1)

    def _load_wp(self):
        import importlib.util
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "weekly_progress_wp", os.path.join(repo, "scripts", "weekly-progress.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


if __name__ == "__main__":
    unittest.main(verbosity=2)