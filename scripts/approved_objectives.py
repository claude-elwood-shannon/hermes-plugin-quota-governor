#!/usr/bin/python3.12
"""approved_objectives.py — MEDIATOR: objetivos aprobados con presupuesto.

Single source of truth: the `approved_objectives` TABLE in the shared
kanban.db (SQLite native concurrency; no parallel JSON file). Both the
bridge (mediator) and Hermes (tick/governor) write it — the bridge manages
declaration (id/name/budget/description/status/criterion), the tick manages
spend (spent_today/spent_total) and autonomous lifecycle transitions.

Schema (directive 2026-09-14):
  id TEXT PK, name TEXT NOT NULL, budget_daily REAL DEFAULT 0.0,
  description TEXT, status TEXT DEFAULT 'active', success_criterion TEXT,
  spent_today REAL DEFAULT 0.0, spent_total REAL DEFAULT 0.0,
  created_at REAL, updated_at REAL, updated_by TEXT
Housekeeping extension (documented deviation): exhausted_days INTEGER
DEFAULT 0, last_exhausted_day TEXT — needed for the 'paused after 3
consecutive exhausted days' rule; the bridge UPDATE never wipes them
(it updates only provided fields).

Statuses: active | achieved | paused | discarded. Rows are never deleted.

Spend accounting (§5): spent_today for objective X = sum of trace.jsonl
costUsd lines whose `objective` tag equals X within the CURRENT CEST day.
Computed fresh from the trace on every tick — the midnight CEST reset is
implicit and restart-safe (a new day simply sums to a fresh window).
spent_total increases by the positive delta since the previous tick.

Budget gate (§6): budget_check() -> (allowed, reason). Unknown objective,
non-active status, or exhausted budget => NOT dispatched. Missing table =>
fail-open for tasks WITHOUT objective tag (the caller decides; the tick
retains tagged tasks).

Lifecycle (§8, tick-side):
  achieved: only for objectives with a machine-checkable criterion
            (registry below; OBJ-AUTODEV is perpetual and NEVER achieved).
  paused:   exhausted_days >= 3 (consecutive CEST days with
            spent_today >= budget_daily at tick time).
  active:   paused -> active on the first day the budget is available.
  discarded: NEVER by the system — mediator only (bridge POST).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"active", "achieved", "paused", "discarded"}
PERPETUAL = {"OBJ-AUTODEV"}  # never auto-achieved

SEED_OBJECTIVES = (
    ("OBJ-AUTODEV", "Autodesarrollo del sistema", 3.00,
     "Hardening, tests, docs, refactor, observabilidad, fix de bugs detectados",
     "active",
     "Objetivo perpetuo de mejora continua — no se marca achieved"),
    ("OBJ-CODEQUALITY", "Calidad de código y buenas prácticas", 0.50,
     "Refactor de anti-patrones, extracción de utilidades comunes, "
     "type hints, docstrings, eliminación de código duplicado, coding standards",
     "active",
     "Todos los scripts pasan py_compile, funciones críticas <50 líneas, "
     "lógica compartida extraída a módulo común, type hints y docstrings "
     "en funciones públicas, docs/coding-standards.md existe"),
    ("OBJ-VLLM", "vLLM híbrido como herramienta", 1.00,
     "vllm-invoke, skills, hints, integración con workers cloud",
     "active",
     "vllm-invoke.sh existe AND vllm-invoke.jsonl tiene >=10 entradas con "
     "exit_code=0 AND skill vllm-delegate.md existe"),
    ("OBJ-METRICS", "Predicción y métricas", 0.50,
     "Backtest, efficiency ratio, KPIs, dashboards, validación de "
     "predicciones de coste",
     "active",
     "efficiency-ratio con veredicto no CRITICO durante 3 días consecutivos "
     "AND backtest de predicción de costes con ratio precision >= 0.7"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approved_objectives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    budget_daily REAL NOT NULL DEFAULT 0.0,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    success_criterion TEXT,
    spent_today REAL DEFAULT 0.0,
    spent_total REAL DEFAULT 0.0,
    created_at REAL,
    updated_at REAL,
    updated_by TEXT,
    exhausted_days INTEGER DEFAULT 0,
    last_exhausted_day TEXT
);
"""


def hermes_root() -> Path:
    env = os.environ.get("AO_HERMES_ROOT", "").strip()
    return Path(env) if env else Path.home() / ".hermes"


def kanban_db_path() -> Path:
    env = os.environ.get("AO_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root = hermes_root() / "kanban.db"
    return root if root.exists() else \
        hermes_root() / "profiles" / "pr-ollama" / "kanban.db"


def trace_path() -> Path:
    env = os.environ.get("AO_TRACE", "").strip()
    if env:
        return Path(env)
    p = hermes_root() / "profiles" / "pr-ollama" / "quota-governor" / "obs" / "trace.jsonl"
    return p if p.exists() else \
        hermes_root() / "quota-governor" / "obs" / "trace.jsonl"


def cest_day_of(epoch: float) -> str:
    """CEST calendar day (UTC+2 fixed, house convention) as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(epoch + 2 * 3600))


def day_start_epoch_cest(day: str) -> float:
    """Epoch of 00:00 CEST (= 22:00 UTC prev day) for a YYYY-MM-DD day."""
    return time.mktime(time.strptime(day, "%Y-%m-%d")) - 2 * 3600


# ---------------------------------------------------------------------------
# Table lifecycle
# ---------------------------------------------------------------------------

def ensure_table(db: Path) -> bool:
    """CREATE TABLE IF NOT EXISTS + seed the 3 initial objectives
    (INSERT OR IGNORE: re-runs never duplicate or overwrite mediator edits)."""
    try:
        con = sqlite3.connect(str(db))
        con.executescript(SCHEMA)
        now = time.time()
        for oid, name, budget, desc, status, crit in SEED_OBJECTIVES:
            con.execute(
                "INSERT OR IGNORE INTO approved_objectives "
                "(id, name, budget_daily, description, status, "
                " success_criterion, created_at, updated_at, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (oid, name, budget, desc, status, crit, now, now, "mediator"))
        con.commit()
        con.close()
        return True
    except sqlite3.Error:
        return False


def table_exists(db: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='approved_objectives'").fetchone()
        con.close()
        return bool(row)
    except sqlite3.Error:
        return False


def list_objectives(db: Path, status: Optional[str] = None) -> list:
    if not table_exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        if status:
            rows = con.execute(
                "SELECT * FROM approved_objectives WHERE status=? "
                "ORDER BY id", (status,)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM approved_objectives ORDER BY id").fetchall()
        con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def get_objective(db: Path, oid: str) -> Optional[dict]:
    if not table_exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM approved_objectives WHERE id=?", (oid,)).fetchone()
        con.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def upsert_objective(db: Path, data: dict, updated_by: str = "mediator") -> dict:
    """Bridge POST /update-objective. Existing id -> UPDATE of the PROVIDED
    fields only (housekeeping columns survive); new id -> INSERT."""
    oid = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    budget = data.get("budget_daily")
    if not oid or not name or not isinstance(budget, (int, float)):
        return {"ok": False, "error": "id, name and numeric budget_daily "
                                      "are required"}
    status = (data.get("status") or "active").strip()
    if status not in VALID_STATUSES:
        return {"ok": False, "error": f"invalid status {status!r} "
                                      f"(valid: {sorted(VALID_STATUSES)})"}
    if not ensure_table(db):
        return {"ok": False, "error": "cannot open kanban.db for write"}
    now = time.time()
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        existing = con.execute(
            "SELECT id FROM approved_objectives WHERE id=?", (oid,)).fetchone()
        if existing:
            sets, vals = ["updated_at=?", "updated_by=?"], [now, updated_by]
            for field in ("name", "budget_daily", "description",
                          "success_criterion", "status"):
                if field in data and data[field] is not None:
                    sets.append(f"{field}=?")
                    vals.append(data[field])
            vals.append(oid)
            con.execute(f"UPDATE approved_objectives SET {', '.join(sets)} "
                        "WHERE id=?", vals)
            action = "updated"
        else:
            con.execute(
                "INSERT INTO approved_objectives (id, name, budget_daily, "
                "description, status, success_criterion, created_at, "
                "updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (oid, name, float(budget), data.get("description"),
                 status, data.get("success_criterion"), now, now, updated_by))
            action = "created"
        con.commit()
        row = con.execute("SELECT * FROM approved_objectives WHERE id=?",
                          (oid,)).fetchone()
        con.close()
        return {"ok": True, "action": action, "objective": dict(row) if row
                else None}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Spend (§5) — computed from the trace for the current CEST day
# ---------------------------------------------------------------------------

def spend_by_objective_today(trace_file: Path, now: float) -> dict:
    """{objective: usd} for cost-bearing lines of the current CEST day."""
    day = cest_day_of(now)
    day0 = day_start_epoch_cest(day)
    out: dict = {}
    try:
        with open(trace_file, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ts = e.get("ts_epoch_utc")
                usd = e.get("costUsd")
                obj = (e.get("objective") or "").strip()
                if not obj or obj == "unattributed":
                    continue
                if not isinstance(ts, (int, float)) or ts < day0 or ts > now:
                    continue
                if not isinstance(usd, (int, float)) or usd <= 0:
                    continue
                out[obj] = out.get(obj, 0.0) + usd
    except OSError:
        return out
    return out


def update_spend(db: Path, now: float | None = None) -> dict:
    """Tick-side spend update (§5.1-5.4). spent_today = trace sum for the
    current CEST day (implicit midnight reset); spent_total += positive
    delta. Returns a summary for the tick log. Missing table -> no-op."""
    now = time.time() if now is None else float(now)
    if not table_exists(db):
        return {"skipped": "table missing"}
    sums = spend_by_objective_today(trace_path(), now)
    updated = []
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, spent_today, spent_total, status, budget_daily, "
            "exhausted_days, last_exhausted_day FROM approved_objectives"
        ).fetchall()
        day = cest_day_of(now)
        for r in rows:
            if r["status"] == "discarded":
                continue
            obj = r["id"]
            new_today = round(sums.get(obj, 0.0), 6)
            prev_today = r["spent_today"] or 0.0
            prev_day = r["last_exhausted_day"]
            # delta: the stored spent_today belongs to the day it was last
            # written (last_exhausted_day tracks the last ticked day). A
            # new CEST day means the previous value was yesterday's — the
            # full new sum is today's delta (implicit midnight reset).
            if prev_day == day:
                delta = new_today - prev_today
            else:
                delta = new_today
            new_total = round((r["spent_total"] or 0.0) + max(delta, 0.0), 6)
            exhausted = 1 if (new_today >= r["budget_daily"]
                              and r["budget_daily"] > 0) else 0
            if exhausted:
                if prev_day == day:
                    ed = r["exhausted_days"] or 0  # already counted today
                elif prev_day and _prev_day_of(day) == prev_day:
                    ed = (r["exhausted_days"] or 0) + 1
                else:
                    ed = 1
            else:
                ed = 0
            con.execute(
                "UPDATE approved_objectives SET spent_today=?, spent_total=?,"
                " exhausted_days=?, last_exhausted_day=?, updated_at=?, "
                "updated_by='tick' WHERE id=?",
                (new_today, new_total, ed, day, now, obj))
            updated.append({"id": obj, "spent_today": new_today,
                            "spent_total": new_total,
                            "exhausted_days": ed})
        con.commit()
        con.close()
        return {"updated": updated}
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def _prev_day_of(day: str) -> str:
    return cest_day_of(day_start_epoch_cest(day) - 12 * 3600)


# ---------------------------------------------------------------------------
# Budget gate (§6)
# ---------------------------------------------------------------------------

def budget_check(db: Path, objective_id: str) -> tuple:
    """(allowed, reason) for dispatching a task tagged objective:<id>.
    table missing -> (True, 'table missing — fail-open') : the CALLER must
    handle the fail-safe for tagged tasks (tick retains them)."""
    if not table_exists(db):
        return True, "table missing — fail-open"
    obj = get_objective(db, objective_id)
    if obj is None:
        return False, f"unknown objective {objective_id} — task not dispatched"
    if obj["status"] != "active":
        return False, (f"objective {objective_id} not active "
                       f"(status={obj['status']})")
    if obj["budget_daily"] and (obj["spent_today"] or 0.0) >= obj["budget_daily"]:
        return False, f"budget exhausted for {objective_id}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Autonomous lifecycle (§8)
# ---------------------------------------------------------------------------

EVENTS_LOG = None  # resolved lazily: hermes_root()/logs/objective-events.jsonl


def _events_path() -> Path:
    return hermes_root() / "logs" / "objective-events.jsonl"


def _log_event(entry: dict) -> None:
    try:
        p = _events_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _check_vllm_criterion() -> tuple:
    """(verified, detail) for OBJ-VLLM."""
    root = hermes_root()
    candidates = [root / "scripts/vllm-invoke.sh",
                  root / "scripts/vllm-invoke.py",
                  root / "profiles/pr-ollama/scripts/vllm-invoke.py"]
    # repo copy (the deployed copies are synced FROM the repo)
    repo = os.environ.get("BRIDGE_PLUGIN_REPO", "").strip()
    if repo:
        candidates.append(Path(repo) / "scripts/vllm-invoke.py")
        candidates.append(Path(repo) / "scripts/vllm-invoke.sh")
    script_ok = any(c.exists() for c in candidates)
    logp = root / "logs" / "vllm-invoke.jsonl"
    ok_entries = 0
    try:
        with open(logp, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    if json.loads(ln).get("exit_code") == 0:
                        ok_entries += 1
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    skill_ok = (root / "profiles/pr-ollama/skills/vllm-delegate/SKILL.md").exists() \
        or (root / "skills/vllm-delegate.md").exists() \
        or (root / "profiles/pr-ollama/skills/vllm-delegate.md").exists()
    verified = script_ok and ok_entries >= 10 and skill_ok
    return verified, (f"script={script_ok} ok_entries={ok_entries} "
                      f"skill={skill_ok}")


def _check_metrics_criterion() -> tuple:
    """(verified, detail) for OBJ-METRICS: efficiency verdict != CRITICO
    on 3 consecutive distinct days AND backtest precision signal."""
    mh = hermes_root() / "profiles/pr-ollama/quota-governor/metrics-history.jsonl"
    days: dict = {}
    try:
        with open(mh, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if e.get("kind") != "efficiency_ratio":
                    continue
                ts = e.get("ts") or ""
                day = ts[:10]
                v = e.get("veredicto")
                if day and v:
                    days.setdefault(day, set()).add(v)
    except OSError:
        pass
    good_days = sorted(d for d, vs in days.items()
                       if vs - {"CRITICO"} and "CRITICO" not in vs)
    streak = 0
    for i, d in enumerate(good_days):
        if i and _prev_day_of(d) == good_days[i - 1]:
            streak += 1
        else:
            streak = 1
    eff_ok = streak >= 3
    # backtest precision signal: OBJ-24 F2 harness verdict file if present
    bt = hermes_root() / "quota-governor" / "backtest-f2-verdict.json"
    bt_ok = False
    try:
        d = json.loads(bt.read_text())
        prec = d.get("precision") or d.get("precision_ratio") or 0.0
        bt_ok = float(prec) >= 0.7
    except (OSError, ValueError, TypeError):
        pass
    return (eff_ok and bt_ok), (f"eff_streak={streak}/3 backtest={bt_ok}")


_CHECKERS = {"OBJ-VLLM": _check_vllm_criterion,
             "OBJ-METRICS": _check_metrics_criterion}


def run_lifecycle(db: Path, now: float | None = None, dry: bool = False) -> list:
    """§8 transitions. Returns list of human lines for the tick/screen:
    '✅ OBJ-X achieved — ...' / '⚠ OBJ-X paused — ...' / '↻ OBJ-X reactivated'."""
    now = time.time() if now is None else float(now)
    out = []
    if not table_exists(db):
        return out
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, status, exhausted_days, budget_daily, spent_today "
            "FROM approved_objectives").fetchall()
        for r in rows:
            oid, status = r["id"], r["status"]
            if oid in PERPETUAL:
                continue
            if status == "active":
                checker = _CHECKERS.get(oid)
                if checker:
                    verified, detail = checker()
                    if verified:
                        if not dry:
                            con.execute(
                                "UPDATE approved_objectives SET "
                                "status='achieved', updated_at=?, "
                                "updated_by='system' WHERE id=? AND "
                                "status='active'", (now, oid))
                        line = f"✅ {oid} achieved — success criterion verificado ({detail})"
                        out.append(line)
                        _log_event({"ts": now, "id": oid,
                                    "event": "achieved", "detail": detail})
                        continue
                if (r["exhausted_days"] or 0) >= 3 and r["budget_daily"]:
                    if not dry:
                        con.execute(
                            "UPDATE approved_objectives SET status='paused',"
                            " updated_at=?, updated_by='system' WHERE id=?"
                            " AND status='active'", (now, oid))
                    line = f"⚠ {oid} paused — budget agotado 3 días seguidos"
                    out.append(line)
                    _log_event({"ts": now, "id": oid, "event": "paused"})
            elif status == "paused":
                if (r["spent_today"] or 0.0) < r["budget_daily"]:
                    if not dry:
                        con.execute(
                            "UPDATE approved_objectives SET status='active',"
                            " exhausted_days=0, updated_at=?, "
                            "updated_by='system' WHERE id=? AND "
                            "status='paused'", (now, oid))
                    line = f"↻ {oid} reactivado — budget disponible de nuevo"
                    out.append(line)
                    _log_event({"ts": now, "id": oid,
                                "event": "reactivated"})
        if not dry:
            con.commit()
        con.close()
    except sqlite3.Error:
        pass
    return out


# ---------------------------------------------------------------------------
# CLI (ops)
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="approved objectives inventory")
    ap.add_argument("--db", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", default=None)
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--update-spend", action="store_true")
    ap.add_argument("--lifecycle", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    db = Path(args.db) if args.db else kanban_db_path()
    if args.ensure:
        print("ensured" if ensure_table(db) else "FAILED")
    if args.update_spend:
        print(json.dumps(update_spend(db), ensure_ascii=False))
    if args.lifecycle:
        for line in run_lifecycle(db, dry=args.dry_run):
            print(line)
    if args.list or not (args.ensure or args.update_spend or args.lifecycle):
        for o in list_objectives(db, args.status):
            print(f"{o['id']:<14} {o['status']:<9} "
                  f"${(o['spent_today'] or 0):.2f}/${o['budget_daily']:.2f} "
                  f"total=${(o['spent_total'] or 0):.2f}  {o['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())