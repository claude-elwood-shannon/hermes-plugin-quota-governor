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

F3 RETENTION / ROTATION (the trace must not grow without limit)
---------------------------------------------------------------
The active file holds a bounded window; history is never destroyed, only
compacted. ``enforce_retention()`` (CLI: ``retention``, ``--dry-run`` to
preview) moves rows older than keep_days — plus the oldest overflow
beyond max_lines — into a gzip archive (obs/archive/trace-<stamp>.jsonl.gz,
write-ahead, then atomic os.replace of the active file) and GCs archives
beyond keep_archives. Defaults: 14 days / 100k lines / 12 archives,
overridable via QUOTA_GOVERNOR_TRACE_{KEEP_DAYS,MAX_LINES,KEEP_ARCHIVES}.

THE CURSOR IS SACRED: rotation never reads, writes or removes
trace-cursor.json. The collector cursor is a per-source timestamp
watermark, independent of the active file's contents, so idempotency
survives rotation (test: collect -> rotate -> collect appends nothing
twice). Corrupt lines and rows without a parseable ts are preserved in
the active file — never silently dropped.

doctor() reports the trace's size and age (trace_bytes, oldest/newest
epoch, age_days), the effective policy, the archives, and
needs_rotation=True when the bounds are already exceeded.

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times are epoch UTC.
"""
from __future__ import annotations

import datetime as dt
import gzip
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


# ---------------------------------------------------------------------------
# F3: retention / rotation (the trace must not grow without limit)
# ---------------------------------------------------------------------------

DEFAULT_KEEP_DAYS = 14.0     # time window: rows older than this are archived
DEFAULT_MAX_LINES = 100_000  # hard cap on active-file rows (overflow archived)
DEFAULT_KEEP_ARCHIVES = 12   # archive GC: keep at most N .jsonl.gz files

_ENV_KEEP_DAYS = "QUOTA_GOVERNOR_TRACE_KEEP_DAYS"
_ENV_MAX_LINES = "QUOTA_GOVERNOR_TRACE_MAX_LINES"
_ENV_KEEP_ARCHIVES = "QUOTA_GOVERNOR_TRACE_KEEP_ARCHIVES"


def archive_dir(hermes_home=None) -> Path:
    """Rotated archives live here: obs/archive/trace-<stamp>.jsonl.gz."""
    return obs_dir(hermes_home) / "archive"


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip())
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def retention_policy(hermes_home=None) -> dict:
    """Effective policy: env overrides over the built-in defaults."""
    return {
        "keep_days": _env_float(_ENV_KEEP_DAYS, DEFAULT_KEEP_DAYS),
        "max_lines": _env_int(_ENV_MAX_LINES, DEFAULT_MAX_LINES),
        "keep_archives": _env_int(_ENV_KEEP_ARCHIVES,
                                  DEFAULT_KEEP_ARCHIVES),
    }


def _read_trace_lines(path: Path) -> tuple:
    """(good_rows, bad_lines) in file order. Never raises."""
    rows, bad = [], []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad.append(line)
    except OSError:
        pass
    return rows, bad


def _plan_eviction(rows: list, keep_days: float, max_lines: int,
                   now: float) -> tuple:
    """Split rows into (keep, evict) under window + cap policy.

    Window rule: ts < now - keep_days*86400 is evicted — EXCEPT rows
    without a parseable ts (None), which are never window-evicted (the
    trace never silently drops what it cannot date). Cap rule: when the
    survivor count still exceeds max_lines, the OLDEST survivors are
    evicted until the file holds exactly the newest max_lines rows.
    """
    cutoff = now - keep_days * 86400.0
    keep, evict = [], []
    for r in rows:
        ts = r.get("ts_epoch_utc")
        if ts is not None and ts < cutoff:
            evict.append(r)
        else:
            keep.append(r)
    if len(keep) > max_lines:
        # newest stay; the oldest overflow -> archive
        keep_sorted = sorted(
            keep, key=lambda r: (r.get("ts_epoch_utc") is not None,
                                 r.get("ts_epoch_utc") or 0.0))
        overflow = keep_sorted[: len(keep) - max_lines]
        evict.extend(overflow)
        keep = keep_sorted[len(keep) - max_lines:]
    return keep, evict


def _gc_archives(adir: Path, keep_n: int, errors: list) -> int:
    """Delete the oldest archive files beyond keep_n. Returns deleted count."""
    try:
        archives = sorted(adir.glob("trace-*.jsonl.gz"))
    except OSError:
        return 0
    excess = archives[:-keep_n] if keep_n > 0 else archives
    dropped = 0
    for old in excess:
        try:
            old.unlink()
            dropped += 1
        except OSError as exc:
            errors.append(f"gc: {exc}")
    return dropped


def enforce_retention(hermes_home=None, keep_days=None, max_lines=None,
                      keep_archives=None, dry_run=False,
                      now=None) -> dict:
    """Rotate the trace under the retention policy; never raise.

    Contract (the cursor is sacred):
      - Reads the active trace once; splits rows into keep/evict.
      - Evicted rows are APPENDED to a gzip archive FIRST (write-ahead),
        then the active file is replaced atomically (os.replace). A crash
        between the two leaves a superset on disk — nothing is lost.
      - The incremental cursor file (trace-cursor.json) is NEVER read,
        written or removed here: collectors stay idempotent regardless of
        what this rotation removes from the active file.
      - Corrupt (non-JSON) lines and ts-less rows stay in the active file.
      - Expired archive files beyond keep_archives are deleted.
    Returns a report dict {ok, rotated, reason, archived, kept, ...}.
    """
    report = {
        "ok": True, "rotated": False, "reason": "none", "archived": 0,
        "kept": 0, "dropped_archives": 0, "archive_path": None,
        "dry_run": bool(dry_run), "cutoff_epoch": None, "errors": [],
    }
    try:
        policy = retention_policy(hermes_home)
        kd = policy["keep_days"] if keep_days is None else float(keep_days)
        ml = policy["max_lines"] if max_lines is None else int(max_lines)
        ka = (policy["keep_archives"] if keep_archives is None
              else int(keep_archives))
        report["policy"] = {"keep_days": kd, "max_lines": ml,
                            "keep_archives": ka}
        now = time.time() if now is None else float(now)
        path = trace_path(hermes_home)
        if not path.exists():
            return report
        rows, bad_lines = _read_trace_lines(path)
        if not rows and not bad_lines:
            return report

        keep, evict = _plan_eviction(rows, kd, ml, now)
        report["cutoff_epoch"] = now - kd * 86400.0
        report["kept"] = len(keep)
        report["archived"] = len(evict)
        if not evict and len(keep) == len(rows):
            report["reason"] = "none"
            # still GC expired archive files even when nothing rotated
            report["dropped_archives"] = _gc_archives(
                archive_dir(hermes_home), ka, report["errors"])
            return report
        report["reason"] = "window" if evict and any(
            (r.get("ts_epoch_utc") is not None
             and r.get("ts_epoch_utc") < report["cutoff_epoch"])
            for r in evict) else "lines"
        if dry_run:
            return report

        if evict:
            adir = archive_dir(hermes_home)
            adir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
            apath = adir / f"trace-{stamp}.jsonl.gz"
            # write-ahead: archive FIRST, only then touch the active file
            with gzip.open(apath, "at", encoding="utf-8") as fh:
                for r in evict:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            report["archive_path"] = str(apath)

        # atomic replace of the active file: recent rows + preserved
        # corrupt lines (they are never silently dropped)
        tmp = path.with_name(path.name + ".tmp-rotate")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in keep:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            for line in bad_lines:
                fh.write(line + "\n")
        os.replace(tmp, path)
        report["rotated"] = True

        # archive GC: keep only the newest ka archive files
        report["dropped_archives"] = _gc_archives(
            archive_dir(hermes_home), ka, report["errors"])
    except Exception as exc:  # fail open, like every public helper here
        report["ok"] = False
        report["errors"].append(repr(exc))
    return report


def doctor(hermes_home=None) -> dict:
    """obs-doctor: writable paths + JSONL parseable + count by class.

    F3 extension: reports the trace's size and age (bytes, oldest/newest
    epoch, age_days), the effective retention policy, the archive files,
    and needs_rotation=True when policy bounds are already exceeded.
    Returns a dict with 'ok' (bool), 'path', 'writable', 'parseable',
    'lines', 'by_class' (consumer_class -> count) plus the F3 keys.
    Never raises.
    """
    path = trace_path(hermes_home)
    result = {
        "ok": True,
        "path": str(path),
        "writable": False,
        "parseable": True,
        "lines": 0,
        "by_class": {},
        # F3:
        "trace_bytes": 0,
        "oldest_epoch": None,
        "newest_epoch": None,
        "age_days": None,
        "retention": retention_policy(hermes_home),
        "archives": {"count": 0, "bytes": 0},
        "needs_rotation": False,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            result["writable"] = True
    except OSError:
        result["ok"] = False
        result["writable"] = False
        return result

    if path.exists():
        try:
            result["trace_bytes"] = path.stat().st_size
        except OSError:
            result["trace_bytes"] = 0

    by_class = {}
    n = 0
    oldest = newest = None
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
                ts = row.get("ts_epoch_utc")
                if isinstance(ts, (int, float)):
                    ts = float(ts)
                    oldest = ts if oldest is None else min(oldest, ts)
                    newest = ts if newest is None else max(newest, ts)
    except OSError:
        result["ok"] = False
        return result
    result["lines"] = n
    result["by_class"] = by_class
    result["oldest_epoch"] = oldest
    result["newest_epoch"] = newest
    if oldest is not None:
        result["age_days"] = (time.time() - oldest) / 86400.0

    try:
        files = sorted(archive_dir(hermes_home).glob("trace-*.jsonl.gz"))
        result["archives"] = {
            "count": len(files),
            "bytes": sum((f.stat().st_size for f in files), 0),
        }
    except OSError:
        pass

    # needs_rotation: policy bounds already exceeded (window or cap).
    pol = result["retention"]
    if oldest is not None and n > 0:
        cutoff = time.time() - pol["keep_days"] * 86400.0
        if oldest < cutoff or n > pol["max_lines"]:
            result["needs_rotation"] = True
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
    sub.add_parser("doctor", help=("check writable paths + parseable + "
                                   "by class + size/age/archives (F3)"))
    p_ret = sub.add_parser(
        "retention", help=("rotate the trace under the retention policy "
                           "(archive+rotate; never touches the cursor)"))
    p_ret.add_argument("--dry-run", action="store_true",
                       help="report the plan without writing anything")
    args = p.parse_args(argv)

    if args.cmd == "collect":
        counts = run_collectors()
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    if args.cmd == "doctor":
        print(json.dumps(doctor(), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "retention":
        rep = enforce_retention(dry_run=args.dry_run)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep.get("ok") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
