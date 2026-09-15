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
        # The independent balance ledger is the retroactive-cover/seed source;
        # sandbox it (empty = no pre-existing spend) so tests are hermetic.
        wd.BALANCE_LEDGER_FILE = os.path.join(self.state_dir,
                                              "nanogpt-balance-ledger.jsonl")

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

    def test_provider_cost_nanogpt_balance_meter(self):
        """OBJ-26: balance.usd_balance is the nanogpt meter (last priority)."""
        prov = {"provider": "nanogpt", "balance": {"usd_balance": "15.49"}}
        self.assertEqual(wd.provider_cost(prov), 15.49)
        self.assertTrue(wd.meter_is_balance(prov))

    def test_meter_is_balance_false_for_spend_meters(self):
        """cost / activity_cost meters are cumulative spend, not balance."""
        self.assertFalse(wd.meter_is_balance({"provider": "nanogpt",
                                              "cost": 1.0}))
        self.assertFalse(wd.meter_is_balance({"provider": "nanogpt",
                                              "raw": {"cost": 1.0}}))
        self.assertFalse(wd.meter_is_balance({"provider": "nanogpt",
                                              "raw": {"activity_cost": 0.5}}))
        self.assertFalse(wd.meter_is_balance({"provider": "nanogpt",
                                              "raw": {}}))


class TestNanogptBalanceBurnObj26(BaseWatchdogTest):
    """OBJ-26: the nanogpt meter is a DECREASING balance — spend = last-now.

    Regression for the sign bug: a cumulative-spend style ``now - last``
    clamps balance DRAIN to 0, making the watchdog blind to nanogpt burn.
    """

    CONFIG = {
        "nanogpt": {
            "enabled": True,
            "burn_rate_warn_usd_per_min": 0.05,
            "burn_total_warn_usd": 0.20,
            "burn_total_stop_usd": 1.00,
            "window_usd_cap": None,
        }
    }

    @staticmethod
    def _nanogpt(balance, burning=True):
        return {
            "profile": "pr-nanogpt", "provider": "nanogpt",
            "model": "z-ai/glm-5.2", "availability": 100.0,
            "bottleneck_pct": 0.0, "bottleneck_window": "weekly_tokens+balance",
            "burning_balance": burning, "error": "",
            "raw": {"covered_first": True},
            "balance": {"usd_balance": balance, "level": "warn"},
        }

    def _snap(self, balance):
        return _mk_snapshot([self._nanogpt(balance)])

    def test_balance_drop_accumulates_burn(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snap(15.49))
        self.assertEqual(wd.run_tick(gate_path=gate, now=1_700_000_000), [])

        # $0.30 drained in 10 min → rate 0.03/min < warn-rate, but cum >= 0.20.
        self.write_gate(gate, self._snap(15.19))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(len(alerts), 1)
        self.assertIn("BURN-WARN nanogpt", alerts[0])
        self.assertIn("$0.30", alerts[0])

    def test_balance_rise_is_topup_not_negative_burn(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snap(10.0))
        self.assertEqual(wd.run_tick(gate_path=gate, now=1_700_000_000), [])
        # Balance RISES (top-up): must clamp to 0, never negative spend.
        self.write_gate(gate, self._snap(20.0))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(alerts, [])
        state = self.read_state()
        self.assertEqual(state["nanogpt"]["cum_cost_usd"], 0.0)

    def test_balance_stop_at_threshold(self):
        self.write_config(self.CONFIG)
        gate = self.stub_gate(self._snap(15.49))
        self.assertEqual(wd.run_tick(gate_path=gate, now=1_700_000_000), [])
        # Drain $1.10 in ~10 min: cum >= stop 1.00 → STOP + daemon kill.
        with patch.object(wd, "kill_daemon", return_value=None):
            self.write_gate(gate, self._snap(14.39))
            alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertTrue(any("BURN-STOP nanogpt" in a for a in alerts))
        self.assertTrue(os.path.exists(wd.STOP_FILE))


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


class TestObj39NonBurningBalanceMeter(BaseWatchdogTest):
    """OBJ-39/t_cce2a554: nanogpt NEVER reports burning (state ``active``,
    weekly window without a status) yet its prepaid balance drops are real
    burn. The watchdog must accumulate and WARN/STOP on that drop even when
    ``is_burning()`` is False — the old ``if burning: cum += tick_cost`` gate
    pinned cum_cost_usd at 0.0 forever, so the $6.98/$5.00 overspend never
    alerted. Uses the PRODUCTION meter shape (no burning_balance, no *_status).
    """

    CONFIG = {
        "nanogpt": {
            "enabled": True,
            "burn_total_warn_usd": 1.0,
            "burn_total_stop_usd": 3.0,   # OBJ-26 production thresholds
            "window_usd_cap": None,
        }
    }

    @staticmethod
    def _prod_nanogpt(balance):
        """Exact production shape: state active, no burning flag, no status."""
        return {
            "profile": "pr-nanogpt", "provider": "nanogpt",
            "model": "z-ai/glm-5.3-flash", "availability": 0.0,
            "bottleneck_pct": 100.0, "bottleneck_window": "weekly_tokens+balance",
            "error": "",
            "raw": {"weekly_tokens_pct": 100.033175, "state": "active",
                    "covered_first": True},
            "balance": {"usd_balance": balance, "weekly_tokens_pct": 100.033175,
                        "state": "active", "policy_allows_balance": True,
                        "level": "stop"},
        }

    def test_seed_from_window_spent_fires_stop_on_first_tick(self):
        """First observation has no last_cost, so per-tick delta is 0; the
        watchdog must seed cum from the authoritative ``window_spent_usd``
        (already $7.79) and STOP on the very first tick against the current
        state — this is exactly the closure criterion (current spent >= stop).
        """
        self.write_config(self.CONFIG)
        # Independent balance ledger carries the trailing window_spent_usd.
        with open(wd.BALANCE_LEDGER_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-09-15T11:43:21+00:00", "usd_balance": 19.33577715,
                "window_spent_usd": 7.786909, "source": "probe",
            }) + "\n")
        gate = self.stub_gate(_mk_snapshot([self._prod_nanogpt(19.33577715)]))
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.assertTrue(any("BURN-STOP nanogpt" in a for a in alerts), alerts)
        self.assertTrue(os.path.exists(wd.STOP_FILE))
        state = self.read_state()
        self.assertGreaterEqual(state["nanogpt"]["cum_cost_usd"], 7.7)

    def test_seeded_spend_from_real_ledger(self):
        """seed_balance_cum reads the trailing window_spent_usd when no real
        fixture, mirroring the independent budget ledger."""
        self.write_config(self.CONFIG)
        # empty sandbox ledger -> seed 0, then no burn (no last_cost) -> WARN
        # threshold 1.0 not hit, no alert. Guard: seed is 0 when ledger absent.
        gate = self.stub_gate(_mk_snapshot([self._prod_nanogpt(19.33)]))
        self.assertEqual(wd.run_tick(gate_path=gate, now=1_700_000_000), [])
        state = self.read_state()
        self.assertEqual(state["nanogpt"]["cum_cost_usd"], 0.0)

    def test_non_burning_balance_drop_accumulates(self):
        """A 0.40 drop with burning=False (production: never burning) still
        accumulates against the WARN/STOP thresholds."""
        self.write_config(self.CONFIG)
        gate = self.stub_gate(_mk_snapshot([self._prod_nanogpt(19.33)]))
        # First tick must have a last_cost to measure a delta; seed by running
        # once with an empty ledger (cum 0, last_cost 19.33), then drain.
        wd.run_tick(gate_path=gate, now=1_700_000_000)
        self.write_gate(gate, _mk_snapshot([self._prod_nanogpt(18.93)]))
        # 0.40 drop < warn 1.0 and < stop 3.0 -> warn rate (0.05/min) not set,
        # so silent but still accumulated in state.
        alerts = wd.run_tick(gate_path=gate, now=1_700_000_600)
        self.assertEqual(alerts, [])
        state = self.read_state()
        self.assertGreaterEqual(state["nanogpt"]["cum_cost_usd"], 0.39)


class TestObj39GapDetection(BaseWatchdogTest):
    """OBJ-39/t_cce2a554 item 3: when a tick arrives >2x the ledger interval
    after the last one (a skipped tick — e.g. the 14-sep 16:46->18:33Z gate
    timeout hole), the watchdog must surface a coverage-gap alert AND fold the
    burn that fell entirely in the hole (from the independent every-3-min
    balance ledger) into the cumulative, so an over-threshold hole still
    produces BURN-STOP retroactively.
    """

    CONFIG = {
        "nanogpt": {
            "enabled": True,
            "burn_total_warn_usd": 1.0,
            "burn_total_stop_usd": 1.0,   # small stop so the hole trips it
            "window_usd_cap": None,
        }
    }

    FIXTURE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures",
        "nanogpt-balance-ledger-sep14-gap.jsonl")

    @staticmethod
    def _prod_nanogpt(balance):
        return TestObj39NonBurningBalanceMeter._prod_nanogpt(balance)

    def test_gap_hole_fires_coverage_alert_and_stop(self):
        self.write_config(self.CONFIG)
        # Use the real 14-sep fixture as the independent balance ledger.
        with open(self.FIXTURE, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertTrue(rows)
        with open(wd.BALANCE_LEDGER_FILE, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        last_ts = _epoch(rows[-1]["ts"])
        # Watchdog's last observation before the hole: balance 8.7498 at
        # 16:46:48Z. It resumes well after (e.g. 18:35Z) with a DROPPED balance.
        gate = self.stub_gate(_mk_snapshot([self._prod_nanogpt(7.95903088)]))
        now = last_ts + 5  # >2x the interval after the last ledger row
        # Seed prior last_ts in the hole to exercise the >2x gap path.
        with open(wd.STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({
                "nanogpt": {
                    "provider": "nanogpt", "last_ts": _epoch(
                        "2026-09-14T16:46:48.767557+00:00"),
                    "burning": False, "cum_cost_usd": 0.0,
                    "last_cost": 8.74979789, "last_pct": 100.033175,
                }
            }, fh)
        alerts = wd.run_tick(gate_path=gate, now=now)
        self.assertTrue(any("BURN-COVERAGE-GAP" in a for a in alerts),
                        f"expected coverage alert, got {alerts}")
        self.assertTrue(any("BURN-STOP nanogpt" in a for a in alerts),
                        f"expected STOP after retro hole cover, got {alerts}")


def _epoch(iso):
    """ISO ts from the fixture ledger -> epoch seconds."""
    import datetime as _dt
    s = str(iso).rstrip("Z").replace("+00:00", "")
    if " " in s:
        s = s.replace(" ", "T")
    dt = _dt.datetime.fromisoformat(s)
    return dt.replace(tzinfo=_dt.timezone.utc).timestamp()


if __name__ == "__main__":
    unittest.main()
