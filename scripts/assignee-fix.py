#!/usr/bin/env python3
"""assignee-fix.py – deterministic G1 profile validation.

The script rewrites any kanban task whose assignee is not in the allowed
set of profiles.  It keeps all other behaviour unchanged.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sqlite3
import sys
from typing import List, Set, Tuple

# ── Config ───────────────────────────────────────────────────────────────────
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
ALLOWED_PROFILES = {"pr-ollama", "pr-nanogpt", "pr-opencode", "pr-vllm"}
PROFILE_PREFERENCE = ["pr-ollama", "pr-nanogpt", "pr-opencode"]
REASSIGNABLE_STATUSES = {"ready", "todo", "triage"}

# ── Profile discovery (G1) ────────────────────────────────────────────────────

def get_existing_profiles() -> Set[str]:
    try:
        proc = subprocess.run(
            ["hermes", "profile", "list"], capture_output=True, text=True, timeout=20
        )
        out = proc.stdout or ""
    except Exception:
        out = ""
    profiles: Set[str] = set()
    for line in out.splitlines():
        line = line.strip().lstrip("\u25c6").strip()
        if not line or line.startswith("──") or line.startswith("Profile"):
            continue
        name = line.split()[0] if line.split() else ""
        if name:
            profiles.add(name)
    if not profiles:
        profiles = set(ALLOWED_PROFILES)
    return profiles


def compute_valid_profiles() -> Tuple[List[str], Set[str]]:
    existing = get_existing_profiles()
    valid_set = existing & ALLOWED_PROFILES
    ordered = [p for p in PROFILE_PREFERENCE if p in valid_set]
    ordered += sorted(valid_set - set(ordered))
    return ordered, valid_set

# ── Database helper ─────────────────────────────────────────────────────────────

def find_invalid_assignee_tasks(conn: sqlite3.Connection, valid_set: Set[str]) -> List[Tuple[str, str, str, str]]:
    rows = conn.execute(
        "SELECT id, title, assignee, status FROM tasks WHERE status IN ('ready','todo','triage')"
    ).fetchall()
    invalid = []
    for row in rows:
        task_id, title, assignee, status = row
        if not assignee:
            continue
        if assignee not in valid_set:
            invalid.append((task_id, title, assignee, status))
    return invalid

# ── Reassignment ─────────────────────────────────────────────────────────────

def reassign_task(task_id: str, correct_profile: str) -> bool:
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

# ── Argument parsing ──────────────────────────────────────────────────

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic G1 profile‑validation enforcement (OBJ‑08)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without reassigning.")
    parser.add_argument("--verbose", action="store_true", help="Debug output.")
    return parser.parse_args()

# ── Core processing (to keep main small) ─────────────────────────────────────

def process_tasks(args: argparse.Namespace, ordered_valid: List[str], valid_set: Set[str]) -> int:
    if not os.path.isfile(KANBAN_DB):
        if args.verbose:
            print(f"VERBOSE: kanban.db not found at {KANBAN_DB}")
        return 0
    fallback_profile = ordered_valid[0]
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    try:
        invalid_tasks = find_invalid_assignee_tasks(conn, valid_set)
    finally:
        conn.close()
    if not invalid_tasks:
        if args.verbose:
            print("VERBOSE: no invalid‑assignee tasks found")
        return 0
    reassigned = 0
    for task_id, title, cur, status in invalid_tasks:
        if args.dry_run:
            print(f"DRY‑RUN: would reassign {task_id} ({status}) from '{cur}' to '{fallback_profile}' – {title[:60]}")
            continue
        if reassign_task(task_id, fallback_profile):
            print(f"reassigned {task_id} from '{cur}' to '{fallback_profile}' ({status}) – G1 fix")
            reassigned += 1
        else:
            print(f"FAILED to reassign {task_id} from '{cur}' to '{fallback_profile}'", file=sys.stderr)
    if not args.dry_run and reassigned:
        pass  # non‑empty stdout signals change to cron layer
    return 0

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_arguments()
    ordered_valid, valid_set = compute_valid_profiles()
    if args.verbose:
        print(f"VERBOSE: valid_profiles (ordered) = {ordered_valid}")
        print(f"VERBOSE: valid_profiles (set) = {sorted(valid_set)}")
    if not valid_set:
        if args.verbose:
            print("VERBOSE: no valid profiles on host – cannot reassign")
        return 0
    return process_tasks(args, ordered_valid, valid_set)

if __name__ == "__main__":
    sys.exit(main())
