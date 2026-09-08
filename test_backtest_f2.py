#!/usr/bin/python3.12
"""test_backtest_f2.py — tests for backtest-f2.py (OBJ-24 F2 harness).

Verifies the snapshot → evaluate → day-verdict cycle with synthetic
time fixtures (same pattern as test_quota_forecast.py): perfect
prediction = error 0/OK, >20% margin deviation = FAIL, provider without
crossing yet = OPEN, and tolerant degradation on missing/corrupt
forecast/history/ledger. Everything runs in tmp: never touches the real
ledger or history.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = "REPO"
SPEC = importlib.util.spec_from_file_location(
    "backtest_f2", f"{PLUGIN}/scripts/backtest-f2.py")
assert SPEC is not None and SPEC.loader is not None
bf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bf)

DAY = 86400


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def snap(snap_epoch, reset_epoch, prov, pct_now, eta90_epoch):
    return {"kind": "snap", "ts": iso(snap_epoch), "reset": iso(reset_epoch),
            "providers": {prov: {
                "pct_now": pct_now,
                "eta_90_iso": iso(eta90_epoch) if eta90_epoch else None,
                "eta_100_iso": None,
                "burn_rate_pct_per_min": 0.1,
                "confidence": 3}}}


def forecast_of(snap_epoch, reset_epoch, prov, pct_now, eta90_epoch):
    """forecast.json dict shaped like quota-forecast.py output."""
    return json.dumps({
        "generated_at": iso(snap_epoch), "enabled": True,
        "next_weekly_reset_iso": iso(reset_epoch),
        "providers": {prov: {
            "pct_now": pct_now,
            "eta_90_iso": iso(eta90_epoch) if eta90_epoch else None,
            "eta_100_iso": None,
            "burn_rate_pct_per_min": 0.1,
            "confidence": 3}}})


def rows_ramp(prov_key, start_epoch, end_epoch, start_pct, end_pct,
              step=900):
    """Linear ramp of metrics rows from (start,start_pct) to (end,end_pct)."""
    out = []
    t = start_epoch
    span = max(end_epoch - start_epoch, 1)
    while t <= end_epoch:
        frac = (t - start_epoch) / span
        out.append({"ts": iso(t), prov_key: round(start_pct + frac * (end_pct - start_pct), 3)})
        t += step
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fc = self.tmp / "forecast.json"
        self.hist = self.tmp / "metrics.jsonl"
        self.ledger = self.tmp / "backtest.jsonl"
        bf.FORECAST = self.fc
        bf.HISTORY = self.hist
        bf.LEDGER = self.ledger

    def run_main(self):
        rc = bf.main()
        self.assertEqual(rc, 0)

    def ledger_lines(self):
        return bf.read_jsonl(self.ledger)

    def by_kind(self, kind):
        return [r for r in self.ledger_lines() if r.get("kind") == kind]


class TestErrorWindow(Base):
    """Core geometry: predicted eta vs actual crossing vs margin to reset."""

    def test_perfect_prediction_zero_error_ok(self):
        # snap t=0, reset +5d, pct 80; crosses 90 exactly at eta_90
        t0 = int(time.time() - 10 * DAY)  # far from "now": stable windows
        reset = t0 + 5 * DAY
        cross = t0 + 2 * DAY
        self.fc.write_text(forecast_of(t0, reset, "pr-ollama", 80.0, cross))
        # history: ramp 80 -> 95, crosses 90 exactly at `cross`
        key_o = bf.qf.METRICAS_WEEKLY["pr-ollama"]
        hist = rows_ramp(key_o, t0, cross, 80.0, 90.0) + \
               rows_ramp(key_o, cross + 900, cross + DAY, 90.2, 95.0)
        self.hist.write_text("\n".join(json.dumps(r) for r in hist) + "\n")
        self.run_main()
        res = self.by_kind("res")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["prov"], "pr-ollama")
        self.assertEqual(res[0]["status"], "OK")
        self.assertLess(res[0]["error_pct"], 1.0)  # ~0, ramp rounding
        day = self.by_kind("day")
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0]["verdict"], "OK")
        self.assertEqual(day[0]["day"], iso(t0)[:10])

    def test_deviation_30pct_of_margin_fail(self):
        # margin to reset = 10h; real error = 3h = 30% > 20% -> FAIL
        t0 = int(time.time() - 10 * DAY)
        reset = t0 + 10 * 3600
        eta_pred = t0 + 2 * 3600
        cross = eta_pred + 3 * 3600     # 30% of the margin off
        self.fc.write_text(forecast_of(t0, reset, "pr-nanogpt", 85.0, eta_pred))
        key_n = bf.qf.METRICAS_WEEKLY["pr-nanogpt"]
        hist = rows_ramp(key_n, t0, cross, 85.0, 90.0) + \
               rows_ramp(key_n, cross + 900, reset - 60, 90.1, 92.0)
        self.hist.write_text("\n".join(json.dumps(
            {"ts": r["ts"], key_n: r[key_n]}) for r in hist) + "\n")
        self.run_main()
        res = self.by_kind("res")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "FAIL")
        self.assertGreater(res[0]["error_pct"], 20.0)
        self.assertLess(res[0]["error_pct"], 40.0)  # ~30%, not absurd
        day = self.by_kind("day")
        self.assertEqual(day[0]["verdict"], "FAIL")

    def test_no_crossing_yet_open(self):
        # snap with future eta and history that never reaches 90 -> OPEN
        t0 = int(time.time() - 12 * 3600)  # snap 12h ago, reset +40h
        reset = t0 + 40 * 3600
        eta_pred = t0 + 20 * 3600
        self.fc.write_text(forecast_of(t0, reset, "pr-ollama", 30.0, eta_pred))
        hist = [{"ts": iso(t0 + i * 900),
                 bf.qf.METRICAS_WEEKLY["pr-ollama"]: 30.0 + i * 0.2}
                for i in range(0, 40)]  # climbs to ~38%, far below 90
        self.hist.write_text("\n".join(json.dumps(r) for r in hist) + "\n")
        self.run_main()
        self.assertEqual(self.by_kind("res"), [])
        day = self.by_kind("day")
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0]["verdict"], "OPEN")
        self.assertEqual(day[0]["n_open"], 1)

    def test_reset_elapses_without_crossing_na(self):
        # the weekly window elapsed below 90 -> NA, day OK
        t0 = int(time.time() - 10 * DAY)
        reset = t0 + 2 * DAY
        self.fc.write_text(forecast_of(t0, reset, "pr-ollama", 20.0,
                                       t0 + 3600))
        hist = [{"ts": iso(t0 + i * 3600),
                 bf.qf.METRICAS_WEEKLY["pr-ollama"]: 20.0 + i * 0.1}
                for i in range(26, 0, -1)][::-1]
        self.hist.write_text("\n".join(json.dumps(r) for r in hist) + "\n")
        self.run_main()
        res = self.by_kind("res")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "NA")
        self.assertEqual(res[0]["reason"], "no_cross_before_reset")
        self.assertEqual(self.by_kind("day")[0]["verdict"], "OK")

    def test_snap_past_milestone_na_not_fail(self):
        # forecast with pct_now >= 90: nothing left to predict -> NA
        t0 = int(time.time() - 5 * DAY)
        reset = t0 + 2 * DAY
        self.fc.write_text(forecast_of(t0, reset, "pr-ollama", 93.0, None))
        self.hist.write_text("")
        self.run_main()
        res = self.by_kind("res")
        self.assertEqual(res[0]["status"], "NA")
        self.assertEqual(res[0]["reason"], "pct_already_past")
        self.assertEqual(self.by_kind("day")[0]["verdict"], "OK")

    def test_crossing_after_snap_ok_with_right_eta(self):
        t0 = int(time.time() - 10 * DAY)
        reset = t0 + 6 * DAY
        cross = t0 + 3 * DAY
        s_ok = snap(t0, reset, "pr-ollama", 70.0, cross)
        self.fc.write_text("{}")
        self.hist.write_text("\n".join(json.dumps(r) for r in
                       rows_ramp(bf.qf.METRICAS_WEEKLY["pr-ollama"],
                                 t0, cross, 70.0, 90.0)) + "\n")
        snaps = [s_ok]
        series = bf.crossings_by_provider(bf.read_jsonl(self.hist))
        res = bf.evaluate_snapshots(snaps, series, set(), time.time())
        self.assertEqual(res[0]["status"], "OK")
        self.assertLess(res[0]["error_pct"], 5.0)


class TestSnapshotFlow(Base):
    """The snapshot mode: forecast.json appended to the ledger each run."""

    def test_snapshot_persists_and_dedupes(self):
        t0 = int(time.time() - 3 * DAY)
        fc = {"generated_at": iso(t0), "enabled": True,
              "next_weekly_reset_iso": iso(t0 + 4 * DAY),
              "providers": {"pr-ollama": {
                  "pct_now": 40.0, "eta_90_iso": None,
                  "eta_100_iso": None, "burn_rate_pct_per_min": 0.0,
                  "confidence": 1}}}
        self.fc.write_text(json.dumps(fc))
        self.hist.write_text("")
        self.run_main()
        snaps = self.by_kind("snap")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["ts"], iso(t0))
        # second run with the SAME forecast.json -> no duplicate snap
        self.run_main()
        self.assertEqual(len(self.by_kind("snap")), 1)
        # new forecast (different generated_at) -> appended
        fc2 = dict(fc, generated_at=iso(t0 + 900))
        self.fc.write_text(json.dumps(fc2))
        self.run_main()
        self.assertEqual(len(self.by_kind("snap")), 2)

    def test_forecast_enabled_false_no_snap(self):
        self.fc.write_text(json.dumps({"generated_at": iso(time.time()),
                                       "enabled": False, "providers": {}}))
        self.run_main()
        self.assertEqual(self.by_kind("snap"), [])


class TestTolerantDegradation(Base):
    """(d) Missing or malformed inputs: silent skip, no exception, exit 0."""

    def test_missing_files(self):
        # no forecast, no history, no ledger
        self.run_main()
        self.assertFalse(self.ledger.exists() and self.ledger.read_text())

    def test_malformed_forecast(self):
        self.fc.write_text("{not json")
        self.run_main()

    def test_malformed_history(self):
        t0 = int(time.time() - 3 * DAY)
        self.fc.write_text(json.dumps({
            "generated_at": iso(t0), "enabled": True,
            "next_weekly_reset_iso": iso(t0 + 4 * DAY),
            "providers": {"pr-ollama": {
                "pct_now": 50.0, "eta_90_iso": iso(t0 + 3600),
                "burn_rate_pct_per_min": 0.5, "confidence": 2}}}))
        self.hist.write_text("basura\n\x00\x01\n[1,2]\n")
        self.run_main()   # no raise; snap recorded, everything OPEN
        self.assertEqual(len(self.by_kind("snap")), 1)
        self.assertEqual(self.by_kind("day")[0]["verdict"], "OPEN")

    def test_ledger_with_broken_lines(self):
        self.ledger.write_text('{"kind":"snap","ts":oops\n{}\n[42]\n"str"\n')
        self.fc.write_text(json.dumps({
            "generated_at": iso(time.time() - 3 * DAY), "enabled": True,
            "next_weekly_reset_iso": iso(time.time() + 3 * DAY),
            "providers": {"pr-ollama": {
                "pct_now": 20.0, "eta_90_iso": None,
                "burn_rate_pct_per_min": 0.0, "confidence": 0}}}))
        self.hist.write_text("")
        self.run_main()   # read_jsonl drops the junk, keeps running
        self.assertTrue(any(r.get("kind") == "snap"
                            for r in self.ledger_lines()))

    def test_second_run_idempotent(self):
        t0 = int(time.time() - 4 * DAY)
        reset = t0 + 3 * DAY
        self.fc.write_text(json.dumps({
            "generated_at": iso(t0), "enabled": True,
            "next_weekly_reset_iso": iso(reset),
            "providers": {"pr-ollama": {
                "pct_now": 95.0, "eta_90_iso": None,
                "burn_rate_pct_per_min": None, "confidence": 0}}}))
        self.hist.write_text("")
        self.run_main()
        n_lines = len(self.ledger_lines())
        self.run_main()
        self.assertEqual(len(self.ledger_lines()), n_lines)


class TestHelpers(unittest.TestCase):
    def test_first_crossing_boundaries(self):
        pts = [(100.0, 80.0), (200.0, 90.0), (300.0, 95.0)]
        self.assertEqual(bf.first_crossing(pts, 0, 1000), 200.0)
        # strictly after: crossing at 200 is skipped, next >=90 is 300
        self.assertEqual(bf.first_crossing(pts, 200, 1000), 300.0)
        self.assertEqual(bf.first_crossing(pts, 150, 1000), 200.0)
        self.assertIsNone(bf.first_crossing(pts, 0, 150))     # upto window
        self.assertIsNone(bf.first_crossing(pts, 300, 1000))

    def test_crossings_dedupes_and_sorts(self):
        rows = [{"ts": "2026-09-08T00:00:00Z", "ollama_weekly_pct": 10},
                {"ts": "2026-09-08T00:00:00Z", "ollama_weekly_pct": 11},
                {"ts": "nonsense", "ollama_weekly_pct": 99}]
        series = bf.crossings_by_provider(rows)
        self.assertEqual(series["pr-ollama"], [(
            bf.qf._parse_iso("2026-09-08T00:00:00Z"), 11.0)])
        self.assertEqual(series["pr-nanogpt"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
