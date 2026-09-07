#!/usr/bin/env python3
"""usd-stats.py — re-runnable USD-per-category table for OBJ-02.

Restores the analysis machinery that t_3b401256 kept in its scratch dir
(workspaces are cleaned on task completion): joining model-cost-ledger.jsonl
to kanban task_runs so the "USD cost calibration" section in
docs/quota-planner.md can be regenerated on demand instead of going stale.

Join method (same as the 2026-09-06/07 calibration, documented in
docs/quota-planner.md § USD cost calibration):

  - The ledger has no task_id; it keys on ``session_id``
    (``<YYYYMMDD>_<HHMMSS>_<hash>``, LOCAL time) created when a kanban
    worker session starts.
  - Attribution = parse the session-start timestamp, then match it into a
    ``task_runs`` window with the same profile:
    ``started_at - 10s <= session_start <= ended_at + 120s``; nearest run
    (by distance to ``started_at``) wins.
  - Ledger rows with a non-empty ``task`` tag (title_generation / approval /
    background_review / compression) are Hermes auxiliary calls, NOT worker
    work — reported separately.
  - Task category = canonical ``cost:<category>`` tag parsed from the task
    body HEADER (one-tag-per-line or pipe-separated style), reusing
    body_header_cost_tag() from objective-proposer.py — single source of
    truth for what "a parseable cost tag" means.
  - Costs are ledger ESTIMATES (token-deltas × published prices), an upper
    bound of roughly +16% vs console (see model-cost-ledger.py docstring).

Usage:
  python3 usd-stats.py                          # markdown table, last 7 days
  python3 usd-stats.py --since 2026-09-06T00:00Z --until 2026-09-08T00:00Z
  python3 usd-stats.py --json /tmp/usd.json     # also write machine-readable
  python3 usd-stats.py --db path.db --ledger path.jsonl
  python3 usd-stats.py --exclude-session 20260907_043853_e300be

Exit codes:
  0 — table produced (possibly empty)
  2 — missing inputs (ledger file / DB)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
LEDGER_FILE = os.path.expanduser(
    "~/.hermes/quota-governor/model-cost-ledger.jsonl")
PLUGIN_REPO = "REPO"
PROPOSER_CANDIDATES = [
    # Sibling in the same tree first: a worktree's usd-stats pairs with that
    # worktree's proposer, and after merge the repo copy is self-consistent.
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "objective-proposer.py"),
    os.path.join(PLUGIN_REPO, "scripts", "objective-proposer.py"),
    os.path.expanduser("~/.hermes/scripts/objective-proposer.py"),
]
RUN_WINDOW_BEFORE_S = 10    # session may start up to 10s before run.started_at
RUN_WINDOW_AFTER_S = 120    # ...and up to 120s after run.ended_at

# Categories that get their own table row (matches the backstop canon set).
CATEGORY_ORDER = ["micro", "tiny", "small", "medium", "complex"]


def _load_proposer():
    """Import body_header_cost_tag from objective-proposer (single truth)."""
    for path in PROPOSER_CANDIDATES:
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(
                f"objective_proposer_usd_{abs(hash(path))}", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit("objective-proposer.py not found (needed for tag parsing)")


def parse_session_start(session_id: str) -> Optional[float]:
    """Epoch seconds for a ``YYYYMMDD_HHMMSS_<hash>`` session id (local time)."""
    if not session_id:
        return None
    head = session_id.split("_")
    if len(head) < 2:
        return None
    try:
        return datetime.strptime(
            f"{head[0]}_{head[1]}", "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


def parse_iso(ts: Optional[str], key: str) -> Optional[float]:
    """ISO-8601 (Z or offset, or naive=UTC) → epoch seconds; None if absent."""
    if not ts:
        return None
    txt = ts.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        raise SystemExit(f"bad ISO timestamp for {key}: {ts}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_ledger(path: str, since: float, until: float,
                exclude_sessions: List[str]) -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or not (since <= ts <= until):
                continue
            if row.get("session_id") in exclude_sessions:
                continue
            rows.append(row)
    return rows


def load_runs_and_tags(db_path: str) -> Tuple[List[dict], Dict[str, Optional[str]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    runs: List[dict] = []
    for r in conn.execute(
        "SELECT id, task_id, profile, status, outcome, started_at, ended_at "
        "FROM task_runs WHERE started_at IS NOT NULL"
    ):
        runs.append(dict(r))
    tags: Dict[str, Optional[str]] = {}
    for r in conn.execute("SELECT id, body FROM tasks WHERE body IS NOT NULL"):
        tags[r["id"]] = _proposer.body_header_cost_tag(r["body"] or "")
    conn.close()
    return runs, tags


def attribute(rows: List[dict], runs: List[dict]) -> Tuple[Dict[int, list], List[dict], List[dict]]:
    """Map ledger rows to task_runs windows.

    Returns ({run_id: [rows]}, [misses], [cron_rows]).  Cron sessions
    (``cron_<jobid>_...``) are the autonomous creator/proposer LLM runs —
    real cost, but not kanban tasks; reported separately, never attributed.
    """
    buckets: Dict[int, list] = {}
    misses: List[dict] = []
    cron_rows: List[dict] = []
    for row in rows:
        sid = row.get("session_id") or ""
        if sid.startswith("cron_"):
            cron_rows.append(row)
            continue
        s = parse_session_start(sid)
        if s is None:
            misses.append(row)
            continue
        candidates = []
        for run in runs:
            if (run.get("profile") or "") != (row.get("profile") or ""):
                continue
            lo = run["started_at"] - RUN_WINDOW_BEFORE_S
            hi = (run["ended_at"] or datetime.now().timestamp()) + RUN_WINDOW_AFTER_S
            if lo <= s <= hi:
                candidates.append(run)
        if not candidates:
            misses.append(row)
            continue
        best = min(candidates, key=lambda r: abs(s - r["started_at"]))
        buckets.setdefault(best["id"], []).append(row)
    return buckets, misses, cron_rows


def _agg(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def build_report(buckets: Dict[int, list], runs: List[dict],
                 tags: Dict[str, Optional[str]], misses: List[dict],
                 cron_rows: List[dict], total_rows: int) -> dict:
    runs_by_id = {r["id"]: r for r in runs}
    # Per-ROW split: a run's session mixes worker rows (task tag empty) with
    # Hermes auxiliary rows (title_generation/approval/...).  Bucketing by
    # run first, then classifying each row, keeps worker cost clean of aux.
    worker: Dict[int, list] = {}
    aux: Dict[int, List[dict]] = {}
    for rid, rows_ in buckets.items():
        w = [r for r in rows_ if not (r.get("task") or "")]
        a = [r for r in rows_ if r.get("task") or ""]
        if w:
            worker[rid] = w
        if a:
            aux[rid] = a

    per_cat: Dict[str, Dict[str, list]] = {}
    model_rows: Dict[str, List[dict]] = {}
    for rid, w_rows in worker.items():
        run = runs_by_id[rid]
        cat = tags.get(run["task_id"]) or "untagged"
        done = run["outcome"] == "completed" or (
            run["outcome"] is None and run["status"] == "done")
        wall_min = None
        if run["ended_at"]:
            wall_min = (run["ended_at"] - run["started_at"]) / 60.0
        entry = {
            "usd": sum(r.get("cost", 0.0) for r in w_rows),
            "calls": sum(r.get("request_count", 0) or 0 for r in w_rows),
            "wall_min": wall_min,
            "models": sorted({r.get("model", "?") for r in w_rows}),
            "task_id": run["task_id"],
        }
        slot = per_cat.setdefault(cat, {"done": [], "crashed": []})
        slot["done" if done else "crashed"].append(entry)
        for r in w_rows:
            model_rows.setdefault(r.get("model", "?"), []).append(r)

    cat_table = {}
    for cat in CATEGORY_ORDER + ["untagged"]:
        if cat not in per_cat:
            continue
        d = per_cat[cat]
        done_usd = [e["usd"] for e in d["done"]]
        cat_table[cat] = {
            "done": {
                "n": len(d["done"]),
                "usd_per_task": _agg(done_usd),
                "calls_per_task": _agg([e["calls"] for e in d["done"]]),
                "wall_min": _agg([e["wall_min"] for e in d["done"]
                                  if e["wall_min"] is not None]),
                "models": sorted({m for e in d["done"] for m in e["models"]}),
                "task_ids": [e["task_id"] for e in d["done"]],
            },
            "crashed": {
                "n": len(d["crashed"]),
                "usd_per_task": _agg([e["usd"] for e in d["crashed"]]),
                "calls_per_task": _agg([e["calls"] for e in d["crashed"]]),
                "task_ids": [e["task_id"] for e in d["crashed"]],
            },
        }

    per_model = {}
    for model, rows_ in sorted(model_rows.items()):
        usd = sum(r.get("cost", 0.0) for r in rows_)
        calls = sum(r.get("request_count", 0) or 0 for r in rows_)
        per_model[model] = {
            "usd": round(usd, 4), "calls": calls,
            "usd_per_call": round(usd / calls, 6) if calls else None,
        }

    aux_kinds: Dict[str, Dict[str, list]] = {}
    for a_rows in aux.values():
        by_kind: Dict[str, list] = {}
        for r in a_rows:
            by_kind.setdefault(r.get("task") or "?", []).append(r)
        for kind, rows_ in by_kind.items():
            slot = aux_kinds.setdefault(
                kind, {"usd_per_run": [], "calls_per_run": []})
            slot["usd_per_run"].append(sum(r.get("cost", 0.0) for r in rows_))
            slot["calls_per_run"].append(
                sum(r.get("request_count", 0) or 0 for r in rows_))

    aux_table = {
        kind: {
            "runs": len(v["usd_per_run"]),
            "usd_per_run": _agg(v["usd_per_run"]),
            "calls_per_run": _agg(v["calls_per_run"]),
        }
        for kind, v in sorted(aux_kinds.items())
    }

    miss_usd = sum(r.get("cost", 0.0) for r in misses)
    cron_usd = sum(r.get("cost", 0.0) for r in cron_rows)
    cron_calls = sum(r.get("request_count", 0) or 0 for r in cron_rows)
    return {
        "window": {"since": _since_iso, "until": _until_iso},
        "ledger_rows_total": total_rows,
        "ledger_rows_attributed_worker": sum(len(w) for w in worker.values()),
        "ledger_rows_attributed_aux": sum(len(a) for a in aux.values()),
        "ledger_rows_cron": len(cron_rows),
        "cron_usd": round(cron_usd, 4),
        "cron_calls": cron_calls,
        "ledger_rows_unattributed": len(misses),
        "unattributed_usd": round(miss_usd, 4),
        "categories": cat_table,
        "per_model": per_model,
        "aux_calls": aux_table,
        "estimate_note": "ledger USD = token-delta estimate, upper bound ~+16%",
    }


def render_markdown(report: dict) -> str:
    out = ["## USD per category (usd-stats.py)",
           "",
           f"Window: {report['window']['since']} → {report['window']['until']} "
           f"(ledger rows: {report['ledger_rows_attributed_worker']} worker, "
           f"{report['ledger_rows_attributed_aux']} aux, "
           f"{report['ledger_rows_cron']} cron (${report['cron_usd']:.2f}, "
           f"{report['cron_calls']} calls), "
           f"{report['ledger_rows_unattributed']} unattributed "
           f"(${report['unattributed_usd']:.2f})). "
           f"{report['estimate_note']}.",
           "",
           "| Category | n done | USD/task (mean/med) | calls/task | wall min | models |",
           "|----------|--------|--------------------|------------|----------|--------|"]
    cats = report["categories"]
    for cat in CATEGORY_ORDER + ["untagged"]:
        if cat not in cats:
            continue
        c = cats[cat]["done"]
        u = c["usd_per_task"]
        cl = c["calls_per_task"]
        w = c["wall_min"]
        if not u.get("n"):
            continue
        out.append(
            f"| {cat} | {u['n']} | ${u.get('mean', 0):.2f} / ${u.get('median', 0):.2f} "
            f"| {cl.get('mean', 0):.0f} | {w.get('mean', 0):.0f} "
            f"| {', '.join(c['models'])} |")
        cr = cats[cat]["crashed"]
        if cr["n"]:
            cu = cr["usd_per_task"]
            out.append(
                f"| {cat} (crashed) | {cr['n']} | "
                f"${cu.get('mean', 0):.2f} / ${cu.get('median', 0):.2f} | — | — | — |")
    if not any(c in cats for c in CATEGORY_ORDER + ["untagged"]):
        out.append("| (no attributable rows in window) | — | — | — | — | — |")
    out += ["", "| Model | USD | calls | USD/call |", "|-------|-----|-------|----------|"]
    for model, m in report["per_model"].items():
        out.append(f"| {model} | ${m['usd']:.2f} | {m['calls']} "
                   f"| ${m['usd_per_call']:.4f} |" if m["usd_per_call"]
                   else f"| {model} | ${m['usd']:.2f} | {m['calls']} | — |")
    if report["aux_calls"]:
        out += ["", "Auxiliary Hermes calls per run:", "",
                "| kind | runs | USD/run (mean/med) | calls/run |",
                "|------|------|--------------------|-----------|"]
        for kind, a in report["aux_calls"].items():
            u = a["usd_per_run"]
            c = a["calls_per_run"]
            out.append(
                f"| {kind} | {a['runs']} | ${u.get('mean', 0):.4f} / "
                f"${u.get('median', 0):.4f} | {c.get('mean', 0):.0f} |")
    return "\n".join(out) + "\n"


_proposer = None
_since_iso = _until_iso = None


def main() -> int:
    global _proposer, _since_iso, _until_iso
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=KANBAN_DB)
    parser.add_argument("--ledger", default=LEDGER_FILE)
    parser.add_argument("--since", default=None,
                        help="ISO timestamp (default: now-7d)")
    parser.add_argument("--until", default=None, help="ISO timestamp (default: now)")
    parser.add_argument("--exclude-session", action="append", default=[],
                        help="session_id to exclude (repeatable) — use for the "
                             "analysis session's own worker usage")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="also write machine-readable JSON here")
    args = parser.parse_args()

    _proposer = _load_proposer()

    until = parse_iso(args.until, "--until") or datetime.now().timestamp()
    since = parse_iso(args.since, "--since") or until - 7 * 86400
    _since_iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ")
    _until_iso = datetime.fromtimestamp(until, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ")

    if not os.path.isfile(args.ledger):
        print(f"ERROR: ledger not found: {args.ledger}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.db):
        print(f"ERROR: kanban DB not found: {args.db}", file=sys.stderr)
        return 2

    rows = load_ledger(args.ledger, since, until, args.exclude_session)
    total = len(rows)
    runs, tags = load_runs_and_tags(args.db)
    buckets, misses, cron_rows = attribute(rows, runs)
    report = build_report(buckets, runs, tags, misses, cron_rows, total)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)

    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
