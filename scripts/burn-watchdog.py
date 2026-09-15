#!/usr/bin/env python3
"""burn-watchdog.py — generic spend-burn watchdog for ALL quota providers.

Governor plugin (MULTI-PROV-08). Detects rapid spend leaks — e.g. the
Sep 7 2026 opencode-go incident ($1.04/30 min while the 5h rolling window
was rate-limited and the balance-fallback was burning prepaid Zen balance) —
and halts them at a configurable cumulative threshold, instead of requiring
manual console observation.

CONTRACT
--------
This watchdog NEVER calls a provider API directly. It consumes the
already-normalized gate snapshot produced by ``quota-gate.py`` — run as a
subprocess, JSON parsed — and inspects the ``context.providers`` array. All
quota data gathering lives in the gate (with its retry + last-known-good
cache). The watchdog only reasons about spend.

The per-request ``cost`` field from chat/completions is the calibrated
burn meter (t_47640f18: when a window is exhausted and balance-fallback is
ON, ``cost`` leaves "0" and reports real USD; summing cost = exact
burned-balance). When a provider entry in the snapshot carries a ``cost``
field (top-level or in ``raw``), the watchdog uses it directly. When absent
(e.g. the gate does not probe chat/completions today), it FALLS BACK to
estimating USD from percent-delta x config ``window_usd_cap`` — a
lower-bound, since burn above a 100% window is invisible to percent.

SIGNALS (per provider, priority order)
-------------------------------------
  (a) ``burning_balance == true`` (window status != ok + balance-fallback)
      — qualitative, strongest. Emitted by the gate for opencode-go.
  (b) any window ``*_status`` != "ok" (generic, data-driven → a 5th
      provider needs ZERO script changes, just snapshot fields + config).
  (c) window-usage percent delta between ticks, converted to USD via
      config ``window_usd_cap`` (rate = USD / elapsed minutes).
  (d) per-provider ``cost`` meter delta (calibrated per t_47640f18).
      NOTE: ollama's ``activity.cost`` is an EXISTING tick signal — we
      observe/record it in the ledger but do NOT re-implement its stop.

ACTIONS (graduated)
-------------------
  WARN  — ledger entry + a ``burn_warnings`` entry in state file
          (~/.hermes/quota-governor/burn-warnings.json) that the gate /
          task creator can surface on its next snapshot.
  STOP  — write the SHARED global STOP file read by the ollama tick
          (~/.hermes/profiles/<profile>/quota-governor/STOP), kill the
          daemon via its PIDFILE, and emit a one-line evidence alert to
          stdout (provider, rate USD/min, cumulative USD, window).
  CLEAR — manual ONLY. Delete the STOP file and/or reset the ledger
          baseline by hand. The watchdog never auto-clears a STOP.

SILENCE
-------
Empty stdout when no burn. Cron runs it every 10m with --no-agent ⇒ zero
token cost. It only emits to stdout when a WARN or STOP fires.

CONFIG
------
~/.hermes/quota-governor/burn-watchdog.json
  {
    "<provider>": {
      "enabled": bool,
      "burn_rate_warn_usd_per_min": float,   # default 0.05
      "burn_total_warn_usd": float,          # default 0.20 (cumulative)
      "burn_total_stop_usd": float,          # default 1.00 (cumulative)
      "window_usd_cap": float|null           # USD value of 100% of the
                                             # provider's window (for the
                                             # percent-delta fallback estimate)
    }
  }
A provider with NO entry, or "enabled": false, is OBSERVE-ONLY: ledger +
rate are computed but no WARN/STOP action is taken (current behavior).

STATE
-----
~/.hermes/quota-governor/burn-ledger.jsonl      — append-only history
~/.hermes/quota-governor/burn-state.json        — per (provider, window):
                                                    last_pct, last_cost,
                                                    cum_cost_usd, baseline,
                                                    first_burn_ts, rate...
~/.hermes/quota-governor/burn-warnings.json     — current open warnings
                                                    (consumed by gate)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HERMES_HOME = os.environ.get(
    "HERMES_HOME", os.path.expanduser("~/.hermes/profiles/pr-ollama")
)
# Single state directory: co-located under <HERMES_HOME>/quota-governor, the
# SAME convention used by the gate (_cache_dir), the balance ledger, and the
# weekly tracker. This was historically split — the watchdog defaulted to
# the top-level ~/.hermes/quota-governor while the ledger/gate resolved
# <HERMES_HOME>/quota-governor — so nanogpt-balance-ledger.jsonl lived in a
# DIFFERENT dir than the watchdog could see, and burn-warnings wrote to a
# place the gate never read. BURN_STATE_DIR remains a test override.
STATE_DIR = os.environ.get(
    "BURN_STATE_DIR", os.path.join(HERMES_HOME, "quota-governor")
)
LEDGER_FILE = os.path.join(STATE_DIR, "burn-ledger.jsonl")
STATE_FILE = os.path.join(STATE_DIR, "burn-state.json")
WARN_FILE = os.path.join(STATE_DIR, "burn-warnings.json")
CONFIG_FILE = os.path.join(STATE_DIR, "burn-watchdog.json")

STOP_FILE = os.path.join(HERMES_HOME, "quota-governor", "STOP")
PIDFILE = os.path.join(HERMES_HOME, "quota-governor-daemon.pid")

# Gap detection (OBJ-39/t_cce2a554): the watchdog cron interval is 10m. A
# tick that slips > GAP_MULTIPLIER x that interval means the per-tick
# balance-delta couldn't see burn that happened in the hole — a 90s gate
# timeout produced a 16:46→18:33Z hole on 14-sep that hid a $7.52 balance
# spike. During a gap the watchdog covers retroactively from the INDEPENDENT
# balance ledger written by nanogpt-balance-ledger.py every ~3 min.
LEDGER_INTERVAL_S = 10 * 60          # watchdog cron interval (10m)
GAP_MULTIPLIER = 2.0                 # >2x interval = a skipped tick
BALANCE_LEDGER_FILE = os.path.join(STATE_DIR, "nanogpt-balance-ledger.jsonl")

DEFAULT_GATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "quota-gate.py"
)

# --------------------------------------------------------------------------
# Config / defaults
# --------------------------------------------------------------------------

DEFAULTS = {
    "enabled": False,               # observe-only unless configured on
    "burn_rate_warn_usd_per_min": 0.05,
    "burn_total_warn_usd": 0.20,
    "burn_total_stop_usd": 1.00,
    "window_usd_cap": None,
}


def load_config() -> Dict[str, Dict[str, Any]]:
    """Load per-provider thresholds. No file ⇒ observe-only everywhere."""
    cfg: Dict[str, Dict[str, Any]] = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for prov, opts in raw.items():
            merged = dict(DEFAULTS)
            if isinstance(opts, dict):
                for k, v in opts.items():
                    if v is not None:
                        merged[k] = v
            cfg[prov] = merged
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        # Broken config must never break the watchdog; degrade to observe-only.
        _stderr(f"burn-watchdog: config error ({exc}); observe-only this run")
    return cfg


def _prov_cfg(config, provider) -> Dict[str, Any]:
    return config.get(provider, dict(DEFAULTS))


# --------------------------------------------------------------------------
# Snapshot ingestion — gate subprocess, JSON output
# --------------------------------------------------------------------------

def load_snapshot(gate_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Run quota-gate.py (subprocess) and return its JSON dict.

    Never talks to a provider directly — the gate owns all API access. On
    failure (missing gate, non-zero exit, unparseable JSON) returns None so
    the watchdog stays silent for that tick (gate transient errors already
    fall back to last-known-good caches internally).
    """
    gate = gate_path or DEFAULT_GATE
    if not os.path.exists(gate):
        _stderr(f"burn-watchdog: gate not found at {gate}; skipping tick")
        return None
    # Strip proxy envs — providers (opencode-go, ollama) reject Tor exit
    # nodes; the gate already does this internally but belt-and-braces here.
    env = dict(os.environ)
    for v in list(env):
        if v.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(v, None)
    try:
        proc = subprocess.run(
            [sys.executable, gate],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _stderr(f"burn-watchdog: gate launch failed ({exc}); skipping tick")
        return None
    if proc.returncode != 0:
        _stderr(f"burn-watchdog: gate exited {proc.returncode}; skipping tick")
        return None
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        _stderr("burn-watchdog: gate produced no output; skipping tick")
        return None
    try:
        return json.loads(lines[-1])
    except (ValueError, IndexError) as exc:
        _stderr(f"burn-watchdog: gate output unparseable ({exc}); skipping tick")
        return None


def providers_from_snapshot(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the normalized providers array from the gate snapshot."""
    return snapshot.get("context", {}).get("providers", [])


# --------------------------------------------------------------------------
# Per-provider window / cost extraction (data-driven, provider-agnostic)
# --------------------------------------------------------------------------

def window_fields(provider: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return [{name, pct, status}] from a provider's raw dict.

    Generic scan: every ``*_pct`` key with a numeric value yields a window
    named after its prefix; a sibling ``<prefix>_status`` is the status.
    A 5th provider needs zero script changes — just snapshot fields.
    """
    raw = provider.get("raw", {}) if isinstance(provider.get("raw"), dict) else {}
    windows: List[Dict[str, Any]] = []
    for key, val in raw.items():
        if not key.endswith("_pct"):
            continue
        name = key[:-4]  # strip "_pct"
        try:
            pct = float(val)
        except (TypeError, ValueError):
            continue
        status = raw.get(f"{name}_status")
        windows.append({"name": name, "pct": pct, "status": status})
    return windows


def provider_cost(provider: Dict[str, Any]) -> Optional[float]:
    """A per-provider USD burn meter from the snapshot, if present.

    Priority: entry ``cost`` → raw ``cost`` → raw ``activity_cost``
    (ollama pay-as-you-go) → entry ``usd_balance`` (nanogpt prepaid
    balance, OBJ-26: exact POST /api/check-balance probe).
    Returns None when the snapshot carries no real USD meter for this
    provider (the watchdog then falls back to the percent-delta x
    window_usd_cap estimate).

    OBJ-26 SIGN CONVENTION: the ``cost``/``activity_cost`` meters are
    CUMULATIVE SPEND (higher = more burned), but the nanogpt
    ``usd_balance`` meter is a DECREASING BALANCE (lower = more burned).
    Callers must treat the returned value as opaque and derive the tick
    spend with ``cost_meter_delta`` / ``meter_is_balance`` — never with a
    plain ``now - last`` difference.
    """
    for key in ("cost",):
        val = provider.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    raw = provider.get("raw", {}) if isinstance(provider.get("raw"), dict) else {}
    for key in ("cost", "activity_cost"):
        val = raw.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # OBJ-26: nanogpt exact prepaid balance travels at entry level.
    bal = provider.get("balance") or {}
    if isinstance(bal, dict) and bal.get("usd_balance") is not None:
        try:
            return float(bal["usd_balance"])
        except (TypeError, ValueError):
            pass
    return None


def meter_is_balance(provider: Dict[str, Any]) -> bool:
    """True when provider_cost() returned the nanogpt DECREASING balance
    (OBJ-26) rather than a cumulative-spend meter: no ``cost`` /
    ``activity_cost`` anywhere, but ``balance.usd_balance`` present."""
    if provider.get("cost") is not None:
        return False
    raw = provider.get("raw", {}) if isinstance(provider.get("raw"), dict) else {}
    if raw.get("cost") is not None or raw.get("activity_cost") is not None:
        return False
    bal = provider.get("balance") or {}
    return isinstance(bal, dict) and bal.get("usd_balance") is not None


def is_burning(provider: Dict[str, Any]) -> bool:
    """Qualitative burn test: window exhausted but (possibly) serving.

    True when the gate's ``burning_balance`` flag is set, or any window
    status is a non-None, non-"ok" value (rate-limited / exceeded).
    """
    if provider.get("burning_balance") is True:
        return True
    for w in window_fields(provider):
        if w["status"] is not None and w["status"] != "ok":
            return True
    return False


# --------------------------------------------------------------------------
# State / ledger persistence
# --------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    except OSError as exc:
        _stderr(f"burn-watchdog: cannot save state ({exc})")


def append_ledger(entry: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        entry.setdefault("ts", int(time.time()))
        with open(LEDGER_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        _stderr(f"burn-watchdog: cannot append ledger ({exc})")


def save_warnings(warnings: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(WARN_FILE, "w", encoding="utf-8") as fh:
            json.dump(warnings, fh, indent=2, sort_keys=True)
    except OSError as exc:
        _stderr(f"burn-watchdog: cannot write warnings ({exc})")


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def write_stop_file(evidence: str) -> None:
    try:
        os.makedirs(os.path.dirname(STOP_FILE), exist_ok=True)
        with open(STOP_FILE, "w", encoding="utf-8") as fh:
            fh.write("burn-watchdog STOP\n" + evidence + "\n")
    except OSError as exc:
        _stderr(f"burn-watchdog: cannot write STOP file ({exc})")


def kill_daemon() -> Optional[int]:
    """Kill the kanban daemon via its PIDFILE. Returns killed pid or None."""
    try:
        with open(PIDFILE, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
        return pid
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.unlink(PIDFILE)
        except OSError:
            pass
        return None


# --------------------------------------------------------------------------
# Core analysis
# --------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0) -> float:
    return lo if v < lo else v


def _estimate_tick_cost(
    provider: Dict[str, Any],
    prior: Dict[str, Any],
    cfg: Dict[str, Any],
    bottleneck: Optional[Dict[str, Any]],
    burning: bool,
):
    """Estimate USD burned over this tick.

    Priority: real cost meter (calibrated), else percent-delta x
    window_usd_cap.
    """
    cost_now = provider_cost(provider)
    cost_delta = 0.0
    if cost_now is not None:
        last_cost = prior.get("last_cost")
        if last_cost is not None:
            if meter_is_balance(provider):
                # OBJ-26: DECREASING balance meter — spend = last - now.
                # A rise is a top-up / external credit, NOT negative burn:
                # clamp it to 0 (the balance ledger re-baselines its own
                # window on top-ups, so the budget stays consistent).
                cost_delta = _clamp(float(last_cost) - cost_now)
            else:
                cost_delta = _clamp(cost_now - float(last_cost))
        # else: first observation with a cost meter — seed the baseline; the
        # next tick's delta measures only what was burned between ticks.

    pct_delta_usd = 0.0
    window_usd_cap = cfg.get("window_usd_cap")
    if window_usd_cap and bottleneck is not None:
        last_pct = prior.get("last_pct")
        if last_pct is not None and burning:
            pct_delta = _clamp(bottleneck["pct"] - float(last_pct))
            pct_delta_usd = pct_delta / 100.0 * float(window_usd_cap)
        # On the first burn observation, seed the baseline so subsequent
        # deltas measure only incremental burn.

    # Use the real cost meter when available; otherwise the estimate.
    tick_cost = cost_delta if cost_now is not None else pct_delta_usd
    return cost_now, tick_cost


def _compute_cumulative(cost_now, prior, tick_cost, provider, burning, cum):
    """Compute cumulative cost for a provider.

    ``burning`` is the CURRENT tick's flag (from ``is_burning``), matching the
    pre-refactor inline logic: only the percent-delta estimate path stays
    gated on it. Handles the special case of a DECREASING balance meter that
    has no ``last_cost`` record: ``seed_balance_cum`` pulls the cumulative
    figure from the stance ledger. After seeding the per-tick delta is added.
    """
    if cost_now is not None:
        if prior.get("last_cost") is None and meter_is_balance(provider):
            cum = seed_balance_cum(provider, prior)
        cum += tick_cost
    elif burning and tick_cost > 0:
        cum += tick_cost
    return cum


def _compute_rate(tick_elapsed, prior, tick_cost):
    """Calculate burn rate given elapsed time and tick cost."""
    if tick_elapsed > 0 and prior.get("last_ts") and tick_cost > 0:
        return tick_cost / max(tick_elapsed, 0.001)
    return 0.0


def analyze_provider(
    provider: Dict[str, Any],
    prior: Dict[str, Any],
    cfg: Dict[str, Any],
    now: float,
    tick_elapsed: float,
) -> Dict[str, Any]:
    """Analyze one provider against its previous state.

    ``prior`` is the saved per-provider state for this tick interval
    (dict keyed by window name with last_pct/last_cost/cum_cost/baseline).
    ``tick_elapsed`` = minutes since the previous observation (used to
    derive USD/min burn rate from percent deltas / cost deltas).

    Returns {"rate": ..., "cum": ..., "burning": bool, "window_pct": {...}}
    """
    burning = is_burning(provider)
    windows = window_fields(provider)
    # Pick the bottleneck (max pct) window for reporting.
    bottleneck = max(windows, key=lambda w: w["pct"], default=None)

    cost_now, tick_cost = _estimate_tick_cost(provider, prior, cfg, bottleneck, burning)

    # Retroactive cover over a >2x-interval hole: the watchdog was not running
    # (gate timeout, cron gap), so the per-tick delta below is only partial.
    # Fold in the balance-ledger burn the watchdog missed BEFORE the current
    # delta accumulates, so a burst that fell entirely in the hole still trips
    # WARN/STOP.
    cover = read_balance_ledger(provider, prior, now)
    if cover["gap"]:
        tick_cost += cover["covered_usd"]

    cum = float(prior.get("cum_cost_usd", 0.0))
    cum = _compute_cumulative(cost_now, prior, tick_cost, provider, burning, cum)

    rate = _compute_rate(tick_elapsed, prior, tick_cost)

    return {
        "burning": burning,
        "rate_usd_per_min": rate,
        "cum_cost_usd": cum,
        "bottleneck_window": bottleneck["name"] if bottleneck else None,
        "bottleneck_pct": bottleneck["pct"] if bottleneck else None,
        "cost_now": cost_now,
        "tick_cost_usd": tick_cost,
        "gap_covered_usd": cover["covered_usd"] if cover["gap"] else 0.0,
        "gap": cover["gap"],
    }


def run_tick(gate_path: Optional[str] = None, now: Optional[float] = None) -> List[str]:
    """Orchestrate a watchdog tick. Delegates provider logic to helpers to keep each function <50 lines."""
    now_t = now if now is not None else time.time()
    config = load_config()
    state = _load_state()
    snapshot = load_snapshot(gate_path)
    alerts: List[str] = []
    active_warnings: Dict[str, Any] = {}

    for provider in providers_from_snapshot(snapshot or {}):
        alerts, active_warnings, state = _handle_provider(provider, state, config, now_t, active_warnings, alerts)

    save_state(state)
    if active_warnings:
        save_warnings(active_warnings)
    return alerts

# --- Helper functions below ----------------------------------------------


def _handle_provider(provider: Dict[str, Any], state: Dict[str, Any], config: Dict[str, Any], now_ts: float, active_warnings: Dict[str, Any], alerts: List[str]):
    """Process one provider and return updated alerts, active_warnings, state."""
    prov = provider.get("provider")
    if not prov:
        return alerts, active_warnings, state
    cfg = _prov_cfg(config, prov)
    pstate = state.get(prov, {})
    res = _analyze_provider_state(provider, pstate, cfg, now_ts)
    _persist_provider_observation(state, prov, pstate, res, now_ts)
    if not cfg.get("enabled"):
        return alerts, active_warnings, state
    return _decide_provider_actions(
        state, prov, pstate, res, cfg, now_ts, alerts, active_warnings
    )


# --------------------------------------------------------------------------
# Balance-ledger retroactive cover + gap detection (OBJ-39/t_cce2a554)
# --------------------------------------------------------------------------

def read_balance_ledger(provider: Dict[str, Any], pstate: Dict[str, Any], now_ts: float) -> Dict[str, Any]:
    """Return {covered_usd, covered_since, gap} from the independent balance ledger.

    Reads nanogpt-balance-ledger.jsonl (the *other*, every-3-min source) and
    accumulates the positive balance drops since the watchdog's last_ts (or
    since the current balance was seeded). This covers burn the watchdog
    missed while it was not running (a >2x-interval hole). Falls back to
    {0.0, None, False} when the ledger is absent or the provider is not a
    balance meter.
    """
    if not (provider_cost(provider) is not None and meter_is_balance(provider)):
        return {"covered_usd": 0.0, "covered_since": None, "gap": False}
    last_ts = pstate.get("last_ts")
    if last_ts is None:
        return {"covered_usd": 0.0, "covered_since": None, "gap": False}
    gap = float(now_ts) - float(last_ts) > LEDGER_INTERVAL_S * GAP_MULTIPLIER
    if not gap:
        return {"covered_usd": 0.0, "covered_since": None, "gap": False}
    covered = 0.0
    covered_since: Optional[float] = None
    try:
        with open(BALANCE_LEDGER_FILE, "r", encoding="utf-8") as fh:
            prev: Optional[float] = None
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                ts = _parse_ledger_ts(row.get("ts"))
                bal = row.get("usd_balance")
                if ts is None or bal is None or ts < float(last_ts):
                    continue
                bal = float(bal)
                if prev is not None and bal < prev:
                    covered += prev - bal
                    covered_since = covered_since if covered_since is not None else ts
                prev = bal
    except OSError:
        pass
    return {"covered_usd": round(covered, 6), "covered_since": covered_since, "gap": gap}


def seed_balance_cum(provider: Dict[str, Any], prior: Dict[str, Any]) -> float:
    """Retroactive cumulative for a balance meter's FIRST observation.

    The first time the watchdog sees a DECREASING balance meter it has no
    ``last_cost``, so per-tick delta would be 0 and cum would seed at 0 — the
    balance the watchdog has missed since the window started (or since it was
    last running) would never register, so WARN/STOP would stay unreachable
    even after $6+ overspent. Mirror the budget ledger: its trailing
    ``window_spent_usd`` is the authoritative sum of balance drops in the
    current weekly window. Seed ``cum`` from it so enforcement starts from the
    real already-burned amount, not from zero (OBJ-39/t_cce2a554).
    """
    if not (provider_cost(provider) is not None and meter_is_balance(provider)):
        return 0.0
    if prior.get("last_cost") is not None:
        return float(prior.get("cum_cost_usd", 0.0))
    try:
        with open(BALANCE_LEDGER_FILE, "r", encoding="utf-8") as fh:
            spend = 0.0
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                v = row.get("window_spent_usd")
                if v is not None:
                    spend = float(v)
            return round(spend, 6)
    except OSError:
        return float(prior.get("cum_cost_usd", 0.0))


def _parse_ledger_ts(raw) -> Optional[float]:
    """Epoch from the ledger's ISO or numeric timestamp; None if unparseable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw)
    s = s.rstrip("Z").replace("+00:00", "")
    if " " in s:
        s = s.replace(" ", "T")
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(s)
        return dt.replace(tzinfo=_dt.timezone.utc if dt.tzinfo is None else dt.tzinfo).timestamp()
    except (ValueError, TypeError):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None


def _analyze_provider_state(provider: Dict[str, Any], pstate: Dict[str, Any], cfg: Dict[str, Any], now_ts: float):
    """Run the analyzer and reset cumulative counters on an episode boundary."""
    tick_elapsed = max((now_ts - float(pstate.get("last_ts", now_ts))) / 60.0, 0.0) if pstate else 0.0
    res = analyze_provider(provider, pstate, cfg, now_ts, tick_elapsed)
    if pstate.get("burning") and not res["burning"]:
        res["cum_cost_usd"] = 0.0
        res.pop("last_pct", None)
        res.pop("last_cost", None)
    return res


def _persist_provider_observation(state: Dict[str, Any], prov: str, pstate: Dict[str, Any], res: Dict[str, Any], now_ts: float):
    """Persist the provider's observation into state and append the ledger entry."""
    nstate = dict(pstate)
    nstate.update({
        "provider": prov,
        "last_ts": now_ts,
        "burning": res["burning"],
    })
    if res["bottleneck_pct"] is not None:
        nstate["last_pct"] = res["bottleneck_pct"]
    if res["cost_now"] is not None:
        nstate["last_cost"] = res["cost_now"]
    nstate["cum_cost_usd"] = res["cum_cost_usd"]
    state[prov] = nstate
    append_ledger({
        "provider": prov,
        "burning": res["burning"],
        "window": res["bottleneck_window"],
        "window_pct": res["bottleneck_pct"],
        "cum_cost_usd": round(res["cum_cost_usd"], 6),
        "rate_usd_per_min": round(res["rate_usd_per_min"], 6),
        "tick_cost_usd": round(res["tick_cost_usd"], 6),
        "cost_meter": res["cost_now"],
    })


def _decide_provider_actions(state: Dict[str, Any], prov: str, pstate: Dict[str, Any], res: Dict[str, Any], cfg: Dict[str, Any], now_ts: float, alerts: List[str], active_warnings: Dict[str, Any]):
    """Apply warning and stop thresholds; return the tuples the caller expects."""
    cum = res["cum_cost_usd"]
    rate = res["rate_usd_per_min"]
    warn_rate = float(cfg.get("burn_rate_warn_usd_per_min", 0.05))
    warn_total = float(cfg.get("burn_total_warn_usd", 0.20))
    stop_total = float(cfg.get("burn_total_stop_usd", 1.00))
    # Coverage gap: the watchdog was not running for >2x its interval, so it
    # back-filled burn from the independent balance ledger. Surface it so the
    # hole is traceable even when the retro-cover didn't itself cross a
    # threshold. Ledger entry carries action=GAP for the audit trail.
    if res.get("gap"):
        alerted = f"BURN-COVERAGE-GAP {prov}: watchdog missed "
        alerted += (
            f"{res['gap_covered_usd']:.2f} during a >{LEDGER_INTERVAL_S // 60 * int(GAP_MULTIPLIER)}m hole; "
            f"recovered from balance ledger (cum ${cum:.2f})"
        )
        alerts.append(alerted)
        append_ledger({
            "provider": prov,
            "action": "GAP",
            "gap_covered_usd": round(res.get("gap_covered_usd", 0.0), 6),
            "cum_cost_usd": round(cum, 6),
        })
    # WARN also fires for a DECREASING balance meter, which never flips
    # `burning` yet burns real prepaid spend. A real cost meter present
    # (cost_now is not None) is sufficient to consider an accumulated WARN;
    # only the pure percent-delta ESTIMATE stays gated on `burning`.
    if res["burning"] or res["cost_now"] is not None:
        _maybe_provider_warn(prov, res, cum, rate, warn_rate, warn_total, stop_total, now_ts, alerts, active_warnings)
    _maybe_provider_stop(state, prov, pstate, res, cum, rate, stop_total, alerts)
    return alerts, active_warnings, state


def _maybe_provider_warn(prov, res, cum, rate, warn_rate, warn_total, stop_total, now_ts, alerts, active_warnings):
    """Emit a BURN-WARN alert and ledger entry when warn thresholds are crossed."""
    warn_hit = (cum >= warn_total) or (rate >= warn_rate and cum > 0)
    if not warn_hit or cum >= stop_total:
        return
    active_warnings[prov] = {
        "ts": int(now_ts),
        "cum_cost_usd": round(cum, 4),
        "rate_usd_per_min": round(rate, 4),
        "window": res["bottleneck_window"],
    }
    alerts.append(
        f"BURN-WARN {prov}: burning paid balance — "
        f"cumulative ${cum:.2f} (rate ${rate:.3f}/min, "
        f"window {res['bottleneck_window']} @ {res['bottleneck_pct']}%)"
    )
    append_ledger({
        "provider": prov,
        "action": "WARN",
        "cum_cost_usd": round(cum, 6),
        "rate_usd_per_min": round(rate, 6),
        "window": res["bottleneck_window"],
    })


def _maybe_provider_stop(state: Dict[str, Any], prov: str, pstate: Dict[str, Any], res: Dict[str, Any], cum: float, rate: float, stop_total: float, alerts: List[str]):
    """Write a STOP file and kill the daemon when cumulative burn crosses the stop threshold."""
    if cum < stop_total or pstate.get("stopped"):
        return
    evidence = (
        f"provider={prov} rate_usd_per_min={rate:.4f} "
        f"cumulative_usd={cum:.4f} window={res['bottleneck_window']}"
    )
    write_stop_file(
        f"burn-watchdog STOP for {prov} at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        + evidence + "\n(manual clear required)"
    )
    killed = kill_daemon()
    alerts.append(
        f"BURN-STOP {prov}: burned ${cum:.2f} >= stop ${stop_total:.2f} "
        f"(rate ${rate:.3f}/min, window {res['bottleneck_window']} "
        f"@ {res['bottleneck_pct']}%) — wrote STOP file, "
        + (f"killed daemon (pid {killed})" if killed else "daemon not running")
    )
    append_ledger({
        "provider": prov,
        "action": "STOP",
        "cum_cost_usd": round(cum, 6),
        "rate_usd_per_min": round(rate, 6),
        "window": res["bottleneck_window"],
    })
    state[prov]["stopped"] = True

# Note: _handle_provider keeps each function well under 50 lines.


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generic spend-burn watchdog (gate-snapshot only)."
    )
    parser.add_argument(
        "--gate", default=None,
        help="Path to quota-gate.py (default: alongside this script).",
    )
    parser.add_argument(
        "--now", type=float, default=None,
        help="Override 'now' (epoch) for deterministic tests.",
    )
    args = parser.parse_args(argv)
    alerts = run_tick(gate_path=args.gate, now=args.now)
    for line in alerts:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
