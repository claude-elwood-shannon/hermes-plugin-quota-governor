#!/usr/bin/env python3
"""
repo-sync-check.py — OBJ-13: Automatic repo synchronization monitor.

Checks the plugin repo at REPO for:
  1. Uncommitted changes to tracked files (excluding .worktrees/)
  2. Commits ahead of origin/main (committed but not pushed)

If either condition is found, creates a kanban task for commit+push.
Idempotent: tracks created sync tasks in repo-sync.jsonl and queries the
board to avoid duplicate sync tasks.

Design principles (mirrors diagnose-crash.py):
  - Dry-run by default. Never creates tasks without --execute.
  - Silent on empty: exits 0 with empty stdout when repo is synced.
  - Max 1 sync task per tick (avoid board flooding).
  - Idempotent: checks board for existing pending sync tasks before creating.
  - Push failure handling: task body includes backoff retry instructions.

Usage:
  python3 repo-sync-check.py              # dry-run, prints decisions to stdout
  python3 repo-sync-check.py --execute    # create sync tasks
  python3 repo-sync-check.py --verbose    # verbose output (debug)
"""

import sqlite3
import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

REPO_DIR = "REPO"
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
SYNC_FILE = os.path.expanduser("~/.hermes/quota-governor/repo-sync.jsonl")
LOG_FILE = os.path.expanduser("~/.hermes/logs/repo-sync-check.log")
MAX_SYNC_PER_TICK = 1
SYNC_VERSION = "1.0"
# Assignee for created sync tasks — profile with the most quota
DEFAULT_ASSIGNEE = "pr-nanogpt"

# ── Logging ──────────────────────────────────────────────────────────────────

VERBOSE = False

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if VERBOSE or level in ("WARN", "ERROR"):
        print(line, file=sys.stderr)

# ── Git Helpers ──────────────────────────────────────────────────────────────

def git(args, cwd=REPO_DIR):
    """Run a git command, return (returncode, stdout, stderr).

    Note: stdout is NOT stripped of leading whitespace — git porcelain
    format uses leading spaces for status codes (e.g. ' M file.py').
    Only trailing whitespace/newlines are stripped.
    """
    result = subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=30,
        cwd=cwd,
    )
    # Strip only trailing newline, preserve leading spaces
    out = result.stdout
    if out.endswith("\n"):
        out = out[:-1]
    return result.returncode, out, result.stderr.strip()

def get_uncommitted_changes():
    """Return list of changed tracked files (excluding .worktrees/ and untracked)."""
    rc, out, _ = git(["status", "--porcelain", "--untracked-files=no"])
    if rc != 0:
        log(f"git status failed (rc={rc})", "ERROR")
        return []
    changes = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Porcelain format: XY <path>  (X=staged, Y=unstaged)
        # Positions 0-1 = status codes, position 2 = space, 3+ = path
        status = line[:2]
        path = line[3:].strip()
        if not path:
            # Handle renamed files (old -> new format)
            path = line[3:].split(" -> ")[-1].strip()
        if not path or path.startswith(".worktrees/"):
            continue
        changes.append({"status": status, "path": path})
    return changes

def get_ahead_count():
    """Return number of commits ahead of origin/main."""
    rc, out, _ = git(["rev-list", "--count", "origin/main..main"])
    if rc != 0:
        # origin/main might not exist yet
        log(f"git rev-list failed (rc={rc}): {out}", "WARN")
        return 0
    try:
        return int(out)
    except ValueError:
        return 0

def get_ahead_commits():
    """Return list of commit hashes and messages ahead of origin/main."""
    rc, out, _ = git(["log", "--oneline", "origin/main..main"])
    if rc != 0:
        return []
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
    return commits

# ── DB Helpers ───────────────────────────────────────────────────────────────

def get_db_path():
    return os.environ.get("HERMES_KANBAN_DB", KANBAN_DB)

def has_pending_sync_task(conn):
    """Check if there's already a pending (non-done) sync task on the board.

    We look for tasks whose title starts with 'OBJ-13:' and are not done.
    This prevents duplicate sync task creation across ticks.
    """
    rows = conn.execute(
        """
        SELECT id, title, status FROM tasks
        WHERE title LIKE 'OBJ-13: Repo sync%'
          AND status IN ('todo', 'ready', 'running', 'blocked')
        ORDER BY created_at DESC
        LIMIT 1
        """,
    ).fetchall()
    return [dict(r) for r in rows] if rows else []

# ── Idempotency ──────────────────────────────────────────────────────────────

def load_synced_records():
    """Load recent sync records from repo-sync.jsonl."""
    records = []
    if not os.path.isfile(SYNC_FILE):
        return records
    try:
        with open(SYNC_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log(f"Failed to load sync file: {e}", "WARN")
    return records

def record_sync(sync_task_id, uncommitted, ahead_commits):
    """Append a sync record to repo-sync.jsonl."""
    os.makedirs(os.path.dirname(SYNC_FILE), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": SYNC_VERSION,
        "sync_task_id": sync_task_id,
        "uncommitted_files": len(uncommitted),
        "ahead_commits": len(ahead_commits),
        "uncommitted_paths": [c["path"] for c in uncommitted][:20],
        "ahead_hashes": [c["hash"] for c in ahead_commits][:20],
    }
    with open(SYNC_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── Task Creation ────────────────────────────────────────────────────────────

def build_sync_body(uncommitted, ahead_commits):
    """Build the body for the sync task."""
    sections = []

    sections.append(
        "OBJ-13: Sincronizacion automatica del repo\n\n"
        "El cron de sincronizacion detecto cambios sin publicar en "
        "REPO.\n\n"
    )

    if uncommitted:
        sections.append("## Cambios sin commitear\n")
        for c in uncommitted[:30]:
            sections.append(f"  {c['status']} {c['path']}")
        if len(uncommitted) > 30:
            sections.append(f"  ... y {len(uncommitted) - 30} mas")
        sections.append("")

    if ahead_commits:
        sections.append("## Commits sin pushear\n")
        for c in ahead_commits[:30]:
            sections.append(f"  {c['hash']} {c['message']}")
        if len(ahead_commits) > 30:
            sections.append(f"  ... y {len(ahead_commits) - 30} mas")
        sections.append("")

    sections.append(
        "## Instrucciones\n\n"
        "1. Revisar los cambios con `git status` y `git diff` en "
        "REPO\n"
        "2. Si hay cambios sin commitear que tocan codigo del plugin:\n"
        "   - Commitear con el nombre/email del usuario (NUNCA inventar datos):\n"
        "     git -c user.name='Claude Elwood Shannon' "
        "-c user.email='claude.el.shannon@proton.me' commit -m '<msg>'\n"
        "   - Excluir archivos de entorno/ruido (.worktrees/, __pycache__/, etc.)\n"
        "3. Pushear a origin/main (Tor via SSH ProxyCommand ya configurado):\n"
        "   git push origin main\n"
        "   (El SSH config usa ProxyCommand nc -x 127.0.0.1:9050 para github.com)\n"
        "   NOTA: NO usar 'torify git push' — causa doble proxy y falla.\n"
        "4. Si el push falla (Tor/red), reintentar con backoff:\n"
        "   - Esperar 30s, reintentar\n"
        "   - Esperar 2min, reintentar\n"
        "   - Esperar 5min, reintentar\n"
        "   - Si despues de 3 intentos falla, documentar el error y bloquear "
        "la tarea con kanban_block(reason='Push failed: <error>')\n"
        "5. Verificar que origin/main coincide con local:\n"
        "   git fetch origin && git rev-list --count origin/main..main\n"
        "   Debe ser 0.\n"
        "6. Dejar evidencia: output de git push y git log en el summary.\n"
    )

    return "\n".join(sections)

def create_sync_task(uncommitted, ahead_commits):
    """Create a sync task via hermes kanban create CLI."""
    body = build_sync_body(uncommitted, ahead_commits)

    # Descriptive title with timestamp
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    parts = []
    if uncommitted:
        parts.append(f"{len(uncommitted)} uncommitted")
    if ahead_commits:
        parts.append(f"{len(ahead_commits)} unpushed")
    title = f"OBJ-13: Repo sync needed ({', '.join(parts)}) [{ts}]"

    try:
        result = subprocess.run(
            ['hermes', 'kanban', 'create', title,
             '--assignee', DEFAULT_ASSIGNEE,
             '--workspace', 'scratch',
             '--body', body,
             '--created-by', 'repo-sync-check.py',
             '--json'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'HERMES_KANBAN_DB': get_db_path()},
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                return data.get('id') or data.get('task_id')
            except json.JSONDecodeError:
                import re
                m = re.search(r'(t_\w+)', result.stdout)
                if m:
                    return m.group(1)
        else:
            log(f"hermes kanban create failed: {result.stderr.strip()}", "ERROR")
    except Exception as e:
        log(f"Exception creating sync task: {e}", "ERROR")
    return None

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="OBJ-13: Automatic repo synchronization monitor"
    )
    parser.add_argument('--execute', action='store_true',
                        help='Create sync tasks (default: dry-run)')
    parser.add_argument('--verbose', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    # CRON_MODE env var enables --execute for no_agent cron jobs.
    if os.environ.get('REPO_SYNC_EXECUTE', '').strip() in ('1', 'true', 'yes'):
        args.execute = True

    VERBOSE = args.verbose

    # Verify repo exists
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        log(f"Repo not found at {REPO_DIR}", "ERROR")
        sys.exit(1)

    # Gather repo state
    uncommitted = get_uncommitted_changes()
    ahead_commits = get_ahead_commits()
    ahead_count = len(ahead_commits)

    if not uncommitted and ahead_count == 0:
        # Repo is synced — silent exit for no_agent cron
        if VERBOSE:
            print("Repo is synced. No action needed.")
        sys.exit(0)

    log(f"Repo needs sync: {len(uncommitted)} uncommitted files, "
        f"{ahead_count} commits ahead of origin/main", "INFO")

    # Check idempotency: is there already a pending sync task?
    if not os.path.isfile(get_db_path()):
        log(f"Kanban DB not found at {get_db_path()}", "WARN")
    else:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            existing = has_pending_sync_task(conn)
        finally:
            conn.close()

        if existing:
            log(f"Pending sync task already exists: {existing[0]['id']} "
                f"(status={existing[0]['status']}). Not creating duplicate.", "INFO")
            if VERBOSE:
                print(f"SKIP: pending sync task {existing[0]['id']} already on board")
            sys.exit(0)

    # Create the sync task
    if args.execute:
        sync_id = create_sync_task(uncommitted, ahead_commits)
        if sync_id:
            record_sync(sync_id, uncommitted, ahead_commits)
            # Print to stdout for cron delivery
            summary_parts = []
            if uncommitted:
                summary_parts.append(f"{len(uncommitted)} uncommitted files")
            if ahead_commits:
                summary_parts.append(f"{len(ahead_commits)} unpushed commits")
            print(
                f"SYNC_TASK_CREATED: {sync_id} — "
                f"{' and '.join(summary_parts)} in plugin repo"
            )
        else:
            log("Failed to create sync task", "ERROR")
            sys.exit(1)
    else:
        # Dry-run: report what would be done
        summary_parts = []
        if uncommitted:
            summary_parts.append(f"{len(uncommitted)} uncommitted files")
            for c in uncommitted[:10]:
                print(f"  UNCOMMITTED: [{c['status']}] {c['path']}")
        if ahead_commits:
            summary_parts.append(f"{len(ahead_commits)} unpushed commits")
            for c in ahead_commits[:10]:
                print(f"  UNPUSHED: {c['hash']} {c['message']}")
        print(f"DRY-RUN: would create sync task ({', '.join(summary_parts)})")

if __name__ == "__main__":
    main()
