"""Multi-provider quota query layer.

Each provider has a different API surface for quota/usage. This module
normalises them into a common ``QuotaSnapshot`` dataclass so the rest of
the governor never needs to know which provider it's talking to.

Supported providers (Sep 2026):
  - Ollama Cloud  — GET /api/usage (session + weekly, fractional)
  - NanoGPT       — GET /api/subscription/v1/usage (daily + monthly + weekly tokens)
  - OpenRouter    — GET /api/v1/key (USD-denominated)
  - OpenCode Go   — GET /zen/go/v1/usage (rolling + weekly + monthly, percent)

Only Ollama Cloud has session/weekly windows that map cleanly to the
governor's "should I keep spawning workers?" decision. NanoGPT,
OpenRouter and OpenCode Go are reported as informational context.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Transient HTTP error retry + last-known-good cache
# --------------------------------------------------------------------------- #
# When a provider API returns a transient error (403 from a WAF/CDN, 429 rate
# limit, 5xx server error), retrying after a short backoff often succeeds.
# If all retries fail, fall back to the last known good values cached on disk
# so the provider is not zeroed out of routing by a blip.
#
# Verified Sep 2026 (OBJ-20): NanoGPT returned HTTP 403 four times in ~2.5h
# on Aug 30, likely a transient Vercel WAF incident. The _no_proxy() fix was
# already in place. The 403 auto-resolved but zeroed NanoGPT availability
# each time, degrading multi-provider routing unnecessarily.

_TRANSIENT_HTTP_CODES = {403, 429, 500, 502, 503, 504}
_RETRY_DELAY = 1.0  # seconds between retries
_MAX_RETRIES = 2    # initial attempt + 2 retries = 3 total tries


def _retry_http(fn, *, retries=_MAX_RETRIES, delay=_RETRY_DELAY):
    """Retry *fn* on transient HTTP errors.

    *fn* must return parsed JSON data (dict).  Raises the last exception
    if all retries are exhausted.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_HTTP_CODES and attempt < retries:
                logger.debug(
                    "transient HTTP %d on attempt %d/%d, retrying in %.1fs",
                    exc.code, attempt + 1, retries + 1, delay,
                )
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                logger.debug(
                    "transient %s on attempt %d/%d, retrying in %.1fs",
                    type(exc).__name__, attempt + 1, retries + 1, delay,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _cache_dir() -> str:
    """Return the quota-governor state directory for cache files."""
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    base = hermes_home if hermes_home else os.path.expanduser("~/.hermes")
    return os.path.join(base, "quota-governor")


def _read_cache(provider: str) -> Optional[dict]:
    """Read last-known-good values for *provider* from the cache file."""
    path = os.path.join(_cache_dir(), f"{provider}-last-good.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(provider: str, data: dict) -> None:
    """Persist last-known-good values for *provider* to the cache file."""
    path = os.path.join(_cache_dir(), f"{provider}-last-good.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        logger.debug("failed to write %s cache: %s", provider, exc)


@dataclass
class QuotaSnapshot:
    """Normalised quota state across all configured providers."""

    # Ollama Cloud (primary — drives the governor decision)
    ollama_session_pct: float = 0.0    # 0-100
    ollama_weekly_pct: float = 0.0     # 0-100
    ollama_session_requests: int = 0
    ollama_weekly_requests: int = 0
    ollama_activity_cost: float = 0.0   # pay-as-you-go spend (activity.cost from /api/usage)

    # NanoGPT (informational)
    nanogpt_daily_pct: Optional[float] = None
    nanogpt_weekly_tokens_pct: Optional[float] = None
    nanogpt_state: Optional[str] = None

    # OpenRouter (informational, USD)
    openrouter_usage_weekly_usd: Optional[float] = None
    openrouter_usage_monthly_usd: Optional[float] = None

    # OpenCode Go (informational)
    opencode_go_rolling_pct: Optional[float] = None
    opencode_go_weekly_pct: Optional[float] = None
    opencode_go_monthly_pct: Optional[float] = None

    # Metadata
    timestamp: str = ""
    errors: list = field(default_factory=list)

    @property
    def session_pct(self) -> float:
        """Primary session usage percentage (Ollama)."""
        return self.ollama_session_pct

    @property
    def weekly_pct(self) -> float:
        """Primary weekly usage percentage (Ollama)."""
        return self.ollama_weekly_pct

    def has_errors(self) -> bool:
        return bool(self.errors)


# ---------------------------------------------------------------------------
# Ollama Cloud
# ---------------------------------------------------------------------------

def _get_env(key: str, env_file: Optional[str] = None) -> Optional[str]:
    """Read a variable from the environment, falling back to a .env file.

    The .env file is sourced manually — we don't trust the shell env to
    have it loaded in all contexts (cron, plugin, etc).
    """
    val = os.environ.get(key)
    if val:
        return val

    if env_file is None:
        # Try the active profile's .env first (HERMES_HOME), then
        # pr-ollama as a legacy fallback, then pr-opencode (OpenCode Go
        # key lives there — MULTI-PROV-06), then the global .env.
        hermes_home = os.environ.get("HERMES_HOME", "").strip()
        candidates = []
        if hermes_home:
            candidates.append(os.path.join(hermes_home, ".env"))
        candidates.append(os.path.expanduser("~/.hermes/profiles/pr-ollama/.env"))
        candidates.append(os.path.expanduser("~/.hermes/profiles/pr-opencode/.env"))
        candidates.append(os.path.expanduser("~/.hermes/.env"))
        for path in candidates:
            if os.path.isfile(path):
                val = _read_env_file(path, key)
                if val:
                    return val
    elif os.path.isfile(env_file):
        return _read_env_file(env_file, key)

    return None


def _read_env_file(path: str, key: str) -> Optional[str]:
    """Parse a .env file for the first uncommented ``KEY=value`` line."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return None


import contextlib


@contextlib.contextmanager
def _no_proxy():
    """Temporarily disable HTTP/HTTPS proxy env vars.

    The profile .env sets ``https_proxy`` for GitHub Tor enforcement, but
    Ollama Cloud (and NanoGPT/OpenRouter) reject connections from Tor exit
    nodes. This context manager unsets the proxy vars for the duration of
    the HTTP call so urllib goes direct.

    Verified Aug 2026: Ollama returns ``501 Tor is not an HTTP Proxy``
    when the proxy is active; unsetting it fixes the call.
    """
    saved = {}
    proxy_vars = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                  "all_proxy", "ALL_PROXY"]
    for var in proxy_vars:
        if var in os.environ:
            saved[var] = os.environ.pop(var)
    try:
        yield
    finally:
        os.environ.update(saved)


def query_ollama() -> dict:
    """Query Ollama Cloud /api/usage.

    Returns a dict with session_pct, weekly_pct, session_requests,
    weekly_requests, or raises on error.
    """
    api_key = _get_env("OLLAMA_API_KEY")
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY not found in env or .env")

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

    # Pay-as-you-go spend (activity.cost — a string like "0.24" when present)
    activity = data.get("activity")
    activity_cost = 0.0
    if activity and isinstance(activity, dict):
        cost = activity.get("cost")
        if cost is not None:
            activity_cost = float(cost)

    result = {
        "session_pct": float(session.get("usage", 0)) * 100,
        "weekly_pct": float(weekly.get("usage", 0)) * 100,
        "session_requests": sum(
            m.get("request_count", 0) for m in session.get("models", [])
        ),
        "weekly_requests": sum(
            m.get("request_count", 0) for m in weekly.get("models", [])
        ),
        "activity_cost": activity_cost,
    }
    _write_cache("ollama", result)
    return result


def query_nanogpt() -> dict:
    """Query NanoGPT subscription usage (informational).

    On transient HTTP errors after all retries, falls back to the
    last-known-good cached values so the provider is not zeroed out
    of routing by a transient API blip.
    """
    api_key = _get_env("NANO_GPT_API_KEY")
    if not api_key:
        return {"state": None, "daily_pct": None, "weekly_tokens_pct": None}

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
    except Exception as exc:
        cached = _read_cache("nanogpt")
        if cached:
            logger.debug("nanogpt query failed, using cached values: %s", exc)
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


def query_openrouter() -> dict:
    """Query OpenRouter key usage (informational, USD).

    Falls back to last-known-good cached values on transient errors.
    """
    api_key = _get_env("OPENROUTER_API_KEY")
    if not api_key:
        return {"usage_weekly_usd": None, "usage_monthly_usd": None}

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
    except Exception as exc:
        cached = _read_cache("openrouter")
        if cached:
            logger.debug("openrouter query failed, using cached values: %s", exc)
            cached["_cached"] = True
            return cached
        raise

    weekly = data.get("usage_weekly")
    monthly = data.get("usage_monthly")
    result = {
        "usage_weekly_usd": float(weekly) if weekly is not None else None,
        "usage_monthly_usd": float(monthly) if monthly is not None else None,
    }
    _write_cache("openrouter", result)
    return result


def query_opencode_go() -> dict:
    """Query OpenCode Go usage (informational).

    Endpoint: GET https://opencode.ai/zen/go/v1/usage
    Response shape (verified Sep 2026, MULTI-PROV-06):

        {"usage": {
            "rolling":  {"status": "ok", "percent": 5, "resetsAt": "..."},
            "weekly":   {"status": "ok", "percent": 2, "resetsAt": "..."},
            "monthly":  {"status": "ok", "percent": 1, "resetsAt": "..."}
        }}

    Unlike Ollama (0-1 fraction) and NanoGPT (percentUsed fraction),
    ``percent`` is ALREADY 0-100 — no multiplication.

    The API key lives in the pr-opencode profile .env. Falls back to
    last-known-good cached values on transient errors (same pattern as
    NanoGPT/OpenRouter, OBJ-20).

    Pitfall (verified Sep 2026, MULTI-PROV-06): opencode.ai sits behind
    Cloudflare, which returns ``403 error code: 1010`` (browser
    signature ban) for urllib's default ``Python-urllib/x.y``
    User-Agent. A custom UA header is required.
    """
    api_key = _get_env("OPENCODE_GO_API_KEY")
    if not api_key:
        return {
            "rolling_pct": None,
            "weekly_pct": None,
            "monthly_pct": None,
        }

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
    except Exception as exc:
        cached = _read_cache("opencode_go")
        if cached:
            logger.debug("opencode_go query failed, using cached values: %s", exc)
            cached["_cached"] = True
            return cached
        raise

    usage = data.get("usage", {})
    rolling = usage.get("rolling", {})
    weekly = usage.get("weekly", {})
    monthly = usage.get("monthly", {})

    def _pct(window: dict) -> Optional[float]:
        """Extract percent (already 0-100) from a window dict."""
        val = window.get("percent")
        return float(val) if val is not None else None

    result = {
        "rolling_pct": _pct(rolling),
        "weekly_pct": _pct(weekly),
        "monthly_pct": _pct(monthly),
    }
    _write_cache("opencode_go", result)
    return result


# ---------------------------------------------------------------------------
# Unified query
# ---------------------------------------------------------------------------

def query_all() -> QuotaSnapshot:
    """Query all configured providers and return a normalised snapshot.

    If a provider fails, its fields stay at defaults and the error is
    recorded. The snapshot is always returned — partial data is better
    than no data.
    """
    from datetime import datetime, timezone

    snapshot = QuotaSnapshot(timestamp=datetime.now(timezone.utc).isoformat())

    # Ollama (primary)
    try:
        ollama = query_ollama()
        snapshot.ollama_session_pct = ollama["session_pct"]
        snapshot.ollama_weekly_pct = ollama["weekly_pct"]
        snapshot.ollama_session_requests = ollama["session_requests"]
        snapshot.ollama_weekly_requests = ollama["weekly_requests"]
        snapshot.ollama_activity_cost = ollama.get("activity_cost", 0.0)
    except Exception as exc:
        snapshot.errors.append(f"ollama: {exc}")
        logger.debug("ollama quota query failed: %s", exc)

    # NanoGPT (informational)
    try:
        nanogpt = query_nanogpt()
        snapshot.nanogpt_daily_pct = nanogpt.get("daily_pct")
        snapshot.nanogpt_weekly_tokens_pct = nanogpt.get("weekly_tokens_pct")
        snapshot.nanogpt_state = nanogpt.get("state")
    except Exception as exc:
        snapshot.errors.append(f"nanogpt: {exc}")
        logger.debug("nanogpt quota query failed: %s", exc)

    # OpenRouter (informational)
    try:
        openrouter = query_openrouter()
        snapshot.openrouter_usage_weekly_usd = openrouter.get("usage_weekly_usd")
        snapshot.openrouter_usage_monthly_usd = openrouter.get("usage_monthly_usd")
    except Exception as exc:
        snapshot.errors.append(f"openrouter: {exc}")
        logger.debug("openrouter quota query failed: %s", exc)

    # OpenCode Go (informational)
    try:
        opencode_go = query_opencode_go()
        snapshot.opencode_go_rolling_pct = opencode_go.get("rolling_pct")
        snapshot.opencode_go_weekly_pct = opencode_go.get("weekly_pct")
        snapshot.opencode_go_monthly_pct = opencode_go.get("monthly_pct")
    except Exception as exc:
        snapshot.errors.append(f"opencode_go: {exc}")
        logger.debug("opencode_go quota query failed: %s", exc)

    return snapshot