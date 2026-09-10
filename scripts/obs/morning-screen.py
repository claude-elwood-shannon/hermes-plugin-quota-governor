#!/usr/bin/python3.12
"""morning-screen.py — OBJ-27 F1: deterministic consumption screen (no_agent).

Reads the OBJ-27 F0 trace + the quota forecast + the kanban board and emits a
compact, human-readable screen that gives the owner the role of witness:
what was consumed, by objective, by consumer class, the unattributed gap, the
provider burn forecast, and the live board state.

WHY no_agent: the screen is a pure function of three existing files. It needs
no LLM, costs zero tokens, and runs on free quota. The morning-report cron
(an LLM job) consumes this screen as its `script` context and writes the
narrative report around it — the LLM reasons, the screen is the ground truth.

Sources (all read-only, never modified):
  1. trace.jsonl        OBJ-27 F0 canonical consumption trace
  2. forecast.json      OBJ-24 F2 EMA burn-rate forecast
  3. kanban.db          board state (running/ready/blocked + active tasks)

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times rendered local (CEST).

Exit: 0 always. Empty stdout only if every source is missing (watchdog
pattern — the cron stays silent rather than alert on a deploy gap).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

CEST = dt.timezone(dt.timedelta(hours=2))  # Europe/Madrid summer (UTC+2)


def get_hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def state_dir(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor"


def trace_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "obs" / "trace.jsonl"


def forecast_path(hermes_home=None) -> Path:
    return state_dir(hermes_home) / "forecast.json"


# The shared board lives at the ROOT ~/.hermes/kanban.db, not under a profile
# home. Same convention as trace.py's _source_homes: scan root + profile homes
# and return the first that exists. Overridable via QUOTA_GOVERNOR_PROFILE_HOMES
# (os.pathsep) for tests / non-standard hosts.
_DEFAULT_PROFILE_HOMES = (
    Path.home() / ".hermes",
) + tuple(
    Path.home() / ".hermes" / "profiles" / name
    for name in ("pr-ollama", "pr-nanogpt", "pr-opencode", "pr-openrouter",
                 "pr-vllm")
)


def _candidate_homes(hermes_home=None) -> list:
    raw = os.environ.get("QUOTA_GOVERNOR_PROFILE_HOMES", "")
    if raw:
        homes = [Path(p) for p in (s.strip() for s in raw.split(os.pathsep))
                 if p]
        if homes:
            return homes
    if hermes_home:
        # Explicit home (tests / callers): respect it only, hermetic.
        return [Path(hermes_home)]
    return list(_DEFAULT_PROFILE_HOMES)


def kanban_db_path(hermes_home=None) -> Path:
    for home in _candidate_homes(hermes_home):
        p = home / "kanban.db"
        if p.exists():
            return p
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "kanban.db"


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


def _fmt_ts(epoch) -> str:
    if not epoch:
        return "-"
    try:
        return dt.datetime.fromtimestamp(float(epoch), CEST).strftime(
            "%d-%b %H:%M")
    except (ValueError, OSError, OverflowError):
        return "-"


def _fmt_usd(v) -> str:
    if v is None:
        return "-"
    return f"${v:,.4f}"


# ---------------------------------------------------------------------------
# Screen builders
# ---------------------------------------------------------------------------

def build_trace_screen(hermes_home=None) -> str:
    """Aggregate the trace: totals, by objective, by class, unattributed gap."""
    rows = _read_jsonl(trace_path(hermes_home))
    if not rows:
        return ""

    lines = []
    n = len(rows)
    with_cost = [r for r in rows if r.get("costUsd")]
    total_usd = sum(r["costUsd"] for r in with_cost)
    ts = [r["ts_epoch_utc"] for r in rows if r.get("ts_epoch_utc")]
    span = ""
    if ts:
        span = f" ({_fmt_ts(min(ts))} -> {_fmt_ts(max(ts))})"

    lines.append(f"CONSUMO (trace, {n} lineas{span})")
    lines.append(f"  coste real acumulado: {_fmt_usd(total_usd)} "
                 f"({len(with_cost)} lineas con costUsd)")

    # by objective (top 12, then unattributed)
    by_obj = Counter(r.get("objective", "unattributed") for r in rows)
    lines.append("  por objetivo:")
    for obj, c in by_obj.most_common(12):
        usd = sum(r.get("costUsd") or 0 for r in rows
                  if r.get("objective") == obj)
        mark = "  <-- SIN ETIQUETA (hueco)" if obj == "unattributed" else ""
        lines.append(f"    {obj:<14} {c:>5} lineas  {_fmt_usd(usd)}{mark}")

    # by consumer class
    by_cls = Counter(r.get("consumer_class", "unattributed") for r in rows)
    cls_str = ", ".join(f"{k}={v}" for k, v in sorted(by_cls.items()))
    lines.append(f"  por clase: {cls_str}")

    # by source
    by_src = Counter(r.get("source", "?") for r in rows)
    src_str = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
    lines.append(f"  por fuente: {src_str}")

    return "\n".join(lines)


def build_forecast_screen(hermes_home=None) -> str:
    """Provider burn forecast: pct_now, eta to 90% (governor stop)."""
    fc = _read_json(forecast_path(hermes_home))
    provs = fc.get("providers", {})
    if not provs:
        return ""

    lines = ["FORECAST (EMA burn, ventana semanal)"]
    for name in sorted(provs):
        p = provs[name]
        pct = p.get("pct_now")
        eta90 = p.get("eta_90_hours")
        eta100 = p.get("eta_100_hours")
        conf = p.get("confidence", 0)
        pct_s = f"{pct:.1f}%" if pct is not None else "-"
        eta90_s = f"{eta90:.1f}h" if eta90 is not None else "-"
        eta100_s = f"{eta100:.1f}h" if eta100 is not None else "-"
        lines.append(f"  {name:<12} {pct_s:>7}  ETA90 {eta90_s:>7}  "
                     f"ETA100 {eta100_s:>7}  conf {conf}")
    reset = fc.get("next_weekly_reset_iso", "")
    if reset:
        lines.append(f"  reset semanal: {reset}")
    return "\n".join(lines)


def build_board_screen(hermes_home=None) -> str:
    """Board state: counts by status + active tasks (running/ready/blocked)."""
    db = kanban_db_path(hermes_home)
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return ""
    try:
        counts = {}
        for r in con.execute(
                "SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
            counts[r["status"]] = r["c"]
        active = con.execute(
            "SELECT id, status, assignee, body FROM tasks "
            "WHERE status IN ('ready','running','blocked') "
            "ORDER BY status, id").fetchall()
    except sqlite3.Error:
        return ""
    finally:
        con.close()

    lines = ["BOARD"]
    total = sum(counts.values())
    status_s = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines.append(f"  total {total} | {status_s}")
    if active:
        lines.append("  activas:")
        for r in active:
            body = (r["body"] or "").replace("\n", " ").strip()[:70]
            lines.append(f"    {r['id']} [{r['status']}] {r['assignee']} "
                         f"| {body}")
    else:
        lines.append("  activas: ninguna")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_screen(hermes_home=None) -> str:
    """Compose the full screen. Empty if every source is missing."""
    parts = [
        build_trace_screen(hermes_home),
        build_forecast_screen(hermes_home),
        build_board_screen(hermes_home),
    ]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "\n\n".join(parts)


def main(argv=None) -> int:
    screen = build_screen()
    if screen:
        print(screen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
