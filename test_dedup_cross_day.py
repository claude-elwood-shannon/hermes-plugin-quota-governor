"""Test cross-day dedup in _already_proposed (OBJ-20 root cause fix).

Verifies that when a recurring error was allowed on a previous day, the
proposer does not re-propose the same error on subsequent days, even if
the stale errors remain in the 7-day observation window.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Import the module under test (hyphenated filename, need importlib)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(PLUGIN_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import importlib
_op = importlib.import_module("objective-proposer")
_already_proposed = _op._already_proposed


class TestCrossDayDedup(unittest.TestCase):
    """Cross-day dedup prevents stale errors from generating duplicate tasks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.proposals_file = os.path.join(self.tmpdir, "proposals.jsonl")
        self._orig_file = _op.PROPOSALS_FILE
        _op.PROPOSALS_FILE = self.proposals_file

    def tearDown(self):
        _op.PROPOSALS_FILE = self._orig_file

    def _write_entry(self, date, allowed, pattern_key=None, title="", evidence=""):
        entry = {
            "timestamp": f"{date}T00:00:00.000000+00:00",
            "date": date,
            "title": title,
            "allowed": allowed,
            "violations": [],
            "warnings": [],
        }
        if pattern_key:
            entry["pattern_key"] = pattern_key
        if evidence:
            entry["evidence"] = evidence
        with open(self.proposals_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def test_same_day_exact_match_blocks(self):
        """Same-day, same pattern_key -> dedup."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pk = "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
        self._write_entry(today, True, pattern_key=pk)
        self.assertTrue(_already_proposed(pk))

    def test_cross_day_blocks_with_pattern_key(self):
        """Previous day allowed with pattern_key -> cross-day dedup blocks."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = "2026-09-01"
        pk = "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
        self._write_entry(yesterday, True, pattern_key=pk,
                         title="OBJ-X: Fix recurring error (4x in 7d)",
                         evidence="Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d")
        self.assertTrue(_already_proposed(pk))

    def test_cross_day_blocks_same_pattern_key(self):
        """Previous day allowed with same pattern_key -> blocks (exact match on evidence)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = "2026-09-01"
        pk = "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
        self._write_entry(yesterday, True, pattern_key=pk,
                         evidence="Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d")
        self.assertTrue(_already_proposed(pk))

    def test_different_error_not_blocked(self):
        """A different error pattern should NOT be blocked by cross-day dedup."""
        yesterday = "2026-09-01"
        old_pk = "recurring_error:Error 'ollama: some other error' appeared 3 times in 7d"
        self._write_entry(yesterday, True, pattern_key=old_pk,
                         evidence="Error 'ollama: some other error' appeared 3 times in 7d")
        new_pk = "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
        self.assertFalse(_already_proposed(new_pk))

    def test_rejected_proposal_also_blocked_cross_day(self):
        """Rejected entries (allowed=False) ALSO block cross-day re-proposal.

        Commit cfd5774 intentionally changed _already_proposed to check ALL
        entries (both allowed and rejected). A rejected entry means "we already
        saw this pattern and decided not to propose it" — re-proposing it every
        tick wastes the GR6 daily quota and clutters the proposals file.
        """
        yesterday = "2026-09-01"
        pk = "recurring_error:Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d"
        self._write_entry(yesterday, False, pattern_key=pk,
                         evidence="Error 'nanogpt: HTTP Error N: Forbidden' appeared 4 times in 7d")
        self.assertTrue(_already_proposed(pk))

    def test_no_file_returns_false(self):
        """No proposals file -> not already proposed."""
        _op.PROPOSALS_FILE = "/nonexistent/path.jsonl"
        self.assertFalse(_already_proposed("any_key"))


if __name__ == "__main__":
    unittest.main()