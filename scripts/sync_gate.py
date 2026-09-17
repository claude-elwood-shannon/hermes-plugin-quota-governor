#!/usr/bin/env python3
"""sync_gate.py — OBJ-13 pre-commit syntax gate (t_cec89d77).

validate-guardrails.py validates OBJECTIVE PROPOSALS before a kanban task
is created; this gate validates the FILES a sync worker is about to commit.
It exists because an OBJ-13 sync commit on 2026-09-15 (f92b3d2) committed
context-sync-corrupted WIP (a misplaced `from __future__` import) and
propagated it to every deployed copy; budget_check and fondo-queue-watch
crashed at 03:21:29 and the commit had to be reverted in 8bc198e.

Checks (deterministic, stdlib-only, no network):
  *.py  -> py_compile (AST-level syntax validation on this interpreter)
  *.sh  -> bash -n (parse without executing)
Anything else (docs, configs, COMMIT_MSG...) is skipped, and so are files
that no longer exist (a deleted path has nothing left to validate).

Contract, mandated by the OBJ-13 task instructions in repo-sync-check.py:
  exit 0 -> commit + propagation allowed
  exit 1 -> NO commit, NO propagation; stdout lists every broken file and
           the alert is persisted to ~/.hermes/logs/sync-gate.log

Usage (run from the repo root; relative paths resolve against CWD):
  python3 scripts/sync_gate.py              # every touched .py/.sh in git status
  python3 scripts/sync_gate.py <file> ...   # explicit file list
  python3 scripts/sync_gate.py --quiet <file> ...
"""
from __future__ import annotations

import argparse
import os
import py_compile
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.expanduser("~/.hermes/logs/sync-gate.log")
PY_EXTS = (".py",)
SH_EXTS = (".sh",)
GENERATED_DIRS = {"__pycache__", ".worktrees"}
CHECK_TIMEOUT = 30  # seconds, bash -n never hangs on well-formed shells

# ── Logging ──────────────────────────────────────────────────────────────────

VERBOSE = False


def log(msg: str, level: str = "INFO") -> None:
    """Append a timestamped line to LOG_FILE (errors also go to stderr)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if VERBOSE or level in ("WARN", "ERROR"):
        print(line, file=sys.stderr)


# ── Path classification ──────────────────────────────────────────────────────

def _resolve(path: str) -> Optional[str]:
    """Absolute path of an existing file, or None (deleted / unreachable)."""
    if os.path.isfile(path):
        return os.path.abspath(path)
    return None


def _skip_reason(path: str) -> Optional[str]:
    """Why this path needs no syntax check, or None if it is checkable code."""
    parts = Path(path).parts
    if GENERATED_DIRS & set(parts):
        return "generated-dir"
    if path.endswith(PY_EXTS) or path.endswith(SH_EXTS):
        return None
    return "not-code"


def _check_name(path: str) -> str:
    """Human-readable name of the check that applies to this path."""
    if path.endswith(PY_EXTS):
        return "py_compile"
    if path.endswith(SH_EXTS):
        return "bash -n"
    return "none"


# ── Checks ───────────────────────────────────────────────────────────────────

def _check_py(path: str) -> Tuple[bool, str]:
    """py_compile one file: (passed, error_detail). Compiles to a throwaway
    .pyc in the system temp dir so the source tree is never touched."""
    fd, cfile = tempfile.mkstemp(suffix=".pyc")
    os.close(fd)
    try:
        py_compile.compile(path, cfile=cfile, doraise=True)
        return True, "py_compile"
    except py_compile.PyCompileError as e:
        return False, str(e).strip()
    finally:
        try:
            os.unlink(cfile)
        except OSError:
            pass


def _check_sh(path: str) -> Tuple[bool, str]:
    """bash -n one file (parse-only, never executes): (passed, error_detail)."""
    try:
        r = subprocess.run(
            ["bash", "-n", path],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"bash -n timed out after {CHECK_TIMEOUT}s"
    if r.returncode == 0:
        return True, "bash -n"
    return False, (r.stderr or "bash -n failed").strip()


def check_file(path: str) -> Tuple[str, str]:
    """Validate one candidate file.

    Returns (status, detail):
      "ok"   — detail names the passing check ("py_compile", "bash -n")
               or the skip reason ("not-code", "generated-dir", "missing")
      "fail" — detail is the syntax error text
    """
    resolved = _resolve(path)
    if resolved is None:
        return "ok", "missing"
    reason = _skip_reason(resolved)
    if reason is not None:
        return "ok", reason
    if resolved.endswith(PY_EXTS):
        ok, detail = _check_py(resolved)
    else:
        ok, detail = _check_sh(resolved)
    return ("ok" if ok else "fail"), detail


def check_files(paths: List[str]) -> Tuple[bool, List[dict]]:
    """Validate every path; return (all_ok, failures).

    failures entries: {"path", "check", "error"} — one per broken file, in
    input order. Skipped and missing paths never fail a sync.
    """
    failures: List[dict] = []
    for p in paths:
        status, detail = check_file(p)
        if status == "fail":
            failures.append({"path": p, "check": _check_name(p), "error": detail})
    return (not failures), failures


# ── Touched-file discovery (git status mode) ─────────────────────────────────

def touched_files(cwd: str = ".") -> List[str]:
    """Every tracked-or-untracked touched path in `git status --porcelain`.

    Mirrors repo-sync-check.get_uncommitted_changes parsing: rename lines
    ("old -> new") yield the NEW path; .worktrees/ paths are excluded;
    deleted paths are kept (they resolve to skip('missing')).
    """
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"git status failed in {cwd}: {e}", "WARN")
        return []
    if r.returncode != 0:
        log(f"git status failed (rc={r.returncode}) in {cwd}", "WARN")
        return []
    paths = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if not path:
            continue
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path.startswith(".worktrees/"):
            continue
        paths.append(path)
    return paths


# ── Alert ────────────────────────────────────────────────────────────────────

def alert(failures: List[dict]) -> None:
    """Print the blocked-commit alert (stdout) and persist it to LOG_FILE."""
    lines = [
        f"SYNC_GATE: BLOCKED — {len(failures)} file(s) failed syntax validation "
        "(no commit, no propagation)"
    ]
    for f in failures:
        first = (f["error"].splitlines() or [""])[0]
        lines.append(f"  BROKEN: {f['path']} [{f['check']}] {first}")
    for line in lines:
        print(line)
    log("BLOCKED: " + "; ".join(f["path"] for f in failures), "ERROR")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OBJ-13 pre-commit syntax gate: py_compile / bash -n "
                    "every touched file before a sync commit."
    )
    parser.add_argument(
        "paths", nargs="*",
        help="files to validate (default: every touched .py/.sh in git status)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="only output on failure (exit code carries the verdict)",
    )
    parser.add_argument("--verbose", action="store_true", help="verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: validate paths (explicit or git status) and gate."""
    global VERBOSE
    args = _parse_args(argv)
    VERBOSE = args.verbose
    paths = args.paths if args.paths else touched_files()
    if not paths:
        if not args.quiet:
            print("SYNC_GATE: no files to validate (clean tree or no args) — pass")
        return 0
    all_ok, failures = check_files(paths)
    if not all_ok:
        alert(failures)
        return 1
    if not args.quiet:
        for p in paths:
            status, detail = check_file(p)
            if status == "ok":
                print(f"OK   {p} ({detail})")
        print("SYNC_GATE: all checked files pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
