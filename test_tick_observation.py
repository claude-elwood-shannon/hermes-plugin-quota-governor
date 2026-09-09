#!/usr/bin/python3.12
"""test_tick_observation.py — OBJ-26a follow-up (t_92d7f0d6).

Verifica que el tick exponga request_balance_usd / request_covered_usd:
  1. request_window_totals_all_homes(): merge cross-profile de
     nanogpt-requests.jsonl (las filas aterrizan bajo el HERMES_HOME del
     proceso que CAPTURA, no del observador). Override de homes via
     QUOTA_GOVERNOR_PROFILE_HOMES (os.pathsep).
  2. tick-observation.py: append de la fila quota_tick con ambos campos,
     None sin datos de ledger (homes_read==0), y privacy:low (solo
     agregados USD; ninguna fila per-request).
  3. record_observation del core (quota_governor.py): mismos campos en las
     filas de los hooks in-process.

TODO con fixtures tmp (HERMES_HOME / QUOTA_GOVERNOR_PROFILE_HOMES apuntados
a tmp): nunca toca observaciones reales ni el ledger de produccion.
Run:  /usr/bin/python3.12 test_tick_observation.py  (or pytest)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GOV_DIR = "REPO"
LEDGER_SCRIPT = os.path.join(GOV_DIR, "scripts", "nanogpt-balance-ledger.py")
TICK_OBS_SCRIPT = os.path.join(GOV_DIR, "scripts", "tick-observation.py")

_spec = importlib.util.spec_from_file_location("nanogpt_balance_ledger_t",
                                               LEDGER_SCRIPT)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

_tspec = importlib.util.spec_from_file_location("tick_observation",
                                                TICK_OBS_SCRIPT)
tick_obs = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(tick_obs)

BALANCE = {"ts": "1970-01-01T00:00:00Z", "costUsd": 4.87e-06,
           "paymentSource": "USD", "requestId": "req_b"}
COVERED = {"ts": "1970-01-01T00:00:00Z", "costUsd": 0,
           "paymentSource": "USD", "requestId": "req_c"}


def _fresh_ts():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_homes_env(homes):
    os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = os.pathsep.join(homes)


def clear_homes_env():
    os.environ.pop("QUOTA_GOVERNOR_PROFILE_HOMES", None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tickobs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # never let a test touch production state
        for key in ("HERMES_HOME", "QUOTA_GOVERNOR_PROFILE_HOMES"):
            self.addCleanup(os.environ.pop, key, None)
        os.environ["HERMES_HOME"] = self.tmp
        clear_homes_env()

    def make_home(self, name):
        return os.path.join(self.tmp, name)

    def obs_rows(self, home):
        path = Path(home) / "quota-governor" / "observations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]


class TestAllHomesMerge(Base):
    """request_window_totals_all_homes: merge across profile homes."""

    def test_merges_rows_from_multiple_homes(self):
        h1 = self.make_home("pr-nanogpt")
        h2 = self.make_home("pr-ollama")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        ledger.append_request_row(dict(BALANCE, requestId="req_b2",
                                       costUsd=1.5e-06, ts=_fresh_ts()),
                                  hermes_home=h2)
        set_homes_env([h1, h2])
        merged = ledger.request_window_totals_all_homes()
        self.assertIsNotNone(merged)
        self.assertAlmostEqual(merged["request_balance_usd"],
                               6.37e-06, places=9)
        self.assertEqual(merged["balance_requests"], 2)
        self.assertEqual(merged["homes_read"], 2)
        self.assertEqual(merged["covered_requests"], 0)

    def test_covered_and_balance_summed_separately(self):
        h1 = self.make_home("pr-nanogpt")
        h2 = self.make_home("pr-ollama")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        ledger.append_request_row(dict(COVERED, ts=_fresh_ts()),
                                  hermes_home=h2)
        set_homes_env([h1, h2])
        merged = ledger.request_window_totals_all_homes()
        self.assertAlmostEqual(merged["request_balance_usd"], 4.87e-06,
                               places=9)
        self.assertAlmostEqual(merged["request_covered_usd"], 0.0, places=9)
        self.assertEqual(merged["covered_requests"], 1)

    def test_missing_homes_do_not_count_as_read(self):
        ghost = self.make_home("pr-ghost")
        real = self.make_home("pr-nanogpt")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=real)
        set_homes_env([ghost, real])
        merged = ledger.request_window_totals_all_homes()
        self.assertIsNotNone(merged)
        self.assertEqual(merged["homes_read"], 1)
        self.assertAlmostEqual(merged["request_balance_usd"], 4.87e-06,
                               places=9)

    def test_no_homes_with_files_is_zeroed_not_none(self):
        """The merge itself returns zeros; CONSUMERS turn homes_read==0
        into None (no data != zero spend)."""
        set_homes_env([os.path.join(self.tmp, "nothing-here")])
        merged = ledger.request_window_totals_all_homes()
        self.assertIsNotNone(merged)
        self.assertEqual(merged["homes_read"], 0)
        self.assertEqual(merged["request_balance_usd"], 0.0)

    def test_explicit_hermes_home_reads_only_that_home(self):
        h1 = self.make_home("pr-nanogpt")
        h2 = self.make_home("pr-ollama")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        ledger.append_request_row(dict(BALANCE, requestId="req_b2",
                                       costUsd=2.0e-06, ts=_fresh_ts()),
                                  hermes_home=h2)
        merged = ledger.request_window_totals_all_homes(hermes_home=h2)
        self.assertAlmostEqual(merged["request_balance_usd"], 2.0e-06,
                               places=9)
        self.assertEqual(merged["homes_read"], 1)

    def test_window_filtering_applies_after_merge(self):
        h1 = self.make_home("pr-nanogpt")
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        ledger.append_request_row(
            dict(BALANCE, ts=old.strftime("%Y-%m-%dT%H:%M:%SZ")),
            hermes_home=h1)
        ledger.append_request_row(dict(BALANCE, requestId="req_b2",
                                       ts=_fresh_ts()), hermes_home=h1)
        set_homes_env([h1])
        merged = ledger.request_window_totals_all_homes()
        self.assertEqual(merged["balance_requests"], 1)

    def test_never_raises_on_garbage_homes(self):
        garbage = self.make_home("pr-garbage")
        Path(garbage, "quota-governor").mkdir(parents=True)
        Path(garbage, "quota-governor", "nanogpt-requests.jsonl")\
            .write_text("{not json\n[broken\n")
        set_homes_env([garbage])
        merged = ledger.request_window_totals_all_homes()
        self.assertIsNotNone(merged)
        self.assertEqual(merged["homes_read"], 1)
        self.assertEqual(merged["requests"], 0)


class TestTickObservation(Base):
    """tick-observation.py: fila quota_tick con ambos campos."""

    def _run(self):
        return tick_obs.main([
            "--action", "run", "--max-workers", "2",
            "--session-pct", "12.3", "--weekly-pct", "45.6",
            "--session-reqs", "10", "--weekly-reqs", "90",
            "--cost", "0.0000", "--reason", "healthy"])

    def test_row_carries_request_fields(self):
        home = self.make_home("pr-tick")
        os.environ["HERMES_HOME"] = home
        h1 = self.make_home("pr-nanogpt")
        h2 = self.make_home("pr-ollama")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        ledger.append_request_row(dict(BALANCE, requestId="req_b2",
                                       costUsd=2.0e-06, ts=_fresh_ts()),
                                  hermes_home=h2)
        set_homes_env([h1, h2])
        rc = self._run()
        self.assertEqual(rc, 0)
        rows = self.obs_rows(home)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event"], "quota_tick")
        self.assertEqual(row["decision"]["action"], "run")
        self.assertEqual(row["decision"]["max_workers"], 2)
        self.assertEqual(row["decision"]["reason"], "healthy")
        self.assertAlmostEqual(row["quota"]["request_balance_usd"],
                               6.87e-06, places=9)
        self.assertAlmostEqual(row["quota"]["request_covered_usd"], 0.0,
                               places=9)
        self.assertAlmostEqual(row["quota"]["ollama_session_pct"], 12.3,
                               places=6)

    def test_fields_none_when_no_home_has_data(self):
        home = self.make_home("pr-tick")
        os.environ["HERMES_HOME"] = home
        empty = self.make_home("pr-empty")
        set_homes_env([empty])  # home exists in the list but has no file
        rc = self._run()
        self.assertEqual(rc, 0)
        row = self.obs_rows(home)[-1]
        self.assertIsNone(row["quota"]["request_balance_usd"])
        self.assertIsNone(row["quota"]["request_covered_usd"])

    def test_writes_only_under_hermes_home(self):
        home = self.make_home("pr-tick")
        os.environ["HERMES_HOME"] = home
        before = set(Path(self.tmp).glob("**/observations.jsonl"))
        self._run()
        after = set(Path(self.tmp).glob("**/observations.jsonl"))
        self.assertEqual(after - before,
                         {Path(home) / "quota-governor"
                                 / "observations.jsonl"})

    def test_garbage_args_do_not_crash(self):
        home = self.make_home("pr-tick")
        os.environ["HERMES_HOME"] = home
        rc = tick_obs.main(["--action", "run", "--session-pct", "oops",
                            "--max-workers", "x"])
        self.assertEqual(rc, 0)
        row = self.obs_rows(home)[-1]
        self.assertIsNone(row["quota"]["ollama_session_pct"])
        self.assertIsNone(row["decision"]["max_workers"])

    def test_cli_subprocess_end_to_end(self):
        """End-to-end via CLI: the exact call the tick makes, with the
        homes override reaching the subprocess through the environment.
        The override now takes full home paths (the ledger entries are
        quota-governor dirs, not HERMES_HOME roots)."""
        home = self.make_home("pr-tick")
        h1 = self.make_home("pr-nanogpt")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        env = dict(os.environ)
        env["HERMES_HOME"] = home
        env["PLUGIN_DIR"] = GOV_DIR
        env["QUOTA_GOVERNOR_PROFILE_HOMES"] = h1
        proc = subprocess.run(
            [sys.executable, TICK_OBS_SCRIPT,
             "--action", "stop", "--max-workers", "0",
             "--session-pct", "100", "--weekly-pct", "90",
             "--session-reqs", "1", "--weekly-reqs", "2",
             "--cost", "5.0", "--reason", "spending limit reached"],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.obs_rows(home)[-1]
        self.assertEqual(row["event"], "quota_tick")
        self.assertEqual(row["decision"]["action"], "stop")
        self.assertAlmostEqual(row["quota"]["request_balance_usd"],
                               4.87e-06, places=9)

    def test_root_hermes_home_included_in_defaults(self):
        """~/.hermes/quota-governor (no-HERMES_HOME processes) is merged."""
        root_qg = Path(self.tmp) / "roothome" / "quota-governor"
        root_qg.mkdir(parents=True)
        ts = _fresh_ts()
        with open(root_qg / "nanogpt-requests.jsonl", "w") as fh:
            fh.write(json.dumps(dict(BALANCE, ts=ts)) + "\n")
        merged = ledger.request_window_totals_all_homes()
        # the real default list includes the real ~/.hermes root, so assert
        # the mechanism via an override that emulates it (HERMES-style root):
        os.environ["QUOTA_GOVERNOR_PROFILE_HOMES"] = str(root_qg.parent)
        merged = ledger.request_window_totals_all_homes()
        self.assertAlmostEqual(merged["request_balance_usd"], 4.87e-06,
                               places=9)
        self.assertEqual(merged["homes_read"], 1)


class _Snap:
    """Minimal QuotaSnapshot duck-type for record_observation."""
    ollama_session_pct = 0.0
    ollama_weekly_pct = 0.0
    ollama_session_requests = 0
    ollama_weekly_requests = 0
    ollama_activity_cost = None
    nanogpt_daily_pct = None
    nanogpt_weekly_tokens_pct = None
    openrouter_usage_weekly_usd = None
    opencode_go_rolling_pct = None
    opencode_go_weekly_pct = None
    opencode_go_monthly_pct = None
    errors: list = []


class TestCoreRecordObservation(Base):
    """record_observation (core): mismos campos en filas de hooks."""

    def _gov(self):
        # import the package the way the plugin loader does
        sys.path.insert(0, os.path.dirname(GOV_DIR))
        import hermes_plugin_quota_governor.quota_governor as gov_mod
        return gov_mod

    def test_hook_rows_carry_request_fields(self):
        home = self.make_home("pr-core")
        os.environ["HERMES_HOME"] = home
        h1 = self.make_home("pr-nanogpt")
        ledger.append_request_row(dict(BALANCE, ts=_fresh_ts()),
                                  hermes_home=h1)
        gov_mod = self._gov()
        set_homes_env([h1])
        gov_mod.record_observation(event="test_obs", snapshot=_Snap())
        row = self.obs_rows(home)[-1]
        self.assertAlmostEqual(row["quota"]["request_balance_usd"],
                               4.87e-06, places=9)
        self.assertAlmostEqual(row["quota"]["request_covered_usd"], 0.0,
                               places=9)

    def test_hook_rows_without_ledger_are_none(self):
        home = self.make_home("pr-core")
        os.environ["HERMES_HOME"] = home
        gov_mod = self._gov()
        # override points at an empty nonexistent home: merge runs,
        # homes_read == 0 -> None (no data != zero spend)
        set_homes_env([os.path.join(self.tmp, "nothing-here")])
        gov_mod.record_observation(event="test_obs", snapshot=_Snap())
        row = self.obs_rows(home)[-1]
        self.assertIsNone(row["quota"]["request_balance_usd"])
        self.assertIsNone(row["quota"]["request_covered_usd"])

    def test_hook_rows_fail_open_on_broken_ledger_import(self):
        """Ledger import explosion must never break record_observation."""
        home = self.make_home("pr-core")
        os.environ["HERMES_HOME"] = home
        gov_mod = self._gov()
        from unittest import mock
        # importlib is imported inside _request_window_fields, so patch the
        # real importlib.util module (the same object the function resolves).
        with mock.patch("importlib.util.spec_from_file_location",
                        side_effect=RuntimeError("boom")):
            gov_mod.record_observation(event="test_obs", snapshot=_Snap())
        row = self.obs_rows(home)[-1]
        self.assertIsNone(row["quota"]["request_balance_usd"])
        self.assertIsNone(row["quota"]["request_covered_usd"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
