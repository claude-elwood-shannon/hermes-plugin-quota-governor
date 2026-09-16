#!/usr/bin/python3.12
"""Tests for verify_recommendation_unchanged.py extracted helpers.

Offline suite: covers _strip_additive (both-sides stripping of additive
keys), report_identical sanity branches and diff_mismatch verdict rc.
Fixtures only — no real board, no git calls (only the main() smoke test
exercises git, reading HEAD's committed gate).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "verify_recommendation_unchanged",
    _HERE / "verify_recommendation_unchanged.py")
assert _SPEC is not None and _SPEC.loader is not None
vru = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vru)


class StripAdditive(unittest.TestCase):
    def test_strips_both_additive_keys_and_preserves_rest(self):
        out = {"wakeAgent": True,
               "context": {"privacy_summary": {"none": 4},
                           "zombie_check": {"count": 0},
                           "max_workers": 1}}
        stripped_out, extra = vru._strip_additive(out)
        self.assertEqual(stripped_out,
                         {"wakeAgent": True, "context": {"max_workers": 1}})
        self.assertEqual(extra, {"privacy_summary": {"none": 4},
                                 "zombie_check": {"count": 0}})

    def test_input_dict_is_not_mutated(self):
        out = {"context": {"privacy_summary": {"none": 1}, "keep": "x"}}
        vru._strip_additive(out)
        self.assertEqual(out, {"context": {"privacy_summary": {"none": 1},
                                           "keep": "x"}})

    def test_no_additive_keys_present(self):
        out = {"wakeAgent": False, "context": {"max_workers": 2}}
        stripped_out, extra = vru._strip_additive(out)
        self.assertEqual(extra, {})
        self.assertEqual(stripped_out, out)

    def test_missing_context_key(self):
        # The original nested helper always sets out2["context"], even when
        # the input had none — the extraction preserves that verbatim.
        stripped_out, extra = vru._strip_additive({"wakeAgent": True})
        self.assertEqual(extra, {})
        self.assertEqual(stripped_out, {"wakeAgent": True, "context": {}})


class ReportIdentical(unittest.TestCase):
    def test_quiet_board_passes(self):
        stripped = {
            "privacy_summary": {"high": 0, "medium": 0, "low": 0, "none": 4},
            "zombie_check": {"count": 0, "has_zombie": False, "tasks": [],
                             "threshold_minutes": 45.0},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.report_identical(stripped)
        self.assertEqual(rc, 0)
        self.assertIn("VERDICT: IDENTICAL", buf.getvalue())
        self.assertIn("privacy_summary sanity", buf.getvalue())
        self.assertIn("zombie_check sanity", buf.getvalue())

    def test_privacy_mismatch_returns_1(self):
        stripped = {
            "privacy_summary": {"high": 1, "medium": 0, "low": 0, "none": 4},
            "zombie_check": {"count": 0, "has_zombie": False, "tasks": [],
                             "threshold_minutes": 45.0},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.report_identical(stripped)
        self.assertEqual(rc, 1)
        self.assertIn("WARNING: privacy_summary unexpected", buf.getvalue())

    def test_privacy_summary_missing_returns_1(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.report_identical({})
        self.assertEqual(rc, 1)

    def test_zombie_check_missing_returns_1(self):
        stripped = {"privacy_summary": {"high": 0, "medium": 0,
                                        "low": 0, "none": 4}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.report_identical(stripped)
        self.assertEqual(rc, 1)
        self.assertIn("WARNING: zombie_check additive key missing",
                      buf.getvalue())

    def test_zombies_reported_returns_1(self):
        stripped = {
            "privacy_summary": {"high": 0, "medium": 0, "low": 0, "none": 4},
            "zombie_check": {"count": 2, "has_zombie": True,
                             "tasks": ["t_x"], "threshold_minutes": 45.0},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.report_identical(stripped)
        self.assertEqual(rc, 1)
        self.assertIn("WARNING: zombie_check unexpected", buf.getvalue())


class DiffMismatch(unittest.TestCase):
    def test_returns_1_and_prints_unified_diff(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.diff_mismatch('{"a": 1}', '{"a": 2}')
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("VERDICT: DIFFERENT", out)
        self.assertIn("--- old(HEAD)", out)
        self.assertIn("+++ new(S1)", out)


class MainSmoke(unittest.TestCase):
    def test_main_reaches_a_verdict_and_returns_int(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vru.main()
        self.assertIn(rc, (0, 1))
        self.assertIn("VERDICT:", buf.getvalue())
        # cleanup contract: both temp artifacts are removed
        self.assertFalse(Path(vru.OLD_GATE).exists())


if __name__ == "__main__":
    unittest.main()