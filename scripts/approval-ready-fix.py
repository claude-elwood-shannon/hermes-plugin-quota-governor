#!/usr/bin/python3.12
"""Script that scans the kanban database for triage tasks with an
approval‑ready tag, completes their package fields when possible, dedups
duplicate proposals and logs the changes.  The behaviour is fully
specified in the task description.

The public CLI entry point `main()` is intentionally short – it only parses
arguments and delegates to `process_db()`.  All heavy lifting is done in
small helpers that are each below 50 source lines.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Resolve the repo‑wide registry.  The tests create a temporary database
# so the path can be overridden via ``--db``.
HERMES_ROOT = Path(os.environ.get("AF_HERMES_ROOT", os.path.expanduser("~/.hermes")))

# ---------------------------------------------------------------------------
# Helper: Paths
# ---------------------------------------------------------------------------

def kanban_db_path() -> Path:
    env = os.environ.get("AF_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root = HERMES_ROOT / "kanban.db"
    return root if root.exists() else HERMES_ROOT / "profiles" / "pr-ollama" / "kanban.db"

# ---------------------------------------------------------------------------
# Approval gate helpers – imported lazily because they are optional in the
# test environment.
# ---------------------------------------------------------------------------
try:
    import approval_gate as ag  # type: ignore
except Exception:
    ag = None

# ---------------------------------------------------------------------------
# Static constants used by our logic
# ---------------------------------------------------------------------------

def fix_log_path() -> Path:
    """Return the path where the JSONL change log is written.

    The tests expect a deterministic location under ``~/.hermes/quota-governor``.
    ``AF_LOG`` can override the target for CI runs.
    """
    env = os.environ.get("AF_LOG", "").strip()
    if env:
        return Path(env)
    return HERMES_ROOT / "quota-governor" / "approval-fixes.jsonl"

COST_RE = re.compile(r"(?im)^\s*cost[eé]?\s*:\s*(\w+)\s*$")
PROFILE_RE = re.compile(r"(?im)^\s*perfil\s*[:\-]\s*(.+)$")
COST_TO_CLASS = {"micro": "C", "tiny": "C", "small": "B", "medium": "B",
                 "complex": "A"}
COST_BUDGET_USD = {"micro": 0.05, "tiny": 0.10, "small": 0.20,
                   "medium": 0.50, "complex": 1.00}
APPROVAL_TAG_RE = re.compile(r"(?im)^\s*approval-ready\s*$")
STAMP_RE = re.compile(r"\[APPROVAL:\s*(pending|approved|approved-with|rejected)[^\]]*\]", re.I)
def _connect_ro(db: Path):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# Snapshot logic for the main loop
# ---------------------------------------------------------------------------

def fetch_triage(db: Path) -> List[dict]:
    """Return all triage rows that contain an approval‑ready tag or stamp."""
    rows: List[dict] = []
    try:
        con = _connect_ro(db)
        r = con.execute("SELECT id, title, body, assignee, status FROM tasks WHERE status='triage' ORDER BY created_at").fetchall()
        con.close()
    except sqlite3.Error:
        return rows
    for row in r:
        body = row["body"] or ""
        if APPROVAL_TAG_RE.search(body) or STAMP_RE.search(body):
            rows.append(dict(row))
    return rows

# ---------------------------------------------------------------------------
# Package completion helpers
# ---------------------------------------------------------------------------

def val_for(name: str, body: str, assignee: str) -> Optional[str]:
    """Derivable package line for `name`, or None when not derivable."""
    if name == "presupuesto":
        m = COST_RE.search(body)
        if m:
            return (f"presupuesto estimado: "
                    f"${COST_BUDGET_USD.get(m.group(1).lower(), 0.10):.2f}"
                    f" (standing budget)")
        return None
    if name == "modelo":
        return "modelo asignado: (pin del gate al aprobar)"
    if name == "perfil":
        if PROFILE_RE.search(body):
            return None
        return f"perfil: {assignee}" if assignee else \
            "perfil: (recomendado por gate al aprobar)"
    if name == "fecha":
        return "fecha estimada de entrega: 2 dias"
    if name == "criterio":
        return ("criterio de exito: (pendiente de definir por humano - "
                "sin criterio NO se promueve)")
    if name == "clase":
        m = COST_RE.search(body)
        if m:
            return f"clase: {COST_TO_CLASS.get(m.group(1).lower(), 'C')}"
        return None
    return None  # objetivo: only derivable from the objective: tag


def fill_package(body: str, assignee: str) -> Tuple[str, List[str], List[str]]:
    """Complete missing package fields; approval_gate owns the field regexes."""
    missing = ag.package_missing(body) if ag else []
    if not missing:
        return body, [], []
    new_body = body.rstrip("\n")
    filled: List[str] = []
    unfillable: List[str] = []
    for name in missing:
        line = val_for(name, body, assignee)
        if line:
            new_body += "\n" + line
            filled.append(name)
        else:
            unfillable.append(name)
    return new_body + "\n", filled, unfillable

# ---------------------------------------------------------------------------
# Approval tag handling
# ---------------------------------------------------------------------------

def strip_approval_tag(body: str) -> str:
    return re.sub(r"(?im)^\s*approval-ready\s*$", "", body).rstrip("\n") + "\n"

# ---------------------------------------------------------------------------
# Dedup helpers – small wrappers around approval_gate's utilities
# ---------------------------------------------------------------------------

def _update_body_cas(db: Path, task_id: str, new_body: str) -> bool:
    con = sqlite3.connect(str(db))
    cur = con.execute("UPDATE tasks SET body=? WHERE id=? AND status='triage'", (new_body, task_id))
    con.commit(); con.close()
    return cur.rowcount == 1


def _archive_duplicate(task_id: str) -> bool:
    if not ag:
        return False
    rc, _ = ag._cli("archive", task_id)
    if rc == 0:
        ag._cli("comment", task_id, "[OBSOLETE-DUPLICATE] propuesta duplicada — archivada por approval-ready-fix")
    return rc == 0

# ---------------------------------------------------------------------------
# Main processing block – keeps each helper below 50 lines
# ---------------------------------------------------------------------------

def process_db(db: Path, execute: bool, no_dedup: bool) -> int:
    applied = 0
    records: List[dict] = []

    rows = fetch_triage(db)
    for r in rows:
        body = r["body"] or ""
        stamp_state = STAMP_RE.search(body)
        stamped = stamp_state and stamp_state.group(1).lower() == "pending"
        tagged = APPROVAL_TAG_RE.search(body)
        if not (tagged or stamped):
            continue
        new_body, filled, unfillable = fill_package(body, r["assignee"] or "")
        if unfillable:
            new_body = strip_approval_tag(new_body)
            records.append({"task": r["id"], "action": "tag-removed", "unfillable": unfillable})
            if execute:
                applied += _update_body_cas(db, r["id"], new_body)
        elif filled:
            records.append({"task": r["id"], "action": "package-filled", "filled": filled})
            if execute:
                applied += _update_body_cas(db, r["id"], new_body)

    if not no_dedup and ag:
        for group in ag.dedup_scan(db):
            for dup in group["duplicates"]:
                records.append({"task": dup, "action": "duplicate-archived", "of": group["keep"]})
                if execute and _archive_duplicate(dup):
                    applied += 1

    if execute and records:
        log_path = fix_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False)+"\n")

    if not execute and records:
        for rec in records:
            print("DRY-RUN:", json.dumps(rec, ensure_ascii=False))
    return applied

# ---------------------------------------------------------------------------
# Entry point – tiny wrapper around argument parsing
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="P5 approval‑ready backstop (dry‑run default).")
    p.add_argument("--execute", action="store_true", help="apply changes instead of printing dry run")
    p.add_argument("--dry-run", action="store_true", help="explicitly run in dry‑run mode")
    p.add_argument("--db", default=None, help="path to kanban sqlite db")
    p.add_argument("--no-dedup", action="store_true", help="skip deduplication (only package completion)")
    args = p.parse_args(argv)
    db = Path(args.db) if args.db else kanban_db_path()
    process_db(db, args.execute and not args.dry_run, args.no_dedup)
    return 0  # exit code reports execution success, not the applied count

if __name__ == "__main__":
    sys.exit(main())
