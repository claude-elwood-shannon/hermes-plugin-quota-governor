#!/usr/bin/python3.12
"""objective-budgets.py — OBJ-28 Phase 0 (observer): rollup by objective tag.

WHAT IT IS
----------
The house keeps its own ledger. OBJ-27 F0's trace (obs/trace.jsonl) is the
system nervous system; this module is the metabolism on top of it: a pure
READ-ONLY function over the trace that aggregates spend and task flow per
``objective:OBJ-xx`` tag and writes ``quota-governor/objective-budgets.json``
(portable config + state, never absolute paths in the repo).

THE GAP IS SHOWN, NOT HIDDEN (same rule as OBJ-27)
--------------------------------------------------
The three cost-bearing trace sources (nanogpt-requests, usage-audit,
model-cost-ledger) carry NO task_id — every cost line is objective=
"unattributed" TODAY. Task-events lines carry the objective stamp but no
cost. A rollup that pretended otherwise would be a fabricated budget; this
observer therefore:

  1. Aggregates ALL cost into an explicit ``unattributed`` bucket (visible
     head of the report, never hidden).
  2. Rolls up per-objective TASK FLOW (created/claimed/completed/crashed/
     gave_up/timed_out/spawn_failed counts) from task-events.
  3. Emits ``cost_unattributed_usd`` per line so the calibration phase can
     see what an ``objective:`` stamp would be worth (what fraction of the
     house bill would become attributable if sources carried task_id).

DUAL CURRENCY (design §2 — never compare fractions across providers)
--------------------------------------------------------------------
``costUsd`` is the only cross-provider-comparable meter (balance-billed
providers); ``quota_pct`` is the operational meter for window-billed
providers where no per-request USD exists. Per the dual-currency rule the
rollup NEVER sums quota fractions across providers: any per-provider quota
sub-bucketing is left to the calibration phase once sources carry the
objective stamp. costUsd aggregates across providers; the per-provider
spend sub-buckets are kept SEPARATE (``by_provider``), never summed into
a quota fraction.

OBSERVER-ONLY: reads the trace read-only, never writes the trace, never
touches the cursor (trace.py's cursor is sacred), never mutates the board.
No veto, no gate, no enforce path — phases 1-3 (calibration, enforce-warn,
enforce-hard-stop) require the user's approval first (class B).

STATUS LADDER (computed here, ACTED ON only by approved phases)
---------------------------------------------------------------
open -> warn (spent >= warn_fraction * budget) -> exhausted (spent >=
hard_stop_fraction * budget) -> done (all tasks done, no further spend).
Phase 0 COMPUTES the status and keeps it derived-only: budgets stay null
until the user approves defaults (design §4), so with no ceiling set the
ladder cannot trip and nothing can ACT on it anyway (no dispatch
blocking, no board writes). Human-set ceilings (budget_*, *_fraction)
and notes are PRESERVED across rollups — the machine owns the meter,
the human owns the ceiling. The first breach is a review signal, not a
punishment (Goodhart mitigation).

VARIANCE (design §5.2): armed, not exercised. A task whose actual spend
exceeds its cost-class estimate by >3x triggers human review in later
phases; the rule ships here (variance_breach) but phase 0 cannot use it:
cost lines carry no task_id, so no per-task actual exists to compare.
The docstring and the _meta note say exactly that.

IDEMPOTENCE / SHAPE
-------------------
Rollup is deterministic and idempotent: same trace -> same file (modulo
timestamps). Run frequency is free (zero tokens, no network, stdlib only).
Exit 0 always; every public helper fails open (a missing trace is an empty
rollup, never a crash). Portability: paths resolve through get_hermes_home()
(HERMES_HOME env or ~/.hermes); no absolute host paths in this module (the
portability test enforces it).

PORTAL HOOK (design §7 report): ``--json`` prints the full rollup as JSON
(stdout = evidence, watchdog pattern: empty stdout would mean no state).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / imports (same conventions as trace.py — import, never re-implement)
# ---------------------------------------------------------------------------

GOV_DIR = Path(__file__).resolve().parent.parent.parent
_OBS_TRACE = GOV_DIR / "scripts" / "obs" / "trace.py"

_spec = None
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("obs_trace", _OBS_TRACE)
    trace = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(trace)
except Exception:  # pragma: no cover - fails open, never into a caller
    trace = None

if trace is not None:
    get_hermes_home = trace.get_hermes_home
    state_dir = trace.state_dir
    trace_path = trace.trace_path
    parse_objective = trace.parse_objective
else:  # pragma: no cover - fallback mirrors trace.py conventions
    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()

    def state_dir(hermes_home=None) -> Path:
        return (Path(hermes_home) if hermes_home else get_hermes_home()) / "quota-governor"

    def trace_path(hermes_home=None) -> Path:
        return state_dir(hermes_home) / "obs" / "trace.jsonl"

    def parse_objective(body):  # pragma: no cover
        if not body:
            return "unattributed"
        for line in body.splitlines()[:8]:
            line = line.strip()
            if line.startswith("objective:"):
                tag = line[len("objective:"):].split("|")[0].split(",")[0].strip()
                if tag:
                    return tag
        return "unattributed"


def budgets_path(hermes_home=None) -> Path:
    """objective-budgets.json lives under quota-governor/ (never in the repo)."""
    return state_dir(hermes_home) / "objective-budgets.json"

# Schema version of objective-budgets.json.
SCHEMA_VERSION = "1.0"

# Task-event kinds that mean real WORK happened (vs board bookkeeping like
# created/spawn_failed). Used for tasks_active. Mirrors the F2 crash-loop
# kinds + completion; ``gave_up``/``timed_out``/``crashed`` are outcomes.
ACTIVE_KINDS = frozenset({"claimed", "completed"})

# Cost-bearing sources: only these can contribute to spent_usd.
COST_SOURCES = frozenset({"nanogpt-requests", "usage-audit", "model-cost-ledger"})

# F3 cost classes (budget_check.py COST_CLASS_PCT) — estimated variance basis.
# Estimated % of a provider window per class; the variance rule compares
# actual spend against this ESTIMATE (never a list price).
COST_CLASS_PCT = {
    "micro": 0.25,
    "tiny": 0.5,
    "small": 2.1,
    "medium": 4.0,
    "complex": 24.8,
}
DEFAULT_CLASS = "tiny"

# Variance threshold: actual > 3x estimate triggers human review (flag only).
VARIANCE_FACTOR = 3.0

# warn / hard-stop fractions (design §1.2 defaults; §4 decisions pending).
WARN_FRACTION = 0.7
HARD_STOP_FRACTION = 1.0

# Task-flow kinds tracked per objective (from task-events + backfill kinds).
FLOW_KINDS = ("created", "claimed", "completed", "crashed", "gave_up",
              "timed_out", "spawn_failed")

# The explicit gap bucket (design §1.3: unattributed spend is shown, never hidden).
UNATTRIBUTED = "unattributed"

# Estimated USD per provider window (shadow) for quota-billed providers —
# NOT used to fabricate budgets; kept for the estimated-variance note only.
# (Phase 0 does not price windows; the ledger's own estimates are the only
# cost basis and they arrive already priced via costUsd on the trace lines.)
EST_USD_PER_WINDOW_PCT = None  # deliberately None: no fabricated conversion

# ---------------------------------------------------------------------------
# Trace reading (read-only, fails open)
# ---------------------------------------------------------------------------

def read_trace(hermes_home=None, window_days=None) -> list:
    """Parse the trace JSONL; optional window in days; never raises.

    Corrupt lines are skipped (trace.py preserves them on disk; we do not
    need them for the rollup). Rows with ts_epoch_utc=None are always kept
    (the trace never silently drops what it cannot date; neither do we).
    """
    path = trace_path(hermes_home)
    cutoff = None
    if window_days is not None:
        try:
            cutoff = time.time() - float(window_days) * 86400.0
        except (TypeError, ValueError):
            cutoff = None
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
                ts = r.get("ts_epoch_utc")
                if cutoff is not None and ts is not None and ts < cutoff:
                    continue
                rows.append(r)
    except OSError:
        return []
    return rows


def parse_cost_class(body) -> str:
    """cost:<clase> from the first body lines (same rule as budget_check.py)."""
    if not body:
        return DEFAULT_CLASS
    for line in body.splitlines()[:8]:
        line = line.strip()
        if line.startswith("cost:"):
            cls = line[5:].strip().lower()
            return cls if cls in COST_CLASS_PCT else DEFAULT_CLASS
    return DEFAULT_CLASS


# ---------------------------------------------------------------------------
# Status ladder + variance (computed here, ACTED ON only in later phases)
# ---------------------------------------------------------------------------

def compute_status(spent, budget, tasks_total, tasks_done,
                   warn_fraction=WARN_FRACTION,
                   hard_stop_fraction=HARD_STOP_FRACTION):
    """open -> warn -> exhausted -> done (design §1.2).

    No budget set (phase 0 default) -> always "open": the ladder is armed
    but nothing can trip without a human-approved ceiling.
    """
    try:
        if budget is None or float(budget) <= 0:
            return "open"
        spent = float(spent or 0.0)
        budget = float(budget)
    except (TypeError, ValueError):
        return "open"
    if spent >= hard_stop_fraction * budget:
        return "exhausted"
    if spent >= warn_fraction * budget:
        return "warn"
    if tasks_total and tasks_done is not None and tasks_done >= tasks_total:
        return "done"
    return "open"


def variance_breach(actual_usd, est_pct, usd_per_window_pct=None):
    """True when actual spend exceeds the class estimate by > VARIANCE_FACTOR.

    ``usd_per_window_pct`` is the USD value of 1% of the assigned provider's
    window. Phase 0 NEVER fabricates that conversion (EST_USD_PER_WINDOW_PCT
    is deliberately None), so in practice this returns None (unexercisable)
    until the join improves and a measured window price exists. Armed here
    so the calibration phase reuses the exact rule (design §5.2).
    """
    if actual_usd is None or not est_pct or usd_per_window_pct is None:
        return None
    try:
        est_usd = float(est_pct) * float(usd_per_window_pct)
    except (TypeError, ValueError):
        return None
    if est_usd <= 0:
        return None
    return float(actual_usd) > VARIANCE_FACTOR * est_usd


# ---------------------------------------------------------------------------
# Rollup (pure function over the trace rows)
# ---------------------------------------------------------------------------

def rollup(rows, window_days=None, now=None) -> dict:
    """Aggregate trace rows -> rollup document. No I/O, no mutation.

    Cost-bearing lines ALL lack a task_id today, so every cent lands in
    the explicit ``unattributed_cost`` bucket and per-objective
    ``spent_usd`` stays 0.0 — the honest observer. The report shows the
    gap instead of fabricating an attribution.
    """
    now = now if now is not None else time.time()
    flow = {}            # objective -> {kind: count}
    tasks = {}           # objective -> set of task_ids
    done_tasks = {}      # objective -> set of completed task_ids
    tagged_spend = {}    # objective -> spent_usd (cost lines WITH the stamp)
    unattr_cost = 0.0
    unattr_cost_lines = 0
    unattr_cost_by_provider = {}
    tagged_events = 0
    untagged_events = 0
    cost_tagged_lines = 0  # cost lines that DO carry an objective (0 today)

    for r in rows:
        obj = r.get("objective") or UNATTRIBUTED
        source = r.get("source")
        if source == "task-events":
            bucket = flow.setdefault(obj, {k: 0 for k in FLOW_KINDS})
            kind = r.get("cause")
            if kind in bucket:
                bucket[kind] += 1
            tid = r.get("consumer_id")
            if tid:
                tasks.setdefault(obj, set()).add(tid)
                if kind == "completed":
                    done_tasks.setdefault(obj, set()).add(tid)
            if obj == UNATTRIBUTED:
                untagged_events += 1
            else:
                tagged_events += 1
        elif source in COST_SOURCES:
            cost = float(r.get("costUsd") or 0.0)
            provider = r.get("provider") or "unknown"
            if obj == UNATTRIBUTED:
                unattr_cost += cost
                unattr_cost_lines += 1
                unattr_cost_by_provider[provider] = (
                    unattr_cost_by_provider.get(provider, 0.0) + cost)
            else:
                # forward-compatible: when a source starts carrying the
                # stamp, its spend lands on the objective (dual-currency
                # rule intact: per-provider buckets, costUsd aggregates).
                cost_tagged_lines += 1
                tagged_spend[obj] = tagged_spend.get(obj, 0.0) + cost

    objectives = {}
    for obj in sorted(set(tasks) | set(flow) | set(tagged_spend)):
        f = flow.get(obj, {k: 0 for k in FLOW_KINDS})
        tids = tasks.get(obj, set())
        spent = round(tagged_spend.get(obj, 0.0), 6)
        objectives[obj] = {
            "status": "open",
            "budget_usd": None,
            "budget_quota_pct": None,
            "currency": None,
            "spent_usd": spent,
            "spent_by_provider": {},
            "flow": {k: f.get(k, 0) for k in FLOW_KINDS},
            "tasks_total": len(tids),
            "tasks_done": len(done_tasks.get(obj, set())),
            "tasks_active": len({t for t in tids
                                 if t not in done_tasks.get(obj, set())}),
            "variance_breach": None,
            "notes": [],
        }

    doc = {
        "_meta": {
            "version": SCHEMA_VERSION,
            "generated_epoch": round(float(now), 3),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(now)),
            "window_days": window_days,
            "trace_lines": len(rows),
            "mode": "observer",
            "note": ("Auto-managed by objective-budgets.py (OBJ-28 phase 0, "
                     "observer-only). spent_* come from the trace rollup; "
                     "manual edits are overwritten. Budgets stay null until "
                     "the user approves defaults (design §4)."),
            "variance_rule": ("armed but unexercisable in phase 0: cost "
                              "lines carry no task_id, so no per-task "
                              "actual exists to compare"),
        },
        "unattributed_cost": {
            "spent_usd": round(unattr_cost, 6),
            "lines": unattr_cost_lines,
            "by_provider": {p: round(v, 6) for p, v
                            in sorted(unattr_cost_by_provider.items())},
            "note": ("cost-bearing sources (nanogpt-requests, usage-audit, "
                     "model-cost-ledger) carry no task_id today; this bucket "
                     "is the visible gap, never hidden (design §1.3)."),
        },
        "unattributed_events": {
            "lines": untagged_events,
            "note": "task events whose body carries no objective: stamp.",
        },
        "cost_lines_attributed": cost_tagged_lines,
        "tagged_events": tagged_events,
        "objectives": objectives,
    }

    # Status ladder pass (phase 0: budgets are null -> all "open").
    for obj, o in doc["objectives"].items():
        o["status"] = compute_status(o["spent_usd"], o["budget_usd"],
                                     o["tasks_total"], o["tasks_done"])
    return doc


def doc_fingerprint(doc) -> str:
    """Stable fingerprint (timestamps stripped) for change detection."""
    d = json.loads(json.dumps(doc))
    d.get("_meta", {}).pop("generated_at", None)
    d.get("_meta", {}).pop("generated_epoch", None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def load_existing(hermes_home=None) -> dict:
    """Existing objective-budgets.json ({} on any error)."""
    try:
        with open(budgets_path(hermes_home), encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


# Fields a HUMAN (or a later approved phase) sets; the rollup auto-derives
# everything else. spent_* are always recomputed from the trace — the
# design's split: the machine owns the meter, the human owns the ceiling.
HUMAN_FIELDS = ("budget_usd", "budget_quota_pct", "currency",
                "warn_fraction", "hard_stop_fraction")


def preserve_human_decisions(doc, prev) -> dict:
    """Carry human-set ceilings + notes across rollups (never overwrite).

    The rollup recomputes flow/spent from the trace on every run; without
    this merge, a user-approved budget_usd would be wiped on the next tick.
    Notes are append-only history (extend / re-scope / stop decisions).
    """
    if not isinstance(prev, dict):
        return doc
    prev_objs = prev.get("objectives")
    if not isinstance(prev_objs, dict):
        return doc
    for obj, o in doc["objectives"].items():
        p = prev_objs.get(obj)
        if not isinstance(p, dict):
            continue
        for field in HUMAN_FIELDS:
            if p.get(field) is not None:
                o[field] = p[field]
        prev_notes = p.get("notes")
        if isinstance(prev_notes, list) and prev_notes:
            seen = {json.dumps(n, sort_keys=True) for n in o["notes"]}
            for n in prev_notes:
                key = json.dumps(n, sort_keys=True)
                if key not in seen:
                    o["notes"].append(n)
        # A previously computed status keeps only if it still derives:
        # recompute AFTER merging the sticky budget fields.
        o["status"] = compute_status(o["spent_usd"], o["budget_usd"],
                                     o["tasks_total"], o["tasks_done"],
                                     warn_fraction=o.get("warn_fraction",
                                                         WARN_FRACTION),
                                     hard_stop_fraction=o.get(
                                         "hard_stop_fraction",
                                         HARD_STOP_FRACTION))
    return doc


def write_budgets(doc, hermes_home=None) -> bool:
    """Atomic write of objective-budgets.json. Never raises."""
    try:
        path = budgets_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="OBJ-28 phase 0: objective budget rollup (observer-only)")
    ap.add_argument("--window-days", type=float, default=30.0,
                    help="trace window to aggregate (default: 30)")
    ap.add_argument("--json", action="store_true",
                    help="print the full rollup document as JSON")
    args = ap.parse_args(argv)

    rows = read_trace(window_days=args.window_days)
    doc = rollup(rows, window_days=args.window_days)

    prev = load_existing()
    if prev:
        doc = preserve_human_decisions(doc, prev)
    changed = doc_fingerprint(doc) != doc_fingerprint(prev) if prev else True
    ok = write_budgets(doc)
    if not ok:
        print("obj-budgets: ERROR writing "
              f"{budgets_path()} — revisar permisos")
        return 0  # fails open into the caller, but stdout shows the wound

    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0

    # Watchdog pattern: silence when nothing changed, one evidence line when
    # the rollup moved (the cron wrapper passes stdout through).
    if changed:
        objs = doc["objectives"]
        print(f"obj-budgets: objectives={len(objs)} "
              f"tagged_events={doc['tagged_events']} "
              f"unattributed_cost=${doc['unattributed_cost']['spent_usd']:.4f} "
              f"unattributed_events={doc['unattributed_events']['lines']} "
              f"window={args.window_days:g}d -> {budgets_path().name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
