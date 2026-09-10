#!/usr/bin/python3.12
"""task-cost-train.py — OBJ-35 P1: per-task cost observer (no_agent).

THE HUECO (gap) THIS FILLS
--------------------------
The house predicts provider WINDOWS (OBJ-24 F2 EMA forecast, ETA-90) but
nobody predicts a TASK: "this objective will cost X and finish on day Y".
P1 is the first phase of OBJ-35 — pure observer, zero inference, zero
tokens. For every closed task it registers one training row:

  (objective, cost_class, clase, model, provider, tokens, real costUsd,
   duration, crashes)

ATTRIBUTION PATH (verified live 2026-09-10, t_dd2eb1bb)
-------------------------------------------------------
  kanban.db tasks.session_id  ->  profiles/<p>/state.db
                                  session_model_usage (task IS NULL)
The trace (OBJ-27 F0) carries cost only for cron-llm rows; worker
sessions bill at the window level, so the per-task join must go through
the state.dbs' session×model token aggregation — the same source
model-cost-ledger.py meters. Cost is then the CATALOG ESTIMATE
(model-cost-ledger.estimate_cost: tokens x published prices, cache-read
included, deepseek peak-hours doubled) — the same convention every
ledger in the house uses for window-billed consumption.

KNOWN LIMITATIONS (documented, not hidden)
------------------------------------------
* Shared sessions: several tasks may run inside one session_id
  (verified: 3 tasks on 20260910_022134_664f58). Their rows repeat the
  session's usage, so per-task cost is OVERSTATED for them. Rows carry
  shared_session=true and session_tasks=n so consumers can filter or
  split. We never fabricate a pro-rata split (no ground truth to
  calibrate it against).
* costUsd is an estimate (catalog), never a bill. Known +16% bias on
  glm-5.2 (see model-cost-ledger docstring). It is a CONSERVATIVE
  UPPER BOUND — the right direction for budgeting.
* Tasks deleted from kanban.db before a tick (daily 04:00 cleanup)
  are lost; at a 15m cadence that window is effectively never hit.
* Tasks without session_id (dispatch-only work, crashed early) are
  still recorded with attributed=false and null cost — the gap is
  shown, not hidden (trace.py convention).

LEDGER
------
~/.hermes/quota-governor/task-cost-train.jsonl (real hermes home, NOT
the profile dir — same convention as forecast-backtest.jsonl).
Append-only, one JSON per line, idempotent per task_id (a task already
in the ledger is skipped; usage is a session total, not a delta, so
re-writing would only drift).

Cron: every 15m, no_agent, wrapper in the profile scripts dir execs
this file from the repo (single source of truth, budget-check pattern).
Watchdog stdout: silent when nothing new; one line when rows were
appended. Always exits 0 — the cron never breaks.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
    """Import a sibling repo module by file path (backtest-f2 pattern)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shared constants: objective/cost/clase tag parsing comes from the trace
# (the canonical stamp, OBJ-28 §1.3); pricing from the calibrated ledger.
_trace = _load_module("obj35_trace", HERE / "trace.py")
parse_objective = _trace.parse_objective

_MCL_PATH = HERE.parent / "model-cost-ledger.py"
try:
    _mcl = _load_module("obj35_model_cost_ledger", _MCL_PATH)
    estimate_cost = _mcl.estimate_cost
    load_mcl_config = _mcl.load_config
    iso_to_epoch = _mcl.iso_to_epoch
    MCL_OK = True
except Exception:  # pragma: no cover - fail-open, tokens still recorded
    MCL_OK = False
    estimate_cost = None
    load_mcl_config = None
    iso_to_epoch = None


# ---------------------------------------------------------------------------
# Paths (real home for sources and ledger — same convention as backtest-f2)
# ---------------------------------------------------------------------------

def real_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def kanban_db_path() -> Path:
    custom = os.environ.get("QUOTA_TASK_COST_KANBAN_DB", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".hermes" / "kanban.db"


def ledger_path() -> Path:
    custom = os.environ.get("QUOTA_TASK_COST_LEDGER", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".hermes" / "quota-governor" / "task-cost-train.jsonl"


PROFILE_HOMES_DEFAULT = ("pr-ollama", "pr-nanogpt", "pr-opencode",
                         "pr-openrouter", "pr-vllm")


def profile_state_dbs() -> list:
    """state.db paths for every profile home (env-overridable for tests)."""
    raw = os.environ.get("QUOTA_TASK_COST_PROFILE_HOMES", "").strip()
    names = [s.strip() for s in raw.split(os.pathsep) if s.strip()] \
        if raw else list(PROFILE_HOMES_DEFAULT)
    base = Path.home() / ".hermes" / "profiles"
    out = []
    for name in names:
        p = base / name / "state.db"
        if p.exists():
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Readers (fail open: every helper returns partial data, never raises)
# ---------------------------------------------------------------------------

def parse_cost_class(body: str):
    """'cost:small' from the first lines of a task body. None when absent.

    Both real board layouts are accepted: a dedicated 'cost: x' line and
    pipe-separated segments on the objective line
    ('objective:OBJ-27 | cost:small | ...').
    """
    if not body:
        return None
    for line in body.splitlines()[:8]:
        for seg in line.split("|"):
            seg = seg.strip()
            if seg.startswith("cost:"):
                tag = seg[len("cost:"):].split(",")[0].strip()
                if tag:
                    return tag
    return None


def parse_clase(body: str):
    """'clase:B' from the first lines of a task body. None when absent.

    Same dual layout support as parse_cost_class.
    """
    if not body:
        return None
    for line in body.splitlines()[:8]:
        for seg in line.split("|"):
            seg = seg.strip()
            if seg.startswith("clase:"):
                tag = seg[len("clase:"):].split(",")[0].strip()
                if tag:
                    return tag
    return None


def load_closed_tasks(kanban_db: Path) -> list:
    """Closed tasks (completed_at present) with tag/board metadata."""
    if not kanban_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT t.id, t.body, t.assignee, t.created_at, "
                "t.completed_at, t.session_id, "
                "  (SELECT COUNT(*) FROM task_events e "
                "   WHERE e.task_id = t.id AND e.kind IN "
                "         ('crashed','gave_up')) AS crashes, "
                "  (SELECT COUNT(*) FROM task_runs r WHERE r.task_id = t.id) "
                "   AS runs "
                "FROM tasks t WHERE t.completed_at IS NOT NULL"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def load_session_usage() -> dict:
    """session_id -> usage rows aggregated by (model, billing_provider).

    Only worker rows (task IS NULL/'') are relevant: title_generation /
    approval / compression rows are housekeeping, not the task's work.
    Scanned read-only from every profile state.db. Never raises.
    """
    out = {}
    for db in profile_state_dbs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                cur = con.execute(
                    "SELECT session_id, model, api_call_count, input_tokens, "
                    "output_tokens, cache_read_tokens, reasoning_tokens, "
                    "billing_provider, last_seen "
                    "FROM session_model_usage "
                    "WHERE task IS NULL OR task = ''")
                for r in cur:
                    sid = r["session_id"]
                    if not sid:
                        continue
                    out.setdefault(sid, []).append({
                        "profile": db.parent.name,
                        "model": r["model"] or "unknown",
                        "api_calls": r["api_call_count"] or 0,
                        "tokens_in": r["input_tokens"] or 0,
                        "tokens_out": r["output_tokens"] or 0,
                        "cache_read": r["cache_read_tokens"] or 0,
                        "reasoning": r["reasoning_tokens"] or 0,
                        "billing_provider": r["billing_provider"] or "unknown",
                        "last_seen": r["last_seen"],
                    })
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return out


def count_session_tasks(kanban_db: Path) -> dict:
    """session_id -> number of tasks sharing it (shared-session flag)."""
    counts = {}
    if not kanban_db.exists():
        return counts
    try:
        con = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        try:
            for sid, n in con.execute(
                    "SELECT session_id, COUNT(*) FROM tasks "
                    "WHERE session_id IS NOT NULL GROUP BY session_id"):
                counts[sid] = n
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return counts


def estimate_group_cost(group: dict, prices=None) -> float:
    """Catalog USD for one aggregated (session, model, provider) group."""
    if not MCL_OK or estimate_cost is None:
        return 0.0
    at_ts = None
    if iso_to_epoch is not None and group.get("last_seen"):
        at_ts = iso_to_epoch(group["last_seen"])
    try:
        return estimate_cost(
            group["model"], group["tokens_in"], group["tokens_out"],
            group["cache_read"], group["reasoning"],
            at_ts=at_ts, prices=prices)
    except Exception:
        return 0.0


def build_row(task: dict, usage_rows: list, session_tasks: int,
              prices=None, now=None, estimate_row=None) -> dict:
    """One training row for one closed task.

    estimate_row (optional): the P3 prediction captured at creation time
    {"p50","p90","stage","model","captured_at"} — carried through for the
    estimated-vs-real comparison (P3 accuracy loop).
    """
    sid = task.get("session_id")
    body = task.get("body") or ""
    objective = parse_objective(body)
    cost_class = parse_cost_class(body)
    clase = parse_clase(body)

    groups = {}
    for u in usage_rows:
        key = (u["profile"], u["model"], u["billing_provider"])
        g = groups.setdefault(key, {
            "profile": u["profile"], "model": u["model"],
            "billing_provider": u["billing_provider"], "api_calls": 0,
            "tokens_in": 0, "tokens_out": 0, "cache_read": 0,
            "reasoning": 0, "last_seen": u["last_seen"],
            "rows": 0,
        })
        g["api_calls"] += u["api_calls"]
        g["tokens_in"] += u["tokens_in"]
        g["tokens_out"] += u["tokens_out"]
        g["cache_read"] += u["cache_read"]
        g["reasoning"] += u["reasoning"]
        g["rows"] += 1

    model_groups = []
    for g in groups.values():
        model_groups.append({
            "profile": g["profile"], "model": g["model"],
            "billing_provider": g["billing_provider"],
            "api_calls": g["api_calls"], "tokens_in": g["tokens_in"],
            "tokens_out": g["tokens_out"], "cache_read": g["cache_read"],
            "reasoning": g["reasoning"],
            "costUsd": round(estimate_group_cost(g, prices), 6),
        })
    model_groups.sort(key=lambda m: -m["costUsd"])

    dominant = model_groups[0] if model_groups else None
    total_cost = sum(m["costUsd"] for m in model_groups)
    created = task.get("created_at")
    completed = task.get("completed_at")
    duration = None
    try:
        if created is not None and completed is not None:
            duration = int(round(float(completed) - float(created)))
    except (TypeError, ValueError):
        duration = None

    attributed = bool(sid) and dominant is not None and MCL_OK
    return {
        "kind": "task",
        "task_id": task["id"],
        "objective": objective,
        "cost_class": cost_class,
        "clase": clase,
        "assignee": task.get("assignee"),
        "created_at": created,
        "completed_at": completed,
        "duration_s": duration,
        "runs": task.get("runs") or 0,
        "crashes": task.get("crashes") or 0,
        "session_id": sid,
        "session_tasks": session_tasks if sid else None,
        "shared_session": bool(sid and session_tasks and session_tasks > 1),
        "attributed": attributed,
        "usage_rows": len(usage_rows),
        "profile": dominant["profile"] if dominant else None,
        "model": dominant["model"] if dominant else None,
        "billing_provider": dominant["billing_provider"] if dominant else None,
        "api_calls": sum(m["api_calls"] for m in model_groups),
        "tokens_in": sum(m["tokens_in"] for m in model_groups),
        "tokens_out": sum(m["tokens_out"] for m in model_groups),
        "cache_read": sum(m["cache_read"] for m in model_groups),
        "reasoning": sum(m["reasoning"] for m in model_groups),
        "costUsd": round(total_cost, 6) if attributed else None,
        "cost_source": "estimated-catalog" if attributed else "none",
        "estimate": estimate_row if estimate_row else None,
        "models": model_groups,
        "ts": now if now is not None else time.time(),
    }


# ---------------------------------------------------------------------------
# Ledger append (idempotent per task_id)
# ---------------------------------------------------------------------------

def load_ledger_task_ids(path: Path) -> set:
    ids = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "task" and r.get("task_id"):
                    ids.add(r["task_id"])
    except OSError:
        pass
    return ids


def append_rows(path: Path, rows: list) -> int:
    """Append rows sorted by completed_at; returns count appended."""
    n = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":"),
                                    sort_keys=True) + "\n")
                n += 1
    except OSError:
        return n
    return n


def load_creation_estimate(ledger: Path, task_id: str):
    """P3 prediction captured when the task was OPEN, if any.

    Reads 'estimate' kind lines ({task_id, estimate:{...}}) from the same
    ledger and returns the estimate dict (or None). This is what makes the
    P3 accuracy loop honest: the prediction precedes the outcome in the
    same append-only file.
    """
    found = None
    try:
        with open(ledger, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (r.get("kind") == "estimate"
                        and r.get("task_id") == task_id
                        and isinstance(r.get("estimate"), dict)):
                    found = r["estimate"]
    except OSError:
        return None
    return found


def collect(ledger=None, kanban_db=None, now=None) -> dict:
    """Full pass: append one training row per not-yet-recorded closed task.

    Returns {"appended": int, "skipped_known": int, "no_body": int,
             "ledger": str}. Never raises.
    """
    ledger = ledger or ledger_path()
    kanban_db = kanban_db or kanban_db_path()
    now = now if now is not None else time.time()

    known = load_ledger_task_ids(ledger)
    tasks = load_closed_tasks(kanban_db)
    usage = load_session_usage()
    session_counts = count_session_tasks(kanban_db)

    prices = None
    if MCL_OK and load_mcl_config is not None:
        try:
            prices = load_mcl_config()["prices"]
        except Exception:
            prices = None

    fresh, no_body = [], 0
    for t in sorted(tasks, key=lambda x: x.get("completed_at") or 0):
        if t["id"] in known:
            continue
        sid = t.get("session_id")
        rows = usage.get(sid, []) if sid else []
        if not (t.get("body") or "").strip():
            no_body += 1
            continue
        fresh.append(build_row(
            t, rows, session_counts.get(sid, 1) if sid else 1,
            prices=prices, now=now,
            estimate_row=load_creation_estimate(ledger, t["id"])))

    appended = append_rows(ledger, fresh) if fresh else 0
    return {
        "appended": appended,
        "skipped_known": len(tasks) - len(fresh) - no_body,
        "no_body": no_body,
        "ledger": str(ledger),
    }


def main(argv=None) -> int:
    try:
        res = collect()
    except Exception:
        return 0  # the cron never breaks
    if res["appended"] or res["no_body"]:
        print("task-cost-train: appended=%d known=%d no_body=%d" % (
            res["appended"], res["skipped_known"], res["no_body"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
