#!/usr/bin/python3.12
"""test_morning_screen_units.py — offline unit suite for the pure helpers
of scripts/obs/morning-screen.py (t_f46aa0f5, objective:OBJ-AUTODEV).

Covers the five pure helpers with no direct unit coverage today:
  1. _usd_or_zero  — costUsd coercion (regression t_cecc9dfe: a raw sum()
     over string/garbage costUsd tumbled the whole portal).
  2. _fmt_ts       — epoch rendering "%d-%b %H:%M" in fixed CEST, "-" on
     falsy/garbage/overflow without raising.
  3. _fmt_usd      — "$x,xxx.0000" money rendering, "-" on None.
  4. _group_closures_by_objective — bucket by the body's `objective:OBJ-*`
     tag (numeric and table ids), "sin etiqueta" fallback, insertion order.
  5. _render_flight_lines — VUELO block: empty-week line, one line per
     sorted objective, 6-id truncation with "+N mas".

Hermetic: in-memory dict fixtures only — no ~/.hermes reads, no network,
no writes. The module is loaded by file path (pattern of
tests/test_p2_desired3_screen.py); tests/conftest.py fixes sys.path.

Run:  .venv/bin/python -m pytest tests/test_morning_screen_units.py -q
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent.parent)
SCREEN_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "morning-screen.py")

_spec = importlib.util.spec_from_file_location("morning_screen", SCREEN_SCRIPT)
screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen)


def _closure_row(tid, body):
    """Minimal done-task stand-in: _group_closures_by_objective only
    subscribes r["id"] and r["body"], so a plain dict is enough."""
    return {"id": tid, "body": body}


class TestUsdOrZero(unittest.TestCase):
    """costUsd coercion: numeric-ish -> float, anything else -> 0.0."""

    def test_decimal_string_coerces(self):
        self.assertEqual(screen._usd_or_zero("0.07"), 0.07)

    def test_none_is_zero(self):
        self.assertEqual(screen._usd_or_zero(None), 0.0)

    def test_garbage_string_is_zero(self):
        self.assertEqual(screen._usd_or_zero("free"), 0.0)

    def test_numeric_passthrough_to_float(self):
        self.assertIsInstance(screen._usd_or_zero(7), float)
        self.assertEqual(screen._usd_or_zero(7), 7.0)
        self.assertEqual(screen._usd_or_zero(0.25), 0.25)

    def test_empty_whitespace_and_bool(self):
        self.assertEqual(screen._usd_or_zero(""), 0.0)
        self.assertEqual(screen._usd_or_zero(" 0.1 "), 0.1)
        self.assertEqual(screen._usd_or_zero(True), 1.0)


class TestFmtTs(unittest.TestCase):
    """Epoch -> "%d-%b %H:%M" in fixed CEST; "-" on falsy/garbage."""

    def test_none_is_dash(self):
        self.assertEqual(screen._fmt_ts(None), "-")

    def test_zero_is_dash(self):
        self.assertEqual(screen._fmt_ts(0), "-")

    def test_empty_string_is_dash(self):
        self.assertEqual(screen._fmt_ts(""), "-")

    def test_valid_epoch_cest_render(self):
        # 1789574400 == 2026-09-16 16:00 UTC -> 18:00 at CEST (+2, module pin)
        self.assertEqual(screen._fmt_ts(1789574400), "16-Sep 18:00")

    def test_epoch_one_is_padded_and_cest_shifted(self):
        # 1970-01-01 00:00 UTC -> 01-Jan 02:00 CEST; zero-padded day.
        self.assertEqual(screen._fmt_ts(1), "01-Jan 02:00")

    def test_numeric_string_epoch_coerces(self):
        self.assertEqual(screen._fmt_ts("1789574400"), "16-Sep 18:00")

    def test_garbage_string_is_dash(self):
        self.assertEqual(screen._fmt_ts("x"), "-")

    def test_overflow_is_dash(self):
        self.assertEqual(screen._fmt_ts(1e30), "-")

    def test_out_of_range_negative_is_dash(self):
        self.assertEqual(screen._fmt_ts(-1e18), "-")

    def test_nonfinite_is_dash(self):
        self.assertEqual(screen._fmt_ts(float("nan")), "-")
        self.assertEqual(screen._fmt_ts(float("inf")), "-")


class TestFmtUsd(unittest.TestCase):
    """Money rendering: "${:,.4f}", "-" only on None."""

    def test_none_is_dash(self):
        self.assertEqual(screen._fmt_usd(None), "-")

    def test_single_value(self):
        self.assertEqual(screen._fmt_usd(1.5), "$1.5000")

    def test_thousands_separator(self):
        self.assertEqual(screen._fmt_usd(1234.5), "$1,234.5000")

    def test_zero_renders(self):
        self.assertEqual(screen._fmt_usd(0), "$0.0000")

    def test_int_renders_with_decimals(self):
        self.assertEqual(screen._fmt_usd(42), "$42.0000")


class TestGroupClosuresByObjective(unittest.TestCase):
    """Bucketing by the body's objective: tag, "sin etiqueta" fallback."""

    def test_numeric_objective_tag(self):
        rows = [_closure_row("t_1", "objective:OBJ-13\nclosed")]
        by = screen._group_closures_by_objective(rows)
        self.assertEqual(sorted(by), ["OBJ-13"])
        self.assertEqual([r["id"] for r in by["OBJ-13"]], ["t_1"])

    def test_table_objective_tag_with_space(self):
        rows = [_closure_row("t_4", "objective: OBJ-AUTODEV (auto)")]
        by = screen._group_closures_by_objective(rows)
        self.assertEqual(sorted(by), ["OBJ-AUTODEV"])
        self.assertEqual(by["OBJ-AUTODEV"][0]["id"], "t_4")

    def test_table_objective_tag_codequality(self):
        rows = [_closure_row("t_2", "objective:OBJ-CODEQUALITY")]
        by = screen._group_closures_by_objective(rows)
        self.assertEqual(by["OBJ-CODEQUALITY"][0]["id"], "t_2")

    def test_untagged_and_none_body_fall_to_sin_etiqueta(self):
        rows = [
            _closure_row("t_3", None),
            _closure_row("t_5", "no tag here"),
        ]
        by = screen._group_closures_by_objective(rows)
        self.assertEqual(sorted(by), ["sin etiqueta"])
        self.assertEqual([r["id"] for r in by["sin etiqueta"]],
                         ["t_3", "t_5"])

    def test_insertion_order_of_buckets_and_rows(self):
        rows = [
            _closure_row("t_1", "objective:OBJ-13"),
            _closure_row("t_2", "objective:OBJ-CODEQUALITY"),
            _closure_row("t_3", None),
            _closure_row("t_4", "objective: OBJ-AUTODEV"),
            _closure_row("t_5", "sin tag"),
            _closure_row("t_6", "objective:OBJ-13"),
        ]
        by = screen._group_closures_by_objective(rows)
        self.assertEqual(list(by),
                         ["OBJ-13", "OBJ-CODEQUALITY", "sin etiqueta",
                          "OBJ-AUTODEV"])
        self.assertEqual([r["id"] for r in by["OBJ-13"]], ["t_1", "t_6"])
        self.assertEqual([r["id"] for r in by["sin etiqueta"]],
                         ["t_3", "t_5"])


class TestRenderFlightLines(unittest.TestCase):
    """VUELO block rendering: header, sorted objectives, 6-id truncation."""

    HEADER = "VUELO (rendicion semanal del mandato — OBJ-42)"

    def test_total_zero_renders_empty_week_line(self):
        out = screen._render_flight_lines({}, 0, 0.0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], self.HEADER)
        self.assertIn("sin cierres en 7d", lines[2])
        self.assertNotIn("cerradas (", out)

    def test_header_counts_and_spent(self):
        by = {"OBJ-13": [_closure_row("t_1", "objective:OBJ-13")]}
        out = screen._render_flight_lines(by, 1, 1.5)
        lines = out.splitlines()
        self.assertEqual(lines[0], self.HEADER)
        self.assertEqual(lines[1],
                         "  cerradas 7d: 1 | gasto balance 7d: $1.5000")

    def test_thousands_spent(self):
        out = screen._render_flight_lines({}, 0, 1234.5)
        self.assertIn("gasto balance 7d: $1,234.5000", out)

    def test_one_line_per_sorted_objective(self):
        by = {
            "OBJ-CODEQUALITY": [_closure_row("t_2",
                                             "objective:OBJ-CODEQUALITY")],
            "OBJ-13": [_closure_row("t_1", "objective:OBJ-13")],
            "sin etiqueta": [_closure_row("t_3", None),
                             _closure_row("t_5", "x")],
        }
        out = screen._render_flight_lines(by, 4, 0.0)
        objs = [ln.strip().split(":")[0] for ln in out.splitlines()[2:]]
        self.assertEqual(objs, ["OBJ-13", "OBJ-CODEQUALITY", "sin etiqueta"])
        self.assertIn("OBJ-13: 1 cerradas (t_1)", out)
        self.assertIn("sin etiqueta: 2 cerradas (t_3, t_5)", out)

    def test_truncation_to_six_ids_with_mas(self):
        rows = [_closure_row("t_%02d" % i, "objective:OBJ-13")
                for i in range(1, 9)]
        out = screen._render_flight_lines({"OBJ-13": rows}, 8, 0.0)
        self.assertIn("OBJ-13: 8 cerradas "
                      "(t_01, t_02, t_03, t_04, t_05, t_06, +2 mas)", out)

    def test_exactly_six_ids_no_mas_suffix(self):
        rows = [_closure_row("t_%02d" % i, "objective:OBJ-13")
                for i in range(1, 7)]
        out = screen._render_flight_lines({"OBJ-13": rows}, 6, 0.0)
        self.assertIn("OBJ-13: 6 cerradas "
                      "(t_01, t_02, t_03, t_04, t_05, t_06)", out)
        self.assertNotIn(" mas", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
