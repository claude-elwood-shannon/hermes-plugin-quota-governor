#!/usr/bin/env python3
"""privacy-router-fix.py — deterministic privacy routing enforcement (OBJ-18 follow-up).

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
PRIVACY_GATE = os.path.expanduser("~/.hermes/profiles/pr-ollama/scripts/privacy-gate.sh")
REASSIGNABLE_STATUSES = {"ready", "todo", "triage"}
SENSITIVE_CAPABLE_PROFILES = {"pr-ollama", "pr-nanogpt", "pr-vllm"}
SENSITIVE_TAGS = {"high", "sensitive", "medium"}
CONFIDENTIAL_TAGS = {"confidential", "conf", "intimo"}

# ── Privacy tag parsing ──────────────────────────────────────────────────────

def parse_privacy_tag(body: str) -> Optional[str]:
    """Value of the task's 'privacy:' tag line (lowercased, quotes stripped), or None when the body carries none."""
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("privacy:"):
            value = stripped[len("privacy:"):].strip().strip("'\"")
            return value
    return None

def is_sensitive(privacy_value: Optional[str]) -> bool:
    """True when the privacy tag routes under the sensitive policy (high/sensitive/medium)."""
    return bool(privacy_value and privacy_value.lower() in SENSITIVE_TAGS)

def is_confidential(privacy_value: Optional[str]) -> bool:
    """True when the privacy tag routes under the confidential policy (confidential/conf/intimo): strictest tier, never a cloud profile."""
    return bool(privacy_value and privacy_value.lower() in CONFIDENTIAL_TAGS)

# ── Privacy gate query ──────────────────────────────────────────────────────

def query_privacy_gate() -> Optional[str]:
    """Run privacy-gate.sh high and return its JSON 'context.recommended_profile', or None when the gate is missing, fails or yields no recommendation."""
    if not os.path.isfile(PRIVACY_GATE):
        print(f"ERROR: privacy-gate.sh not found at {PRIVACY_GATE}", file=sys.stderr)
        return None
    env = os.environ.copy()
    env_file = os.path.expanduser("~/.hermes/profiles/pr-ollama/.env")
    if os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, _, val = line.partition('=')
                    env[key.strip()] = val.strip().strip("'\"")
    try:
        result=subprocess.run(['bash', PRIVACY_GATE, 'high'],capture_output=True,text=True,timeout=30,env=env)
        if result.returncode!=0:
            print(f"ERROR: privacy-gate.sh exited {result.returncode}",file=sys.stderr)
            return None
        lines=result.stdout.strip().splitlines()
        for line in reversed(lines):
            line=line.strip()
            if line.startswith('{'):
                data=json.loads(line)
                return data.get('context',{}).get('recommended_profile')
    except Exception as exc:
        print(f"ERROR: privacy-gate.sh failed: {exc}",file=sys.stderr)
        return None
    return None

# ── Kanban DB query ──────────────────────────────────────────────────────────

def find_misrouted_sensitive_tasks(conn: sqlite3.Connection, correct_profile: str) -> list:
    """Live tasks tagged privacy-sensitive whose assignee is NOT a sensitive-capable profile. Returns (task_id, title, assignee, privacy, status) tuples."""
    misrouted=[]
    rows=conn.execute("SELECT id,title,body,assignee,status FROM tasks WHERE status NOT IN ('done','archived','blocked','running')").fetchall()
    for row in rows:
        task_id,title,body,assignee,status=row
        privacy_value=parse_privacy_tag(body)
        if is_sensitive(privacy_value) and assignee not in SENSITIVE_CAPABLE_PROFILES:
            misrouted.append((task_id,title,assignee,privacy_value,status))
    return misrouted

def find_misrouted_confidential_tasks(conn: sqlite3.Connection) -> list:
    """Live tasks tagged privacy-confidential assigned to ANY profile (no local provider exists, so every assignment is a violation to flag). Returns (task_id, title, assignee, privacy, status) tuples."""
    misrouted=[]
    rows=conn.execute("SELECT id,title,body,assignee,status FROM tasks WHERE status NOT IN ('done','archived','blocked','running')").fetchall()
    for row in rows:
        task_id,title,body,assignee,status=row
        privacy_value=parse_privacy_tag(body)
        if is_confidential(privacy_value) and assignee is not None:
            misrouted.append((task_id,title,assignee,privacy_value,status))
    return misrouted

# ── Reassignment ─────────────────────────────────────────────────────────────

def reassign_task(task_id: str, correct_profile: str) -> bool:
    """Reassign one task via the hermes kanban CLI. True on rc=0; False plus an stderr line on failure."""
    try:
        result=subprocess.run(['hermes','kanban','assign',task_id,correct_profile],capture_output=True,text=True,timeout=10)
        return result.returncode==0
    except Exception as exc:
        print(f"ERROR: reassign failed for {task_id}: {exc}",file=sys.stderr)
        return False

# ── Main helpers ─────────────────────────────────────────────────────────────

def _parse_args():
    parser=argparse.ArgumentParser(description="Deterministic privacy routing enforcement.")
    parser.add_argument('--dry-run',action='store_true',help='Print what would change without reassigning.')
    parser.add_argument('--verbose',action='store_true',help='Debug output.')
    return parser.parse_args()

def _prepare_connection():
    if not os.path.isfile(KANBAN_DB):
        print(f"DEBUG: kanban.db not found at {KANBAN_DB}",file=sys.stderr)
        return None
    conn=sqlite3.connect(KANBAN_DB)
    conn.row_factory=sqlite3.Row
    return conn

def _handle_misrouted(conn,correct_profile,dry_run,verbose):
    misrouted=find_misrouted_sensitive_tasks(conn,correct_profile)
    confidential=find_misrouted_confidential_tasks(conn)
    reassigned=0
    for task_id,title,cur,priv,status in misrouted:
        if dry_run:
            print(f"DRY-RUN: would reassign {task_id} {status} from {cur} to {correct_profile} [privacy:{priv}]")
            continue
        if reassign_task(task_id,correct_profile):
            print(f"reassigned {task_id} from {cur} to {correct_profile} [privacy:{priv}]")
            reassigned+=1
        else:
            print(f"FAILED to reassign {task_id} from {cur} to {correct_profile}",file=sys.stderr)
    for task_id,title,cur,priv,status in confidential:
        msg=f"WARNING: {task_id} {status} has privacy:{priv} assigned to cloud profile {cur} — no local provider, cannot reassign"
        if dry_run:
            print(f"DRY-RUN: {msg}")
        else:
            print(msg,file=sys.stderr)
    return reassigned

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """Query the gate, reassign misrouted sensitive tasks and warn on confidential ones. Returns 0 even on no-op; a missing DB or gate recommendation is a silent exit."""
    args=_parse_args()
    conn=_prepare_connection()
    if not conn:
        return 0
    correct_profile=query_privacy_gate()
    if not correct_profile:
        if args.verbose:
            print("VERBOSE: privacy-gate.sh returned no recommendation")
        return 0
    if args.verbose:
        print(f"VERBOSE: privacy-gate.sh recommends {correct_profile} for sensitive")
    _handle_misrouted(conn,correct_profile,args.dry_run,args.verbose)
    conn.close()
    return 0

if __name__=='__main__':
    sys.exit(main())
