#!/usr/bin/python3.12
"""fondo-queue-watch.py — OBJ-30b: anti-parada mechanism during budget windows.

Watchdog (no_agent cron, zero tokens): while a FONDO WINDOW is active — a task
carrying a "Contrato de la ventana" contract is open on the board (status
running/ready/todo) — AND the ready+running queue has been EMPTY for longer
than EMPTY_GRACE_MIN, apply the anti-stall cascade so the board never idles
with free quota:

  1. Promote the next phase of the objective with an approved plan, if the
     window task body lists one (Cartera / Fases section).
  2. Else create a structural class-C successor (test/doc/hardening) of a
     recently closed task (OBJ-29 layer-2 pattern).
  3. Else take the first ready-able class-C triage task and ASSIGN it a
     profile (never ready-without-assignee while quota is free).
  4. Else annotate 'cola seca + supply_ratio' on the window task and STOP
     (filler is forbidden — the golden rule).

State: an append-only JSONL ledger (every decision) + a small state file
tracking when the queue became empty, so the >30min grace is MEASURED, not
guessed. The state file is the only mutable thing besides the board.

Watchdog contract: SILENT stdout = nothing to do. Non-empty stdout = an
action was taken (the cron layer delivers it). Exit 0 always.

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times are epoch UTC; the
grace window compares epoch, never local clocks.

Usage:
  fondo-queue-watch.py            # dry-run (default): print decisions, no mutation
  fondo-queue-watch.py --execute  # actually act (max 1 action per tick)
"""
from __future__ import annotations

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

EMPTY_GRACE_MIN = 30          # queue empty for > this long -> act
MAX_ACTIONS_PER_TICK = 1      # warm-up-then-scale: one action per tick
WINDOW_CONTRACT_MARKER = "Contrato de la ventana"  # window task body marker
VALID_COSTS = {"micro", "tiny", "small", "medium"}
SMALL_COSTS = {"micro", "tiny", "small"}           # class-C promotable volume
ACTIVE_STATUSES = ("running", "ready", "todo")     # window task is "open"
QUEUE_STATUSES = ("ready", "running")              # the live queue

# Class-C keyword heuristic (mirrors autopromote.py, conservative).
CLASS_C_KEYWORDS = re.compile(
    r"\b(docs?|documentaci|catalog|inventario|investigaci|research|test coverage|"
    r"cobertura de tests|matriz|observabilidad|audit|hardening|test)\b", re.I)
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


def _get_hermes_root() -> Path:
    """Shared root (~/.hermes) for the board, which lives OUTSIDE profiles.

    The cron env sets HERMES_HOME to the profile (…/profiles/pr-ollama);
    the kanban board is a shared resource at ~/.hermes/kanban.db, so a
    bare HERMES_HOME/kanban.db points at a nonexistent file (t_99e3b849).
    Mirrors health_checks._get_hermes_root / concurrency_guard."""
    val = os.environ.get("HERMES_HOME", "").strip()
    home = Path(val).resolve() if val else (Path.home() / ".hermes").resolve()
    profiles_root = (Path.home() / ".hermes" / "profiles").resolve()
    try:
        home.relative_to(profiles_root)
        return (Path.home() / ".hermes").resolve()
    except ValueError:
        return home


def kanban_db_path(hermes_home=None) -> Path:
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()
    if hermes_home:
        return Path(hermes_home) / "kanban.db"
    return _get_hermes_root() / "kanban.db"


def ledger_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "fondo-queue-watch.jsonl"


def state_file_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "fondo-queue-watch-state.json"


# ---------------------------------------------------------------------------
# Board reads (fail open: missing/corrupt -> empty, never raise)
# ---------------------------------------------------------------------------

def _connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def find_window_tasks(db_path: Path) -> list:
    """Tasks that represent an active fondo window.

    A window task carries the contract marker in its body AND is open
    (running/ready/todo). The mechanism task itself (OBJ-30b) does NOT carry
    the marker, so it never self-triggers.
    """
    if not db_path.exists():
        return []
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee, status, body FROM tasks "
                "WHERE status IN ('running','ready','todo')").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows
            if WINDOW_CONTRACT_MARKER in (r["body"] or "")]


def queue_counts(db_path: Path, exclude_ids: set | None = None) -> dict:
    """Counts of ready and running tasks (the live queue).

    ``exclude_ids``: task ids to exclude from the count — the fondo window
    task(s) themselves are running but are NOT the work queue; the queue is
    the *other* work. Without this, a window task would make the queue look
    permanently non-empty and the anti-stall mechanism would never fire.
    """
    exclude_ids = exclude_ids or set()
    if not db_path.exists():
        return {"ready": 0, "running": 0}
    try:
        con = _connect(db_path)
        try:
            if exclude_ids:
                ph = ",".join("?" * len(exclude_ids))
                row = con.execute(
                    f"SELECT SUM(status='ready'), SUM(status='running') "
                    f"FROM tasks WHERE id NOT IN ({ph})",
                    tuple(exclude_ids)).fetchone()
            else:
                row = con.execute(
                    "SELECT SUM(status='ready'), SUM(status='running') "
                    "FROM tasks").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return {"ready": 0, "running": 0}
    return {"ready": row[0] or 0, "running": row[1] or 0}


def recent_done_tasks(db_path: Path, hours: int = 24) -> list:
    """Recently closed (done) tasks, newest first — for structural successors."""
    if not db_path.exists():
        return []
    cutoff = time.time() - hours * 3600
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
    return [dict(r) for r in rows]


def triage_tasks(db_path: Path) -> list:
    """All triage tasks, oldest first."""
    if not db_path.exists():
        return []
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee, body FROM tasks "
                "WHERE status='triage' ORDER BY created_at").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _norm(s: str) -> str:
    """Normalize a title for fuzzy matching: lowercase, drop non-alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def task_exists(db_path: Path, title: str) -> bool:
    """True if a non-archived task with this title (fuzzy) already exists.

    Fuzzy: normalized substring match — the cartera may list 'OBJ-27 F1 — …'
    while the board task is titled 'OBJ-27-F1: informe…'. Normalizing both
    (lowercase, drop non-alphanumerics) makes 'OBJ-27 F1' match 'OBJ-27-F1'.
    """
    if not db_path.exists():
        return False
    target = _norm(title)
    if not target:
        return False
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT title FROM tasks WHERE status!='archived'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return any(target in _norm(r["title"]) for r in rows)


# ---------------------------------------------------------------------------
# Classification helpers (mirror autopromote.py / triage-bridge.py)
# ---------------------------------------------------------------------------

def header_of(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def parse_cost(body: str):
    m = re.search(r"cost[eé]?\s*:\s*([A-Za-z]+)", header_of(body or ""), re.I)
    return m.group(1).lower() if m else None


def is_class_c(title: str, body: str) -> bool:
    text = f"{title}\n{body or ''}"
    if not CLASS_C_KEYWORDS.search(text):
        return False
    cost = parse_cost(body)
    return cost in SMALL_COSTS


def has_approval_gate(title: str, body: str) -> bool:
    gate_text = f"{title or ''}\n{header_of(body or '')}"
    return bool(APPROVAL_GATE_RE.search(gate_text))


def first_readyable_triage(triage: list) -> dict | None:
    """First triage task that is class-C, small volume, no approval gate."""
    for t in triage:
        if is_class_c(t["title"], t["body"]) and not has_approval_gate(
                t["title"], t["body"]):
            return t
    return None


# ---------------------------------------------------------------------------
# State (empty-since tracking)
# ---------------------------------------------------------------------------

def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _log(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Actions (mutations via CLI — the cron scheduler context is not fenced)
# ---------------------------------------------------------------------------

class _CliResult:
    """Minimal stand-in for subprocess.CompletedProcess on CLI failure."""
    def __init__(self, returncode: int = 1, stdout: str = "",
                 stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cli(*args, timeout=60):
    try:
        return subprocess.run(["hermes", "kanban", *args],
                              capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return _CliResult(returncode=1, stderr=str(exc))


def create_task(title: str, body: str, assignee: str) -> str | None:
    r = _cli("create", title, "--assignee", assignee, "--workspace", "scratch",
             "--body", body, "--json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("id")
    except (ValueError, AttributeError):
        m = re.search(r"(t_\w+)", r.stdout)
        return m.group(1) if m else None


def assign_task(task_id: str, assignee: str) -> bool:
    return _cli("assign", task_id, assignee).returncode == 0


def comment_task(task_id: str, body: str) -> bool:
    return _cli("comment", task_id, body).returncode == 0


# ---------------------------------------------------------------------------
# Cascade steps
# ---------------------------------------------------------------------------

def _parse_cartera(body: str) -> list:
    """Extract candidate next-phase titles from a Cartera/Fases section.

    Lines like '1. OBJ-27 F1 — INFORME...' or '- OBJ-29 supply_ratio ...'.
    Returns the full item text (trimmed, up to a ':' or '—' separator) as a
    candidate title — the whole phase name, not just the leading identifier.
    """
    out = []
    in_section = False
    for line in (body or "").splitlines():
        s = line.strip()
        if re.match(r"^#{1,3}\s*(Cartera|Fases|Cartera de la ventana)", s, re.I):
            in_section = True
            continue
        if in_section and re.match(r"^#{1,3}\s", s):
            break
        if not in_section:
            continue
        m = re.match(r"^\s*(?:\d+[.)]|[-*])\s*(.+)$", s)
        if not m:
            continue
        item = m.group(1).strip()
        # Trim at a ':' or '—' separator (keeps the phase name, drops the
        # trailing description).
        item = re.split(r"\s*[:—]\s*", item, maxsplit=1)[0].strip()
        if item:
            out.append(item)
    return out


def step1_promote_next_phase(window, db_path, execute: bool = False) -> str | None:
    """Promote the next phase of the objective with an approved plan.

    If the window task body lists a Cartera/Fases and a listed phase is not
    already on the board, create it as a ready task with the window's assignee.
    In dry-run (execute=False) it reports the plan without mutating.
    """
    assignee = window.get("assignee") or "pr-ollama"
    for cand in _parse_cartera(window.get("body", "")):
        if not cand:
            continue
        if task_exists(db_path, cand):
            continue
        body = (f"objective:OBJ-30 | cost:tiny | privacy:low | clase:C\n\n"
                f"Fase siguiente del fondo (OBJ-30b anti-parada): {cand}.\n"
                f"Promovida automaticamente por fondo-queue-watch.py — la "
                f"cartera del fondo estaba vacia y esta fase estaba planificada.")
        if not execute:
            return f"promoted-next-phase {cand} (assignee={assignee})"
        tid = create_task(cand, body, assignee)
        if tid:
            return f"promoted-next-phase {cand} -> {tid} (assignee={assignee})"
    return None


def step2_structural_successor(done_tasks, db_path, execute: bool = False) -> str | None:
    """Create a structural class-C successor (test/doc/hardening) of a
    recently closed task (OBJ-29 layer-2 pattern)."""
    for t in done_tasks:
        base = t["title"]
        cand = f"Test/hardening: {base}"
        if task_exists(db_path, cand):
            continue
        assignee = t.get("assignee") or "pr-ollama"
        body = (f"objective:OBJ-30 | cost:tiny | privacy:low | clase:C\n\n"
                f"Sucesor estructural de {t['id']} ({base}) — OBJ-29 capa 2: "
                f"test/doc/hardening de lo cerrado recientemente.\n"
                f"Promovido por fondo-queue-watch.py (cola vacia en ventana de fondo).")
        if not execute:
            return f"structural-successor {cand} (assignee={assignee})"
        tid = create_task(cand, body, assignee)
        if tid:
            return f"structural-successor {cand} -> {tid} (assignee={assignee})"
    return None


def step3_assign_triage(triage, execute: bool = False) -> str | None:
    """Take the first ready-able class-C triage task and ASSIGN it a profile
    (never ready-without-assignee while quota is free)."""
    t = first_readyable_triage(triage)
    if not t:
        return None
    assignee = "pr-ollama"  # default; the rule is "assign a profile"
    if not execute:
        return f"assigned-triage {t['id']} ({t['title'][:40]}) -> {assignee}"
    if assign_task(t["id"], assignee):
        return f"assigned-triage {t['id']} ({t['title'][:40]}) -> {assignee}"
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(hermes_home=None, execute: bool = False, now=None) -> list:
    """One tick. Returns list of decision strings (empty = silent)."""
    now = time.time() if now is None else float(now)
    home = Path(hermes_home) if hermes_home else get_hermes_home()
    # Board resolution: only pin the DB from an EXPLICIT hermes_home arg
    # (tests pass a fixture dir). In production main() passes nothing and
    # kanban_db_path() resolves the shared root board from the env.
    db = kanban_db_path(home) if hermes_home else kanban_db_path()
    ledger = ledger_path(home)
    state_path = state_file_path(home)

    decisions: list[str] = []

    def act(entry: dict, msg: str):
        _log(ledger, entry)
        if execute:
            decisions.append(msg)
        else:
            decisions.append(f"DRY: {msg}")

    # 1. Is a fondo window active?
    windows = find_window_tasks(db)
    if not windows:
        _log(ledger, {"ts": now, "action": "no-window",
                      "reason": "no fondo window active"})
        return decisions  # silent: not in a window

    window = windows[0]

    # 2. Is the queue empty? (exclude the window task(s) themselves — they are
    # running but are NOT the work queue; the queue is the *other* work.)
    q = queue_counts(db, exclude_ids={w["id"] for w in windows})
    if q["ready"] + q["running"] > 0:
        # Queue has work — reset the empty-since marker, stay silent.
        _write_state(state_path, {"empty_since": None, "window": window["id"]})
        _log(ledger, {"ts": now, "action": "queue-nonempty",
                      "ready": q["ready"], "running": q["running"]})
        return decisions

    # 3. How long has it been empty?
    state = _read_state(state_path)
    empty_since = state.get("empty_since")
    if empty_since is None:
        _write_state(state_path, {"empty_since": now, "window": window["id"]})
        _log(ledger, {"ts": now, "action": "queue-empty-start",
                      "empty_since": now})
        return decisions  # first empty tick: start the clock, wait

    empty_min = (now - empty_since) / 60.0
    if empty_min <= EMPTY_GRACE_MIN:
        _log(ledger, {"ts": now, "action": "queue-empty-grace",
                      "empty_min": round(empty_min, 1)})
        return decisions  # within grace: keep waiting

    # 4. Grace exceeded — apply the cascade (max 1 action per tick).
    # Step 1: promote next phase of the objective with approved plan.
    msg = step1_promote_next_phase(window, db, execute=execute)
    if msg:
        act({"ts": now, "action": "step1", "detail": msg}, msg)
        return decisions

    # Step 2: structural class-C successor of a recently closed task.
    msg = step2_structural_successor(recent_done_tasks(db), db, execute=execute)
    if msg:
        act({"ts": now, "action": "step2", "detail": msg}, msg)
        return decisions

    # Step 3: assign the first ready-able class-C triage task a profile.
    msg = step3_assign_triage(triage_tasks(db), execute=execute)
    if msg:
        act({"ts": now, "action": "step3", "detail": msg}, msg)
        return decisions

    # Step 4: nothing legitimate — annotate and STOP (filler forbidden).
    note = (f"cola seca + supply_ratio: cola ready+running vacia "
            f"{empty_min:.0f} min en ventana de fondo; sin fase planificada, "
            f"sin sucesor estructural, sin triage clase C ready-able. "
            f"Parada legitima (regla de oro: no filler).")
    if execute:
        comment_task(window["id"], note)
    act({"ts": now, "action": "step4-stop", "detail": note}, f"STOP: {note}")
    return decisions


def main(argv=None) -> int:
    execute = "--execute" in (argv or sys.argv[1:])
    for line in run(execute=execute):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
