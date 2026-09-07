#!/usr/bin/env python3
"""Tests for model-cost-ledger.py (MULTI-PROV-09, t_5bdd7cfa).

Covers:
  - estimate_cost: token x price formula, unknown model -> 0, peak x2
  - anchor_from_resets_at: ok / anomalous / rate-limited pending reset
  - window_key: anchored buckets vs epoch-floor fallback
  - sync_ledger: delta accumulation, cursor idempotence, negative-delta clamp
  - summarize + model_window_warnings: 50%-of-window threshold
  - config override (prices / window_usd / warn_fraction) from model-cost.json
  - resolve_anchor with the gate import unavailable (fallback paths)
Run:  python3 test_model_cost_ledger.py  (or pytest)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "model_cost_ledger", os.path.join(SCRIPT_DIR, "scripts", "model-cost-ledger.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def make_fake_home(rows, tag="t"):
    """Create a temp hermes home with profiles/pr-x/state.db holding *rows*.

    rows: list of dicts with session_id, model, task, api_call_count,
    input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
    last_seen (ISO). Returns the home path (caller cleans up).
    """
    home = tempfile.mkdtemp(prefix=f"mcl-{tag}-")
    prof = os.path.join(home, "profiles", "pr-opencode")
    os.makedirs(prof)
    db = sqlite3.connect(os.path.join(prof, "state.db"))
    db.execute(
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT,"
        " billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,"
        " task TEXT, api_call_count INTEGER, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,"
        " cost_source TEXT, first_seen TEXT, last_seen TEXT,"
        " PRIMARY KEY (session_id, model, billing_base_url, task))"
    )
    for r in rows:
        db.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["session_id"], r["model"], r.get("provider", "opencode-go"),
             "https://opencode.ai/zen/go/v1", "", r.get("task", ""),
             r["calls"], r["in"], r["out"], r.get("cache", 0),
             r.get("cache_w", 0), r.get("reason", 0), 0.0, 0.0,
             "unknown", "none", r["last_seen"], r["last_seen"]),
        )
    db.commit()
    db.close()
    return home


class TestEstimate(unittest.TestCase):
    def test_formula(self):
        # 1M in, 1M out, 1M cache at qwen3.8-flash (0.15/0.47/0.016)
        c = _mod.estimate_cost("qwen3.8-flash", 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 0.15 + 0.47 + 0.016, places=6)

    def test_reasoning_added_to_output(self):
        c1 = _mod.estimate_cost("qwen3.8-flash", 0, 1000, 0, reasoning_tokens=500)
        c2 = _mod.estimate_cost("qwen3.8-flash", 0, 1500, 0)
        self.assertAlmostEqual(c1, c2, places=9)

    def test_unknown_model_zero(self):
        self.assertEqual(_mod.estimate_cost("mystery-model", 1e6, 1e6, 0), 0.0)

    def test_peak_double_deepseek(self):
        peak = dt.datetime(2026, 9, 7, 2, 30, tzinfo=dt.timezone.utc).timestamp()   # Mon 02:30 UTC
        off = dt.datetime(2026, 9, 7, 5, 30, tzinfo=dt.timezone.utc).timestamp()    # Mon 05:30 UTC
        cp = _mod.estimate_cost("deepseek-v4-flash", 1_000_000, 0, 0, at_ts=peak)
        co = _mod.estimate_cost("deepseek-v4-flash", 1_000_000, 0, 0, at_ts=off)
        self.assertAlmostEqual(cp, 2 * co, places=6)

    def test_weekend_never_peak(self):
        sat = dt.datetime(2026, 9, 5, 2, 30, tzinfo=dt.timezone.utc).timestamp()
        self.assertFalse(_mod.is_peak(dt.datetime.utcfromtimestamp(sat)))


class TestAnchor(unittest.TestCase):
    def test_healthy_window(self):
        reset = dt.datetime(2026, 9, 7, 4, 45).timestamp()
        now = reset - 4 * 3600  # 1h into the 5h window
        anchor = _mod.anchor_from_resets_at(dt.datetime.utcfromtimestamp(reset).isoformat() + "Z", now)
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(anchor + 5 * 3600, reset, delta=1)
        # now falls in the window [anchor, anchor+5h)
        self.assertEqual(_mod.window_key(now, anchor), _mod.window_key(reset - 1, anchor))
        self.assertNotEqual(_mod.window_key(now, anchor), _mod.window_key(reset + 1, anchor))

    def test_rate_limited_pending_reset(self):
        # resetsAt still points at the pending (now slightly past) reset
        reset = dt.datetime(2026, 9, 7, 4, 45, tzinfo=dt.timezone.utc).timestamp()
        now = reset + 3600  # past resetsAt -> pending
        s = dt.datetime.fromtimestamp(reset, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        anchor = _mod.anchor_from_resets_at(s, now)
        self.assertAlmostEqual(anchor, reset - _mod.WINDOW_SECONDS, delta=1)

    def test_anomalous_future_reset_none(self):
        reset = dt.datetime(2026, 9, 20, 4, 45).timestamp()
        now = reset - 3600 * 10  # 10h before resetsAt -> >5h window, anomalous
        self.assertIsNone(_mod.anchor_from_resets_at(
            dt.datetime.utcfromtimestamp(reset).isoformat(), now))

    def test_garbage_none(self):
        self.assertIsNone(_mod.anchor_from_resets_at("not-a-date"))
        self.assertIsNone(_mod.anchor_from_resets_at(None))

    def test_window_key_anchored(self):
        anchor = 1_700_000_000
        k1 = _mod.window_key(anchor + 10, anchor)
        k2 = _mod.window_key(anchor + _mod.WINDOW_SECONDS - 1, anchor)
        k3 = _mod.window_key(anchor + _mod.WINDOW_SECONDS + 1, anchor)
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_window_key_epoch_fallback_stable(self):
        ts = 1_757_240_000
        self.assertEqual(_mod.window_key(ts), _mod.window_key(ts))


class TestSyncLedger(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"session_id": "s1", "model": "qwen3.8-flash", "calls": 10,
             "in": 100_000, "out": 20_000, "cache": 50_000,
             "last_seen": "2026-09-07T01:00:00+00:00"},
            {"session_id": "s2", "model": "glm-5.2", "calls": 4,
             "in": 5_000, "out": 50_000, "reason": 10_000, "cache": 100_000,
             "last_seen": "2026-09-07T01:10:00+00:00"},
            {"session_id": "s3", "model": "ollama-only", "provider": "ollama-cloud",
             "calls": 50, "in": 1e6, "out": 1e5, "last_seen": "2026-09-07T01:10:00+00:00"},
        ]
        self.home = make_fake_home(self.rows, tag="sync")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def _ledger_rows(self):
        p = _mod.ledger_path(self.home)
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def test_first_sync_two_opencode_rows(self):
        n = _mod.sync_ledger(self.home, now=1_800_000_000)
        self.assertEqual(n, 2)  # s3 is ollama-cloud -> excluded
        rows = self._ledger_rows()
        self.assertEqual({r["model"] for r in rows}, {"qwen3.8-flash", "glm-5.2"})
        q = [r for r in rows if r["model"] == "qwen3.8-flash"][0]
        self.assertAlmostEqual(q["cost"],
                               100_000 / 1e6 * 0.15 + 20_000 / 1e6 * 0.47 + 50_000 / 1e6 * 0.016,
                               places=6)
        self.assertEqual(q["request_count"], 10)

    def test_second_sync_idempotent(self):
        _mod.sync_ledger(self.home, now=1_800_000_000)
        n2 = _mod.sync_ledger(self.home, now=1_800_000_000)
        self.assertEqual(n2, 0)
        self.assertEqual(len(self._ledger_rows()), 2)

    def test_delta_accumulation(self):
        _mod.sync_ledger(self.home, now=1_800_000_000)
        # bump s1 totals in the DB
        db = sqlite3.connect(os.path.join(self.home, "profiles/pr-opencode/state.db"))
        db.execute("UPDATE session_model_usage SET api_call_count=15,"
                   " input_tokens=150000, output_tokens=25000,"
                   " cache_read_tokens=60000, last_seen='2026-09-07T01:30:00+00:00'"
                   " WHERE session_id='s1'")
        db.commit()
        db.close()
        n = _mod.sync_ledger(self.home, now=1_800_000_100)
        self.assertEqual(n, 1)
        last = self._ledger_rows()[-1]
        self.assertEqual(last["request_count"], 5)
        self.assertEqual(last["tokens"]["in"], 50_000)
        self.assertAlmostEqual(last["cost"],
                               50_000 / 1e6 * 0.15 + 5_000 / 1e6 * 0.47 + 10_000 / 1e6 * 0.016,
                               places=6)

    def test_negative_delta_clamped(self):
        _mod.sync_ledger(self.home, now=1_800_000_000)
        db = sqlite3.connect(os.path.join(self.home, "profiles/pr-opencode/state.db"))
        db.execute("UPDATE session_model_usage SET api_call_count=1, input_tokens=10,"
                   " output_tokens=0, cache_read_tokens=0 WHERE session_id='s1'")
        db.commit()
        db.close()
        n = _mod.sync_ledger(self.home, now=1_800_000_200)
        self.assertEqual(n, 0)  # reset -> no positive delta -> no row
        cur = _mod._load_json(_mod.cursor_path(self.home), {})
        self.assertEqual(cur["s1|qwen3.8-flash|"]["calls"], 1)  # realigned

    def test_if_due_throttles(self):
        n1 = _mod.sync_model_cost_ledger_if_due(self.home, now=1_800_000_000)
        self.assertGreaterEqual(n1, 1)
        n2 = _mod.sync_model_cost_ledger_if_due(self.home, now=1_800_000_000 + 60)
        self.assertEqual(n2, 0)  # not due (< 20 min)
        n3 = _mod.sync_model_cost_ledger_if_due(self.home, now=1_800_000_000 + 21 * 60)
        self.assertEqual(n3, 0)  # due but nothing changed

    def test_resolve_anchor_fallback_no_network(self):
        # fake home has no gate module nearby? scripts dir does have quota-gate.py;
        # but query_opencode_go will fail (no key in this env) and last-good file
        # is absent -> epoch fallback, never raises.
        anchor, src = _mod.resolve_anchor(self.home, now=1_800_000_000)
        self.assertIn(src, ("epoch-fallback", "last-good-cache", "live", "cached-last-good"))


class TestWarnings(unittest.TestCase):
    def setUp(self):
        self.home = make_fake_home([
            {"session_id": "s1", "model": "glm-5.2", "calls": 100,
             "in": 3_000_000, "out": 1_400_000, "cache": 40_000_000,
             "last_seen": "2026-09-07T01:10:00+00:00"},
        ], tag="warn")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def _write_ledger(self, window, cost=9.90):
        os.makedirs(_mod.state_dir(self.home), exist_ok=True)
        with open(_mod.ledger_path(self.home), "w") as fh:
            fh.write(json.dumps({
                "ts": 1_800_000_000, "window": window, "profile": "pr-opencode",
                "model": "glm-5.2", "cost": cost, "request_count": 100,
                "tokens": {"in": 1, "out": 1, "cache_read": 1},
                "session_id": "s1", "task": "",
            }) + "\n")

    NOW = 1_800_000_000

    def _current_window(self):
        # reset 1h after NOW -> anchor = NOW-4h -> current bucket start
        anchor = self.NOW + 3600 - _mod.WINDOW_SECONDS
        return _mod.window_key(self.NOW, anchor)

    def _reset_at(self):
        return (dt.datetime.utcfromtimestamp(self.NOW + 3600).isoformat() + "Z")

    def test_warning_above_half(self):
        self._write_ledger(self._current_window())
        warns = _mod.model_window_warnings(self.home, now=self.NOW, reset_at=self._reset_at())
        self.assertEqual(len(warns), 1)
        self.assertIn("WARNING: glm-5.2 consumed", warns[0])
        self.assertIn("82%", warns[0])  # 9.90/12.00 = 82.5%

    def test_no_warning_below_half(self):
        self._write_ledger(self._current_window(), cost=4.0)
        self.assertEqual(_mod.model_window_warnings(self.home, now=self.NOW, reset_at=self._reset_at()), [])

    def test_config_override_budget(self):
        os.makedirs(_mod.state_dir(self.home), exist_ok=True)
        with open(_mod.config_path(self.home), "w") as fh:
            json.dump({"window_usd": 100.0, "warn_fraction": 0.5}, fh)
        self._write_ledger(self._current_window())  # $9.90 of $100 -> 10%
        self.assertEqual(_mod.model_window_warnings(self.home, now=self.NOW, reset_at=self._reset_at()), [])

    def test_current_window_shares_shape(self):
        window = self._current_window()
        self._write_ledger(window)
        shares = _mod.current_window_shares(self.home, now=self.NOW, reset_at=self._reset_at())
        self.assertEqual(shares["window"], window)
        self.assertIn("glm-5.2", shares["models"])
        self.assertAlmostEqual(shares["models"]["glm-5.2"]["cost_usd"], 9.90, places=2)


class TestGateIntegration(unittest.TestCase):
    """quota-gate.py must degrade gracefully when the ledger is absent/broken."""

    def setUp(self):
        gp = os.path.join(SCRIPT_DIR, "scripts", "quota-gate.py")
        spec = importlib.util.spec_from_file_location("quota_gate_mcl", gp)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)

    def test_model_cost_context_never_raises(self):
        # point ledger module resolution at a broken state by unsetting cache
        self.gate._MODEL_LEDGER_MOD = self.gate._LEDGER_UNSET
        ctx, warns = self.gate.model_cost_context(None)
        self.assertIsInstance(warns, list)
        # restore
        self.gate._MODEL_LEDGER_MOD = self.gate._LEDGER_UNSET

    def test_model_cost_context_with_real_module(self):
        self.gate._MODEL_LEDGER_MOD = _mod
        import shutil
        home = make_fake_home([], tag="gate")
        orig_home = _mod.HERMES_HOME_DEFAULT
        _mod.HERMES_HOME_DEFAULT = home
        try:
            n = _mod.sync_ledger(home, now=1_800_000_000)
            self.assertEqual(n, 0)
            ctx, warns = self.gate.model_cost_context(None)
            self.assertIsInstance(warns, list)
        finally:
            _mod.HERMES_HOME_DEFAULT = orig_home
            self.gate._MODEL_LEDGER_MOD = self.gate._LEDGER_UNSET
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
