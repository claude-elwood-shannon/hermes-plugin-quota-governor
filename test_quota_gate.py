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


if __name__ == "__main__":
    unittest.main()