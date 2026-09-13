#!/usr/bin/python3.12
"""trace-backfill.py — OBJ-27 F5b: give the trace a past.

The F0 trace "dawns today": its three collectors only append what they see
from their first run onward, so every series starts mid-history. This
backfill rebuilds the 30-day consumption window from the EXISTING sources,
reusing the same collectors (import, never re-implement) and the same
canonical schema, and fills it into obs/trace.jsonl with idempotence.

WHAT IT FILLS (and what it does NOT)
------------------------------------
  source=usage-audit      cron/usage_audit.jsonl rows OLDER than the F0
                          cursor (the collector already owns everything
                          newer). Same shape: consumer_class=cron-llm,
                          shadow price from the SAME catalog (trace.py
                          load_prices — one source of truth).
  source=model-cost-ledger
                          quota-governor/model-cost-ledger.jsonl deltas
                          (opencode-go per-model estimates, Sep-06+). These
                          are NOT covered by any F0 collector: opencode-go
                          bills no per-request USD and its usage lives in
                          profile state.db session_model_usage, which the
                          F0 collectors do not read. consumer_class=worker,
                          consumer_id=session_id, costUsd=estimated (the
                          ledger's own rule: conservative UPPER bound —
                          labeled "estimated" on the portal, never billing
                          truth). Only profiles the F0 nanogpt collector
                          does not already cover (everything except
                          pr-ollama balance billing; pr-ollama rows are
                          SKIPPED here — those windows are already traced
                          as nanogpt-requests real costUsd).
  source=task-events      kanban task_events kinds beyond the F0
                          claimed/completed pair — 'created', 'crashed',
                          'gave_up', 'timed_out', 'spawn_failed' — so the
                          board's created/crashed 30d history exists in the
                          trace the way the F2 crash-loop check already
                          expects claimed/completed to be.

NOT backfilled (the gap stays visible, never fabricated):
  - nanogpt-requests: the real-cost source is a 1-line file from Sep-09;
    there is no deeper history anywhere on the host. The collector's own
    cursor covers what exists.
  - Anything with costUsd=None stays None: no prices are invented for rows
    that carry no tokens, no estimates are fabricated where a source has
    no basis.

IDEMPOTENCE
-----------
Two layers (F0's cursor is SACRED — never read or written here):
  1. cursor: obs/backfill-cursor.json — per-source high-water mark. First
     pass ingests everything older than the F0 cursor; later passes are
     no-ops.
  2. natural keys: every row carries a dedup key (source, consumer_id,
     cause, ts). The writer loads the ACTIVE trace's keys, rewrites the
     file with the union (backfill rows merged in by timestamp), and only
     counts a row as "appended" when its key was genuinely new. Rotation,
     concurrent collectors, or a lost cursor can therefore never duplicate
     history.

ORDER IS PRESERVED: the file is rewritten sorted by (ts, natural key)
with ts-less rows kept at the head (the trace never silently drops what
it cannot date; ordering by ts keeps the portal's series sane).

OBSERVER-ONLY: reads the sources read-only (sqlite mode=ro), appends only
to trace.jsonl. Never touches trace-cursor.json, never modifies a source.
Fails open: a missing source is an empty result, never a crash.

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes); sources are scanned across the same _source_homes as F0. No
absolute host paths in this module (the portability test enforces it).
Zero tokens, no network, stdlib only. Exit 0 always.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Sibling import: trace.py is the single source of truth for the canonical
# schema, the paths, the price catalog and the two JSONL collectors we
# extend. Hyphenated dirs force the importlib pattern (F0 test precedent).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
import importlib.util

_spec = importlib.util.spec_from_file_location("obs_trace", _HERE / "trace.py")
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise SystemExit("trace-backfill: cannot load sibling trace.py")
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)

CURSOR_NAME = "backfill-cursor.json"

# task-events kinds the F0 collector does NOT collect; backfilled here so
# the board history (created / crashes) exists in the trace.
TASK_EVENT_KINDS = ("created", "crashed", "gave_up", "timed_out", "spawn_failed")

# model-cost-ledger: skip these profiles — their windows are ALREADY traced
# by the F0 nanogpt-requests collector as real costUsd (no double count).
# opencode-go's estimates are the actual gap this source fills.
MCL_SKIP_PROFILES = {"pr-ollama"}


# ---------------------------------------------------------------------------
# Backfill cursor (own file; the F0 cursor is sacred and never touched)
# ---------------------------------------------------------------------------

def cursor_path(hermes_home=None) -> Path:
    return tr.obs_dir(hermes_home) / CURSOR_NAME


def _load_cursor(hermes_home=None) -> dict:
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


# ---------------------------------------------------------------------------
# Natural dedup key
# ---------------------------------------------------------------------------

def dedup_key(row: dict) -> tuple:
    """(source, consumer_id, cause, ts) — the row's identity in the trace."""
    return (row.get("source"), row.get("consumer_id"),
            row.get("cause"), row.get("ts_epoch_utc"))


# ---------------------------------------------------------------------------
# Source 1: historical usage-audit (older than the F0 cursor watermark)
# ---------------------------------------------------------------------------

def backfill_usage_audit(hermes_home=None, before_ts=None) -> list:
    """usage-audit rows OLDER than the F0 collector's watermark.

    Delegates row construction to trace.collect_usage_audit (same shape,
    same prices, same dedupe by fire_id) and keeps only the past.
    """
    cursor = tr._load_cursor(hermes_home)
    floor = cursor.get("usage-audit")
    if before_ts is not None:
        floor = before_ts if floor is None else min(floor, float(before_ts))
    rows = [r for r in tr.collect_usage_audit(hermes_home=hermes_home)
            if r.get("ts_epoch_utc") is not None
            and (floor is None or r["ts_epoch_utc"] < floor)]
    rows.sort(key=lambda r: r["ts_epoch_utc"])
    return rows


# ---------------------------------------------------------------------------
# Source 2: model-cost-ledger deltas (opencode-go + other non-nanogpt
# profiles) — the only per-model consumption history opencode-go has.
# ---------------------------------------------------------------------------

def _mcl_row(r: dict) -> dict:
    """model-cost-ledger delta -> one canonical trace line.

    costUsd comes from the ledger's own estimate (its documented rule:
    conservative UPPER bound, labels included downstream). The shadow
    catalog is NOT re-applied here — that would double-price the delta.
    """
    tok = r.get("tokens") or {}
    tin = tok.get("in")
    tout = tok.get("out")
    model = r.get("model")
    return {
        "ts_epoch_utc": tr._opt_float(r.get("ts")),
        "consumer_class": "worker",
        "consumer_id": r.get("session_id"),
        "cause": "session-delta",
        "model": model,
        "provider": "opencode-go",
        "tokens_in": tr._opt_int(tin),
        "tokens_out": tr._opt_int(tout),
        "costUsd": tr._opt_float(r.get("cost")),
        "requestId": None,
        "objective": "unattributed",
        "source": "model-cost-ledger",
        "otel": {
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": tin,
            "gen_ai.usage.output_tokens": tout,
        },
    }


def backfill_model_cost_ledger(hermes_home=None) -> list:
    """Ledger deltas -> trace lines. Dedupes by natural key, skips profiles
    already covered by the F0 nanogpt-requests collector, and honors the
    per-source high-water mark so repeated runs are no-ops."""
    cursor = _load_cursor(hermes_home)
    seen_ts = cursor.get("model-cost-ledger")
    homes = tr._source_homes(hermes_home)
    rows = []
    seen_keys = set()
    for home in homes:
        path = home / "quota-governor" / "model-cost-ledger.jsonl"
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
                    ts = tr._opt_float(r.get("ts"))
                    if ts is None:
                        continue
                    if seen_ts is not None and ts <= seen_ts:
                        continue
                    if (r.get("profile") or "") in MCL_SKIP_PROFILES:
                        continue
                    row = _mcl_row(r)
                    k = dedup_key(row)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda r: r["ts_epoch_utc"])
    return rows


# ---------------------------------------------------------------------------
# Source 3: task-events board history (created / crashes / timeouts)
# ---------------------------------------------------------------------------

def backfill_task_events(hermes_home=None, kanban_db=None) -> list:
    """task_events kinds beyond the F0 claimed/completed pair.

    Same join rule as F0 (parse_objective on the task body) so the board
    history attributes to objectives like everything else. Dedupes by the
    F0 natural key (task_id, kind, ts) AND against claimed/completed keys
    (a task created AND claimed in the same second yields two events).
    """
    dbs = [Path(kanban_db)] if kanban_db else [
        home / "kanban.db" for home in tr._source_homes(hermes_home)]
    rows = []
    seen = set()
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            events = con.execute(
                "SELECT task_id, kind, created_at FROM task_events "
                f"WHERE kind IN ({','.join('?' * len(TASK_EVENT_KINDS))})",
                TASK_EVENT_KINDS).fetchall()
            bodies = {}
            for ev in events:
                key = (ev["task_id"], ev["kind"], ev["created_at"])
                if key in seen:
                    continue
                seen.add(key)
                tid = ev["task_id"]
                if tid not in bodies:
                    row = con.execute(
                        "SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()
                    bodies[tid] = row["body"] if row else None
                rows.append({
                    "ts_epoch_utc": tr._opt_float(ev["created_at"]),
                    "consumer_class": "worker",
                    "consumer_id": tid,
                    "cause": ev["kind"],
                    "model": None,
                    "provider": None,
                    "tokens_in": None,
                    "tokens_out": None,
                    "costUsd": None,
                    "requestId": None,
                    "objective": tr.parse_objective(bodies[tid]),
                    "source": "task-events",
                    "otel": {},
                })
        except sqlite3.Error:
            continue
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass
    rows.sort(key=lambda r: (r["ts_epoch_utc"] is None, r["ts_epoch_utc"] or 0))
    return rows


# ---------------------------------------------------------------------------
# Merge + write: union with the active trace, ordered, dedup by natural key
# ---------------------------------------------------------------------------

def read_existing_keys(hermes_home=None) -> set:
    """Natural keys already present in the active trace. Never raises."""
    keys = set()
    try:
        with open(tr.trace_path(hermes_home), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(dedup_key(json.loads(line)))
                except ValueError:
                    continue
    except OSError:
        pass
    return keys


def merge_and_write(rows: list, hermes_home=None) -> dict:
    """Merge backfill rows into the active trace (dedup by natural key).

    The file is REWRITTEN once: existing lines are kept byte-identical
    (including corrupt lines — never silently dropped), new rows are
    inserted sorted by (ts, key) with ts-less rows kept at the head.
    Returns {appended, duplicates, kept_lines}.
    """
    keys = read_existing_keys(hermes_home)
    existing = []
    bad_lines = []
    try:
        with open(tr.trace_path(hermes_home), encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    existing.append(json.loads(s))
                except ValueError:
                    bad_lines.append(s)
    except OSError:
        pass

    fresh, dup = [], 0
    for r in rows:
        k = dedup_key(r)
        if k in keys:
            dup += 1
            continue
        keys.add(k)
        fresh.append(r)

    def _order(r):
        ts = r.get("ts_epoch_utc")
        if not isinstance(ts, (int, float)):
            return (0, 0.0, ())          # undatable rows first, stable
        return (1, float(ts), tuple(str(x) for x in dedup_key(r)))

    merged = sorted(existing + fresh, key=_order)
    path = tr.trace_path(hermes_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-backfill")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in merged:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            for line in bad_lines:
                fh.write(line + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        return {"appended": 0, "duplicates": dup, "kept_lines": len(existing),
                "error": repr(exc)}
    return {"appended": len(fresh), "duplicates": dup,
            "kept_lines": len(existing), "bad_lines": len(bad_lines)}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_backfill(hermes_home=None, before_ts=None, dry_run=False) -> dict:
    """Collect every backfill source, merge into the trace, save the cursor.

    Returns a report {source: collected} plus {appended, duplicates}.
    Never raises. Dry-run reports what WOULD be written without touching
    the trace or the cursor.
    """
    report = {"ok": True, "dry_run": bool(dry_run), "sources": {},
              "appended": 0, "duplicates": 0}
    try:
        batches = {
            "usage-audit": backfill_usage_audit(
                hermes_home=hermes_home, before_ts=before_ts),
            "model-cost-ledger": backfill_model_cost_ledger(
                hermes_home=hermes_home),
            "task-events": backfill_task_events(hermes_home=hermes_home),
        }
        report["sources"] = {k: len(v) for k, v in batches.items()}
        rows = [r for v in batches.values() for r in v]
        if dry_run:
            keys = read_existing_keys(hermes_home)
            fresh = [r for r in rows if dedup_key(r) not in keys]
            report["appended"] = len(fresh)
            report["duplicates"] = len(rows) - len(fresh)
            return report
        res = merge_and_write(rows, hermes_home=hermes_home)
        report["appended"] = res.get("appended", 0)
        report["duplicates"] = res.get("duplicates", 0)
        report["kept_lines"] = res.get("kept_lines", 0)
        if res.get("error"):
            report["ok"] = False
            report["error"] = res["error"]

        # cursors: per-source high-water marks (ours, never the F0 one) —
        # plus one F0 sync for fresh adoptants: when the F0 collector has
        # NO cursor for a source yet, the backfill's high-water mark is
        # seeded there too. Otherwise the collector would re-append the
        # same history after its first run and double-count it. When the
        # F0 cursor EXISTS it is sacred and never touched.
        cursor = _load_cursor(hermes_home)
        f0_cursor = tr._load_cursor(hermes_home)
        f0_dirty = False
        for name in ("usage-audit", "model-cost-ledger"):
            marks = [r.get("ts_epoch_utc") for r in batches.get(name, [])
                     if isinstance(r.get("ts_epoch_utc"), (int, float))]
            if not marks:
                continue
            top = max(marks)
            if top > (cursor.get(name) or 0.0):
                cursor[name] = top
            if name == "usage-audit" and "usage-audit" not in f0_cursor:
                f0_cursor["usage-audit"] = top
                f0_dirty = True
        _save_cursor(cursor, hermes_home=hermes_home)
        if f0_dirty:
            tr._save_cursor(f0_cursor, hermes_home=hermes_home)
    except Exception as exc:  # fail open, like every public helper here
        report["ok"] = False
        report["error"] = repr(exc)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be backfilled, write nothing")
    p.add_argument("--json", action="store_true",
                   help="emit the report as JSON (default: human lines)")
    args = p.parse_args(argv)
    rep = run_backfill(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        status = "ok" if rep.get("ok") else "ERROR"
        print(f"backfill {status}: appended={rep.get('appended')} "
              f"duplicates={rep.get('duplicates')} "
              f"sources={rep.get('sources')}")
        if rep.get("error"):
            print(f"  error: {rep['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
