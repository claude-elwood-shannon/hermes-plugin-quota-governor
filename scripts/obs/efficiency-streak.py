#!/usr/bin/python3.12
"""OBJ-METRICS gauge: consecutive no-CRITICO days from efficiency-ratio.log.

The OBJ-METRICS success criterion is "efficiency-ratio with a verdict
other than CRITICO for 3 consecutive days". Nothing measured it: the
shared log ~/.hermes/logs/efficiency-ratio.log carries one
efficiency_ratio record per hour (JSON lines interleaved with
`[date time] efficiency-ratio: tick` liveness lines) and no tool
reduced it to a per-day verdict plus a consecutive-day streak. This
script is that gauge: zero tokens, stdlib only, read-only inputs.

Usage:
  python3 scripts/obs/efficiency-streak.py [--log PATH] [--json]

Behavior:
  - Only JSON lines are parsed; anything else (liveness ticks, deploy
    notes) is ignored. Records apply in file order, so a UTC day's
    verdict is its LAST record -- matching how the ratio recovers
    intra-day (a CRITICO morning followed by an OK close is a
    no-CRITICO day).
  - The streak counts consecutive calendar days ending at the most
    recent day present in the log; a day missing from the log or a
    CRITICO verdict ends it. A missing or empty log yields a zeroed
    report (fail-open, like every observability script here).

Env overrides: none (pass --log).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

CRITICO = "CRITICO"
DEFAULT_LOG = Path.home() / ".hermes" / "logs" / "efficiency-ratio.log"


def parse_log(path: Path) -> dict[str, str]:
    """Map UTC day (ts[:10]) -> verdict of the LAST efficiency_ratio record.

    Non-JSON lines are skipped, as are JSON records without a usable ts,
    a string veredicto, or the efficiency_ratio kind. File order decides
    which record is "last", not re-sorted timestamps.
    """
    days: dict[str, str] = {}
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return days
    with fh:
        for ln in fh:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "efficiency_ratio":
                continue
            ts = rec.get("ts")
            veredicto = rec.get("veredicto")
            if isinstance(ts, str) and len(ts) >= 10 and isinstance(veredicto, str):
                days[ts[:10]] = veredicto
    return days


def prev_day(day: str) -> str:
    """UTC calendar day before `day` (YYYY-MM-DD), as YYYY-MM-DD."""
    y, m, d = (int(part) for part in day.split("-"))
    return (date(y, m, d) - timedelta(days=1)).isoformat()


def compute_streak(days: dict[str, str]) -> int:
    """Consecutive no-CRITICO days ending at the latest logged day.

    Walks back one calendar day at a time: a day missing from the log
    or a CRITICO verdict ends the streak. An empty map or a CRITICO
    latest day yields 0.
    """
    day = max(days) if days else None
    streak = 0
    while day is not None and days.get(day) not in (None, CRITICO):
        streak += 1
        day = prev_day(day)
    return streak


def build_report(days: dict[str, str]) -> dict:
    """Assemble the OBJ-METRICS gauge report (see module docstring)."""
    latest_day = max(days) if days else None
    streak = compute_streak(days)
    return {
        "days": {d: days[d] for d in sorted(days)[-7:]},
        "streak_days": streak,
        "meets_3_day_criterion": streak >= 3,
        "latest_day": latest_day,
        "latest_veredicto": days.get(latest_day) if latest_day else None,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the report (JSON or human summary) and exit 0."""
    ap = argparse.ArgumentParser(
        description="OBJ-METRICS gauge: consecutive no-CRITICO days "
                    "from the efficiency-ratio log.")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG,
                    help=f"efficiency-ratio log path (default: {DEFAULT_LOG})")
    ap.add_argument("--json", action="store_true",
                    help="print the machine-readable JSON report")
    args = ap.parse_args(argv)
    report = build_report(parse_log(args.log))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"latest_day: {report['latest_day']}")
        print(f"latest_veredicto: {report['latest_veredicto']}")
        print(f"streak_days: {report['streak_days']}")
        print(f"meets_3_day_criterion: {report['meets_3_day_criterion']}")
        print("last 7 days:")
        for day, veredicto in report["days"].items():
            print(f"  {day} {veredicto}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
