#!/usr/bin/env python3
"""Tests for validate-guardrails.py — OBJ-17 guardrails validator.

Tests cover all 11 guardrails (GR1-GR11) plus edge cases:
  - Clean proposals that should pass
  - Proposals that violate each individual guardrail
  - Proposals that trigger warnings (require human approval)
  - Dynamic checks (GR1: active objectives, GR6: daily limit)
  - Combined checks (multiple violations)

Run:
  python3 -m pytest test_validate_guardrails.py -v
  python3 test_validate_guardrails.py  # direct run
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with filename-based module (validate-guardrails.py → validate_guardrails)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "validate_guardrails",
    os.path.join(SCRIPT_DIR, "scripts", "validate-guardrails.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["validate_guardrails"] = _mod
_spec.loader.exec_module(_mod)

from validate_guardrails import (
    GuardrailResult,
    Violation,
    Warning_,
    REPOS_ROOT,
    PLUGIN_REPO,
    check_max_active_objectives,
    check_file_scope,
    check_system_files,
    check_credentials,
    check_daily_proposal_limit,
    check_config_yaml,
    check_triage_only,
    check_package_install,
    check_other_repos,
    check_os_files,
    validate_objective,
    record_proposal,
)


def _make_kanban_db(tasks: list) -> str:
    """Create a temp kanban.db with the given tasks.

    Each task is a dict: {body, status}
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
    for i, task in enumerate(tasks):
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"t_test{i}", f"Task {i}", task["body"], task["status"], "test", 0),
        )
    conn.commit()
    conn.close()
    return path


def _make_state_file(entries: list) -> str:
    """Create a temp state file with the given entries (JSONL)."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


# ===========================================================================
# GR1: Max active objectives
# ===========================================================================

class TestGR1MaxActiveObjectives(unittest.TestCase):

    def test_no_db_returns_allowed(self):
        """If kanban.db doesn't exist, fail open (allowed)."""
        result = check_max_active_objectives("/nonexistent/path.db")
        self.assertTrue(result.allowed)
        self.assertEqual(result.violations, [])

    def test_few_active_objectives_allowed(self):
        """3 active objectives should be allowed."""
        tasks = [
            {"body": "objective:OBJ-1", "status": "running"},
            {"body": "objective:OBJ-2", "status": "ready"},
            {"body": "objective:OBJ-3", "status": "blocked"},
        ]
        db = _make_kanban_db(tasks)
        try:
            result = check_max_active_objectives(db)
            self.assertTrue(result.allowed)
            self.assertEqual(result.violations, [])
        finally:
            os.unlink(db)

    def test_five_active_objectives_blocked(self):
        """5 active objectives should be blocked (limit is 5)."""
        tasks = [
            {"body": "objective:OBJ-1", "status": "running"},
            {"body": "objective:OBJ-2", "status": "ready"},
            {"body": "objective:OBJ-3", "status": "blocked"},
            {"body": "objective:OBJ-4", "status": "ready"},
            {"body": "objective:OBJ-5", "status": "running"},
        ]
        db = _make_kanban_db(tasks)
        try:
            result = check_max_active_objectives(db)
            self.assertFalse(result.allowed)
            self.assertEqual(len(result.violations), 1)
            self.assertEqual(result.violations[0].id, "GR1")
        finally:
            os.unlink(db)

    def test_done_tasks_not_counted(self):
        """Tasks in done/archived status should not count as active."""
        tasks = [
            {"body": "objective:OBJ-1", "status": "done"},
            {"body": "objective:OBJ-2", "status": "running"},
        ]
        db = _make_kanban_db(tasks)
        try:
            result = check_max_active_objectives(db)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(db)

    def test_same_objective_multiple_tasks_counts_once(self):
        """Multiple tasks for the same objective count as 1."""
        tasks = [
            {"body": "objective:OBJ-1 subtask A", "status": "running"},
            {"body": "objective:OBJ-1 subtask B", "status": "ready"},
            {"body": "objective:OBJ-1 subtask C", "status": "blocked"},
        ]
        db = _make_kanban_db(tasks)
        try:
            result = check_max_active_objectives(db)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(db)


# ===========================================================================
# GR2/GR3/GR11: File scope
# ===========================================================================

class TestGR2GR3FileScope(unittest.TestCase):

    def test_plugin_repo_path_allowed(self):
        """Paths inside the plugin repo should be allowed."""
        text = f"Refactor {PLUGIN_REPO}/health_checks.py"
        result = check_file_scope(text)
        self.assertTrue(result.allowed)

    def test_hermes_path_allowed(self):
        """Paths inside ~/.hermes/ should be allowed."""
        text = "Update ~/.hermes/scripts/daily-report.py"
        result = check_file_scope(text)
        self.assertTrue(result.allowed)

    def test_outside_path_blocked(self):
        """Paths outside allowed dirs should be blocked."""
        text = "Modify /home/user/projects/other-repo/main.py"
        result = check_file_scope(text)
        self.assertFalse(result.allowed)
        self.assertTrue(any(v.id in ("GR2", "GR4") for v in result.violations))

    def test_no_paths_allowed(self):
        """Text without any file paths should be allowed."""
        text = "Improve documentation and refactor health checks"
        result = check_file_scope(text)
        self.assertTrue(result.allowed)


# ===========================================================================
# GR4: System files
# ===========================================================================

class TestGR4SystemFiles(unittest.TestCase):

    def test_env_file_blocked(self):
        text = "Read ~/.env for configuration"
        result = check_system_files(text)
        self.assertFalse(result.allowed)

    def test_ssh_config_blocked(self):
        text = "Modify .ssh/config to add host"
        result = check_system_files(text)
        self.assertFalse(result.allowed)

    def test_etc_path_blocked(self):
        text = "Update /etc/hostname"
        result = check_system_files(text)
        self.assertFalse(result.allowed)

    def test_clean_text_allowed(self):
        text = "Refactor health_checks.py in the plugin repo"
        result = check_system_files(text)
        self.assertTrue(result.allowed)


# ===========================================================================
# GR5: Credentials
# ===========================================================================

class TestGR5Credentials(unittest.TestCase):

    def test_api_key_triggers_warning(self):
        text = "Add a new API key for the service"
        result = check_credentials(text)
        self.assertTrue(result.allowed)  # Warning, not violation
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].id, "GR5")

    def test_token_triggers_warning(self):
        text = "Generate a token for authentication"
        result = check_credentials(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)

    def test_password_triggers_warning(self):
        text = "Set the password for the database"
        result = check_credentials(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)

    def test_clean_text_no_warning(self):
        text = "Refactor health_checks.py"
        result = check_credentials(text)
        self.assertEqual(result.warnings, [])

    def test_multiple_credential_words_one_warning(self):
        """Multiple credential words should produce only 1 warning."""
        text = "Add API key, token, and password for the service"
        result = check_credentials(text)
        self.assertEqual(len(result.warnings), 1)


# ===========================================================================
# GR6: Daily proposal limit
# ===========================================================================

class TestGR6DailyProposalLimit(unittest.TestCase):

    def test_no_state_file_allowed(self):
        result = check_daily_proposal_limit("/nonexistent/path.jsonl")
        self.assertTrue(result.allowed)

    def test_proposal_today_blocked(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [{"date": today, "allowed": True, "title": "OBJ-20"}]
        state = _make_state_file(entries)
        try:
            result = check_daily_proposal_limit(state)
            self.assertFalse(result.allowed)
            self.assertEqual(result.violations[0].id, "GR6")
        finally:
            os.unlink(state)

    def test_proposal_yesterday_allowed(self):
        yesterday = "2020-01-01"  # Far in the past
        entries = [{"date": yesterday, "allowed": True, "title": "OBJ-20"}]
        state = _make_state_file(entries)
        try:
            result = check_daily_proposal_limit(state)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(state)

    def test_rejected_proposal_not_counted(self):
        """A rejected (allowed=false) proposal doesn't count toward the daily limit."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [{"date": today, "allowed": False, "title": "OBJ-20"}]
        state = _make_state_file(entries)
        try:
            result = check_daily_proposal_limit(state)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(state)


# ===========================================================================
# GR7: config.yaml
# ===========================================================================

class TestGR7ConfigYaml(unittest.TestCase):

    def test_config_yaml_triggers_warning(self):
        text = "Modify config.yaml to add new settings"
        result = check_config_yaml(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].id, "GR7")

    def test_config_yml_triggers_warning(self):
        text = "Update config.yml with new options"
        result = check_config_yaml(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)

    def test_clean_text_no_warning(self):
        text = "Refactor the quota planner"
        result = check_config_yaml(text)
        self.assertEqual(result.warnings, [])


# ===========================================================================
# GR8: Triage-only (no-op)
# ===========================================================================

class TestGR8TriageOnly(unittest.TestCase):

    def test_always_passes(self):
        result = check_triage_only()
        self.assertTrue(result.allowed)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.warnings, [])


# ===========================================================================
# GR9: Package install
# ===========================================================================

class TestGR9PackageInstall(unittest.TestCase):

    def test_pip_install_triggers_warning(self):
        text = "Run pip install requests to add dependency"
        result = check_package_install(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].id, "GR9")

    def test_apt_install_triggers_warning(self):
        text = "Run apt install curl"
        result = check_package_install(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)

    def test_npm_install_triggers_warning(self):
        text = "Run npm install express"
        result = check_package_install(text)
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.warnings), 1)

    def test_clean_text_no_warning(self):
        text = "Refactor health_checks.py"
        result = check_package_install(text)
        self.assertEqual(result.warnings, [])


# ===========================================================================
# GR10: Other repos
# ===========================================================================

class TestGR10OtherRepos(unittest.TestCase):

    def test_other_repo_blocked(self):
        text = f"Modify {REPOS_ROOT}/other-project/main.py"
        result = check_other_repos(text)
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].id, "GR10")

    def test_plugin_repo_allowed(self):
        text = f"Modify {REPOS_ROOT}/hermes-plugin-quota-governor/health_checks.py"
        result = check_other_repos(text)
        self.assertTrue(result.allowed)

    def test_no_repo_mention_allowed(self):
        text = "Refactor health_checks.py"
        result = check_other_repos(text)
        self.assertTrue(result.allowed)


# ===========================================================================
# GR11: OS files
# ===========================================================================

class TestGR11OSFiles(unittest.TestCase):

    def test_etc_path_blocked(self):
        text = "Modify /etc/hosts"
        result = check_os_files(text)
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].id, "GR11")

    def test_var_path_blocked(self):
        text = "Write to /var/log/custom.log"
        result = check_os_files(text)
        self.assertFalse(result.allowed)

    def test_proc_path_blocked(self):
        text = "Read /proc/cpuinfo"
        result = check_os_files(text)
        self.assertFalse(result.allowed)

    def test_hermes_path_allowed(self):
        text = "Update ~/.hermes/scripts/test.py"
        result = check_os_files(text)
        self.assertTrue(result.allowed)


# ===========================================================================
# Integration: validate_objective (all checks combined)
# ===========================================================================

class TestValidateObjective(unittest.TestCase):

    def test_clean_proposal_allowed(self):
        """A clean proposal touching only plugin files should pass."""
        result = validate_objective(
            title="OBJ-20: Optimizar health checks",
            body="Refactorizar health_checks.py en el plugin repo para reducir falsos positivos",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.warnings, [])
        self.assertFalse(result.requires_human_approval)

    def test_system_file_violation_blocked(self):
        result = validate_objective(
            title="OBJ-21: System config",
            body="Modificar /etc/hosts para resolver DNS",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertFalse(result.allowed)

    def test_credential_warning_not_blocked(self):
        result = validate_objective(
            title="OBJ-22: New integration",
            body="Add API key for external service integration",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_human_approval)

    def test_multiple_violations(self):
        result = validate_objective(
            title="OBJ-23: Bad proposal",
            body=f"Modify /etc/hosts, {REPOS_ROOT}/other/file.py, and install packages with pip install requests",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertFalse(result.allowed)
        self.assertGreater(len(result.violations), 1)

    def test_config_yaml_warning(self):
        result = validate_objective(
            title="OBJ-24: Config update",
            body="Modificar config.yaml para añadir nuevo provider",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_human_approval)

    def test_package_install_warning(self):
        result = validate_objective(
            title="OBJ-25: New dependency",
            body="Run pip install requests to add HTTP client",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.requires_human_approval)

    def test_combined_warnings(self):
        """Multiple warnings should all be captured."""
        result = validate_objective(
            title="OBJ-26: Complex proposal",
            body="Modify config.yaml and add API key and pip install requests",
            kanban_db="/nonexistent.db",
            state_file="/nonexistent.jsonl",
        )
        self.assertTrue(result.allowed)
        self.assertGreaterEqual(len(result.warnings), 2)  # At least GR5 + GR7 or GR9


# ===========================================================================
# Record proposal
# ===========================================================================

class TestRecordProposal(unittest.TestCase):

    def test_record_writes_jsonl(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(path)  # remove so it's created fresh
        try:
            result = GuardrailResult(allowed=True)
            record_proposal("OBJ-20: Test", result, path, task_id="t_123")
            with open(path) as f:
                entry = json.loads(f.readline())
            self.assertEqual(entry["title"], "OBJ-20: Test")
            self.assertTrue(entry["allowed"])
            self.assertEqual(entry["task_id"], "t_123")
            self.assertIn("timestamp", entry)
            self.assertIn("date", entry)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_record_rejected_proposal(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(path)
        try:
            result = GuardrailResult(allowed=False)
            result.violations.append(Violation("GR1", "too many objectives"))
            record_proposal("OBJ-21: Rejected", result, path)
            with open(path) as f:
                entry = json.loads(f.readline())
            self.assertFalse(entry["allowed"])
            self.assertEqual(len(entry["violations"]), 1)
            self.assertEqual(entry["violations"][0]["id"], "GR1")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    # ── OBJ-16 regression: validate-guardrails dedup ───────────────────────

    def test_record_same_title_same_day_dedup(self):
        """Recording the same proposal title twice on the same day writes
        only ONE entry. This is the core OBJ-16 RC2 regression test:
        the autonomous-task-creator LLM cron calls validate-guardrails.py
        --record every 30m with the same title, and without dedup it
        spams the proposals file with duplicates."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(path)
        try:
            result = GuardrailResult(allowed=False)
            result.violations.append(Violation("GR6", "Daily limit"))
            title = "OBJ-21: Fix proposer dedup bug"
            record_proposal(title, result, path)
            record_proposal(title, result, path)
            record_proposal(title, result, path)
            with open(path) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1,
                "Same title same day must record only once (dedup)")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_record_different_titles_not_deduped(self):
        """Different titles on the same day are both recorded (no false dedup)."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(path)
        try:
            result = GuardrailResult(allowed=True)
            record_proposal("OBJ-30: Task A", result, path)
            record_proposal("OBJ-31: Task B", result, path)
            with open(path) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 2,
                "Different titles must both be recorded")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_record_includes_pattern_kind(self):
        """record_proposal now includes pattern_kind and pattern_key fields
        so cross-path dedup with objective-proposer.py works."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(path)
        try:
            result = GuardrailResult(allowed=True)
            record_proposal("OBJ-30: Add test coverage for scripts", result, path)
            with open(path) as f:
                entry = json.loads(f.readline())
            self.assertIn("pattern_kind", entry,
                "record_proposal must include pattern_kind for cross-path dedup")
            self.assertIn("pattern_key", entry,
                "record_proposal must include pattern_key for cross-path dedup")
            self.assertTrue(entry["pattern_key"].startswith(entry["pattern_kind"] + ":"),
                "pattern_key must start with pattern_kind:")
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ===========================================================================
# GuardrailResult
# ===========================================================================

class TestGuardrailResult(unittest.TestCase):

    def test_requires_human_approval_with_warnings(self):
        r = GuardrailResult()
        r.warnings.append(Warning_("GR5", "test"))
        self.assertTrue(r.requires_human_approval)

    def test_requires_human_approval_without_warnings(self):
        r = GuardrailResult()
        self.assertFalse(r.requires_human_approval)

    def test_allowed_no_violations(self):
        r = GuardrailResult()
        self.assertTrue(r.allowed)

    def test_not_allowed_with_violations(self):
        r = GuardrailResult()
        r.violations.append(Violation("GR1", "test"))
        r.allowed = False
        self.assertFalse(r.allowed)

    def test_to_dict_structure(self):
        r = GuardrailResult(allowed=False)
        r.violations.append(Violation("GR1", "test violation"))
        r.warnings.append(Warning_("GR5", "test warning"))
        d = r.to_dict()
        self.assertIn("allowed", d)
        self.assertIn("violations", d)
        self.assertIn("warnings", d)
        self.assertIn("requires_human_approval", d)
        self.assertEqual(d["violations"][0]["id"], "GR1")
        self.assertEqual(d["warnings"][0]["id"], "GR5")


if __name__ == "__main__":
    unittest.main()