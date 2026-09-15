"""Tests for burn-watchdog module functions.

This file contains a minimal set of tests exercising the public helper
functions that are required for the "OBJ‑AUTODEV" kanban card: ``is_burning``,
``meter_is_balance``, ``provider_cost`` and ``window_fields``.  It also tests
``_estimate_tick_cost`` – the internal cost estimation routine – because it
encodes the logic required by the unit‑tests in the kanban.

The tests make no network calls; all data is fabricated and passed directly
to the functions.

Each test follows the same form: build a provider dict that matches the
scenario described in the comment, call the function, and assert the
expected result.

The file is kept intentionally simple – only the necessary imports and
assertions are present – so it can be imported even by the CI environment
that does not have brand‑new tooling.
"""

import importlib.util
import os

# Load burn-watchdog module.
_spec = importlib.util.spec_from_file_location(
    "burn_watchdog", os.path.join("scripts", "burn-watchdog.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

# Expose functions.
is_burning = _module.is_burning
meter_is_balance = _module.meter_is_balance
provider_cost = _module.provider_cost
window_fields = _module.window_fields
_estimate_tick_cost = _module._estimate_tick_cost

# Tests
def test_is_burning_flag():
    provider = {"burning_balance": True}
    assert is_burning(provider) is True

def test_is_burning_window_status():
    provider = {"raw": {"window_pct": 25, "window_status": "limit_exceeded"}}
    assert is_burning(provider) is True

def test_is_burning_sane():
    provider = {"raw": {"window_pct": 25, "window_status": "ok"}}
    assert is_burning(provider) is False

def test_meter_is_balance_with_cost():
    provider = {"cost": 1.0}
    assert meter_is_balance(provider) is False

def test_meter_is_balance_with_balance():
    provider = {"balance": {"usd_balance": 50}}
    assert meter_is_balance(provider) is True

def test_provider_cost_from_cost():
    provider = {"cost": 2.5}
    assert provider_cost(provider) == 2.5

def test_provider_cost_from_raw():
    provider = {"raw": {"cost": 3.5}}
    assert provider_cost(provider) == 3.5

def test_provider_cost_from_balance():
    provider = {"balance": {"usd_balance": 7}}
    assert provider_cost(provider) == 7

def test_provider_cost_none():
    provider = {"raw": {"other": 0}}
    assert provider_cost(provider) is None

def test_window_fields_extraction():
    provider = {
        "raw": {
            "cpu_pct": 80,
            "cpu_status": "over",
            "io_pct": 30,
            "io_status": "ok",
        }
    }
    fields = window_fields(provider)
    assert len(fields) == 2
    assert any(f["name"] == "cpu" and f["pct"] == 80 and f["status"] == "over" for f in fields)
    assert any(f["name"] == "io" and f["pct"] == 30 and f["status"] == "ok" for f in fields)

def test_estimate_tick_cost_with_meter():
    provider = {"cost": 10}
    prior = {"last_cost": 8}
    cfg = {"window_usd_cap": 100}
    cost_now, tick_cost = _estimate_tick_cost(provider, prior, cfg, None, True)
    assert cost_now == 10
    assert tick_cost == 2

def test_estimate_tick_cost_with_balance_meter():
    provider = {"balance": {"usd_balance": 80}}
    prior = {"last_cost": 90}
    cfg = {"window_usd_cap": 100}
    cost_now, tick_cost = _estimate_tick_cost(provider, prior, cfg, None, True)
    assert cost_now == 80
    assert tick_cost == 10

def test_estimate_tick_cost_with_pct_estimate():
    provider = {"raw": {"cpu_pct": 30}}
    prior = {"last_pct": 10}
    cfg = {"window_usd_cap": 200}
    bottleneck = {"pct": 50}
    cost_now, tick_cost = _estimate_tick_cost(provider, prior, cfg, bottleneck, True)
    assert cost_now is None
    assert tick_cost == 80
