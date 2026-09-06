#!/usr/bin/env python3
"""quota-gate.py — pre-run script for the autonomous task creator cron job.

Queries all configured providers (Ollama Cloud, NanoGPT, OpenRouter,
OpenCode Go) and outputs a recommended profile with the most available
quota.

OpenCode Go (MULTI-PROV-06, Sep 2026):
  Endpoint: GET https://opencode.ai/zen/go/v1/usage
  Windows: rolling + weekly + monthly, ``percent`` is already 0-100.
  The gate reports it as a full candidate (not informational) because
  all three windows map to the availability scoring; the profile
  pr-opencode is added to ALLOWED_PROFILES so the task creator can
  assign to it.

Privacy routing (Phase 2):
  Tasks may carry a ``privacy:`` tag in their body (public|sensitive|
  confidential).  When present, the gate filters providers by their
  privacy capability before applying the normal availability scoring.

    public       → any provider (no filtering)
    sensitive    → zero-retention providers only (no training on data)
    confidential → local model only (data never leaves the host)

  The privacy level is read from the ``QUOTA_GATE_PRIVACY`` env var or
  from the ``privacy:`` field of a JSON object piped on stdin.

Parked profiles (t_7da69d59, Sep 2026):
  Providers config ``scripts/providers.json`` may mark a profile with
  ``"parked": true`` (temporarily out of use, e.g. pr-openrouter).  Parked
  profiles are excluded from the candidate set BEFORE recommended_profile
  is computed, so guardrail G1 no longer logs "recommended profile
  'pr-openrouter' not in allowed set; falling back" on every tick.  They
  still appear in the ``providers`` array (informational, with a
  ``parked: true`` flag).

Output (last line, JSON):
  {"wakeAgent": false}  — skip this tick, all providers exhausted
  {"wakeAgent": true, "context": {
      "providers": [...],              # each entry carries "parked": bool
      "recommended_profile": "pr-...",
      "recommended_model": "...",
      "recommended_worker_model": "...",   # cheap model for worker tasks
      "worker_models": {profile: cheap model},     # MULTI-PROV-07
      "interactive_models": {profile: quality model},
      "peak_pricing": {"active": bool, ...},       # MULTI-PROV-07
      "max_task_cost": "medium|small|tiny|micro|any",
      "max_workers": N,
      "privacy_level": "public|sensitive|confidential|none",
      "warning": null
  }}

The JSON context is consumed by the autonomous-task-creator cron prompt.
The agent reads recommended_profile and recommended_model to decide which
profile to assign tasks to, instead of hardcoding pr-ollama.

Design references:
  - ~/.hermes/profiles/pr-ollama/docs/multi-provider-design.md
  - ~/.hermes/profiles/pr-ollama/docs/privacy-by-provider-design.md §7
  - ~/.hermes/profiles/pr-ollama/docs/autonomous-objectives.md §8.6
"""
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Guardrail G1: only these profiles may receive auto-created tasks.
# The autonomous task creator must NEVER assign to any other profile.
# pr-opencode added in MULTI-PROV-06 (OpenCode Go provider).
ALLOWED_PROFILES = {"pr-ollama", "pr-nanogpt", "pr-opencode"}

# ---------------------------------------------------------------------------
# Privacy capability mapping (Phase 2 — privacy-by-provider-design.md §4, §7)
# ---------------------------------------------------------------------------
# Each privacy level maps to the set of provider names (as they appear in the
# ProviderStatus["provider"] field) that are *capable* of handling tasks at
# that level.  The profile must also be in ALLOWED_PROFILES.
#
# public       — no restriction; any cloud or local provider qualifies.
# sensitive    — provider must have a no-training / zero-retention policy
#                 for API data.  Ollama Cloud and NanoGPT qualify (they do
#                 not train on API data).  OpenRouter *passes through* to
#                 sub-providers with their own policies, so it is excluded
#                 for sensitive data unless data_collection:deny is set.
#                 (Per design doc §4.1, OpenRouter does not retain prompt
#                 content, but the sub-provider might — safer to exclude.)
# confidential — data must never leave the host.  Only local providers
#                 (custom/ollama-local) qualify.  On this host there is no
#                 local profile configured, so confidential tasks get
#                 wakeAgent:false with a warning.
PRIVACY_CAPABILITIES = {
    # OpenCode Go: public only — its retention/training policy is not
    # audited (see provider-privacy-audit.md, which predates it), so we
    # take the same conservative stance as OpenRouter without
    # data_collection:deny. Revisit if a ZDR policy is verified.
    "public": {"ollama-cloud", "nanogpt", "openrouter", "opencode-go", "custom"},
    "sensitive": {"ollama-cloud", "nanogpt", "custom"},
    "confidential": {"custom"},
}

# Reverse: provider name → set of privacy levels it can handle
_PROVIDER_PRIVACY = {}
for _level, _providers in PRIVACY_CAPABILITIES.items():
    for _prov in _providers:
        _PROVIDER_PRIVACY.setdefault(_prov, set()).add(_level)

VALID_PRIVACY_LEVELS = {"public", "sensitive", "confidential"}

# ---------------------------------------------------------------------------#
# Privacy-level provider preference override (OBJ-18 Phase 3)
# ---------------------------------------------------------------------------#
# When a privacy level is active, this dict overrides PROVIDER_PREFERENCE
# and changes select_provider() to *preference-first* sorting: the provider
# with the lowest preference number wins, availability is only a tie-breaker.
#
# Rationale (OBJ-18 completion criterion):
#   "privacy:high va a NanoGPT, privacy:low va a Ollama"
#
# With availability-first routing the criterion is NOT guaranteed: if Ollama
# has more spare quota than NanoGPT, Ollama wins even for sensitive tasks.
# Preference-first ensures NanoGPT is always chosen for sensitive tasks as
# long as it has *any* availability > 0.
#
# For `public` we keep availability-first (no override entry → falls back to
# the normal PROVIDER_PREFERENCE tie-breaker).  This means privacy:low still
# routes to whichever provider has the most quota — which is usually Ollama
# on this host, but not guaranteed.  See §6.1 of privacy-routing-matrix.md
# for the design discussion.
PRIVACY_PROVIDER_PREFERENCE = {
    # sensitive: NanoGPT preferred over Ollama (OpenRouter already excluded)
    "sensitive": {"pr-nanogpt": 0, "pr-ollama": 1, "pr-openrouter": 2},
    # confidential: only local/custom qualifies
    "confidential": {"pr-local": 0, "custom": 0},
}

# Levels that trigger preference-first routing (vs availability-first)
_PREFERENCE_FIRST_LEVELS = set(PRIVACY_PROVIDER_PREFERENCE.keys())

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
# Providers config (parked profiles — t_7da69d59)
# ---------------------------------------------------------------------------

PROVIDERS_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "providers.json"
)


def load_providers_config(path=None):
    """Read providers.json (provider-profile config). Returns {} if absent.

    Format: {"providers": {"<profile>": {"parked": bool, "reason": str}}}
    Malformed JSON must never break the gate — fall back to {} silently.
    """
    if path is None:
        path = PROVIDERS_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data["providers"]
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_parked_profiles(config=None):
    """Set of profile names marked parked:true — temporarily out of use.

    Parked profiles are excluded from the candidate set BEFORE
    recommended_profile is computed (t_7da69d59), so guardrail G1 does
    not warn-and-fall-back on every tick when e.g. pr-openrouter wins
    availability ranking.
    """
    if config is None:
        config = load_providers_config()
    return {
        name for name, cfg in config.items()
        if isinstance(cfg, dict) and cfg.get("parked") is True
    }


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
        # pr-opencode holds OPENCODE_GO_API_KEY (MULTI-PROV-06); the gate
        # runs from the pr-ollama cron, so the active HERMES_HOME .env
        # does not contain it.
        os.path.expanduser("~/.hermes/profiles/pr-opencode/.env"),
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


# ---------------------------------------------------------------------------#
# Transient HTTP retry + last-known-good cache (OBJ-20)
# ---------------------------------------------------------------------------#

_TRANSIENT_HTTP_CODES = {403, 429, 500, 502, 503, 504}
_RETRY_DELAY = 1.0
_MAX_RETRIES = 2


def _retry_http(fn, *, retries=_MAX_RETRIES, delay=_RETRY_DELAY):
    """Retry *fn* on transient HTTP errors (403, 429, 5xx, network)."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_HTTP_CODES and attempt < retries:
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < retries:
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _cache_dir():
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    base = hermes_home if hermes_home else os.path.expanduser("~/.hermes")
    return os.path.join(base, "quota-governor")


def _read_cache(provider):
    path = os.path.join(_cache_dir(), f"{provider}-last-good.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(provider, data):
    path = os.path.join(_cache_dir(), f"{provider}-last-good.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


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


def validate_recommended_profile(recommended, existing, warnings,
                                 providers_list=None, parked=None):
    """Return a profile that is safe to assign tasks to.

    Guardrail G1: the recommended profile must (a) exist on the host and
    (b) be in ALLOWED_PROFILES. If it is not, fall back to the allowed
    provider with the highest availability from providers_list (not
    alphabetical order). Returns None if no allowed profile qualifies.

    *parked* (t_7da69d59): profiles excluded from the fallback too. In
    the normal flow parked profiles never reach this function (they are
    filtered in select_provider first), so the G1 warning for a parked
    profile is no longer emitted on every tick.
    """
    parked = parked or set()
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
            and p["profile"] not in parked
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

    def _do_request():
        req = urllib.request.Request(
            "https://ollama.com/api/usage",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with _no_proxy():
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())

    data = _retry_http(_do_request)

    session = data.get("limits", {}).get("session", {})
    weekly = data.get("limits", {}).get("weekly", {})

    activity = data.get("activity")
    activity_cost = 0.0
    if activity and isinstance(activity, dict):
        cost = activity.get("cost")
        if cost is not None:
            activity_cost = float(cost)

    result = {
        "session_pct": float(session.get("usage", 0)) * 100,
        "weekly_pct": float(weekly.get("usage", 0)) * 100,
        "activity_cost": activity_cost,
    }
    _write_cache("ollama", result)
    return result


def query_nanogpt():
    """Query NanoGPT subscription usage. Returns dict or raises.

    Falls back to last-known-good cache on transient errors (OBJ-20).
    """
    api_key = get_env("NANO_GPT_API_KEY")
    if not api_key:
        raise RuntimeError("NANO_GPT_API_KEY not configured")

    def _do_request():
        req = urllib.request.Request(
            "https://nano-gpt.com/api/subscription/v1/usage",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with _no_proxy():
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())

    try:
        data = _retry_http(_do_request)
    except Exception:
        cached = _read_cache("nanogpt")
        if cached:
            cached["_cached"] = True
            return cached
        raise

    daily = data.get("daily", {})
    weekly = data.get("weeklyInputTokens", {})
    result = {
        "state": data.get("state"),
        "daily_pct": float(daily.get("percentUsed", 0)) * 100 if daily else None,
        "weekly_tokens_pct": (
            float(weekly.get("percentUsed", 0)) * 100 if weekly else None
        ),
    }
    _write_cache("nanogpt", result)
    return result


def query_openrouter():
    """Query OpenRouter key usage. Returns dict or raises.

    Falls back to last-known-good cache on transient errors (OBJ-20).
    """
    api_key = get_env("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    def _do_request():
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with _no_proxy():
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode()).get("data", {})

    try:
        data = _retry_http(_do_request)
    except Exception:
        cached = _read_cache("openrouter")
        if cached:
            cached["_cached"] = True
            return cached
        raise

    weekly_usd = data.get("usage_weekly")
    monthly_usd = data.get("usage_monthly")
    limit = data.get("limit")  # spending limit in USD, or null
    usage = data.get("usage")  # total usage in USD
    expires_at = data.get("expires_at")  # ISO timestamp or null

    result = {
        "usage_weekly_usd": float(weekly_usd) if weekly_usd is not None else None,
        "usage_monthly_usd": float(monthly_usd) if monthly_usd is not None else None,
        "limit": float(limit) if limit is not None else None,
        "usage": float(usage) if usage is not None else None,
        "expires_at": expires_at,
    }
    _write_cache("openrouter", result)
    return result


def query_opencode_go():
    """Query OpenCode Go usage (MULTI-PROV-06). Returns dict or raises.

    Endpoint: GET https://opencode.ai/zen/go/v1/usage
    ``percent`` is already 0-100 (no fraction conversion).
    Falls back to last-known-good cache on transient errors (OBJ-20).
    Sends a custom User-Agent — Cloudflare 1010-bans Python-urllib.
    """
    api_key = get_env("OPENCODE_GO_API_KEY")
    if not api_key:
        raise RuntimeError("OPENCODE_GO_API_KEY not configured")

    def _do_request():
        req = urllib.request.Request(
            "https://opencode.ai/zen/go/v1/usage",
            headers={
                "Authorization": f"Bearer {api_key}",
                # Cloudflare 1010-bans the default Python-urllib UA.
                "User-Agent": "hermes-quota-governor/1.0",
            },
        )
        with _no_proxy():
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())

    try:
        data = _retry_http(_do_request)
    except Exception:
        cached = _read_cache("opencode_go")
        if cached:
            cached["_cached"] = True
            return cached
        raise

    usage = data.get("usage", {})
    rolling = usage.get("rolling", {})
    weekly = usage.get("weekly", {})
    monthly = usage.get("monthly", {})

    def _pct(window):
        """Extract percent (already 0-100) from a window dict."""
        val = window.get("percent")
        return float(val) if val is not None else None

    result = {
        "rolling_pct": _pct(rolling),
        "weekly_pct": _pct(weekly),
        "monthly_pct": _pct(monthly),
        "rolling_status": rolling.get("status"),
        "weekly_status": weekly.get("status"),
        "monthly_status": monthly.get("status"),
        "rolling_resets_at": rolling.get("resetsAt"),
        "weekly_resets_at": weekly.get("resetsAt"),
        "monthly_resets_at": monthly.get("resetsAt"),
    }
    _write_cache("opencode_go", result)
    return result


# ---------------------------------------------------------------------------
# Provider status computation
# ---------------------------------------------------------------------------

# Profile → default model mapping (from config.yaml of each profile)
PROFILE_MODELS = {
    "pr-ollama": "glm-5.2",
    "pr-nanogpt": "zai-org/glm-5.2",
    "pr-openrouter": "z-ai/glm-5.2:free",
    "pr-opencode": "glm-5.3-flash",  # opencode-go provider, MULTI-PROV-06
}

# ---------------------------------------------------------------------------
# Cost-based model selection (MULTI-PROV-07, Sep 2026)
# ---------------------------------------------------------------------------
# Generic rule, all providers: auto-created WORKER tasks must use the CHEAP
# model of the assigned profile; the expensive/interactive model is reserved
# for interactive sessions and tasks tagged cost:medium or above.
#
# Measured per-million prices (USD):
#   pr-opencode:  glm-5.2 $1.40/$4.40  vs  qwen3.8-flash $0.15/$0.47 (~9x cheaper)
#   pr-ollama:    glm-5.2 $1.40/$4.40  vs  deepseek-v4-flash $0.22/M
#   pr-nanogpt:   zai-org/glm-5.2 (subscription-covered; cheap covered tier TBD —
#                 qwen3.5-4b NOT covered, HTTP 402, verified Sep 7 2026)
#
# INTERACTIVE MODEL WARNING (Sep 7 2026, verified live — OpenCode Go console):
# glm-5.2 via opencode-go burned 82% of the 5h window alone ($9.84 of $12)
# while the worker (qwen3.8-flash) used 7.5% and glm-5.3-flash 3.6%.  The
# pr-opencode interactive model is therefore glm-5.3-flash, NOT glm-5.2.
# glm-5.2 stays interactive for pr-ollama (Ollama Cloud Pro window is wider).
#
# PITFALL (verified live, MULTI-PROV-07 diagnostic): deepseek-v4-flash via
# OpenCode Go returns RegionError 403 (China-hosted, requires explicit
# account opt-in).  It is therefore ONLY usable as pr-ollama's worker model,
# NEVER as pr-opencode's.  minimax-m2.7 on OpenCode Go fails with Internal
# server error — also not usable.
PROFILE_WORKER_MODELS = {
    "pr-ollama": "deepseek-v4-flash",       # $0.22/M
    "pr-nanogpt": "zai-org/glm-5.2",        # Sep 7 2026: subscription-COVERED (proven by
                                            # worker t_a5f2d953).  qwen3.5-4b returns HTTP
                                            # 402 Insufficient balance — NOT included in the
                                            # NanoGPT subscription, prepaid balance empty.
                                            # Cheaper covered candidate TBD via MULTI-PROV-10.2.
    "pr-openrouter": "z-ai/glm-5.2:free",   # free tier already — keep as is
    "pr-opencode": "qwen3.8-flash",         # $0.15/$0.47/M, no peak pricing
}

# Cost tiers whose auto-created tasks must use the worker (cheap) model.
# medium+ may use PROFILE_MODELS (interactive/quality model).
WORKER_COST_TIERS = {"micro", "tiny", "small"}

# ---------------------------------------------------------------------------
# Peak pricing (MULTI-PROV-07)
# ---------------------------------------------------------------------------
# OpenCode Go / Ollama Cloud DeepSeek models double in price during peak
# hours: Monday–Friday 01:00–04:00 UTC and 06:00–10:00 UTC.  qwen3.8-flash
# and glm-5.2 have NO peak pricing, so today's recommended worker models
# are unaffected — but the gate exposes peak_pricing in its context so
# future peak-priced models can be handled without re-touching the gate.
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))  # half-open [start, end) hour ranges
PEAK_AFFECTED_MODELS = {
    "ollama-cloud": ["deepseek-v4-flash"],
    "opencode-go": ["deepseek-v4-flash"],
}


def is_peak_hours(now=None):
    """True if *now* (UTC, default: current time) falls in peak pricing hours.

    Peak = Monday–Friday within any PEAK_WINDOWS_UTC hour range.
    Weekends are never peak.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return any(start <= now.hour < end for start, end in PEAK_WINDOWS_UTC)


def peak_pricing_context(now=None):
    """Build the peak_pricing block for the gate output context."""
    active = is_peak_hours(now)
    return {
        "active": active,
        "windows_utc": [f"{s:02d}:00-{e:02d}:00" for s, e in PEAK_WINDOWS_UTC],
        "days": "monday-friday",
        "multiplier": 2,
        "affected_models": PEAK_AFFECTED_MODELS if active else {},
        "note": (
            "DeepSeek models double in price during peak hours. Current "
            "worker models (qwen3.8-flash, glm-5.2, glm-5.3-flash) have no "
            "peak pricing."
        ),
    }


def worker_model_for(profile):
    """Cheap worker model for *profile* (MULTI-PROV-07 rule).

    Falls back to the interactive PROFILE_MODELS entry if the profile has
    no cheap model mapped.
    """
    return PROFILE_WORKER_MODELS.get(profile, PROFILE_MODELS.get(profile))

# Tie-breaking preference order (lower = preferred)
PROVIDER_PREFERENCE = {
    "pr-ollama": 0,
    "pr-nanogpt": 1,
    "pr-openrouter": 2,
    "pr-opencode": 3,  # newest provider — lowest tie-break priority
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


def compute_opencode_go_status():
    """Build a ProviderStatus dict for OpenCode Go (MULTI-PROV-06).

    All three windows (rolling, weekly, monthly) contribute to the
    bottleneck: the window with the highest percent is the bottleneck,
    availability = 100 - bottleneck. A window whose ``status`` is not
    "ok" (e.g. rate-limited) is treated as fully used.
    """
    raw = query_opencode_go()
    rolling_pct = raw.get("rolling_pct")
    weekly_pct = raw.get("weekly_pct")
    monthly_pct = raw.get("monthly_pct")

    windows = [
        ("rolling", rolling_pct, raw.get("rolling_status")),
        ("weekly", weekly_pct, raw.get("weekly_status")),
        ("monthly", monthly_pct, raw.get("monthly_status")),
    ]

    # A non-ok status on any window means that window is exhausted
    # (rate-limited / over quota) — treat as 100%.
    pcts = []
    for name, pct, status in windows:
        if status is not None and status != "ok":
            pcts.append((name, 100.0))
        elif pct is not None:
            pcts.append((name, float(pct)))

    if not pcts:
        # No usage data — assume fully available
        return {
            "profile": "pr-opencode",
            "provider": "opencode-go",
            "model": PROFILE_MODELS["pr-opencode"],
            "availability": 100.0,
            "bottleneck_pct": 0.0,
            "bottleneck_window": "unknown",
            "error": "",
            "raw": raw,
        }

    bottleneck_window, bottleneck_pct = max(pcts, key=lambda x: x[1])
    availability = max(100.0 - bottleneck_pct, 0.0)

    # Balance-fallback detection (calibrated live Sep 7 2026, t_47640f18):
    # when a window is exhausted (status "rate-limited") but the API keeps
    # serving requests, OpenCode Go is burning prepaid Zen balance — money,
    # not subscription quota.  Availability stays 0 (correct: don't route
    # more work here), but the context must say WHY so nobody mistakes
    # "burning paid balance" for a hard block.
    burning_balance = any(
        status is not None and status != "ok" for _, _, status in windows
    )

    return {
        "profile": "pr-opencode",
        "provider": "opencode-go",
        "model": PROFILE_MODELS["pr-opencode"],
        "availability": round(availability, 1),
        "bottleneck_pct": round(bottleneck_pct, 1),
        "bottleneck_window": bottleneck_window,
        "burning_balance": burning_balance,
        "error": "",
        "raw": {
            "rolling_pct": rolling_pct,
            "weekly_pct": weekly_pct,
            "monthly_pct": monthly_pct,
            "rolling_status": raw.get("rolling_status"),
            "weekly_status": raw.get("weekly_status"),
            "monthly_status": raw.get("monthly_status"),
        },
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


def _normalise_privacy_value(value):
    """Normalise a raw privacy tag value to a canonical level.

    Accepts the three canonical levels (public, sensitive, confidential)
    plus the high/medium/low aliases and legacy abbreviations.

    Aliases:
      high     → sensitive   (strictest cloud-capable level)
      medium   → sensitive   (conservative: same as high)
      low      → public      (no restriction)
      pub      → public
      publico  → public
      sens     → sensitive
      selectivo→ sensitive
      conf     → confidential
      intimo   → confidential

    Returns the canonical level string, or None if the value is not
    a recognised privacy level or alias.
    """
    if not value:
        return None
    value = value.strip().lower()
    if value in VALID_PRIVACY_LEVELS:
        return value
    # high/medium/low aliases (OBJ-18)
    if value in ("high",):
        return "sensitive"
    if value in ("medium",):
        return "sensitive"
    if value in ("low",):
        return "public"
    # Legacy abbreviations and Spanish aliases
    if value in ("pub", "publico"):
        return "public"
    if value in ("sens", "selectivo"):
        return "sensitive"
    if value in ("conf", "intimo", "confidential"):
        return "confidential"
    return None


def parse_privacy_tag(text):
    """Extract the privacy level from a task body or arbitrary text.

    Looks for ``privacy: <level>`` where <level> is one of
    public, sensitive, or confidential — or the high/medium/low
    aliases.  The tag may appear anywhere in the text and is
    case-insensitive.  Returns the normalised level string, or
    None if no valid tag is found.

    Examples:
      "privacy:sensitive\\nrest of body"  →  "sensitive"
      "privacy:high\\nrest of body"        →  "sensitive"
      "privacy:low"                        →  "public"
      "Some text\\nprivacy: confidential"  →  "confidential"
      "no tag here"                        →  None
    """
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("privacy:"):
            value = stripped[len("privacy:"):].strip()
            # Remove surrounding quotes if present
            value = value.strip("'\"")
            return _normalise_privacy_value(value)
    return None


def parse_privacy_level():
    """Determine the privacy level for this gate run.

    Resolution order:
      1. QUOTA_GATE_PRIVACY env var (set by the cron prompt or caller).
      2. JSON object on stdin with a "privacy" or "privacy_level" field.
      3. None (no privacy constraint — normal behaviour).
    """
    # 1. Env var (accepts canonical levels and high/medium/low aliases)
    env_val = os.environ.get("QUOTA_GATE_PRIVACY", "").strip()
    normalised = _normalise_privacy_value(env_val)
    if normalised:
        return normalised

    # 2. stdin JSON (accepts canonical levels and high/medium/low aliases)
    try:
        if not sys.stdin.isatty():
            stdin_data = sys.stdin.read().strip()
            if stdin_data:
                obj = json.loads(stdin_data)
                if isinstance(obj, dict):
                    val = obj.get("privacy") or obj.get("privacy_level")
                    if val:
                        normalised = _normalise_privacy_value(val)
                        if normalised:
                            return normalised
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    return None


def select_provider(providers_list, privacy_level=None, parked=None):
    """Pick the provider with the most available quota.

    *parked* (t_7da69d59): set of profile names marked ``parked: true`` in
    providers.json.  Parked profiles are removed from the candidate set
    here — BEFORE any recommendation is computed — so a parked profile can
    never become recommended_profile and trigger the G1 warn-and-fallback
    warning on every tick.  None → no parked filtering (tests / callers
    without config).

    If *privacy_level* is given (public|sensitive|confidential), providers
    are first filtered to those capable of handling that privacy level
    before the normal availability scoring is applied.

    Routing mode:
      * **availability-first** (default, and for ``public``): providers are
        sorted by availability descending, then by PROVIDER_PREFERENCE as
        tie-breaker.
      * **preference-first** (for ``sensitive`` and ``confidential``):
        providers are sorted by PRIVACY_PROVIDER_PREFERENCE first, then by
        availability as tie-breaker.  This ensures the OBJ-18 criterion
        (high → NanoGPT) is satisfied even when a less-preferred provider
        has more spare quota.

    Returns the best ProviderStatus dict, or None if all exhausted
    (or if no provider satisfies the privacy constraint).
    """
    candidates = []
    for p in providers_list:
        if p["error"]:
            # Errored providers are excluded from selection
            continue
        if p["availability"] <= 0:
            continue
        # Parked profiles never enter the candidate set (t_7da69d59)
        if parked and p["profile"] in parked:
            continue
        # Privacy filtering: skip providers that can't handle this level
        if privacy_level:
            capable = _PROVIDER_PRIVACY.get(p["provider"], set())
            if privacy_level not in capable:
                continue
        candidates.append(p)

    if not candidates:
        return None

    # Determine routing mode: preference-first for sensitive/confidential,
    # availability-first for public and no-privacy.
    use_preference_first = (
        privacy_level is not None
        and privacy_level in _PREFERENCE_FIRST_LEVELS
    )

    if use_preference_first:
        pref_map = PRIVACY_PROVIDER_PREFERENCE[privacy_level]
        # Sort by privacy preference (ascending), then availability (descending)
        candidates.sort(
            key=lambda p: (
                pref_map.get(p["profile"], 99),
                -p["availability"],
            )
        )
    else:
        # Availability-first (public or no privacy)
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

    # --- Parse privacy level (Phase 2) ---
    privacy_level = parse_privacy_level()

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

    # OpenCode Go (MULTI-PROV-06)
    opencode_go_key = get_env("OPENCODE_GO_API_KEY")
    if opencode_go_key:
        try:
            providers_list.append(compute_opencode_go_status())
        except Exception as exc:
            providers_list.append({
                "profile": "pr-opencode",
                "provider": "opencode-go",
                "model": PROFILE_MODELS["pr-opencode"],
                "availability": 0.0,
                "bottleneck_pct": 100.0,
                "bottleneck_window": "error",
                "error": str(exc),
                "raw": {},
            })
            warnings.append(f"opencode_go: {exc}")
    # else: not configured — skip silently

    # --- Parked profiles (t_7da69d59) ---
    # Providers config may park profiles temporarily (e.g. pr-openrouter).
    # Parked profiles stay in the providers array for observability but are
    # excluded from selection BEFORE recommended_profile is computed, so
    # guardrail G1 does not warn-and-fall-back every tick.
    parked = get_parked_profiles()
    for p in providers_list:
        p["parked"] = p["profile"] in parked

    # --- Select recommended provider (with privacy filtering) ---
    recommended = select_provider(providers_list, privacy_level=privacy_level,
                                  parked=parked)

    # If privacy filtering eliminated all candidates, warn
    if recommended is None and privacy_level:
        capable_providers = PRIVACY_CAPABILITIES.get(privacy_level, set())
        available_names = [p["provider"] for p in providers_list if not p["error"]]
        warnings.append(
            f"privacy:{privacy_level} excludes all available providers "
            f"(available: {available_names}, capable: {sorted(capable_providers)})"
        )

    # --- Guardrail G1: validate the recommended profile against the host ---
    existing_profiles = get_existing_profiles()
    valid_profiles = sorted(existing_profiles & ALLOWED_PROFILES)

    if recommended is not None:
        recommended_profile = validate_recommended_profile(
            recommended["profile"], existing_profiles, warnings,
            providers_list=providers_list,
            parked=parked,
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
        # All providers exhausted/errored, or no allowed profile exists,
        # or privacy filtering eliminated all candidates
        output = {
            "wakeAgent": False,
            "context": {
                "providers": providers_list,
                "recommended_profile": None,
                "recommended_model": None,
                "max_task_cost": None,
                "max_workers": 0,
                "privacy_level": privacy_level or "none",
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
            "recommended_worker_model": worker_model_for(recommended["profile"]),
            "model_selection_rule": (
                "Workers (cost:micro/tiny/small) MUST use the profile's "
                "cheap worker model; only cost:medium+ tasks may use the "
                "interactive model (recommended_model)."
            ),
            "worker_models": PROFILE_WORKER_MODELS,
            "interactive_models": PROFILE_MODELS,
            "peak_pricing": peak_pricing_context(),
            "max_task_cost": max_cost,
            "max_workers": max_workers,
            "privacy_level": privacy_level or "none",
            "valid_profiles": valid_profiles,
            "warning": warning,
        },
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()