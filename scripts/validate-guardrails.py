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

PLUGIN_REPO = str(Path(__file__).resolve().parent.parent)
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

# Other repos in the repos root (parent of the plugin repo)
REPOS_ROOT = str(Path(PLUGIN_REPO).parent)
OTHER_REPOS_PATTERN = re.compile(
    re.escape(REPOS_ROOT) + r"/(?!hermes-plugin-quota-governor)[\w\-./]+",
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
        # Extract objective:OBJ-x tags — MEDIATOR t_4fa0a4b5: ids include
        # approved_objectives TABLE ids (OBJ-AUTODEV, ...), not only OBJ-0N.
        for match in re.finditer(r"objective:\s*(OBJ-[A-Za-z0-9._-]+)",
                                 body, re.IGNORECASE):
            active_objectives.add(match.group(1).upper())

    if len(active_objectives) >= MAX_ACTIVE_OBJECTIVES:
        result.allowed = False
        result.violations.append(Violation(
            id="GR1",
            message=f"Too many active objectives: {len(active_objectives)} "
                    f"(limit: {MAX_ACTIVE_OBJECTIVES}). Active: {sorted(active_objectives)}",
        ))

    return result


def _path_is_allowed(expanded: str) -> bool:
    """Whether an expanded, normalized path is within an allowed prefix."""
    for prefix in ALLOWED_PATH_PREFIXES:
        if expanded.startswith(os.path.normpath(prefix)):
            return True
    return False


def _path_is_system(expanded: str) -> bool:
    """Whether an expanded path matches a known system/blacklisted path."""
    for sys_path in SYSTEM_FILE_BLACKLIST:
        if expanded.startswith(sys_path) or expanded.endswith(sys_path):
            return True
    return False


def _collect_paths(text: str) -> set:
    """All file paths mentioned in the text (any PATH_PATTERNS form)."""
    found_paths = set()
    for pattern in PATH_PATTERNS:
        for match in pattern.finditer(text):
            path = match.group(1)
            if path:
                found_paths.add(path)
    return found_paths


def _file_scope_violation(path: str, expanded: str) -> Violation:
    """Classify one out-of-scope path as GR4 (system file) or GR2 (other)."""
    if _path_is_system(expanded):
        return Violation(
            id="GR4",
            message=f"Objective touches system file: {path}",
        )
    return Violation(
        id="GR2",
        message=f"Objective touches file outside allowed scope: {path} "
                f"(only {PLUGIN_REPO} and {HERMES_HOME} are allowed)",
    )


def check_file_scope(text: str) -> GuardrailResult:
    """GR2/GR3/GR11: No touching files outside allowed paths.

    Checks that any file paths mentioned in the text are within
    the plugin repo or ~/.hermes/.
    """
    result = GuardrailResult()

    for path in _collect_paths(text):
        # Expand ~ paths, then normalize for prefix comparison
        expanded = os.path.normpath(os.path.expanduser(path))

        if not _path_is_allowed(expanded):
            result.allowed = False
            result.violations.append(_file_scope_violation(path, expanded))

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
    """GR10: No touching other repos in the repos root."""
    result = GuardrailResult()

    for match in OTHER_REPOS_PATTERN.finditer(text):
        path = match.group(0)
        # Exclude the plugin repo itself, layout-independently: compare the
        # FIRST path component under REPOS_ROOT (a substring check breaks
        # when the checkout parent dir is named like the repo — e.g. GitHub
        # Actions' .../work/<repo>/<repo> layout).
        try:
            rel = os.path.relpath(path, REPOS_ROOT)
            top = Path(rel).parts[0] if rel != "." else ""
        except ValueError:
            top = ""
        if top != "hermes-plugin-quota-governor":
            result.allowed = False
            result.violations.append(Violation(
                id="GR10",
                message=f"Objective touches other repo in the repos root: {path}",
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

def _merge_result(combined: GuardrailResult, result: GuardrailResult) -> None:
    """Merge one check's violations and warnings into the combined result."""
    combined.violations.extend(result.violations)
    combined.warnings.extend(result.warnings)


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
    _merge_result(combined, check_max_active_objectives(kanban_db))
    # GR2/GR3/GR11: File scope (static)
    _merge_result(combined, check_file_scope(full_text))
    # GR4: System files (static)
    _merge_result(combined, check_system_files(full_text))
    # GR5: Credentials (static, warning)
    _merge_result(combined, check_credentials(full_text))
    # GR6: Daily proposal limit (dynamic)
    _merge_result(combined, check_daily_proposal_limit(state_file))
    # GR7: config.yaml (static, warning)
    _merge_result(combined, check_config_yaml(full_text))
    # GR8: Triage-only (no-op, enforced at creation)
    _merge_result(combined, check_triage_only())
    # GR9: Package install (static, warning)
    _merge_result(combined, check_package_install(full_text))
    # GR10: Other repos (static)
    _merge_result(combined, check_other_repos(full_text))
    # GR11: OS files (static)
    _merge_result(combined, check_os_files(full_text))

    # Allowed only if no violations
    combined.allowed = len(combined.violations) == 0

    return combined


def _already_recorded_today(state_path: str, today: str, title: str) -> bool:
    """Dedup: whether an entry with the same title was already recorded
    today (unreadable/garbage lines are skipped, I/O errors fail open)."""
    if not os.path.exists(state_path):
        return False
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if (entry.get("date", "") == today
                            and entry.get("title", "") == title):
                        return True
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return False


def _pattern_kind_from_title(title: str) -> str:
    """Derive a pattern_kind from the title for cross-path dedup with
    objective-proposer.py (which uses kind:suffix pattern_keys)."""
    kind_match = re.match(r"OBJ-\d+:\s*(.+?)(?:\s*[\(\—]|$)", title)
    if kind_match:
        return kind_match.group(1).strip().lower().replace(" ", "_")
    return title[:40]


def _proposal_entry(
    title: str,
    result: GuardrailResult,
    pattern_kind: str,
    today: str,
    task_id: Optional[str] = None,
) -> dict:
    """Build the JSONL entry for a recorded proposal."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "date": today,
        "title": title,
        "pattern_kind": pattern_kind,
        "allowed": result.allowed,
        "violations": [{"id": v.id, "message": v.message} for v in result.violations],
        "warnings": [{"id": w.id, "message": w.message} for w in result.warnings],
        "requires_human_approval": result.requires_human_approval,
        # Include evidence/pattern_key for cross-path dedup with objective-proposer.
        # Without this, _already_proposed() can't match entries from validate-guardrails
        # against entries from objective-proposer, causing duplicate proposals.
        "evidence": title,  # title is the best available proxy here
        "pattern_key": f"{pattern_kind}:{title[:80]}",
    }
    if task_id:
        entry["task_id"] = task_id
    return entry


def _append_entry(state_path: str, entry: dict) -> None:
    """Append one entry as a JSON line; I/O errors are swallowed (fail open)."""
    try:
        with open(state_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def record_proposal(
    title: str,
    result: GuardrailResult,
    state_file: str,
    task_id: Optional[str] = None,
) -> None:
    """Record a proposal (allowed or rejected) in the state file.

    Dedup: if a proposal with the same title was already recorded today,
    skip recording a duplicate. This prevents the autonomous-task-creator
    LLM cron from spamming the proposals file with the same rejected
    objective every 30m tick, which exhausts GR6 and clutters the file.
    (Fixes OBJ-16 root cause 2.)
    """
    state_path = os.path.expanduser(state_file)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    # Dedup: check if an entry with the same title was already recorded today.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _already_recorded_today(state_path, today, title):
        # Already recorded this exact proposal today — skip.
        return

    pattern_kind = _pattern_kind_from_title(title)
    entry = _proposal_entry(title, result, pattern_kind, today, task_id=task_id)
    _append_entry(state_path, entry)


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