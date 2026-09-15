#!/usr/bin/python3.12
"""approval-ready-fix.py — P5 deterministic backstop (OBJ-pattern).

The cron LLM agent (autonomous-task-creator) will not always comply with
the Flujo B rules. This script enforces them in code — same pattern as
assignee-fix.py (OBJ-08) and privacy-router-fix.py (OBJ-18 follow-up):

  1. PACKAGE COMPLETION: triage tasks whose body already carries a
     [APPROVAL: pending] stamp (or an approval-ready tag) but are missing
     package fields get the missing fields filled deterministically where
     the value is derivable (cost tag -> presupuesto/cuenta; cost tag ->
     clase; objective tag -> objetivo; profile from assignee). A package
     that CANNOT be completed (no cost/objective tag at all) gets the
     `approval-ready` tag REMOVED so triage-bridge never promotes it
     blindly (it would stay human-refinement triage) — the stamp stays
     for the record.

  2. DEDUP (paste 18 §8): proposals (open tasks with a signature) that
     duplicate an OLDER open proposal are archived with comment
     `[OBSOLETE-DUPLICATE] firma <sig> de <older>`. Same fingerprint
     logic as approval_gate.py (imported).

  3. RATE LIMIT / BACKLOG-AWARE: pure read — reports in the ledger line
     whether the creator had headroom (ready+running < 3). Enforcement
     of creation limits lives in the creator prompt (G2/G6) and the
     triage-bridge cap, not here.

Dry-run by default. Every applied change appends to
~/.hermes/quota-governor/approval-fixes.jsonl. Only triage tasks are
modified (status-guarded UPDATE). Never touches running/done/archived.
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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import approval_gate as ag
except Exception:
    ag = None

_HERMES_ROOT = Path(os.environ.get("AF_HERMES_ROOT",
                                   os.path.expanduser("~/.hermes")))


def fix_log_path() -> Path:
    env = os.environ.get("AF_LOG", "").strip()
    if env:
        return Path(env)
    return _HERMES_ROOT / "quota-governor" / "approval-fixes.jsonl"

COST_RE = re.compile(r"(?im)^\s*cost[eé]?\s*:\s*(\w+)\s*$")
PROFILE_RE = re.compile(r"(?im)^\s*perfil\s*[:\-]\s*(.+)$")
COST_TO_CLASS = {"micro": "C", "tiny": "C", "small": "B", "medium": "B",
                 "complex": "A"}
COST_BUDGET_USD = {"micro": 0.05, "tiny": 0.10, "small": 0.20,
                   "medium": 0.50, "complex": 1.00}


def kanban_db_path() -> Path:
    env = os.environ.get("AF_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root = _HERMES_ROOT / "kanban.db"
    return root if root.exists() else \
        _HERMES_ROOT / "profiles" / "pr-ollama" / "kanban.db"


def _connect_ro(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _update_body_cas(db: Path, task_id: str, new_body: str) -> bool:
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "UPDATE tasks SET body=? WHERE id=? AND status='triage'",
        (new_body, task_id))
    con.commit()
    ok = cur.rowcount == 1
    con.close()
    return ok


def fill_package(body: str, assignee: str) -> tuple:
    """Return (new_body, filled:[...], unfillable:[...]) for a triage body."""
    missing = ag.package_missing(body) if ag else []
    if not missing:
        return body, [], []
    lines = []

    def val(name: Any) -> Any:
        if name == "presupuesto":
            m = COST_RE.search(body)
            return (f"presupuesto estimado: ${COST_BUDGET_USD.get(m.group(1).lower(), 0.10):.2f}"
                    f" (standing budget)" if m else None)
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
            return ("criterio de exito: (pendiente de definir por humano — "
                    "sin criterio NO se promueve)")
        if name == "clase":
            m = COST_RE.search(body)
            if m:
                return f"clase: {COST_TO_CLASS.get(m.group(1).lower(), 'C')}"
            return None
        if name == "objetivo":
            return None  # derivable only from objective: tag; absence is
            # detected by PACKAGE_FIELDS below
        return None

    new_body = body
    filled, unfillable = [], []
    for name in missing:
        line = val(name)
        if line:
            filled.append(name)
            new_body = new_body.rstrip("\n") + "\n" + line + "\n"
        else:
            unfillable.append(name)
    return new_body, filled, unfillable


def strip_approval_tag(body: str) -> str:
    return re.sub(r"(?im)^\s*approval-ready\s*$", "", body)


def archive_duplicate(task_id: str) -> bool:
    rc, _ = ag._cli("archive", task_id) if ag else (1, "")
    if rc == 0 and ag:
        ag._cli("comment", task_id,
                "[OBSOLETE-DUPLICATE] propuesta duplicada (misma firma que "
                "una anterior abierta) — archivada por approval-ready-fix")
    return rc == 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="P5 approval-ready backstop (dry-run default).")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-dedup", action="store_true",
                    help="skip dedup archiving (only package completion)")
    args = ap.parse_args(argv)
    execute = args.execute and not args.dry_run

    db = Path(args.db) if args.db else kanban_db_path()
    if ag is None:
        print("approval-ready-fix: approval_gate unavailable", file=sys.stderr)
        return 0
    if not db.exists():
        return 0  # silent

    applied = 0
    records = []

    try:
        con = _connect_ro(db)
        rows = con.execute(
            "SELECT id, title, body, assignee, status FROM tasks "
            "WHERE status='triage' ORDER BY created_at").fetchall()
        con.close()
    except sqlite3.Error:
        return 0

    for r in rows:
        body = r["body"] or ""
        tagged = bool(ag.APPROVAL_TAG_RE.search(body))
        stamped = ag.stamp_state(body) in ("pending",)
        if not (tagged or stamped):
            continue
        new_body, filled, unfillable = fill_package(body, r["assignee"] or "")
        if unfillable:
            # Cannot complete the package -> remove the tag so the bridge
            # never auto-promotes an incomplete Flujo B package.
            new_body = strip_approval_tag(new_body).rstrip("\n") + "\n"
            records.append({"ts": int(time.time()), "task": r["id"],
                            "action": "tag-removed",
                            "unfillable": unfillable})
            if execute and _update_body_cas(db, r["id"], new_body):
                applied += 1
                print(f"approval-ready-fix: {r['id']} tag removed "
                      f"(unfillable: {', '.join(unfillable)})")
        elif filled:
            records.append({"ts": int(time.time()), "task": r["id"],
                            "action": "package-filled", "filled": filled})
            if execute and _update_body_cas(db, r["id"], new_body):
                applied += 1
                print(f"approval-ready-fix: {r['id']} package completed "
                      f"({', '.join(filled)})")

    if not args.no_dedup:
        for group in ag.dedup_scan(db):
            for dup in group["duplicates"]:
                records.append({"ts": int(time.time()), "task": dup,
                                "action": "duplicate-archived",
                                "signature": group["signature"],
                                "of": group["keep"]})
                if execute and archive_duplicate(dup):
                    applied += 1
                    print(f"approval-ready-fix: {dup} archived "
                          f"(duplicate of {group['keep']})")

    if execute and records:
        fix_log = fix_log_path()
        fix_log.parent.mkdir(parents=True, exist_ok=True)
        with open(fix_log, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if not execute and records:
        for rec in records:
            print("DRY-RUN:", json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())