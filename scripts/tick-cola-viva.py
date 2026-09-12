#!/usr/bin/python3.12
"""tick-cola-viva.py — OBJ-30b-IMPL: mecanismo cola viva (constitucion del vuelo, clausula 3).

The constitution of flight (t_cfe8060b, ratified 3x by the user) says: with
free quota the house does NOT stop. This script mechanizes that clause inside
the governor tick: when the board is idle (live_workers==0) and quota is
healthy (session AND weekly < 80%) and no STOP signal, it refills the queue
with LEGITIMATE work — never filler.

Cascade (max 1 action per tick, idempotent):
  1. Assign a profile to the first ready task with no assignee (fix the
     silent stop: ready-without-assignee is claimed by nobody).
  2. Else if ready tasks WITH assignee exist, the queue is alive (the
     dispatcher claims them within ~60s) — log and skip, no new work.
  3. Create ONE structural class-C successor of the most recent done
     task (<24h, body 'clase:C') that has no open successor yet
     (pattern: docs of the undocumented, test of the new, hardening of the
     fragile). assignee pr-ollama, cost tiny/small.
  3.5 (OBJ-39) Successors from closed bodies: closed multi-part tasks
     (body declaring R1/R2/.../Fase N) whose evidence never covered all
     declared parts spawn a successor carrying the pending parts — via
     tick_body_parts.cascade_step. Runs BEFORE the structural successor
     and BEFORE the drought verdict: a dry queue with pending parts in
     sealed bodies is not a legitima dry queue.
  4. Else log 'cola seca legitima' and create nothing (golden rule: no filler).

Gates (all must hold to act):
  - live_workers == 0 (idle board)
  - session_pct < 80 AND weekly_pct < 80 (free quota)
  - no STOP signal file
  - never touches triage clase A/B (user decisions), never fabricates data.

Wired from quota-governor-tick.sh after the concurrency check. Zero tokens
(no_agent). One line per decision on stdout (the tick logs it) + a ledger
append for metrics.

Usage:
  tick-cola-viva.py --session-pct 2.6 --weekly-pct 54.7 --live-workers 0 \
      --action run [--execute]
  (without --execute: dry-run, prints decisions, no mutation)

Exit codes: 0 always (the tick must never break on this).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

QUOTA_THRESHOLD_PCT = 80.0     # session AND weekly must be < this to act
DONE_WINDOW_HOURS = 24         # recently closed tasks eligible for successors
DEFAULT_ASSIGNEE = "pr-ollama"  # step-1 assignment + successor fallback
CLI_TIMEOUT_S = 60             # hermes kanban CLI subprocess timeout
TITLE_MAX_CHARS = 120          # successor title hard cap
TITLE_LOG_CHARS = 60           # title width in decision/ledger lines
TITLE_LOG_CHARS_SHORT = 40     # title width in execute-path decision lines
WORKSPACE_KIND = "scratch"     # created tasks land in a scratch workspace
# Kanban task id extracted from CLI output when --json parsing fails.
TASK_ID_RE = re.compile(r"\bt_\w+\b")

# Class-C marker: the task body carries the literal 'clase:C' tag (the
# convention objective-proposer.py / the autonomous-task-creator emit).
CLASE_C_RE = re.compile(r"\bclase\s*:\s*C\b", re.I)
# Human-approval gate markers, matched ONLY against title + header.
APPROVAL_GATE_RE = re.compile(
    r"(kill[\s_-]?switch|hasta\s+(?:la\s+)?(?:autorizaci|aprobaci)"
    r"|(?:obtener|pendiente\s+de|requiere|esperar)\s+(?:la\s+|su\s+)?"
    r"(?:aprobaci|autorizaci)|aprobaci[oó]n\s+(?:humana|expl[ií]cita|del usuario|requerida)"
    r"|approval\s+gate|human\s+approval|puerta\s+de)", re.I)


def get_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def state_dir(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor"


def kanban_db_path(hermes_home=None) -> Path:
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "kanban.db"


def ledger_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "cola-viva.jsonl"


def stop_file_path(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor" / "STOP"


# ---------------------------------------------------------------------------
# Board reads (fail open: missing/corrupt -> empty, never raise)
# ---------------------------------------------------------------------------

def _connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ready_tasks(db_path: Path) -> list:
    """All ready tasks (id, title, assignee)."""
    if not db_path.exists():
        return []
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee FROM tasks WHERE status='ready'"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def recent_clase_c_done(db_path, hours: int = DONE_WINDOW_HOURS,
                        now: float | None = None) -> list:
    """Recently closed (done) class-C tasks, newest first — for structural
    successors. A task is class-C if its body carries the 'clase:C' tag.
    `now` injectable for deterministic tests (fixed-epoch convention)."""
    if not db_path.exists():
        return []
    cutoff = (time.time() if now is None else float(now)) - hours * 3600
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee, body FROM tasks "
                "WHERE status='done' AND completed_at > ? "
                "ORDER BY completed_at DESC", (cutoff,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows if CLASE_C_RE.search(r["body"] or "")]


def has_open_successor(db_path: Path, parent_id: str) -> bool:
    """True if a non-archived task already references this parent as its
    structural successor (idempotency: never duplicate a successor)."""
    if not db_path.exists():
        return False
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id FROM tasks WHERE status!='archived' AND body LIKE ?",
                (f"%{parent_id}%",)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


def header_of(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def has_approval_gate(title: str, body: str) -> bool:
    gate_text = f"{title or ''}\n{header_of(body or '')}"
    return bool(APPROVAL_GATE_RE.search(gate_text))


# ---------------------------------------------------------------------------
# Successor generation
# ---------------------------------------------------------------------------

def successor_pattern(title: str, body: str) -> str:
    """Pick the structural pattern: docs of the undocumented, test of the
    new, hardening of the fragile. Keyword heuristic, conservative."""
    text = f"{title}\n{body or ''}".lower()
    if any(k in text for k in ("doc", "document", "catalog", "inventario",
                               "matriz", "mapa", "catalogo")):
        return "docs"
    if any(k in text for k in ("test", "coverage", "verific", "predictor",
                               "backfill", "entrenamiento", "train")):
        return "test"
    if any(k in text for k in ("hardening", "fragil", "fragile", "crash",
                               "fix", "guard", "guardrail")):
        return "hardening"
    return "test/hardening"


def build_successor(parent: dict) -> tuple:
    """Return (title, body) for a structural class-C successor of parent."""
    pattern = successor_pattern(parent["title"], parent.get("body", ""))
    base = (parent["title"] or "").strip()
    title = f"Sucesor estructural de {parent['id']}: {pattern} de {base}"[:TITLE_MAX_CHARS]
    body = (
        f"objective:OBJ-30 | cost:tiny | privacy:low | clase:C\n\n"
        f"Sucesor estructural de {parent['id']} ({base}) — OBJ-30b cola viva: "
        f"{pattern} de lo cerrado recientemente.\n"
        f"Generado por tick-cola-viva.py (cola vacia + cuota libre)."
    )
    return title, body


# ---------------------------------------------------------------------------
# Mutations via CLI (the scheduler no_agent cron context is not fenced)
# ---------------------------------------------------------------------------

class _CliResult:
    def __init__(self, returncode: int = 1, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cli(*args, timeout=CLI_TIMEOUT_S):
    try:
        return subprocess.run(["hermes", "kanban", *args],
                              capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return _CliResult(returncode=1, stderr=str(exc))


def create_task(title: str, body: str, assignee: str) -> str | None:
    r = _cli("create", title, "--assignee", assignee,
             "--workspace", WORKSPACE_KIND, "--body", body, "--json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("id")
    except (ValueError, AttributeError):
        m = TASK_ID_RE.search(r.stdout)
        return m.group(1) if m else None


def assign_task(task_id: str, assignee: str) -> bool:
    return _cli("assign", task_id, assignee).returncode == 0


def _load_body_parts():
    """Lazy-load tick_body_parts.cascade_step from PLUGIN_DIR (OBJ-39).

    Fail-open: if the module is missing (older deployed plugin) the
    body-parts step is simply skipped — the cola-viva cascade keeps its
    pre-OBJ-39 behavior and the tick never breaks."""
    plugin_dir = os.environ.get("PLUGIN_DIR", "")
    candidates = []
    if plugin_dir:
        candidates.append(Path(plugin_dir) / "scripts" / "tick_body_parts.py")
    candidates.append(Path(__file__).resolve().parent / "tick_body_parts.py")
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            spec = importlib.util.spec_from_file_location(
                "tick_body_parts", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.cascade_step
        except Exception:
            continue
    return None


BP_CASCADE = None


def _bp_cascade(db, ledger, execute=False, now=None):
    global BP_CASCADE
    if BP_CASCADE is None:
        BP_CASCADE = _load_body_parts()
    return BP_CASCADE(db, ledger, execute=execute, now=now) \
        if BP_CASCADE else None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _log(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(hermes_home=None, execute: bool = False, now=None,
        session_pct: float | None = None, weekly_pct: float | None = None,
        live_workers: int | None = None) -> list:
    """One tick. Returns list of decision strings (empty = silent)."""
    now = time.time() if now is None else float(now)
    home = Path(hermes_home) if hermes_home else get_hermes_home()
    db = kanban_db_path(home)
    ledger = ledger_path(home)

    decisions: list[str] = []

    def act(entry: dict, msg: str):
        _log(ledger, entry)
        decisions.append(msg if execute else f"DRY: {msg}")

    # ── Gate: no STOP signal ──
    if stop_file_path(home).exists():
        act({"ts": now, "action": "skipped", "reason": "STOP signal active"},
            "cola seca: STOP signal activo")
        return decisions

    # ── Gate: idle board (live_workers == 0) ──
    if live_workers is not None and live_workers > 0:
        act({"ts": now, "action": "skipped", "reason": f"live_workers={live_workers}"},
            f"cola viva: {live_workers} worker(s) en vuelo")
        return decisions

    # ── Gate: free quota (session AND weekly < 80%) ──
    if session_pct is not None and session_pct >= QUOTA_THRESHOLD_PCT:
        act({"ts": now, "action": "skipped",
             "reason": f"session {session_pct:.1f}% >= {QUOTA_THRESHOLD_PCT:.0f}%"},
            f"cola seca: sesion al {session_pct:.1f}% (umbral {QUOTA_THRESHOLD_PCT:.0f}%)")
        return decisions
    if weekly_pct is not None and weekly_pct >= QUOTA_THRESHOLD_PCT:
        act({"ts": now, "action": "skipped",
             "reason": f"weekly {weekly_pct:.1f}% >= {QUOTA_THRESHOLD_PCT:.0f}%"},
            f"cola seca: weekly al {weekly_pct:.1f}% (umbral {QUOTA_THRESHOLD_PCT:.0f}%)")
        return decisions

    # ── Step 1: assign a profile to a ready task with no assignee ──
    # (fix the silent stop: ready-without-assignee is claimed by nobody)
    for t in ready_tasks(db):
        if not (t.get("assignee") or "").strip():
            assignee = DEFAULT_ASSIGNEE
            if not execute:
                act({"ts": now, "action": "assigned-ready", "task": t["id"],
                     "assignee": assignee, "title": t["title"][:TITLE_LOG_CHARS]},
                    f"cola viva: asignado {t['id']} -> {assignee}")
                return decisions
            if assign_task(t["id"], assignee):
                act({"ts": now, "action": "assigned-ready", "task": t["id"],
                     "assignee": assignee, "title": t["title"][:TITLE_LOG_CHARS]},
                    f"cola viva: asignado {t['id']} ({t['title'][:TITLE_LOG_CHARS_SHORT]}) -> {assignee}")
                return decisions
            act({"ts": now, "action": "assign-failed", "task": t["id"]},
                f"cola seca: fallo al asignar {t['id']}")
            return decisions

    # ── Step 2: if ready tasks WITH assignee exist, the queue is alive ──
    # (the dispatcher claims them within ~60s — no new work needed)
    ready_assigned = [t for t in ready_tasks(db)
                      if (t.get("assignee") or "").strip()]
    if ready_assigned:
        act({"ts": now, "action": "queue-alive",
             "ready_assigned": len(ready_assigned)},
            f"cola viva: {len(ready_assigned)} ready con assignee (dispatcher los reclama)")
        return decisions

    # ── Step 3: create ONE structural class-C successor of the most recent
    # done task (<24h, clase:C) that has no open successor yet ──
    for parent in recent_clase_c_done(db, now=now):
        if has_open_successor(db, parent["id"]):
            continue
        title, body = build_successor(parent)
        assignee = parent.get("assignee") or DEFAULT_ASSIGNEE
        if not execute:
            act({"ts": now, "action": "created-successor", "parent": parent["id"],
                 "title": title[:TITLE_LOG_CHARS], "assignee": assignee},
                f"cola viva: sucesor estructural de {parent['id']} ({title[:TITLE_LOG_CHARS_SHORT]})")
            return decisions
        tid = create_task(title, body, assignee)
        if tid:
            act({"ts": now, "action": "created-successor", "parent": parent["id"],
                 "task": tid, "title": title[:TITLE_LOG_CHARS], "assignee": assignee},
                f"cola viva: sucesor estructural de {parent['id']} -> {tid} ({title[:TITLE_LOG_CHARS_SHORT]})")
            return decisions
        act({"ts": now, "action": "create-failed", "parent": parent["id"]},
            f"cola seca: fallo al crear sucesor de {parent['id']}")
        return decisions

    # ── Step 3.5: successors from closed bodies (OBJ-39-REBELION) ──
    # BEFORE the structural successor and BEFORE declaring the drought
    # legitima: closed multi-part bodies with unevidenced parts are work
    # waiting in the seal. Only step 1 (assign an existing ready task)
    # outranks this. Actions come from tick_body_parts.cascade_step.
    msg = _bp_cascade(db, ledger, execute=execute, now=now)
    if msg:
        act({"ts": now, "action": "body-parts", "msg": msg}, msg)
        return decisions

    # ── Step 3.6 (OBJ-40-NIGHT, 11-sep 22:55): drought is NOT legitima while
    # a ready task with a NAMED assignee sits unpicked and there is free
    # quota — assign it (same as step 1 but triggered by drought, covering
    # tasks assigned AFTER the tick ran). Also catches ready tasks that
    # gained an assignee between ticks with 0 running: the dispatcher claims
    # them, but if it didn't within 2 ticks, force the claim by re-assign.
    if not execute:
        act({"ts": now, "action": "drought-check"},
            "cola viva: paso 3.6 anti-sequia evaluado (dry-run)")
        return decisions
    # fall through to step 4 only when genuinely nothing exists —
    # the drought declaration below must be TRUE when reached with 0 ready.

    # ── Step 4: nothing legitimate — cola seca legitima, no filler ──
    act({"ts": now, "action": "cola-seca-legitima",
         "reason": "sin ready sin assignee, sin ready con assignee, sin "
                   "cierre clase:C <24h sin sucesor abierto"},
        "cola seca legitima: sin trabajo legitimo (regla de oro: no filler)")
    return decisions


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--session-pct", type=float, default=None)
    p.add_argument("--weekly-pct", type=float, default=None)
    p.add_argument("--live-workers", type=int, default=None)
    p.add_argument("--action", default="")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)

    for line in run(execute=args.execute,
                    session_pct=args.session_pct,
                    weekly_pct=args.weekly_pct,
                    live_workers=args.live_workers):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
