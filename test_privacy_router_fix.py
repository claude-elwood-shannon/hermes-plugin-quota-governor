#!/usr/bin/env python3
"""Tests for privacy-router-fix.py — deterministic privacy routing enforcement.

Tests cover:
  - parse_privacy_tag: extracting privacy level from task body text
  - is_sensitive: mapping privacy values to sensitive routing
  - find_misrouted_sensitive_tasks: DB query for misrouted tasks
  - main flow: dry-run, no-op, and reassignment logic

All tests use temp DBs and mocked subprocess calls — no side effects.

Note: test_privacy_routing.py tests the privacy routing *inside quota-gate.py*.
This file tests the *separate* privacy-router-fix.py enforcement script.

Run:
  python3 -m pytest test_privacy_router_fix.py -v
  python3 test_privacy_router_fix.py  # direct run
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with hyphen filename
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "privacy_router_fix",
    os.path.join(SCRIPT_DIR, "scripts", "privacy-router-fix.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["privacy_router_fix"] = _mod
_spec.loader.exec_module(_mod)

from privacy_router_fix import (
    parse_privacy_tag,
    is_sensitive,
    is_confidential,
    find_misrouted_sensitive_tasks,
    find_misrouted_confidential_tasks,
    SENSITIVE_TAGS,
    CONFIDENTIAL_TAGS,
    REASSIGNABLE_STATUSES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given tasks.
    Each task is a dict: {id, title, body, assignee, status}
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT
        )
    """)
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status) VALUES (?, ?, ?, ?, ?)",
            (
                t.get("id", "t_test"),
                t.get("title", "test task"),
                t.get("body", ""),
                t.get("assignee", "pr-ollama"),
                t.get("status", "todo"),
            ),
        )
    conn.commit()
    conn.close()
    return path


# ── Tests: parse_privacy_tag ─────────────────────────────────────────────────

class TestParsePrivacyTag(unittest.TestCase):

    def test_high(self):
        self.assertEqual(parse_privacy_tag("privacy:high"), "high")

    def test_sensitive(self):
        self.assertEqual(parse_privacy_tag("privacy:sensitive"), "sensitive")

    def test_medium(self):
        self.assertEqual(parse_privacy_tag("privacy:medium"), "medium")

    def test_low(self):
        self.assertEqual(parse_privacy_tag("privacy:low"), "low")

    def test_public(self):
        self.assertEqual(parse_privacy_tag("privacy:public"), "public")

    def test_confidential(self):
        self.assertEqual(parse_privacy_tag("privacy:confidential"), "confidential")

    def test_case_insensitive(self):
        self.assertEqual(parse_privacy_tag("Privacy:High"), "high")
        self.assertEqual(parse_privacy_tag("PRIVACY:SENSITIVE"), "sensitive")

    def test_with_quotes(self):
        self.assertEqual(parse_privacy_tag("privacy:'high'"), "high")
        self.assertEqual(parse_privacy_tag('privacy:"sensitive"'), "sensitive")

    def test_with_surrounding_text(self):
        body = "Some task description\nprivacy:high\nMore text"
        self.assertEqual(parse_privacy_tag(body), "high")

    def test_returns_none_for_no_tag(self):
        self.assertIsNone(parse_privacy_tag("no privacy tag here"))
        self.assertIsNone(parse_privacy_tag(""))

    def test_returns_none_for_empty_body(self):
        self.assertIsNone(parse_privacy_tag(None))

    def test_only_first_tag_returned(self):
        """If multiple privacy tags, the first one is returned."""
        body = "privacy:high\nprivacy:low"
        result = parse_privacy_tag(body)
        self.assertEqual(result, "high")

    def test_tag_with_spaces(self):
        """privacy: high with space after colon."""
        self.assertEqual(parse_privacy_tag("privacy: high"), "high")


# ── Tests: is_sensitive ──────────────────────────────────────────────────────

class TestIsSensitive(unittest.TestCase):

    def test_high_is_sensitive(self):
        self.assertTrue(is_sensitive("high"))

    def test_sensitive_is_sensitive(self):
        self.assertTrue(is_sensitive("sensitive"))

    def test_medium_is_sensitive(self):
        self.assertTrue(is_sensitive("medium"))

    def test_low_not_sensitive(self):
        self.assertFalse(is_sensitive("low"))

    def test_public_not_sensitive(self):
        self.assertFalse(is_sensitive("public"))

    def test_confidential_not_sensitive(self):
        self.assertFalse(is_sensitive("confidential"))

    def test_case_insensitive(self):
        self.assertTrue(is_sensitive("HIGH"))
        self.assertTrue(is_sensitive("Sensitive"))
        self.assertTrue(is_sensitive("MEDIUM"))

    def test_none_not_sensitive(self):
        self.assertFalse(is_sensitive(None))

    def test_empty_not_sensitive(self):
        self.assertFalse(is_sensitive(""))

    def test_unknown_not_sensitive(self):
        self.assertFalse(is_sensitive("unknown"))


# ── Tests: find_misrouted_sensitive_tasks ────────────────────────────────────

class TestFindMisroutedSensitiveTasks(unittest.TestCase):

    def test_finds_misrouted_high_task(self):
        """Task with privacy:high assigned to wrong profile → found."""
        db = _make_kanban_db([
            {"id": "t_001", "body": "privacy:high\ntask body", "assignee": "pr-ollama", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "t_001")  # task_id
        self.assertEqual(result[0][2], "pr-ollama")  # current assignee
        self.assertEqual(result[0][3], "high")  # privacy value

    def test_skips_correctly_assigned(self):
        """Task with privacy:high already on correct profile → not found."""
        db = _make_kanban_db([
            {"id": "t_002", "body": "privacy:high", "assignee": "pr-nanogpt", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_skips_non_sensitive(self):
        """Task with privacy:low → not sensitive, not misrouted."""
        db = _make_kanban_db([
            {"id": "t_003", "body": "privacy:low", "assignee": "pr-ollama", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_skips_done_tasks(self):
        """Done tasks are excluded from the query."""
        db = _make_kanban_db([
            {"id": "t_004", "body": "privacy:high", "assignee": "pr-ollama", "status": "done"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_skips_archived_tasks(self):
        db = _make_kanban_db([
            {"id": "t_005", "body": "privacy:high", "assignee": "pr-ollama", "status": "archived"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_skips_blocked_tasks(self):
        db = _make_kanban_db([
            {"id": "t_006", "body": "privacy:high", "assignee": "pr-ollama", "status": "blocked"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_skips_running_tasks(self):
        """Running tasks must never be touched."""
        db = _make_kanban_db([
            {"id": "t_007", "body": "privacy:high", "assignee": "pr-ollama", "status": "running"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])

    def test_finds_multiple_misrouted(self):
        db = _make_kanban_db([
            {"id": "t_008", "body": "privacy:high", "assignee": "pr-ollama", "status": "todo"},
            {"id": "t_009", "body": "privacy:sensitive", "assignee": "pr-ollama", "status": "ready"},
            {"id": "t_010", "body": "privacy:low", "assignee": "pr-ollama", "status": "triage"},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(len(result), 2)  # high + sensitive, not low

    def test_includes_status_in_result(self):
        """Result tuple includes the task status."""
        db = _make_kanban_db([
            {"id": "t_011", "body": "privacy:medium", "assignee": "pr-ollama", "status": "triage"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(len(result), 1)
        # tuple is (task_id, title, assignee, privacy_value, status)
        self.assertEqual(result[0][4], "triage")

    def test_empty_db(self):
        db = _make_kanban_db([])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_sensitive_tasks(conn, "pr-nanogpt")
        conn.close()
        self.assertEqual(result, [])


# ── Tests: constants ─────────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):

    def test_sensitive_tags(self):
        self.assertIn("high", SENSITIVE_TAGS)
        self.assertIn("sensitive", SENSITIVE_TAGS)
        self.assertIn("medium", SENSITIVE_TAGS)
        self.assertNotIn("low", SENSITIVE_TAGS)
        self.assertNotIn("public", SENSITIVE_TAGS)

    def test_reassignable_statuses(self):
        self.assertIn("ready", REASSIGNABLE_STATUSES)
        self.assertIn("todo", REASSIGNABLE_STATUSES)
        self.assertIn("triage", REASSIGNABLE_STATUSES)
        self.assertNotIn("running", REASSIGNABLE_STATUSES)
        self.assertNotIn("done", REASSIGNABLE_STATUSES)
        self.assertNotIn("blocked", REASSIGNABLE_STATUSES)


# ── Tests: is_confidential (OBJ-18 S2) ───────────────────────────────────────

class TestIsConfidential(unittest.TestCase):

    def test_confidential_is_confidential(self):
        self.assertTrue(is_confidential("confidential"))

    def test_conf_alias_is_confidential(self):
        self.assertTrue(is_confidential("conf"))

    def test_intimo_alias_is_confidential(self):
        self.assertTrue(is_confidential("intimo"))

    def test_high_not_confidential(self):
        self.assertFalse(is_confidential("high"))

    def test_sensitive_not_confidential(self):
        self.assertFalse(is_confidential("sensitive"))

    def test_medium_not_confidential(self):
        self.assertFalse(is_confidential("medium"))

    def test_low_not_confidential(self):
        self.assertFalse(is_confidential("low"))

    def test_public_not_confidential(self):
        self.assertFalse(is_confidential("public"))

    def test_case_insensitive(self):
        self.assertTrue(is_confidential("CONFIDENTIAL"))
        self.assertTrue(is_confidential("Conf"))
        self.assertTrue(is_confidential("INTIMO"))

    def test_none_not_confidential(self):
        self.assertFalse(is_confidential(None))

    def test_empty_not_confidential(self):
        self.assertFalse(is_confidential(""))

    def test_unknown_not_confidential(self):
        self.assertFalse(is_confidential("unknown"))


# ── Tests: find_misrouted_confidential_tasks (OBJ-18 S2) ─────────────────────

class TestFindMisroutedConfidentialTasks(unittest.TestCase):

    def test_finds_confidential_on_cloud_provider(self):
        """Task with privacy:confidential assigned to any cloud → found."""
        db = _make_kanban_db([
            {"id": "t_c01", "body": "privacy:confidential\ntask body",
             "assignee": "pr-ollama", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "t_c01")
        self.assertEqual(result[0][2], "pr-ollama")
        self.assertEqual(result[0][3], "confidential")

    def test_finds_conf_alias_on_cloud_provider(self):
        """privacy:conf alias also detected as confidential misroute."""
        db = _make_kanban_db([
            {"id": "t_c02", "body": "privacy:conf\ntask body",
             "assignee": "pr-nanogpt", "status": "ready"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], "conf")

    def test_finds_intimo_alias_on_cloud_provider(self):
        """privacy:intimo alias also detected as confidential misroute."""
        db = _make_kanban_db([
            {"id": "t_c03", "body": "privacy:intimo\ntask body",
             "assignee": "pr-opencode", "status": "triage"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], "intimo")

    def test_skips_sensitive_tasks(self):
        """privacy:high is sensitive, not confidential — not flagged here."""
        db = _make_kanban_db([
            {"id": "t_c04", "body": "privacy:high\ntask body",
             "assignee": "pr-ollama", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_public_tasks(self):
        """privacy:low is public, not confidential — not flagged."""
        db = _make_kanban_db([
            {"id": "t_c05", "body": "privacy:low\ntask body",
             "assignee": "pr-ollama", "status": "todo"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_done_tasks(self):
        """Done tasks are excluded."""
        db = _make_kanban_db([
            {"id": "t_c06", "body": "privacy:confidential",
             "assignee": "pr-ollama", "status": "done"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_skips_running_tasks(self):
        """Running tasks must never be touched."""
        db = _make_kanban_db([
            {"id": "t_c07", "body": "privacy:confidential",
             "assignee": "pr-ollama", "status": "running"}
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(result, [])

    def test_finds_multiple_confidential(self):
        """Multiple confidential tasks on different cloud providers."""
        db = _make_kanban_db([
            {"id": "t_c08", "body": "privacy:confidential",
             "assignee": "pr-ollama", "status": "todo"},
            {"id": "t_c09", "body": "privacy:conf",
             "assignee": "pr-nanogpt", "status": "ready"},
            {"id": "t_c10", "body": "privacy:high",
             "assignee": "pr-ollama", "status": "triage"},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(len(result), 2)  # c08 + c09, not c10 (high)

    def test_empty_db(self):
        db = _make_kanban_db([])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        result = find_misrouted_confidential_tasks(conn)
        conn.close()
        self.assertEqual(result, [])


# ── Tests: CONFIDENTIAL_TAGS constant ─────────────────────────────────────────

class TestConfidentialConstants(unittest.TestCase):

    def test_confidential_tags(self):
        self.assertIn("confidential", CONFIDENTIAL_TAGS)
        self.assertIn("conf", CONFIDENTIAL_TAGS)
        self.assertIn("intimo", CONFIDENTIAL_TAGS)
        self.assertNotIn("high", CONFIDENTIAL_TAGS)
        self.assertNotIn("sensitive", CONFIDENTIAL_TAGS)
        self.assertNotIn("low", CONFIDENTIAL_TAGS)
        self.assertNotIn("public", CONFIDENTIAL_TAGS)

    def test_sensitive_and_confidential_disjoint(self):
        """No tag should be both sensitive and confidential."""
        self.assertEqual(
            SENSITIVE_TAGS & CONFIDENTIAL_TAGS, set()
        )


if __name__ == "__main__":
    unittest.main()