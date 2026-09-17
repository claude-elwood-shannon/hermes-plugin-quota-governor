#!/usr/bin/python3.12
"""objective-lifecycle.py — Governance P2: hourly objective lifecycle cron.

Question answered hourly (zero tokens, stdlib only):

    "Are the non-static objectives actually producing, and is their
    priority (nice) and budget (budget_daily) drifting toward where
    the work is?"

Sources (read-only): kanban.db approved_objectives (P1 schema), the OBJ-27
F0 consumption trace (obs/trace.jsonl), and the board's tasks table. All
paths resolve with profile fallbacks like efficiency-ratio.py and are
env-overridable (OL_KANBAN_DB, OL_TRACE, OL_LOG, OL_ALARMS, OL_HERMES_ROOT).

What it does, in order (mirrors task t_16ef4d71 / P2 body):

  1. FOCUS RESET — expired focus_until rows get nice=0, focus_until=NULL.
     Rows with NULL focus_until never match (SQL NULL comparison), so the
     ratchet's own nice values are untouched by this step.

  2. PER-OBJECTIVE EVALUATION (status='active' AND governance != 'static'):
       a. preset resolution — preset_id column; 'auto' picks by efficiency
          (no data -> protected, low -> conservative, high -> aggressive,
          else normal). P1 shipped the 5 system preset rows with NULL
          parameter columns, so BUILT-IN defaults below are the documented
          base; any non-NULL DB column overrides its default. A preset with
          is_active=0 or an unknown preset_id falls back to 'normal'.
       b. efficiency 24h — task-events flow from the trace for the
          objective's tag: verified = completed, attempted = completed +
          crashed + gave_up + timed_out + spawn_failed. Cost lines are NOT
          used: today every cost line is objective="unattributed" (known
          gap — objective-budgets.py documents it), so a cost-based
          efficiency would be fabricated. No task-events -> efficiency
          None -> SIN DATOS -> no adjustment, ever.
       c. ratchet — state and streaks persist in the lifecycle JSONL itself
          (last entry per objective; no extra state file). An efficiency
          signal only ADJUSTS after the streak reaches the preset's
          consecutive_high/low threshold; the first tick merely records the
          state. Direction (Unix nice semantics — higher nice = lower
          dispatch priority):
             sustained LOW  efficiency -> nice -= step (prioritize recovery)
                                           budget_pct -= step (cut waste)
             sustained HIGH efficiency -> nice += step (demote, it produces)
                                           budget_pct += step (reward)
          nice applies when governance in (responsive, dynamic); budget when
          governance in (elastic, dynamic). Values clamp to the preset caps
          and budget_daily is recomputed as
          budget_baseline * (1 + budget_adjustment_pct/100) (baseline 0
          disables budget moves). A cooldown since the LAST actual
          adjustment (preset cooldown_min) throttles repeats.
       d. diagnostics — live board queues (running/ready/blocked) for the
          objective tag plus the window's failure-cause split, with a
          deterministic dominant-failure note.
       e. notification — state TRANSITIONS into sustained-low (and cap
          saturation when an adjustment is clamped) append one line to
          cron-alarms.jsonl, the same channel cron-health-check.sh already
          feeds and the morning screen consumes.
       f. log — ONE JSONL line per evaluated objective to
          ~/.hermes/logs/objective-lifecycle.jsonl (kind="objective_lifecycle").

Governance matrix: static = never evaluated (only the focus reset applies);
responsive = nice only; elastic = budget only; dynamic = both; unknown
values are treated as static (fail-closed for an unknown enum).

Safety: DIRECCION-STOP irrelevant (writes are bounded DB scalars, no spawn).
Fail-open per objective — one objective's error never aborts the others.
Writes touch ONLY nice, budget_daily, budget_adjustment_pct, updated_at,
updated_by on approved_objectives; the UPDATE is skipped when nothing
changed. governance/preset_id/focus decisions stay human (bridge P3).

Usage:
  objective-lifecycle.py              # run + append log (silent-ish stdout)
  objective-lifecycle.py --dry-run    # compute + print entries, ZERO writes
  objective-lifecycle.py --verbose    # per-objective summary on stdout

Every tick prints one liveness line "[ts] objective-lifecycle: tick" (the
cron wrapper also writes one — watchdog pattern; cron-health-check.sh
monitors the log with a 90 min window).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (profile fallbacks, like efficiency-ratio.py; env-overridable)
# ---------------------------------------------------------------------------

_HERMES_ROOT = Path(os.environ.get(
    "OL_HERMES_ROOT", os.path.expanduser("~/.hermes")))
PROFILE_HOME = _HERMES_ROOT / "profiles" / "pr-ollama"


def kanban_db_path() -> Path:
    env = os.environ.get("OL_KANBAN_DB", "").strip()
    if env:
        return Path(env)
    root_db = _HERMES_ROOT / "kanban.db"          # the shared board (root)
    return root_db if root_db.exists() else PROFILE_HOME / "kanban.db"


def trace_path() -> Path:
    env = os.environ.get("OL_TRACE", "").strip()
    if env:
        return Path(env)
    p = PROFILE_HOME / "quota-governor" / "obs" / "trace.jsonl"
    return p if p.exists() else \
        _HERMES_ROOT / "quota-governor" / "obs" / "trace.jsonl"


def lifecycle_log_path() -> Path:
    env = os.environ.get("OL_LOG", "").strip()
    if env:
        return Path(env)
    return _HERMES_ROOT / "logs" / "objective-lifecycle.jsonl"


def alarms_path() -> Path:
    env = os.environ.get("OL_ALARMS", "").strip()
    if env:
        return Path(env)
    return _HERMES_ROOT / "logs" / "cron-alarms.jsonl"


WINDOW_S = 24 * 3600            # evaluation window: 24h
STALE_PRIOR_S = 3 * 3600        # prior entry older than this breaks streaks

ATTEMPTED_CAUSES = ("completed", "crashed", "gave_up",
                    "timed_out", "spawn_failed")
FAILURE_CAUSES = ("crashed", "gave_up", "timed_out", "spawn_failed")

GOV_NICE = frozenset({"responsive", "dynamic"})
GOV_BUDGET = frozenset({"elastic", "dynamic"})

# 'auto' preset selection (fixed mapping; documented, never random).
AUTO_LOW_EFF = 0.35
AUTO_HIGH_EFF = 0.70

# ---------------------------------------------------------------------------
# Built-in preset base (P1 shipped the 5 system rows with NULL parameters —
# these defaults ARE the documented base; non-NULL DB columns override).
# ---------------------------------------------------------------------------
BUILTIN_PRESETS: dict[str, dict] = {
    "normal": {
        "nice_step": 1, "nice_cap_high": 5, "nice_cap_low": -5,
        "budget_step_pct": 10, "budget_cap_high_pct": 30,
        "budget_cap_low_pct": -30,
        "eval_frequency_min": 60, "cooldown_min": 240,
        "trigger_eff_high": 0.7, "trigger_eff_low": 0.3,
        "consecutive_high": 2, "consecutive_low": 2,
    },
    "aggressive": {
        "nice_step": 2, "nice_cap_high": 10, "nice_cap_low": -10,
        "budget_step_pct": 20, "budget_cap_high_pct": 50,
        "budget_cap_low_pct": -50,
        "eval_frequency_min": 30, "cooldown_min": 120,
        "trigger_eff_high": 0.6, "trigger_eff_low": 0.4,
        "consecutive_high": 2, "consecutive_low": 2,
    },
    "conservative": {
        "nice_step": 1, "nice_cap_high": 3, "nice_cap_low": -3,
        "budget_step_pct": 5, "budget_cap_high_pct": 15,
        "budget_cap_low_pct": -15,
        "eval_frequency_min": 120, "cooldown_min": 480,
        "trigger_eff_high": 0.8, "trigger_eff_low": 0.2,
        "consecutive_high": 3, "consecutive_low": 3,
    },
    "startup": {
        "nice_step": 2, "nice_cap_high": 10, "nice_cap_low": -10,
        "budget_step_pct": 25, "budget_cap_high_pct": 60,
        "budget_cap_low_pct": -60,
        "eval_frequency_min": 30, "cooldown_min": 60,
        "trigger_eff_high": 0.5, "trigger_eff_low": 0.3,
        "consecutive_high": 1, "consecutive_low": 1,
    },
    "protected": {
        "nice_step": 0, "nice_cap_high": 0, "nice_cap_low": 0,
        "budget_step_pct": 0, "budget_cap_high_pct": 0,
        "budget_cap_low_pct": 0,
        "eval_frequency_min": 1440, "cooldown_min": 0,
        "trigger_eff_high": 1.0, "trigger_eff_low": 0.0,
        "consecutive_high": 99, "consecutive_low": 99,
    },
}

PRESET_PARAM_KEYS = (
    "nice_step", "nice_cap_high", "nice_cap_low",
    "budget_step_pct", "budget_cap_high_pct", "budget_cap_low_pct",
    "eval_frequency_min", "cooldown_min",
    "trigger_eff_high", "trigger_eff_low",
    "consecutive_high", "consecutive_low",
)


def ts_utc(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


# ---------------------------------------------------------------------------
# Trace: task-events flow per objective (the only honest efficiency source)
# ---------------------------------------------------------------------------

def parse_trace_stats(trace: Path, window_s: float = WINDOW_S,
                      now: float | None = None) -> dict[str, dict]:
    """Per-objective task-event cause counts within the window.

    Only lines whose source == "task-events" count: they carry the
    objective stamp. Cost lines (nanogpt-requests / usage-audit /
    model-cost-ledger) carry objective="unattributed" and NO task_id, so
    they are deliberately ignored here (see module docstring).
    """
    if now is None:
        now = time.time()
    cutoff = now - window_s
    stats: dict[str, dict] = {}
    if not trace.exists():
        return stats
    with open(trace, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("source") != "task-events":
                continue
            try:
                ts = float(d.get("ts_epoch_utc") or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            cause = d.get("cause")
            if cause not in ATTEMPTED_CAUSES:
                continue
            oid = str(d.get("objective") or "unattributed").upper()
            bucket = stats.setdefault(oid, {c: 0 for c in ATTEMPTED_CAUSES})
            bucket[cause] += 1
    return stats


def efficiency_of(stats_row: dict) -> tuple[float | None, int, int]:
    """(efficiency, attempted, verified); (None, 0, 0) when no attempts."""
    attempted = sum(stats_row.get(c, 0) for c in ATTEMPTED_CAUSES)
    verified = stats_row.get("completed", 0)
    if attempted <= 0:
        return None, 0, 0
    return verified / attempted, attempted, verified


# ---------------------------------------------------------------------------
# Presets: DB overlay on the built-in base + 'auto' resolution
# ---------------------------------------------------------------------------

def load_presets(conn: sqlite3.Connection) -> dict[str, dict]:
    """preset_id -> effective parameter dict (built-in base + DB override).

    A DB row with is_active == 0 is dropped (unknown id falls back to
    'normal' at resolution time). NULL columns fall through to the base.
    """
    presets = {k: dict(v) for k, v in BUILTIN_PRESETS.items()}
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(adjustment_presets)")}
        if not cols:
            return presets
        usable = [k for k in PRESET_PARAM_KEYS if k in cols]
        for row in conn.execute("SELECT * FROM adjustment_presets"):
            d = {k: row[k] for k in row.keys()}
            pid = d.get("id")
            if not pid or d.get("is_active") == 0:
                continue
            merged = presets.get(pid, {k: None for k in PRESET_PARAM_KEYS})
            for k in usable:
                if d.get(k) is not None:
                    merged[k] = d[k]
            presets[pid] = merged
    except sqlite3.Error:
        pass  # table missing -> built-ins only (fail-open)
    return presets


def resolve_preset(preset_id: str | None, presets: dict[str, dict],
                   eff: float | None) -> tuple[str, dict, str | None]:
    """(resolved_id, preset, note). Unknown/missing ids -> 'normal'."""
    pid = (preset_id or "normal").strip().lower() or "normal"
    if pid == "auto":
        if eff is None:
            return "auto:protected", presets["protected"], \
                "auto: no data -> protected (no adjustments without evidence)"
        if eff <= AUTO_LOW_EFF:
            return "auto:conservative", presets["conservative"], \
                "auto: low efficiency -> conservative (small careful steps)"
        if eff >= AUTO_HIGH_EFF:
            return "auto:aggressive", presets["aggressive"], \
                "auto: high efficiency -> aggressive (wider steps)"
        return "auto:normal", presets["normal"], "auto: mid efficiency"
    preset = presets.get(pid)
    if preset is None:
        return "normal", presets["normal"], f"unknown preset '{pid}' -> normal"
    return pid, preset, None


# ---------------------------------------------------------------------------
# Lifecycle log: the ratchet's persistent state (last entry per objective)
# ---------------------------------------------------------------------------

def read_lifecycle_entries(log: Path) -> list[dict]:
    entries = []
    if not log.exists():
        return entries
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict) and \
                    d.get("kind") == "objective_lifecycle" and d.get("objective"):
                entries.append(d)
    return entries


def prior_state(entries: list[dict], oid: str) -> dict:
    """Latest persisted ratchet state for one objective."""
    for d in reversed(entries):
        if d.get("objective") == oid:
            return {
                "state": d.get("state"),
                "streak_low": int(d.get("streak_low") or 0),
                "streak_high": int(d.get("streak_high") or 0),
                "last_adjusted": d.get("last_adjusted"),
                "ts_epoch": d.get("ts_epoch"),
                "alarm_low_fired": bool(d.get("alarm_low_fired")),
            }
    return {"state": None, "streak_low": 0, "streak_high": 0,
            "last_adjusted": None, "ts_epoch": None,
            "alarm_low_fired": False}


# ---------------------------------------------------------------------------
# Board diagnosis (read-only)
# ---------------------------------------------------------------------------

def board_diagnosis(conn: sqlite3.Connection, oid: str) -> dict:
    """Live queue sizes for the objective's tag + nothing else.

    Task bodies carry the tag as `objective:OBJ-xxx`; LIKE is
    case-insensitive for ASCII in SQLite, matching the writer side.
    """
    out: dict[str, int] = {}
    try:
        like = f"%objective:{oid}%"
        for status in ("running", "ready", "blocked"):
            out[status] = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ? AND body LIKE ?",
                (status, like)).fetchone()[0]
    except sqlite3.Error as e:
        return {"error": str(e)}
    return out


def dominant_failure(stats_row: dict) -> str:
    """Deterministic cause note for the diagnostics block."""
    counts = {c: stats_row.get(c, 0) for c in FAILURE_CAUSES}
    total_fail = sum(counts.values())
    if total_fail == 0:
        return ("no_failure_signals" if any(
            stats_row.get(c, 0) for c in ATTEMPTED_CAUSES)
            else "no_data_in_window")
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return f"dominant_failure={top[0]} (n={top[1]}/{total_fail})"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_conn(db: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def focus_reset_expired(conn: sqlite3.Connection, now: float,
                        write: bool) -> list[dict]:
    """P2 step 1: expired focus_until -> nice=0, focus_until=NULL.

    Returns one record per reset row (id, nice_before, focus_before).
    With write=False nothing is mutated (dry-run report only).
    """
    resets = []
    try:
        rows = conn.execute(
            "SELECT id, nice, focus_until FROM approved_objectives "
            "WHERE focus_until IS NOT NULL AND focus_until < ?",
            (now,)).fetchall()
    except sqlite3.Error as e:
        return [{"error": str(e)}]
    for r in rows:
        resets.append({"id": r["id"], "nice_before": r["nice"],
                       "focus_before": r["focus_until"]})
        if write:
            conn.execute(
                "UPDATE approved_objectives SET nice = 0, focus_until = NULL, "
                "updated_at = ?, updated_by = 'objective-lifecycle' "
                "WHERE id = ?", (now, r["id"]))
    return resets


def load_objectives(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM approved_objectives WHERE status = 'active' "
            "ORDER BY id").fetchall()
    except sqlite3.Error:
        return []


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Per-objective evaluation -> (entry, updates)
# ---------------------------------------------------------------------------

def evaluate_objective(conn: sqlite3.Connection, row: sqlite3.Row,
                       presets: dict[str, dict], stats: dict[str, dict],
                       prior: dict, now: float,
                       window_s: float = WINDOW_S) -> tuple[dict, list]:
    oid = row["id"]
    gov = (row["governance"] or "static").strip().lower()
    entry = {
        "ts": ts_utc(now), "kind": "objective_lifecycle", "objective": oid,
        "governance": gov, "window_hours": round(window_s / 3600, 1),
        "ts_epoch": now,
        "state": None, "streak_low": 0, "streak_high": 0,
        "last_adjusted": prior.get("last_adjusted"),
        "cooldown_ok": True, "evaluated": False, "actions": [],
        "diagnostics": {}, "alarms": [],
        # t_f5838b27 §14: objective state snapshot per entry — the
        # hermes-objective-lifecycle OO stream becomes the dashboard's
        # nice/budget/preset time series (nice resets included).
        "nice": row["nice"] if row["nice"] is not None else 0,
        "budget_daily": row["budget_daily"],
        "budget_baseline": row["budget_baseline"],
        "budget_adjustment_pct": row["budget_adjustment_pct"],
        "preset_id": row["preset_id"] or "normal",
        "focus_until": row["focus_until"],
    }
    updates: list[tuple[str, tuple]] = []

    # Stale-prior break: the ratchet measures CONSECUTIVE evaluations. If the
    # previous entry is too old (cron down / eval_frequency skipped), the
    # streak restarts from this tick instead of carrying hours-old evidence.
    p_ts = prior.get("ts_epoch")
    if p_ts is not None:
        try:
            if (now - float(p_ts)) > STALE_PRIOR_S:
                prior = dict(prior, state=None, streak_low=0, streak_high=0,
                             alarm_low_fired=False)
                entry["stale_note"] = (
                    "stale prior entry — streak restarted "
                    f"(gap > {STALE_PRIOR_S // 3600}h)")
        except (TypeError, ValueError):
            pass

    stats_row = stats.get(oid) or {c: 0 for c in ATTEMPTED_CAUSES}
    eff, attempted, verified = efficiency_of(stats_row)
    entry["efficiency"] = None if eff is None else round(eff, 3)
    entry["attempted"] = attempted
    entry["verified"] = verified
    entry["diagnostics"] = {
        "board": board_diagnosis(conn, oid),
        "causes": {c: stats_row.get(c, 0) for c in ATTEMPTED_CAUSES},
        "note": dominant_failure(stats_row),
    }

    if eff is None:
        entry["state"] = None
        entry["note"] = "SIN DATOS: no task-events in window — no adjustment"
        return entry, updates

    pid, preset, note = resolve_preset(row["preset_id"], presets, eff)
    entry["preset_id"] = pid
    if note:
        entry["preset_note"] = note

    # Current ratchet state from this tick's efficiency.
    if eff >= preset["trigger_eff_high"]:
        state, streak = "high", prior["streak_high"] + 1
    elif eff <= preset["trigger_eff_low"]:
        state, streak = "low", prior["streak_low"] + 1
    else:
        state, streak = None, 0
    entry["state"] = state
    entry["streak_low"] = streak if state == "low" else 0
    entry["streak_high"] = streak if state == "high" else 0
    # The once-per-episode low alarm: carried while the objective stays low
    # (stale break above resets it), cleared as soon as it leaves low.
    entry["alarm_low_fired"] = bool(
        prior.get("alarm_low_fired")) if state == "low" else False

    # Cooldown: measured from the LAST actual adjustment.
    last_adj = prior.get("last_adjusted")
    if last_adj is not None and (now - last_adj) < preset["cooldown_min"] * 60:
        entry["cooldown_ok"] = False
        entry["note"] = ("cooldown active "
                         f"({preset['cooldown_min']}min) — state carried, "
                         "no adjustment")
        return entry, updates

    # Threshold not yet reached: record only (two-tick ratchet minimum).
    if state is None or (
            state == "low" and streak < preset["consecutive_low"]) or (
            state == "high" and streak < preset["consecutive_high"]):
        entry["note"] = "state recorded — streak below trigger threshold"
        return entry, updates

    # --- adjustment tick -------------------------------------------------
    evaluated_actions: list[dict] = []
    entry["evaluated"] = True  # trigger threshold met, cooldown clear

    if gov in GOV_NICE and preset["nice_step"]:
        step = preset["nice_step"]
        cap_hi, cap_lo = preset["nice_cap_high"], preset["nice_cap_low"]
        cur = int(row["nice"] or 0)
        want = cur - step if state == "low" else cur + step
        new = int(_clamp(want, cap_lo, cap_hi))
        if new != cur:
            updates.append((
                "UPDATE approved_objectives SET nice = ?, updated_at = ?, "
                "updated_by = 'objective-lifecycle' WHERE id = ?",
                (new, now, oid)))
            action = {"type": "nice", "from": cur, "to": new,
                      "reason": f"sustained {state} efficiency "
                                f"(streak={streak})"}
            if new == want and state == "low" and new <= cap_lo:
                pass  # exactly at cap, not beyond — no alarm
            if (state == "low" and want < cap_lo) or \
                    (state == "high" and want > cap_hi):
                action["clamped"] = True
                entry["alarms"].append({
                    "status": "NICE_CAP", "objective": oid,
                    "detail": f"nice wanted {want}, clamped to {new} "
                              f"(caps [{cap_lo}, {cap_hi}])"})
            evaluated_actions.append(action)
        entry["last_adjusted"] = now

    if gov in GOV_BUDGET and preset["budget_step_pct"] and \
            (row["budget_baseline"] or 0) > 0:
        step = preset["budget_step_pct"]
        cap_hi, cap_lo = preset["budget_cap_high_pct"], \
            preset["budget_cap_low_pct"]
        cur_pct = float(row["budget_adjustment_pct"] or 0)
        baseline = float(row["budget_baseline"])
        cur_budget = float(row["budget_daily"] or 0)
        want_pct = cur_pct - step if state == "low" else cur_pct + step
        new_pct = round(_clamp(want_pct, cap_lo, cap_hi), 2)
        new_budget = round(baseline * (1 + new_pct / 100.0), 4)
        if abs(new_budget - cur_budget) > 1e-9 or new_pct != cur_pct:
            updates.append((
                "UPDATE approved_objectives SET budget_adjustment_pct = ?, "
                "budget_daily = ?, updated_at = ?, "
                "updated_by = 'objective-lifecycle' WHERE id = ?",
                (new_pct, new_budget, now, oid)))
            action = {"type": "budget", "from_pct": cur_pct,
                      "to_pct": new_pct, "from_daily": cur_budget,
                      "to_daily": new_budget,
                      "reason": f"sustained {state} efficiency "
                                f"(streak={streak})"}
            if (state == "low" and want_pct < cap_lo) or \
                    (state == "high" and want_pct > cap_hi):
                action["clamped"] = True
                entry["alarms"].append({
                    "status": "BUDGET_CAP", "objective": oid,
                    "detail": f"pct wanted {want_pct}, clamped to {new_pct} "
                              f"(caps [{cap_lo}, {cap_hi}])"})
            evaluated_actions.append(action)
        entry["last_adjusted"] = now

    if not evaluated_actions:
        entry["note"] = ("trigger met but preset step is 0 / caps bind — "
                         "nothing to change")
        entry["last_adjusted"] = prior.get("last_adjusted")
    else:
        entry["note"] = f"adjustment applied ({len(evaluated_actions)} action(s))"
    entry["actions"] = evaluated_actions
    return entry, updates


# ---------------------------------------------------------------------------
# Notification (cron-alarms.jsonl — the channel morning-screen consumes)
# ---------------------------------------------------------------------------

def alarm_lines(entries: list[dict], prior_map: dict[str, dict]) -> list[dict]:
    """State TRANSITIONS into sustained-low + cap saturation -> alarms.

    A sustained-low episode alarms exactly ONCE: the trip tick (evaluated,
    cooldown clear) fires unless the prior entry already fired for this
    episode (alarm_low_fired flag persists in the log and resets whenever
    the objective leaves the low state).
    """
    out = []
    for e in entries:
        oid = e.get("objective")
        if not oid:
            continue
        prev = prior_map.get(oid) or {}
        for a in e.get("alarms", []):
            out.append({
                "ts": e["ts"], "cron": "objective-lifecycle",
                "status": a["status"], "action": "none",
                "action_result": "-", "objective": oid,
                "detail": a["detail"]})
        if e.get("state") == "low" and e.get("evaluated") and \
                e.get("cooldown_ok", True) and \
                e.get("efficiency") is not None and \
                not prev.get("alarm_low_fired"):
            e["alarm_low_fired"] = True
            out.append({
                "ts": e["ts"], "cron": "objective-lifecycle",
                "status": "EFFICIENCY_LOW", "action": "none",
                "action_result": "-",
                "objective": oid,
                "detail": f"efficiency={e.get('efficiency')} "
                          f"streak_low={e.get('streak_low')} "
                          f"note={e.get('note')}"})
    return out


def append_jsonl(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, verbose: bool = False,
        db: Path | None = None, trace: Path | None = None,
        log: Path | None = None, alarms: Path | None = None) -> dict:
    now = time.time()
    db = db or kanban_db_path()
    trace = trace or trace_path()
    log = log or lifecycle_log_path()
    alarms = alarms or alarms_path()

    print(f"[{ts_utc(now)}] objective-lifecycle: "
          f"{'DRY-RUN' if dry_run else 'tick'}")

    summary = {"db": str(db), "trace": str(trace), "resets": [],
               "entries": [], "updates_applied": 0, "alarms": 0,
               "errors": []}
    if not db.exists():
        summary["errors"].append(f"kanban.db not found: {db}")
        print("objective-lifecycle: kanban.db not found — nothing to do")
        return summary

    conn = open_conn(db, readonly=dry_run)
    try:
        resets = focus_reset_expired(conn, now, write=not dry_run)
        summary["resets"] = resets
        if resets and not dry_run:
            conn.commit()
        for r in resets:
            print(f"  focus reset: {r}")

        presets = load_presets(conn)
        stats = parse_trace_stats(trace, now=now)
        prior_entries = read_lifecycle_entries(log)
        prior_map = {oid_: prior_state(prior_entries, oid_)
                     for oid_ in {e["objective"] for e in prior_entries}}

        entries, all_updates, all_alarms = [], [], []
        for row in load_objectives(conn):
            gov = (row["governance"] or "static").strip().lower()
            if gov == "static":
                continue  # matrix: static = never evaluated
            if gov not in GOV_NICE and gov not in GOV_BUDGET:
                # fail-closed for unknown enums (same spirit as static);
                # evaluated=True so the row still logs (visible, inert).
                try:
                    stats_row = stats.get(row["id"]) or {
                        c: 0 for c in ATTEMPTED_CAUSES}
                    eff, attempted, verified = efficiency_of(stats_row)
                except Exception:
                    eff, attempted, verified = None, 0, 0
                entries.append({
                    "ts": ts_utc(now), "kind": "objective_lifecycle",
                    "objective": row["id"], "governance": gov,
                    "window_hours": round(WINDOW_S / 3600, 1),
                    "ts_epoch": now, "state": None, "streak_low": 0,
                    "streak_high": 0, "last_adjusted": None,
                    "cooldown_ok": True, "evaluated": True, "actions": [],
                    "efficiency": None if eff is None else round(eff, 3),
                    "attempted": attempted, "verified": verified,
                    "diagnostics": {},
                    "note": (f"unknown governance '{gov}' — treated as "
                             "static (no adjustments)"),
                })
                continue
            oid = row["id"]
            try:
                entry, updates = evaluate_objective(
                    conn, row, presets, stats,
                    prior_map.get(oid) or prior_state(prior_entries, oid),
                    now)
            except Exception as e:  # fail-open per objective
                summary["errors"].append(f"{oid}: {e}")
                print(f"  ERROR {oid}: {e}")
                continue
            entries.append(entry)
            all_updates.extend(updates)
            all_alarms.extend(
                a for a in alarm_lines([entry],
                                       {oid: prior_map.get(oid) or {}})
                if a["objective"] == oid)

        summary["entries"] = entries

        if dry_run:
            for e in entries:
                print(json.dumps(e, ensure_ascii=False))
            for sql, params in all_updates:
                print(f"  [would-exec] {sql} | {params}")
            for a in all_alarms:
                print(f"  [would-alarm] {json.dumps(a, ensure_ascii=False)}")
        else:
            summary["updates_applied"] = append_jsonl(log, entries)
            summary["alarms"] = append_jsonl(alarms, all_alarms)
            try:
                for sql, params in all_updates:
                    conn.execute(sql, params)
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                summary["errors"].append(f"db write failed: {e}")
                print(f"objective-lifecycle: db write failed: {e}")
            if verbose:
                for e in entries:
                    print(f"  {e['objective']} eff={e.get('efficiency')} "
                          f"state={e.get('state')} "
                          f"preset={e.get('preset_id')} "
                          f"actions={len(e.get('actions', []))} "
                          f"note={e.get('note')}")
    finally:
        conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Governance P2: hourly objective lifecycle cron")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, zero writes")
    ap.add_argument("--verbose", action="store_true",
                    help="per-objective summary lines on stdout")
    args = ap.parse_args(argv)
    run(dry_run=args.dry_run, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
