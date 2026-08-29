"""Multi-provider quota query layer.

Each provider has a different API surface for quota/usage. This module
normalises them into a common ``QuotaSnapshot`` dataclass so the rest of
the governor never needs to know which provider it's talking to.

Supported providers (Aug 2026):
  - Ollama Cloud  — GET /api/usage (session + weekly, fractional)
  - NanoGPT       — GET /api/subscription/v1/usage (daily + monthly + weekly tokens)
  - OpenRouter    — GET /api/v1/key (USD-denominated)

Only Ollama Cloud has session/weekly windows that map cleanly to the
governor's "should I keep spawning workers?" decision. NanoGPT and
OpenRouter are reported as informational context.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


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
        # pr-ollama as a legacy fallback, then the global .env.
        hermes_home = os.environ.get("HERMES_HOME", "").strip()
        candidates = []
        if hermes_home:
            candidates.append(os.path.join(hermes_home, ".env"))
        candidates.append(os.path.expanduser("~/.hermes/profiles/pr-ollama/.env"))
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

    req = urllib.request.Request(
        "https://ollama.com/api/usage",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

    session = data.get("limits", {}).get("session", {})
    weekly = data.get("limits", {}).get("weekly", {})

    # Pay-as-you-go spend (activity.cost — a string like "0.24" when present)
    activity = data.get("activity")
    activity_cost = 0.0
    if activity and isinstance(activity, dict):
        cost = activity.get("cost")
        if cost is not None:
            activity_cost = float(cost)

    return {
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


def query_nanogpt() -> dict:
    """Query NanoGPT subscription usage (informational)."""
    api_key = _get_env("NANO_GPT_API_KEY")
    if not api_key:
        return {"state": None, "daily_pct": None, "weekly_tokens_pct": None}

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


def query_openrouter() -> dict:
    """Query OpenRouter key usage (informational, USD)."""
    api_key = _get_env("OPENROUTER_API_KEY")
    if not api_key:
        return {"usage_weekly_usd": None, "usage_monthly_usd": None}

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with _no_proxy():
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode()).get("data", {})

    weekly = data.get("usage_weekly")
    monthly = data.get("usage_monthly")
    return {
        "usage_weekly_usd": float(weekly) if weekly is not None else None,
        "usage_monthly_usd": float(monthly) if monthly is not None else None,
    }


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

    return snapshot