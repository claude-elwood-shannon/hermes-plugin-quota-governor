#!/usr/bin/env python3
"""validate-guardrails.py — OBJ-17 guardrails validator for autonomous objectives.

Validates a proposed objective against the 11 guardrails (GR1-GR11)
before it can be created as a kanban task. Designed to be called by the
autonomous-task-creator cron job (OBJ-16) or manually.

Usage:
  python3 validate-guardrails.py \
    --title "OBJ-20: Optimizar health checks" \
    --body "Refactorizar health_checks.py para reducir falsos positivos..." \
    --kanban-db ~/.hermes/kanban.db \
    --state-file ~/.hermes/quota-governor/objective-proposals.jsonl

Output (JSON on stdout):
  {
    "allowed": true|false,
    "violations": [{"id": "GR1", "message": "..."}],
    "warnings": [{"id": "GR7", "message": "..."}],
    "requires_human_approval": true|false
  }

Exit codes:
  0 — all guardrails passed (allowed: true)
  1 — one or more guardrails violated (allowed: false)

References:
  - docs/guardrails-autonomous-objectives.md (design doc)
  - autonomous-objectives.md §OBJ-17
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_REPO = "REPO"
HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))

MAX_ACTIVE_OBJECTIVES = 5
DAILY_PROPOSAL_LIMIT = 1

# Paths the governor is ALLOWED to touch
# HERMES_HOME may be profile-specific (~/.hermes/profiles/pr-ollama) but
# the governor also needs access to ~/.hermes/ root (scripts, logs, etc.)
_HERMES_ROOT = str(Path.home() / ".hermes")
ALLOWED_PATH_PREFIXES = [
    PLUGIN_REPO,
    HERMES_HOME,
    _HERMES_ROOT,
]

# Paths the governor is NEVER allowed to touch
SYSTEM_FILE_BLACKLIST = [
    ".env",
    ".ssh/config",
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    "/etc/",
    "/var/",
    "/proc/",
    "/sys/",
    "/usr/",
    "/boot/",
    "/dev/",
    "/run/",
    "/lib/",
    "/lib64/",
    "/sbin/",
    "/bin/",
]

# Other repos in ~/git/ (not the plugin repo)
OTHER_REPOS_PATTERN = re.compile(
    r"~/git/(?!hermes-plugin-quota-governor)[\w\-./]+",
    re.IGNORECASE,
)

# Credential-related keywords
CREDENTIAL_KEYWORDS = [
    "api key", "api-key", "apikey",
    "credential", "credentials",
    "token", "secret",
    "password", "passwd",
    "auth key", "authkey",
    "private key", "private-key",
    "access key", "access-key",
]

# Package installation commands
PACKAGE_INSTALL_PATTERNS = [
    re.compile(r"\bpip\s+install\b", re.IGNORECASE),
    re.compile(r"\bpip3\s+install\b", re.IGNORECASE),
    re.compile(r"\bapt\s+install\b", re.IGNORECASE),
    re.compile(r"\bapt-get\s+install\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+install\b", re.IGNORECASE),
    re.compile(r"\byarn\s+add\b", re.IGNORECASE),
    re.compile(r"\buv\s+install\b", re.IGNORECASE),
    re.compile(r"\bcargo\s+install\b", re.IGNORECASE),
    re.compile(r"\bbrew\s+install\b", re.IGNORECASE),
    re.compile(r"\bconda\s+install\b", re.IGNORECASE),
]

# Config file patterns
CONFIG_FILE_PATTERNS = [
    re.compile(r"config\.ya?ml", re.IGNORECASE),
]

# File path detection regex (matches absolute paths and ~ paths)
PATH_PATTERNS = [
    re.compile(r"(?:^|\s)((?:/[\w\-./]+)+)", re.MULTILINE),  # absolute paths
    re.compile(r"(~[/\w\-./]+)"),  # tilde paths
    re.compile(r"(\.\.?/[\w\-./]+)"),  # relative paths
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    """A hard guardrail violation — blocks the proposal."""
    id: str
    message: str


@dataclass
class Warning_:
    """A soft warning — proposal allowed but requires human approval."""
    id: str
    message: str


@dataclass
class GuardrailResult:
    """Result of all guardrail checks."""
    allowed: bool = True
    violations: List[Violation] = field(default_factory=list)
    warnings: List[Warning_] = field(default_factory=list)

    @property
    def requires_human_approval(self) -> bool:
        return len(self.warnings) > 0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "violations": [{"id": v.id, "message": v.message} for v in self.violations],
            "warnings": [{"id": w.id, "message": w.message} for w in self.warnings],
            "requires_human_approval": self.requires_human_approval,
        }


# ---------------------------------------------------------------------------
# Guardrail checks
# ---------------------------------------------------------------------------

def check_max_active_objectives(kanban_db: str) -> GuardrailResult:
    """GR1: No more than 5 active objectives simultaneously.

    Counts objectives (from task body tags `objective:OBJ-N`) that have
    at least one task in ready/running/blocked state.
    """
    result = GuardrailResult()

    db_path = os.path.expanduser(kanban_db)
    if not os.path.exists(db_path):
        # Can't check — allow (no evidence of violation)
        return result

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get all non-terminal tasks with objective tags in their body
        cursor.execute(
            "SELECT body, status FROM tasks WHERE status IN ('ready', 'running', 'blocked')"
        )
        rows = cursor.fetchall()
        conn.close()
    except sqlite3.Error:
        # DB error — allow (fail open, not silent block)
        return result

    active_objectives = set()
    for row in rows:
        body = row["body"] or ""
        # Extract objective:OBJ-N tags
        for match in re.finditer(r"objective:(OBJ-\d+)", body, re.IGNORECASE):
            active_objectives.add(match.group(1).upper())

    if len(active_objectives) >= MAX_ACTIVE_OBJECTIVES:
        result.allowed = False
        result.violations.append(Violation(
            id="GR1",
            message=f"Too many active objectives: {len(active_objectives)} "
                    f"(limit: {MAX_ACTIVE_OBJECTIVES}). Active: {sorted(active_objectives)}",
        ))

    return result


def check_file_scope(text: str) -> GuardrailResult:
    """GR2/GR3/GR11: No touching files outside allowed paths.

    Checks that any file paths mentioned in the text are within
    the plugin repo or ~/.hermes/.
    """
    result = GuardrailResult()

    # Collect all paths mentioned in the text
    found_paths = set()
    for pattern in PATH_PATTERNS:
        for match in pattern.finditer(text):
            path = match.group(1)
            if path:
                found_paths.add(path)

    # Expand ~ paths for comparison
    for path in found_paths:
        expanded = os.path.expanduser(path)
        # Normalize for prefix comparison
        expanded = os.path.normpath(expanded)

        # Check if it's within an allowed prefix
        allowed = False
        for prefix in ALLOWED_PATH_PREFIXES:
            if expanded.startswith(os.path.normpath(prefix)):
                allowed = True
                break

        if not allowed:
            # Check if it's a known system path
            is_system = False
            for sys_path in SYSTEM_FILE_BLACKLIST:
                if expanded.startswith(sys_path) or expanded.endswith(sys_path):
                    is_system = True
                    break

            if is_system:
                result.allowed = False
                result.violations.append(Violation(
                    id="GR4",
                    message=f"Objective touches system file: {path}",
                ))
            else:
                # Outside allowed paths but not a system file
                result.allowed = False
                result.violations.append(Violation(
                    id="GR2",
                    message=f"Objective touches file outside allowed scope: {path} "
                            f"(only {PLUGIN_REPO} and {HERMES_HOME} are allowed)",
                ))

    return result


def check_system_files(text: str) -> GuardrailResult:
    """GR4: No touching system files (.env, .ssh/config, /etc/, etc.)."""
    result = GuardrailResult()

    text_lower = text.lower()

    for sys_path in SYSTEM_FILE_BLACKLIST:
        if sys_path in text_lower:
            result.allowed = False
            result.violations.append(Violation(
                id="GR4",
                message=f"Objective mentions system file: {sys_path}",
            ))

    return result


def check_credentials(text: str) -> GuardrailResult:
    """GR5: No proposing objectives that require new credentials without approval."""
    result = GuardrailResult()

    text_lower = text.lower()

    for keyword in CREDENTIAL_KEYWORDS:
        if keyword in text_lower:
            result.warnings.append(Warning_(
                id="GR5",
                message=f"Objective mentions credentials: '{keyword}'. "
                        f"Requires human approval before proceeding.",
            ))
            break  # One warning is enough

    return result


def check_daily_proposal_limit(state_file: str) -> GuardrailResult:
    """GR6: Maximum 1 new objective proposed per day."""
    result = GuardrailResult()

    state_path = os.path.expanduser(state_file)
    if not os.path.exists(state_path):
        return result  # No proposals yet

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_date = entry.get("date", "")
                    entry_allowed = entry.get("allowed", False)
                    # Only count proposals that were allowed (actually created)
                    if entry_date == today and entry_allowed:
                        result.allowed = False
                        result.violations.append(Violation(
                            id="GR6",
                            message=f"Daily proposal limit reached: already proposed "
                                    f"1 objective today ({today}).",
                        ))
                        break
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass  # Can't read state — fail open

    return result


def check_config_yaml(text: str) -> GuardrailResult:
    """GR7: No modifying config.yaml without human approval."""
    result = GuardrailResult()

    for pattern in CONFIG_FILE_PATTERNS:
        if pattern.search(text):
            result.warnings.append(Warning_(
                id="GR7",
                message="Objective modifies config.yaml. "
                        "Requires human approval before proceeding.",
            ))
            break

    return result


def check_triage_only() -> GuardrailResult:
    """GR8: All proposed objectives must enter triage (not auto-promoted).

    This is enforced at task-creation time (the creator uses --triage flag),
    not at validation time. This check is a no-op that always passes —
    it exists for documentation completeness and to ensure the validator
    output explicitly confirms triage-only status.
    """
    result = GuardrailResult()
    # No-op: enforced by the task creator, not the validator.
    # The validator output always implies "create in triage".
    return result


def check_package_install(text: str) -> GuardrailResult:
    """GR9: No proposing objectives that require installing packages without approval."""
    result = GuardrailResult()

    for pattern in PACKAGE_INSTALL_PATTERNS:
        if pattern.search(text):
            result.warnings.append(Warning_(
                id="GR9",
                message=f"Objective requires package installation "
                        f"({pattern.pattern.strip()}). "
                        f"Requires human approval before proceeding.",
            ))
            break

    return result


def check_other_repos(text: str) -> GuardrailResult:
    """GR10: No touching other repos in ~/git/."""
    result = GuardrailResult()

    for match in OTHER_REPOS_PATTERN.finditer(text):
        path = match.group(0)
        # Ensure it's not just "REPO"
        # (the regex already excludes it, but double-check)
        if "hermes-plugin-quota-governor" not in path:
            result.allowed = False
            result.violations.append(Violation(
                id="GR10",
                message=f"Objective touches other repo in ~/git/: {path}",
            ))

    return result


def check_os_files(text: str) -> GuardrailResult:
    """GR11: No modifying OS files outside ~/.hermes/."""
    result = GuardrailResult()

    # This is largely covered by check_file_scope, but we add explicit
    # checks for common OS paths that might be mentioned without a full path
    os_patterns = [
        re.compile(r"/etc/\S+", re.IGNORECASE),
        re.compile(r"/var/\S+", re.IGNORECASE),
        re.compile(r"/usr/\S+", re.IGNORECASE),
        re.compile(r"/proc/\S+", re.IGNORECASE),
        re.compile(r"/sys/\S+", re.IGNORECASE),
        re.compile(r"/boot/\S+", re.IGNORECASE),
        re.compile(r"/dev/\S+", re.IGNORECASE),
    ]

    for pattern in os_patterns:
        for match in pattern.finditer(text):
            path = match.group(0).rstrip(".,;:)]}")
            result.allowed = False
            result.violations.append(Violation(
                id="GR11",
                message=f"Objective modifies OS file outside ~/.hermes/: {path}",
            ))

    return result


# ---------------------------------------------------------------------------
# Main validation
# ---------------------------------------------------------------------------

def validate_objective(
    title: str,
    body: str,
    kanban_db: str = "~/.hermes/kanban.db",
    state_file: str = "~/.hermes/quota-governor/objective-proposals.jsonl",
) -> GuardrailResult:
    """Run all 11 guardrail checks on a proposed objective.

    Args:
        title: Title of the proposed objective.
        body: Body/description of the proposed objective.
        kanban_db: Path to kanban.db for dynamic checks.
        state_file: Path to proposals state file for GR6.

    Returns:
        GuardrailResult with all violations and warnings.
    """
    combined = GuardrailResult()
    full_text = f"{title}\n{body}"

    # GR1: Max active objectives (dynamic)
    r = check_max_active_objectives(kanban_db)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR2/GR3/GR11: File scope (static)
    r = check_file_scope(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR4: System files (static)
    r = check_system_files(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR5: Credentials (static, warning)
    r = check_credentials(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR6: Daily proposal limit (dynamic)
    r = check_daily_proposal_limit(state_file)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR7: config.yaml (static, warning)
    r = check_config_yaml(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR8: Triage-only (no-op, enforced at creation)
    r = check_triage_only()
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR9: Package install (static, warning)
    r = check_package_install(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR10: Other repos (static)
    r = check_other_repos(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # GR11: OS files (static)
    r = check_os_files(full_text)
    combined.violations.extend(r.violations)
    combined.warnings.extend(r.warnings)

    # Allowed only if no violations
    combined.allowed = len(combined.violations) == 0

    return combined


def record_proposal(
    title: str,
    result: GuardrailResult,
    state_file: str,
    task_id: Optional[str] = None,
) -> None:
    """Record a proposal (allowed or rejected) in the state file."""
    state_path = os.path.expanduser(state_file)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "title": title,
        "allowed": result.allowed,
        "violations": [{"id": v.id, "message": v.message} for v in result.violations],
        "warnings": [{"id": w.id, "message": w.message} for w in result.warnings],
        "requires_human_approval": result.requires_human_approval,
        # Include evidence/pattern_key for cross-path dedup with objective-proposer.
        # Without this, _already_proposed() can't match entries from validate-guardrails
        # against entries from objective-proposer, causing duplicate proposals.
        "evidence": title,  # title is the best available proxy here
    }
    if task_id:
        entry["task_id"] = task_id

    try:
        with open(state_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="OBJ-17 guardrails validator for autonomous objectives."
    )
    parser.add_argument("--title", required=True, help="Title of the proposed objective")
    parser.add_argument("--body", required=True, help="Body/description of the proposed objective")
    parser.add_argument("--kanban-db", default="~/.hermes/kanban.db",
                        help="Path to kanban.db (default: ~/.hermes/kanban.db)")
    parser.add_argument("--state-file", default="~/.hermes/quota-governor/objective-proposals.jsonl",
                        help="Path to proposals state file")
    parser.add_argument("--quiet", action="store_true",
                        help="Only output exit code (no JSON)")
    parser.add_argument("--record", action="store_true",
                        help="Record this proposal in the state file")
    parser.add_argument("--task-id", default=None,
                        help="Task ID if the proposal was created (for --record)")
    args = parser.parse_args()

    result = validate_objective(
        title=args.title,
        body=args.body,
        kanban_db=args.kanban_db,
        state_file=args.state_file,
    )

    if args.record:
        record_proposal(args.title, result, args.state_file, task_id=args.task_id)

    if not args.quiet:
        print(json.dumps(result.to_dict(), indent=2))

    sys.exit(0 if result.allowed else 1)


if __name__ == "__main__":
    main()