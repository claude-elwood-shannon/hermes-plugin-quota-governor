#!/usr/bin/env python3
"""
repo-sync-check.py — OBJ-13: Automatic repo synchronization monitor.

Monitors one or more repositories and creates kanban tasks for commit+push
when a repo has uncommitted or unpushed changes. Repos are driven by an
external config file:

    ~/.hermes/quota-governor/repo-watch.json

a JSON array of objects:
    {
      "repo":     "REPO",  # absolute path
      "remote":   "origin",            # remote ref (default "origin")
      "assignee": "pr-nanogpt",        # profile (default "auto" -> DEFAULT_ASSIGNEE)
      "enabled":  true                 # skip when false
    }

If the config file does NOT exist, the watchdog falls back to the historic
hardcoded REPO_DIR (single-repo behavior, zero regression).

Safety properties (mirrors diagnose-crash.py):
  - Dry-run by default. Never creates tasks without --execute.
  - Silent on empty: exits 0 with empty stdout when every watched repo is synced.
  - Max 1 sync task per tick total (flood prevention across all repos).
  - Per-repo idempotency: queries the board for an existing pending sync task
    for that repo before creating; tracks created tasks in repo-sync.jsonl.
  - Oldest-desync priority: from the dirty repos that need a task, creates the
    task for the repo that has been desynced the longest.
  - Fail-safe config: a corrupt/malformed repo-watch.json aborts with no tasks
    created; a bad/disabled repo entry is skipped, never fatal.
  - Push failure handling: task body includes backoff retry instructions.

Usage:
  python3 repo-sync-check.py              # dry-run, prints decisions to stdout
  python3 repo-sync-check.py --execute    # create sync tasks
  python3 repo-sync-check.py --verbose    # verbose output (debug)

Deploy-drift (OBJ-06 / t_b8511377):
  Every run also md5-compares each repo scripts/ file against its deployed
  copies in ~/.hermes/scripts/ and ~/.hermes/profiles/*/scripts/. A repo-only
  script (never deployed) is NOT an alert. Divergence prints a DEPLOY_DRIFT
  alert on stdout (cron delivery) — alert-only, never auto-deploys.
"""

import glob
import hashlib
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

REPO_DIR = str(Path(__file__).resolve().parent.parent)
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
SYNC_FILE = os.path.expanduser("~/.hermes/quota-governor/repo-sync.jsonl")
LOG_FILE = os.path.expanduser("~/.hermes/logs/repo-sync-check.log")
CONFIG_FILE = os.path.expanduser("~/.hermes/quota-governor/repo-watch.json")
MAX_SYNC_PER_TICK = 1
SYNC_VERSION = "3.0"
# Deploy-drift (OBJ-06/t_b8511377): deployed copies of the repo's scripts/.
# Scripts are deployed to ~/.hermes/scripts/ and ~/.hermes/profiles/*/scripts/.
# A script present in the repo but NOT deployed anywhere is "no-deployed"
# (not stale). A script whose deployed md5 differs from the repo md5 is drift.
DEPLOY_DIRS = (
    [os.path.expanduser("~/.hermes/scripts")]
    + sorted(
        glob.glob(os.path.expanduser("~/.hermes/profiles/*/scripts"))
    )
)
# Assignee for created sync tasks — profile with the most quota
DEFAULT_ASSIGNEE = "pr-nanogpt"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"

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

# ── Config loading ───────────────────────────────────────────────────────────

def load_repo_configs():
    """Return a list of repo config dicts.

    Priority:
      1. If repo-watch.json exists and is valid JSON, return its array
         (each entry normalized with defaults). Corrupt/malformed config
         raises ValueError so the caller can abort safely with no tasks.
      2. Otherwise (file absent), return the legacy single-repo config so
         today's behavior is unchanged.
    """
    if not os.path.isfile(CONFIG_FILE):
        log(f"Config file {CONFIG_FILE} not found — falling back to single-repo {REPO_DIR}")
        return [_normalize_config({"repo": REPO_DIR})]

    try:
        with open(CONFIG_FILE, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"Corrupt or unreadable config {CONFIG_FILE}: {e}", "ERROR")
        raise ValueError(f"repo-watch.json is corrupt or unreadable: {e}")

    if not isinstance(raw, list):
        log(f"Config {CONFIG_FILE} is not a JSON array", "ERROR")
        raise ValueError("repo-watch.json must be a JSON array of repo objects")

    configs = []
    for entry in raw:
        if not isinstance(entry, dict):
            log(f"Config entry is not an object: {entry!r} — skipping", "WARN")
            continue
        if "repo" not in entry or not isinstance(entry.get("repo"), str) or not entry.get("repo"):
            log(f"Config entry missing valid 'repo' path: {entry!r} — skipping", "WARN")
            continue
        configs.append(_normalize_config(entry))
    return configs

def _normalize_config(entry):
    """Apply defaults: remote=origin, branch=main, assignee=auto, enabled=True."""
    return {
        "repo": entry["repo"],
        "remote": entry.get("remote", DEFAULT_REMOTE) or DEFAULT_REMOTE,
        "branch": entry.get("branch", DEFAULT_BRANCH) or DEFAULT_BRANCH,
        "assignee": entry.get("assignee", "auto"),
        "enabled": entry.get("enabled", True),
    }

def repo_basename(repo_dir):
    return os.path.basename(os.path.normpath(repo_dir)) or repo_dir

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

def get_uncommitted_changes(cwd=REPO_DIR):
    """Return list of changed tracked files (excluding .worktrees/ and untracked)."""
    rc, out, _ = git(["status", "--porcelain", "--untracked-files=no"], cwd=cwd)
    if rc != 0:
        log(f"git status failed (rc={rc}) in {cwd}", "ERROR")
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

def get_ahead_count(cwd=REPO_DIR, remote=DEFAULT_REMOTE, branch=DEFAULT_BRANCH):
    """Return number of commits ahead of <remote>/<branch>."""
    rc, out, _ = git(["rev-list", "--count", f"{remote}/{branch}..{branch}"], cwd=cwd)
    if rc != 0:
        log(f"git rev-list failed (rc={rc}) in {cwd}", "WARN")
        return 0
    try:
        return int(out)
    except ValueError:
        return 0

def get_ahead_commits(cwd=REPO_DIR, remote=DEFAULT_REMOTE, branch=DEFAULT_BRANCH):
    """Return list of commit hashes and messages ahead of <remote>/<branch>."""
    rc, out, _ = git(["log", "--oneline", f"{remote}/{branch}..{branch}"], cwd=cwd)
    if rc != 0:
        return []
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
    return commits

def get_ahead_oldest_ts(cwd=REPO_DIR, remote=DEFAULT_REMOTE, branch=DEFAULT_BRANCH):
    """Return the unix timestamp of the OLDEST commit ahead of <remote>/<branch>, or None."""
    rc, out, _ = git(["log", "--format=%ct", f"{remote}/{branch}..{branch}"], cwd=cwd)
    if rc != 0:
        return None
    times = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            times.append(int(line))
        except ValueError:
            continue
    return min(times) if times else None

def _min_mtime_for_changes(cwd, changes):
    """Earliest mtime among changed tracked paths (robust to deleted/renamed)."""
    mtimes = []
    for c in changes[:200]:
        p = os.path.join(cwd, c["path"])
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            continue
    return min(mtimes) if mtimes else None

def compute_desync_time(cwd, uncommitted, ahead_commits, remote, branch):
    """Return the earliest timestamp at which this repo became desynced.

    Combines the oldest ahead-commit time (committed-but-unpushed) with the
    earliest mtime of uncommitted files. This is used to prioritize which
    repo to sync first (oldest desync first). Returns None if no desync.
    """
    candidates = []
    if ahead_commits:
        ts = get_ahead_oldest_ts(cwd=cwd, remote=remote, branch=branch)
        if ts:
            candidates.append(ts)
    if uncommitted:
        ts = _min_mtime_for_changes(cwd, uncommitted)
        if ts:
            candidates.append(ts)
    return min(candidates) if candidates else None

# ── Deploy-drift (OBJ-06 / t_b8511377) ──────────────────────────────────────
#
# The git-vs-origin sweep above cannot catch the failure mode where the
# repo is clean but the DEPLOYED copies of scripts/ are stale (that is how
# quota-governor-tick ran the Aug-27 version until Sep-08). This section
# audits repo scripts vs their deployed copies. Alert-only: never
# auto-deploys, never mutates deployed files.


def md5_of_file(path):
    """Return the md5 hex digest of a file, or None if unreadable."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError as e:
        log(f"md5_of_file({path}) failed: {e}", "WARN")
        return None
    return h.hexdigest()


def check_deploy_drift(repo_dir=None, deploy_dirs=None):
    """Compare each repo scripts/ file against its deployed copies.

    Returns a list of drift dicts. Semantics:
      - file deployed in >= 1 DEPLOY_DIRS and every deployed md5 == repo md5
            → in sync, not reported.
      - file deployed but at least one deployed md5 differs from repo md5
            → drift (stale deployed copy).
      - file present ONLY in the repo (no deployed copy anywhere)
            → no-deployed, NOT reported (explicit acceptance criterion).
      - repo file unreadable → skipped with a WARN (fail-safe).
    """
    if repo_dir is None:
        repo_dir = REPO_DIR
    if deploy_dirs is None:
        deploy_dirs = DEPLOY_DIRS
    scripts_dir = os.path.join(repo_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        log(f"No scripts/ dir under {repo_dir} — deploy-drift check skipped", "WARN")
        return []

    drift = []
    for name in sorted(os.listdir(scripts_dir)):
        repo_path = os.path.join(scripts_dir, name)
        if not os.path.isfile(repo_path):
            continue  # subdirs (__pycache__ etc.) are not deployable scripts
        repo_md5 = md5_of_file(repo_path)
        if repo_md5 is None:
            continue

        deployed = []
        for d in deploy_dirs:
            dep_path = os.path.join(d, name)
            if not os.path.isfile(dep_path):
                continue
            dep_md5 = md5_of_file(dep_path)
            if dep_md5 is None:
                continue  # unreadable deployed copy: skip this copy
            try:
                mtime = os.path.getmtime(dep_path)
            except OSError:
                mtime = None
            deployed.append({
                "dir": d,
                "path": dep_path,
                "md5": dep_md5,
                "mtime": mtime,
            })

        if not deployed:
            continue  # no-deployed: script exists only in the repo — OK

        stale = [d for d in deployed if d["md5"] != repo_md5]
        if stale:
            drift.append({
                "script": name,
                "repo_md5": repo_md5,
                "repo_path": repo_path,
                "stale_copies": stale,
            })
    return drift


def _fmt_ts(ts):
    """Unix timestamp → 'YYYY-MM-DD HH:MM:SS' local time, or 'unknown'."""
    if ts is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "unknown"


def report_deploy_drift(drift):
    """Print a human-readable deploy-drift alert to stdout (cron delivery)."""
    print("DEPLOY_DRIFT: deployed scripts differ from repo — manual deploy needed (no auto-deploy)")
    for d in drift:
        print(f"  {d['script']}: repo md5 {d['repo_md5']}")
        for s in d["stale_copies"]:
            print(
                f"    deploy {s['path']} md5 {s['md5']} "
                f"(mtime {_fmt_ts(s['mtime'])}) — STALE"
            )

# ── DB Helpers ───────────────────────────────────────────────────────────────

def get_db_path():
    return os.environ.get("HERMES_KANBAN_DB", KANBAN_DB)

def has_pending_sync_task(conn, repo=None):
    """Check if there's already an active (non-done, non-blocked) sync task.

    When `repo` is provided, restrict the match to tasks whose title names
    that repo (its basename) so multiple repos are independently idempotent.
    Without repo, behaves exactly as before (any OBJ-13: Repo sync task).

    We look for tasks whose title starts with 'OBJ-13:' and are in an
    active state (todo, ready, running). Blocked tasks are excluded
    because a blocked sync task indicates a push failure that needs
    human intervention — a new sync task should be created for any
    new changes detected.
    """
    if repo:
        basename = repo_basename(repo)
        rows = conn.execute(
            """
            SELECT id, title, status FROM tasks
            WHERE title LIKE 'OBJ-13: Repo sync%'
              AND title LIKE ?
              AND status IN ('todo', 'ready', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%{basename}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, title, status FROM tasks
            WHERE title LIKE 'OBJ-13: Repo sync%'
              AND status IN ('todo', 'ready', 'running')
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

def _record_pattern_key(repo_dir, remote):
    """Composite identity for the ledger — per-repo idempotency marker."""
    return f"{repo_dir}@{remote}"

def record_sync(sync_task_id, uncommitted, ahead_commits, repo=REPO_DIR, remote=DEFAULT_REMOTE, branch=DEFAULT_BRANCH):
    """Append a sync record to repo-sync.jsonl."""
    os.makedirs(os.path.dirname(SYNC_FILE), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": SYNC_VERSION,
        "sync_task_id": sync_task_id,
        "repo": repo,
        "remote": remote,
        "branch": branch,
        "pattern_key": _record_pattern_key(repo, remote),
        "repo_basename": repo_basename(repo),
        "uncommitted_files": len(uncommitted),
        "ahead_commits": len(ahead_commits),
        "uncommitted_paths": [c["path"] for c in uncommitted][:20],
        "ahead_hashes": [c["hash"] for c in ahead_commits][:20],
    }
    with open(SYNC_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

def recently_synced_repo(repo_dir, remote=DEFAULT_REMOTE, window_seconds=3600):
    """True if a ledger record exists for this repo within the last `window_seconds`."""
    key = _record_pattern_key(repo_dir, remote)
    cutoff = time.time() - window_seconds
    for rec in load_synced_records():
        rec_key = rec.get("pattern_key")
        if rec_key is None:
            # Legacy records lack pattern_key; fall back to repo field/path match.
            rec_key = _legacy_key(rec)
        if rec_key == key:
            try:
                ts = datetime.fromisoformat(rec.get("timestamp", "")).timestamp()
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                return True
    return False

def _legacy_key(rec):
    """Best-effort identity for pre-v2 ledger entries (no pattern_key, no repo)."""
    r = rec.get("repo")
    if r:
        return f"{r}@{rec.get('remote', DEFAULT_REMOTE)}"
    # Old single-repo records: treat as the legacy REPO_DIR.
    return f"{REPO_DIR}@{DEFAULT_REMOTE}"

# ── Task Creation ────────────────────────────────────────────────────────────

def build_sync_body(uncommitted, ahead_commits, repo=REPO_DIR, remote=DEFAULT_REMOTE, branch=DEFAULT_BRANCH, assignee=None):
    """Build the body for the sync task."""
    sections = []
    repo_label = repo
    display_name = repo_basename(repo)

    sections.append(
        "objective:OBJ-13 | cost:micro | model:worker\n\n"
        "OBJ-13: Sincronizacion automatica del repo\n\n"
        f"El cron de sincronizacion detecto cambios sin publicar en {repo_label} "
        f"(repo: {display_name}, remote: {remote}, branch: {branch}).\n\n"
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
        f"{repo_label}\n"
        "2. Si hay cambios sin commitear que tocan codigo del plugin:\n"
        "   - Commitear con el nombre/email del usuario (NUNCA inventar datos):\n"
        "     git -c user.name='Claude Elwood Shannon' "
        "-c user.email='claude.el.shannon@proton.me' commit -m '<msg>'\n"
        "   - Excluir archivos de entorno/ruido (.worktrees/, __pycache__/, etc.)\n"
        f"3. Pushear a {remote}/{branch} (Tor via SSH ProxyCommand ya configurado):\n"
        f"   git push {remote} {branch}\n"
        "   (El SSH config usa ProxyCommand nc -x 127.0.0.1:9050 para github.com)\n"
        "   NOTA: NO usar 'torify git push' — causa doble proxy y falla.\n"
        "4. Si el push falla (Tor/red), reintentar con backoff:\n"
        "   - Esperar 30s, reintentar\n"
        "   - Esperar 2min, reintentar\n"
        "   - Esperar 5min, reintentar\n"
        "   - Si despues de 3 intentos falla, documentar el error y bloquear "
        "la tarea con kanban_block(reason='Push failed: <error>')\n"
        f"5. Verificar que {remote}/{branch} coincide con local:\n"
        f"   git fetch {remote} && git rev-list --count {remote}/{branch}..{branch}\n"
        "   Debe ser 0.\n"
        "6. Dejar evidencia: output de git push y git log en el summary.\n"
    )

    return "\n".join(sections)

def _resolve_assignee(cfg):
    if cfg["assignee"] == "auto":
        return DEFAULT_ASSIGNEE
    return cfg["assignee"]

def create_sync_task(uncommitted, ahead_commits, cfg):
    """Create a sync task via hermes kanban create CLI."""
    repo = cfg["repo"]
    remote = cfg["remote"]
    branch = cfg["branch"]
    assignee = _resolve_assignee(cfg)
    basename = repo_basename(repo)
    body = build_sync_body(uncommitted, ahead_commits, repo=repo, remote=remote, branch=branch, assignee=assignee)

    # Descriptive title with repo basename + timestamp
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    parts = []
    if uncommitted:
        parts.append(f"{len(uncommitted)} uncommitted")
    if ahead_commits:
        parts.append(f"{len(ahead_commits)} unpushed")
    title = f"OBJ-13: Repo sync {basename} needed ({', '.join(parts)}) [{ts}]"

    try:
        result = subprocess.run(
            ['hermes', 'kanban', 'create', title,
             '--assignee', assignee,
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

def _repo_needs_sync(cfg):
    """Return (uncommitted, ahead_commits) or (None, None) if repo is clean/invalid.

    Also verifies the repo dir is a valid git checkout; invalid repos are
    skipped with a warning (fail-safe), never fatal.
    """
    repo = cfg["repo"]
    if not os.path.isdir(os.path.join(repo, ".git")):
        log(f"Repo not found or not a git checkout at {repo} — skipping", "WARN")
        return None, None
    uncommitted = get_uncommitted_changes(cwd=repo)
    ahead_commits = get_ahead_commits(cwd=repo, remote=cfg["remote"], branch=cfg["branch"])
    if not uncommitted and not ahead_commits:
        return [], []
    return uncommitted, ahead_commits

def _gather_dirty_repos(configs, db_path):
    """Return list of {cfg, uncommitted, ahead_commits, desync_time, pending} for dirty repos."""
    dirty = []
    conn = None
    try:
        if os.path.isfile(db_path):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        for cfg in configs:
            if not cfg["enabled"]:
                log(f"Repo disabled in config: {cfg['repo']} — skipping", "INFO")
                continue
            uncommitted, ahead_commits = _repo_needs_sync(cfg)
            if uncommitted is None:
                continue
            if not uncommitted and not ahead_commits:
                continue
            # If the board DB is unavailable we cannot consult it; treat as no
            # pending task (do not let a missing DB block sync creation).
            pending_id = None
            if conn is not None:
                pending = has_pending_sync_task(conn, repo=cfg["repo"])
                pending_id = pending[0]["id"] if pending else None
            desync_time = compute_desync_time(cfg["repo"], uncommitted, ahead_commits, cfg["remote"], cfg["branch"])
            dirty.append({
                "cfg": cfg,
                "uncommitted": uncommitted,
                "ahead_commits": ahead_commits,
                "desync_time": desync_time,
                "pending": bool(pending_id),
                "pending_id": pending_id,
            })
    finally:
        if conn:
            conn.close()
    return dirty

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

    # Load repo configs. Corrupt config aborts safely with no tasks created.
    try:
        configs = load_repo_configs()
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if not configs:
        log("No enabled repos configured — nothing to do", "INFO")
        sys.exit(0)

    # Verify default-repo fallback still points at a valid checkout (legacy path).
    if len(configs) == 1 and os.path.abspath(configs[0]["repo"]) == os.path.abspath(REPO_DIR):
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            log(f"Repo not found at {REPO_DIR}", "ERROR")
            sys.exit(1)

    db_path = get_db_path()

    # Deploy-drift audit (alert-only, before the git sync sweep so a drift
    # alert is delivered even if the sweep later exits early). Never fatal:
    # any error here must not break the historic git-vs-origin behavior.
    try:
        drift = check_deploy_drift(repo_dir=REPO_DIR)
        if drift:
            report_deploy_drift(drift)
            log(f"Deploy drift detected in {len(drift)} script(s)", "WARN")
    except Exception as e:
        log(f"Deploy-drift check failed (non-fatal): {e}", "WARN")

    # Gather dirty repos (respecting enabled flag and per-repo pending state).
    dirty = _gather_dirty_repos(configs, db_path)

    if not dirty:
        # All watched repos synced (or no eligible desync) — silent exit.
        if VERBOSE:
            print("All watched repos are synced. No action needed.")
        sys.exit(0)

    # A repo that already has a pending sync task for IT is not eligible to
    # create a new one this tick; it's already covered.
    candidates = [d for d in dirty if not d["pending"]]

    # Flood prevention: cap total new tasks at 1 per tick. Among eligible
    # (non-pending) dirty repos, pick the one with the OLDEST desync.
    if not candidates:
        # Every dirty repo already has a pending task on the board.
        if VERBOSE:
            print("SKIP: all dirty repos already have pending sync tasks on board")
        for d in dirty:
            p = d.get("pending_id")
            if p:
                log(f"Repo {d['cfg']['repo']} already pending ({p}) — not creating duplicate", "INFO")
        sys.exit(0)

    candidates.sort(key=lambda d: d["desync_time"] if d["desync_time"] is not None else float("inf"))
    target = candidates[0]
    cfg = target["cfg"]
    uncommitted = target["uncommitted"]
    ahead_commits = target["ahead_commits"]
    basename = repo_basename(cfg["repo"])

    log(f"Repo {cfg['repo']} needs sync: {len(uncommitted)} uncommitted files, "
        f"{len(ahead_commits)} commits ahead of {cfg['remote']}/{cfg['branch']}", "INFO")

    if args.execute:
        sync_id = create_sync_task(uncommitted, ahead_commits, cfg)
        if sync_id:
            record_sync(sync_id, uncommitted, ahead_commits, repo=cfg["repo"], remote=cfg["remote"], branch=cfg["branch"])
            # Print to stdout for cron delivery
            summary_parts = []
            if uncommitted:
                summary_parts.append(f"{len(uncommitted)} uncommitted files")
            if ahead_commits:
                summary_parts.append(f"{len(ahead_commits)} unpushed commits")
            print(
                f"SYNC_TASK_CREATED: {sync_id} — "
                f"{' and '.join(summary_parts)} in {basename}"
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
        if len(candidates) > 1:
            print(f"Note: {len(candidates)} eligible dirty repos this tick; choosing oldest desync first.")
        print(f"DRY-RUN: would create sync task for {basename} ({', '.join(summary_parts)})")

if __name__ == "__main__":
    main()
