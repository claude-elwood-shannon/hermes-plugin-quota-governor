#!/usr/bin/env python3
"""Tests for scripts/burn-watchdog.py.

Covers:
  - provider-agnostic signal extraction (a 5th provider needs zero changes)
  - the Sep 7 2026 opencode-go leak sequence reproduction: window percent
    deltas + cost meter > 0 → WARN at ~$0.20 then STOP at configurable
    threshold, with full history in the ledger
  - observe-only behavior (no enforcement when a provider is missing config)
  - silence (empty stdout) when nothing burns
  - STOP file write + daemon kill; manual-clear-only semantics
  - episode boundary: cumulative resets when burn recovers (window reset)

Run:
  python3 -m pytest test_burn_watchdog.py -v
  python3 test_burn_watchdog.py          # direct run
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

# Import the hyphen-named script via import machinery (matches test_quota_gate).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "burn_watchdog",
    os.path.join(SCRIPT_DIR, "scripts", "burn-watchdog.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["burn_watchdog"] = _mod
_spec.loader.exec_module(_mod)

import burn_watchdog as wd  # noqa: E402


def _mk_snapshot(providers):
    """Build a wall-clock snapshot shape matching quota-gate.py output."""
    return {"wakeAgent": True, "context": {"providers": providers}}


def _opencode(rolling_pct=100.0, status="rate-limited", cost=None,
              availability=0.0, bottleneck="rolling"):
    """A provider entry shaped like opencode-go in the gate snapshot.

    ``burning_balance`` true when any window status != ok. ``raw.cost`` is
    the calibrated per-request-burn USD meter (t_47640f18). ``raw.cost=None``
    when the snapshot carries no real meter → watchdog falls back to the
    percent-delta x window_usd_cap estimate.
    """
    raw = {
        "rolling_pct": rolling_pct,
        "weekly_pct": 7.0,
        "monthly_pct": 23.0,
        "rolling_status": status,
        "weekly_status": "ok",
        "monthly_status": "ok",
    }
    if cost is not None:
        raw["cost"] = cost
    return {
        "profile": "pr-opencode",
        "provider": "opencode-go",
        "model": "glm-5.3-flash",
        "availability": availability,
        "bottleneck_pct": 100.0,
        "bottleneck_window": bottleneck,
        "burning_balance": status != "ok",
        "error": "",
        "raw": raw,
    }


class BaseWatchdogTest(unittest.TestCase):
    """Isolate state, ledger, warnings, STOP + config into a temp sandbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.hermes_home = os.path.join(self.tmp.name, "profile")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(os.path.join(self.hermes_home, "quota-governor"),
                    exist_ok=True)
        self._patchers = []
        for env_key, val in (
            ("BURN_STATE_DIR", self.state_dir),
            ("HERMES_HOME", self.hermes_home),
        ):
            p = patch.dict(os.environ, {env_key: val}, clear=True)
            p.start()
            self._patchers.append(p)
        # Reload module-level path constants with the sandbox.
        wd.STATE_DIR = self.state_dir
        wd.LEDGER_FILE = os.path.join(self.state_dir, "burn-ledger.jsonl")
        wd.STATE_FILE = os.path.join(self.state_dir, "burn-state.json")
        wd.WARN_FILE = os.path.join(self.state_dir, "burn-warnings.json")
        wd.CONFIG_FILE = os.path.join(self.state_dir, "burn-watchdog.json")
        wd.STOP_FILE = os.path.join(self.hermes_home, "quota-governor", "STOP")
        wd.PIDFILE = os.path.join(self.hermes_home, "quota-governor-daemon.pid")

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def write_config(self, cfg):
        with open(wd.CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    def write_pid(self, pid):
        with open(wd.PIDFILE, "w", encoding="utf-8") as fh:
            fh.write(str(pid))

    def stub_gate(self, snapshot):
        """Write a temp gate script that echoes the JSON-encoded snapshot."""
        gate = os.path.join(self.tmp.name, "stub-gate.py")
        self.write_gate(gate, snapshot)
        os.chmod(gate, 0o755)
        return gate

    def write_gate(self, gate, snapshot):
        raw = json.dumps(snapshot)
        with open(gate, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\nimport json\n")
            # JSON uses double quotes; embed inside a single-quoted literal.
            fh.write("print('''%s''')\n" % raw)

    def _snapshot(self, rolling_pct, status, cost):
        return _mk_snapshot([_opencode(rolling_pct=rolling_pct,
                                       status=status, cost=cost)])

    def read_ledger(self):
        if not os.path.exists(wd.LEDGER_FILE):
            return []
        with open(wd.LEDGER_FILE, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def read_state(self):
        if not os.path.exists(wd.STATE_FILE):
            return {}
        with open(wd.STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)


class TestSignalExtraction(BaseWatchdogTest):
    """Provider-agnostic window/cost/is_burning extraction."""

    def test_window_fields_generic(self):
        prov = _opencode(rolling_pct=100.0, status="rate-limited")
        windows = wd.window_fields(prov)
        by_name = {w["name"]: w for w in windows}
        self.assertEqual(by_name["rolling"]["pct"], 100.0)
        self.assertEqual(by_name["rolling"]["status"], "rate-limited")
        self.assertEqual(by_name["weekly"]["status"], "ok")
        self.assertEqual(len(windows), 3)

    def test_fifth_provider_needs_no_script_changes(self):
        # A brand-new provider with the same shape but different window names.
        raw = {
            "hourly_pct": 30.0,
            "hourly_status": "exceeded",   # non-ok status => burning
            "daily_pct": 5.0,
            "daily_status": "ok",
            "cost": 0.05,                   # real USD meter present
        }
        prov = {
            "provider": "acme-ai",
            "raw": raw,
        }
        self.assertTrue(wd.is_burning(prov))
        self.assertEqual(wd.provider_cost(prov), 0.05)
        names = {w["name"] for w in wd.window_fields(prov)}
        self.assertEqual(names, {"hourly", "daily"})

    def test_is_burning_flag_and_status(self):
        self.assertTrue(wd.is_burning(_opencode(status="rate-limited")))
        self.assertTrue(wd.is_burning({**_opencode(status="exceeded")}))
        ok = _opencode(status="ok", rolling_pct=50.0)
        ok["burning_balance"] = False
        self.assertFalse(wd.is_burning(ok))

    def test_provider_cost_prefers_direct_then_raw(self):
        self.assertEqual(wd.provider_cost({"raw": {"cost": "0.00003327"}}),
                         0.00003327)
        self.assertEqual(wd.provider_cost({"cost": "1.5", "raw": {"cost": "9"}}), 1.5)
        self.assertIsNone(wd.provider_cost({"raw": {}}))


class TestLeakSequence(BaseWatchdogTest):
    """Reproduce the Sep 7 2026 opencode-go leak and verify WARN then STOP."""

    CONFIG = {
        "opencode-go": {
            "enabled": True,
            "burn_rate_warn_usd_per_min": 0.05,
            "burn_total_warn_usd": 0.20,
            "burn_total_stop_usd": 1.00,
            "window_usd_cap": 12.0,   # rolling window $12 (incident console)
        }
    }

    def test_warn_then_stop_with_full_ledger(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snapshot(100.0, "ok", 0.0))

        # The incident: rolling hits 100% / rate-limited, cost meter goes >0
        # (burning prepaid balance), cumulative burn climbs to $1.04 in ~30 min.
        # First tick just seeds the baseline (no burn yet -> no alert).
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 0.01))
        self.assertEqual(wd.run_tick(gate_path=gate, now=1_700_000_000), [])

        # ~10 min later: cost meter climbed to ~$0.22 -> cumulative >= $0.20
        # warn threshold fires.
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 0.22))
        alerts1 = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(len(alerts1), 1)
        self.assertIn("BURN-WARN", alerts1[0])
        self.assertIn("opencode-go", alerts1[0])
        self.assertIn("$0.2", alerts1[0])  # cumulative $0.22 formatted

        # ~10 min more: cost meter climbed past $1.00 -> STOP fires.
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 1.20))
        # Fake a daemon pid so we can assert the kill path.
        # Build a tiny real child to kill safely (no real daemon in sandbox).
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.write_pid(proc.pid)
        alerts2 = wd.run_tick(gate_path=gate, now=1_700_001_200)
        self.assertEqual(len(alerts2), 1)
        self.assertIn("BURN-STOP", alerts2[0])
        self.assertIn("wrote STOP file", alerts2[0])
        proc.wait(timeout=10)  # daemon should have been SIGTERM'd

        # STOP file written with evidence.
        self.assertTrue(os.path.exists(wd.STOP_FILE))
        with open(wd.STOP_FILE, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("opencode-go", content)
        self.assertIn("manual clear required", content)

        # STOP does not re-fire on a subsequent over-threshold tick.
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 1.30))
        alerts3 = wd.run_tick(gate_path=gate, now=1_700_001_800)
        self.assertEqual(alerts3, [])

        # Full history recorded in the ledger, including the WARN + STOP actions.
        ledger = self.read_ledger()
        kinds = [e.get("action") for e in ledger if e.get("action")]
        self.assertIn("WARN", kinds)
        self.assertIn("STOP", kinds)
        cost_meter_readings = [e for e in ledger if "cost_meter" in e]
        self.assertTrue(cost_meter_readings)

    def test_observe_only_without_config(self):
        # No config file at all -> observe only, no WARN/STOP enforcement.
        gate = self.stub_gate(self._snapshot(100.0, "rate-limited", 5.0))
        # First tick seeds baseline; second accumulates cost but must stay silent.
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(alerts, [])
        self.assertTrue(os.path.exists(wd.LEDGER_FILE))  # still recorded
        # No STOP file written even though cumulative way over default 1.0.
        self.assertFalse(os.path.exists(wd.STOP_FILE))

    def test_disabled_provider_is_observe_only(self):
        self.write_config({"opencode-go": {"enabled": False}})
        gate = self.stub_gate(self._snapshot(100.0, "rate-limited", 2.5))
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(alerts, [])
        self.assertFalse(os.path.exists(wd.STOP_FILE))

    def test_silent_when_no_burn(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snapshot(50.0, "ok", 0.0))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.assertEqual(alerts, [])
        # No warnings file written for a non-burning provider.
        self.assertFalse(os.path.exists(wd.WARN_FILE))

    def test_episode_resets_on_recovery(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snapshot(100.0, "rate-limited", 0.05))
        # First tick seeds the baseline; second advances the meter to $0.30
        # (delta $0.25) while burning => cumulative >= warn threshold.
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 0.30))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(len(alerts), 1)
        self.assertIn("BURN-WARN", alerts[0])
        state_before = self.read_state()
        self.assertGreaterEqual(state_before["opencode-go"]["cum_cost_usd"], 0.20)

        # Window resets: status back to ok, cost meter to 0 -> episode closes.
        self.write_gate(gate, self._snapshot(5.0, "ok", 0.0))
        alerts = wd.run_tick(gate_path=gate, now=1_700_001_200)
        self.assertEqual(alerts, [])  # recovered, no alert, reset cumulative
        state_after = self.read_state()
        self.assertEqual(state_after["opencode-go"]["cum_cost_usd"], 0.0)


class TestFallbackEstimate(BaseWatchdogTest):
    """Percent-delta x window_usd_cap fallback when no cost meter present."""

    CONFIG = {
        "opencode-go": {
            "enabled": True,
            "burn_total_warn_usd": 0.20,
            "burn_total_stop_usd": 1.00,
            "window_usd_cap": 12.0,
        }
    }

    def _no_cost_snapshot(self, rolling_pct, status):
        return _mk_snapshot([_opencode(rolling_pct=rolling_pct, status=status,
                                       cost=None)])

    def test_percent_delta_drives_cumulative(self):
        # First observation seeds (no delta, no cum). Second observation with
        # a 10pp jump (10% of $12 = $1.20) while rate-limited => burning,
        # cumulative >= stop $1.00.
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._no_cost_snapshot(90.0, "ok"))
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.write_gate(gate, self._no_cost_snapshot(100.0, "rate-limited"))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(len(alerts), 1)
        self.assertIn("BURN-STOP", alerts[0])


class TestStopFileKill(BaseWatchdogTest):
    def test_kill_no_daemon(self):
        self.write_config({"opencode-go": {"enabled": True,
                                           "burn_total_stop_usd": 0.10}})
        gate = self.stub_gate(self._snapshot(100.0, "rate-limited", 0.01))
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.write_gate(gate, self._snapshot(100.0, "rate-limited", 0.20))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(len(alerts), 1)
        self.assertIn("daemon not running", alerts[0])
        self.assertTrue(os.path.exists(wd.STOP_FILE))


if __name__ == "__main__":
    unittest.main()
