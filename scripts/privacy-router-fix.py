#!/usr/bin/env python3
"""privacy-router-fix.py — deterministic privacy routing enforcement (OBJ-18 follow-up).

The cron LLM agent (autonomous-task-creator) consistently skips the Phase 2
privacy-gate.sh re-run, so privacy:high tasks are assigned to pr-ollama
instead of pr-nanogpt. This script makes routing deterministic instead of
relying on LLM compliance.

How it works:
  1. Scans the kanban DB for tasks with `privacy:high` (or `privacy:sensitive`)
     in the body that are assigned to a profile OTHER than the privacy-gate
     recommended profile.
  2. Runs `privacy-gate.sh high` to get the correct profile.
  3. Reassigns mismatched tasks to the correct profile via `hermes kanban assign`.
  4. Only reassigns tasks in status `ready`, `todo`, or `triage` — never
     touches `running` tasks (assign_task refuses running tasks anyway).
  5. Idempotent: a task already correctly assigned is a no-op.

  Additionally (OBJ-18 S2):
  6. Scans for `privacy:confidential` tasks assigned to ANY cloud provider.
     Confidential data must never leave the host. Since no local provider
     is configured on this host, these tasks cannot be reassigned — they
     are flagged with a WARNING on stderr so the operator can intervene.

This script is wired two ways:
  - Plugin hook `on_kanban_dispatch_tick` (near-real-time, ~60s after tick)
  - No-agent cron job every 5 minutes (backup)

Output (stdout): one line per reassignment, or empty (silent) when nothing
to do. Empty stdout means the cron layer delivers nothing (watchdog pattern).

Usage:
  python3 privacy-router-fix.py              # scan + reassign
  python3 privacy-router-fix.py --dry-run    # scan only, print what would change
  python3 privacy-router-fix.py --verbose    # debug output

Exit codes:
  0 = success (including no-op)
  1 = catastrophic error (DB unreadable, etc.)

Design:
  - NEVER modifies task status — only reassigns.
  - NEVER touches running tasks.
  - Silent on empty (no misrouted tasks found).
  - Idempotent: running twice is safe.
  - No secrets in output — task IDs and profile names only.
"""
import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys
from typing import Optional

# ── Config ───────────────────────────────────────────────────────────────────

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
PRIVACY_GATE = os.path.expanduser(
    "~/.hermes/profiles/pr-ollama/scripts/privacy-gate.sh"
)

# Only reassign tasks in these statuses (not running, done, blocked, archived)
REASSIGNABLE_STATUSES = {"ready", "todo", "triage"}

# Privacy tags that trigger sensitive routing
SENSITIVE_TAGS = {"high", "sensitive", "medium"}

# Privacy tags that trigger confidential routing (stricter than sensitive)
CONFIDENTIAL_TAGS = {"confidential", "conf", "intimo"}


# ── Privacy tag parsing ──────────────────────────────────────────────────────

def parse_privacy_tag(body: str) -> Optional[str]:
    """Extract the privacy level from a task body.

    Looks for `privacy:<level>` where level is one of:
    high, medium, low, public, sensitive, confidential
    (case-insensitive). Returns the raw value (not normalised) or None.
    """
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("privacy:"):
            value = stripped[len("privacy:"):].strip().strip("'\"")
            return value
    return None


def is_sensitive(privacy_value: Optional[str]) -> bool:
    """Check if a privacy value maps to sensitive routing."""
    if not privacy_value:
        return False
    return privacy_value.lower() in SENSITIVE_TAGS


def is_confidential(privacy_value: Optional[str]) -> bool:
    """Check if a privacy value maps to confidential routing.

    Confidential is stricter than sensitive: data must never leave the host.
    On this host no local provider is configured, so confidential tasks
    cannot be routed to ANY cloud provider — they should be flagged.
    """
    if not privacy_value:
        return False
    return privacy_value.lower() in CONFIDENTIAL_TAGS


# ── Privacy gate query ──────────────────────────────────────────────────────

def query_privacy_gate() -> Optional[str]:
    """Run privacy-gate.sh high and return the recommended_profile.

    Returns None if the gate fails or returns no recommendation.
    """
    if not os.path.isfile(PRIVACY_GATE):
        print(
            f"ERROR: privacy-gate.sh not found at {PRIVACY_GATE}",
            file=sys.stderr,
        )
        return None

    # Source the .env for API keys (privacy-gate.sh needs them via quota-gate.py)
    env = os.environ.copy()
    env_file = os.path.expanduser("~/.hermes/profiles/pr-ollama/.env")
    if os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    env[key.strip()] = val.strip().strip("'\"")

    try:
        result = subprocess.run(
            ["bash", PRIVACY_GATE, "high"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            print(
                f"ERROR: privacy-gate.sh exited {result.returncode}",
                file=sys.stderr,
            )
            return None

        # The output is the last line of stdout (JSON)
        lines = result.stdout.strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                return data.get("context", {}).get("recommended_profile")
    except Exception as exc:
        print(f"ERROR: privacy-gate.sh failed: {exc}", file=sys.stderr)
        return None

    return None


# ── Kanban DB query ──────────────────────────────────────────────────────────

def find_misrouted_sensitive_tasks(
    conn: sqlite3.Connection, correct_profile: str
) -> list:
    """Find tasks with privacy:high/sensitive assigned to the wrong profile.

    Returns a list of (task_id, title, assignee, privacy_value, status) tuples.
    """
    misrouted = []
    rows = conn.execute(
        "SELECT id, title, body, assignee, status FROM tasks "
        "WHERE status NOT IN ('done', 'archived', 'blocked', 'running')",
    ).fetchall()

    for row in rows:
        task_id, title, body, assignee, status = row
        privacy_value = parse_privacy_tag(body)
        if is_sensitive(privacy_value) and assignee != correct_profile:
            misrouted.append((task_id, title, assignee, privacy_value, status))

    return misrouted


def find_misrouted_confidential_tasks(conn: sqlite3.Connection) -> list:
    """Find confidential tasks assigned to ANY cloud provider.

    Confidential data must never leave the host. On this host no local
    provider is configured, so ANY cloud assignee is a misroute.
    Returns a list of (task_id, title, assignee, privacy_value, status) tuples.
    """
    misrouted = []
    rows = conn.execute(
        "SELECT id, title, body, assignee, status FROM tasks "
        "WHERE status NOT IN ('done', 'archived', 'blocked', 'running')",
    ).fetchall()

    for row in rows:
        task_id, title, body, assignee, status = row
        privacy_value = parse_privacy_tag(body)
        if is_confidential(privacy_value) and assignee is not None:
            misrouted.append(
                (task_id, title, assignee, privacy_value, status)
            )

    return misrouted


# ── Reassignment ─────────────────────────────────────────────────────────────

def reassign_task(task_id: str, correct_profile: str) -> bool:
    """Reassign a task via `hermes kanban assign`."""
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

def main():
    parser = argparse.ArgumentParser(
        description="Deterministic privacy routing enforcement."
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
        return  # silent, nothing to do

    # 1. Query the privacy gate for the correct sensitive-profile
    correct_profile = query_privacy_gate()
    if not correct_profile:
        if args.verbose:
            print("VERBOSE: privacy-gate.sh returned no recommendation")
        return  # silent, can't determine correct profile

    if args.verbose:
        print(f"VERBOSE: privacy-gate.sh recommends {correct_profile} for sensitive")

    # 2. Find misrouted sensitive tasks
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    try:
        misrouted = find_misrouted_sensitive_tasks(conn, correct_profile)
        # Also find confidential tasks on any cloud provider (no local
        # provider exists, so any cloud assignee is a misroute).
        confidential_misrouted = find_misrouted_confidential_tasks(conn)
    finally:
        conn.close()

    if not misrouted and not confidential_misrouted:
        if args.verbose:
            print("VERBOSE: no misrouted sensitive or confidential tasks found")
        return  # silent, nothing to do

    # 3. Reassign sensitive tasks
    reassigned = 0
    for task_id, title, current_assignee, privacy_value, status in misrouted:
        if args.dry_run:
            print(
                f"DRY-RUN: would reassign {task_id} "
                f"({status}) from {current_assignee} to {correct_profile} "
                f"[privacy:{privacy_value}]"
            )
            continue

        if reassign_task(task_id, correct_profile):
            print(
                f"reassigned {task_id} "
                f"from {current_assignee} to {correct_profile} "
                f"[privacy:{privacy_value}]"
            )
            reassigned += 1
        else:
            print(
                f"FAILED to reassign {task_id} "
                f"from {current_assignee} to {correct_profile}",
                file=sys.stderr,
            )

    if not args.dry_run and reassigned:
        # Non-empty stdout ensures the cron layer delivers this
        pass

    # 4. Flag confidential tasks (cannot reassign — no local provider)
    for task_id, title, current_assignee, privacy_value, status in confidential_misrouted:
        msg = (
            f"WARNING: {task_id} ({status}) has privacy:{privacy_value} "
            f"but is assigned to cloud profile {current_assignee} — "
            f"no local provider configured, cannot reassign"
        )
        if args.dry_run:
            print(f"DRY-RUN: {msg}")
        else:
            print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())