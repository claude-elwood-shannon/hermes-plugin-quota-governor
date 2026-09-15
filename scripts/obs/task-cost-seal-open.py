#!/usr/bin/python3.12
"""task-cost-seal-open.py — OBJ-METRICS P3: deferred seal for open tasks.

THE HUECO (gap) THIS FILLS
-------------------------
task-cost-seal.py captures the P3 estimate at task-creation time, but
nothing invokes it there — so the ledger had 0 'estimate' rows and the
backtest could never pair prediction vs real cost (P3 unreachable). This
pass closes the loop honestly: it seals tasks that are STILL OPEN
(status ready/running, completed_at NULL), so the prediction still
precedes the outcome in the same append-only ledger. A task sealed here
has already started (its body and model are known, its cost is not),
which makes the estimate slightly less "creation-time" than the ideal
hook — but it precedes the result, which is what backtest honesty needs.

IDEMPOTENCE
-----------
A task is skipped when the ledger already holds a kind=estimate row for
its task_id (sealed here before, or by task-cost-seal.py at creation).
Existing ledger rows are NEVER edited or removed (append-only).

Usage:
  task-cost-seal-open.py            seal every pending open task
  task-cost-seal-open.py --dry-run  print what WOULD be sealed, write nothing

Env overrides (same as task-cost-train.py):
  QUOTA_TASK_COST_LEDGER     ledger jsonl path
  QUOTA_TASK_COST_KANBAN_DB  kanban.db path

Exit 0 always; stdout only when something new was sealed (watchdog
pattern — silent cron tick means no news).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    """Import a sibling repo module by file path (seal.py pattern)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_seal = _load("obj_p3_seal_open_seal", _HERE / "task-cost-seal.py")
_train = _load("obj_p3_seal_open_train", _HERE / "task-cost-train.py")


def load_estimated_task_ids(ledger: Path) -> set:
    """task_ids that already have a kind=estimate row in the ledger.

    Tolerant reader: unreadable file or malformed lines yield partial/
    empty sets, never a raise (fail-open convention).
    """
    ids = set()
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
                if r.get("kind") == "estimate" and r.get("task_id"):
                    ids.add(r["task_id"])
    except OSError:
        pass
    return ids


def load_open_tasks(kanban_db: Path) -> list:
    """Still-open tasks (ready/running, no completed_at) with body+model.

    Read-only connection; any sqlite error returns [] (the cron must not
    break on a locked or missing board). The 'model' key carries
    model_override — the board has no per-task model column otherwise.
    """
    if not kanban_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, body, model_override FROM tasks "
                "WHERE status IN ('ready','running') "
                "  AND completed_at IS NULL"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def seal_open(dry_run: bool = False, ledger: Path | None = None,
              kanban_db: Path | None = None,
              now: float | None = None) -> list:
    """Seal every open task lacking an estimate row. Returns sealed lines.

    One entry per newly sealed task: {"task_id", "stage", "p50", "p90"}.
    dry_run computes the same estimates but writes nothing. Never raises
    on per-task failures — a bad body degrades and the rest continue.
    """
    ledger = Path(ledger) if ledger is not None else Path(_seal.ledger_path())
    kanban_db = (Path(kanban_db) if kanban_db is not None
                 else Path(_train.kanban_db_path()))
    now = now if now is not None else time.time()

    already = load_estimated_task_ids(ledger)
    sealed = []
    for t in sorted(load_open_tasks(kanban_db), key=lambda x: x["id"]):
        if t["id"] in already:
            continue
        body = (t.get("body") or "").strip()
        if not body:
            continue  # no tags to estimate from; nothing honest to record
        try:
            if dry_run:
                res = _seal._est.estimate_for_body(
                    _seal._est.load_rows(), body, t.get("model_override"))
            else:
                res = _seal.seal_one(t["id"], body,
                                     t.get("model_override"), now=now)
        except Exception as e:  # degrade: board keeps working next tick
            print("task-cost-seal-open: degrade %s (%s)"
                  % (t["id"], e.__class__.__name__))
            continue
        sealed.append({"task_id": t["id"], "stage": res.get("stage"),
                       "p50": res.get("p50"), "p90": res.get("p90")})
    return sealed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="task-cost-seal-open.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sealed; write nothing")
    args = ap.parse_args(argv)
    try:
        sealed = seal_open(dry_run=args.dry_run)
    except Exception as e:  # the cron never breaks
        print("task-cost-seal-open: degrade (%s)" % e.__class__.__name__)
        return 0
    prefix = "would seal" if args.dry_run else "task-cost-seal-open: sealed"
    for s in sealed:
        print("%s %s stage=%s p50=%s p90=%s" % (
            prefix, s["task_id"], s["stage"], s["p50"], s["p90"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
