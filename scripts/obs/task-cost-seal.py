#!/usr/bin/python3.12
"""task-cost-seal.py — OBJ-35 P2/P3 hook: capture the estimate at creation.

Called with a task body (and optional model) any time AFTER a task is
created and BEFORE it closes; appends one 'estimate' line to the P1
ledger:

  {"kind":"estimate","task_id":"t_...","ts":...,
   "estimate":{"p50":...,"p90":...,"stage":...,"model":...}}

task-cost-train.py later attaches this prediction to the closed task's
row, and task-cost-backtest.py measures prediction-vs-real error. This
ordering (prediction BEFORE outcome, same append-only file) is what
makes P3 honest.

CLI:
  task-cost-seal.py --task-id t_xxx --body-file body.md [--model m]
  echo "objective:OBJ-1 | cost:small" | task-cost-seal.py --task-id t_x --body -

Exit 0 always; stdout: one summary line when an estimate was recorded
(and 'sin datos' stage when there is no history — recorded too, so the
absence of history is itself tracked).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_est = _load("obj35_seal_estimator", _HERE / "task-cost-estimator.py")
_train = _load("obj35_seal_train", _HERE / "task-cost-train.py")


def ledger_path() -> Path:
    return _train.ledger_path()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="task-cost-seal.py")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--body-file", required=True,
                    help="path to task body, or '-' for stdin")
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)

    try:
        if args.body_file == "-":
            body = sys.stdin.read()
        else:
            body = Path(args.body_file).read_text(encoding="utf-8")

        rows = _est.load_rows()
        res = _est.estimate_for_body(rows, body, args.model)
        line = {
            "kind": "estimate",
            "task_id": args.task_id,
            "ts": time.time(),
            "estimate": {
                "p50": res.get("p50"),
                "p90": res.get("p90"),
                "n": res.get("n"),
                "stage": res.get("stage"),
                "model": res.get("model"),
                "objective": res.get("objective"),
                "cost_class": res.get("cost_class"),
            },
        }
        ledger = ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":"),
                                sort_keys=True) + "\n")
        print("task-cost-seal: %s stage=%s p50=%s p90=%s" % (
            args.task_id, res.get("stage"), res.get("p50"), res.get("p90")))
    except Exception as e:
        print("task-cost-seal: degrade (%s)" % e.__class__.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
