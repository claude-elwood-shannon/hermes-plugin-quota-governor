#!/usr/bin/python3.12
"""triage-bridge.py — OBJ-21: deterministic triage->todo bridge (no LLM).

Promotes kanban tasks sitting in `triage` to `todo` WITHOUT any LLM call,
using hermes_cli.kanban_db.specify_triage_task() from the Hermes core, which
additionally:
  - refuses tasks parked by block_loop_detected with kind=needs_input until a
    human comment arrives (SWEEP-FIX lesson from t_9b508127),
  - writes an auditable 'specified' event,
  - recomputes readiness immediately.

Promotion criteria (ALL mandatory):
  1. status == 'triage'
  2. body header carries a valid `objective:OBJ-<id>` tag AND a valid
     `cost:` tag (micro|tiny|small|medium) — tags are the header block before
     the first blank line, exactly the format objective-proposer.py and the
     autonomous-task-creator emit (and cost-tag-fix.py backstops).
  3. NO human-approval markers in the TITLE or HEADER gate block — critical
     filter (lesson t_9b508127): never re-promote tasks that await a human
     decision ("kill switch", "hasta autorizacion", "obtener aprobación",
     "approval gate", "puerta de"). Body prose that merely mentions approval
     as a deliverable step (e.g. "the doc will be approved later") is NOT a
     gate; only title/header phrasing blocks promotion.
  4. Max 1 promotion per execution (gradual drain — never flood the board).
  5. Idempotent: every decision appended to
     ~/.hermes/quota-governor/triage-bridge.jsonl; a task already promoted or
     already decided this way is never retried blindly (the status check plus
     this ledger make double-fires no-ops).

Kill switch: ~/.hermes/quota-governor/PROMOTE-STOP (same file as autopromote).

Usage:
  triage-bridge.py            # dry-run (default): print decisions, no mutation
  triage-bridge.py --execute  # actually promote (max 1)

Cron registration (no_agent, stdout only when something promotes):
  hermes cron create triage-bridge --name triage-bridge --script triage-bridge.py \
      --no-agent --deliver local "*/30 * * * *"
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_SRC = "~/.hermes/hermes-agent"
KANBAN_DB = Path(os.environ.get("TRIAGE_BRIDGE_DB",
                                "~/.hermes/kanban.db"))
LEDGER = Path(os.environ.get("TRIAGE_BRIDGE_LEDGER",
    "~/.hermes/quota-governor/triage-bridge.jsonl"))
KILL_SWITCH = Path(os.environ.get("TRIAGE_BRIDGE_KILL_SWITCH",
                                  "~/.hermes/quota-governor/PROMOTE-STOP"))
MAX_PER_TICK = 1
VALID_COSTS = {"micro", "tiny", "small", "medium"}

# Header = the tag block before the first blank line (same convention as
# cost-tag-fix.py / objective-proposer.py).
OBJECTIVE_RE = re.compile(r"\bobjective\s*:\s*(OBJ-[A-Za-z0-9._-]+)", re.I)
COST_RE = re.compile(r"\bcost[eé]?\s*:\s*([A-Za-z]+)", re.I)

# Human-approval gate markers, matched ONLY against title + header.
APPROVAL_GATE_RE = re.compile(
    r"(kill[\s_-]?switch|hasta\s+(?:la\s+)?(?:autorizaci|aprobaci)"
    r"|(?:obtener|pendiente\s+de|requiere|esperar)\s+(?:la\s+|su\s+)?"
    r"(?:aprobaci|autorizaci)|aprobaci[oó]n\s+(?:humana|expl[ií]cita|del usuario|requerida)"
    r"|approval\s+gate|human\s+approval|puerta\s+de)",
    re.I)


def header_of(body: str) -> str:
    """Return the tag header: text up to the first blank line."""
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def parse_tags(body: str):
    """Extract (objective_id, cost_value) from the header only. None if absent."""
    h = header_of(body)
    m_obj = OBJECTIVE_RE.search(h)
    m_cost = COST_RE.search(h)
    return (m_obj.group(1) if m_obj else None,
            m_cost.group(1).lower() if m_cost else None)


def has_approval_gate(title: str, body: str) -> bool:
    gate_text = f"{title or ''}\n{header_of(body or '')}"
    return bool(APPROVAL_GATE_RE.search(gate_text))


def evaluate(title: str, body: str):
    """Return (promotable: bool, reason: str) for one triage task."""
    obj, cost = parse_tags(body)
    if not obj:
        return False, "sin tag objective:OBJ- en el header"
    if not cost:
        return False, f"{obj}: sin tag cost: en el header"
    if cost not in VALID_COSTS:
        return False, f"{obj}: cost:{cost} no valido {sorted(VALID_COSTS)}"
    if has_approval_gate(title, body):
        return False, f"{obj}: marcador de aprobacion humana en titulo/header (filtro critico t_9b508127)"
    return True, f"{obj} cost:{cost} sin gate humano"


def already_promoted(task_id: str) -> bool:
    """True if this bridge already promoted task_id (ledger is append-only)."""
    if not LEDGER.exists():
        return False
    try:
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("task") == task_id and e.get("action") == "promoted":
                return True
    except OSError:
        return False
    return False


def log(entry: dict, dry: bool = False) -> None:
    if dry:
        print("DRY:", json.dumps(entry, ensure_ascii=False))
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def fetch_triage(db_path=KANBAN_DB):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT id, title, body FROM tasks WHERE status='triage' ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def promote_via_core(db_path, task_id: str) -> bool:
    """specify_triage_task moves triage->todo, honours human gates, writes the
    auditable 'specified' event. Returns False when the core refuses."""
    if HERMES_SRC not in sys.path:
        sys.path.insert(0, HERMES_SRC)
    from hermes_cli.kanban_db import specify_triage_task
    wconn = sqlite3.connect(str(db_path))
    wconn.row_factory = sqlite3.Row
    try:
        return bool(specify_triage_task(wconn, task_id, author="triage-bridge"))
    finally:
        wconn.close()


def run(db_path=KANBAN_DB, execute: bool = False, now: str | None = None):
    """Core loop. Returns list of decision entries (for tests and reporting)."""
    now = now or datetime.now(timezone.utc).isoformat()
    decisions: list[dict] = []

    def decide(entry):
        decisions.append(entry)
        log(entry, dry=not execute)

    if KILL_SWITCH.exists():
        decide({"ts": now, "action": "skipped", "reason": "PROMOTE-STOP activo"})
        return decisions

    rows = fetch_triage(db_path)
    if not rows:
        return decisions

    promoted = 0
    for task_id, title, body in rows:
        promotable, reason = evaluate(title, body)
        if not promotable:
            decide({"ts": now, "task": task_id, "action": "kept-in-triage",
                    "reason": reason})
            continue
        if promoted >= MAX_PER_TICK:
            decide({"ts": now, "task": task_id, "action": "cap-reached",
                    "reason": f"{MAX_PER_TICK}/tick — proximo tick"} )
            continue
        if already_promoted(task_id):
            decide({"ts": now, "task": task_id, "action": "kept-in-triage",
                    "reason": "ya promocionada por este puente (ledger)"})
            continue
        if not execute:
            decide({"ts": now, "task": task_id, "action": "would-promote",
                    "reason": reason, "title": title})
            promoted += 1
            continue
        if promote_via_core(db_path, task_id):
            decide({"ts": now, "task": task_id, "action": "promoted",
                    "reason": reason, "title": title})
            print(f"TRIAGE-BRIDGE: {task_id} -> todo ({title})")
            promoted += 1
        else:
            decide({"ts": now, "task": task_id, "action": "specify-rejected",
                    "reason": "core rechazo (not in triage o human-gate pendiente)"})
    return decisions


def main(argv):
    execute = "--execute" in argv
    db = Path(os.environ.get("TRIAGE_BRIDGE_DB", str(KANBAN_DB)))
    run(db_path=db, execute=execute)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
