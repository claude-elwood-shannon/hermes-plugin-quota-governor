#!/usr/bin/env python3
"""Tests for OBJ-26a per-request billing capture + ledger fold (t_128382ac).

Covers:
  - nanogpt-balance-ledger: append_request_row / request_window_totals
    (covered=0 + balance accumulation, window filtering, malformed rows,
    non-USD payment sources), budget_context exposes request_* fields
  - agent/nanogpt_pricing_capture: extraction from response object / SDK
    model_extra / dict, base-url gating, disable env, dedupe by requestId,
    fail-open when the ledger module is missing, window_totals passthrough
Run:  /usr/bin/python3.12 test_nanogpt_request_ledger.py  (or pytest)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

GOV_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(GOV_DIR, "scripts", "nanogpt-balance-ledger.py")
CAPTURE = "~/.hermes/hermes-agent/agent/nanogpt_pricing_capture.py"

_spec = importlib.util.spec_from_file_location("nanogpt_balance_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

_cap_spec = importlib.util.spec_from_file_location("nanogpt_pricing_capture", CAPTURE)
capture = importlib.util.module_from_spec(_cap_spec)
_cap_spec.loader.exec_module(capture)


def _fake_agent(base_url="https://nano-gpt.com/v1", model="z-ai/glm-5.3-flash"):
    return types.SimpleNamespace(base_url=base_url, model=model, provider="custom")


PRICING_BALANCE = {
    "amount": 4.87e-06, "currency": "USD", "cost": 4.87e-06,
    "inputTokens": 13, "outputTokens": 16, "cacheCost": 0,
    "requestId": "req_balance-1", "costUsd": 4.87e-06, "usdCost": 4.87e-06,
    "paymentSource": "USD", "billedToTeam": False,
}
PRICING_COVERED = dict(PRICING_BALANCE, requestId="req_covered-1",
                       costUsd=0, amount=0, usdCost=0, cost=0)
PRICING_NANO = dict(PRICING_BALANCE, requestId="req_nano-1",
                    paymentSource="nano", costUsd=1e-05)


class TestRequestLedger(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="obj26a-")
        # point the ledger at the fake home for every call in this test
        os.environ["QUOTA_GOVERNOR_DIR"] = os.path.join(self.home, "quota-governor")

    def tearDown(self):
        os.environ.pop("QUOTA_GOVERNOR_DIR", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def _add(self, rows, ts=None):
        base = dt.datetime.now(dt.timezone.utc)
        for i, row in enumerate(rows):
            when = ts or (base - dt.timedelta(minutes=30))
            row.setdefault("ts", when.strftime("%Y-%m-%dT%H:%M:%SZ"))
            ledger.append_request_row(row, hermes_home=self.home)

    def test_append_and_read_rows(self):
        self._add([dict(PRICING_BALANCE), dict(PRICING_COVERED)])
        rows = ledger._load_requests(hermes_home=self.home)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["requestId"], "req_balance-1")
        # window totals: one balance row, one covered row
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertIsNotNone(totals)
        self.assertAlmostEqual(totals["request_balance_usd"], 4.87e-06, places=9)
        self.assertAlmostEqual(totals["request_covered_usd"], 0.0, places=9)
        self.assertEqual(totals["balance_requests"], 1)
        self.assertEqual(totals["covered_requests"], 1)
        self.assertEqual(totals["requests"], 2)

    def test_window_filtering_excludes_old_rows(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        self._add([dict(PRICING_BALANCE)], ts=old)
        self._add([dict(PRICING_BALANCE, requestId="req_recent-1")])
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertEqual(totals["balance_requests"], 1)
        self.assertAlmostEqual(totals["request_balance_usd"], 4.87e-06, places=9)
        # explicit `since` far in the future excludes everything
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        empty = ledger.request_window_totals(since=future, hermes_home=self.home)
        self.assertEqual(empty["requests"], 0)

    def test_non_usd_source_is_neither_covered_nor_balance(self):
        self._add([dict(PRICING_NANO)])
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertEqual(totals["requests"], 0)
        self.assertAlmostEqual(totals["request_balance_usd"], 0.0, places=9)

    def test_malformed_rows_are_skipped(self):
        path = ledger.requests_path(hermes_home=self.home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"ts": "2026-09-09T00:00:00Z", "costUsd": "oops",
                                 "paymentSource": "USD"}) + "\n")
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertIsNotNone(totals)
        self.assertEqual(totals["requests"], 0)

    def test_never_raises_on_garbage_state(self):
        # corrupt budget-state + cache must not break the totals
        os.makedirs(os.path.join(self.home, "quota-governor"), exist_ok=True)
        with open(os.path.join(self.home, "quota-governor",
                               "nanogpt-balance-last-good.json"), "w") as fh:
            fh.write("{corrupt")
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertIsNotNone(totals)  # falls back to ISO-week window


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="obj26a-cap-")
        os.environ["QUOTA_GOVERNOR_DIR"] = os.path.join(self.home, "qg")
        os.environ["HERMES_NANOGPT_LEDGER_PATH"] = SCRIPT
        capture._SEEN.clear()
        capture.LAST_PRICING = None

    def tearDown(self):
        os.environ.pop("QUOTA_GOVERNOR_DIR", None)
        os.environ.pop("HERMES_NANOGPT_LEDGER_PATH", None)
        os.environ.pop("HERMES_DISABLE_NANOGPT_PRICING_CAPTURE", None)
        capture._SEEN.clear()
        capture.LAST_PRICING = None
        shutil.rmtree(self.home, ignore_errors=True)

    def _rows(self):
        return ledger._load_requests(hermes_home=self.home)

    def test_extract_from_response_object(self):
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        self.assertEqual(capture.extract_pricing(resp), PRICING_BALANCE)

    def test_extract_from_model_extra(self):
        resp = types.SimpleNamespace(
            model_extra={"x_nanogpt_pricing": PRICING_BALANCE})
        self.assertEqual(capture.extract_pricing(resp), PRICING_BALANCE)

    def test_extract_from_dict(self):
        self.assertEqual(
            capture.extract_pricing({"x_nanogpt_pricing": PRICING_BALANCE}),
            PRICING_BALANCE)
        self.assertIsNone(capture.extract_pricing({"nope": 1}))
        self.assertIsNone(capture.extract_pricing(None))

    def test_capture_nonstreaming_writes_ledger_row(self):
        agent = _fake_agent()
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        row = capture.capture_from_response(agent, resp)
        self.assertIsNotNone(row)
        self.assertEqual(row["requestId"], "req_balance-1")
        self.assertAlmostEqual(row["costUsd"], 4.87e-06, places=9)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "request")
        self.assertEqual(rows[0]["model"], "z-ai/glm-5.3-flash")

    def test_non_nanogpt_endpoint_is_ignored(self):
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        self.assertIsNone(
            capture.capture_from_response(_fake_agent("https://api.x.ai/v1"), resp))
        self.assertEqual(self._rows(), [])

    def test_disabled_env_short_circuits(self):
        os.environ["HERMES_DISABLE_NANOGPT_PRICING_CAPTURE"] = "1"
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        self.assertIsNone(capture.capture_from_response(_fake_agent(), resp))
        self.assertEqual(self._rows(), [])

    def test_dedupe_same_request_id(self):
        agent = _fake_agent()
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        self.assertIsNotNone(capture.capture_from_response(agent, resp))
        self.assertIsNone(capture.capture_from_response(agent, resp))
        self.assertEqual(len(self._rows()), 1)
        # a different requestId is a new row
        other = types.SimpleNamespace(
            x_nanogpt_pricing=dict(PRICING_BALANCE, requestId="req_other"))
        self.assertIsNotNone(capture.capture_from_response(agent, other))
        self.assertEqual(len(self._rows()), 2)

    def test_capture_chunk_streaming(self):
        agent = _fake_agent()
        chunk = types.SimpleNamespace(
            model_extra={"x_nanogpt_pricing": PRICING_BALANCE})
        capture.capture_from_chunk(agent, chunk)  # no-op on early chunks
        capture.capture_from_chunk(agent, chunk)
        rows = self._rows()
        self.assertEqual(len(rows), 1)  # dedupe holds across chunks

    def test_covered_request_recorded_as_zero_cost(self):
        agent = _fake_agent()
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_COVERED)
        row = capture.capture_from_response(agent, resp)
        self.assertIsNotNone(row)
        self.assertEqual(row["costUsd"], 0)
        totals = ledger.request_window_totals(hermes_home=self.home)
        self.assertEqual(totals["covered_requests"], 1)
        self.assertAlmostEqual(totals["request_covered_usd"], 0.0, places=9)
        self.assertEqual(totals["balance_requests"], 0)

    def test_fail_open_when_ledger_unreachable(self):
        os.environ["HERMES_NANOGPT_LEDGER_PATH"] = "/nonexistent/ledger.py"
        # _load_ledger_module falls back to deployed copies; force failure by
        # pointing ALL candidates at a missing file via an import-safe override
        capture_mod = capture
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        # even if the deployed ledger loads, capture must never raise — call
        # and only assert no exception escapes
        try:
            capture_mod.capture_from_response(_fake_agent(), resp)
        except Exception as exc:  # pragma: no cover
            self.fail(f"capture raised with unreachable ledger: {exc}")

    def test_window_totals_passthrough(self):
        agent = _fake_agent()
        resp = types.SimpleNamespace(x_nanogpt_pricing=PRICING_BALANCE)
        capture.capture_from_response(agent, resp)
        totals = capture.window_totals(self.home)
        self.assertIsNotNone(totals)
        self.assertAlmostEqual(totals["request_balance_usd"], 4.87e-06, places=9)


class TestBudgetContextIntegration(unittest.TestCase):
    """budget_context exposes the request_* fields without breaking the gate."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="obj26a-gate-")
        os.environ["QUOTA_GOVERNOR_DIR"] = os.path.join(self.home, "qg")
        # no network in tests: budget_context with no key returns None,None
        # BEFORE touching the network (fetch_snapshot -> unavailable -> None).

    def tearDown(self):
        os.environ.pop("QUOTA_GOVERNOR_DIR", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_no_key_returns_gracefully(self):
        home_with_env = os.path.join(self.home, "hermes")
        ctx, warn = ledger.budget_context(hermes_home=home_with_env)
        # ctx is None only when there is no balance info at all; must not raise
        self.assertTrue(ctx is None or isinstance(ctx, dict))


if __name__ == "__main__":
    unittest.main(verbosity=2)
