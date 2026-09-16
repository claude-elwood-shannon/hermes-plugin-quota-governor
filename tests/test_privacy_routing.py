#!/usr/bin/env python3
"""test_privacy_routing.py — verify privacy-based provider routing in quota-gate.py.

Tests:
  1. parse_privacy_tag: extracts privacy level from task body text
  2. parse_privacy_level: resolves from env var and stdin
  3. select_provider with privacy_level=None: behaves as before (no filtering)
  4. select_provider with privacy_level="public": same as None (all providers ok)
  5. select_provider with privacy_level="sensitive": excludes OpenRouter
  6. select_provider with privacy_level="confidential": only local providers
  7. End-to-end via main(): QUOTA_GATE_PRIVACY env var flows to output JSON
  8. Edge case: confidential with no local provider → wakeAgent:false + warning
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Ensure the script dir is on the path
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

# Import the module (quota-gate.py has a hyphen, so import via importlib)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "quota_gate", os.path.join(SCRIPTS_DIR, "quota-gate.py")
)
quota_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(quota_gate)

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {desc}")
    else:
        failed += 1
        print(f"  FAIL: {desc} {detail}")


# ---------------------------------------------------------------------------
# Shared fixture provider lists
# ---------------------------------------------------------------------------
MOCK_PROVIDERS = [
    {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
     "availability": 80.0, "bottleneck_pct": 20.0, "bottleneck_window": "session",
     "error": "", "raw": {}},
    {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
     "availability": 60.0, "bottleneck_pct": 40.0, "bottleneck_window": "daily",
     "error": "", "raw": {}},
    {"profile": "pr-openrouter", "provider": "openrouter", "model": "z-ai/glm-5.2:free",
     "availability": 90.0, "bottleneck_pct": 10.0, "bottleneck_window": "weekly_usd",
     "error": "", "raw": {}},
]

MOCK_PROVIDERS_SENSITIVE = [
    {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
     "availability": 50.0, "bottleneck_pct": 50.0, "bottleneck_window": "session",
     "error": "", "raw": {}},
    {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
     "availability": 70.0, "bottleneck_pct": 30.0, "bottleneck_window": "daily",
     "error": "", "raw": {}},
    {"profile": "pr-openrouter", "provider": "openrouter", "model": "z-ai/glm-5.2:free",
     "availability": 95.0, "bottleneck_pct": 5.0, "bottleneck_window": "weekly_usd",
     "error": "", "raw": {}},
]

MOCK_CLOUD_ONLY = [
    {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
     "availability": 90.0, "bottleneck_pct": 10.0, "bottleneck_window": "session",
     "error": "", "raw": {}},
    {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
     "availability": 80.0, "bottleneck_pct": 20.0, "bottleneck_window": "daily",
     "error": "", "raw": {}},
]


def simulate_main(providers_list, privacy_level):
    """Simulate the main() flow without live API calls."""
    warnings = []
    recommended = quota_gate.select_provider(providers_list, privacy_level=privacy_level)
    if recommended is None and privacy_level:
        capable = quota_gate.PRIVACY_CAPABILITIES.get(privacy_level, set())
        available = [p["provider"] for p in providers_list if not p["error"]]
        warnings.append(
            f"privacy:{privacy_level} excludes all available providers "
            f"(available: {available}, capable: {sorted(capable)})"
        )
    if recommended is None:
        return {
            "wakeAgent": False,
            "context": {
                "privacy_level": privacy_level or "none",
                "warning": "; ".join(warnings) if warnings else "all providers exhausted",
            },
        }
    return {
        "wakeAgent": True,
        "context": {
            "recommended_profile": recommended["profile"],
            "recommended_model": recommended["model"],
            "privacy_level": privacy_level or "none",
            "warning": "; ".join(warnings) if warnings else None,
        },
    }


def _parse_privacy_via_stdin(json_str):
    """Run parse_privacy_level with stdin JSON (no env var set)."""
    code = (
        "import sys, json, os, importlib.util\n"
        "os.environ.pop('QUOTA_GATE_PRIVACY', None)\n"
        f"_spec = importlib.util.spec_from_file_location('qg', "
        f"'{os.path.join(SCRIPTS_DIR, 'quota-gate.py')}')\n"
        "qg = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(qg)\n"
        "print(qg.parse_privacy_level())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=json_str, capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return None
    val = proc.stdout.strip()
    return val if val != "None" else None


# ---------------------------------------------------------------------------
# Test group helpers
# ---------------------------------------------------------------------------
def _test1_parse_privacy_tag():
    """Test 1: parse_privacy_tag extracts the privacy level from task text."""
    print("\n--- Test 1: parse_privacy_tag ---")

    check("public tag",
          quota_gate.parse_privacy_tag("privacy:public\nrest") == "public")
    check("sensitive tag",
          quota_gate.parse_privacy_tag("privacy:sensitive\nrest") == "sensitive")
    check("confidential tag",
          quota_gate.parse_privacy_tag("privacy:confidential\nrest") == "confidential")
    check("case insensitive",
          quota_gate.parse_privacy_tag("Privacy:Sensitive\nrest") == "sensitive")
    check("quoted value",
          quota_gate.parse_privacy_tag('privacy: "sensitive"') == "sensitive")
    check("abbreviation pub",
          quota_gate.parse_privacy_tag("privacy:pub") == "public")
    check("abbreviation conf",
          quota_gate.parse_privacy_tag("privacy:conf") == "confidential")
    check("spanish publico",
          quota_gate.parse_privacy_tag("privacy:publico") == "public")
    check("no tag returns None",
          quota_gate.parse_privacy_tag("no tag here") is None)
    check("empty text returns None",
          quota_gate.parse_privacy_tag("") is None)
    check("invalid value returns None",
          quota_gate.parse_privacy_tag("privacy:unknown") is None)
    check("tag in middle of body",
          quota_gate.parse_privacy_tag("some text\nprivacy: confidential\nmore text") == "confidential")

    # --- high/medium/low aliases (OBJ-18) ---
    check("high alias → sensitive",
          quota_gate.parse_privacy_tag("privacy:high") == "sensitive")
    check("medium alias → sensitive",
          quota_gate.parse_privacy_tag("privacy:medium") == "sensitive")
    check("low alias → public",
          quota_gate.parse_privacy_tag("privacy:low") == "public")
    check("high alias case insensitive",
          quota_gate.parse_privacy_tag("Privacy:HIGH") == "sensitive")
    check("low alias with trailing text",
          quota_gate.parse_privacy_tag("privacy: low\nrest") == "public")
    check("high alias quoted",
          quota_gate.parse_privacy_tag('privacy: "high"') == "sensitive")
    check("medium alias in body context",
          quota_gate.parse_privacy_tag("objective:OBJ-18\nprivacy:medium\nauto_created:true") == "sensitive")


def _test2_env_var():
    """Test 2: parse_privacy_level resolves from the QUOTA_GATE_PRIVACY env var."""
    print("\n--- Test 2: parse_privacy_level (env var) ---")

    os.environ["QUOTA_GATE_PRIVACY"] = "sensitive"
    check("env var sensitive",
          quota_gate.parse_privacy_level() == "sensitive")

    os.environ["QUOTA_GATE_PRIVACY"] = "confidential"
    check("env var confidential",
          quota_gate.parse_privacy_level() == "confidential")

    os.environ["QUOTA_GATE_PRIVACY"] = ""
    check("env var empty → None",
          quota_gate.parse_privacy_level() is None)

    del os.environ["QUOTA_GATE_PRIVACY"]
    check("env var unset → None",
          quota_gate.parse_privacy_level() is None)

    # --- env var with high/medium/low aliases (OBJ-18) ---
    os.environ["QUOTA_GATE_PRIVACY"] = "high"
    check("env var high → sensitive",
          quota_gate.parse_privacy_level() == "sensitive")

    os.environ["QUOTA_GATE_PRIVACY"] = "medium"
    check("env var medium → sensitive",
          quota_gate.parse_privacy_level() == "sensitive")

    os.environ["QUOTA_GATE_PRIVACY"] = "low"
    check("env var low → public",
          quota_gate.parse_privacy_level() == "public")

    os.environ["QUOTA_GATE_PRIVACY"] = "HIGH"
    check("env var HIGH (uppercase) → sensitive",
          quota_gate.parse_privacy_level() == "sensitive")

    del os.environ["QUOTA_GATE_PRIVACY"]


def _test3_no_privacy_baseline():
    """Test 3: select_provider with no privacy filtering (baseline)."""
    print("\n--- Test 3: select_provider (no privacy, baseline) ---")

    result = quota_gate.select_provider(MOCK_PROVIDERS, privacy_level=None)
    # OBJ-26: preference-first for ALL levels — pr-nanogpt (pref 0) wins even
    # with lower availability (openrouter 90% is no longer first).
    check("no privacy → preference-first picks pr-nanogpt (OBJ-26)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    result = quota_gate.select_provider(MOCK_PROVIDERS, privacy_level="public")
    check("public → same preference-first (pr-nanogpt, OBJ-26)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")


def _test4_sensitive():
    """Test 4: select_provider with sensitive excludes OpenRouter."""
    print("\n--- Test 4: select_provider (sensitive) ---")

    result = quota_gate.select_provider(MOCK_PROVIDERS, privacy_level="sensitive")
    check("sensitive → excludes openrouter, picks nanogpt (preference-first over ollama 80%)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    result = quota_gate.select_provider(MOCK_PROVIDERS_SENSITIVE, privacy_level="sensitive")
    check("sensitive → picks nanogpt (70%) over openrouter (95%, excluded)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")


def _test5_confidential():
    """Test 5: select_provider with confidential only allows local/custom."""
    print("\n--- Test 5: select_provider (confidential) ---")

    # No local provider in the mock list → should return None
    result = quota_gate.select_provider(MOCK_PROVIDERS, privacy_level="confidential")
    check("confidential → no local provider → None",
          result is None,
          f"got {result}")

    # Add a local provider
    mock_with_local = MOCK_PROVIDERS + [
        {"profile": "pr-local", "provider": "custom", "model": "llama3.2:3b",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "local",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_with_local, privacy_level="confidential")
    check("confidential → local provider available → picks custom",
          result is not None and result["provider"] == "custom",
          f"got {result['provider'] if result else 'None'}")


def _test6_errored():
    """Test 6: select_provider with errored providers + privacy."""
    print("\n--- Test 6: select_provider (errored + privacy) ---")

    mock_errored = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 0.0, "bottleneck_pct": 100.0, "bottleneck_window": "error",
         "error": "connection refused", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 60.0, "bottleneck_pct": 40.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
        {"profile": "pr-openrouter", "provider": "openrouter", "model": "z-ai/glm-5.2:free",
         "availability": 90.0, "bottleneck_pct": 10.0, "bottleneck_window": "weekly_usd",
         "error": "", "raw": {}},
    ]

    result = quota_gate.select_provider(mock_errored, privacy_level="sensitive")
    check("sensitive → ollama errored, picks nanogpt (60%)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    result = quota_gate.select_provider(mock_errored, privacy_level="confidential")
    check("confidential → no local available → None",
          result is None)


def _test7_capabilities():
    """Test 7: PRIVACY_CAPABILITIES mapping is internally consistent."""
    print("\n--- Test 7: PRIVACY_CAPABILITIES consistency ---")

    check("public allows ollama-cloud",
          "ollama-cloud" in quota_gate.PRIVACY_CAPABILITIES["public"])
    check("public allows nanogpt",
          "nanogpt" in quota_gate.PRIVACY_CAPABILITIES["public"])
    check("public allows openrouter",
          "openrouter" in quota_gate.PRIVACY_CAPABILITIES["public"])
    check("sensitive allows ollama-cloud",
          "ollama-cloud" in quota_gate.PRIVACY_CAPABILITIES["sensitive"])
    check("sensitive allows nanogpt",
          "nanogpt" in quota_gate.PRIVACY_CAPABILITIES["sensitive"])
    check("sensitive excludes openrouter",
          "openrouter" not in quota_gate.PRIVACY_CAPABILITIES["sensitive"])
    check("confidential only allows local (custom + vllm-local OBJ-40)",
          quota_gate.PRIVACY_CAPABILITIES["confidential"] == {"custom",
                                                              "vllm-local"})

    # Reverse mapping
    check("reverse: ollama-cloud handles public",
          "public" in quota_gate._PROVIDER_PRIVACY["ollama-cloud"])
    check("reverse: ollama-cloud handles sensitive",
          "sensitive" in quota_gate._PROVIDER_PRIVACY["ollama-cloud"])
    check("reverse: openrouter does NOT handle sensitive",
          "sensitive" not in quota_gate._PROVIDER_PRIVACY.get("openrouter", set()))
    check("reverse: custom handles confidential",
          "confidential" in quota_gate._PROVIDER_PRIVACY["custom"])


def _test8_e2e():
    """Test 8: End-to-end via main() with env var."""
    print("\n--- Test 8: End-to-end via main() ---")

    # We can't call main() directly because it queries live APIs.
    # Instead, we test the select_provider + privacy_level flow by simulating
    # the main() logic with mock providers.

    # Public → wakeAgent true, preference-first → pr-nanogpt (OBJ-26)
    out = simulate_main(MOCK_PROVIDERS, "public")
    check("e2e public → wakeAgent:true",
          out["wakeAgent"] is True)
    check("e2e public → profile pr-nanogpt (preference-first, OBJ-26)",
          out["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out['context']['recommended_profile']}")
    check("e2e public → privacy_level public",
          out["context"]["privacy_level"] == "public")

    # Sensitive → wakeAgent true, picks nanogpt (preference-first, openrouter excluded)
    out = simulate_main(MOCK_PROVIDERS, "sensitive")
    check("e2e sensitive → wakeAgent:true",
          out["wakeAgent"] is True)
    check("e2e sensitive → profile pr-nanogpt (preference-first)",
          out["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out['context']['recommended_profile']}")
    check("e2e sensitive → privacy_level sensitive",
          out["context"]["privacy_level"] == "sensitive")

    # Confidential → wakeAgent false (no local provider)
    out = simulate_main(MOCK_PROVIDERS, "confidential")
    check("e2e confidential → wakeAgent:false",
          out["wakeAgent"] is False)
    check("e2e confidential → privacy_level confidential",
          out["context"]["privacy_level"] == "confidential")
    check("e2e confidential → warning mentions privacy exclusion",
          "privacy:confidential excludes" in (out["context"]["warning"] or ""),
          f"warning: {out['context']['warning']}")

    # None → behaves as before (no privacy field shown as "none")
    out = simulate_main(MOCK_PROVIDERS, None)
    check("e2e none → wakeAgent:true",
          out["wakeAgent"] is True)
    check("e2e none → privacy_level none",
          out["context"]["privacy_level"] == "none")


def _test9_alias_high():
    """Test 9a: privacy:high → sensitive → NanoGPT routing."""
    print("\n--- Test 9: Alias routing (high→NanoGPT, low→Ollama) ---")

    # Use mock_providers_sensitive where nanogpt has higher availability
    # than ollama for sensitive, so sensitive→nanogpt
    # mock_providers_sensitive: ollama 50%, nanogpt 70%, openrouter 95% (excluded)

    # privacy:high → sensitive → NanoGPT (70%, openrouter excluded)
    high_level = quota_gate.parse_privacy_tag("privacy:high")
    check("parse_privacy_tag('privacy:high') → sensitive",
          high_level == "sensitive",
          f"got {high_level}")

    out_high = simulate_main(MOCK_PROVIDERS_SENSITIVE, high_level)
    check("e2e high → wakeAgent:true",
          out_high["wakeAgent"] is True)
    check("e2e high → routes to pr-nanogpt (sensitive, NanoGPT)",
          out_high["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out_high['context']['recommended_profile']}")
    check("e2e high → privacy_level sensitive",
          out_high["context"]["privacy_level"] == "sensitive")


def _test9_alias_low_medium():
    """Test 9b: privacy:low → public and privacy:medium → sensitive routing."""
    # privacy:low → public → Ollama (all providers eligible, openrouter 90% wins)
    # BUT OBJ-18 criterion says "low → Ollama". In the public lane, the
    # highest-availability provider wins. With mock_providers (openrouter 90%),
    # openrouter would win. To verify the low→Ollama criterion specifically,
    # we use a mock where ollama has the highest availability.
    mock_low = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 95.0, "bottleneck_pct": 5.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 60.0, "bottleneck_pct": 40.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
        {"profile": "pr-openrouter", "provider": "openrouter", "model": "z-ai/glm-5.2:free",
         "availability": 30.0, "bottleneck_pct": 70.0, "bottleneck_window": "weekly_usd",
         "error": "", "raw": {}},
    ]

    low_level = quota_gate.parse_privacy_tag("privacy:low")
    check("parse_privacy_tag('privacy:low') → public",
          low_level == "public",
          f"got {low_level}")

    out_low = simulate_main(mock_low, low_level)
    check("e2e low → wakeAgent:true",
          out_low["wakeAgent"] is True)
    # OBJ-26: preference-first — pr-nanogpt (pref 0) wins over pr-ollama even
    # with ollama at 95% availability; the low→Ollama criterion predates OBJ-26.
    check("e2e low → routes to pr-nanogpt (preference-first, OBJ-26)",
          out_low["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out_low['context']['recommended_profile']}")
    check("e2e low → privacy_level public",
          out_low["context"]["privacy_level"] == "public")

    # privacy:medium → sensitive → same routing as high
    medium_level = quota_gate.parse_privacy_tag("privacy:medium")
    check("parse_privacy_tag('privacy:medium') → sensitive",
          medium_level == "sensitive",
          f"got {medium_level}")

    out_medium = simulate_main(MOCK_PROVIDERS_SENSITIVE, medium_level)
    check("e2e medium → routes to pr-nanogpt (sensitive)",
          out_medium["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out_medium['context']['recommended_profile']}")


def _test10_preference_first_a():
    """Test 10a: preference-first routing (sensitive → NanoGPT always) — wins."""
    # The OBJ-18 completion criterion requires privacy:high → NanoGPT even
    # when Ollama has MORE availability.  This is preference-first routing.
    print("\n--- Test 10: Preference-first routing (sensitive → NanoGPT always) ---")

    # Mock where Ollama has MUCH more availability than NanoGPT.
    # With availability-first, Ollama would win. With preference-first,
    # NanoGPT must win because it's preferred for sensitive.
    mock_pref = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 95.0, "bottleneck_pct": 5.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 30.0, "bottleneck_pct": 70.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]

    result = quota_gate.select_provider(mock_pref, privacy_level="sensitive")
    check("sensitive preference-first: ollama=95, nanogpt=30 → nanogpt wins",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    # Even more extreme: ollama 100%, nanogpt 1%
    mock_extreme = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 1.0, "bottleneck_pct": 99.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_extreme, privacy_level="sensitive")
    check("sensitive preference-first: ollama=100, nanogpt=1 → nanogpt wins",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    # OBJ-26: public ALSO uses preference-first now — pr-nanogpt (pref 0)
    # wins over pr-ollama regardless of the 95-vs-30 availability gap.
    result_pub = quota_gate.select_provider(mock_pref, privacy_level="public")
    check("public preference-first (OBJ-26): ollama=95, nanogpt=30 → nanogpt wins",
          result_pub is not None and result_pub["profile"] == "pr-nanogpt",
          f"got {result_pub['profile'] if result_pub else 'None'}")


def _test10_preference_first_b():
    """Test 10b: preference-first fallbacks and PRIVACY_PROVIDER_PREFERENCE."""
    # Fallback: nanogpt errored → ollama
    mock_nanogpt_err = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 80.0, "bottleneck_pct": 20.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 0.0, "bottleneck_pct": 100.0, "bottleneck_window": "error",
         "error": "connection refused", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_nanogpt_err, privacy_level="sensitive")
    check("sensitive preference-first: nanogpt errored → fallback to ollama",
          result is not None and result["profile"] == "pr-ollama",
          f"got {result['profile'] if result else 'None'}")
    # Fallback: nanogpt availability=0 (no error, exhausted quota) → ollama
    mock_nanogpt_zero = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 50.0, "bottleneck_pct": 50.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 0.0, "bottleneck_pct": 100.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_nanogpt_zero, privacy_level="sensitive")
    check("sensitive preference-first: nanogpt availability=0 → fallback to ollama",
          result is not None and result["profile"] == "pr-ollama",
          f"got {result['profile'] if result else 'None'}")
    # E2E: privacy:high → sensitive → NanoGPT wins (preference-first)
    mock_pref = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 95.0, "bottleneck_pct": 5.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 30.0, "bottleneck_pct": 70.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
    ]
    out_high_pref = simulate_main(mock_pref, "sensitive")
    check("e2e high (preference-first): ollama=95, nanogpt=30 → nanogpt",
          out_high_pref["context"]["recommended_profile"] == "pr-nanogpt",
          f"got {out_high_pref['context']['recommended_profile']}")

    # PRIVACY_PROVIDER_PREFERENCE structure checks
    check("PRIVACY_PROVIDER_PREFERENCE has sensitive key",
          "sensitive" in quota_gate.PRIVACY_PROVIDER_PREFERENCE)
    check("sensitive prefers nanogpt over ollama",
          quota_gate.PRIVACY_PROVIDER_PREFERENCE["sensitive"]["pr-nanogpt"]
          < quota_gate.PRIVACY_PROVIDER_PREFERENCE["sensitive"]["pr-ollama"])
    check("sensitive has confidential key",
          "confidential" in quota_gate.PRIVACY_PROVIDER_PREFERENCE)


def _test11_three_providers():
    """Test 11: Preference-first with 3 providers (sensitive excludes openrouter)."""
    print("\n--- Test 11: Preference-first with 3 providers ---")

    mock_3 = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 90.0, "bottleneck_pct": 10.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 20.0, "bottleneck_pct": 80.0, "bottleneck_window": "daily",
         "error": "", "raw": {}},
        {"profile": "pr-openrouter", "provider": "openrouter", "model": "z-ai/glm-5.2:free",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "weekly_usd",
         "error": "", "raw": {}},
    ]

    # Sensitive: openrouter excluded, nanogpt preferred over ollama
    result = quota_gate.select_provider(mock_3, privacy_level="sensitive")
    check("sensitive 3-prov: openrouter excluded, nanogpt preferred over ollama (90 vs 20)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")

    # Public: all eligible, preference-first (OBJ-26) → pr-nanogpt (pref 0)
    result = quota_gate.select_provider(mock_3, privacy_level="public")
    check("public 3-prov: preference-first → pr-nanogpt (OBJ-26)",
          result is not None and result["profile"] == "pr-nanogpt",
          f"got {result['profile'] if result else 'None'}")


def _test12_stdin():
    """Test 12: parse_privacy_level reads the privacy level from stdin JSON."""
    print("\n--- Test 12: stdin JSON privacy level ---")

    # parse_privacy_level reads from stdin JSON when env var is not set.
    # We simulate this by piping JSON to a subprocess that calls the function.

    # Note: parse_privacy_level reads stdin only when not a tty.
    # In subprocess, stdin is a pipe (not a tty), so it reads the JSON.
    check("stdin JSON {\"privacy\": \"sensitive\"} → sensitive",
          _parse_privacy_via_stdin('{"privacy": "sensitive"}') == "sensitive")
    check("stdin JSON {\"privacy_level\": \"confidential\"} → confidential",
          _parse_privacy_via_stdin('{"privacy_level": "confidential"}') == "confidential")
    check("stdin JSON {\"privacy\": \"high\"} → sensitive (alias)",
          _parse_privacy_via_stdin('{"privacy": "high"}') == "sensitive")
    check("stdin JSON {\"privacy\": \"low\"} → public (alias)",
          _parse_privacy_via_stdin('{"privacy": "low"}') == "public")
    check("stdin JSON {\"privacy\": \"medium\"} → sensitive (alias)",
          _parse_privacy_via_stdin('{"privacy": "medium"}') == "sensitive")
    check("stdin JSON with no privacy field → None",
          _parse_privacy_via_stdin('{"objective": "OBJ-18"}') is None)
    check("stdin JSON empty {} → None",
          _parse_privacy_via_stdin('{}') is None)


def _test13_confidential_hardened_a():
    """Test 13a: confidential with no local provider → None + capability matrix."""
    print("\n--- Test 13: Confidential routing ---")

    # Confidential with no local provider → None (wakeAgent:false)
    result = quota_gate.select_provider(MOCK_CLOUD_ONLY, privacy_level="confidential")
    check("confidential → no local provider → None",
          result is None,
          f"got {result}")

    # Confidential excludes ALL cloud providers even if they have high availability
    check("confidential → ollama-cloud NOT capable",
          "confidential" not in quota_gate._PROVIDER_PRIVACY.get("ollama-cloud", set()))
    check("confidential → nanogpt NOT capable",
          "confidential" not in quota_gate._PROVIDER_PRIVACY.get("nanogpt", set()))
    check("confidential → openrouter NOT capable",
          "confidential" not in quota_gate._PROVIDER_PRIVACY.get("openrouter", set()))
    check("confidential → custom IS capable",
          "confidential" in quota_gate._PROVIDER_PRIVACY.get("custom", set()))

    # E2E: confidential → wakeAgent:false with warning
    out_conf = simulate_main(MOCK_CLOUD_ONLY, "confidential")
    check("e2e confidential → wakeAgent:false",
          out_conf["wakeAgent"] is False)
    check("e2e confidential → warning mentions privacy exclusion",
          "privacy:confidential excludes" in (out_conf["context"]["warning"] or ""),
          f"warning: {out_conf['context']['warning']}")


def _test13_confidential_hardened_b():
    """Test 13b: confidential with local provider → picks custom, alias tags."""
    # Confidential with local provider → picks custom (preference-first)
    mock_with_local_conf = MOCK_CLOUD_ONLY + [
        {"profile": "pr-local", "provider": "custom", "model": "llama3.2:3b",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "local",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_with_local_conf, privacy_level="confidential")
    check("confidential → local provider available → picks custom",
          result is not None and result["provider"] == "custom",
          f"got {result['provider'] if result else 'None'}")

    # Confidential preference-first: even if ollama has 100% avail, custom wins
    mock_conf_extreme = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-local", "provider": "custom", "model": "llama3.2:3b",
         "availability": 1.0, "bottleneck_pct": 99.0, "bottleneck_window": "local",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_conf_extreme, privacy_level="confidential")
    check("confidential preference-first: ollama=100, custom=1 → custom wins",
          result is not None and result["provider"] == "custom",
          f"got {result['provider'] if result else 'None'}")

    # Confidential via env var alias: privacy:conf → confidential
    conf_env_level = quota_gate.parse_privacy_tag("privacy:conf")
    check("parse_privacy_tag('privacy:conf') → confidential",
          conf_env_level == "confidential",
          f"got {conf_env_level}")

    # Confidential via env var alias: privacy:intimo → confidential
    intimo_level = quota_gate.parse_privacy_tag("privacy:intimo")
    check("parse_privacy_tag('privacy:intimo') → confidential",
          intimo_level == "confidential",
          f"got {intimo_level}")


def _test14_opencode():
    """Test 14: sensitive excludes OpenCode Go provider."""
    print("\n--- Test 14: Sensitive excludes OpenCode Go ---")

    # OpenCode Go (opencode-go) is public-only per the privacy matrix.
    # It must NOT appear in PRIVACY_CAPABILITIES["sensitive"].
    check("opencode-go in public capabilities",
          "opencode-go" in quota_gate.PRIVACY_CAPABILITIES["public"])
    check("opencode-go NOT in sensitive capabilities",
          "opencode-go" not in quota_gate.PRIVACY_CAPABILITIES["sensitive"])
    check("opencode-go NOT in confidential capabilities",
          "opencode-go" not in quota_gate.PRIVACY_CAPABILITIES["confidential"])

    # Verify routing: a sensitive task with opencode-go available → excluded
    mock_with_opencode = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 10.0, "bottleneck_pct": 90.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
        {"profile": "pr-opencode", "provider": "opencode-go", "model": "glm-5.2",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
    ]
    result = quota_gate.select_provider(mock_with_opencode, privacy_level="sensitive")
    check("sensitive → opencode-go (100%) excluded, ollama (10%) wins",
          result is not None and result["profile"] == "pr-ollama",
          f"got {result['profile'] if result else 'None'}")

    # But public → opencode-go is eligible; OBJ-26 preference-first still puts
    # pr-ollama (pref 1) above pr-opencode (pref 2) in the public lane.
    result = quota_gate.select_provider(mock_with_opencode, privacy_level="public")
    check("public → pr-ollama (pref 1 beats opencode-go pref 2, OBJ-26)",
          result is not None and result["profile"] == "pr-ollama",
          f"got {result['profile'] if result else 'None'}")


def _test15_no_degradation():
    """Test 15: sensitive never falls back to public-only providers."""
    print("\n--- Test 15: No privacy degradation ---")

    # If all sensitive-capable providers are exhausted, the gate must NOT
    # fall back to a public-only provider (opencode-go, openrouter).
    # It must return None (wakeAgent:false) instead.
    mock_sens_exhausted = [
        {"profile": "pr-ollama", "provider": "ollama-cloud", "model": "glm-5.2",
         "availability": 0.0, "bottleneck_pct": 100.0, "bottleneck_window": "error",
         "error": "quota exhausted", "raw": {}},
        {"profile": "pr-nanogpt", "provider": "nanogpt", "model": "zai-org/glm-5.2",
         "availability": 0.0, "bottleneck_pct": 100.0, "bottleneck_window": "daily",
         "error": "quota exhausted", "raw": {}},
        {"profile": "pr-opencode", "provider": "opencode-go", "model": "glm-5.2",
         "availability": 100.0, "bottleneck_pct": 0.0, "bottleneck_window": "session",
         "error": "", "raw": {}},
    ]

    result = quota_gate.select_provider(mock_sens_exhausted, privacy_level="sensitive")
    check("sensitive: all sensitive providers exhausted → None (no degradation to opencode)",
          result is None,
          f"got {result['profile'] if result else 'None'}")

    # E2E: sensitive exhausted → wakeAgent:false
    out_sens_exhausted = simulate_main(mock_sens_exhausted, "sensitive")
    check("e2e sensitive exhausted → wakeAgent:false (no degradation)",
          out_sens_exhausted["wakeAgent"] is False)
    check("e2e sensitive exhausted → warning mentions privacy exclusion",
          "privacy:sensitive excludes" in (out_sens_exhausted["context"]["warning"] or ""),
          f"warning: {out_sens_exhausted['context']['warning']}")


def _print_results():
    """Print the cumulative PASS/FAIL summary block."""
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")


def main():
    """Run the 15 privacy-routing check groups. Returns 0/1.

    pytest collection safety: mutates os.environ (QUOTA_GATE_PRIVACY)
    and sys.argv, and runs quota-gate.py as subprocess — none of it
    may leak into the pytest interpreter collecting sibling files
    (importlib mode). Runs on explicit invocation only.
    """
    _test1_parse_privacy_tag()
    _test2_env_var()
    _test3_no_privacy_baseline()
    _test4_sensitive()
    _test5_confidential()
    _test6_errored()
    _test7_capabilities()
    _test8_e2e()
    _test9_alias_high()
    _test9_alias_low_medium()
    _test10_preference_first_a()
    _test10_preference_first_b()
    _test11_three_providers()
    _test12_stdin()
    _test13_confidential_hardened_a()
    _test13_confidential_hardened_b()
    _test14_opencode()
    _test15_no_degradation()
    _print_results()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    return 0 if failed == 0 else 1

# ── Tests: pytest wrapper (canonical batch) ─────────────────────────────────
def test_full_suite():
    """Run the 15 privacy-routing check groups; assert 0 failed."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        capture_output=True, text=True, timeout=120,
    )
    print(result.stdout[-2000:] if result.stdout else "")
    print(result.stderr[-2000:] if result.stderr else "")
    assert result.returncode == 0, (
        "self-run failed (rc=%d); see captured output above" % result.returncode
    )
    summary = [ln for ln in result.stdout.splitlines() if "Results:" in ln]
    assert summary, "summary line not found in output"
    assert "0 failed" in summary[-1], "unexpected: " + summary[-1]


if __name__ == "__main__":
    sys.exit(main())
