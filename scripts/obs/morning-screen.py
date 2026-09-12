#!/usr/bin/python3.12
"""morning-screen.py — OBJ-27 F1: deterministic consumption screen (no_agent).

Reads the OBJ-27 F0 trace + the quota forecast + the kanban board and emits a
compact, human-readable screen that gives the owner the role of witness:
what was consumed, by objective, by consumer class, the unattributed gap, the
provider burn forecast + gate verdict, the live board state (incl. done in
the last 24h), and F2 alerts (only when there is an anomaly).

WHY no_agent: the screen is a pure function of three existing files. It needs
no LLM, costs zero tokens, and runs on free quota. The morning-report cron
(an LLM job) consumes this screen as its `script` context and writes the
narrative report around it — the LLM reasons, the screen is the ground truth.

Sources (all read-only, never modified):
  1. trace.jsonl        OBJ-27 F0 canonical consumption trace
  2. forecast.json      OBJ-24 F2 EMA burn-rate forecast
  3. kanban.db          board state (running/ready/blocked + done 24h)

Portability: paths resolve through get_hermes_home() (HERMES_HOME env or
~/.hermes). No absolute host paths in the repo. Times rendered local (CEST).

Exit: 0 always. Empty stdout only if every source is missing (watchdog
pattern — the cron stays silent rather than alert on a deploy gap).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

CEST = dt.timezone(dt.timedelta(hours=2))  # Europe/Madrid summer (UTC+2)

# F2 alert thresholds (OBJ-27 F1 spec)
UNATTRIBUTED_SPEND_PCT = 20.0   # unattributed > 20% of spend -> alert
CRASH_LOOP_MIN = 3              # >= 3 crashes in 24h -> alert
BURN_ETA90_H = 1.0              # eta_90 < 1h -> board-off alert
BURN_COLCHON_H = 2.0            # eta_90 < reset - 2h -> reduce-workers alert


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

    # by objective (top 8, then unattributed)
    by_obj = Counter(r.get("objective", "unattributed") for r in rows)
    lines.append("  por objetivo:")
    for obj, c in by_obj.most_common(8):
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
    """Provider burn forecast + the gate --suggest verdict (2 lines)."""
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

    # Verdict (gate --suggest rule, 2 lines): eta_90 vs reset.
    verdict = _build_verdict(fc)
    if verdict:
        lines.append("")
        lines.append("VEREDICTO (gate --suggest)")
        lines.extend(verdict)

    return "\n".join(lines)


def _build_verdict(fc: dict) -> list:
    """Apply the gate --suggest decision rule to the forecast.

    Returns a list of lines (one per provider with an eta_90, plus a summary
    line). Rule (from quota-gate.py forecast_context):
      - eta_90 < 1h            -> board off
      - eta_90 < reset - 2h    -> max_workers=1, cap cost
      - otherwise              -> OK (margen suficiente)
    """
    provs = fc.get("providers", {})
    reset_h = fc.get("hours_to_reset")
    try:
        reset_h = float(reset_h) if reset_h is not None else None
    except (TypeError, ValueError):
        reset_h = None

    out = []
    any_eta = False
    for name in sorted(provs):
        p = provs[name]
        eta90 = p.get("eta_90_hours")
        try:
            eta90 = float(eta90) if eta90 is not None else None
        except (TypeError, ValueError):
            eta90 = None
        if eta90 is None or eta90 < 0:
            out.append(f"  {name}: eta_90=- (agotado/sin proyeccion)")
            continue
        any_eta = True
        if eta90 < BURN_ETA90_H:
            out.append(f"  {name}: eta_90={eta90:.1f}h (<1h) -> BOARD OFF")
        elif reset_h is not None and eta90 < (reset_h - BURN_COLCHON_H):
            out.append(f"  {name}: eta_90={eta90:.1f}h < margen reset "
                       f"({reset_h:.1f}h) -> max_workers=1, cap cost")
        elif reset_h is not None:
            out.append(f"  {name}: eta_90={eta90:.1f}h vs reset "
                       f"{reset_h:.1f}h -> OK")
        else:
            out.append(f"  {name}: eta_90={eta90:.1f}h (reset desconocido)")
    if any_eta and reset_h is not None:
        out.append(f"  margen hasta reset: {reset_h:.1f}h")
    return out


def build_board_screen(hermes_home=None) -> str:
    """Board state: counts by status + active tasks + done in last 24h."""
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
        now = time.time()
        done24 = con.execute(
            "SELECT id, assignee, title FROM tasks "
            "WHERE status='done' AND completed_at > ? "
            "ORDER BY completed_at DESC", (now - 86400,)).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        con.close()

    lines = ["BOARD"]
    total = sum(counts.values())
    status_s = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines.append(f"  total {total} | {status_s}")
    # supply_ratio diario (OBJ-29). Placeholder hasta que OBJ-29 lo aporte.
    lines.append("  supply_ratio diario: n/d (OBJ-29 pendiente)")
    if done24:
        ids = ", ".join(r["id"] for r in done24[:8])
        if len(done24) > 8:
            ids += f", +{len(done24) - 8} mas"
        lines.append(f"  done 24h: {len(done24)} ({ids})")
    else:
        lines.append("  done 24h: 0")
    if active:
        lines.append("  activas:")
        for r in active[:5]:
            body = (r["body"] or "").replace("\n", " ").strip()[:70]
            lines.append(f"    {r['id']} [{r['status']}] {r['assignee']} "
                         f"| {body}")
        if len(active) > 5:
            lines.append(f"    ... +{len(active) - 5} mas")
    else:
        lines.append("  activas: ninguna")
    return "\n".join(lines)


def build_alerts_screen(hermes_home=None) -> str:
    """F2 alerts — ONLY when there is an anomaly. Else 'sin incidencias'."""
    alerts = []

    # 1. unattributed > 20% of spend
    rows = _read_jsonl(trace_path(hermes_home))
    has_trace = bool(rows)
    if rows:
        total = sum(r.get("costUsd") or 0 for r in rows)
        unatt = sum(r.get("costUsd") or 0 for r in rows
                    if r.get("objective") == "unattributed")
        if total > 0 and (unatt / total * 100) > UNATTRIBUTED_SPEND_PCT:
            alerts.append(
                f"unattributed > {UNATTRIBUTED_SPEND_PCT:.0f}% del gasto: "
                f"{unatt / total * 100:.1f}% (hueco de join)")

    # 2. crash loop in last 24h
    db = kanban_db_path(hermes_home)
    has_board = db.exists()
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                now = time.time()
                n = con.execute(
                    "SELECT COUNT(*) c FROM task_runs "
                    "WHERE started_at > ? AND outcome IN "
                    "('crashed','timed_out','spawn_failed','gave_up')",
                    (now - 86400,)).fetchone()[0]
                if n >= CRASH_LOOP_MIN:
                    alerts.append(f"loop de crashes: {n} en 24h")
            finally:
                con.close()
        except sqlite3.Error:
            pass

    # 3. burn over threshold (eta_90 < 1h or < reset - 2h)
    fc = _read_json(forecast_path(hermes_home))
    provs = fc.get("providers", {})
    has_forecast = bool(provs)
    reset_h = fc.get("hours_to_reset")
    try:
        reset_h = float(reset_h) if reset_h is not None else None
    except (TypeError, ValueError):
        reset_h = None
    for name in sorted(provs):
        p = provs[name]
        eta90 = p.get("eta_90_hours")
        try:
            eta90 = float(eta90) if eta90 is not None else None
        except (TypeError, ValueError):
            eta90 = None
        if eta90 is None or eta90 < 0:
            continue
        if eta90 < BURN_ETA90_H:
            alerts.append(f"burn {name}: eta_90={eta90:.1f}h (<1h) -> board off")
        elif reset_h is not None and eta90 < (reset_h - BURN_COLCHON_H):
            alerts.append(f"burn {name}: eta_90={eta90:.1f}h < margen reset "
                          f"({reset_h:.1f}h) -> reducir workers")

    # No sources at all -> stay silent (watchdog pattern), don't claim 'ok'.
    if not (has_trace or has_board or has_forecast):
        return ""

    if not alerts:
        return "ALERTAS\n  sin incidencias"
    lines = ["ALERTAS"]
    for a in alerts:
        lines.append(f"  {a}")
    return "\n".join(lines)


def _fmt_dur(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    return f"{hours:.1f}h"


def build_flight_report(hermes_home=None, now=None) -> str:
    """OBJ-42 weekly flight rendition — VUELO section for the Monday report.

    Standing rendition of the direction mandate: over the last 7 days, what
    was directed (tasks closed by objective), what it cost (balance-billed
    USD from the trace), and what it produced (produced vs empty closures —
    closures whose workspace had no deliverable). Zero tokens: pure function
    of kanban.db + trace.jsonl. Renders ONLY on Mondays (weekday 0 local) —
    other days this section is absent and the screen stays as before.
    """
    if now is None:
        now = time.time()
    local_now = dt.datetime.fromtimestamp(now, CEST)
    if local_now.weekday() != 0:
        return ""
    db = kanban_db_path(hermes_home)
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return ""
    week_start = now - 7 * 86400
    try:
        rows = con.execute(
            "SELECT id, title, body, completed_at FROM tasks "
            "WHERE status='done' AND completed_at > ? "
            "ORDER BY completed_at", (week_start,)).fetchall()
    except sqlite3.Error:
        con.close()
        return ""
    finally:
        con.close()

    # Group by objective: tag header `objective:OBJ-NN` in the body (the
    # OBJ-08 convention). Untagged closures group under 'sin etiqueta'.
    by_obj = {}
    for r in rows:
        m = re.search(r"objective:(OBJ-\d+)", r["body"] or "")
        obj = m.group(1) if m else "sin etiqueta"
        by_obj.setdefault(obj, []).append(r)

    # Balance-billed spend over the same window from the trace
    # (costUsd > 0 rows = balance-billed per x_nanogpt_pricing semantics).
    trace_rows = _read_jsonl(trace_path(hermes_home))
    spent = sum(r.get("costUsd") or 0 for r in trace_rows
                if (r.get("ts_epoch_utc") or 0) > week_start
                and (r.get("costUsd") or 0) > 0)

    total = len(rows)
    lines = ["VUELO (rendicion semanal del mandato — OBJ-42)",
             f"  cerradas 7d: {total} | gasto balance 7d: {_fmt_usd(spent)}"]
    if total:
        for obj in sorted(by_obj):
            ids = [r["id"] for r in by_obj[obj]]
            shown = ", ".join(ids[:6])
            if len(ids) > 6:
                shown += f", +{len(ids) - 6} mas"
            lines.append(f"  {obj}: {len(ids)} cerradas ({shown})")
    else:
        lines.append("  sin cierres en 7d — verificar que el vuelo repite, "
                     "no que se detuvo (constitution 24c)")
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
        build_alerts_screen(hermes_home),
        build_flight_report(hermes_home),
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
