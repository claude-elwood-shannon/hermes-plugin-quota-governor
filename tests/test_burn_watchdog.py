import sys
import os
from pathlib import Path

# Add the repository root and scripts dir to sys.path so we can import burn_watchdog
ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = ROOT / "scripts"
sys.path.append(str(ROOT))
# Import burn_watchdog module
try:
    # load burn_watchdog as module from file
    from importlib import machinery
    script_path = SCRIPTS_DIR / "burn-watchdog.py"
    loader = machinery.SourceFileLoader("burn_watchdog", str(script_path))
    bw = loader.load_module()
except Exception as exc:
    raise ImportError(f"cannot import burn_watchdog: {exc}")

import pytest

# Helper to create provider dicts

def simple_provider(**kwargs):
    return {"provider": "test", **kwargs}

# Tests

def test_window_fields_parses_window_data():
    prov = simple_provider(raw={"usage_pct": 55.2, "usage_status": "ok", "buffer_pct": 78.4, "buffer_status": "rate-limited"})
    windows = bw.window_fields(prov)
    assert len(windows) == 2
    names = {w["name"] for w in windows}
    assert names == {"usage", "buffer"}
    for w in windows:
        if w["name"] == "usage":
            assert w["pct"] == 55.2
            assert w["status"] == "ok"
        else:
            assert w["pct"] == 78.4
            assert w["status"] == "rate-limited"


def test_provider_cost_prioritises_cost_fields():
    # explicit cost key
    prov = simple_provider(cost=123.45, raw={"cost": 99.9})
    assert bw.provider_cost(prov) == 123.45
    # fallback to raw cost
    prov = simple_provider(raw={"cost": 67.89})
    assert bw.provider_cost(prov) == 67.89
    # fallback to raw activity_cost
    prov = simple_provider(raw={"activity_cost": 45.0})
    assert bw.provider_cost(prov) == 45.0
    # fallback to balance.usd_balance
    prov = simple_provider(balance={"usd_balance": 23.21})
    assert bw.provider_cost(prov) == 23.21
    # no cost at all
    prov = simple_provider()
    assert bw.provider_cost(prov) is None


def test_meter_is_balance_respects_balance_meter():
    # balance meter present, no cost
    prov = simple_provider(raw={"some":1}, balance={"usd_balance": 10.5})
    assert bw.meter_is_balance(prov) is True
    # cost present overrides balance
    prov = simple_provider(cost=5.0, raw={"cost": 2.0}, balance={"usd_balance": 10.5})
    assert bw.meter_is_balance(prov) is False
    # raw cost present overrides balance
    prov = simple_provider(raw={"cost": 3.8}, balance={"usd_balance": 9.1})
    assert bw.meter_is_balance(prov) is False


def test_is_burning_true_conditions():
    # burning_balance flag
    prov = simple_provider(burning_balance=True)
    assert bw.is_burning(prov)
    # window status not ok
    prov = simple_provider(raw={"usage_pct": 50, "usage_status": "rate-limited"})
    assert bw.is_burning(prov)
    # no burning indicators
    prov = simple_provider(raw={"usage_pct": 50, "usage_status": "ok"})
    assert bw.is_burning(prov) is False


def test_estimate_tick_cost_with_cost_meter():
    # cost meter present, cumulative
    provider = simple_provider(cost=10.0)
    prior = {"last_cost": 5.0}
    cfg = {"window_usd_cap": None}
    tick_res = bw._estimate_tick_cost(provider, prior, cfg, None, False)
    assert tick_res[0] == 10.0  # cost_now
    assert tick_res[1] == 5.0   # tick_cost

    # decreasing balance meter
    provider = simple_provider(balance={"usd_balance": 3.0})
    prior = {"last_cost": 5.0}
    tick_res = bw._estimate_tick_cost(provider, prior, cfg, None, False)
    assert tick_res[0] == 3.0
    # clamp: last_cost 5 - now 3 = 2 positive
    assert tick_res[1] == 2.0

    # percent delta estimation when no cost
    provider = simple_provider(raw={"usage_pct": 70})
    prior = {"last_pct": 50}
    cfg = {"window_usd_cap": 200.0}
    bottleneck = {"pct": 80}
    tick_res = bw._estimate_tick_cost(provider, prior, cfg, bottleneck, True)
    # pct delta = 80-50=30; usd=30/100*200=60
    assert tick_res[1] == 60.0

