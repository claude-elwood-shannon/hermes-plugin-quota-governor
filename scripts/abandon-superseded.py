#!/usr/bin/env python3
"""abandon-superseded.py — stamp `abandoned:` onto superseded lost tasks (OBJ-08).

`objective-progress.json` (weekly-progress.py) flags an objective
needs_attention whenever a task is archived WITHOUT completed_at. That is the
right fail-loud default for a REAL loss, but it is a false alarm for an
abandoned attempt whose work was superseded: re-attempted and completed by
other tasks of the same objective (the OBJ-06 case: t_0c59b30b / t_94141677,
never executed, superseded by the t_8511285d chain; OBJ-19 crash repeats;
OBJ-09/16/18/20 retries that were re-proposed and done later).

This tool is the write side of the convention the detector reads (t_78acb6dc):
an archived task without completed_at counts as RESOLVED when its tag header
carries an explicit `abandoned: superseded-by <ids> — <reason>` line. The
stamp lives in the TAG HEADER (before the first blank line), the same
header-only convention as objective:/cost:/auto_created: tags — prose mentions
never count.

Candidate rule (conservative): an archived task with no completed_at that has
no abandoned: stamp yet is a candidate ONLY when
  (a) its own body references one or more task ids, or is referenced-by, or
      shares a normalized title with, a LATER task of the same objective that
      reached done (or archived-with-completed_at); AND
  (b) that superseding task was created AFTER the candidate (temporal guard,
      so an old orphan can never be "adopted" by an unrelated newer task); AND
  (c) the superseding task's completion is verifiable in the DB.
Losses whose work was never replaced stay unstamped -> needs_attention.

Safety (mirrors cost-tag-fix.py / privacy-router-fix.py):
  - Dry-run by default: prints decisions, writes nothing.
  - Only ever modifies the BODY of tasks with status='archived'; a live
    (running/ready/...) task is never touched. A task that flips to
    done/running between scan and write is skipped via the status-guarded
    WHERE clause (mini-CAS).
  - Never sets or clears status / completed_at — the detector reads the
    stamp; the board history stays intact.
  - The superseding ids named in the stamp are re-resolved from the DB at
    scan time and embedded verbatim, so the evidence is auditable in-place.
  - Append-only JSONL log of every applied stamp.
  - No secrets in output — task ids, titles, reasons only.

Usage:
  python3 abandon-superseded.py             # dry-run: print the stamp plan
  python3 abandon-superseded.py --execute   # apply stamps
  python3 abandon-superseded.py --only t_x,t_y   # restrict to specific ids
  python3 abandon-superseded.py --verbose   # show skipped candidates too

Exit codes:
  0 = success (including no-op)
  1 = catastrophic error (DB unreadable, etc.)

References:
  - scripts/weekly-progress.py  ABANDONED_TAG_RE (the read side)
  - scripts/cost-tag-fix.py     (sibling header-stamp pattern)
  - REPO/docs/obj06-closure-audit.md §5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
STAMP_LOG = os.path.expanduser("~/.hermes/quota-governor/abandon-stamps.jsonl")

# Tag header = block before the first blank line (triage-bridge convention).
OBJECTIVE_RE = re.compile(
    r"\bobjective\s*:\s*(OBJ-([A-Za-z0-9][A-Za-z0-9._-]*))", re.I)
ABANDONED_TAG_RE = re.compile(r"^abandoned\s*:", re.I)
TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
WHITESPACE_RE = re.compile(r"\s+")

PROTECTED_STATUSES = {"running", "ready", "todo", "triage", "blocked", "done"}
STAMPABLE_STATUSES = {"archived"}  # the only status we ever touch


def header_of(body: str) -> str:
    """Tag header: text up to the first blank line."""
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def header_bounds(lines: List[str]) -> int:
    """Index (exclusive) where the tag header ends: first blank line."""
    for i, ln in enumerate(lines):
        if not ln.strip():
            return i
    return len(lines)


def objective_of(body: str) -> Optional[str]:
    m = OBJECTIVE_RE.search(header_of(body or ""))
    return m.group(1).upper() if m else None


def has_abandoned_stamp(body: str) -> bool:
    return any(ABANDONED_TAG_RE.match(ln) for ln in header_of(body or "").splitlines())


def norm_title(title: str) -> str:
    """Normalize a task title for same-work comparison.

    Lowercase, accent-folded, whitespace-collapsed; em-dash attempt suffixes
    ("[intento 2]", "(reintentos)", trailing " — ..." parts) are stripped so
    retry generations of the same job compare equal.
    """
    t = (title or "").lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = re.sub(r"\s*[\[\(].*?(intento|reintento|fresh|retry).*?[\]\)]\s*$", "",
               t, flags=re.I)
    t = t.split("\u2014")[0].split("—")[0].strip(" :;-")
    t = WHITESPACE_RE.sub(" ", t).strip(" :;-")
    return t


def load_board(db_path: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, status, body, created_at, completed_at "
            "FROM tasks").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def is_superseding(t: Dict[str, Any]) -> bool:
    """Terminal-success state: done, or archived WITH completed_at."""
    return (t["status"] == "done"
            or (t["status"] == "archived" and t["completed_at"]))


def supersession_evidence(lost: Dict[str, Any],
                          obj_tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the supersession evidence for one lost task, or None.

    A lost task (archived, no completed_at, unstamped) is considered
    superseded when a LATER task of the same objective reached terminal
    success AND relates to it by (any of):
      - the lost task's body references the later task's id, or
      - the later task's body references the lost task's id, or
      - both share the same normalized title.
    Returns the first (earliest-created) matching later task plus the ids of
    all matching ones; None when no such task exists (a REAL loss).
    """
    created = lost["created_at"] or 0
    my_title = norm_title(lost["title"])
    my_ids = set(TASK_ID_RE.findall(lost["body"] or ""))
    matches: List[Dict[str, Any]] = []
    for u in obj_tasks:
        if u["id"] == lost["id"] or not is_superseding(u):
            continue
        if (u["created_at"] or 0) <= created:
            continue  # must be strictly later (temporal guard)
        theirs = set(TASK_ID_RE.findall(u["body"] or ""))
        related = (lost["id"] in theirs
                   or u["id"] in my_ids
                   or (my_title and my_title == norm_title(u["title"])))
        if related:
            matches.append(u)
    if not matches:
        return None
    matches.sort(key=lambda t: t["created_at"] or 0)
    return {"first": matches[0], "all": [m["id"] for m in matches]}


def build_stamp_line(reason: str, superseder_ids: List[str]) -> str:
    ids = ",".join(superseder_ids[:8])  # cap for header readability
    return f"abandoned: superseded-by {ids} — {reason}"


def plan_stamp(lost: Dict[str, Any], evidence: Dict[str, Any],
               reason: str) -> Dict[str, Any]:
    """Compute the new body with the stamp appended to the tag header."""
    body = lost["body"] or ""
    lines: List[str] = list(body.splitlines())
    end = header_bounds(lines)
    stamp = build_stamp_line(reason, evidence["all"])
    if lost.get("_stamp_line") is not None:
        # replace an existing variant (not used today; header is always stamped
        # fresh because has_abandoned_stamp() gate skips stamped tasks)
        lines[lost["_stamp_line"]] = stamp
    else:
        lines.insert(end, stamp)
    new_body = "\n".join(lines)
    if body.endswith("\n"):
        new_body += "\n"
    return {
        "id": lost["id"],
        "title": lost["title"],
        "objective": lost["_objective"],
        "superseded_by": evidence["all"],
        "first_superseder": evidence["first"]["id"],
        "stamp": stamp,
        "old_body": body,
        "new_body": new_body,
    }


def find_candidates(db_path: str) -> List[Dict[str, Any]]:
    """Scan the board and return stamp plans for unstamped superseded losses."""
    tasks = load_board(db_path)
    by_obj: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        obj = objective_of(t["body"] or "")
        if not obj or obj == "OBJ-X":
            continue
        t["_objective"] = obj
        by_obj.setdefault(obj, []).append(t)

    plans = []
    for obj, obj_tasks in sorted(by_obj.items()):
        lost = [t for t in obj_tasks
                if t["status"] in STAMPABLE_STATUSES
                and not t["completed_at"]
                and not has_abandoned_stamp(t["body"] or "")]
        for t in lost:
            ev = supersession_evidence(t, obj_tasks)
            if not ev:
                continue
            first = ev["first"]
            reason = (f"re-proposed as {first['id']} "
                      f"({first['status']}, completed_at={first['completed_at']}); "
                      f"same objective, work superseded")
            t["_stamp_line"] = None
            plans.append(plan_stamp(t, ev, reason))
    return plans


def execute_plan(conn: sqlite3.Connection, plan: Dict[str, Any]) -> bool:
    """Apply one stamp with a status-guarded UPDATE (mini-CAS)."""
    cur = conn.execute(
        "UPDATE tasks SET body = ? WHERE id = ? AND status = 'archived'",
        (plan["new_body"], plan["id"]),
    )
    conn.commit()
    return cur.rowcount == 1


def log_stamp(plan: Dict[str, Any], log_path: str) -> None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    record = {
        "ts": int(time.time()),
        "task_id": plan["id"],
        "objective": plan["objective"],
        "superseded_by": plan["superseded_by"],
        "stamp": plan["stamp"],
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stamp abandoned: on archived lost tasks whose work was "
                    "superseded (OBJ-08 detector convention). Dry-run by default.")
    ap.add_argument("--execute", action="store_true",
                    help="apply the stamps (default: dry-run, prints only)")
    ap.add_argument("--only", help="comma-separated task ids to restrict to")
    ap.add_argument("--db", default=KANBAN_DB, help="kanban.db path override")
    ap.add_argument("--log", default=STAMP_LOG, help="JSONL stamp-log path override")
    ap.add_argument("--verbose", action="store_true", help="also print non-candidates")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"ERROR: kanban.db not found at {args.db}", file=sys.stderr)
        return 1
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    try:
        plans = find_candidates(args.db)
    except sqlite3.Error as exc:
        print(f"ERROR: cannot read {args.db}: {exc}", file=sys.stderr)
        return 1
    if only:
        plans = [p for p in plans if p["id"] in only]
        if args.verbose:
            print(f"VERBOSE: --only filter: {len(plans)} plan(s) match")

    if not plans:
        if args.verbose:
            print("VERBOSE: no superseded lost tasks to stamp")
        return 0

    if not args.execute:
        for p in plans:
            print(f"DRY-RUN: stamp {p['id']} ({p['objective']}) "
                  f"superseded-by {','.join(p['superseded_by'])}")
            print(f"  header line: {p['stamp']}")
        return 0

    conn = sqlite3.connect(args.db)
    applied = 0
    try:
        for p in plans:
            if execute_plan(conn, p):
                log_stamp(p, args.log)
                print(f"stamped {p['id']} ({p['objective']}): {p['stamp']}")
                applied += 1
            else:
                print(f"SKIPPED {p['id']}: status guard rejected update "
                      "(task changed under us)", file=sys.stderr)
    finally:
        conn.close()
    if args.verbose:
        print(f"VERBOSE: {len(plans)} plan(s), {applied} applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())