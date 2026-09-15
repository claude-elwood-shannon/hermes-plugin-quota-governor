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
STATE_DIR = os.environ.get(
    "BURN_STATE_DIR", os.path.expanduser("~/.hermes/quota-governor")
)
LEDGER_FILE = os.path.join(STATE_DIR, "burn-ledger.jsonl")
STATE_FILE = os.path.join(STATE_DIR, "burn-state.json")
WARN_FILE = os.path.join(STATE_DIR, "burn-warnings.json")
CONFIG_FILE = os.path.join(STATE_DIR, "burn-watchdog.json")

STOP_FILE = os.path.join(HERMES_HOME, "quota-governor", "STOP")
PIDFILE = os.path.join(HERMES_HOME, "quota-governor-daemon.pid")

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

    # --- Estimate USD cost accumulated over this tick ---
    # Priority: real cost meter (calibrated), else percent-delta x window_usd_cap.
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

    cum = float(prior.get("cum_cost_usd", 0.0))
    if burning:
        cum += tick_cost
    elif cost_now is None:
        # Not burning and no real meter — don't accumulate estimates.
        cum = float(prior.get("cum_cost_usd", 0.0))

    # Derived burn rate = cost consumed over the last tick interval (USD/min).
    # Needs at least one prior observation (tick_elapsed reflects the gap);
    # on the very first observation we cannot yet derive a rate.
    if burning and tick_elapsed > 0 and prior.get("last_ts"):
        rate = tick_cost / max(tick_elapsed, 0.001)
    else:
        rate = 0.0

    return {
        "burning": burning,
        "rate_usd_per_min": rate,
        "cum_cost_usd": cum,
        "bottleneck_window": bottleneck["name"] if bottleneck else None,
        "bottleneck_pct": bottleneck["pct"] if bottleneck else None,
        "cost_now": cost_now,
        "tick_cost_usd": tick_cost,
    }


def run_tick(gate_path: Optional[str] = None, now: Optional[float] = None) -> List[str]:
    """Full watchdog tick. Returns a list of alert lines to print to stdout.

    Silent (empty list) when nothing needs reporting. Pure function over
    disk state + gate snapshot, so it is unit-testable without a provider.
    """
    now_t = now if now is not None else time.time()
    config = load_config()
    state = _load_state()
    snapshot = load_snapshot(gate_path)
    alerts: List[str] = []
    active_warnings: Dict[str, Any] = {}

    # Window bookkeeping keyed by provider -> window -> last values.
    # We derive the per-provider prior (across its windows) into one blob.
    for provider in providers_from_snapshot(snapshot or {}):
        prov = provider.get("provider")
        if not prov:
            continue
        cfg = _prov_cfg(config, prov)
        pstate = state.get(prov, {})
        # Prior state holds the provider's last seen bottleneck pct/cost + cum.
        if "last_ts" in pstate and pstate["last_ts"]:
            tick_elapsed = max((now_t - float(pstate["last_ts"])) / 60.0, 0.0)
        else:
            tick_elapsed = 0.0

        res = analyze_provider(provider, pstate, cfg, now_t, tick_elapsed)

        # Episode boundary: a provider that WAS burning and now is not has
        # recovered (window reset). Close the burn episode — reset cumulative
        # cost and window baseline so the next episode starts from zero. The
        # STOP file, if any, is NEVER auto-cleared (manual only).
        was_burning = bool(pstate.get("burning"))
        if was_burning and not res["burning"]:
            res["cum_cost_usd"] = 0.0
            res.pop("last_pct", None)
            res.pop("last_cost", None)

        # Persist observation for next tick.
        nstate = dict(pstate)
        nstate["provider"] = prov
        nstate["last_ts"] = now_t
        nstate["burning"] = res["burning"]
        if res["bottleneck_pct"] is not None:
            nstate["last_pct"] = res["bottleneck_pct"]
        if res["cost_now"] is not None:
            nstate["last_cost"] = res["cost_now"]
        nstate["cum_cost_usd"] = res["cum_cost_usd"]
        state[prov] = nstate

        # Record every observation in the ledger (full history).
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

        if not cfg.get("enabled"):
            # Observe-only provider (or unknown): no enforcement.
            continue

        # --- WARN ---
        cum = res["cum_cost_usd"]
        rate = res["rate_usd_per_min"]
        warn_rate = float(cfg.get("burn_rate_warn_usd_per_min", 0.05))
        warn_total = float(cfg.get("burn_total_warn_usd", 0.20))
        stop_total = float(cfg.get("burn_total_stop_usd", 1.00))

        if not res["burning"]:
            # Not burning → clear any warning for this provider.
            continue

        warn_hit = (cum >= warn_total) or (rate >= warn_rate and cum > 0)
        if warn_hit and cum < stop_total:
            msg = (
                f"BURN-WARN {prov}: burning paid balance — "
                f"cumulative ${cum:.2f} (rate ${rate:.3f}/min, "
                f"window {res['bottleneck_window']} @ {res['bottleneck_pct']}%)"
            )
            active_warnings[prov] = {
                "ts": int(now_t),
                "cum_cost_usd": round(cum, 4),
                "rate_usd_per_min": round(rate, 4),
                "window": res["bottleneck_window"],
            }
            alerts.append(msg)
            append_ledger({
                "provider": prov,
                "action": "WARN",
                "cum_cost_usd": round(cum, 6),
                "rate_usd_per_min": round(rate, 6),
                "window": res["bottleneck_window"],
            })

        # --- STOP ---
        if cum >= stop_total:
            if not pstate.get("stopped"):
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
                msg = (
                    f"BURN-STOP {prov}: burned ${cum:.2f} >= stop ${stop_total:.2f} "
                    f"(rate ${rate:.3f}/min, window {res['bottleneck_window']} "
                    f"@ {res['bottleneck_pct']}%) — wrote STOP file, "
                    + (f"killed daemon (pid {killed})" if killed else "daemon not running")
                )
                alerts.append(msg)
                append_ledger({
                    "provider": prov,
                    "action": "STOP",
                    "cum_cost_usd": round(cum, 6),
                    "rate_usd_per_min": round(rate, 6),
                    "window": res["bottleneck_window"],
                })
            # Mark stopped regardless so we don't re-fire until the episode
            # is cleared (manual STOP-file removal or burn recovery).
            nstate["stopped"] = True

    save_state(state)
    # Only write the warnings file when there ARE active warnings — empty
    # implies no burn and must not disturb the gate/task-creator.
    if active_warnings:
        save_warnings(active_warnings)
    return alerts


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
