#!/usr/bin/env python3
"""Tests for the OBJ-02 cost-tag guarantee in objective-proposer.py (t_e3d17323).

Covers enforce_cost_tag + body_header_cost_tag:
  - canonical line-style bodies are untouched (the 5 existing detector paths)
  - pipe-style canonical tags are parsed and left alone
  - missing tag → injected (default tiny, position after auto_created)
  - non-canonical values (Cost: Small, coste:micro, cost:xl) → rewritten to a
    canonical category
  - backstop-layering: creator output must satisfy the cost-tag-fix.py backstop
    (action 'ok') so the two layers never fight
  - idempotency
  - prose 'cost:' mentions below the header are ignored
  - create_triage_task applies the enforcement before calling hermes CLI

Run:
  python3 -m pytest test_cost_tag_enforcer.py -v
  python3 test_cost_tag_enforcer.py   # direct run
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

_spec = importlib.util.spec_from_file_location(
    "objective_proposer", os.path.join(SCRIPT_DIR, "scripts", "objective-proposer.py")
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["objective_proposer"] = _mod
_spec.loader.exec_module(_mod)

from objective_proposer import (
    DEFAULT_COST_TAG,
    body_header_cost_tag,
    enforce_cost_tag,
    create_triage_task,
)


class TestBodyHeaderCostTag(unittest.TestCase):
    def test_line_style(self):
        self.assertEqual(
            body_header_cost_tag("objective:X\nauto_created:true\ncost:small\n"),
            "small",
        )

    def test_pipe_style(self):
        self.assertEqual(
            body_header_cost_tag(
                "objective:X | auto_created:true | cost:tiny | model:fast\n"),
            "tiny",
        )

    def test_case_and_spacing_normalised_on_read(self):
        self.assertEqual(
            body_header_cost_tag("auto_created:true\n  Cost :  Small \n"), "small"
        )

    def test_prose_below_header_ignored(self):
        self.assertIsNone(
            body_header_cost_tag("objective:X\nauto_created:true\n\nprose cost:small\n")
        )

    def test_no_tag_returns_none(self):
        self.assertIsNone(body_header_cost_tag("objective:X\n\nBody\n"))

    def test_empty_body(self):
        self.assertIsNone(body_header_cost_tag(""))

    def test_unknown_value_returns_none(self):
        self.assertIsNone(body_header_cost_tag("objective:X | cost:xl\n"))


class TestEnforceCostTag(unittest.TestCase):
    def test_canonical_line_body_untouched(self):
        body = "objective:OBJ-X\nauto_created:true\ncost:small\nmodel:fast\n\nBody...\n"
        new, action = enforce_cost_tag(body)
        self.assertEqual(action, "keep")
        self.assertEqual(new, body)

    def test_canonical_pipe_body_untouched(self):
        body = "objective:OBJ-3 | auto_created:true | cost:small | model:fast\n\nB\n"
        new, action = enforce_cost_tag(body)
        self.assertEqual(action, "keep")
        self.assertEqual(new, body)

    def test_all_static_detector_bodies(self):
        # The 5 static cost tags used across the 5 detectors: 4x small, 1x tiny.
        for tset in ("small", "tiny"):
            body = f"objective:OBJ-X\nauto_created:true\ncost:{tset}\nprovider:pr-ollama\n\nB\n"
            new, action = enforce_cost_tag(body)
            self.assertEqual(action, "keep")
            self.assertEqual(new, body)

    def test_missing_tag_injected(self):
        body = "objective:OBJ-X\nauto_created:true\nmodel:fast\n\nBody...\n"
        new, action = enforce_cost_tag(body)
        self.assertEqual(action, "inject")
        head = new.split('\n\n', 1)[0]
        self.assertIn("cost:tiny", head)
        idx_auto = head.splitlines().index("auto_created:true")
        self.assertEqual(head.splitlines()[idx_auto + 1], "cost:tiny")

    def test_missing_tag_body_without_auto_created(self):
        new, action = enforce_cost_tag("objective:OBJ-X\n\nB\n")
        self.assertEqual(action, "inject")
        self.assertEqual(new.splitlines()[0], "cost:tiny")

    def test_nonstandard_case_value_normalised(self):
        new, action = enforce_cost_tag("objective:X\nauto_created:true\nCost: Small\n")
        self.assertEqual(action, "keep")  # value 'small' parses fine; no rewrite
        self.assertEqual(body_header_cost_tag(new), "small")

    def test_spanish_value_unmappable_rewritten(self):
        # 'coste: mesmer'-style prose / unmappable values → canonical default.
        new, action = enforce_cost_tag("objective:X\nauto_created:true\ncoste:mesmer\n")
        self.assertEqual(action, "normalise")
        self.assertEqual(body_header_cost_tag(new), DEFAULT_COST_TAG)

    def test_pipe_invalid_rewritten_in_place_keeps_separators(self):
        body = "objective:OBJ-1 | auto_created:true | cost:xl | model:fast\n\nB\n"
        new, action = enforce_cost_tag(body)
        self.assertEqual(action, "normalise")
        self.assertIn(" | cost:tiny | ", new)
        self.assertEqual(body_header_cost_tag(new), "tiny")

    def test_pipe_missing_segment_injected(self):
        body = "objective:OBJ-2 | auto_created:true | model:fast\n\nB\n"
        new, action = enforce_cost_tag(body)
        self.assertEqual(action, "inject")
        self.assertIn(" | cost:tiny", new.split("\n\n")[0])

    def test_idempotent(self):
        body = "objective:X\nauto_created:true\nmodel:fast\n\nB\n"
        once, a1 = enforce_cost_tag(body)
        twice, a2 = enforce_cost_tag(once)
        self.assertEqual(a2, "keep")
        self.assertEqual(once, twice)

    def test_default_override_respected(self):
        new, action = enforce_cost_tag(
            "objective:X\nauto_created:true\n\nB\n", default="small"
        )
        self.assertEqual(action, "inject")
        self.assertIn("cost:small", new)

    def test_trailing_newline_preserved(self):
        no_nl = "objective:X\nauto_created:true\nmodel:fast\n\nB"
        new, action = enforce_cost_tag(no_nl)
        self.assertEqual(action, "inject")
        self.assertFalse(new.endswith("\n"))


class TestBackstopLayering(unittest.TestCase):
    """Layering: proposer-enforced bodies must be a no-op for the
    cost-tag-fix.py DB backstop (action 'ok' or 'skip')."""

    @staticmethod
    def _backstop_action(body: str) -> str:
        ctf_path = os.path.join(SCRIPT_DIR, "scripts", "cost-tag-fix.py")
        spec = importlib.util.spec_from_file_location("cost_tag_fix_l", ctf_path)
        ctf = importlib.util.module_from_spec(spec)
        sys.modules["cost_tag_fix_l"] = ctf
        spec.loader.exec_module(ctf)
        action, _idx, _val = ctf.analyze_body(body)
        return action

    def _run_case(self, name: str, body: str):
        new, action = enforce_cost_tag(body)
        bs = self._backstop_action(new)
        self.assertIn(bs, ("ok", "skip"),
                      f"{name}: proposer action={action}, backstop={bs}")

    def test_line(self):
        self._run_case(
            "line", "objective:OBJ-X\nauto_created:true\ncost:small\nmodel:fast\n\nB\n")

    def test_pipe(self):
        self._run_case(
            "pipe", "objective:OBJ-3 | auto_created:true | cost:small | model:fast\n\nB\n")

    def test_inject(self):
        self._run_case(
            "inject", "objective:X\nauto_created:true\nmodel:fast\n\nB\n")

    def test_normalise(self):
        self._run_case(
            "normalise", "objective:OBJ-1 | auto_created:true | cost:xl | model:fast\n\nB\n")


class TestCreateTriageTaskEnforces(unittest.TestCase):
    """create_triage_task must apply enforcement before the CLI call."""

    def test_execute_enforces_body(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = "created task t_abc123"
                stderr = ""

            return R()

        body = "objective:OBJ-X\nauto_created:true\nmodel:fast\n\nB\n"
        with patch.object(_mod, "subprocess") as fake_subprocess:
            fake_subprocess.run = fake_run
            fake_subprocess.TimeoutExpired = _mod.subprocess.TimeoutExpired
            fake_subprocess.OSError = OSError
            task_id = _mod.create_triage_task("OBJ-X test", body)
        self.assertEqual(task_id, "t_abc123")
        sent_body = captured["cmd"][captured["cmd"].index("--body") + 1]
        self.assertEqual(body_header_cost_tag(sent_body), "tiny")

    def test_execute_keeps_canonical_body(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = "created task t_def456"
                stderr = ""

            return R()

        body = "objective:X\nauto_created:true\ncost:small\n\nB\n"
        with patch.object(_mod, "subprocess") as fake_subprocess:
            fake_subprocess.run = fake_run
            fake_subprocess.TimeoutExpired = _mod.subprocess.TimeoutExpired
            fake_subprocess.OSError = OSError
            task_id = _mod.create_triage_task("OBJ-X test", body)
        self.assertEqual(task_id, "t_def456")
        sent_body = captured["cmd"][captured["cmd"].index("--body") + 1]
        # Untouched: the command receives the exact original body.
        self.assertEqual(sent_body, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
