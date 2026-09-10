#!/usr/bin/python3.12
"""trace.py — OBJ-27 F0: canonical consumption trace (observer-only).

WHAT IT IS
----------
The house keeps its own ledger. This module defines the canonical trace
schema and an append-only JSONL writer under
``get_hermes_home()/quota-governor/obs/trace.jsonl``, plus three v0
collectors that read EXISTING sources (never touching Hermes core) and
emit one trace line per event:

  1. nanogpt-requests  (nanogpt-balance-ledger per-request billing)
       -> requestId + costUsd REAL (balance providers)
  2. usage-audit      (cron/usage_audit.jsonl, written by cron scheduler)
       -> tokens; costUsd = shadow price from the model-cost catalog
  3. task-events      (kanban.db task_events: claimed/completed)
       -> the JOIN KEY: task_id -> task body -> objective:OBJ-xx tag

CANONICAL SCHEMA (one JSON object per line)
-------------------------------------------
  ts_epoch_utc   float  epoch seconds UTC (render local only when showing)
  consumer_class one of {user-chat, worker, cron-llm, auxiliary, probe,
                        unattributed}
  consumer_id    str    task_id / fire_id / requestId / session_id
  cause          str    what triggered the line (claimed/completed/request/...)
  model          str    model id (gen_ai.request.model)
  provider       str    provider id
  tokens_in      int    input tokens (gen_ai.usage.input_tokens)
  tokens_out     int    output tokens (gen_ai.usage.output_tokens)
  costUsd        float  real USD (balance) or shadow price (catalog)
  requestId      str    provider request id when known
  objective      str    OBJ-xx from the task body, else "unattributed"
  source         str    which collector emitted the line
  otel           dict   OTel semantic-convention mapping where applicable

THE GAP IS SHOWN, NOT HIDDEN
----------------------------
A line whose objective cannot be joined (no task_id in the source) falls
EXPLICITLY into consumer_class=unattributed / objective="unattributed".
The report (F1, later) shows the hole; this module never fabricates a
join. task-events is the only source that carries the objective tag today.

OBSERVER-ONLY: collectors only READ existing files/DBs and APPEND to the
trace. They never modify the sources, never touch Hermes core, and never
raise into a caller (every public helper fails open).

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times are epoch UTC.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

CONSUMER_CLASSES = frozenset({
    "user-chat", "worker", "cron-llm", "auxiliary", "probe", "unattributed",
})

# OTel semantic-convention names (gen_ai.*) mapped from the SOURCE field
# names (each collector's row uses its own keys: nanogpt uses inputTokens,
# usage-audit uses prompt_tokens/completion_tokens).
OTEL_MAP = {
    "model": "gen_ai.request.model",
    "inputTokens": "gen_ai.usage.input_tokens",
    "outputTokens": "gen_ai.usage.output_tokens",
    "prompt_tokens": "gen_ai.usage.input_tokens",
    "completion_tokens": "gen_ai.usage.output_tokens",
    "requestId": "gen_ai.request.id",
}

# Default shadow-price catalog (USD per million tokens: [in, out, cache]).
# Overridable via model-cost.json under the state dir (same shape as the
# plugin's model-cost-ledger config). Unknown models cost 0 (still traced:
# token counts are provider-side truth).
DEFAULT_PRICES = {
    "glm-5.2": (1.40, 4.40, 0.26),
    "glm-5.3": (1.40, 4.40, 0.26),
    "glm-5.1": (1.40, 4.40, 0.26),
    "glm-5.3-flash": (0.1571, 0.5236, 0.0314),
    "qwen3.8-flash": (0.15, 0.47, 0.016),
    "deepseek-v4-flash": (0.22, 0.66, 0.007),
}


def get_hermes_home() -> Path:
    """Active HERMES_HOME, else ~/.hermes. Never absolute in the repo."""
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def state_dir(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor"


def obs_dir(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "obs"


def trace_path(hermes_home=None) -> Path:
    return obs_dir(hermes_home) / "trace.jsonl"


def cursor_path(hermes_home=None) -> Path:
    return obs_dir(hermes_home) / "trace-cursor.json"


def model_cost_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "model-cost.json"


# Cross-profile source homes. The three v0 sources live under DIFFERENT
# HERMES_HOME roots (verified live 2026-09-10): usage_audit under the
# profile home (pr-ollama/cron), nanogpt-requests and kanban.db under the
# root ~/.hermes. Same convention as nanogpt-balance-ledger's
# _DEFAULT_PROFILE_HOMES: collectors scan every home, dedupe by natural
# key, and the WRITER stays under get_hermes_home(). Overridable via
# QUOTA_GOVERNOR_PROFILE_HOMES (os.pathsep) for tests / non-standard hosts.
_DEFAULT_PROFILE_HOMES = (
    Path.home() / ".hermes",
) + tuple(
    Path.home() / ".hermes" / "profiles" / name
    for name in ("pr-ollama", "pr-nanogpt", "pr-opencode", "pr-openrouter",
                 "pr-vllm")
)


def _source_homes(hermes_home=None) -> list:
    """Candidate HERMES_HOME roots to scan for sources. Never raises."""
    raw = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES", "")
    if raw:
        homes = [Path(p) for p in (s.strip() for s in raw.split(os.pathsep))
                 if p]
        if homes:
            return homes
    if hermes_home:
        return [Path(hermes_home)]
    return list(_DEFAULT_PROFILE_HOMES)


def _load_cursor(hermes_home=None) -> dict:
    """Per-source watermark: {source: max_ts_epoch_utc}. {} on error."""
    try:
        with open(cursor_path(hermes_home), encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cursor(cursor: dict, hermes_home=None) -> None:
    try:
        path = cursor_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cursor, fh, indent=2, sort_keys=True)
    except OSError:
        pass


def _filter_new(rows: list, cursor_ts) -> list:
    """Rows with ts_epoch_utc > cursor_ts (None ts always kept)."""
    if not cursor_ts:
        return rows
    out = []
    for r in rows:
        ts = r.get("ts_epoch_utc")
        if ts is None or ts > cursor_ts:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Writer (append-only, never raises)
# ---------------------------------------------------------------------------

def append_trace(row: dict, hermes_home=None) -> bool:
    """Append one canonical trace line. Never raises; returns success."""
    try:
        path = trace_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Objective join (from task body)
# ---------------------------------------------------------------------------

def parse_objective(body: str) -> str:
    """Parse 'objective:OBJ-xx' from the first lines of a task body.

    Returns the tag (e.g. 'OBJ-27') or 'unattributed' when absent. The
    stamp is the enforcement point (OBJ-28 design §1.3): the rollup reads
    this field, never re-parses ad-hoc.
    """
    if not body:
        return "unattributed"
    for line in body.splitlines()[:8]:
        line = line.strip()
        if line.startswith("objective:"):
            tag = line[len("objective:"):].strip()
            # The tag is the first token (the body may carry '| cost:tiny'
            # or ', auto_created:true' metadata on the same line).
            tag = tag.split("|")[0].split(",")[0].strip()
            if tag:
                return tag
    return "unattributed"


# ---------------------------------------------------------------------------
# Shadow price (catalog) for window-billed providers
# ---------------------------------------------------------------------------

def load_prices(hermes_home=None) -> dict:
    """model-cost.json prices merged over the built-in catalog. {} on error."""
    prices = dict(DEFAULT_PRICES)
    try:
        with open(model_cost_path(hermes_home), encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw.get("prices"), dict):
            for m, p in raw["prices"].items():
                if isinstance(p, (list, tuple)) and len(p) == 3:
                    prices[m] = (float(p[0]), float(p[1]), float(p[2]))
    except (OSError, ValueError, TypeError):
        pass
    return prices


def shadow_cost(model, tokens_in, tokens_out, prices=None) -> float:
    """USD shadow price = in/1e6*price_in + out/1e6*price_out. 0 if unknown."""
    prices = prices if prices is not None else DEFAULT_PRICES
    p = prices.get(model)
    if not p:
        return 0.0
    pin, pout, _pcache = p
    return (float(tokens_in or 0) / 1e6 * pin
            + float(tokens_out or 0) / 1e6 * pout)


# ---------------------------------------------------------------------------
# Collector 1: nanogpt per-request billing (requestId + costUsd REAL)
# ---------------------------------------------------------------------------

def collect_nanogpt_requests(hermes_home=None) -> list:
    """Emit trace lines from nanogpt-requests.jsonl (real costUsd).

    consumer_class=worker (these are worker API calls), consumer_id=requestId.
    No task_id in the source -> objective=unattributed (the gap shows).
    Scans every source home; dedupes by requestId.
    """
    rows = []
    seen = set()
    for home in _source_homes(hermes_home):
        try:
            with open(home / "quota-governor" / "nanogpt-requests.jsonl",
                      encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    rid = r.get("requestId") or ""
                    if rid in seen:
                        continue
                    seen.add(rid)
                    rows.append({
                        "ts_epoch_utc": _parse_ts(r.get("ts")),
                        "consumer_class": "worker",
                        "consumer_id": rid,
                        "cause": "request",
                        "model": r.get("model"),
                        "provider": r.get("provider"),
                        "tokens_in": _opt_int(r.get("inputTokens")),
                        "tokens_out": _opt_int(r.get("outputTokens")),
                        "costUsd": _opt_float(r.get("costUsd")),
                        "requestId": rid,
                        "objective": "unattributed",
                        "source": "nanogpt-requests",
                        "otel": _otel(r),
                    })
        except OSError:
            continue
    return rows


# ---------------------------------------------------------------------------
# Collector 2: cron usage_audit (tokens; costUsd = shadow price)
# ---------------------------------------------------------------------------

def collect_usage_audit(hermes_home=None) -> list:
    """Emit trace lines from cron/usage_audit.jsonl.

    consumer_class=cron-llm, consumer_id=fire_id. costUsd = shadow price
    from the catalog (window-billed provider: no per-request USD).
    objective=unattributed (no task_id in the source). Scans every source
    home; dedupes by fire_id.
    """
    prices = load_prices(hermes_home)
    rows = []
    seen = set()
    for home in _source_homes(hermes_home):
        try:
            with open(home / "cron" / "usage_audit.jsonl",
                      encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    fid = r.get("fire_id") or r.get("job_id")
                    if fid in seen:
                        continue
                    seen.add(fid)
                    tin = _opt_int(r.get("prompt_tokens"))
                    tout = _opt_int(r.get("completion_tokens"))
                    model = r.get("model")
                    rows.append({
                        "ts_epoch_utc": _parse_ts(r.get("ts")),
                        "consumer_class": "cron-llm",
                        "consumer_id": fid,
                        "cause": "cron-fire",
                        "model": model,
                        "provider": None,
                        "tokens_in": tin,
                        "tokens_out": tout,
                        "costUsd": shadow_cost(model, tin, tout, prices),
                        "requestId": None,
                        "objective": "unattributed",
                        "source": "usage-audit",
                        "otel": _otel(r),
                    })
        except OSError:
            continue
    return rows


# ---------------------------------------------------------------------------
# Collector 3: kanban task_events (claimed/completed) — the objective JOIN
# ---------------------------------------------------------------------------

def collect_task_events(hermes_home=None, kanban_db=None) -> list:
    """Emit trace lines from kanban task_events (claimed/completed).

    consumer_class=worker, consumer_id=task_id, cause=kind. The objective
    is JOINED from the task body (parse_objective) — this is the join key
    the OBJ-28 rollup consumes. No tokens/costUsd (board events carry none).
    Scans every source home for a kanban.db; dedupes by (task_id, kind, ts).
    """
    dbs = [Path(kanban_db)] if kanban_db else [
        home / "kanban.db" for home in _source_homes(hermes_home)
    ]
    rows = []
    seen = set()
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                events = con.execute(
                    "SELECT task_id, kind, created_at FROM task_events "
                    "WHERE kind IN ('claimed','completed')").fetchall()
                bodies = {}
                for ev in events:
                    key = (ev["task_id"], ev["kind"], ev["created_at"])
                    if key in seen:
                        continue
                    seen.add(key)
                    tid = ev["task_id"]
                    if tid not in bodies:
                        row = con.execute(
                            "SELECT body FROM tasks WHERE id=?",
                            (tid,)).fetchone()
                        bodies[tid] = row["body"] if row else None
                    rows.append({
                        "ts_epoch_utc": _opt_float(ev["created_at"]),
                        "consumer_class": "worker",
                        "consumer_id": tid,
                        "cause": ev["kind"],
                        "model": None,
                        "provider": None,
                        "tokens_in": None,
                        "tokens_out": None,
                        "costUsd": None,
                        "requestId": None,
                        "objective": parse_objective(bodies[tid]),
                        "source": "task-events",
                        "otel": {},
                    })
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            continue
    return rows


# ---------------------------------------------------------------------------
# Orchestrator + doctor
# ---------------------------------------------------------------------------

def run_collectors(hermes_home=None, kanban_db=None) -> dict:
    """Run all three collectors and append NEW lines to the trace.

    Incremental: a per-source cursor (max ts_epoch_utc) skips already-traced
    lines on re-runs, so a cron cadence appends only new events. Returns
    {source: count} of lines appended. Never raises.
    """
    cursor = _load_cursor(hermes_home)
    counts = {}
    for name in ("nanogpt-requests", "usage-audit", "task-events"):
        try:
            if name == "task-events":
                lines = collect_task_events(hermes_home=hermes_home,
                                            kanban_db=kanban_db)
            elif name == "nanogpt-requests":
                lines = collect_nanogpt_requests(hermes_home=hermes_home)
            else:
                lines = collect_usage_audit(hermes_home=hermes_home)
        except Exception:
            lines = []
        new_lines = _filter_new(lines, cursor.get(name))
        n = 0
        max_ts = cursor.get(name)
        for row in new_lines:
            if append_trace(row, hermes_home=hermes_home):
                n += 1
            ts = row.get("ts_epoch_utc")
            if ts is not None and (max_ts is None or ts > max_ts):
                max_ts = ts
        if n:
            cursor[name] = max_ts
        counts[name] = n
    _save_cursor(cursor, hermes_home=hermes_home)
    return counts


def doctor(hermes_home=None) -> dict:
    """obs-doctor: writable paths + JSONL parseable + count by class.

    Returns a dict with 'ok' (bool), 'path', 'writable', 'parseable',
    'lines', and 'by_class' (consumer_class -> count). Never raises.
    """
    path = trace_path(hermes_home)
    result = {
        "ok": True,
        "path": str(path),
        "writable": False,
        "parseable": True,
        "lines": 0,
        "by_class": {},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            result["writable"] = True
    except OSError:
        result["ok"] = False
        result["writable"] = False
        return result

    by_class = {}
    n = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    result["parseable"] = False
                    result["ok"] = False
                    break
                n += 1
                cls = row.get("consumer_class", "unattributed")
                by_class[cls] = by_class.get(cls, 0) + 1
    except OSError:
        result["ok"] = False
        return result
    result["lines"] = n
    result["by_class"] = by_class
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts):
    """ISO (Z or offset) or epoch -> epoch seconds. None on unparseable."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def _opt_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _otel(r: dict) -> dict:
    """OTel semconv mapping for the fields present in the source row."""
    out = {}
    for src_key, semconv in OTEL_MAP.items():
        val = r.get(src_key)
        if val is not None:
            out[semconv] = val
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="run all collectors, append to trace")
    sub.add_parser("doctor", help="check writable paths + parseable + by class")
    args = p.parse_args(argv)

    if args.cmd == "collect":
        counts = run_collectors()
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    if args.cmd == "doctor":
        print(json.dumps(doctor(), ensure_ascii=False, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
