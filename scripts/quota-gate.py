#!/usr/bin/env python3
"""quota-gate.py — pre-run script for the autonomous task creator cron job.

Queries all configured providers (Ollama Cloud, NanoGPT, OpenRouter) and
outputs a recommended profile with the most available quota.

Output (last line, JSON):
  {"wakeAgent": false}  — skip this tick, all providers exhausted
  {"wakeAgent": true, "context": {
      "providers": [...],
      "recommended_profile": "pr-...",
      "recommended_model": "...",
      "max_task_cost": "medium|small|tiny|micro|any",
      "max_workers": N,
      "warning": null
  }}

The JSON context is consumed by the autonomous-task-creator cron prompt.
The agent reads recommended_profile and recommended_model to decide which
profile to assign tasks to, instead of hardcoding pr-ollama.

Design reference: ~/.hermes/profiles/pr-ollama/docs/multi-provider-design.md
"""
import json
import os
import subprocess
import sys
import urllib.request

# Guardrail G1: only these profiles may receive auto-created tasks.
# The autonomous task creator must NEVER assign to any other profile.
ALLOWED_PROFILES = {"pr-ollama", "pr-nanogpt"}

# Try to import the plugin's providers module for query functions.
# If import fails, we fall back to inline implementations.
_PLUGIN_PATH = os.path.expanduser(
    "~/.hermes/profiles/pr-ollama/plugins/hermes-plugin-quota-governor"
)
try:
    sys.path.insert(0, _PLUGIN_PATH)
    import providers as _providers
    _HAVE_PLUGIN = True
except Exception:
    _HAVE_PLUGIN = False


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def get_env(key):
    """Read from environment or .env file."""
    val = os.environ.get(key)
    if val:
        return val
    for path in (
        os.path.expanduser("~/.hermes/profiles/pr-ollama/.env"),
        os.path.expanduser("~/.hermes/.env"),
    ):
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and line.startswith(f"{key}="):
                            return line.split("=", 1)[1].strip().strip("'\"")
            except OSError:
                pass
    return None


def _no_proxy():
    """Temporarily disable proxy env vars (providers reject Tor)."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {}
        for var in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                     "all_proxy", "ALL_PROXY"]:
            if var in os.environ:
                saved[var] = os.environ.pop(var)
        try:
            yield
        finally:
            os.environ.update(saved)

    return _ctx()


# ---------------------------------------------------------------------------
# Profile validation (Guardrail G1)
# ---------------------------------------------------------------------------

def get_existing_profiles():
    """Return the set of profile names that actually exist on this host.

    Parses `hermes profile list` output. Falls back to the known-good
    profiles if the command is unavailable or fails to parse, so the gate
    never blocks on a transient CLI error.
    """
    try:
        proc = subprocess.run(
            ["hermes", "profile", "list"],
            capture_output=True, text=True, timeout=20,
        )
        out = proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        out = ""

    profiles = set()
    for line in out.splitlines():
        # Skip header, separator, and empty lines. The active profile is
        # prefixed with a '◆' marker; strip it.
        line = line.strip().lstrip("\u25c6").strip()
        if not line or line.startswith("─") or line.startswith("Profile"):
            continue
        name = line.split()[0] if line.split() else ""
        if name:
            profiles.add(name)

    # Fallback: if we couldn't parse anything, assume the known-good set.
    if not profiles:
        profiles = set(ALLOWED_PROFILES)
    return profiles


def validate_recommended_profile(recommended, existing, warnings, providers_list=None):
    """Return a profile that is safe to assign tasks to.

    Guardrail G1: the recommended profile must (a) exist on the host and
    (b) be in ALLOWED_PROFILES. If it is not, fall back to the allowed
    provider with the highest availability from providers_list (not
    alphabetical order). Returns None if no allowed profile qualifies.
    """
    if recommended in existing and recommended in ALLOWED_PROFILES:
        return recommended

    if recommended not in ALLOWED_PROFILES:
        warnings.append(
            f"recommended profile '{recommended}' not in allowed set "
            f"{sorted(ALLOWED_PROFILES)}; falling back"
        )
    elif recommended not in existing:
        warnings.append(
            f"recommended profile '{recommended}' does not exist on host; "
            "falling back"
        )

    # BUG 1 FIX: Fall back to the allowed provider with the highest
    # availability, not the first one alphabetically. If providers_list
    # is not available (e.g. called from tests without it), fall back to
    # the old alphabetical behavior as a safe default.
    if providers_list:
        allowed_candidates = [
            p for p in providers_list
            if p["profile"] in ALLOWED_PROFILES
            and p["profile"] in existing
            and not p["error"]
            and p["availability"] > 0
        ]
        if allowed_candidates:
            allowed_candidates.sort(
                key=lambda p: (-p["availability"],
                               PROVIDER_PREFERENCE.get(p["profile"], 99))
            )
            return allowed_candidates[0]["profile"]
    else:
        for candidate in sorted(ALLOWED_PROFILES):
            if candidate in existing:
                return candidate

    warnings.append("no allowed profile exists on host; skipping tick")
    return None


# ---------------------------------------------------------------------------
# Provider queries
# ---------------------------------------------------------------------------

def query_ollama():
    """Query Ollama Cloud usage. Returns dict or raises."""
    api_key = get_env("OLLAMA_API_KEY")
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY not configured")

    req = urllib.request.Request(
        "https://ollama.com/api/usage",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

    session = data.get("limits", {}).get("session", {})
    weekly = data.get("limits", {}).get("weekly", {})

    activity = data.get("activity")
    activity_cost = 0.0
    if activity and isinstance(activity, dict):
        cost = activity.get("cost")
        if cost is not None:
            activity_cost = float(cost)

    return {
        "session_pct": float(session.get("usage", 0)) * 100,
        "weekly_pct": float(weekly.get("usage", 0)) * 100,
        "activity_cost": activity_cost,
    }


def query_nanogpt():
    """Query NanoGPT subscription usage. Returns dict or raises."""
    api_key = get_env("NANO_GPT_API_KEY")
    if not api_key:
        raise RuntimeError("NANO_GPT_API_KEY not configured")

    req = urllib.request.Request(
        "https://nano-gpt.com/api/subscription/v1/usage",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

    daily = data.get("daily", {})
    weekly = data.get("weeklyInputTokens", {})
    return {
        "state": data.get("state"),
        "daily_pct": float(daily.get("percentUsed", 0)) * 100 if daily else None,
        "weekly_tokens_pct": (
            float(weekly.get("percentUsed", 0)) * 100 if weekly else None
        ),
    }


def query_openrouter():
    """Query OpenRouter key usage. Returns dict or raises."""
    api_key = get_env("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode()).get("data", {})

    weekly_usd = data.get("usage_weekly")
    monthly_usd = data.get("usage_monthly")
    limit = data.get("limit")  # spending limit in USD, or null
    usage = data.get("usage")  # total usage in USD
    expires_at = data.get("expires_at")  # ISO timestamp or null

    return {
        "usage_weekly_usd": float(weekly_usd) if weekly_usd is not None else None,
        "usage_monthly_usd": float(monthly_usd) if monthly_usd is not None else None,
        "limit": float(limit) if limit is not None else None,
        "usage": float(usage) if usage is not None else None,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Provider status computation
# ---------------------------------------------------------------------------

# Profile → default model mapping (from config.yaml of each profile)
PROFILE_MODELS = {
    "pr-ollama": "glm-5.2",
    "pr-nanogpt": "zai-org/glm-5.2",
    "pr-openrouter": "z-ai/glm-5.2:free",
}

# Tie-breaking preference order (lower = preferred)
PROVIDER_PREFERENCE = {
    "pr-ollama": 0,
    "pr-nanogpt": 1,
    "pr-openrouter": 2,
}


def compute_ollama_status():
    """Build a ProviderStatus dict for Ollama Cloud."""
    raw = query_ollama()
    s = raw["session_pct"]
    w = raw["weekly_pct"]
    cost = raw["activity_cost"]

    bottleneck_pct = max(s, w)
    bottleneck_window = "session" if s >= w else "weekly"

    # Pay-as-you-go deprioritization: if at 100% session with active cost,
    # set availability=5 so it's only chosen if no free alternative exists.
    if s >= 100 and cost > 0:
        availability = 5.0
    else:
        availability = max(100.0 - bottleneck_pct, 0.0)

    return {
        "profile": "pr-ollama",
        "provider": "ollama-cloud",
        "model": PROFILE_MODELS["pr-ollama"],
        "availability": round(availability, 1),
        "bottleneck_pct": round(bottleneck_pct, 1),
        "bottleneck_window": bottleneck_window,
        "error": "",
        "raw": {"session_pct": round(s, 1), "weekly_pct": round(w, 1),
                "activity_cost": cost},
    }


def compute_nanogpt_status():
    """Build a ProviderStatus dict for NanoGPT."""
    raw = query_nanogpt()
    state = raw.get("state")
    daily_pct = raw.get("daily_pct")
    weekly_pct = raw.get("weekly_tokens_pct")

    # Skip if account not active
    if state != "active":
        return {
            "profile": "pr-nanogpt",
            "provider": "nanogpt",
            "model": PROFILE_MODELS["pr-nanogpt"],
            "availability": 0.0,
            "bottleneck_pct": 100.0,
            "bottleneck_window": "state",
            "error": f"state is '{state}', not 'active'",
            "raw": raw,
        }

    # Compute bottleneck
    pcts = []
    if daily_pct is not None:
        pcts.append(("daily", daily_pct))
    if weekly_pct is not None:
        pcts.append(("weekly_tokens", weekly_pct))

    if not pcts:
        # No usage data — assume fully available
        return {
            "profile": "pr-nanogpt",
            "provider": "nanogpt",
            "model": PROFILE_MODELS["pr-nanogpt"],
            "availability": 100.0,
            "bottleneck_pct": 0.0,
            "bottleneck_window": "unknown",
            "error": "",
            "raw": raw,
        }

    bottleneck_window, bottleneck_pct = max(pcts, key=lambda x: x[1])
    availability = max(100.0 - bottleneck_pct, 0.0)

    return {
        "profile": "pr-nanogpt",
        "provider": "nanogpt",
        "model": PROFILE_MODELS["pr-nanogpt"],
        "availability": round(availability, 1),
        "bottleneck_pct": round(bottleneck_pct, 1),
        "bottleneck_window": bottleneck_window,
        "error": "",
        "raw": {"daily_pct": daily_pct, "weekly_tokens_pct": weekly_pct,
                "state": state},
    }


def compute_openrouter_status():
    """Build a ProviderStatus dict for OpenRouter."""
    raw = query_openrouter()
    limit = raw.get("limit")
    usage = raw.get("usage")
    weekly_usd = raw.get("usage_weekly_usd")
    expires_at = raw.get("expires_at")

    # Check key expiry
    if expires_at:
        from datetime import datetime, timezone
        try:
            expiry = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )
            if datetime.now(timezone.utc) > expiry:
                return {
                    "profile": "pr-openrouter",
                    "provider": "openrouter",
                    "model": PROFILE_MODELS["pr-openrouter"],
                    "availability": 0.0,
                    "bottleneck_pct": 100.0,
                    "bottleneck_window": "key_expired",
                    "error": f"key expired {expires_at[:10]}",
                    "raw": raw,
                }
        except (ValueError, TypeError):
            pass  # Can't parse expiry — continue with usage-based check

    if limit is not None and limit > 0:
        # Spending limit set: usage/limit * 100
        if usage is not None:
            bottleneck_pct = (usage / limit) * 100
        else:
            bottleneck_pct = 0.0
        bottleneck_window = "spending_limit"
    else:
        # No limit: heuristic — $5/week as soft ceiling
        if weekly_usd is not None:
            bottleneck_pct = min(weekly_usd / 5.0 * 100, 100.0)
        else:
            bottleneck_pct = 0.0
        bottleneck_window = "weekly_usd"

    availability = max(100.0 - bottleneck_pct, 0.0)

    return {
        "profile": "pr-openrouter",
        "provider": "openrouter",
        "model": PROFILE_MODELS["pr-openrouter"],
        "availability": round(availability, 1),
        "bottleneck_pct": round(bottleneck_pct, 1),
        "bottleneck_window": bottleneck_window,
        "error": "",
        "raw": {"usage_weekly_usd": weekly_usd, "limit": limit,
                "usage": usage, "expires_at": expires_at},
    }


# ---------------------------------------------------------------------------
# Decision algorithm
# ---------------------------------------------------------------------------

def bottleneck_to_max_cost(bottleneck_pct):
    """Map bottleneck percentage to max_task_cost tier."""
    if bottleneck_pct < 30:
        return "any"
    if bottleneck_pct < 60:
        return "medium"
    if bottleneck_pct < 80:
        return "small"
    if bottleneck_pct < 95:
        return "tiny"
    return "micro"


def bottleneck_to_max_workers(bottleneck_pct):
    """Map bottleneck percentage to max workers."""
    if bottleneck_pct < 50:
        return 2
    if bottleneck_pct < 80:
        return 1
    return 0


def select_provider(providers_list):
    """Pick the provider with the most available quota.

    Returns the best ProviderStatus dict, or None if all exhausted.
    """
    candidates = []
    for p in providers_list:
        if p["error"]:
            # Errored providers are excluded from selection
            continue
        if p["availability"] <= 0:
            continue
        candidates.append(p)

    if not candidates:
        return None

    # Sort by availability descending, then by preference order
    candidates.sort(
        key=lambda p: (-p["availability"], PROVIDER_PREFERENCE.get(p["profile"], 99))
    )

    top = candidates[0]

    # If top provider is in paying mode (availability=5, bottleneck>=100),
    # check if a free provider exists with availability > 5
    if top["bottleneck_pct"] >= 100 and top["availability"] <= 5:
        free = [c for c in candidates if c["bottleneck_pct"] < 100]
        if free:
            return free[0]

    return top


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    providers_list = []
    warnings = []

    # --- Query each provider ---
    # Ollama
    try:
        providers_list.append(compute_ollama_status())
    except Exception as exc:
        providers_list.append({
            "profile": "pr-ollama",
            "provider": "ollama-cloud",
            "model": PROFILE_MODELS["pr-ollama"],
            "availability": 0.0,
            "bottleneck_pct": 100.0,
            "bottleneck_window": "error",
            "error": str(exc),
            "raw": {},
        })
        warnings.append(f"ollama: {exc}")

    # NanoGPT
    nanogpt_key = get_env("NANO_GPT_API_KEY")
    if nanogpt_key:
        try:
            providers_list.append(compute_nanogpt_status())
        except Exception as exc:
            providers_list.append({
                "profile": "pr-nanogpt",
                "provider": "nanogpt",
                "model": PROFILE_MODELS["pr-nanogpt"],
                "availability": 0.0,
                "bottleneck_pct": 100.0,
                "bottleneck_window": "error",
                "error": str(exc),
                "raw": {},
            })
            warnings.append(f"nanogpt: {exc}")
    # else: not configured — skip silently

    # OpenRouter
    openrouter_key = get_env("OPENROUTER_API_KEY")
    if openrouter_key:
        try:
            providers_list.append(compute_openrouter_status())
        except Exception as exc:
            providers_list.append({
                "profile": "pr-openrouter",
                "provider": "openrouter",
                "model": PROFILE_MODELS["pr-openrouter"],
                "availability": 0.0,
                "bottleneck_pct": 100.0,
                "bottleneck_window": "error",
                "error": str(exc),
                "raw": {},
            })
            warnings.append(f"openrouter: {exc}")
    # else: not configured — skip silently

    # --- Select recommended provider ---
    recommended = select_provider(providers_list)

    # --- Guardrail G1: validate the recommended profile against the host ---
    existing_profiles = get_existing_profiles()
    valid_profiles = sorted(existing_profiles & ALLOWED_PROFILES)

    if recommended is not None:
        recommended_profile = validate_recommended_profile(
            recommended["profile"], existing_profiles, warnings,
            providers_list=providers_list,
        )
        if recommended_profile is None:
            # No allowed profile exists on host — do not create any task.
            recommended = None
        else:
            # BUG 2 FIX: Don't mutate the original dict in providers_list.
            # Make a shallow copy so the providers array in the output
            # retains the original profile names for each provider.
            recommended = dict(recommended)
            recommended["profile"] = recommended_profile
            # Keep the model consistent with the (possibly changed) profile.
            recommended["model"] = PROFILE_MODELS.get(
                recommended_profile, recommended["model"]
            )

    if recommended is None:
        # All providers exhausted/errored, or no allowed profile exists
        output = {
            "wakeAgent": False,
            "context": {
                "providers": providers_list,
                "recommended_profile": None,
                "recommended_model": None,
                "max_task_cost": None,
                "max_workers": 0,
                "valid_profiles": valid_profiles,
                "warning": "; ".join(warnings) if warnings else "all providers exhausted",
            },
        }
        print(json.dumps(output))
        return

    # --- Build output ---
    bottleneck = recommended["bottleneck_pct"]
    max_cost = bottleneck_to_max_cost(bottleneck)
    max_workers = bottleneck_to_max_workers(bottleneck)

    # Build warning if any provider had errors
    warning = None
    if warnings:
        warning = "; ".join(warnings)

    # Note paying-mode deprioritization in warning
    if recommended["bottleneck_pct"] >= 100 and recommended["availability"] <= 5:
        if warning:
            warning += f"; {recommended['profile']} in paying mode (deprioritized)"
        else:
            warning = f"{recommended['profile']} in paying mode (deprioritized)"

    output = {
        "wakeAgent": True,
        "context": {
            "providers": providers_list,
            "recommended_profile": recommended["profile"],
            "recommended_model": recommended["model"],
            "max_task_cost": max_cost,
            "max_workers": max_workers,
            "valid_profiles": valid_profiles,
            "warning": warning,
        },
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()