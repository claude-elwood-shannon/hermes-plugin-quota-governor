#!/usr/bin/python3.12
"""Tests for efficiency-streak.py (OBJ-METRICS). Offline fixtures only.

Loads the kebab-named CLI script by path (importlib.util) and exercises
the OBJ-METRICS gauge against temporary logs (pytest tmp_path): no
network, no board, no host state.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "efficiency_streak", _HERE / "scripts" / "obs" / "efficiency-streak.py")
assert _SPEC is not None and _SPEC.loader is not None  # repo layout is fixed
es = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(es)


def _rec(day: str, hour: int, veredicto: str) -> str:
    """One efficiency_ratio JSON line for `day` at `hour`:00:01Z."""
    return json.dumps({"ts": f"{day}T{hour:02d}:00:01Z",
                       "kind": "efficiency_ratio",
                       "window": "24h",
                       "veredicto": veredicto})


def _log(tmp_path: Path, lines: list[str]) -> Path:
    """Write a temporary efficiency-ratio log from raw lines."""
    path = tmp_path / "efficiency-ratio.log"
    path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
    return path


def test_intermediate_critico_day_breaks_streak(tmp_path):
    """(a) A CRITICO day inside the run resets the streak."""
    lines = [_rec("2026-09-13", 23, "OK"),
             _rec("2026-09-14", 23, "CRITICO"),
             _rec("2026-09-15", 23, "BAJO"),
             _rec("2026-09-16", 23, "OK")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert rep["days"]["2026-09-14"] == "CRITICO"
    assert rep["streak_days"] == 2
    assert rep["meets_3_day_criterion"] is False
    assert rep["latest_day"] == "2026-09-16"
    assert rep["latest_veredicto"] == "OK"


def test_missing_day_gap_breaks_streak(tmp_path):
    """(b) A day absent from the log ends the streak."""
    lines = [_rec("2026-09-13", 23, "OK"),
             _rec("2026-09-14", 23, "OK"),
             # 2026-09-15 never appears
             _rec("2026-09-16", 23, "OK")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert "2026-09-15" not in rep["days"]
    assert rep["streak_days"] == 1
    assert rep["meets_3_day_criterion"] is False


def test_three_no_critico_days_meet_criterion(tmp_path):
    """(c) 3 consecutive no-CRITICO days -> meets_3_day_criterion."""
    lines = [_rec("2026-09-13", 23, "CRITICO"),
             _rec("2026-09-14", 23, "BAJO"),
             _rec("2026-09-15", 23, "BAJO"),
             _rec("2026-09-16", 23, "OK")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert rep["streak_days"] == 3
    assert rep["meets_3_day_criterion"] is True


def test_non_json_lines_ignored(tmp_path):
    """(d) Liveness tick lines and garbage never affect the report."""
    lines = ["[2026-09-16 22:00:01] efficiency-ratio: tick",
             "not json at all {",
             _rec("2026-09-16", 23, "OK")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert rep["streak_days"] == 1
    assert list(rep["days"]) == ["2026-09-16"]


def test_last_record_of_day_wins(tmp_path):
    """A CRITICO morning followed by an OK close is a no-CRITICO day."""
    lines = [_rec("2026-09-15", 8, "CRITICO"),
             _rec("2026-09-15", 23, "OK"),
             _rec("2026-09-16", 8, "CRITICO"),
             _rec("2026-09-16", 23, "OK")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert rep["streak_days"] == 2


def test_missing_log_fails_open_zeroed(tmp_path, capsys):
    """Missing or empty log: zeroed report, exit 0, JSON still printed."""
    missing = tmp_path / "nope.log"
    rep = es.build_report(es.parse_log(missing))
    assert rep == {"days": {}, "streak_days": 0,
                   "meets_3_day_criterion": False,
                   "latest_day": None, "latest_veredicto": None}
    assert es.main(["--log", str(missing), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["streak_days"] == 0
    assert out["meets_3_day_criterion"] is False


def test_non_ratio_records_and_bad_types_skipped(tmp_path):
    """Foreign JSON kinds, missing ts/veredicto are skipped safely."""
    lines = [json.dumps({"ts": "2026-09-16T00:00:01Z", "kind": "other"}),
             json.dumps({"kind": "efficiency_ratio", "veredicto": "OK"}),
             _rec("2026-09-16", 23, "BAJO")]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert rep["days"] == {"2026-09-16": "BAJO"}


def test_days_window_is_last_seven(tmp_path):
    """`days` carries at most the 7 most recent logged days."""
    lines = [_rec(f"2026-09-{d:02d}", 12, "OK") for d in range(1, 11)]
    rep = es.build_report(es.parse_log(_log(tmp_path, lines)))
    assert list(rep["days"]) == [f"2026-09-{d:02d}" for d in range(4, 11)]
