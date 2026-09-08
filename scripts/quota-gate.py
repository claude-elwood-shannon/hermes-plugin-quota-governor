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
  {"wakeAgent": false}  — skip this tick, all providers exhausted,
                        or a zombie worker guard fired (G3 deterministic)
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
      "privacy_summary": {"high": N, "medium": N, "low": N, "none": N},
      "zombie_check": {"has_zombie": bool, "count": N, "threshold_minutes": 45,
                       "tasks": [...]},            # OBJ-21 G3 deterministic
      "warning": null
  }}

privacy_summary (OBJ-18 S1) is a read-only census of the "privacy:"
tags found in the bodies of active (non-terminal) tasks in kanban.db.
It does NOT influence provider routing — it only reports how many
active tasks carry each privacy tag so the task creator and user can
see the privacy demand landscape.  Routing by privacy is S2.

zombie_check (OBJ-21, t_e793b2b9) IS decisional: when a running task's
live age (heartbeat preferred, else started_at) exceeds 45 minutes the
gate forces wakeAgent:false — the G3 creator-silence rule, now
deterministic instead of prompt-dependent.  The prompt keeps G3 as a
second line of defence.

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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Modo sugerente (OBJ-24 F2): --suggest inyecta forecast_warning en el
# contexto (OBSERVADOR, nunca cambia wakeAgent ni la recomendación).
# Veto real solo con --enforce tras una semana sin falsos positivos.
_SUGGEST = "--suggest" in sys.argv[1:]
_ENFORCE = "--enforce" in sys.argv[1:]

# Colchón (horas) antes del reset semanal para disparar la regla de
# reducción de workers del predictor (criterio OBJ-24 F2).
FORECAST_COLCHON_H = 2.0

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


def _config_zen_monthly_soft_cap():
    """Read the optional monthly Zen soft-cap for pr-opencode (t_47640f18).

    PLACEHOLDER — the exact USD ceiling for Zen credits that autonomous
    tasks may burn in a month is PENDING USER DECISION. Until the user
    names an amount this returns None (no cap enforced), regardless of
    the env var. Set OPENCODE_GO_ZEN_MONTHLY_SOFT_CAP (USD float) only
    after the user confirms the figure; the var is read but currently
    documented as inert so a stray value cannot silently constrain quota.
    """
    raw = os.environ.get("OPENCODE_GO_ZEN_MONTHLY_SOFT_CAP", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
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


def load_burn_warnings():
    """Read the burn-watchdog's active warnings (MULTI-PROV-08).

    The watchdog persists open burn warnings to
    ``~/.hermes/quota-governor/burn-warnings.json``. When any exist, they
    are surfaced into the gate snapshot's ``context.burn_warnings`` so the
    task creator / user can see a provider burning prepaid balance before
    it hits the STOP threshold. Absent/unparseable -> empty dict (never
    breaks the gate).
    """
    path = os.path.join(_cache_dir(), "burn-warnings.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_forecast():
    """Read the OBJ-24 F2 predictor output (forecast.json).

    Written by quota-forecast.py (no_agent cron, every 15m after
    quota-metrics). Absent/unparseable -> {} (never breaks the gate).
    """
    path = os.path.join(_cache_dir(), "forecast.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def forecast_context(forecast):
    """Build the ``forecast_warning`` block for the creator context (F2).

    Decision rule (criterio definido en la tarea OBJ-24):
      - ETA_90 < margen hasta el reset (2h de colchón): reducir max_workers
        a 1 y marcar max_task_cost en el contexto (suggest level: warn).
      - ETA_90 < 1h: wakeAgent:false (board se apaga solo).

    En modo --suggest TODO es observador: devuelve un dict con la(s)
    regla(s) disparadas y los providers afectados; el gate solo lo anota
    en context.forecast_warning. Con --enforce (F2, tras 7 días de
    backtest) el caller decide cómo aplicar el veto.
    """
    if not isinstance(forecast, dict) or not forecast.get("enabled"):
        return None

    reset_h = forecast.get("hours_to_reset")
    try:
        reset_h = float(reset_h) if reset_h is not None else None
    except (TypeError, ValueError):
        reset_h = None

    providers = forecast.get("providers") or {}
    fired = []
    shutdown = False
    for prov, f in providers.items():
        if not isinstance(f, dict):
            continue
        eta90 = f.get("eta_90_hours")
        try:
            eta90 = float(eta90) if eta90 is not None else None
        except (TypeError, ValueError):
            eta90 = None
        if eta90 is None or eta90 < 0:
            continue
        if eta90 < 1.0:
            fired.append(f"{prov}: eta_90={eta90:.2f}h (<1h) → board off")
            shutdown = True
        elif reset_h is not None and eta90 < (reset_h - FORECAST_COLCHON_H):
            fired.append(
                f"{prov}: eta_90={eta90:.2f}h < margen reset "
                f"({reset_h:.1f}h) → max_workers=1, cap cost")
    if not fired:
        return None
    return {
        "mode": "suggest" if _SUGGEST and not _ENFORCE else "enforce",
        "hours_to_reset": reset_h,
        "rules": fired,
        "shutdown": shutdown,
        "note": ("predictor EMA (6h, alpha 0.3) sobre metrics-history — "
                 "observador; veto real solo con --enforce tras 7d de backtest"),
    }


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

    import uuid

    session_id = str(uuid.uuid4())

    def _do_request():
        req = urllib.request.Request(
            "https://opencode.ai/zen/go/v1/usage",
            headers={
                "Authorization": f"Bearer {api_key}",
                # Cloudflare 1010-bans the default Python-urllib UA.
                "User-Agent": "hermes-quota-governor/1.0",
                # OpenCode Go requires x-opencode-session for routing since 2026-09-06.
                "x-opencode-session": session_id,
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
#   pr-nanogpt:   zai-org/glm-5.2 $0.42/$1.32 interactive vs z-ai/glm-5.3-flash
#                 $0.075/$0.25 worker (subscription-covered, proven Sep 8 2026
#                 by worker t_154b29f2 — 5.6x/5.3x cheaper)
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
    "pr-nanogpt": "z-ai/glm-5.3-flash",     # Sep 8 2026: subscription-COVERED (proven by
                                            # worker t_154b29f2, run 407 — no HTTP 402, real
                                            # artifacts in workspace).  Probe 8-sep 00:55 CEST:
                                            # 200 OK on nano-gpt.com/v1/chat/completions.
                                            # Price 0.075/0.25 USD/M in/out — 5.6x cheaper input,
                                            # 5.3x cheaper output than glm-5.2 (0.42/1.32).
                                            # Previous: zai-org/glm-5.2 (worker t_a5f2d953,
                                            # archived as historical coverage evidence).
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


# ---------------------------------------------------------------------------
# Per-model cost ledger (MULTI-PROV-09, t_5bdd7cfa)
# ---------------------------------------------------------------------------
# The ledger accumulates per-model consumption within OpenCode Go 5h
# rolling windows from the token deltas Hermes records in each profile's
# state.db (the API cost field is not persisted by Hermes — verified Sep 7
# 2026).  Integration is deliberately non-fatal: observability must never
# break the gate, so every helper here is wrapped in try/except and only
# ADDS context fields/warnings.  See scripts/model-cost-ledger.py for the
# estimation formula and its limitations.

def _model_ledger_module():
    """Lazily import model-cost-ledger.py (hyphenated filename)."""
    global _MODEL_LEDGER_MOD
    if _MODEL_LEDGER_MOD is _LEDGER_UNSET:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "model-cost-ledger.py")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("model_cost_ledger", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _MODEL_LEDGER_MOD = mod
        except Exception:
            _MODEL_LEDGER_MOD = None
    return _MODEL_LEDGER_MOD


_LEDGER_UNSET = object()
_MODEL_LEDGER_MOD = _LEDGER_UNSET


def model_cost_context(rolling_resets_at=None):
    """Best-effort per-model cost block for the gate snapshot.

    Returns (context_dict_or_None, warning_list).  Opportunistically syncs
    the ledger (max one sync per 20 min, cron cadence is 30 min so normally
    every gate run refreshes it).  Any error degrades to (None, []).
    """
    try:
        mod = _model_ledger_module()
        if mod is None:
            return None, []
        mod.sync_model_cost_ledger_if_due()
        shares = mod.current_window_shares(reset_at=rolling_resets_at)
        warns = mod.model_window_warnings(reset_at=rolling_resets_at)
        return (shares if shares and shares.get("models") else None), warns
    except Exception:
        return None, []


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
    # Distinct state: "burning-balance" (money burn) vs a hard "blocked"
    # (no fallback / balance exhausted). With balance-fallback the requests
    # keep succeeding past 100% — that is NOT a hard stop, it is spend.
    state = "burning-balance" if burning_balance else "ok"

    return {
        "profile": "pr-opencode",
        "provider": "opencode-go",
        "model": PROFILE_MODELS["pr-opencode"],
        "availability": round(availability, 1),
        "bottleneck_pct": round(bottleneck_pct, 1),
        "bottleneck_window": bottleneck_window,
        "burning_balance": burning_balance,
        "state": state,
        # Soft cap placeholder (t_47640f18): a configurable ceiling for the
        # monthly Zen credits acceptable on autonomous tasks. The exact USD
        # value is PENDING USER DECISION — leave unset (None) until the user
        # names an amount. Read from OPENCODE_GO_ZEN_MONTHLY_SOFT_CAP USD.
        "zen_monthly_soft_cap_usd": _config_zen_monthly_soft_cap(),
        "error": "",
        "raw": {
            "rolling_pct": rolling_pct,
            "weekly_pct": weekly_pct,
            "monthly_pct": monthly_pct,
            "rolling_status": raw.get("rolling_status"),
            "weekly_status": raw.get("weekly_status"),
            "monthly_status": raw.get("monthly_status"),
            "rolling_resets_at": raw.get("rolling_resets_at"),
            "weekly_resets_at": raw.get("weekly_resets_at"),
            "monthly_resets_at": raw.get("monthly_resets_at"),
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


# ---------------------------------------------------------------------------#
# Privacy summary — active-task tag census (OBJ-18 S1)
# ---------------------------------------------------------------------------#
# Maps each recognised raw privacy tag value to its summary bucket
# (high / medium / low).  This is the reverse of the high/medium/low →
# canonical mapping in _normalise_privacy_value: it lets the gate report
# a census of active tasks grouped by the OBJ-18 alias namespace, even
# when a task body uses the canonical form (privacy:sensitive → high,
# privacy:public → low) or a legacy abbreviation.
_PRIVACY_SUMMARY_BUCKETS = {
    # OBJ-18 aliases (the primary tag format per the routing matrix)
    "high": "high",
    "medium": "medium",
    "low": "low",
    # Canonical levels → closest OBJ-18 bucket
    "sensitive": "high",       # high and sensitive share the strictest cloud lane
    "public": "low",           # low and public share the unrestricted lane
    "confidential": "high",    # strictest overall — group with high
    # Legacy abbreviations / Spanish aliases (→ canonical → bucket)
    "pub": "low",
    "publico": "low",
    "sens": "high",
    "selectivo": "high",
    "conf": "high",
    "intimo": "high",
}

# Active task statuses — the privacy summary only counts tasks that are
# not in a terminal state (done / archived).  Triaged tasks are included
# because they represent pending work that will eventually run.
_ACTIVE_STATUSES_FOR_PRIVACY = ("ready", "running", "blocked", "todo", "triage")

# ---------------------------------------------------------------------------
# Deterministic zombie guard (OBJ-21, t_e793b2b9, Sep 2026)
# ---------------------------------------------------------------------------
# Guardrail G3 of the autonomous-task-creator prompt ("if any running task is
# older than 45 minutes → respond [SILENT]") used to live ONLY in the prompt,
# so its enforcement was stochastic: one tick may apply it, the next may
# mis-read the board and feed work anyway.  The gate now computes the same
# check deterministically and forces wakeAgent:false when a zombie exists,
# regardless of what the LLM sees.  The prompt instruction stays as a second
# line of defence.
#
# Threshold 45 min mirrors G3 (raised from 10 min on Sep 7 2026 — normal
# workers run 12–40 min, so 10 min produced false zombies and silenced the
# creator all night).
#
# Age base follows health_checks.check_zombie_workers(): prefer the worker's
# last_heartbeat_at (liveness), fall back to started_at when no heartbeat has
# been recorded yet.  A task with BOTH timestamps NULL counts as a zombie
# (undispatched/undated running row).  Measuring age from started_at of an
# already-COMPLETED task is meaningless — the query only looks at
# status='running' rows (that exact mistake produced the false "87 min
# zombie" report that motivated OBJ-21).
ZOMBIE_RUNNING_MINUTES = 45.0

# Delimiter-aware privacy tag regexes (OBJ-18 S1).
#
# The tag must start at a line start, after whitespace, or after an
# opening delimiter ( '(' '[' '*' — covers the creator's bold style
# ``**privacy:high**`` ), and the value must end at end-of-line,
# whitespace, or closing punctuation.  '|' is deliberately NOT a valid
# value char NOR a valid end delimiter: prose that MENTIONS the tag
# format (e.g. "privacy:high|medium|low" inside a task description)
# must NOT be counted as a tag — the '|' fails the match.
_PRIVACY_TAG_VALUE_RE = re.compile(
    r"(?:^|(?<=[\s(\[*]))privacy:[ \t]*['\"]?"
    r"([^\s'\",;.:|)\]}*]+)"
    r"['\"]?(?=$|[\s)\]}'\"*,;.:])",
    re.IGNORECASE,
)
_PRIVACY_TAG_EMPTY_RE = re.compile(
    r"(?:^|(?<=[\s(\[*]))privacy:[ \t]*(?=$|[\s)\]}'\"*,;.:])",
    re.IGNORECASE,
)


def _parse_privacy_tag_raw(text):
    """Extract the raw privacy tag value (un-normalised) from text.

    Unlike parse_privacy_tag() (line-start only, used for the routing
    level), this scans the WHOLE body so tags written inline the way
    the task creator writes them are also detected::

        privacy:high                     (own line)
        **privacy:high**                 (bold, creator style)
        objective:OBJ-18 privacy:high   (mid-line, space-delimited)
        (privacy:low)                    (parenthesised)

    Delimiter-aware: the tag must start after a line start, whitespace,
    ``(``, ``[`` or ``*`` and the value must end at end-of-line,
    whitespace or a closing delimiter (``) ] } ' " * , ; . :``).
    Prose that MENTIONS the tag format (e.g. ``privacy:high|medium|low``
    in a description) does not match — the ``|`` after the value fails
    the end-delimiter check at every backtrack position.

    Returns:
      * the lowercased raw value string (e.g. "high") for a
        syntactically well-formed tag — even if the value is not a
        recognised level (the caller warns for those);
      * "" for a tag present with an EMPTY value (``privacy:`` alone);
      * None when no ``privacy:`` tag is present (or only prose
        mentions that don't satisfy the delimiters).
    """
    if not text:
        return None
    m = _PRIVACY_TAG_VALUE_RE.search(text)
    if m:
        return m.group(1).lower()
    if _PRIVACY_TAG_EMPTY_RE.search(text):
        # "privacy:" present but no value where one is expected
        return ""
    return None


def compute_privacy_summary(kanban_db_path=None, warnings=None):
    """Census the privacy: tags of active tasks in kanban.db (OBJ-18 S1).

    Scans the body of every non-terminal task (ready / running / blocked
    / todo / triage) for a ``privacy:<level>`` tag and counts them into
    the OBJ-18 alias buckets::

        {"high": N, "medium": N, "low": N, "none": N}

    * Tasks with no ``privacy:`` tag count as ``none``.
    * Tasks with a recognised tag (high/medium/low or their canonical /
      alias equivalents) count in the corresponding bucket.
    * Tasks with a malformed tag (e.g. ``privacy:xyz``) count as
      ``none`` AND emit a warning via the *warnings* list, so the gate
      snapshot surfaces the bad tag without breaking.

    This is a READ-ONLY census: it does NOT change the recommendation
    logic, the privacy_level field, or provider routing.  Routing by
    privacy is S2 (separate task, after user approval of the matrix).

    Args:
        kanban_db_path: Path to kanban.db.  Defaults to
            ``$HERMES_KANBAN_DB`` env var, then ``~/.hermes/kanban.db``.
        warnings: Optional list to append warning strings to.

    Returns:
        dict with keys "high", "medium", "low", "none" (all ints >= 0).
    """
    summary = {"high": 0, "medium": 0, "low": 0, "none": 0}
    if warnings is None:
        warnings = []

    # Resolve the kanban.db path
    if kanban_db_path is None:
        kanban_db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not kanban_db_path:
        kanban_db_path = os.path.expanduser("~/.hermes/kanban.db")

    if not os.path.isfile(kanban_db_path):
        # No kanban.db — nothing to census.  Not an error.
        return summary

    try:
        import sqlite3
        conn = sqlite3.connect(kanban_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(_ACTIVE_STATUSES_FOR_PRIVACY))
        cursor.execute(
            f"SELECT id, body FROM tasks WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES_FOR_PRIVACY,
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as exc:
        # DB error — fail open: return empty summary + warning.
        warnings.append(f"privacy_summary: kanban.db read failed: {exc}")
        return summary

    for row in rows:
        task_id = row["id"]
        body = row["body"] or ""
        raw = _parse_privacy_tag_raw(body)
        if raw is None:
            # No privacy: tag → none
            summary["none"] += 1
        elif raw == "":
            # "privacy:" with no value → malformed
            summary["none"] += 1
            warnings.append(
                f"privacy_summary: task {task_id} has empty privacy: tag "
                f"(ignored, counted as none)"
            )
        else:
            bucket = _PRIVACY_SUMMARY_BUCKETS.get(raw)
            if bucket:
                summary[bucket] += 1
            else:
                # Unrecognised tag value → malformed, ignore with warning
                summary["none"] += 1
                warnings.append(
                    f"privacy_summary: task {task_id} has unrecognised "
                    f"privacy:{raw} tag (ignored, counted as none)"
                )

    return summary


def compute_zombie_check(kanban_db_path=None, warnings=None, now=None):
    """Deterministic G3 zombie guard (OBJ-21, t_e793b2b9, Sep 2026).

    Scans kanban.db for tasks with status ``running`` whose live age
    exceeds ZOMBIE_RUNNING_MINUTES (45 min — the G3 threshold), so the
    creator-silence decision no longer depends on the LLM reading the
    board correctly.  Age is measured ONLY from live running rows,
    preferring ``last_heartbeat_at`` (worker liveness) over
    ``started_at`` — measuring from the started_at of a completed task
    is what produced the false "87 min zombie" report behind OBJ-21.

    Args:
        kanban_db_path: Path to kanban.db.  Defaults to
            ``$HERMES_KANBAN_DB`` env var, then ``~/.hermes/kanban.db``.
        warnings: Optional list to append warning strings to.
        now: Frozen epoch seconds (tests); defaults to time.time().

    Returns:
        dict: {"has_zombie": bool, "count": int, "threshold_minutes": 45.0,
               "tasks": [ {id, title, assignee, age_minutes,
                           age_source, minutes_over_threshold}, ... ]}
        Tasks sorted by age descending, capped at the 5 oldest (the
        decision signal and the worst offenders, not a full census).
    """
    if warnings is None:
        warnings = []
    result = {
        "has_zombie": False,
        "count": 0,
        "threshold_minutes": ZOMBIE_RUNNING_MINUTES,
        "tasks": [],
    }
    if now is None:
        now = time.time()

    # Resolve the kanban.db path (same order as compute_privacy_summary)
    if kanban_db_path is None:
        kanban_db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not kanban_db_path:
        kanban_db_path = os.path.expanduser("~/.hermes/kanban.db")

    if not os.path.isfile(kanban_db_path):
        # No kanban.db — nothing to guard.  Not an error, fail open.
        return result

    try:
        import sqlite3
        conn = sqlite3.connect(kanban_db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, assignee, started_at, last_heartbeat_at "
            "FROM tasks WHERE status = 'running'"
        ).fetchall()
        conn.close()
    except Exception as exc:
        # DB error — fail open (no zombie declared) + warning.
        warnings.append(f"zombie_check: kanban.db read failed: {exc}")
        return result

    zombies = []
    for row in rows:
        hb = row["last_heartbeat_at"]
        started = row["started_at"]
        if hb is not None:
            base, source = float(hb), "heartbeat"
        elif started is not None:
            base, source = float(started), "started_at"
        else:
            # No heartbeat and no started_at — undated running row,
            # treat as zombie (health_checks precedent).
            base, source = 0, "unknown"

        age_minutes = max(0.0, (now - base) / 60.0)
        if age_minutes > ZOMBIE_RUNNING_MINUTES + 1e-9:
            zombies.append({
                "id": row["id"],
                "title": row["title"],
                "assignee": row["assignee"] or "unknown",
                "age_minutes": round(age_minutes, 1),
                "age_source": source,
                "minutes_over_threshold": round(
                    age_minutes - ZOMBIE_RUNNING_MINUTES, 1
                ),
            })

    zombies.sort(key=lambda t: t["age_minutes"], reverse=True)
    result["count"] = len(zombies)
    result["has_zombie"] = bool(zombies)
    # Signal + worst offenders only; a full census is not needed to
    # silence the creator (and keeps the snapshot small).
    result["tasks"] = zombies[:5]
    return result


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

    # --- Per-model cost ledger (MULTI-PROV-09, t_5bdd7cfa) ---
    # Opportunistic sync + per-window shares/warnings.  Reuse the gate's
    # own live rolling_resets_at (from the query above) so no second API
    # call is needed for window anchoring.  Never fatal.
    _ocg_reset = None
    for p in providers_list:
        if p.get("provider") == "opencode-go":
            _ocg_reset = (p.get("raw") or {}).get("rolling_resets_at")
    model_cost, model_cost_warnings = model_cost_context(_ocg_reset)
    for w in model_cost_warnings:
        warnings.append(w)

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

    # --- Deterministic zombie guard (OBJ-21) ---
    # G3 of the creator prompt (any running task > 45 min → [SILENT])
    # enforced HERE, deterministically: if a zombie exists the gate forces
    # wakeAgent:false regardless of the recommendation.  Computed once,
    # before both output branches, so the context always carries it.
    zombie_check = compute_zombie_check(warnings=warnings)
    if zombie_check["has_zombie"]:
        worst = zombie_check["tasks"][0]
        warnings.append(
            f"zombie_guard: {zombie_check['count']} task(s) running >"
            f"{int(ZOMBIE_RUNNING_MINUTES)} min "
            f"(oldest {worst['id']} {worst['age_minutes']}min via "
            f"{worst['age_source']}) — creator silenced (G3 deterministic)"
        )

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

    # --- Predictor EMA (OBJ-24 F2) — modo --suggest: observador ---
    # Solo se activa con --suggest (wrapper forecast-gate.sh); el gate del
    # cron (sin flags) sigue produciendo el mismo JSON que antes — cero
    # regresión. forecast.json ausente/corrupto -> None (silencio).
    forecast_warning = forecast_context(load_forecast()) if _SUGGEST else None

    if recommended is None or zombie_check["has_zombie"]:
        # All providers exhausted/errored, no allowed profile exists,
        # privacy filtering eliminated all candidates — OR the
        # deterministic zombie guard fired (OBJ-21: G3 enforced here).
        privacy_summary = compute_privacy_summary(warnings=warnings)
        if recommended is not None:
            # Zombie guard overrode a live recommendation: keep the
            # recommendation fields visible in the context so the SILENT
            # rationale is auditable, but never wake the agent.
            rec_profile = recommended["profile"]
            rec_model = recommended["model"]
            rec_cost = bottleneck_to_max_cost(recommended["bottleneck_pct"])
            rec_workers = bottleneck_to_max_workers(recommended["bottleneck_pct"])
        else:
            rec_profile = None
            rec_model = None
            rec_cost = None
            rec_workers = 0
        output = {
            "wakeAgent": False,
            "context": {
                "providers": providers_list,
                "recommended_profile": rec_profile,
                "recommended_model": rec_model,
                "max_task_cost": rec_cost,
                "max_workers": rec_workers,
                "privacy_level": privacy_level or "none",
                "privacy_summary": privacy_summary,
                "zombie_check": zombie_check,
                "valid_profiles": valid_profiles,
                "warning": "; ".join(warnings) if warnings else "all providers exhausted",
                "burn_warnings": load_burn_warnings(),
            },
        }
        if model_cost:
            output["context"]["model_cost"] = model_cost
        if forecast_warning:
            output["context"]["forecast_warning"] = forecast_warning
        print(json.dumps(output))
        return

    # --- Build output ---
    bottleneck = recommended["bottleneck_pct"]
    max_cost = bottleneck_to_max_cost(bottleneck)
    max_workers = bottleneck_to_max_workers(bottleneck)

    # Privacy summary (OBJ-18 S1) — computed BEFORE the warning string is
    # frozen so any malformed-tag warnings it emits are surfaced in the
    # output ``warning`` field.  Read-only census; does NOT affect routing.
    privacy_summary = compute_privacy_summary(warnings=warnings)

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

    # Burning-balance WARNING (t_47640f18): surface it in the snapshot even
    # when pr-opencode is NOT the recommended provider (it has availability 0
    # while burning, so it is never recommended) — otherwise the money-burn
    # state would be invisible to the task creator. Explicitly notes that
    # burning-balance means spending prepaid Zen, NOT free subscription quota.
    for p in providers_list:
        if p.get("provider") == "opencode-go" and p.get("burning_balance"):
            note = (f"{p['profile']} state=burning-balance: an OpenCode Go window "
                    f"is exhausted and balance-fallback is spending PREPAID ZEN "
                    f"credits (money), not free quota. No new work routed there "
                    f"until reset.")
            if warning:
                warning += "; " + note
            else:
                warning = note

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
            "privacy_summary": privacy_summary,
            # OBJ-21: always present.  Reaches this branch only when
            # has_zombie is False (a zombie forces the early return above).
            "zombie_check": zombie_check,
            "valid_profiles": valid_profiles,
            "warning": warning,
            "burn_warnings": load_burn_warnings(),
            "model_cost": model_cost,
        },
    }
    if forecast_warning:
        output["context"]["forecast_warning"] = forecast_warning
    print(json.dumps(output))


if __name__ == "__main__":
    main()