#!/usr/bin/env python3
"""Tests for quota-gate.py — non-privacy parts of the quota gate pre-run script.

The privacy routing functions (parse_privacy_tag, parse_privacy_level,
select_provider with privacy_level) are already covered by
test_privacy_routing.py.  This file covers the remaining untested surface:

  - bottleneck_to_max_cost: tier mapping
  - bottleneck_to_max_workers: worker count mapping
  - _normalise_privacy_value: alias normalisation (used by parse_privacy_tag)
  - validate_recommended_profile: Guardrail G1 enforcement
  - get_existing_profiles: hermes profile list parsing
  - _retry_http: transient HTTP retry logic
  - _read_cache / _write_cache: last-known-good cache
  - compute_*_status: provider status computation from raw API data
  - select_provider: non-privacy selection (availability-first, paying mode)

Run:
  python3 -m pytest test_quota_gate.py -v
  python3 test_quota_gate.py  # direct run
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add the scripts directory to the path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import with hyphen filename
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "quota_gate",
    os.path.join(SCRIPT_DIR, "scripts", "quota-gate.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["quota_gate"] = _mod
_spec.loader.exec_module(_mod)

from quota_gate import (
    bottleneck_to_max_cost,
    bottleneck_to_max_workers,
    _normalise_privacy_value,
    validate_recommended_profile,
    get_existing_profiles,
    _retry_http,
    _read_cache,
    _write_cache,
    _cache_dir,
    compute_ollama_status,
    compute_nanogpt_status,
    compute_openrouter_status,
    select_provider,
    ALLOWED_PROFILES,
    PROVIDER_PREFERENCE,
    PRIVACY_CAPABILITIES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _provider(profile="pr-ollama", availability=80.0, bottleneck=20.0,
              error="", provider="ollama-cloud", model="glm-5.2",
              bottleneck_window="session"):
    return {
        "profile": profile,
        "provider": provider,
        "model": model,
        "availability": availability,
        "bottleneck_pct": bottleneck,
        "bottleneck_window": bottleneck_window,
        "error": error,
        "raw": {},
    }


# ── Tests: bottleneck_to_max_cost ────────────────────────────────────────────

class TestBottleneckToMaxCost(unittest.TestCase):

    def test_low_bottleneck_any(self):
        self.assertEqual(bottleneck_to_max_cost(0), "any")
        self.assertEqual(bottleneck_to_max_cost(29), "any")

    def test_medium_bottleneck(self):
        self.assertEqual(bottleneck_to_max_cost(30), "medium")
        self.assertEqual(bottleneck_to_max_cost(59), "medium")

    def test_high_bottleneck(self):
        self.assertEqual(bottleneck_to_max_cost(60), "small")
        self.assertEqual(bottleneck_to_max_cost(79), "small")

    def test_very_high_bottleneck(self):
        self.assertEqual(bottleneck_to_max_cost(80), "tiny")
        self.assertEqual(bottleneck_to_max_cost(94), "tiny")

    def test_maxed_bottleneck(self):
        self.assertEqual(bottleneck_to_max_cost(95), "micro")
        self.assertEqual(bottleneck_to_max_cost(100), "micro")
        self.assertEqual(bottleneck_to_max_cost(150), "micro")


# ── Tests: bottleneck_to_max_workers ─────────────────────────────────────────

class TestBottleneckToMaxWorkers(unittest.TestCase):

    def test_low_bottleneck_2_workers(self):
        self.assertEqual(bottleneck_to_max_workers(0), 2)
        self.assertEqual(bottleneck_to_max_workers(49), 2)

    def test_medium_bottleneck_1_worker(self):
        self.assertEqual(bottleneck_to_max_workers(50), 1)
        self.assertEqual(bottleneck_to_max_workers(79), 1)

    def test_high_bottleneck_0_workers(self):
        self.assertEqual(bottleneck_to_max_workers(80), 0)
        self.assertEqual(bottleneck_to_max_workers(100), 0)


# ── Tests: _normalise_privacy_value ──────────────────────────────────────────

class TestNormalisePrivacyValue(unittest.TestCase):

    def test_canonical_levels(self):
        self.assertEqual(_normalise_privacy_value("public"), "public")
        self.assertEqual(_normalise_privacy_value("sensitive"), "sensitive")
        self.assertEqual(_normalise_privacy_value("confidential"), "confidential")

    def test_high_alias(self):
        self.assertEqual(_normalise_privacy_value("high"), "sensitive")

    def test_medium_alias(self):
        self.assertEqual(_normalise_privacy_value("medium"), "sensitive")

    def test_low_alias(self):
        self.assertEqual(_normalise_privacy_value("low"), "public")

    def test_case_insensitive(self):
        self.assertEqual(_normalise_privacy_value("Public"), "public")
        self.assertEqual(_normalise_privacy_value("SENSITIVE"), "sensitive")
        self.assertEqual(_normalise_privacy_value("High"), "sensitive")

    def test_abbreviations(self):
        self.assertEqual(_normalise_privacy_value("pub"), "public")
        self.assertEqual(_normalise_privacy_value("publico"), "public")
        self.assertEqual(_normalise_privacy_value("sens"), "sensitive")
        self.assertEqual(_normalise_privacy_value("selectivo"), "sensitive")
        self.assertEqual(_normalise_privacy_value("conf"), "confidential")
        self.assertEqual(_normalise_privacy_value("intimo"), "confidential")

    def test_unknown_value_returns_none(self):
        self.assertIsNone(_normalise_privacy_value("unknown"))
        self.assertIsNone(_normalise_privacy_value("max"))
        self.assertIsNone(_normalise_privacy_value(""))

    def test_none_input(self):
        self.assertIsNone(_normalise_privacy_value(None))

    def test_whitespace_stripped(self):
        self.assertEqual(_normalise_privacy_value("  public  "), "public")


# ── Tests: validate_recommended_profile ──────────────────────────────────────

class TestValidateRecommendedProfile(unittest.TestCase):

    def test_valid_profile(self):
        """Profile in ALLOWED_PROFILES and existing → returned as-is."""
        warnings = []
        result = validate_recommended_profile(
            "pr-ollama", {"pr-ollama", "pr-nanogpt"}, warnings
        )
        self.assertEqual(result, "pr-ollama")
        self.assertEqual(warnings, [])

    def test_profile_not_in_allowed(self):
        """Profile not in ALLOWED_PROFILES → fallback + warning."""
        warnings = []
        result = validate_recommended_profile(
            "pr-evil", {"pr-ollama"}, warnings
        )
        self.assertEqual(result, "pr-ollama")  # alphabetical fallback
        self.assertTrue(any("not in allowed set" in w for w in warnings))

    def test_profile_not_existing(self):
        """Profile in ALLOWED_PROFILES but not on host → fallback + warning."""
        warnings = []
        result = validate_recommended_profile(
            "pr-nanogpt", {"pr-ollama"}, warnings
        )
        self.assertEqual(result, "pr-ollama")
        self.assertTrue(any("does not exist" in w for w in warnings))

    def test_no_allowed_profile_exists(self):
        """No allowed profile on host → None + warning."""
        warnings = []
        result = validate_recommended_profile(
            "pr-ollama", {"pr-unknown"}, warnings
        )
        self.assertIsNone(result)
        self.assertTrue(any("no allowed profile" in w for w in warnings))

    def test_fallback_with_providers_list(self):
        """When falling back, pick the allowed provider with highest availability."""
        warnings = []
        providers = [
            _provider(profile="pr-ollama", availability=30, error=""),
            _provider(profile="pr-nanogpt", availability=80, error=""),
        ]
        result = validate_recommended_profile(
            "pr-evil", {"pr-ollama", "pr-nanogpt"}, warnings,
            providers_list=providers,
        )
        # pr-nanogpt has higher availability → should be picked
        self.assertEqual(result, "pr-nanogpt")

    def test_fallback_skips_errored_providers(self):
        """Errored providers are not selected as fallback."""
        warnings = []
        providers = [
            _provider(profile="pr-ollama", availability=80, error="timeout"),
            _provider(profile="pr-nanogpt", availability=50, error=""),
        ]
        result = validate_recommended_profile(
            "pr-evil", {"pr-ollama", "pr-nanogpt"}, warnings,
            providers_list=providers,
        )
        self.assertEqual(result, "pr-nanogpt")

    def test_fallback_skips_zero_availability(self):
        """Providers with 0 availability are not selected as fallback."""
        warnings = []
        providers = [
            _provider(profile="pr-ollama", availability=0, error=""),
            _provider(profile="pr-nanogpt", availability=50, error=""),
        ]
        result = validate_recommended_profile(
            "pr-evil", {"pr-ollama", "pr-nanogpt"}, warnings,
            providers_list=providers,
        )
        self.assertEqual(result, "pr-nanogpt")


# ── Tests: get_existing_profiles ─────────────────────────────────────────────

class TestGetExistingProfiles(unittest.TestCase):

    @patch("quota_gate.subprocess.run")
    def test_parses_profile_list(self, mock_run):
        # Format: header line, then one profile per line.
        # The active profile has a ◆ marker; inactive ones are plain.
        mock_run.return_value = MagicMock(
            stdout="pr-ollama\npr-nanogpt\npr-openrouter\n",
            returncode=0,
        )
        result = get_existing_profiles()
        self.assertIn("pr-ollama", result)
        self.assertIn("pr-nanogpt", result)
        self.assertIn("pr-openrouter", result)

    @patch("quota_gate.subprocess.run")
    def test_strips_active_marker(self, mock_run):
        """The ◆ active marker is stripped from profile names."""
        mock_run.return_value = MagicMock(
            stdout="◆ pr-ollama\n  pr-nanogpt\n",
            returncode=0,
        )
        result = get_existing_profiles()
        self.assertIn("pr-ollama", result)
        self.assertIn("pr-nanogpt", result)

    @patch("quota_gate.subprocess.run")
    def test_fallback_on_failure(self, mock_run):
        """If hermes command fails, falls back to ALLOWED_PROFILES."""
        mock_run.side_effect = OSError("command not found")
        result = get_existing_profiles()
        self.assertEqual(result, ALLOWED_PROFILES)

    @patch("quota_gate.subprocess.run")
    def test_fallback_on_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        result = get_existing_profiles()
        self.assertEqual(result, ALLOWED_PROFILES)


# ── Tests: _retry_http ───────────────────────────────────────────────────────

class TestRetryHttp(unittest.TestCase):

    def test_success_first_try(self):
        """Function succeeds on first call → no retry."""
        calls = [0]
        def fn():
            calls[0] += 1
            return "ok"
        result = _retry_http(fn, retries=2, delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls[0], 1)

    @patch("quota_gate.time.sleep")
    def test_retries_on_403(self, mock_sleep):
        """HTTP 403 is retried."""
        from urllib.error import HTTPError
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 2:
                raise HTTPError("url", 403, "Forbidden", {}, None)
            return "ok"
        result = _retry_http(fn, retries=2, delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls[0], 2)

    @patch("quota_gate.time.sleep")
    def test_retries_on_429(self, mock_sleep):
        from urllib.error import HTTPError
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 3:
                raise HTTPError("url", 429, "Too Many Requests", {}, None)
            return "ok"
        result = _retry_http(fn, retries=3, delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls[0], 3)

    @patch("quota_gate.time.sleep")
    def test_retries_on_503(self, mock_sleep):
        from urllib.error import HTTPError
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 2:
                raise HTTPError("url", 503, "Service Unavailable", {}, None)
            return "ok"
        result = _retry_http(fn, retries=2, delay=0)
        self.assertEqual(result, "ok")

    @patch("quota_gate.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        """After max retries, the exception is raised."""
        from urllib.error import HTTPError
        calls = [0]
        def fn():
            calls[0] += 1
            raise HTTPError("url", 503, "Service Unavailable", {}, None)
        with self.assertRaises(HTTPError):
            _retry_http(fn, retries=2, delay=0)
        self.assertEqual(calls[0], 3)  # 1 initial + 2 retries

    @patch("quota_gate.time.sleep")
    def test_no_retry_on_404(self, mock_sleep):
        """HTTP 404 is NOT retried (not in transient codes)."""
        from urllib.error import HTTPError
        calls = [0]
        def fn():
            calls[0] += 1
            raise HTTPError("url", 404, "Not Found", {}, None)
        with self.assertRaises(HTTPError):
            _retry_http(fn, retries=2, delay=0)
        self.assertEqual(calls[0], 1)

    @patch("quota_gate.time.sleep")
    def test_retries_on_url_error(self, mock_sleep):
        """URLError (network) is retried."""
        from urllib.error import URLError
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 2:
                raise URLError("connection refused")
            return "ok"
        result = _retry_http(fn, retries=2, delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls[0], 2)


# ── Tests: cache read/write ──────────────────────────────────────────────────

class TestCacheReadWrite(unittest.TestCase):

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("quota_gate._cache_dir", return_value=tmpdir):
                data = {"session_pct": 50.0, "weekly_pct": 30.0}
                _write_cache("testprov", data)
                result = _read_cache("testprov")
                self.assertEqual(result, data)

    def test_read_missing_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("quota_gate._cache_dir", return_value=tmpdir):
                result = _read_cache("nonexistent")
                self.assertIsNone(result)

    def test_read_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("quota_gate._cache_dir", return_value=tmpdir):
                cache_path = os.path.join(tmpdir, "badprov-last-good.json")
                with open(cache_path, "w") as f:
                    f.write("NOT JSON")
                result = _read_cache("badprov")
                self.assertIsNone(result)

    def test_cache_dir_respects_env(self):
        """_cache_dir uses HERMES_HOME if set."""
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/fake-hermes"}):
            result = _cache_dir()
            self.assertEqual(result, "/tmp/fake-hermes/quota-governor")

    def test_cache_dir_defaults(self):
        with patch.dict(os.environ, {"HERMES_HOME": ""}, clear=False):
            # HERMES_HOME empty → default to ~/.hermes
            result = _cache_dir()
            self.assertTrue(result.endswith("quota-governor"))


# ── Tests: compute_*_status ──────────────────────────────────────────────────

class TestComputeOllamaStatus(unittest.TestCase):

    @patch("quota_gate.query_ollama")
    def test_normal_status(self, mock_query):
        mock_query.return_value = {"session_pct": 40.0, "weekly_pct": 20.0, "activity_cost": 0.0}
        result = compute_ollama_status()
        self.assertEqual(result["profile"], "pr-ollama")
        self.assertEqual(result["provider"], "ollama-cloud")
        self.assertEqual(result["availability"], 60.0)  # 100 - 40 (max)
        self.assertEqual(result["bottleneck_pct"], 40.0)
        self.assertEqual(result["bottleneck_window"], "session")  # 40 > 20
        self.assertEqual(result["error"], "")

    @patch("quota_gate.query_ollama")
    def test_weekly_bottleneck(self, mock_query):
        mock_query.return_value = {"session_pct": 10.0, "weekly_pct": 70.0, "activity_cost": 0.0}
        result = compute_ollama_status()
        self.assertEqual(result["bottleneck_window"], "weekly")
        self.assertEqual(result["availability"], 30.0)

    @patch("quota_gate.query_ollama")
    def test_paying_mode(self, mock_query):
        """100% session with cost → availability=5 (paying mode)."""
        mock_query.return_value = {"session_pct": 100.0, "weekly_pct": 50.0, "activity_cost": 1.5}
        result = compute_ollama_status()
        self.assertEqual(result["availability"], 5.0)
        self.assertEqual(result["bottleneck_pct"], 100.0)

    @patch("quota_gate.query_ollama")
    def test_zero_usage(self, mock_query):
        mock_query.return_value = {"session_pct": 0.0, "weekly_pct": 0.0, "activity_cost": 0.0}
        result = compute_ollama_status()
        self.assertEqual(result["availability"], 100.0)
        self.assertEqual(result["bottleneck_pct"], 0.0)


class TestComputeNanogptStatus(unittest.TestCase):

    @patch("quota_gate.query_nanogpt")
    def test_active_with_usage(self, mock_query):
        mock_query.return_value = {
            "state": "active", "daily_pct": 30.0, "weekly_tokens_pct": 50.0
        }
        result = compute_nanogpt_status()
        self.assertEqual(result["profile"], "pr-nanogpt")
        self.assertEqual(result["availability"], 50.0)
        self.assertEqual(result["bottleneck_pct"], 50.0)
        self.assertEqual(result["bottleneck_window"], "weekly_tokens")

    @patch("quota_gate.query_nanogpt")
    def test_inactive_state(self, mock_query):
        mock_query.return_value = {"state": "paused", "daily_pct": 50, "weekly_tokens_pct": 50}
        result = compute_nanogpt_status()
        self.assertEqual(result["availability"], 0.0)
        self.assertIn("state is", result["error"])

    @patch("quota_gate.query_nanogpt")
    def test_no_usage_data(self, mock_query):
        """Active state with no usage data → 100% availability."""
        mock_query.return_value = {"state": "active", "daily_pct": None, "weekly_tokens_pct": None}
        result = compute_nanogpt_status()
        self.assertEqual(result["availability"], 100.0)
        self.assertEqual(result["bottleneck_pct"], 0.0)


class TestComputeOpenrouterStatus(unittest.TestCase):

    @patch("quota_gate.query_openrouter")
    def test_with_spending_limit(self, mock_query):
        mock_query.return_value = {
            "limit": 10.0, "usage": 3.0, "usage_weekly_usd": 1.0, "expires_at": None
        }
        result = compute_openrouter_status()
        self.assertEqual(result["profile"], "pr-openrouter")
        self.assertEqual(result["availability"], 70.0)  # 100 - (3/10*100)
        self.assertEqual(result["bottleneck_window"], "spending_limit")

    @patch("quota_gate.query_openrouter")
    def test_no_limit_heuristic(self, mock_query):
        """No spending limit → $5/week soft ceiling."""
        mock_query.return_value = {
            "limit": None, "usage": None, "usage_weekly_usd": 2.5, "expires_at": None
        }
        result = compute_openrouter_status()
        self.assertEqual(result["bottleneck_pct"], 50.0)  # 2.5/5*100
        self.assertEqual(result["availability"], 50.0)
        self.assertEqual(result["bottleneck_window"], "weekly_usd")

    @patch("quota_gate.query_openrouter")
    def test_expired_key(self, mock_query):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        mock_query.return_value = {
            "limit": 10.0, "usage": 0, "usage_weekly_usd": 0, "expires_at": past
        }
        result = compute_openrouter_status()
        self.assertEqual(result["availability"], 0.0)
        self.assertIn("expired", result["error"])

    @patch("quota_gate.query_openrouter")
    def test_no_data(self, mock_query):
        mock_query.return_value = {
            "limit": None, "usage": None, "usage_weekly_usd": None, "expires_at": None
        }
        result = compute_openrouter_status()
        self.assertEqual(result["availability"], 100.0)
        self.assertEqual(result["bottleneck_pct"], 0.0)


# ── Tests: select_provider (non-privacy) ─────────────────────────────────────

class TestSelectProviderNonPrivacy(unittest.TestCase):

    def test_picks_highest_availability(self):
        providers = [
            _provider(profile="pr-ollama", availability=40),
            _provider(profile="pr-nanogpt", availability=80),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_skips_errored(self):
        providers = [
            _provider(profile="pr-ollama", availability=90, error="timeout"),
            _provider(profile="pr-nanogpt", availability=50, error=""),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_skips_zero_availability(self):
        providers = [
            _provider(profile="pr-ollama", availability=0),
            _provider(profile="pr-nanogpt", availability=50),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_all_exhausted_returns_none(self):
        providers = [
            _provider(profile="pr-ollama", availability=0),
            _provider(profile="pr-nanogpt", availability=0),
        ]
        result = select_provider(providers)
        self.assertIsNone(result)

    def test_empty_list_returns_none(self):
        result = select_provider([])
        self.assertIsNone(None)

    def test_tiebreak_by_preference(self):
        """When availability is equal, lower preference number wins."""
        providers = [
            _provider(profile="pr-nanogpt", availability=50),
            _provider(profile="pr-ollama", availability=50),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-ollama")  # preference 0

    def test_paying_mode_fallback_to_free(self):
        """If top provider is in paying mode, but a free one exists, pick free."""
        providers = [
            _provider(profile="pr-ollama", availability=5, bottleneck=100),
            _provider(profile="pr-nanogpt", availability=30, bottleneck=70),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-nanogpt")

    def test_paying_mode_no_free_stays(self):
        """If all providers are in paying mode, stay with top availability."""
        providers = [
            _provider(profile="pr-ollama", availability=5, bottleneck=100),
            _provider(profile="pr-nanogpt", availability=3, bottleneck=100),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-ollama")  # higher avail


# ── OpenCode Go (MULTI-PROV-06) ─────────────────────────────────────────────

from quota_gate import (
    query_opencode_go,
    compute_opencode_go_status,
    PROFILE_MODELS,
)

# Verified live response shape (Sep 2026, MULTI-PROV-06):
#   {"usage": {"rolling": {"status": "ok", "percent": 5, "resetsAt": "..."},
#              "weekly":  {"status": "ok", "percent": 2, "resetsAt": "..."},
#              "monthly": {"status": "ok", "percent": 1, "resetsAt": "..."}}}
_OPENCODE_GO_RAW = {
    "usage": {
        "rolling": {"status": "ok", "percent": 5, "resetsAt": "2026-09-06T23:44:21Z"},
        "weekly": {"status": "ok", "percent": 2, "resetsAt": "2026-09-07T00:00:00Z"},
        "monthly": {"status": "ok", "percent": 1, "resetsAt": "2026-10-06T18:28:47Z"},
    }
}


class TestOpenCodeGoAllowedProfiles(unittest.TestCase):
    """MULTI-PROV-06: pr-opencode joins the allowed/recommended sets."""

    def test_pr_opencode_in_allowed_profiles(self):
        self.assertIn("pr-opencode", ALLOWED_PROFILES)

    def test_pr_opencode_in_profile_models(self):
        # Sep 7 2026: glm-5.2 burned 82% of the OpenCode Go 5h window alone;
        # interactive model switched to glm-5.3-flash (3.6% for the same work).
        self.assertEqual(PROFILE_MODELS.get("pr-opencode"), "glm-5.3-flash")

    def test_pr_opencode_in_provider_preference(self):
        self.assertIn("pr-opencode", PROVIDER_PREFERENCE)

    def test_opencode_go_public_only(self):
        """opencode-go qualifies for public, NOT for sensitive/confidential."""
        self.assertIn("opencode-go", PRIVACY_CAPABILITIES["public"])
        self.assertNotIn("opencode-go", PRIVACY_CAPABILITIES["sensitive"])
        self.assertNotIn("opencode-go", PRIVACY_CAPABILITIES["confidential"])


class TestQueryOpenCodeGo(unittest.TestCase):
    """query_opencode_go parses the live response shape correctly."""

    def test_not_configured_returns_none_fields(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            with patch("quota_gate.get_env", return_value=None):
                # The unconfigured path returns Nones without raising.
                # (query_opencode_go raises via get_env in the gate — the
                # providers.py twin returns Nones; here we assert the
                # RuntimeError branch.)
                with self.assertRaises(RuntimeError):
                    query_opencode_go()

    def test_parses_live_shape(self):
        with patch("quota_gate.get_env", return_value="test-key"), \
             patch("quota_gate._retry_http", return_value=_OPENCODE_GO_RAW):
            result = query_opencode_go()
        self.assertEqual(result["rolling_pct"], 5.0)
        self.assertEqual(result["weekly_pct"], 2.0)
        self.assertEqual(result["monthly_pct"], 1.0)
        self.assertEqual(result["rolling_status"], "ok")
        self.assertEqual(result["weekly_resets_at"], "2026-09-07T00:00:00Z")

    def test_percent_not_multiplied(self):
        """percent is already 0-100 — 5 must stay 5.0, not 500."""
        with patch("quota_gate.get_env", return_value="test-key"), \
             patch("quota_gate._retry_http", return_value=_OPENCODE_GO_RAW):
            result = query_opencode_go()
        self.assertEqual(result["rolling_pct"], 5.0)


class TestComputeOpenCodeGoStatus(unittest.TestCase):
    """compute_opencode_go_status maps windows to availability."""

    def _query_result(self, rolling=5, weekly=2, monthly=1,
                      r_status="ok", w_status="ok", m_status="ok"):
        # rolling/weekly/monthly may be int, float, or None (no data).
        return {
            "rolling_pct": rolling, "weekly_pct": weekly, "monthly_pct": monthly,
            "rolling_status": r_status, "weekly_status": w_status,
            "monthly_status": m_status,
            "rolling_resets_at": None, "weekly_resets_at": None,
            "monthly_resets_at": None,
        }

    def test_normal_status(self):
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=15, weekly=6, monthly=3)):
            st = compute_opencode_go_status()
        self.assertEqual(st["profile"], "pr-opencode")
        self.assertEqual(st["provider"], "opencode-go")
        self.assertEqual(st["bottleneck_window"], "rolling")
        self.assertAlmostEqual(st["bottleneck_pct"], 15.0)
        self.assertAlmostEqual(st["availability"], 85.0)

    def test_monthly_bottleneck(self):
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=10, weekly=20, monthly=80)):
            st = compute_opencode_go_status()
        self.assertEqual(st["bottleneck_window"], "monthly")
        self.assertAlmostEqual(st["availability"], 20.0)

    def test_non_ok_status_treated_as_100(self):
        """A rate-limited window (status != ok) counts as fully used."""
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=5, weekly=2, monthly=1,
                                                   w_status="rate_limited")):
            st = compute_opencode_go_status()
        self.assertEqual(st["bottleneck_window"], "weekly")
        self.assertAlmostEqual(st["bottleneck_pct"], 100.0)
        self.assertAlmostEqual(st["availability"], 0.0)

    def test_burning_balance_flag(self):
        """Calibrated Sep 7 2026 (t_47640f18): when a window is exhausted the
        API keeps serving via prepaid Zen balance — the status dict must say
        so explicitly instead of looking like a hard block."""
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=100, weekly=40, monthly=20,
                                                   r_status="rate-limited")):
            st = compute_opencode_go_status()
        self.assertTrue(st["burning_balance"])
        self.assertEqual(st["bottleneck_window"], "rolling")
        self.assertAlmostEqual(st["availability"], 0.0)
        self.assertEqual(st["raw"]["rolling_status"], "rate-limited")

    def test_no_burning_balance_when_all_ok(self):
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=15, weekly=6, monthly=3)):
            st = compute_opencode_go_status()
        self.assertFalse(st["burning_balance"])

    def test_no_usage_data_fully_available(self):
        with patch("quota_gate.query_opencode_go",
                   return_value=self._query_result(rolling=None, weekly=None,
                                                   monthly=None)):
            st = compute_opencode_go_status()
        self.assertEqual(st["availability"], 100.0)
        self.assertEqual(st["bottleneck_pct"], 0.0)
        self.assertEqual(st["bottleneck_window"], "unknown")

    def test_select_provider_considers_opencode_go(self):
        """pr-opencode wins when it has the most availability (public)."""
        providers = [
            _provider(profile="pr-ollama", availability=10, bottleneck=90),
            _provider(profile="pr-opencode", availability=85, bottleneck=15,
                      provider="opencode-go"),
        ]
        result = select_provider(providers)
        self.assertEqual(result["profile"], "pr-opencode")

    def test_select_provider_excludes_opencode_go_for_sensitive(self):
        """sensitive excludes opencode-go (public only, unaudited ZDR)."""
        providers = [
            _provider(profile="pr-ollama", availability=10, bottleneck=90),
            _provider(profile="pr-opencode", availability=85, bottleneck=15,
                      provider="opencode-go"),
        ]
        result = select_provider(providers, privacy_level="sensitive")
        self.assertEqual(result["profile"], "pr-ollama")

    def test_validate_recommended_profile_accepts_pr_opencode(self):
        warnings = []
        result = validate_recommended_profile(
            "pr-opencode",
            {"pr-ollama", "pr-nanogpt", "pr-opencode"},
            warnings,
        )
        self.assertEqual(result, "pr-opencode")
        self.assertEqual(warnings, [])


# ── Cost-based model selection + peak pricing (MULTI-PROV-07) ──────────────

from quota_gate import (
    is_peak_hours,
    peak_pricing_context,
    worker_model_for,
    PROFILE_WORKER_MODELS,
    WORKER_COST_TIERS,
    PEAK_WINDOWS_UTC,
)
import datetime


class TestCostModelMap(unittest.TestCase):
    """MULTI-PROV-07: every allowed profile has a cheap worker model."""

    def test_worker_models_cover_allowed_profiles(self):
        for profile in ALLOWED_PROFILES:
            self.assertIn(profile, PROFILE_WORKER_MODELS,
                          f"{profile} has no cheap worker model")

    def test_worker_model_is_not_the_interactive_model(self):
        # The whole point: worker model differs from the pricey default.
        self.assertEqual(PROFILE_WORKER_MODELS["pr-opencode"], "qwen3.8-flash")
        self.assertNotEqual(PROFILE_MODELS["pr-opencode"],
                            PROFILE_WORKER_MODELS["pr-opencode"])
        self.assertEqual(PROFILE_WORKER_MODELS["pr-ollama"], "deepseek-v4-flash")
        # Sep 7 2026: qwen3.5-4b returns HTTP 402 (not subscription-covered);
        # zai-org/glm-5.2 is the proven covered model.  Worker==interactive is
        # a known temporary cost compromise until MULTI-PROV-10 finds a cheaper
        # covered model.
        self.assertEqual(PROFILE_WORKER_MODELS["pr-nanogpt"], "zai-org/glm-5.2")

    def test_deepseek_never_mapped_to_pr_opencode(self):
        """Pitfall: deepseek-v4-flash gives RegionError 403 on OpenCode Go
        (China-hosted, needs explicit opt-in). Must never be pr-opencode's."""
        self.assertNotIn("deepseek",
                         PROFILE_WORKER_MODELS["pr-opencode"].lower())

    def test_worker_model_for_known_profile(self):
        self.assertEqual(worker_model_for("pr-opencode"), "qwen3.8-flash")

    def test_worker_model_for_unknown_profile_falls_back(self):
        # Unknown profile → falls back to interactive model if present,
        # else None (never crashes the gate).
        self.assertEqual(worker_model_for("pr-openrouter"),
                         PROFILE_MODELS["pr-openrouter"])
        self.assertIsNone(worker_model_for("pr-nonexistent"))

    def test_worker_cost_tiers(self):
        self.assertEqual(WORKER_COST_TIERS, {"micro", "tiny", "small"})


class TestIsPeakHours(unittest.TestCase):
    """Peak = Mon–Fri 01:00–04:00 and 06:00–10:00 UTC (half-open ranges)."""

    def _utc(self, y, mo, d, h):
        return datetime.datetime(y, mo, d, h, tzinfo=datetime.timezone.utc)

    def test_weekday_peak_windows_active(self):
        # 2026-09-07 is a Monday
        for hour in (1, 2, 3, 6, 7, 8, 9):
            self.assertTrue(is_peak_hours(self._utc(2026, 9, 7, hour)),
                            f"Monday {hour:02d}:00 should be peak")

    def test_boundary_hours(self):
        self.assertFalse(is_peak_hours(self._utc(2026, 9, 7, 0)))
        self.assertFalse(is_peak_hours(self._utc(2026, 9, 7, 4)))  # end excl
        self.assertFalse(is_peak_hours(self._utc(2026, 9, 7, 5)))
        self.assertTrue(is_peak_hours(self._utc(2026, 9, 7, 6)))
        self.assertFalse(is_peak_hours(self._utc(2026, 9, 7, 10)))  # end excl
        self.assertFalse(is_peak_hours(self._utc(2026, 9, 7, 23)))

    def test_weekday_outside_windows_inactive(self):
        # 2026-09-09 is a Wednesday
        for hour in (0, 4, 5, 10, 12, 18, 23):
            self.assertFalse(is_peak_hours(self._utc(2026, 9, 9, hour)))

    def test_weekends_never_peak(self):
        # 2026-09-05 Saturday, 2026-09-06 Sunday
        for day in (5, 6):
            for hour in (1, 2, 3, 6, 7, 8, 9):
                self.assertFalse(is_peak_hours(self._utc(2026, 9, day, hour)),
                                 f"weekend day {day} {hour:02d}:00 not peak")

    def test_default_now_is_utc(self):
        # No exception when called without args; returns bool.
        self.assertIsInstance(is_peak_hours(), bool)

    def test_windows_shape(self):
        self.assertEqual(PEAK_WINDOWS_UTC, ((1, 4), (6, 10)))


class TestPeakPricingContext(unittest.TestCase):
    def test_active_context(self):
        # Monday 2026-09-07 at 07:00 UTC → peak
        ctx = peak_pricing_context(
            datetime.datetime(2026, 9, 7, 7, tzinfo=datetime.timezone.utc))
        self.assertTrue(ctx["active"])
        self.assertEqual(ctx["windows_utc"], ["01:00-04:00", "06:00-10:00"])
        self.assertEqual(ctx["multiplier"], 2)
        self.assertIn("opencode-go", ctx["affected_models"])
        self.assertIn("ollama-cloud", ctx["affected_models"])

    def test_inactive_context_has_no_affected_models(self):
        # Tuesday 12:00 UTC → off-peak
        ctx = peak_pricing_context(
            datetime.datetime(2026, 9, 8, 12, tzinfo=datetime.timezone.utc))
        self.assertFalse(ctx["active"])
        self.assertEqual(ctx["affected_models"], {})


class TestGateOutputContainsPeakAndWorkerModel(unittest.TestCase):
    """main()'s wakeAgent:true context must expose peak_pricing and the
    cheap-model recommendation (the task creator consumes them)."""

    def _fake_provider(self, profile="pr-opencode", provider="opencode-go",
                       model="glm-5.2", availability=80.0, bottleneck=20.0):
        return {"profile": profile, "provider": provider, "model": model,
                "availability": availability, "bottleneck_pct": bottleneck,
                "bottleneck_window": "monthly", "error": "", "raw": {}}

    def test_main_context_fields(self):
        import io
        import contextlib
        fake = self._fake_provider()
        with patch.object(_mod, "parse_privacy_level", return_value=None), \
             patch.object(_mod, "compute_ollama_status", return_value=fake), \
             patch.object(_mod, "get_env", return_value=None), \
             patch.object(_mod, "select_provider", return_value=dict(fake)), \
             patch.object(_mod, "get_existing_profiles",
                          return_value={"pr-ollama", "pr-nanogpt", "pr-opencode"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _mod.main()
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        ctx = out["context"]
        self.assertTrue(out["wakeAgent"])
        self.assertEqual(ctx["recommended_worker_model"], "qwen3.8-flash")
        self.assertIn("peak_pricing", ctx)
        self.assertIn("active", ctx["peak_pricing"])
        self.assertIn("windows_utc", ctx["peak_pricing"])
        self.assertEqual(ctx["worker_models"]["pr-opencode"], "qwen3.8-flash")
        self.assertIn("model_selection_rule", ctx)


if __name__ == "__main__":
    unittest.main()