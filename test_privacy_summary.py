#!/usr/bin/env python3
"""test_privacy_summary.py — OBJ-18 S1: privacy_summary census in quota-gate.py.

Covers compute_privacy_summary() and _parse_privacy_tag_raw() (OBJ-18 S1,
t_179df1d6): a READ-ONLY census of the ``privacy:`` tags found in the
bodies of active (non-terminal) kanban tasks, exposed in the gate
snapshot as ``privacy_summary``.

Key invariants under test:
  1. Task without a privacy tag → counted as "none".
  2. Malformed tag (empty value, unrecognised value) → counted as
     "none" AND a warning is emitted (tag ignored, never breaks).
  3. Counting is multiprofile-correct: tasks assigned to different
     profiles/assignees are all censused together from the same DB.
  4. Recognised tags bucket correctly:
       privacy:high / sensitive / sens      → high
       privacy:medium                      → medium
       privacy:low / public / pub          → low
  5. Terminal tasks (done / archived) are NOT counted.
  6. No kanban.db / DB error → zeroed summary, fail open.
  7. The summary never feeds select_provider (routing untouched, S2).

Run:
  /usr/bin/python3.12 test_privacy_summary.py
  /usr/bin/python3.12 -m pytest test_privacy_summary.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure the script dir is on the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

# Import the module (quota-gate.py has a hyphen, so import via importlib)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "quota_gate_privacy_summary",
    os.path.join(SCRIPTS_DIR, "quota-gate.py"),
)
quota_gate = importlib.util.module_from_spec(_spec)
sys.modules["quota_gate_privacy_summary"] = quota_gate
_spec.loader.exec_module(quota_gate)

from quota_gate_privacy_summary import (
    compute_privacy_summary,
    _parse_privacy_tag_raw,
    _PRIVACY_SUMMARY_BUCKETS,
    _ACTIVE_STATUSES_FOR_PRIVACY,
    select_provider,
    PRIVACY_CAPABILITIES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given tasks.

    Each task is a dict: {id, body, status, assignee}.
    Mirrors the real tasks table schema (subset relevant to the census).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # remove so sqlite creates fresh

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            created_at INTEGER,
            started_at INTEGER,
            completed_at INTEGER
        )
    """)
    for task in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task.get("id", "t_test"),
                task.get("title", "Test task"),
                task.get("body", ""),
                task.get("status", "ready"),
                task.get("assignee", "pr-nanogpt"),
                0,
            ),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests: _parse_privacy_tag_raw ────────────────────────────────────────────

class TestParsePrivacyTagRaw(unittest.TestCase):

    def test_high_alias(self):
        self.assertEqual(_parse_privacy_tag_raw("privacy:high"), "high")

    def test_medium_alias(self):
        self.assertEqual(_parse_privacy_tag_raw("privacy:medium"), "medium")

    def test_low_alias(self):
        self.assertEqual(_parse_privacy_tag_raw("privacy:low"), "low")

    def test_canonical_levels(self):
        self.assertEqual(_parse_privacy_tag_raw("privacy:public"), "public")
        self.assertEqual(_parse_privacy_tag_raw("privacy:sensitive"), "sensitive")
        self.assertEqual(_parse_privacy_tag_raw("privacy:confidential"), "confidential")

    def test_case_insensitive(self):
        self.assertEqual(_parse_privacy_tag_raw("Privacy:HIGH"), "high")
        self.assertEqual(_parse_privacy_tag_raw("PRIVACY:Medium"), "medium")

    def test_quoted_value(self):
        self.assertEqual(_parse_privacy_tag_raw('privacy: "high"'), "high")

    def test_tag_in_middle_of_body(self):
        body = "some text\nprivacy: low\nmore text"
        self.assertEqual(_parse_privacy_tag_raw(body), "low")

    def test_no_tag_returns_none(self):
        self.assertIsNone(_parse_privacy_tag_raw("no tag here"))

    def test_empty_text_returns_none(self):
        self.assertIsNone(_parse_privacy_tag_raw(""))

    def test_empty_value_returns_empty_string(self):
        """'privacy:' with no value → empty string (malformed signal)."""
        self.assertEqual(_parse_privacy_tag_raw("privacy:"), "")

    def test_unrecognised_value_preserved(self):
        """Unrecognised value is returned raw so caller can warn."""
        self.assertEqual(_parse_privacy_tag_raw("privacy:unknown"), "unknown")

    def test_none_input(self):
        self.assertIsNone(_parse_privacy_tag_raw(None))

    def test_inline_midline_tag_detected(self):
        """Creator style: tags mid-line, space-delimited (e2e regression)."""
        self.assertEqual(
            _parse_privacy_tag_raw(
                "objective:OBJ-E2E privacy:high auto_created:true"),
            "high",
        )
        self.assertEqual(
            _parse_privacy_tag_raw(
                "**Objetivo**: OBJ-18. **cost:small** **privacy:medium**"),
            "medium",
        )

    def test_bold_tag_detected(self):
        """Creator bold style: **privacy:high**."""
        self.assertEqual(_parse_privacy_tag_raw("**privacy:high**"), "high")

    def test_parenthesised_tag_detected(self):
        self.assertEqual(_parse_privacy_tag_raw("(privacy:low)"), "low")

    def test_bracketed_tag_detected(self):
        self.assertEqual(_parse_privacy_tag_raw("[privacy:high]"), "high")

    def test_prose_mention_not_counted(self):
        """Prose that MENTIONS the tag format must NOT count as a tag.

        Regression: real kanban bodies say "privacy:high|medium|low" in
        task descriptions (e.g. this feature's own task body).  The '|'
        is not a valid value char nor end delimiter → no match.
        """
        self.assertIsNone(_parse_privacy_tag_raw(
            "Anadir deteccion del tag privacy:high|medium|low en bodies"))
        self.assertIsNone(_parse_privacy_tag_raw(
            "tag privacy:high|medium|low en bodies de tareas activas"))
        self.assertIsNone(_parse_privacy_tag_raw(
            "Detection of privacy:high|medium|low tags"))

    def test_tag_with_trailing_period(self):
        self.assertEqual(_parse_privacy_tag_raw("privacy:high."), "high")
        self.assertEqual(_parse_privacy_tag_raw("privacy:low, rest"), "low")

    def test_tag_end_of_string(self):
        self.assertEqual(_parse_privacy_tag_raw("work then privacy:high"), "high")

    def test_tag_preceded_by_punctuation_not_word(self):
        """privacy: glued to a word (e.g. 'myprivacy:high') is NOT a tag."""
        self.assertIsNone(_parse_privacy_tag_raw("myprivacy:high"))


# ── Tests: bucket mapping ────────────────────────────────────────────────────

class TestPrivacySummaryBuckets(unittest.TestCase):

    def test_obj18_aliases_bucket_directly(self):
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["high"], "high")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["medium"], "medium")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["low"], "low")

    def test_canonical_levels_map_to_obj18_buckets(self):
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["sensitive"], "high")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["public"], "low")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["confidential"], "high")

    def test_legacy_aliases_map_through_canonical(self):
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["pub"], "low")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["publico"], "low")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["sens"], "high")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["selectivo"], "high")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["conf"], "high")
        self.assertEqual(_PRIVACY_SUMMARY_BUCKETS["intimo"], "high")


# ── Tests: compute_privacy_summary ───────────────────────────────────────────

class TestComputePrivacySummary(unittest.TestCase):

    def test_task_without_tag_counts_as_none(self):
        """Acceptance: task sin tag → none."""
        db = _make_kanban_db([
            {"id": "t_a", "body": "**Goal**\nSome work without privacy tag", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_multiple_tasks_no_tags(self):
        db = _make_kanban_db([
            {"id": "t_a", "body": "task A no tag", "status": "ready"},
            {"id": "t_b", "body": "task B no tag", "status": "running"},
            {"id": "t_c", "body": "task C no tag", "status": "blocked"},
            {"id": "t_d", "body": "task D no tag", "status": "todo"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 4})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_recognised_tags_bucket_correctly(self):
        db = _make_kanban_db([
            {"id": "t_high", "body": "privacy:high\nwork", "status": "ready"},
            {"id": "t_med", "body": "privacy:medium\nwork", "status": "ready"},
            {"id": "t_low", "body": "privacy:low\nwork", "status": "ready"},
            {"id": "t_none", "body": "work only", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 1, "medium": 1, "low": 1, "none": 1})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_malformed_empty_tag_ignored_with_warning(self):
        """Acceptance: tag malformado (empty) → ignored + warning."""
        db = _make_kanban_db([
            {"id": "t_bad", "body": "privacy:\nwork", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            # Counted as none, warning emitted
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
            self.assertEqual(len(warnings), 1)
            self.assertIn("t_bad", warnings[0])
            self.assertIn("empty privacy: tag", warnings[0])
        finally:
            os.unlink(db)

    def test_malformed_unrecognised_tag_ignored_with_warning(self):
        """Acceptance: tag malformado (unknown value) → ignored + warning."""
        db = _make_kanban_db([
            {"id": "t_bad2", "body": "privacy:ultra\nwork", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
            self.assertEqual(len(warnings), 1)
            self.assertIn("t_bad2", warnings[0])
            self.assertIn("unrecognised", warnings[0])
            self.assertIn("privacy:ultra", warnings[0])
        finally:
            os.unlink(db)

    def test_multiprofile_counting(self):
        """Acceptance: conteo multiprofile correcto.

        Tasks assigned to DIFFERENT profiles (assignees) are all censused
        together in the same summary — the census is per-DB, not per-profile.
        Bodies use the creator's inline tag style (regression for the
        line-start-only gap found by the live e2e).
        """
        db = _make_kanban_db([
            {"id": "t_ollama_high",
             "body": "objective:OBJ-18 privacy:high auto_created:true",
             "status": "ready", "assignee": "pr-ollama"},
            {"id": "t_ollama_low",
             "body": "**Objetivo** **privacy:low** **cost:small**",
             "status": "running", "assignee": "pr-ollama"},
            {"id": "t_nanogpt_high",
             "body": "privacy:high\nwork", "status": "ready",
             "assignee": "pr-nanogpt"},
            {"id": "t_nanogpt_medium",
             "body": "objective:X **privacy:medium**", "status": "todo",
             "assignee": "pr-nanogpt"},
            {"id": "t_opencode_none",
             "body": "work without tag", "status": "blocked",
             "assignee": "pr-opencode"},
            {"id": "t_openrouter_high",
             "body": "privacy:high\nwork", "status": "ready",
             "assignee": "pr-openrouter"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            # 3 high (ollama, nanogpt, openrouter), 1 medium (nanogpt),
            # 1 low (ollama), 1 none (opencode)
            self.assertEqual(summary, {"high": 3, "medium": 1, "low": 1, "none": 1})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_canonical_tags_bucket_through_alias_map(self):
        """Canonical tags (public/sensitive/confidential) bucket via the map."""
        db = _make_kanban_db([
            {"id": "t_sens", "body": "privacy:sensitive\nwork", "status": "ready"},
            {"id": "t_pub", "body": "privacy:public\nwork", "status": "ready"},
            {"id": "t_conf", "body": "privacy:confidential\nwork", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 2, "medium": 0, "low": 1, "none": 0})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_confidential_alias_conf_buckets_as_high(self):
        """privacy:conf and privacy:intimo aliases bucket as high (OBJ-18 S2)."""
        db = _make_kanban_db([
            {"id": "t_conf_alias", "body": "privacy:conf\nwork", "status": "ready"},
            {"id": "t_intimo", "body": "privacy:intimo\nwork", "status": "todo"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 2, "medium": 0, "low": 0, "none": 0})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_terminal_tasks_not_counted(self):
        """done / archived tasks are excluded from the census."""
        db = _make_kanban_db([
            {"id": "t_done_high", "body": "privacy:high\nwork", "status": "done"},
            {"id": "t_archived_low", "body": "privacy:low\nwork", "status": "archived"},
            {"id": "t_active_high", "body": "privacy:high\nwork", "status": "ready"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            # Only the active task counts
            self.assertEqual(summary, {"high": 1, "medium": 0, "low": 0, "none": 0})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_triage_tasks_counted(self):
        """Triage tasks are pending work — included in the census."""
        db = _make_kanban_db([
            {"id": "t_triage_low", "body": "privacy:low\nidea", "status": "triage"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 1, "none": 0})
        finally:
            os.unlink(db)

    def test_empty_db_all_zero(self):
        db = _make_kanban_db([])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 0})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)

    def test_missing_db_returns_zeroed_summary(self):
        """No kanban.db → zeroed summary, no error, no warning."""
        warnings = []
        summary = compute_privacy_summary("/nonexistent/path/kanban.db",
                                          warnings=warnings)
        self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 0})
        self.assertEqual(warnings, [])

    def test_corrupt_db_fails_open_with_warning(self):
        """DB read error → zeroed summary + warning (fail open, never breaks)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        with os.fdopen(fd, "w") as f:
            f.write("THIS IS NOT A SQLITE DATABASE")
        try:
            warnings = []
            summary = compute_privacy_summary(path, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 0})
            self.assertEqual(len(warnings), 1)
            self.assertIn("kanban.db read failed", warnings[0])
        finally:
            os.unlink(path)

    def test_null_body_counts_as_none(self):
        """NULL body → no tag → none (no crash)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
                assignee TEXT, created_at INTEGER,
                started_at INTEGER, completed_at INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO tasks (id, body, status) VALUES (?, ?, ?)",
            ("t_null", None, "ready"),
        )
        conn.commit()
        conn.close()
        try:
            warnings = []
            summary = compute_privacy_summary(path, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(path)

    def test_default_path_resolution_env_var(self):
        """compute_privacy_summary() with no args uses HERMES_KANBAN_DB env."""
        db = _make_kanban_db([
            {"id": "t_env", "body": "privacy:high\nwork", "status": "ready"},
        ])
        old = os.environ.get("HERMES_KANBAN_DB")
        os.environ["HERMES_KANBAN_DB"] = db
        try:
            warnings = []
            summary = compute_privacy_summary(warnings=warnings)
            self.assertEqual(summary, {"high": 1, "medium": 0, "low": 0, "none": 0})
        finally:
            if old is None:
                os.environ.pop("HERMES_KANBAN_DB", None)
            else:
                os.environ["HERMES_KANBAN_DB"] = old
            os.unlink(db)

    def test_warnings_arg_optional(self):
        """warnings=None works — no crash, warnings dropped."""
        db = _make_kanban_db([
            {"id": "t_bad", "body": "privacy:junk\nwork", "status": "ready"},
        ])
        try:
            summary = compute_privacy_summary(db)  # no warnings arg
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
        finally:
            os.unlink(db)

    def test_real_task_body_prose_mention_counts_as_none(self):
        """This feature's own task body (prose 'privacy:high|medium|low')
        must NOT be counted as a privacy tag."""
        real_body = (
            "**Objetivo**: OBJ-18 (fase 2, privacidad). **auto_created:true** "
            "**cost:small** **model:fast**\n\n"
            "**Contexto**: La matriz de routing esta documentada en "
            "~/.hermes/profiles/pr-ollama/docs/privacy-routing-matrix.md "
            "(y privacy-by-provider-design.md). El quota-gate actual NO "
            "considera privacidad: el snapshot emite privacy_level: none y "
            "el creator asigna solo por cuota.\n\n"
            "**Trabajo**:\n1. Localizar el script del gate (sistema "
            "quota-governor en ~/git/) y leer como parsea los bodies de "
            "tareas kanban.\n2. Anadir deteccion del tag privacy:high|medium|low "
            "en bodies de tareas activas (kanban.db) y exponer un campo "
            "privacy_summary en el JSON del snapshot\n"
        )
        db = _make_kanban_db([
            {"id": "t_selfref", "body": real_body, "status": "running"},
        ])
        try:
            warnings = []
            summary = compute_privacy_summary(db, warnings=warnings)
            self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 1})
            self.assertEqual(warnings, [])
        finally:
            os.unlink(db)


# ── Tests: routing invariants (S1 must NOT change routing) ────────────────────

class TestRoutingUnchanged(unittest.TestCase):
    """S1 is read-only: privacy_summary must not affect select_provider.

    The summary is computed AFTER select_provider in main() and its result
    is only embedded in the output context.  These tests pin the existing
    routing behaviour so any accidental coupling fails loudly.
    """

    MOCK_PROVIDERS = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 80.0, "bottleneck_pct": 20.0,
         "bottleneck_window": "session", "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt",
         "model": "zai-org/glm-5.2", "availability": 60.0,
         "bottleneck_pct": 40.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]

    def test_summary_does_not_feed_select_provider(self):
        """select_provider signature has no summary param; routing identical
        regardless of what the summary would be (it is not an input)."""
        warnings = []
        summary = compute_privacy_summary("/nonexistent/kanban.db",
                                          warnings=warnings)
        # The summary is pure output — select_provider does not take it.
        result = select_provider(self.MOCK_PROVIDERS, privacy_level=None)
        self.assertEqual(result["profile"], "pr-ollama")
        self.assertEqual(summary, {"high": 0, "medium": 0, "low": 0, "none": 0})

    def test_baseline_routing_no_privacy(self):
        """Availability-first: ollama 80% wins over nanogpt 60%."""
        result = select_provider(self.MOCK_PROVIDERS, privacy_level=None)
        self.assertEqual(result["profile"], "pr-ollama")

    def test_baseline_routing_sensitive(self):
        """Sensitive → preference-first: nanogpt wins regardless of quota."""
        result = select_provider(self.MOCK_PROVIDERS, privacy_level="sensitive")
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_confidential_routing_no_cloud_provider(self):
        """Confidential → no cloud provider qualifies, returns None (OBJ-18 S2)."""
        result = select_provider(self.MOCK_PROVIDERS, privacy_level="confidential")
        self.assertIsNone(result)

    def test_confidential_excludes_all_cloud_in_capabilities(self):
        """PRIVACY_CAPABILITIES['confidential'] must only contain custom."""
        self.assertEqual(
            PRIVACY_CAPABILITIES["confidential"], {"custom"}
        )

    def test_active_statuses_cover_pending_work(self):
        """All non-terminal statuses are censused; done/archived are not."""
        self.assertIn("ready", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertIn("running", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertIn("blocked", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertIn("todo", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertIn("triage", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertNotIn("done", _ACTIVE_STATUSES_FOR_PRIVACY)
        self.assertNotIn("archived", _ACTIVE_STATUSES_FOR_PRIVACY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
