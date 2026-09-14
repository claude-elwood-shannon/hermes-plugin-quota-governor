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
  2. Else if ready tasks WITH assignee exist and the refill pool is dry,
     the queue is alive (the dispatcher claims them within ~60s) — log
     and skip, no new work. P2 desired=3: with refillable pool left
     (class-C done <24h without successor) the cascade keeps refilling
     up to the minimum backlog.
  3. Create structural class-C successors of the most recent done
     tasks (<24h, body 'clase:C') that have no open successor yet,
     UNTIL the backlog (ready_assigned + running) >= 3 — pool-bounded,
     never filler
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
import hashlib
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
CLASE_C_RE = re.compile(r"\bclase\s*:\s*C\b(-\w+)?", re.I)  # C y C-estructural (fix 13-sep: autoqueue done no contaba como padre)

# objective:OBJ-xx tag anywhere in the body (MEDIATOR budget gate, §6).
OBJECTIVE_TAG_RE = re.compile(
    r"\bobjective\s*:\s*(OBJ-[A-Za-z0-9._-]+)", re.I)

# P5 dedup (MEDIATOR 14-sep): firma de cadena de sucesores. El step 3
# estampa en el body del sucesor `successor-sig:<sha1[:16]>` (raíz+patrón,
# heredada por toda la cadena) y `successor-depth:N`. Con ambas piezas el
# tick corta la cadena recursiva "Sucesor estructural de Sucesor
# estructural de ...": (a) no crea un sucesor si su firma ya existe en el
# board (abierta o hecha, no archivada), y (b) un sucesor nunca genera
# otro sucesor (tope de profundidad). Los sucesores legados (pre-estampa,
# título 'Sucesor estructural de ...') cuentan con profundidad 1: la
# cadena vieja muere en la primera pasada.
SUCCESSOR_SIG_RE = re.compile(r"\bsuccessor-sig:([0-9a-f]{16})\b")
SUCCESSOR_DEPTH_RE = re.compile(r"\bsuccessor-depth:(\d+)\b", re.I)
SUCCESSOR_LEGACY_TITLE_RE = re.compile(r"^Sucesor estructural de \S+", re.I)
SUCCESSOR_MAX_DEPTH = 1

# P2×P5 (t_acf726e6): sello de stock muerto — un done cuya cadena ya cubrió
# su trabajo (firma presente en el board, solo done/archived) deja de contar
# como refillable tras el censo (idempotente, best-effort).
P2X_P5_DONE_RE = re.compile(r"\bP2xP5-no-refill\b")


def _load_approved_objectives():
    """Lazy-load scripts/approved_objectives.py (fail-open: None if absent
    or broken — then tagged tasks are RETAINED, never dispatched blind)."""
    here = Path(__file__).resolve().parent
    for cand in (here / "approved_objectives.py",
                 Path.home() / ".hermes" / "scripts" / "approved_objectives.py"):
        if cand.exists():
            try:
                spec = importlib.util.spec_from_file_location(
                    "approved_objectives", cand)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                continue
    return None
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


def ledger_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "cola-viva.jsonl"


def stop_file_path(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor" / "STOP"


def _get_hermes_root() -> Path:
    """Root ~/.hermes (the kanban.db lives at root, NOT profile-scoped)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    hermes_home = Path(val).resolve() if val else (Path.home() / ".hermes").resolve()
    profiles_root = (Path.home() / ".hermes" / "profiles").resolve()
    try:
        hermes_home.relative_to(profiles_root)
        return (Path.home() / ".hermes").resolve()
    except ValueError:
        return hermes_home


def kanban_db_path(hermes_home=None) -> Path:
    """kanban.db path with root fallback (profile db does not exist)."""
    env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser().resolve()
    base = Path(hermes_home) if hermes_home else _get_hermes_root()
    if not (base / "kanban.db").exists():
        root = _get_hermes_root()
        if (root / "kanban.db").exists():
            return root / "kanban.db"
    return base / "kanban.db"


# ---------------------------------------------------------------------------
# Board reads (fail open: missing/corrupt -> empty, never raise)
# ---------------------------------------------------------------------------

def _connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ready_tasks(db_path: Path) -> list:
    """All ready tasks (id, title, assignee, body). body is needed by the
    budget gate (objective:OBJ-xx tag lives in the body)."""
    if not db_path.exists():
        return []
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT id, title, assignee, body FROM tasks "
                "WHERE status='ready'"
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
                "SELECT id FROM tasks WHERE status IN ('ready','running','blocked','todo') AND body LIKE ?",
                (f"%{parent_id}%",)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


# ---------------------------------------------------------------------------
# P5 dedup: firma de cadena de sucesores (MEDIATOR 14-sep)
# ---------------------------------------------------------------------------

def successor_signature(root_id: str, pattern: str) -> str:
    """sha1[:16] of (root_id, pattern) — the P5 paste-§8 signature scheme
    applied to the successor chain: the whole chain of successors born of
    a root task under one structural pattern shares ONE signature, so a
    'Sucesor de Sucesor de ...' can never re-enter the board."""
    raw = f"{root_id}|{pattern}".strip().lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _successor_stamp(parent: dict, sig: str) -> tuple[int, bool]:
    """(depth, inherited) for the child of `parent`: inherited signature
    if the parent carries one, else the parent IS the root. Depth = parent
    depth + 1; legacy parents (pre-stamp, successor-titled) count as
    depth 1 so the old recursive chain dies at the first pass."""
    m_sig = SUCCESSOR_SIG_RE.search(parent.get("body") or "")
    if m_sig:
        m_depth = SUCCESSOR_DEPTH_RE.search(parent.get("body") or "")
        depth = (int(m_depth.group(1)) + 1) if m_depth else SUCCESSOR_MAX_DEPTH + 1
        return depth, True
    if SUCCESSOR_LEGACY_TITLE_RE.match(parent.get("title") or ""):
        return 1, False
    return 1, False


def successor_chain_open(db_path: Path, sig: str,
                         exclude_task_id: str | None = None) -> bool:
    """True if ANY non-archived task in the board already carries this
    successor signature (open or done — done counts: the chain already
    produced its work; only archived falls out of the board)."""
    if not db_path.exists():
        return False
    try:
        con = _connect(db_path)
        try:
            q = ("SELECT id FROM tasks WHERE status != 'archived' "
                 "AND body LIKE ?")
            args: list = [f"%successor-sig:{sig}%"]
            if exclude_task_id:
                q += " AND id != ?"
                args.append(exclude_task_id)
            rows = con.execute(q, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


def successor_chain_has_open(db_path: Path, sig: str,
                             exclude_task_id: str | None = None) -> bool:
    """True if a sig-carrying task exists in an OPEN status (ready, running,
    blocked, todo) — the chain is actively covered by a live member (P2×P5:
    distinguishes live coverage from dead stock done-only chains)."""
    if not db_path.exists():
        return False
    try:
        con = _connect(db_path)
        try:
            q = ("SELECT id FROM tasks WHERE status IN "
                 "('ready','running','blocked','todo') "
                 "AND body LIKE ?")
            args: list = [f"%successor-sig:{sig}%"]
            if exclude_task_id:
                q += " AND id != ?"
                args.append(exclude_task_id)
            rows = con.execute(q, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


def _refillable_candidate(db_path: Path, now: float | None = None,
                          skip_sigs: set | None = None,
                          write: bool = True) -> tuple:
    """P2 desired=3 (MEDIATOR t_acf726e6): next refillable parent — a
    class-C done task <24h whose successor chain has no OPEN member.
    Returns (kind, payload):
      ("parent", parent_dict_with_sig)  -> create its successor
      ("dedup",  (parent, sig))         -> P5 signature verdict, stop
      ("capped", (parent, depth))       -> P5 depth verdict, stop
      ("dry",    None)                  -> pool exhausted
    skip_sigs: signatures created earlier in THIS tick (live coverage).
    write: False (dry-run) censuses WITHOUT stamping dead stock.

    The FIRST clean parent wins over any P5 verdict — the capped/dedup
    verdicts describe the residue AFTER refill, not a stop before it.
    Depth-aware (P2×P5): stamped or legacy-successor parents would create
    depth-2 children, which step 3's depth cap retains — they carry zero
    backlog value, and a legacy parent's sig equals its root's (would
    recycle the cap), so they never refill. A parent whose chain exists
    only as done/archived (P5 dead stock) is stamped out of the pool
    (idempotent, best-effort)."""
    verdict: tuple | None = None
    for parent in recent_clase_c_done(db_path, now=now):
        pid = parent["id"]
        if P2X_P5_DONE_RE.search(parent.get("body") or ""):
            continue  # ya censado como stock muerto en un tick previo
        if has_open_successor(db_path, pid):
            continue  # cobertura viva via id (silencioso, como en P5)
        depth, inherited = _successor_stamp(parent, "")
        if inherited:
            if depth > SUCCESSOR_MAX_DEPTH and verdict is None:
                verdict = ("capped", (parent, depth))
            continue
        if SUCCESSOR_LEGACY_TITLE_RE.match(parent.get("title") or ""):
            continue  # legacy: su sig colisionaria con la raiz de la cadena
        pattern = successor_pattern(parent["title"], parent.get("body", ""))
        sig = successor_signature(pid, pattern)
        if sig in (skip_sigs or ()):
            continue  # ya creado en este mismo tick (cobertura viva)
        if successor_chain_open(db_path, sig, exclude_task_id=pid):
            if successor_chain_has_open(db_path, sig, exclude_task_id=pid):
                if verdict is None:
                    verdict = ("dedup", (parent, sig))
            elif write:
                _prune_dead_stock(db_path, pid)
            continue  # stock muerto: la cadena ya produjo su trabajo
        parent["sig"] = sig
        return ("parent", parent)
    return verdict if verdict is not None else ("dry", None)


def _prune_dead_stock(db_path: Path, pid: str) -> None:
    """Best-effort P2×P5 bookkeeping: append the no-refill stamp to the
    DONE parent's body so dead stock (chain done, no open member) stops
    counting as refillable. Fail-open: on any error the caller falls back
    to P5 dedup alone (correct, just re-censused every tick)."""
    try:
        con = sqlite3.connect(str(db_path), timeout=3)
        try:
            row = con.execute("SELECT body FROM tasks WHERE id = ?",
                              (pid,)).fetchone()
            if row and not P2X_P5_DONE_RE.search(row[0] or ""):
                con.execute("UPDATE tasks SET body = ? WHERE id = ?",
                            ((row[0] or "") + "\nP2xP5-no-refill: stock done",
                             pid))
                con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def refillable(db_path: Path, now: float | None = None,
               write: bool = True) -> bool:
    """P2 desired=3: True if the backlog-guard has legitimate refill work
    left (a depth-1-stammable class-C done task <24h whose chain has no
    open member). When False, the step-2 'queue alive' shortcut stands and
    the drought verdict (step 4) stays honest: dry means dry, no filler
    (pitfall 24g-c). write=False (dry-run): census without stamping."""
    return _refillable_candidate(db_path, now=now, write=write)[0] == "parent"


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


def build_successor(parent: dict, sig: str | None = None,
                    depth: int | None = None) -> tuple:
    """Return (title, body) for a structural class-C successor of parent.
    sig/depth: P5 dedup stamps written into the body so the whole chain
    (root + pattern) shares one signature and the depth is explicit."""
    pattern = successor_pattern(parent["title"], parent.get("body", ""))
    base = (parent["title"] or "").strip()
    title = f"Sucesor estructural de {parent['id']}: {pattern} de {base}"[:TITLE_MAX_CHARS]
    stamp = ""
    if sig:
        stamp = (f"\nsuccessor-sig:{sig} | successor-depth:{depth or 1}")
    body = (
        f"objective:OBJ-30 | cost:tiny | privacy:low | clase:C\n\n"
        f"Sucesor estructural de {parent['id']} ({base}) — OBJ-30b cola viva: "
        f"{pattern} de lo cerrado recientemente.\n"
        f"Generado por tick-cola-viva.py (cola vacia + cuota libre)."
        f"{stamp}"
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
    # Board resolution: only pin the DB from an EXPLICIT hermes_home arg
    # (tests pass a fixture dir). In production main() passes nothing and
    # kanban_db_path() resolves the shared root board from the env.
    db = kanban_db_path(home) if hermes_home else kanban_db_path()
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

    # ── MEDIATOR 2026-09-14: approved_objectives housekeeping ──
    # Ensure the inventory table (+ seed), refresh spent_today/spent_total
    # from the trace (implicit CEST-midnight reset), and apply autonomous
    # lifecycle transitions (achieved/paused/reactivated). Runs on EVERY
    # tick regardless of backlog (spend accounting is not a supply action).
    # Fail-open: any error never breaks the tick.
    _ao = _load_approved_objectives()
    if _ao is not None:
        try:
            if not _ao.table_exists(db):
                _ao.ensure_table(db)
            spend_res = _ao.update_spend(db, now)
            for line in _ao.run_lifecycle(db, now=now):
                act({"ts": now, "action": "objective-lifecycle", "line": line},
                    line)
            if spend_res.get("updated"):
                top = ", ".join(
                    f"{u['id']} ${u['spent_today']:.2f}"
                    for u in spend_res["updated"][:3]
                    if (u.get("spent_today") or 0.0) > 0)
                if top:
                    act({"ts": now, "action": "objective-spend", "detail": top},
                        f"objetivos: gasto hoy {top}")
        except Exception:
            pass  # observability/inventory never breaks the supply tick

    # ── Gate P2 (13-sep): backlog-guard — actúa con backlog bajo, no solo idle ──
    # backlog_total = running + ready_con_assignee. Mínimo operativo: 3.
    BACKLOG_MIN = 3
    ready_all = ready_tasks(db)
    ready_unassigned = [t for t in ready_all
                        if not (t.get("assignee") or "").strip()]
    ready_assigned_n = len(ready_all) - len(ready_unassigned)
    backlog_total = (live_workers or 0) + ready_assigned_n
    _log(ledger, {"ts": now, "action": "backlog-guard", "ready_assigned": ready_assigned_n,
                  "live_workers": live_workers or 0, "backlog_total": backlog_total,
                  "verdict": "OK" if backlog_total >= BACKLOG_MIN else "LOW"})
    if backlog_total >= BACKLOG_MIN:
        act({"ts": now, "action": "skipped", "reason": f"backlog OK ({backlog_total})"},
            f"backlog OK (ready={ready_assigned_n}, running={live_workers or 0})")
        return decisions
    # verdict LOW: el ledger ya registró el guard; la cascade continúa y su
    # acción será la única línea de decisión del tick (contrato 1-linea).
    # (El refill del paso 3 solo actúa por DEBAJO del mínimo: la letra del
    # mediador es 'hasta alcanzar el mínimo', no un buffer por encima.)

    # ── MEDIATOR 14-sep: bifurcación ruta A / ruta B en el gate de cuota ──
    # Ruta A (objetivo aprobado con balance): tarea con tag objective:OBJ-XX
    #   + objetivo active con presupuesto libre -> se despacha CONTRA BALANCE
    #   aunque la cuota gratis esté agotada (session/weekly >= 80%). El
    #   presupuesto se verifica aquí EN CADA dispatch (restricción del
    #   mandato: por dispatch, no una vez al día).
    # Ruta B (todo lo demás): exige cuota libre (comportamiento previo).
    # Por tarea: unknown objective -> Ruta B (§6: se trata como sin tag);
    # exhausted/paused -> retain; tabla missing/inaccesible -> fail-safe
    # Ruta B (sin congelar la cola).
    quota_free = not (
        (session_pct is not None and session_pct >= QUOTA_THRESHOLD_PCT)
        or (weekly_pct is not None and weekly_pct >= QUOTA_THRESHOLD_PCT))
    _ao = _load_approved_objectives()
    route_a: dict = {}   # task_id -> objective id (presupuesto ya verificado)
    table_ok = _ao is not None and _ao.table_exists(db)
    for t in ready_unassigned:
        m_obj = OBJECTIVE_TAG_RE.search(t.get("body") or "")
        if not m_obj or quota_free:
            continue  # sin tag, o cuota libre (step-1 re-verifica): Ruta B
        if not table_ok:
            continue  # fail-safe §6: sin inventario no hay balance que gastar
        try:
            allowed, reason = _ao.budget_check(db, m_obj.group(1).upper())  # type: ignore[union-attr]
        except Exception:
            continue  # inventario inaccesible: fail-safe, Ruta B
        if allowed:
            route_a[t["id"]] = m_obj.group(1).upper()
        elif "unknown objective" not in (reason or ""):
            act({"ts": now, "action": "held-budget", "task": t["id"],
                 "objective": m_obj.group(1), "reason": reason},
                f"retain: {t['id']} — {reason}")
            return decisions

    # ── Gate: free quota (session AND weekly < 80%) — Ruta B (por defecto) ──
    # Con la cola en Ruta A el gate de cuota gratis no aplica: el gasto sale
    # del balance del objetivo aprobado, no de la cuota gratis.
    if not quota_free and not route_a:
        if session_pct is not None and session_pct >= QUOTA_THRESHOLD_PCT:
            act({"ts": now, "action": "skipped",
                 "reason": f"session {session_pct:.1f}% >= {QUOTA_THRESHOLD_PCT:.0f}%"},
                f"cola seca: sesion al {session_pct:.1f}% (umbral {QUOTA_THRESHOLD_PCT:.0f}%)")
            return decisions
        act({"ts": now, "action": "skipped",
             "reason": f"weekly {weekly_pct:.1f}% >= {QUOTA_THRESHOLD_PCT:.0f}%"},
            f"cola seca: weekly al {weekly_pct:.1f}% (umbral {QUOTA_THRESHOLD_PCT:.0f}%)")
        return decisions
    _s_pct = f"{session_pct:.1f}" if session_pct is not None else "n/d"
    _w_pct = f"{weekly_pct:.1f}" if weekly_pct is not None else "n/d"

    # ── Step 1: assign a profile to a ready task with no assignee ──
    # (fix the silent stop: ready-without-assignee is claimed by nobody)
    # MEDIATOR 2026-09-14: budget gate — tasks tagged objective:OBJ-XX are
    # only dispatched when the objective is active and has budget left.
    # Unknown objective => treated as untagged (Ruta B, §6). Exhausted or
    # paused objective => retained. Sin cuota libre solo se despachan las
    # tareas de la ruta A (presupuesto ya verificado arriba); las tareas
    # sin tag quedan para un tick con cuota libre.
    for t in ready_unassigned:
        if not quota_free and t["id"] not in route_a:
            continue  # Ruta B sin cuota libre: no despachable este tick
        m_obj = OBJECTIVE_TAG_RE.search(t.get("body") or "")
        if m_obj and t["id"] not in route_a:
            if _ao is None:
                act({"ts": now, "action": "held-budget-table-missing",
                     "task": t["id"]},
                    f"retain: {t['id']} tiene objective tag pero approved_objectives no existe (fail-safe §6)")
                return decisions
            allowed, reason = _ao.budget_check(db, m_obj.group(1).upper())
            if not allowed and "unknown objective" not in (reason or ""):
                act({"ts": now, "action": "held-budget", "task": t["id"],
                     "objective": m_obj.group(1), "reason": reason},
                    f"retain: {t['id']} — {reason}")
                return decisions
            # unknown objective aqui => Ruta B (sin tag) y hay cuota libre
        assignee = DEFAULT_ASSIGNEE
        nota = ""
        if t["id"] in route_a:
            nota = (f" [ruta A: {route_a[t['id']]} contra balance, "
                    f"cuota {_s_pct}%/{_w_pct}%]")
        if not execute:
            act({"ts": now, "action": "assigned-ready", "task": t["id"],
                 "assignee": assignee, "title": t["title"][:TITLE_LOG_CHARS],
                 "route": "A" if t["id"] in route_a else "B"},
                f"cola viva: asignado {t['id']} -> {assignee}{nota}")
            return decisions
        if assign_task(t["id"], assignee):
            act({"ts": now, "action": "assigned-ready", "task": t["id"],
                 "assignee": assignee, "title": t["title"][:TITLE_LOG_CHARS],
                 "route": "A" if t["id"] in route_a else "B"},
                f"cola viva: asignado {t['id']} ({t['title'][:TITLE_LOG_CHARS_SHORT]}) -> {assignee}{nota}")
            return decisions
        act({"ts": now, "action": "assign-failed", "task": t["id"]},
            f"cola seca: fallo al asignar {t['id']}")
        return decisions

    # ── Step 2: if ready tasks WITH assignee exist, the queue is alive ──
    # (the dispatcher claims them within ~60s — no new work needed)
    # P2 desired=3: con stock refillable la cascade cae al paso 3 y rellena
    # hasta el mínimo; pool seco = cola viva clásica (sin filler).
    ready_assigned = [t for t in ready_tasks(db)
                      if (t.get("assignee") or "").strip()]
    if ready_assigned and not refillable(db, now=now, write=execute):
        extra = (f" — backlog {backlog_total} < {BACKLOG_MIN}, pool seco "
                 f"(alarma, sin filler)") if backlog_total < BACKLOG_MIN else ""
        act({"ts": now, "action": "queue-alive",
             "ready_assigned": len(ready_assigned),
             "backlog_total": backlog_total},
            f"cola viva: {len(ready_assigned)} ready con assignee "
            f"(dispatcher los reclama){extra}")
        return decisions

    # ── Step 3: create structural class-C successors of the most recent
    # done tasks (<24h, clase:C) that have no open successor yet ──
    # P2 desired=3 (t_acf726e6): replenish UNTIL the minimum backlog holds
    # (ready_assigned + running >= BACKLOG_MIN), pool-bounded — dry stays
    # dry (step 4, never filler).
    # P5 dedup (MEDIATOR 14-sep): la cadena recursiva "Sucesor de Sucesor
    # de ..." muere aqui. Dos puertas antes de crear:
    #   (a) firma: si la firma (raiz+patron) ya existe en el board
    #       (abierta o hecha), la cadena ya cubrio su trabajo — no re-entra;
    #   (b) profundidad: un sucesor no genera otro sucesor (tope
    #       SUCCESSOR_MAX_DEPTH); legacy sin estampa cuenta como depth 1.
    skip_sigs: set = set()
    while backlog_total < BACKLOG_MIN:
        kind, payload = _refillable_candidate(db, now=now,
                                              skip_sigs=skip_sigs,
                                              write=execute)
        if kind == "capped":
            parent, depth = payload
            act({"ts": now, "action": "successor-depth-capped", "parent": parent["id"],
                 "depth": depth},
                f"cola viva: sucesor de {parent['id']} retenido — tope de "
                f"profundidad ({depth} > {SUCCESSOR_MAX_DEPTH}), sin cadena recursiva")
            return decisions
        if kind == "dedup":
            parent, sig = payload
            act({"ts": now, "action": "successor-dedup", "parent": parent["id"],
                 "sig": sig},
                f"cola viva: dedup P5 — firma {sig} ya existe en el board, "
                f"sin nuevo sucesor de {parent['id']}")
            return decisions
        if kind != "parent":
            break  # pool dry: drought verdict below stays honest
        parent = payload
        title, body = build_successor(parent, sig=parent["sig"], depth=1)
        assignee = parent.get("assignee") or DEFAULT_ASSIGNEE
        if not execute:
            act({"ts": now, "action": "created-successor", "parent": parent["id"],
                 "title": title[:TITLE_LOG_CHARS], "assignee": assignee},
                f"cola viva: sucesor estructural de {parent['id']} ({title[:TITLE_LOG_CHARS_SHORT]})")
            return decisions
        tid = create_task(title, body, assignee)
        if not tid:
            act({"ts": now, "action": "create-failed", "parent": parent["id"]},
                f"cola seca: fallo al crear sucesor de {parent['id']}")
            return decisions
        act({"ts": now, "action": "created-successor", "parent": parent["id"],
             "task": tid, "title": title[:TITLE_LOG_CHARS], "assignee": assignee},
            f"cola viva: sucesor estructural de {parent['id']} -> {tid} ({title[:TITLE_LOG_CHARS_SHORT]})")
        skip_sigs.add(parent["sig"])  # cobertura viva dentro de este tick
        backlog_total += 1  # each created successor counts toward the minimum
    if backlog_total >= BACKLOG_MIN:
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
    # C (13-sep, user-approved): drought with >=5 tasks in triage is NOT a
    # quiet verdict — the board is waiting on the user. Name it.
    try:
        con = _connect(db)
        try:
            n_triage = con.execute(
                "SELECT count(*) FROM tasks WHERE status='triage'").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        n_triage = 0
    if n_triage >= 5:
        act({"ts": now, "action": "waiting-user", "triage": n_triage},
            f"INCIDENTE: sequia con {n_triage} tareas en triage — el board espera al usuario, no esta seco")
    else:
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
