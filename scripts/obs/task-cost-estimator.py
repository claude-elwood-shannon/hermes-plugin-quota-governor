#!/usr/bin/python3.12
"""task-cost-estimator.py — OBJ-35 P2: empirical task-cost estimator (no_agent).

WHAT IT IS
----------
Descriptive statistics over the P1 training ledger
(task-cost-train.jsonl) — NOT machine learning. For each
(objective, cost_class, model) group it stores n / median / p90 of the
REAL historical task cost. `estimate_cost_usd(task_body)` answers, at
task-creation time, "tasks like this historically cost X (p50) and at
worst Y (p90)" — the house's first per-TASK price tag.

FALLBACK CHAIN (honest degradation, gap never hidden)
-----------------------------------------------------
  1. (objective, cost_class, model)   exact group, n >= MIN_N
  2. (cost_class, model)              drop the objective
  3. (cost_class, *)                  drop the model
  4. None                             "sin datos" — caller falls back to
      the theoretical F3 table (budget_check.COST_CLASS_PCT). The
      estimator NEVER invents a number.

Robustness rules (statistics before ML):
  * n < MIN_N (3) -> group not used; the chain falls through.
  * unattributed / costless training rows are excluded at load time.
  * outlier guard: the p90 already absorbs tail risk; no trimming.
  * zero-cost rows (free windows) are real data and stay in.

CLI
---
  report                      full table sorted by group n
  estimate --body <file>      estimate for a task body (reads stdin with -)
  estimate --objective OBJ-27 --cost-class small --model deepseek-v4-flash

Exit 0 always; report to stdout; designed for cron/manual no_agent use.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MIN_N = 3
# Reuse the trace's tag parser (the canonical stamp, OBJ-28 §1.3).
_HERE = Path(__file__).resolve().parent


def _load_trace():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "obj35_est_trace", _HERE / "trace.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_trace()
parse_objective = _trace.parse_objective

try:
    from task_cost_train import parse_cost_class, parse_clase  # type: ignore
except ImportError:  # direct-file execution: load by path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "obj35_est_train", _HERE / "task-cost-train.py")
    assert _spec is not None and _spec.loader is not None
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    parse_cost_class = _mod.parse_cost_class
    parse_clase = _mod.parse_clase


def ledger_path() -> Path:
    import os
    custom = os.environ.get("QUOTA_TASK_COST_LEDGER", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".hermes" / "quota-governor" / "task-cost-train.jsonl"


# ---------------------------------------------------------------------------
# Statistics (descriptive only)
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list, q: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def estimate(values: list) -> dict:
    """n / p50 / p90 / mean for a list of costs (USD)."""
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    if not n:
        return {"n": 0, "p50": None, "p90": None, "mean": None}
    return {
        "n": n,
        "p50": round(_percentile(vals, 0.50), 6),
        "p90": round(_percentile(vals, 0.90), 6),
        "mean": round(sum(vals) / n, 6),
    }


def load_rows(path=None) -> list:
    """Attributed training rows (costUsd present) from the ledger."""
    path = path or ledger_path()
    rows = []
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
                if r.get("kind") != "task":
                    continue
                if not r.get("attributed") or r.get("costUsd") is None:
                    continue
                rows.append(r)
    except OSError:
        pass
    return rows


def _group(rows: list, keyfunc) -> dict:
    out = {}
    for r in rows:
        k = keyfunc(r)
        if k is None:
            continue
        out.setdefault(k, []).append(r["costUsd"])
    return {k: estimate(v) for k, v in out.items()}


def build_table(rows: list) -> dict:
    """(objective, cost_class, model) -> estimate; skips unusable keys."""
    return _group(
        rows,
        lambda r: (r.get("objective"), r.get("cost_class"), r.get("model"))
        if r.get("objective") and r.get("cost_class") and r.get("model")
        and r.get("costUsd") is not None
        else None)


# ---------------------------------------------------------------------------
# Estimation with fallback chain
# ---------------------------------------------------------------------------

def estimate_cost_usd(rows: list, objective, cost_class,
                      model) -> dict:
    """Estimate with fallback chain; None stages mean 'sin datos'.

    Returns {"stage", "n", "p50", "p90", "mean"} — stage names the group
    granularity that actually produced the numbers.
    """
    chain = (
        ("objective+class+model", lambda r: (
            (r.get("objective"), r.get("cost_class"), r.get("model"))
            == (objective, cost_class, model))),
        ("class+model", lambda r: (
            (r.get("cost_class"), r.get("model"))
            == (cost_class, model))),
        ("class", lambda r: r.get("cost_class") == cost_class),
    )
    for stage, pred in chain:
        vals = [r["costUsd"] for r in rows
                if r.get("costUsd") is not None and pred(r)]
        est = estimate(vals)
        if est["n"] >= MIN_N:
            return {"stage": stage, **est}
    return {"stage": "sin-datos", "n": 0, "p50": None, "p90": None,
            "mean": None}


def estimate_for_body(rows: list, body: str, model: str = None) -> dict:
    """Parse a task body's tags and estimate. model falls back to the
    estimator's own dominant model per class, else None -> stage 'class'."""
    objective = parse_objective(body)
    cost_class = parse_cost_class(body or "")
    clase = parse_clase(body or "")
    if model is None:
        # most frequent model among historical rows of this class
        counts = {}
        for r in rows:
            if r.get("cost_class") == cost_class and r.get("model"):
                counts[r["model"]] = counts.get(r["model"], 0) + 1
        model = max(counts, key=counts.get) if counts else None
    res = estimate_cost_usd(rows, objective, cost_class, model)
    return {"objective": objective, "cost_class": cost_class,
            "clase": clase, "model": model, **res}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_report(rows: list) -> str:
    table = build_table(rows)
    if not table:
        return "task-cost-estimator: tabla vacia (sin datos atribuidos)"
    lines = ["OBJ-35 P2 task-cost table (USD, catalog estimate)",
             "group (objective, cost_class, model) | n | p50 | p90"]
    for k, v in sorted(table.items(), key=lambda kv: -kv[1]["n"]):
        lines.append("  (%s, %s, %s) | n=%d | p50=%.4f | p90=%.4f"
                     % (k[0], k[1], k[2], v["n"], v["p50"] or 0.0,
                        v["p90"] or 0.0))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="task-cost-estimator.py")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("report")
    p_est = sub.add_parser("estimate")
    p_est.add_argument("--body", help="task body text, or '-' for stdin")
    p_est.add_argument("--objective")
    p_est.add_argument("--cost-class", dest="cost_class")
    p_est.add_argument("--model")
    args = ap.parse_args(argv)

    rows = load_rows()
    if args.cmd in (None, "report"):
        print(cmd_report(rows))
        return 0
    if args.cmd == "estimate":
        if args.body == "-":
            body = sys.stdin.read()
        elif args.body:
            body = args.body
        else:
            body = ""
        if args.objective or args.cost_class:
            # explicit fields beat body tags when both present
            parsed = parse_objective(body or "")
            objective = args.objective or parsed
            cost_class = args.cost_class or parse_cost_class(body or "")
            res = estimate_cost_usd(rows, objective, cost_class, args.model)
            res.update({"objective": objective,
                        "cost_class": cost_class, "model": args.model})
        else:
            res = estimate_for_body(rows, body, args.model)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
