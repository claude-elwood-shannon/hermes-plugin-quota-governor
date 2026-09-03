#!/usr/bin/env python3
"""assignee-fix.py — deterministic G1 profile validation enforcement (OBJ-08).

The cron LLM agent (autonomous-task-creator) sometimes invents profile names
not present on the host (e.g. 'alice'), violating Guardrail G1.  The prompt
already forbids this, but deepseek-v4-flash does not always comply.  This
script makes G1 enforcement deterministic instead of relying on LLM
compliance — the same pattern used by privacy-router-fix.py (OBJ-18 follow-up).

How it works:
  1. Computes ``valid_profiles`` = ``hermes profile list`` ∩ ALLOWED_PROFILES.
     This is the authoritative set of profiles that may receive auto-created
     tasks (mirrors quota-gate.py's G1 logic).
  2. Scans the kanban DB for tasks whose ``assignee`` is NOT in
     ``valid_profiles`` and that are in a reassignable status
     (ready, todo, triage — never running/done/blocked/archived).
  3. Reassigns each invalid task to the first valid profile (preference order:
     pr-ollama, then pr-nanogpt).  If no valid profile exists, leaves the task
     alone and logs a warning (the LLM agent should have produced [SILENT]).
  4. Idempotent: a task already assigned to a valid profile is a no-op.

This script is wired two ways:
  - Plugin hook ``on_kanban_dispatch_tick`` (near-real-time, ~60s after tick)
  - No-agent cron job every 5 minutes (backup)

Output (stdout): one line per reassignment, or empty (silent) when nothing
to do.  Empty stdout means the cron layer delivers nothing (watchdog pattern).

Usage:
  python3 assignee-fix.py              # scan + reassign
  python3 assignee-fix.py --dry-run    # scan only, print what would change
  python3 assignee-fix.py --verbose    # debug output

Exit codes:
  0 = success (including no-op)
  1 = catastrophic error (DB unreadable, etc.)

Design:
  - NEVER modifies task status — only reassigns.
  - NEVER touches running tasks.
  - Silent on empty (no invalid-assignee tasks found).
  - Idempotent: running twice is safe.
  - No secrets in output — task IDs and profile names only.

References:
  - docs/autonomous-objectives.md §OBJ-08
  - scripts/privacy-router-fix.py (sibling — same pattern, OBJ-18 follow-up)
  - scripts/quota-gate.py ALLOWED_PROFILES + get_existing_profiles (G1)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
import sys
from typing import List, Optional, Set, Tuple

# ── Config ───────────────────────────────────────────────────────────────────

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")

# Guardrail G1: only these profiles may receive auto-created tasks.
# Mirrors quota-gate.py ALLOWED_PROFILES.
ALLOWED_PROFILES = {"pr-ollama", "pr-nanogpt"}

# Preference order for fallback reassignment: pr-ollama first (cheaper,
# availability-first), then pr-nanogpt.  Never pr-openrouter for auto-tasks.
PROFILE_PREFERENCE = ["pr-ollama", "pr-nanogpt"]

# Only reassign tasks in these statuses (not running, done, blocked, archived).
# Mirrors privacy-router-fix.py REASSIGNABLE_STATUSES.
REASSIGNABLE_STATUSES = {"ready", "todo", "triage"}


# ── Profile discovery (G1) ────────────────────────────────────────────────────

def get_existing_profiles() -> Set[str]:
    """Return the set of profile names that actually exist on this host.

    Parses ``hermes profile list`` output.  Falls back to the known-good
    profiles if the command is unavailable or fails to parse, so the fix
    never blocks on a transient CLI error.  Mirrors quota-gate.py's
    get_existing_profiles exactly.
    """
    try:
        proc = subprocess.run(
            ["hermes", "profile", "list"],
            capture_output=True, text=True, timeout=20,
        )
        out = proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        out = ""

    profiles: Set[str] = set()
    for line in out.splitlines():
        # Skip header, separator, and empty lines.  The active profile is
        # prefixed with a '◆' marker; strip it.
        line = line.strip().lstrip("\u25c6").strip()
        if not line or line.startswith("─") or line.startswith("Profile"):
            continue
        name = line.split()[0] if line.split() else ""
        if name:
            profiles.add(name)

    # Fallback: if we couldn't parse anything, assume the known-good set.
    if not profiles:
        profiles = set(ALLOWED_PROFILES)
    return profiles


def compute_valid_profiles() -> Tuple[List[str], Set[str]]:
    """Compute the ordered list of valid profiles for reassignment.

    Returns (ordered_valid, valid_set) where ordered_valid is sorted by
    PROFILE_PREFERENCE and valid_set is the set form for membership tests.
    If no allowed profile exists on the host, returns ([], set()).
    """
    existing = get_existing_profiles()
    valid_set = existing & ALLOWED_PROFILES
    # Order by preference, then alphabetically for determinism.
    ordered = [p for p in PROFILE_PREFERENCE if p in valid_set]
    ordered += sorted(valid_set - set(ordered))
    return ordered, valid_set


# ── Kanban DB query ──────────────────────────────────────────────────────────

def find_invalid_assignee_tasks(
    conn: sqlite3.Connection, valid_set: Set[str]
) -> List[Tuple[str, str, str, str]]:
    """Find tasks whose assignee is not in valid_profiles.

    Only scans reassignable statuses (ready/todo/triage) — never touches
    running, done, blocked, or archived tasks.

    Returns a list of (task_id, title, assignee, status) tuples.
    A task with a NULL/empty assignee is NOT flagged here — unassigned
    tasks are normal (triage tasks, manual tasks).  Only tasks assigned
    to a *non-existent* profile are G1 violations.
    """
    invalid = []
    rows = conn.execute(
        "SELECT id, title, assignee, status FROM tasks "
        "WHERE status IN ('ready', 'todo', 'triage')",
    ).fetchall()

    for row in rows:
        task_id, title, assignee, status = row
        # Skip unassigned tasks (NULL or empty) — those are not G1 violations.
        if not assignee:
            continue
        if assignee not in valid_set:
            invalid.append((task_id, title, assignee, status))

    return invalid


# ── Reassignment ─────────────────────────────────────────────────────────────

def reassign_task(task_id: str, correct_profile: str) -> bool:
    """Reassign a task via ``hermes kanban assign``."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "assign", task_id, correct_profile],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception as exc:
        print(f"ERROR: reassign failed for {task_id}: {exc}", file=sys.stderr)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic G1 profile-validation enforcement (OBJ-08)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without reassigning.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Debug output."
    )
    args = parser.parse_args()

    if not os.path.isfile(KANBAN_DB):
        if args.verbose:
            print(f"VERBOSE: kanban.db not found at {KANBAN_DB}")
        return 0  # silent, nothing to do

    # 1. Compute valid profiles (G1).
    ordered_valid, valid_set = compute_valid_profiles()
    if args.verbose:
        print(f"VERBOSE: valid_profiles (ordered) = {ordered_valid}")
        print(f"VERBOSE: valid_profiles (set) = {sorted(valid_set)}")

    if not valid_set:
        # No allowed profile exists on host — cannot reassign.  The LLM
        # agent should have produced [SILENT].  Log and exit silently.
        if args.verbose:
            print("VERBOSE: no valid profiles on host — cannot reassign")
        return 0

    fallback_profile = ordered_valid[0]

    # 2. Find tasks with invalid assignees.
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    try:
        invalid_tasks = find_invalid_assignee_tasks(conn, valid_set)
    finally:
        conn.close()

    if not invalid_tasks:
        if args.verbose:
            print("VERBOSE: no invalid-assignee tasks found")
        return 0  # silent, nothing to do

    # 3. Reassign each invalid task to the fallback profile.
    reassigned = 0
    for task_id, title, current_assignee, status in invalid_tasks:
        if args.dry_run:
            print(
                f"DRY-RUN: would reassign {task_id} "
                f"({status}) from '{current_assignee}' to '{fallback_profile}' "
                f"— {title[:60]}"
            )
            continue

        if reassign_task(task_id, fallback_profile):
            print(
                f"reassigned {task_id} "
                f"from '{current_assignee}' to '{fallback_profile}' "
                f"({status}) — G1 fix"
            )
            reassigned += 1
        else:
            print(
                f"FAILED to reassign {task_id} "
                f"from '{current_assignee}' to '{fallback_profile}'",
                file=sys.stderr,
            )

    if not args.dry_run and reassigned:
        # Non-empty stdout ensures the cron layer delivers this.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())