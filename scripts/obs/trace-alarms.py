#!/usr/bin/python3.12
"""trace-alarms.py — OBJ-27 F2: consumption alarms over the trace (no_agent).

Watchdog over the OBJ-27 F0 canonical trace and the OBJ-24 F2 forecast.
The watchdog pattern is the contract: SILENCE = NO INCIDENTS. stdout is
empty when every check is healthy (or every source is missing); it emits
one "ALARM ..." line per real anomaly, which the cron layer delivers.

Alarms (OBJ-27 F2 spec):
  1. crash loop   — same task_id with >= CRASH_LOOP_MIN_CLAIMS 'claimed'
                    events and ZERO 'completed' events inside the window
                    (trace task-events; a loop that eventually completed is
                    a retry, not a loop).
  2. burn         — forecast eta_90 < BURN_ETA90_H (board off), or
                    eta_90 < hours_to_reset - BURN_COLCHON_H (reduce
                    workers). Same rule as the F1 screen's gate verdict.
  3. unattributed — spend with objective=unattributed > UNATTRIBUTED
                    _SPEND_PCT % of total trace costUsd (the join gap,
                    shown, never hidden — OBJ-28 design §5.1).

SOURCES (read-only, never modified; fail open — corrupt/missing stays
silent rather than alerting on a deploy gap):
  trace.jsonl     quota-governor/obs/trace.jsonl  (F0)
  forecast.json   quota-governor/forecast.json    (OBJ-24 F2 EMA burn)

This module is SELF-CONTAINED on purpose: scripts/obs/trace.py is being
extended by sibling tasks (F3 retention); the alarm needs only the
canonical path conventions, re-implemented locally exactly like
morning-screen.py (F1 precedent). Stdlib only, no network, zero tokens.

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times are epoch UTC;
windows compare epoch, never local clocks.

Exit: 0 always. Empty stdout = healthy or no sources (the cron stays
silent instead of alerting).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Thresholds (kept identical to the F1 screen's alert thresholds so the
# human report and the watchdog never disagree about what an anomaly is.)
# ---------------------------------------------------------------------------

UNATTRIBUTED_SPEND_PCT = 20.0   # unattributed > 20% of spend -> alarm
CRASH_LOOP_MIN_CLAIMS = 3       # >= 3 claimed without completed -> alarm
CRASH_WINDOW_S = 86400.0        # ... inside the last 24h
BURN_ETA90_H = 1.0              # eta_90 < 1h -> board off
BURN_COLCHON_H = 2.0            # eta_90 < reset - 2h -> reduce workers


def get_hermes_home() -> Path:
    """Active HERMES_HOME, else ~/.hermes. Never absolute in the repo."""
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def state_dir(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor"


def trace_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "obs" / "trace.jsonl"


def forecast_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "forecast.json"


# ---------------------------------------------------------------------------
# Readers (fail open: missing/corrupt source -> empty, never raise)
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Check 1: crash loop over the trace (task-events claimed/completed)
# ---------------------------------------------------------------------------

def check_crash_loop(rows: list, now=None) -> list:
    """Same task_id with >= MIN claims and zero completions in the window.

    Only task-events rows count. Events with a missing/unparseable ts are
    EXCLUDED (counting them as recent would fabricate a loop). The alarm
    line carries the task id and its claim count — the evidence.
    """
    now = time.time() if now is None else float(now)
    lo = now - CRASH_WINDOW_S
    per_task = {}
    for r in rows:
        if r.get("source") != "task-events":
            continue
        cause = r.get("cause")
        if cause not in ("claimed", "completed"):
            continue
        ts = r.get("ts_epoch_utc")
        if not isinstance(ts, (int, float)) or not (lo <= ts <= now):
            continue
        tid = r.get("consumer_id")
        if not tid:
            continue
        c = per_task.setdefault(tid, {"claimed": 0, "completed": 0})
        c[cause] += 1
    return [
        f"ALARM crash-loop: {tid} {c['claimed']} claimed sin completed "
        f"en 24h"
        for tid, c in sorted(per_task.items())
        if c["claimed"] >= CRASH_LOOP_MIN_CLAIMS and c["completed"] == 0
    ]


# ---------------------------------------------------------------------------
# Check 2: burn over threshold (forecast eta_90 vs reset margin)
# ---------------------------------------------------------------------------

def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_burn(forecast: dict) -> list:
    """eta_90 < 1h -> board off; eta_90 < reset-2h -> reduce workers.

    Providers without a usable eta_90 (None/negative = exhausted or no
    projection) are skipped — an already-exhausted window is not a new
    anomaly every tick. Without hours_to_reset only the <1h rule applies.
    """
    provs = forecast.get("providers", {})
    reset_h = _as_float(forecast.get("hours_to_reset"))
    out = []
    for name in sorted(provs):
        p = provs.get(name) or {}
        eta90 = _as_float(p.get("eta_90_hours"))
        if eta90 is None or eta90 < 0:
            continue
        if eta90 < BURN_ETA90_H:
            out.append(f"ALARM burn {name}: eta_90={eta90:.1f}h (<1h) "
                       f"-> board off")
        elif reset_h is not None and eta90 < (reset_h - BURN_COLCHON_H):
            out.append(f"ALARM burn {name}: eta_90={eta90:.1f}h < margen "
                       f"reset ({reset_h:.1f}h) -> reducir workers")
    return out


# ---------------------------------------------------------------------------
# Check 3: unattributed share of trace spend
# ---------------------------------------------------------------------------

def check_unattributed(rows: list) -> list:
    """objective=unattributed costUsd > PCT% of total -> one alarm line."""
    total = sum(r.get("costUsd") or 0 for r in rows)
    if total <= 0:
        return []
    unatt = sum(r.get("costUsd") or 0 for r in rows
                if r.get("objective") == "unattributed")
    pct = unatt / total * 100.0
    if pct > UNATTRIBUTED_SPEND_PCT:
        return [f"ALARM unattributed: {pct:.1f}% del gasto sin objetivo "
                f"(> {UNATTRIBUTED_SPEND_PCT:.0f}%) — "
                f"${unatt:.4f} de ${total:.4f}"]
    return []


# ---------------------------------------------------------------------------
# Orchestrator (pure over disk state -> list of alarm lines)
# ---------------------------------------------------------------------------

def run_checks(hermes_home=None, now=None) -> list:
    """Run every check. Returns alarm lines; empty = silence."""
    now = time.time() if now is None else float(now)
    rows = _read_jsonl(trace_path(hermes_home))
    forecast = _read_json(forecast_path(hermes_home))
    alerts = []
    if rows:
        alerts.extend(check_crash_loop(rows, now))
        alerts.extend(check_unattributed(rows))
    if forecast:
        alerts.extend(check_burn(forecast))
    return alerts


def main(argv=None) -> int:
    for line in run_checks():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
