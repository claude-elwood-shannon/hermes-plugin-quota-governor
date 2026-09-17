#!/usr/bin/python3.12
"""Tests for criterion_evidenced() / declared_criterion() in
scripts/obs/efficiency-ratio.py.

Offline only: both functions are pure string logic — no network, no
kanban.db, no fixtures. Expectations pin the behavior AS IMPLEMENTED
today (anchor-first evidence rule, OBJ-METRICS t_7aaa897c); these tests
change no logic. If a real bug is found later, mark the case xfail with
a comment instead of silently changing the expectation.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "efficiency_ratio", _HERE / "scripts" / "obs" / "efficiency-ratio.py")
er = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(er)


class CriterionEvidencedPaths(unittest.TestCase):
    """success: <path> — anchor evidence via full run or basename."""

    CRIT = "tests/test_efficiency_ratio_criterion.py existe en el repo"

    def test_path_evidenced_true(self):
        out = ("Added tests/test_efficiency_ratio_criterion.py covering the "
               "criterion; pytest passed. Done.")
        self.assertTrue(er.criterion_evidenced(self.CRIT, out))

    def test_path_not_evidenced_false(self):
        out = ("Wrote a new offline test suite for the criterion function; "
               "everything is green. Done.")
        self.assertFalse(er.criterion_evidenced(self.CRIT, out))

    def test_path_basename_counts_as_hit(self):
        # Documented rule: an anchor also hits via its basename (>= 5 chars).
        crit = "scripts/obs/efficiency-ratio.py verificado"
        out = "efficiency-ratio.py extended with the new checks. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_basename_under_5_chars_is_not_evidence(self):
        # tail "a.py" is shorter than 5 chars -> no anchor hit -> False.
        crit = "out/a.py generado"
        out = "a.py generated and logged. Done."
        self.assertFalse(er.criterion_evidenced(crit, out))


class CriterionEvidencedCommands(unittest.TestCase):
    """success: <command> — output must quote the command's anchors."""

    CRIT = "python3 -m pytest tests/test_x.py -q termina rc=0"

    def test_command_output_true(self):
        out = "ran python3 -m pytest tests/test_x.py -q, rc=0. Done."
        self.assertTrue(er.criterion_evidenced(self.CRIT, out))

    def test_command_missing_from_output_false(self):
        out = "pytest suite is green, 8 passed. Done."
        self.assertFalse(er.criterion_evidenced(self.CRIT, out))


class NormVariants(unittest.TestCase):
    """_norm(): lowercase + NFKD + combining-strip + typographic fold +
    whitespace collapse — both sides must meet in the middle."""

    def test_case_and_accents_folded(self):
        crit = "resultado: verificación y número de tests pasan"
        out = "VERIFICACION del NUMERO: 12 passed. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_accents_only_on_output_side(self):
        crit = "verificacion y numero de tests pasan"
        out = "Verificación del número: 12 passed. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_typographic_hyphens_folded(self):
        # U+2011 non-breaking hyphen (the exact _FOLD docstring case).
        crit = "scripts/fondo\u2011queue\u2011watch.py revisado"
        out = "scripts/fondo-queue-watch.py reviewed. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_whitespace_collapsed(self):
        crit = "  out/quality-check.json     regenerado   "
        out = "regenerated out/quality-check.json. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_backticks_do_not_block_match(self):
        out = "ran `python3 -m pytest tests/test_x.py -q` -> rc=0. Done."
        self.assertTrue(er.criterion_evidenced(self.CRIT_CMD, out))

    CRIT_CMD = "python3 -m pytest tests/test_x.py -q termina rc=0"


class HonestyRule(unittest.TestCase):
    """No completion word, or zero overlap, is never enough."""

    def test_no_completion_word_false(self):
        crit = "tests/test_x.py actualizado"
        out = "tests/test_x.py was updated in this branch."
        self.assertFalse(er.criterion_evidenced(crit, out))

    def test_completion_word_alone_false(self):
        crit = "tests/test_efficiency_ratio_criterion.py existe en el repo"
        out = "All done, nothing left."
        self.assertFalse(er.criterion_evidenced(crit, out))


class SpanishAlias(unittest.TestCase):
    """'criterio de éxito:' alias resolves like 'success:' and evidences
    the same way."""

    def test_declared_criterion_alias_prose(self):
        body = ("objective:OBJ-METRICS\n\n"
                "criterio de éxito: pytest -q tests/test_x.py en verde\n")
        kind, crit = er.declared_criterion(body)
        self.assertEqual(kind, "prose")
        self.assertEqual(crit, "pytest -q tests/test_x.py en verde")
        out = "pytest -q tests/test_x.py en verde, rc=0. Done."
        self.assertTrue(er.criterion_evidenced(crit, out))

    def test_tag_and_alias_equivalent_evidence(self):
        kind_t, crit_t = er.declared_criterion("success: tests/test_x.py\n")
        kind_e, crit_e = er.declared_criterion(
            "Criterio de éxito: tests/test_x.py\n")
        self.assertEqual(kind_t, "tag")
        self.assertEqual(kind_e, "prose")
        self.assertEqual(crit_t, crit_e)
        out = "created tests/test_x.py. Done."
        self.assertTrue(er.criterion_evidenced(crit_t, out))
        self.assertTrue(er.criterion_evidenced(crit_e, out))


class EdgeCases(unittest.TestCase):
    """Empty / None criterion and output -> False, never an exception."""

    def test_empty_criterion_false(self):
        for crit in ("", "   ", None):
            self.assertFalse(er.criterion_evidenced(crit, "all done"))

    def test_empty_output_false(self):
        self.assertFalse(er.criterion_evidenced("tests/test_x.py", ""))
        self.assertFalse(er.criterion_evidenced("tests/test_x.py", None))


if __name__ == "__main__":
    unittest.main()
