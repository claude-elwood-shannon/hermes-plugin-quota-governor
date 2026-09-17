import sys
import pathlib
import importlib.util
import pytest

# Load module
MODULE_PATH = pathlib.Path("/data/git/hermes-plugin-quota-governor/scripts/obs/portal-build.py")
spec = importlib.util.spec_from_file_location("portal_build", str(MODULE_PATH))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Helpers
_fmt_ts = module._fmt_ts
_fmt_day = module._fmt_day
_slug = module._slug
_bar = module._bar

# Tests

def test_fmt_ts_valid():
    assert _fmt_ts(1695031200) == "18-Sep 12:00"

def test_fmt_ts_invalid():
    assert _fmt_ts("bad") == "-"

def test_fmt_day():
    assert _fmt_day(1695031200) == "18-Sep"

def test_slug():
    assert len(_slug("a"*70)) == 60
    assert _slug("!@#") == "___"

def test_bar():
    h = _bar(50.5)
    assert '<div class="bar "' in h
    assert 'width:50%' in h


def test_gather_alarm_lines_fail_open(monkeypatch, tmp_path):
    """One poisoned source must never tumble the portal build (t_cecc9dfe):
    alarm_lines degrades to [] like every other fail-open reader."""
    def _poison(*a, **k):
        raise RuntimeError("poisoned row")
    monkeypatch.setattr(module.ta, "run_checks", _poison)
    data = module.gather(hermes_home=str(tmp_path))
    assert data["alarm_lines"] == []
