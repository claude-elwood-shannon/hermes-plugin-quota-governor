#!/usr/bin/env python3
"""cost-tag-fix.py — deterministic cost: tag enforcement for USD calibration (OBJ-02).

The USD cost calibration (docs/quota-planner.md § 'USD cost calibration') groups
model-cost-ledger.jsonl rows by task category, which requires a parseable
`cost:<category>` tag in the task body header.  The autonomous-task-creator cron
prompt (rule G7) already instructs the LLM to emit the tag, and
objective-proposer.py emits it statically — but LLM compliance is not
guaranteed.  This script is the deterministic backstop, same pattern as
assignee-fix.py (OBJ-08) and privacy-router-fix.py (OBJ-18 follow-up).

How it works:
  1. Scans the kanban DB for tasks whose body HEADER (the tag block before the
     first blank line) contains `auto_created:true`.  Prose mentions of
     'auto_created' or 'cost:' in the task description are deliberately NOT
     counted — the 2026-09-07 audit showed loose substring matching yields
     false positives either way.
  2. If the header has no cost tag → inject one:
       - `cost:small` if the title+body mentions medium/complex/código/codigo/implement
       - `cost:tiny` otherwise
     The tag is inserted right after the auto_created line.
  3. If the header has a NON-STANDARD cost tag (`Cost: Small`, `coste:micro`,
     `cost: small`, or a value outside {micro,tiny,small,medium,complex}) →
     normalise it in place to the canonical `cost:<category>` form (alias map:
     large/xl/high→complex, lite/minimal/low→micro/tiny).
  4. Only touches tasks whose status is not 'archived' and not 'running'
     (never mutate the body a live worker is reading).
  5. Idempotent: a canonical, correctly-formatted tag is a no-op.

Dry-run is the DEFAULT; pass --execute to apply.  Every applied change is
logged append-only to ~/.hermes/quota-governor/cost-tag-fixes.jsonl.

Usage:
  python3 cost-tag-fix.py                 # dry-run (scan + report only)
  python3 cost-tag-fix.py --execute       # apply fixes + JSONL log
  python3 cost-tag-fix.py --execute --verbose
  python3 cost-tag-fix.py --db path.db    # override DB (tests)

Exit codes:
  0 = success (including no-op)
  1 = catastrophic error (DB unreadable, etc.)

Design:
  - NEVER modifies task status or assignee — only the body tag header.
  - NEVER touches running or archived tasks.
  - Direct sqlite UPDATE (no `hermes kanban` CLI supports body edits); the
    UPDATE re-checks the status guard in its WHERE clause as a mini-CAS.
  - No secrets in output — task IDs and tag values only.

References:
  - docs/autonomous-objectives.md §OBJ-02
  - scripts/assignee-fix.py / scripts/privacy-router-fix.py (sibling pattern)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from typing import List, Optional, Tuple

# ── Config ───────────────────────────────────────────────────────────────────

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
FIX_LOG = os.path.expanduser("~/.hermes/quota-governor/cost-tag-fixes.jsonl")

# The five calibration categories (OBJ-02: micro/tiny/small/medium, plus
# 'complex' which the prompt also emits).
CANONICAL_COSTS = {"micro", "tiny", "small", "medium", "complex"}

# Values the LLM sometimes emits instead of a canonical category → canonical.
COST_ALIASES = {
    "large": "complex",
    "xl": "complex",
    "high": "complex",
    "big": "complex",
    "lite": "micro",
    "minimal": "micro",
    "low": "tiny",
    "small": "small",
    "medium": "medium",
}

# Keyword heuristic: header-missing task looks heavier → default cost:small.
HEAVY_KEYWORDS = ("medium", "complex", "código", "codigo", "implement")

# Statuses we refuse to touch: archived (frozen) and running (live worker
# may be reading/re-rendering its own body).
PROTECTED_STATUSES = {"archived", "running"}

# Header tag lines.  `(?i)` handles Cost:/COST:; `[eé]?` handles coste:.
AUTO_LINE_RE = re.compile(r"(?i)^\s*auto[_\s-]?created\s*:\s*true\s*$")
COST_LINE_RE = re.compile(r"(?i)^\s*cost[eé]?\s*:\s*(.*?)\s*$")


# ── Body analysis ────────────────────────────────────────────────────────────

def header_bounds(lines: List[str]) -> int:
    """Return index (exclusive) where the tag header ends: first blank line."""
    for i, ln in enumerate(lines):
        if not ln.strip():
            return i
    return len(lines)


def has_auto_created_header(body: str) -> bool:
    """True iff the body's tag HEADER (before the first blank line) contains
    an auto_created:true line.  Prose mentions do not count."""
    if not body:
        return False
    lines = body.splitlines()
    end = header_bounds(lines)
    return any(AUTO_LINE_RE.match(ln) for ln in lines[:end])


def normalize_cost_value(raw: str) -> Optional[str]:
    """Map a raw tag value to a canonical category, or None if unmappable."""
    v = (raw or "").strip().strip("'\"").lower()
    if v in CANONICAL_COSTS:
        return v
    return COST_ALIASES.get(v)


def infer_default_cost(full_text: str) -> str:
    """Heuristic default category for a task missing its tag entirely."""
    lowered = (full_text or "").lower()
    if any(k in lowered for k in HEAVY_KEYWORDS):
        return "small"
    return "tiny"


def analyze_body(body: str) -> Tuple[str, Optional[int], Optional[str]]:
    """Decide what a task body needs.

    Returns (action, line_index, value) where action is one of:
      'ok'        — canonical tag present & well-formed → no-op
      'skip'      — not an auto_created-header task → never touch
      'inject'    — header has auto_created but no cost line → insert `value`
      'normalize' — header cost line at `line_index` is non-canonical → replace
                    with `value` (value may be None if the tag is unmappable →
                    replaced with the inferred default)
    """
    lines = body.splitlines()
    end = header_bounds(lines)
    header = lines[:end]

    auto_idx = next((i for i, ln in enumerate(header) if AUTO_LINE_RE.match(ln)), None)
    if auto_idx is None:
        return ("skip", None, None)

    cost_match = next(
        (COST_LINE_RE.match(ln) for ln in header if COST_LINE_RE.match(ln)), None)
    if cost_match is None:
        return ("inject", auto_idx, infer_default_cost(body))

    raw = cost_match.group(1)
    cost_idx = header.index(cost_match.string)
    canon = normalize_cost_value(raw)
    # Already canonical AND exactly in the strict `cost:<value>` form → no-op.
    if canon is not None and header[cost_idx] == f"cost:{canon}":
        return ("ok", None, None)
    return ("normalize", cost_idx, canon)


def apply_fix_to_body(body: str, action: str, line_index: int, value: str) -> str:
    """Return the new body with the tag injected/normalised.  Pure function."""
    lines = body.splitlines(keepends=False)
    if action == "inject":
        # Insert right after the auto_created line, preserving it verbatim.
        lines.insert(line_index + 1, f"cost:{value}")
    elif action == "normalize":
        lines[line_index] = f"cost:{value}"
    # Preserve trailing newline behaviour: body written with \n joins.
    new = "\n".join(lines)
    if body.endswith("\n"):
        new += "\n"
    return new


# ── DB scan / write ───────────────────────────────────────────────────────────

def find_fixable_tasks(conn: sqlite3.Connection) -> List[dict]:
    """Scan non-protected tasks; return fix plans.

    Each plan: {id, status, action, line_index, value, old_body, new_body}.
    """
    plans: List[dict] = []
    rows = conn.execute(
        "SELECT id, body, status FROM tasks "
        "WHERE body LIKE '%auto%' AND body IS NOT NULL"
    ).fetchall()
    for task_id, body, status in rows:
        if status in PROTECTED_STATUSES:
            continue
        action, line_index, value = analyze_body(body)
        if action == "skip" or action == "ok":
            continue
        assert line_index is not None  # guaranteed for inject/normalize
        if action == "inject":
            resolved = value
        else:  # normalize — unmappable value falls back to the heuristic
            resolved = value or infer_default_cost(body)
        assert resolved is not None
        new_body = apply_fix_to_body(body, action, line_index, resolved)
        if new_body == body:
            continue
        plans.append({
            "id": task_id,
            "status": status,
            "action": action,
            "line_index": line_index,
            "value": resolved,
            "old_body": body,
            "new_body": new_body,
        })
    return plans


def execute_plan(conn: sqlite3.Connection, plan: dict) -> bool:
    """Apply one plan with a status-guarded UPDATE (mini-CAS).  Returns True
    if the row was actually updated."""
    cur = conn.execute(
        "UPDATE tasks SET body = ? WHERE id = ? "
        "AND status NOT IN ('archived', 'running')",
        (plan["new_body"], plan["id"]),
    )
    conn.commit()
    return cur.rowcount == 1


def log_fix(plan: dict, log_path: str) -> None:
    """Append one JSONL record per applied fix (append-only)."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    record = {
        "ts": int(time.time()),
        "task_id": plan["id"],
        "action": plan["action"],
        "cost": plan["value"],
        "status": plan["status"],
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic cost: tag enforcement for USD calibration (OBJ-02)."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply fixes (default is dry-run).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run (no-op; dry-run is already the default).",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug output.")
    parser.add_argument("--db", default=KANBAN_DB, help="kanban.db path override.")
    parser.add_argument("--log", default=FIX_LOG, help="JSONL fix-log path override.")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.db):
        if args.verbose:
            print(f"VERBOSE: kanban.db not found at {args.db}")
        return 0  # silent, nothing to do

    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.Error as exc:
        print(f"ERROR: cannot open {args.db}: {exc}", file=sys.stderr)
        return 1

    try:
        plans = find_fixable_tasks(conn)
    finally:
        pass  # keep conn for writes

    if not plans:
        if args.verbose:
            print("VERBOSE: no tasks need cost-tag fixes")
        conn.close()
        return 0

    applied = 0
    for plan in plans:
        if not args.execute:
            print(
                f"DRY-RUN: would {plan['action']} cost:{plan['value']} "
                f"on {plan['id']} ({plan['status']})"
            )
            continue
        if execute_plan(conn, plan):
            log_fix(plan, args.log)
            print(
                f"fixed {plan['id']}: {plan['action']} → cost:{plan['value']}"
            )
            applied += 1
        else:
            print(
                f"SKIPPED {plan['id']}: status guard rejected update "
                "(task changed under us)",
                file=sys.stderr,
            )

    conn.close()
    if args.verbose:
        print(f"VERBOSE: {len(plans)} plan(s), {applied} applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
