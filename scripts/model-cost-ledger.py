#!/usr/bin/env python3
"""model-cost-ledger.py — per-model cost ledger for OpenCode Go 5h windows
(MULTI-PROV-09, t_5bdd7cfa).

WHAT IT DOES
------------
Scans every Hermes profile's ``state.db`` table ``session_model_usage`` and
appends DELTA rows — restricted to provider ``opencode-go`` — to a durable
JSONL ledger:

    ~/.hermes/quota-governor/model-cost-ledger.jsonl

Each row covers one session×model×task usage delta since the last sync,
attributed to the OpenCode Go rolling-5h window it fell in.  Window
boundaries are ANCHORED to the live ``rolling.resetsAt`` returned by
``GET /zen/go/v1/usage`` — windows roll from their own start and are NOT
fixed clock hours (see pr-ollama/docs/opencode-go-reset-semantics.md) — so
the ledger reproduces the console's per-model consumption table.

WHY NOT the per-request ``cost`` field (calibrated t_47640f18)
--------------------------------------------------------------
The API's per-response ``cost`` field reports REAL USD charged from prepaid
Zen balance ONLY while a window is exhausted (status "rate-limited"); under
subscription coverage it is "0" because consumption is paid from the
included USD allowance, not balance.  Moreover Hermes does NOT persist that
field anywhere: ``session_model_usage.estimated_cost_usd`` is 0.0 with
cost_source="none" across all profile DBs (verified live Sep 7 2026).  The
only per-model, per-call usage source available locally is the token
aggregation in state.db.  So this ledger estimates consumption from tokens ×
published prices — which is exactly how OpenCode Go meters a window's USD
budget — and it covers BOTH phases (subscription share AND balance burn).

ESTIMATION RULE
---------------
    cost_usd = input/1e6*price_in + (output+reasoning)/1e6*price_out
             + cache_read/1e6*price_cache_read

cache_write is EXCLUDED (no published cache-write price in the docs/go
2026-09-07 catalog; including it would inflate estimates).  DeepSeek
models double in peak hours (mono-fr 01:00-04:00 & 06:00-10:00 UTC) and
the estimate applies the x2 when the row's timestamp falls in peak.
Prices: MODEL_PRICES below, overridable via
``~/.hermes/quota-governor/model-cost.json`` (shape: {"prices": {model:
[in, out, cache]}, "window_usd": float, "warn_fraction": float}) so the
formula can be recalibrated from console data without code changes.

LIMITATIONS (documented honestly)
---------------------------------
- Attribution granularity: state.db aggregates per session×model; a sync
  can only attribute the DELTA since the previous sync (default cadence:
  every 30 min via the quota-gate cron, or on demand).  Sub-window bursts
  between syncs land in the window of the newest activity.
- If the cursor file is deleted while the ledger remains, totals would
  double-count: delete BOTH together to re-baseline.
- cost>0 (real balance burn) and cost==0 (subscription-covered) are NOT
  distinguishable retroactively here; both phases are metered by the same
  token-price estimate.  For the balance phase the API cost field remains
  the exact ground truth whenever a probe is run manually.

USAGE
-----
    python3 model-cost-ledger.py sync            # append deltas (cron-safe,
                                                # prints rows appended, silent-friendly)
    python3 model-cost-ledger.py report          # current 5h window per model
    python3 model-cost-ledger.py report --json   # machine-readable
    python3 model-cost-ledger.py report --last 24h

The gate (quota-gate.py) calls sync_model_cost_ledger_if_due() +
model_window_warnings() from this module; both are best-effort and never
raise into the gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERMES_HOME_DEFAULT = os.path.expanduser("~/.hermes")


def state_dir(hermes_home=None):
    base = hermes_home or HERMES_HOME_DEFAULT
    return os.environ.get("QUOTA_GOVERNOR_DIR",
                          os.path.join(base, "quota-governor"))


def ledger_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "model-cost-ledger.jsonl")


def cursor_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "model-cost-ledger.cursor.json")


def config_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "model-cost.json")


WINDOW_SECONDS = 5 * 3600  # rolling 5h window
SYNC_MIN_INTERVAL = 20 * 60  # gate-triggered opportunistic sync: max 1 per 20m
DEFAULT_WINDOW_BUDGET_USD = 12.0  # live plan value, Sep 2026 console
DEFAULT_WARN_FRACTION = 0.5

# Published USD per-million prices for OpenCode Go (docs/go 2026-09-07
# catalog, t_9c61f54e).  Unknown models cost 0 (still ledgered: request
# and token counts are provider-side truth; extending prices retroactively
# matters via the raw token columns).
MODEL_PRICES = {
    # model:            (input, output, cache_read)
    "qwen3.8-flash":       (0.15, 0.47, 0.016),
    "glm-5.3-flash":       (0.15, 0.50, 0.03),
    "glm-5.2":             (1.40, 4.40, 0.26),
    "glm-5.3":             (1.40, 4.40, 0.26),
    "glm-5.1":             (1.40, 4.40, 0.26),
    "deepseek-v4-flash":   (0.22, 0.66, 0.007),  # base/off-peak; x2 at peak
    "kimi-k2.7-code":      (0.95, 4.00, 0.19),
    "minimax-m3":          (0.30, 1.20, 0.06),
    "hy3":                 (0.14, 0.58, 0.035),
    "mimo-v2.5":           (0.14, 0.28, 0.0028),
}

PEAK_AFFECTED = {"deepseek-v4-flash"}  # x2 mono-fr 01-04 & 06-10 UTC


def load_config(hermes_home=None):
    cfg = {
        "prices": MODEL_PRICES,
        "window_usd": DEFAULT_WINDOW_BUDGET_USD,
        "warn_fraction": DEFAULT_WARN_FRACTION,
    }
    try:
        with open(config_path(hermes_home), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw.get("prices"), dict):
            merged = dict(MODEL_PRICES)
            for m, p in raw["prices"].items():
                if isinstance(p, (list, tuple)) and len(p) == 3:
                    a, b, c = (float(x) for x in p)
                    merged[m] = (a, b, c)
            cfg["prices"] = merged
        if isinstance(raw.get("window_usd"), (int, float)) and raw["window_usd"] > 0:
            cfg["window_usd"] = float(raw["window_usd"])
        if isinstance(raw.get("warn_fraction"), (int, float)) and 0 < raw["warn_fraction"] <= 1:
            cfg["warn_fraction"] = float(raw["warn_fraction"])
    except (OSError, ValueError):
        pass
    return cfg


def is_peak(utc_dt):
    """Peak pricing hours (mirrors quota-gate.py MULTI-PROV-07)."""
    if utc_dt.weekday() >= 5:
        return False
    return (1 <= utc_dt.hour < 4) or (6 <= utc_dt.hour < 10)


def estimate_cost(model, input_tokens, output_tokens, cache_read,
                  reasoning_tokens=0, at_ts=None, prices=None):
    """USD estimate for one usage delta (see ESTIMATION RULE above)."""
    prices = prices if prices is not None else MODEL_PRICES
    p = prices.get(model)
    if not p:
        return 0.0
    pin, pout, pcache = p
    if at_ts is not None and model in PEAK_AFFECTED:
        if is_peak(dt.datetime.utcfromtimestamp(at_ts)):
            pin, pout, pcache = pin * 2, pout * 2, pcache * 2
    return (input_tokens / 1e6 * pin
            + (output_tokens + reasoning_tokens) / 1e6 * pout
            + cache_read / 1e6 * pcache)


# ---------------------------------------------------------------------------
# Window anchoring
# ---------------------------------------------------------------------------

def iso_to_epoch(s):
    """ISO timestamp (possibly Z-suffixed) or epoch number -> epoch seconds.

    state.db stores last_seen as ISO strings in Hermes but numeric epoch in
    some builds/profiles (verified live Sep 7 2026) — accept both, None on
    anything unparseable.
    """
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def anchor_from_resets_at(s, now=None):
    """Window anchor (= resetsAt - 5h) from rolling.resetsAt. None if untrustworthy.

    resetsAt is ALWAYS the END of the current rolling window — both while
    status is ok and while rate-limited (verified live Sep 7 2026: the
    exhausted window kept resetsAt pointing at its pending reset until the
    flip, then it jumped +5h; see pr-ollama/docs/opencode-go-reset-semantics.md).
    A slightly-past resetsAt (stale cache, mid-reset flip) is still a valid
    anchor; events after it simply bucket into the NEXT window, which is
    correct. A resetsAt far in the past or >5h+margin in the future is
    garbage or a different window — caller falls back.
    """
    reset = iso_to_epoch(s)
    if reset is None:
        return None
    now = now if now is not None else time.time()
    remaining = reset - now
    if remaining > WINDOW_SECONDS + 120 or remaining < -6 * 3600:
        return None
    return reset - WINDOW_SECONDS


def window_key(ts_epoch, anchor=None):
    """Deterministic ISO start-of-window for an event timestamp (UTC).

    With anchor: windows are [anchor + k*5h).  Without anchor: 5h floors
    from the epoch (offline fallback — internally consistent, possibly
    rotated relative to the provider's actual windows).
    """
    if anchor:
        k = int((ts_epoch - anchor) // WINDOW_SECONDS)
        start = anchor + k * WINDOW_SECONDS
    else:
        start = (int(ts_epoch) // WINDOW_SECONDS) * WINDOW_SECONDS
    return dt.datetime.utcfromtimestamp(start).strftime("%Y-%m-%dT%H:%MZ")


# ---------------------------------------------------------------------------
# State-DB scan
# ---------------------------------------------------------------------------

def scan_opencode_go_usage(hermes_home=None):
    """List of session_model_usage rows (dicts) with provider opencode-go,
    scanned read-only from every profile state.db.  Never raises."""
    base = hermes_home or HERMES_HOME_DEFAULT
    profiles_dir = os.path.join(base, "profiles")
    rows = []
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        return rows
    for name in names:
        path = os.path.join(profiles_dir, name, "state.db")
        if not os.path.exists(path):
            continue
        try:
            db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        except sqlite3.Error:
            continue
        try:
            db.row_factory = sqlite3.Row
            cur = db.execute(
                "SELECT session_id, model, task, api_call_count, input_tokens,"
                " output_tokens, cache_read_tokens, reasoning_tokens,"
                " last_seen FROM session_model_usage"
                " WHERE billing_provider = 'opencode-go'"
            )
            for r in cur:
                d = dict(r)
                d["profile"] = name
                rows.append(d)
        except sqlite3.Error:
            pass
        finally:
            db.close()
    return rows


# ---------------------------------------------------------------------------
# Ledger sync (delta accumulator)
# ---------------------------------------------------------------------------

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def sync_ledger(hermes_home=None, now=None):
    """Append usage DELTAS since the cursor to the ledger.

    Returns number of rows appended (0 = nothing new — cron-silent safe).
    Cursor format: {"<session_id>|<model>|<task>": {"calls": int,
    "in": int, "out": int, "cache": int, "reason": int, "ts": float}}.
    Negative deltas (DB reset/rewind) are clamped to zero and the cursor is
    realigned, so the ledger never contains negative consumption.
    """
    now = now if now is not None else time.time()
    cfg = load_config(hermes_home)
    anchor, _a_src = resolve_anchor(hermes_home, now)
    cur_path = cursor_path(hermes_home)
    cursor = _load_json(cur_path, {})
    appended = 0
    out = []
    for row in scan_opencode_go_usage(hermes_home):
        ts = iso_to_epoch(row.get("last_seen"))
        if not ts:
            continue
        key = "%s|%s|%s" % (row["session_id"], row["model"], row.get("task") or "")
        prev = cursor.get(key) or {}
        tot_calls = row.get("api_call_count") or 0
        tot_in = row.get("input_tokens") or 0
        tot_out = row.get("output_tokens") or 0
        tot_cache = row.get("cache_read_tokens") or 0
        tot_reason = row.get("reasoning_tokens") or 0
        d_calls = max(tot_calls - (prev.get("calls") or 0), 0)
        d_in = max(tot_in - (prev.get("in") or 0), 0)
        d_out = max(tot_out - (prev.get("out") or 0), 0)
        d_cache = max(tot_cache - (prev.get("cache") or 0), 0)
        d_reason = max(tot_reason - (prev.get("reason") or 0), 0)
        cursor[key] = {"calls": tot_calls, "in": tot_in, "out": tot_out,
                       "cache": tot_cache, "reason": tot_reason, "ts": ts}
        if not (d_calls or d_in or d_out or d_cache or d_reason):
            continue
        cost = estimate_cost(row["model"], d_in, d_out, d_cache, d_reason,
                             at_ts=ts, prices=cfg["prices"])
        out.append({
            "ts": round(ts, 3),
            "window": window_key(ts, anchor),
            "profile": row["profile"],
            "model": row["model"],
            "cost": round(cost, 6),
            "request_count": d_calls,
            "tokens": {"in": d_in, "out": d_out + d_reason, "cache_read": d_cache},
            "session_id": row["session_id"],
            "task": row.get("task") or "",
        })
        appended += 1
    if out:
        os.makedirs(state_dir(hermes_home), exist_ok=True)
        with open(ledger_path(hermes_home), "a", encoding="utf-8") as fh:
            for r in out:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    _save_json(cur_path, cursor)
    return appended


# ---------------------------------------------------------------------------
# Anchor resolution (best-effort, never raises)
# ---------------------------------------------------------------------------

def resolve_anchor(hermes_home=None, now=None):
    """(anchor, source) for window bucketing.

    Tries the live usage endpoint via quota-gate's query helper (same dir);
    falls back to the last-good cache resetsAt the gate maintains, then to
    epoch-floor windows (source="epoch-fallback", anchor=None).
    """
    now = now if now is not None else time.time()
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for gate_name in ("quota-gate.py",):
        gate_path = os.path.join(here, gate_name)
        if not os.path.exists(gate_path):
            continue
        spec = importlib.util.spec_from_file_location("_qg_ledger", gate_path)
        if spec is None or spec.loader is None:
            break
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            data = mod.query_opencode_go()
            src = "cached-last-good" if data.get("_cached") else "live"
            if src == "live":
                anchor = anchor_from_resets_at(data.get("rolling_resets_at"), now)
                if anchor:
                    return anchor, src
        except Exception:
            pass
        break
    # last-good cache written by the gate itself (no network)
    try:
        cached = _load_json(os.path.join(state_dir(hermes_home),
                                         "opencode_go-last-good.json"), {})
        anchor = anchor_from_resets_at(cached.get("rolling_resets_at"), now)
        if anchor:
            return anchor, "last-good-cache"
    except Exception:
        pass
    return None, "epoch-fallback"


# ---------------------------------------------------------------------------
# Summaries / warnings (consumed by report CLI and quota-gate.py)
# ---------------------------------------------------------------------------

def load_ledger(hermes_home=None):
    rows = []
    try:
        with open(ledger_path(hermes_home), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def summarize(rows, hermes_home=None, now=None):
    """{window: {model: {cost_usd, requests, tokens_in/out/cache, profiles}}}."""
    cfg = load_config(hermes_home)
    out = {}
    for r in rows:
        w = r.get("window", "?")
        m = r.get("model", "?")
        slot = out.setdefault(w, {}).setdefault(m, {
            "cost_usd": 0.0, "requests": 0, "in": 0, "out": 0, "cache": 0,
            "profiles": set(),
        })
        slot["cost_usd"] += r.get("cost", 0.0)
        slot["requests"] += r.get("request_count", 0)
        tok = r.get("tokens") or {}
        slot["in"] += tok.get("in", 0)
        slot["out"] += tok.get("out", 0)
        slot["cache"] += tok.get("cache_read", 0)
        slot["profiles"].add(r.get("profile", ""))
    for w, models in out.items():
        for m, s in models.items():
            s["window_fraction"] = round(s["cost_usd"] / cfg["window_usd"], 4) if cfg["window_usd"] else None
            s["cost_usd"] = round(s["cost_usd"], 4)
            s["profiles"] = sorted(s["profiles"])
    return out, cfg


def model_window_warnings(hermes_home=None, now=None, reset_at=None):
    """Warning strings for any model > warn_fraction of the current window.

    Used by quota-gate.py per tick.  reset_at: the live rolling_resets_at
    (the gate already queried it) so no extra API call is needed there.
    Returns [] on ANY error — observability must never break the gate.
    """
    try:
        now = now if now is not None else time.time()
        if reset_at:
            anchor = anchor_from_resets_at(reset_at, now)
            src = "live" if anchor else "none"
        else:
            anchor, src = resolve_anchor(hermes_home, now)
        current = window_key(now, anchor)
        rows = [r for r in load_ledger(hermes_home) if r.get("window") == current]
        summary, cfg = summarize(rows, hermes_home, now)
        models = summary.get(current, {})
        warns = []
        for m, s in sorted(models.items(), key=lambda kv: -kv[1]["cost_usd"]):
            frac = s["cost_usd"] / cfg["window_usd"] if cfg["window_usd"] else 0
            if frac >= cfg["warn_fraction"]:
                warns.append(
                    "WARNING: %s consumed %.0f%% of the OpenCode Go 5h window"
                    " ($%.2f of $%.2f budget, %d requests, window %s)"
                    % (m, frac * 100, s["cost_usd"], cfg["window_usd"],
                       s["requests"], current))
        return warns
    except Exception:
        return []


def current_window_shares(hermes_home=None, now=None, reset_at=None):
    """{window, budget, per-model shares} for the gate context. {} on error."""
    try:
        now = now if now is not None else time.time()
        if reset_at:
            anchor = anchor_from_resets_at(reset_at, now)
        else:
            anchor, _ = resolve_anchor(hermes_home, now)
        current = window_key(now, anchor)
        summary, cfg = summarize(load_ledger(hermes_home), hermes_home, now)
        return {
            "window": current,
            "window_budget_usd": cfg["window_usd"],
            "warn_fraction": cfg["warn_fraction"],
            "models": summary.get(current, {}),
            "note": ("Estimated from token deltas x published prices "
                     "(Hermes does not persist the API cost field; see "
                     "model-cost-ledger.py docstring). Authoritative while "
                     "subscription-covered; upper bound while burning balance."),
        }
    except Exception:
        return {}


def sync_model_cost_ledger_if_due(hermes_home=None, now=None):
    """Opportunistic sync for the gate: at most one sync per SYNC_MIN_INTERVAL.

    Returns rows appended (0 when skipped/not-due/error).  Never raises.
    """
    try:
        now = now if now is not None else time.time()
        marker = os.path.join(state_dir(hermes_home), "model-cost-ledger.lastsync")
        try:
            last = os.path.getmtime(marker)
        except OSError:
            last = 0
        if now - last < SYNC_MIN_INTERVAL:
            return 0
        n = sync_ledger(hermes_home, now)
        os.makedirs(state_dir(hermes_home), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write(str(now))
        return n
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_report(as_json=False, since_hours=None, hermes_home=None):
    rows = load_ledger(hermes_home)
    now = time.time()
    anchor, src = resolve_anchor(hermes_home, now)
    current = window_key(now, anchor)
    if since_hours:
        cutoff = now - since_hours * 3600
        rows = [r for r in rows if r.get("ts", 0) >= cutoff]
    summary, cfg = summarize(rows, hermes_home, now)
    cur_models = summary.get(current, {})
    result = {
        "window": current,
        "window_budget_usd": cfg["window_usd"],
        "anchor_source": src,
        "models": {
            m: {"cost_usd": s["cost_usd"], "requests": s["requests"],
                "window_fraction": s.get("window_fraction"),
                "profiles": s["profiles"]}
            for m, s in sorted(cur_models.items(), key=lambda kv: -kv[1]["cost_usd"])
        },
        "history": {
            w: {m: {"cost_usd": s["cost_usd"], "requests": s["requests"]}
                for m, s in sorted(models.items(), key=lambda kv: -kv[1]["cost_usd"])}
            for w, models in sorted(summary.items()) if w != current
        },
        "ledger_file": ledger_path(hermes_home),
    }
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return result
    print("OpenCode Go per-model cost — window %s (budget $%.2f, anchor: %s)"
          % (current, cfg["window_usd"], src))
    print("ledger: %s" % result["ledger_file"])
    print("%-20s %10s %9s %9s %s" % ("model", "cost_usd", "%window", "requests", "profiles"))
    for m, s in result["models"].items():
        print("%-20s %10.4f %8.1f%% %9d %s"
              % (m, s["cost_usd"], 100 * (s["window_fraction"] or 0),
                 s["requests"], ",".join(s["profiles"])))
    if not result["models"]:
        print("(no ledger rows in the current window yet)")
    if result["history"]:
        print("\npast windows:")
        for w, models in result["history"].items():
            total = sum(s["cost_usd"] for s in models.values())
            print("  %s  $%.2f total — %s" % (w, total,
                  ", ".join("%s $%.2f" % (m, s["cost_usd"]) for m, s in models.items())))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenCode Go per-model cost ledger")
    ap.add_argument("command", choices=["sync", "report"])
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--last", metavar="Nh", help="restrict report to last N hours")
    ap.add_argument("--hermes-home", default=None, help="override ~/.hermes (tests)")
    args = ap.parse_args(argv)

    if args.command == "sync":
        print(sync_ledger(hermes_home=args.hermes_home))
        return 0
    hours = None
    if args.last:
        m = re.match(r"^(\d+(?:\.\d+)?)h$", args.last)
        if not m:
            ap.error("--last expects Nh (e.g. 24h)")
        hours = float(m.group(1))
    cmd_report(as_json=args.json, since_hours=hours, hermes_home=args.hermes_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
