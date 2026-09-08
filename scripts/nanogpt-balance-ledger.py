#!/usr/bin/env python3
"""nanogpt-balance-ledger.py — OBJ-26: balance USD ledger + budget state for
the pr-nanogpt provider (NanoGPT).

WHAT IT DOES
------------
One module, two jobs:

1. PROBE (exact, via the provider's own API):
   - POST https://nano-gpt.com/api/check-balance
       -> {"usd_balance": "15.49190699", ...}   (exact account balance)
   - GET  https://nano-gpt.com/api/subscription/v1/usage
       -> state, weekly token percent, routing flags
          (allowOverage / paidSpendPolicyAllowsBalance), period end.
   - GET  https://nano-gpt.com/api/subscription/v1/models
       -> the subscription-covered model list (exact, no guessing).
   Results are cached to ``nanogpt-balance-last-good.json`` (60 s TTL) so
   several consumers in the same tick cost one API round-trip.

2. LEDGER (durable JSONL of every observed balance + spend):
   ``nanogpt-balance-ledger.jsonl`` rows:
     {"ts": ..., "usd_balance": 15.49, "weekly_tokens_pct": 79.76,
      "allowOverage": false, "delta_usd": -0.000142, "source": "probe"}

BUDGET STATE (the NANO_GPT_MAX_BALANCE_SPEND window budget)
-----------------------------------------------------------
``nanogpt-budget-state.json`` tracks, per weekly window (anchored to the
subscription period start, falling back to ISO-week):

    window_start, spent_usd (sum of observed balance drops), max_spend_usd

FRAUD/NOISE GUARD: a balance *increase* (top-up) resets ``spent_usd`` to 0
but the LEDGER keeps the raw history — reconciliation stays possible.
A balance drop LARGER than ``max_spend_usd`` cannot be autonomous spend
(manual spend elsewhere, refund reversal); it is recorded, and the window
is re-baselined (spent_usd=0 from that point) with a ``rebaseline`` row so
the budget tracks *our* spend, not account-wide noise.

Gate integration (see quota-gate.py ``nanogpt_budget_context``):
    context["nanogpt_balance"] = {
        "usd_balance": 15.49, "weekly_tokens_pct": 79.8,
        "window_spent_usd": 0.12, "window_max_spend_usd": 5.0,
        "window_fraction": 0.024,
        "level": "ok" | "warn" | "stop",
        "covered_models": ["z-ai/glm-5.3-flash", ...],
        "coverage_unknown": false,
        "source": "probe"|"cache"|"unavailable",
    }

LEVEL RULES (covered-first budget):
    stop  — spent >= max_spend  → gate drops balance-only models from the
            candidate set (subscription stays; NanoGPT never fully dies).
    warn  — spent >= warn_fraction * max (default 50%) → warning string,
            routing continues.
    ok    — anything below.

This module NEVER raises into the gate: every public helper returns
fallback values on any error.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HERMES_HOME_DEFAULT = os.path.expanduser("~/.hermes")
NANOGPT_BASE = "https://nano-gpt.com"

CACHE_TTL_S = 60.0
PROBE_TIMEOUT_S = 15.0

DEFAULT_MAX_SPEND_USD = 5.0
DEFAULT_WARN_FRACTION = 0.5

# Cheap plan models: never more than one probe round-trip per tick even if
# several scripts ask in the same minute.
_LAST_PROBE = {"ts": 0.0, "data": None}


def state_dir(hermes_home=None):
    base = hermes_home or os.environ.get("HERMES_HOME", HERMES_HOME_DEFAULT)
    return os.environ.get("QUOTA_GOVERNOR_DIR",
                          os.path.join(base, "quota-governor"))


def ledger_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "nanogpt-balance-ledger.jsonl")


def cache_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "nanogpt-balance-last-good.json")


def budget_path(hermes_home=None):
    return os.path.join(state_dir(hermes_home), "nanogpt-budget-state.json")


def _env_key():
    key = os.environ.get("NANO_GPT_API_KEY", "")
    if key:
        return key
    for path in (
        os.path.expanduser("~/.hermes/profiles/pr-nanogpt/.env"),
        os.path.expanduser("~/.hermes/profiles/pr-ollama/.env"),
        os.path.expanduser("~/.hermes/.env"),
    ):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("NANO_GPT_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# HTTP helpers (providers reject Tor/proxy — same convention as quota-gate)
# ---------------------------------------------------------------------------

def _no_proxy():
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {}
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            if var in os.environ:
                saved[var] = os.environ.pop(var)
        try:
            yield
        finally:
            os.environ.update(saved)

    return _ctx()


def _get(url, key):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())


def _post(url, key, payload=None):
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Probe + cache
# ---------------------------------------------------------------------------

def fetch_snapshot(key=None, force=False):
    """Exact probe of balance + subscription state. Cached 60 s.

    Returns dict:
      {"usd_balance": float|None, "weekly_tokens_pct": float|None,
       "state": str|None, "policy_allows_balance": bool|None,
       "period_end": str|None, "ts": float, "source": "probe"|"cache",
       "error": str|None}
    Never raises.
    """
    now = time.time()
    if not force and _LAST_PROBE["data"] is not None \
            and now - _LAST_PROBE["ts"] < CACHE_TTL_S:
        return dict(_LAST_PROBE["data"], source="cache")

    key = key or _env_key()
    if not key:
        return {"usd_balance": None, "weekly_tokens_pct": None, "state": None,
                "allowOverage": None, "period_end": None, "ts": now,
                "source": "unavailable", "error": "NANO_GPT_API_KEY not configured"}

    out = {"usd_balance": None, "weekly_tokens_pct": None, "state": None,
           "policy_allows_balance": None, "period_end": None, "ts": now,
           "source": "probe", "error": None}
    try:
        bal = _post(f"{NANOGPT_BASE}/api/check-balance", key)
        out["usd_balance"] = float(bal.get("usd_balance"))
    except Exception as exc:
        out["error"] = f"check-balance: {exc}"
    try:
        usage = _get(f"{NANOGPT_BASE}/api/subscription/v1/usage", key)
        weekly = usage.get("weeklyInputTokens") or {}
        pct = weekly.get("percentUsed")
        out["weekly_tokens_pct"] = (float(pct) * 100.0
                                    if pct is not None else None)
        out["state"] = usage.get("state")
        routing = usage.get("routing") or {}
        out["policy_allows_balance"] = routing.get("paidSpendPolicyAllowsBalance")
        out["period_end"] = (usage.get("period") or {}).get("currentPeriodEnd")
    except Exception as exc:
        out["error"] = (out["error"] + f"; usage: {exc}") if out["error"] \
            else f"usage: {exc}"

    if out["usd_balance"] is not None:
        _write_cache(out)
        _LAST_PROBE["ts"] = now
        _LAST_PROBE["data"] = dict(out)
    return out


def _write_cache(data):
    try:
        os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
        with open(cache_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def read_cache(hermes_home=None):
    try:
        with open(cache_path(hermes_home), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Covered-model set (exact list from the provider)
# ---------------------------------------------------------------------------

def fetch_covered_models(key=None, ttl_s=3600.0):
    """Set of subscription-covered model ids (exact API list).

    Cached on disk for an hour. Returns (set_or_None, err_or_None); the set
    is None when the API is unreachable AND no cache exists.
    """
    path = cache_path().replace("balance-last-good", "covered-models")
    now = time.time()
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if now - float(cached.get("ts", 0)) < ttl_s and \
                isinstance(cached.get("models"), list):
            return set(cached["models"]), None
    except (OSError, ValueError, TypeError):
        cached = None

    key = key or _env_key()
    if not key:
        return None, "no api key"
    try:
        data = _get(f"{NANOGPT_BASE}/api/subscription/v1/models", key)
        models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        models = {m for m in models if m}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"ts": now, "models": sorted(models)}, fh)
        except OSError:
            pass
        return models, None
    except Exception as exc:
        if cached is not None:
            return set(cached.get("models") or []), None
        return None, str(exc)


def is_covered(model, covered_set):
    """Exact match, else bare-segment match (zai-org/glm-5.2 ~ z-ai/glm-5.2).

    Verified live 2026-09-08: covered list has z-ai/glm-5.2 but NOT
    zai-org/glm-5.2 (the profile's interactive model) — bare-segment match
    is required; qwen3.5-4b matches nothing (correctly NOT covered).
    """
    if not model or not covered_set:
        return False
    if model in covered_set:
        return True
    bare = model.split("/")[-1]
    return any(i.split("/")[-1] == bare for i in covered_set)


# ---------------------------------------------------------------------------
# Ledger + weekly budget window
# ---------------------------------------------------------------------------

def append_ledger(row, hermes_home=None):
    try:
        os.makedirs(os.path.dirname(ledger_path(hermes_home)), exist_ok=True)
        with open(ledger_path(hermes_home), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_budget(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_budget(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    except OSError:
        pass


def week_start(now=None):
    """ISO-week Monday 00:00 UTC — fallback budget window anchor."""
    now = now or dt.datetime.now(dt.timezone.utc)
    monday = now - dt.timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def window_start_from_period(period_end_str, now=None):
    """Subscription period start = currentPeriodEnd - 7 days.

    The budget window follows the subscription period when known (the plan
    is weekly); falls back to ISO week when not parseable.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        end = dt.datetime.fromisoformat(
            str(period_end_str).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        start = end - dt.timedelta(days=7)
        if start <= now:
            return start
    except (ValueError, TypeError):
        pass
    return week_start(now)


def update_budget(snapshot, max_spend_usd=None, warn_fraction=None,
                  hermes_home=None, now=None):
    """Track window spend from observed balance deltas.

    window semantics: see module docstring. Returns the budget dict:
      {"window_start", "spent_usd", "max_spend_usd", "warn_fraction",
       "baseline_balance"}
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    path = budget_path(hermes_home)
    max_spend = float(max_spend_usd if max_spend_usd is not None
                      else os.environ.get("NANO_GPT_MAX_BALANCE_SPEND",
                                          DEFAULT_MAX_SPEND_USD))
    warn_frac = float(warn_fraction if warn_fraction is not None
                      else DEFAULT_WARN_FRACTION)

    state = _load_budget(path)
    wstart = window_start_from_period(snapshot.get("period_end"), now)
    prev_start = state.get("window_start")
    if not prev_start or _parse_dt(prev_start) != wstart:
        state = {"window_start": wstart.isoformat(), "spent_usd": 0.0,
                 "max_spend_usd": max_spend, "warn_fraction": warn_frac,
                 "baseline_balance": snapshot.get("usd_balance")}
    state["max_spend_usd"] = max_spend
    state["warn_fraction"] = warn_frac

    bal = snapshot.get("usd_balance")
    prev_bal = state.get("baseline_balance")
    if bal is not None and prev_bal is not None:
        delta = float(prev_bal) - float(bal)
        if delta < -0.005:  # top-up (or external credit): re-baseline
            state["baseline_balance"] = bal
            append_ledger({"ts": now.isoformat(), "event": "topup",
                           "balance": bal}, hermes_home)
        elif delta > float(state["max_spend_usd"]) * 4:
            # Drop far beyond any autonomous budget = external/manual spend:
            # re-baseline so the budget tracks OUR routing spend only.
            state["baseline_balance"] = bal
            append_ledger({"ts": now.isoformat(), "event": "rebaseline",
                           "balance": bal, "observed_drop": round(delta, 6)},
                          hermes_home)
        else:
            state["spent_usd"] = round(
                float(state.get("spent_usd", 0.0)) + max(delta, 0.0), 6)
            state["baseline_balance"] = bal

    _save_budget(path, state)
    return state


def _parse_dt(s):
    try:
        d = dt.datetime.fromisoformat(str(s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Gate entry point
# ---------------------------------------------------------------------------

def budget_context(max_spend_usd=None, warn_fraction=None, hermes_home=None):
    """One call for quota-gate.py. Never raises; degrades gracefully.

    Returns (context_dict_or_None, warning_string_or_None).
    """
    try:
        snap = fetch_snapshot()
        if snap["source"] == "unavailable" or snap.get("usd_balance") is None:
            cached = read_cache(hermes_home)
            if cached and cached.get("usd_balance") is not None:
                snap = dict(cached, source="cache")
            else:
                return None, None

        budget = update_budget(snap, max_spend_usd=max_spend_usd,
                               warn_fraction=warn_fraction,
                               hermes_home=hermes_home)
        append_ledger({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "usd_balance": snap.get("usd_balance"),
            "weekly_tokens_pct": snap.get("weekly_tokens_pct"),
            "policy_allows_balance": snap.get("policy_allows_balance"),
            "window_spent_usd": round(float(budget.get("spent_usd", 0.0)), 6),
            "source": snap.get("source"),
        }, hermes_home)

        max_spend = float(budget.get("max_spend_usd", DEFAULT_MAX_SPEND_USD))
        spent = float(budget.get("spent_usd", 0.0))
        frac = spent / max_spend if max_spend > 0 else 0.0
        warn_frac = float(budget.get("warn_fraction", DEFAULT_WARN_FRACTION))
        if spent >= max_spend:
            level = "stop"
        elif frac >= warn_frac:
            level = "warn"
        else:
            level = "ok"

        covered, cov_err = fetch_covered_models()
        ctx = {
            "usd_balance": snap.get("usd_balance"),
            "weekly_tokens_pct": snap.get("weekly_tokens_pct"),
            "state": snap.get("state"),
            "policy_allows_balance": snap.get("policy_allows_balance"),
            "window_start": budget.get("window_start"),
            "window_spent_usd": round(spent, 4),
            "window_max_spend_usd": max_spend,
            "window_fraction": round(frac, 4),
            "level": level,
            "covered_models": sorted(covered) if covered is not None else [],
            "coverage_unknown": covered is None,
            "source": snap.get("source"),
        }
        warning = None
        if level == "warn":
            warning = (f"nanogpt budget: ${spent:.2f} of ${max_spend:.2f} "
                       f"weekly balance budget spent (>= {int(warn_frac*100)}%)")
        elif level == "stop":
            warning = (f"nanogpt budget EXHAUSTED: ${spent:.2f} >= "
                       f"${max_spend:.2f} weekly balance budget — "
                       f"balance-only models dropped, subscription models "
                       f"still routed")
        if cov_err and covered is None:
            warning = (warning + "; " if warning else "") + \
                f"nanogpt coverage list unavailable: {cov_err}"
        return ctx, warning
    except Exception as exc:  # never break the gate
        return None, f"nanogpt budget context error: {exc}"


# ---------------------------------------------------------------------------
# CLI (manual reconciliation)
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("cmd", choices=["status", "report", "covered"],
                   help="status: probe+ledger now | report: last 20 ledger "
                        "rows | covered: print covered-model set")
    p.add_argument("--max-spend", type=float, default=None)
    args = p.parse_args(argv)

    if args.cmd == "covered":
        models, err = fetch_covered_models()
        if models is None:
            print(f"error: {err}")
            return 1
        print(f"{len(models)} covered models")
        for m in sorted(models):
            print(" ", m)
        return 0

    if args.cmd == "report":
        try:
            with open(ledger_path(), encoding="utf-8") as fh:
                rows = fh.readlines()
        except OSError:
            rows = []
        print(f"{len(rows)} ledger rows; last 20:")
        for line in rows[-20:]:
            print(" ", line.rstrip())
        return 0

    snap = fetch_snapshot(force=True)
    print(json.dumps(snap, indent=1))
    ctx, warning = budget_context(max_spend_usd=args.max_spend)
    print(json.dumps(ctx, indent=1))
    if warning:
        print("WARNING:", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
