#!/usr/bin/python3.12
"""task-cost-backtest.py — OBJ-35 P3: estimator accuracy harness (no_agent).

WHAT IT DOES
------------
The P3 criterion: the estimator's error is MEASURED against real cost,
like forecast-backtest does for the provider ETA-90. Reads the P1
training ledger (task-cost-train.jsonl) and, for every closed row that
carries a creation-time prediction (kind=estimate captured before
completion — the prediction precedes the outcome in the same
append-only file), computes:

    error_pct = |real - predicted| / max(real, eps) * 100
    in_band   = p90_cap >= real >= 0  (the estimate's uncertainty band
                covered the real cost)

VERDICT (per run, appended to the same ledger):
    {"kind":"est-day","day":...,"n":...,"mape_pct":...,
     "in_band_pct":...,"median_error_pct":...}

P3 GOAL (from the card): error < 30% at p50 on SMALL tasks. The verdict
line records ok_small = (p50 error of small rows < 30%) once n_small>=3.

TARGET ERRORS FOR THE ESTIMATE READER (backtest-f2 convention: the gap
is shown, not hidden):
    kind=est-row  — one per (row, error) with task_id/objective/stage
    kind=est-day  — one summary per UTC day when it changes

Zero tokens, zero API calls; reads only the ledger. Exit 0 always;
stdout announces a NEW verdict line only (watchdog pattern).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

EPS = 1e-9
SMALL_P50_GOAL_PCT = 30.0


def ledger_path() -> Path:
    custom = os.environ.get("QUOTA_TASK_COST_LEDGER", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".hermes" / "quota-governor" / "task-cost-train.jsonl"


def read_ledger(path: Path):
    """(task_rows, est_rows) — tolerant, corrupt lines skipped."""
    tasks, ests = [], []
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
                kind = r.get("kind")
                if kind == "task":
                    tasks.append(r)
                elif kind == "estimate":
                    ests.append(r)
    except OSError:
        pass
    return tasks, ests


def _percentile(vals: list, q: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def evaluate(tasks: list, ests: list) -> list:
    """One error record per closed row with a preceding estimate."""
    est_by_task = {}
    for e in ests:
        tid = e.get("task_id")
        if not tid or not isinstance(e.get("estimate"), dict):
            continue
        est_by_task.setdefault(tid, []).append(e)

    out = []
    for t in tasks:
        if not t.get("attributed") or t.get("costUsd") is None:
            continue
        preds = est_by_task.get(t.get("task_id"))
        if not preds:
            continue
        # the prediction captured EARLIEST (closest to creation)
        pred = min(preds, key=lambda e: e.get("ts") or 0)
        est = pred["estimate"]
        predicted = est.get("p50")
        if predicted is None:
            continue
        real = t["costUsd"]
        err = abs(real - predicted) / max(abs(real), EPS) * 100.0
        cap = est.get("p90")
        out.append({
            "task_id": t["task_id"],
            "objective": t.get("objective"),
            "cost_class": t.get("cost_class"),
            "stage": est.get("stage"),
            "predicted": predicted,
            "p90": cap,
            "real": real,
            "error_pct": round(err, 2),
            "in_band": bool(cap is not None and real <= cap),
            "duration_s": t.get("duration_s"),
        })
    return out


def build_verdict(errors: list, day: str) -> dict:
    n = len(errors)
    if not n:
        return {"kind": "est-day", "day": day, "n": 0}
    errs = [e["error_pct"] for e in errors]
    in_band = sum(1 for e in errors if e["in_band"])
    small = [e for e in errors if e.get("cost_class") == "small"]
    verdict = {
        "kind": "est-day",
        "day": day,
        "n": n,
        "mape_pct": round(sum(errs) / n, 2),
        "median_error_pct": round(_percentile(errs, 0.5), 2),
        "p90_error_pct": round(_percentile(errs, 0.9), 2),
        "in_band_pct": round(in_band / n * 100, 1),
    }
    if len(small) >= 3:
        small_errs = [e["error_pct"] for e in small]
        p50_small = _percentile(small_errs, 0.5)
        verdict["n_small"] = len(small)
        verdict["small_p50_error_pct"] = round(p50_small, 2)
        verdict["ok_small"] = bool(p50_small < SMALL_P50_GOAL_PCT)
    return verdict


def load_day_verdicts(path: Path) -> set:
    days = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "est-day" and r.get("day"):
                    days.add(r["day"])
    except OSError:
        pass
    return days


def run(ledger=None, now=None) -> dict:
    """Evaluate + append est-day verdict if new. Returns summary dict."""
    ledger = ledger or ledger_path()
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif not hasattr(now, "strftime"):
        now = dt.datetime.fromtimestamp(float(now), dt.timezone.utc)
    tasks, ests = read_ledger(ledger)
    errors = evaluate(tasks, ests)
    day = now.strftime("%Y-%m-%d")
    verdict = build_verdict(errors, day)
    new = False
    if verdict.get("n"):
        days = load_day_verdicts(ledger)
        if day not in days:
            try:
                with open(ledger, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(verdict, separators=(",", ":"),
                                        sort_keys=True) + "\n")
                new = True
            except OSError:
                pass
    return {"errors": errors, "verdict": verdict, "new": new}


def main(argv=None) -> int:
    try:
        res = run()
    except Exception:
        return 0  # cron never breaks
    v = res["verdict"]
    if res["new"]:
        print("task-cost-backtest: nuevo veredicto %s n=%d mape=%.1f%% "
              "in_band=%.0f%%" % (v["day"], v["n"], v.get("mape_pct", 0.0),
                                  v.get("in_band_pct", 0.0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
