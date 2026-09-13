#!/usr/bin/python3.12
"""test_objective_budgets.py — OBJ-28 phase 0: rollup by objective tag.

Covers (fixtures only, no network, no live state):
  1. read_trace parses JSONL, skips corrupt lines, honours window_days
  2. rollup: per-objective flow from task-events (all backfill kinds)
  3. rollup: cost lines land in unattributed_cost (per-provider split)
  4. rollup: tagged_events / unattributed_events / cost_lines_attributed
  5. compute_status ladder: open/warn/exhausted/done + no-budget = open
  6. variance_breach: armed rule, None without a window price
  7. write_budgets atomic write + load_existing round-trip
  8. preserve_human_decisions: sticky budgets, notes survive, status recomputes
  9. main() end-to-end under HERMES_HOME fixture + idempotence (silent 2nd run)
 10. privacy: no absolute host paths in the repo module (portability)

Run:  /usr/bin/python3.12 test_objective_budgets.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

GOV_DIR = str(Path(__file__).resolve().parent.parent.parent)
SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "objective-budgets.py")

_spec = importlib.util.spec_from_file_location("obj_budgets", SCRIPT)
ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ob)


def _row(ts=1000.0, source="task-events", objective="OBJ-01", cause="claimed",
         tid="t_1", provider=None, cost=None):
    return {
        "ts_epoch_utc": ts, "consumer_class": "worker",
        "consumer_id": tid, "cause": cause, "model": None,
        "provider": provider, "tokens_in": None, "tokens_out": None,
        "costUsd": cost, "requestId": None, "objective": objective,
        "source": source, "otel": {},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obj-budgets-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def budgets_file(self):
        return Path(self.tmp) / "quota-governor" / "objective-budgets.json"


class TestReadTrace(Base):
    def test_reads_valid_jsonl(self):
        tp = ob.trace_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
        rows = ob.read_trace()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["objective"], "OBJ-01")

    def test_skips_corrupt_lines(self):
        tp = ob.trace_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text("not json\n" + json.dumps(_row()) + "\n", encoding="utf-8")
        self.assertEqual(len(ob.read_trace()), 1)

    def test_window_filters_old_rows(self):
        tp = ob.trace_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        old = _row(ts=1.0)
        new = _row(ts=9e9, tid="t_2")
        tp.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n",
                      encoding="utf-8")
        rows = ob.read_trace(window_days=1)
        self.assertEqual([r["consumer_id"] for r in rows], ["t_2"])

    def test_missing_file_fails_open(self):
        self.assertEqual(ob.read_trace(), [])


class TestRollup(Base):
    def test_flow_per_objective(self):
        rows = [
            _row(cause="created", tid="t_1", objective="OBJ-01"),
            _row(cause="claimed", tid="t_1", objective="OBJ-01"),
            _row(cause="completed", tid="t_1", objective="OBJ-01"),
            _row(cause="crashed", tid="t_2", objective="OBJ-01"),
            _row(cause="gave_up", tid="t_3", objective="OBJ-02"),
            _row(cause="timed_out", tid="t_4", objective="OBJ-02"),
            _row(cause="spawn_failed", tid="t_5", objective="OBJ-02"),
        ]
        doc = ob.rollup(rows, now=1000.0)
        o1 = doc["objectives"]["OBJ-01"]
        self.assertEqual(o1["flow"]["created"], 1)
        self.assertEqual(o1["flow"]["claimed"], 1)
        self.assertEqual(o1["flow"]["completed"], 1)
        self.assertEqual(o1["flow"]["crashed"], 1)
        self.assertEqual(o1["tasks_total"], 2)
        self.assertEqual(o1["tasks_done"], 1)
        self.assertEqual(o1["tasks_active"], 1)
        o2 = doc["objectives"]["OBJ-02"]
        self.assertEqual(o2["flow"]["gave_up"], 1)
        self.assertEqual(o2["flow"]["timed_out"], 1)
        self.assertEqual(o2["flow"]["spawn_failed"], 1)

    def test_cost_lands_in_unattributed(self):
        rows = [
            _row(source="model-cost-ledger", provider="opencode-go",
                 cost=1.5, objective="unattributed"),
            _row(source="usage-audit", provider=None, cost=0.5,
                 objective="unattributed"),
            _row(source="nanogpt-requests", provider="custom", cost=0.25,
                 objective="unattributed"),
            _row(cause="created", tid="t_9", objective="OBJ-05"),
        ]
        doc = ob.rollup(rows, now=1000.0)
        ua = doc["unattributed_cost"]
        self.assertAlmostEqual(ua["spent_usd"], 2.25, places=6)
        self.assertEqual(ua["lines"], 3)
        self.assertAlmostEqual(ua["by_provider"]["opencode-go"], 1.5, places=6)
        self.assertAlmostEqual(ua["by_provider"]["unknown"], 0.5, places=6)
        self.assertAlmostEqual(ua["by_provider"]["custom"], 0.25, places=6)
        self.assertEqual(doc["cost_lines_attributed"], 0)
        self.assertEqual(doc["objectives"]["OBJ-05"]["spent_usd"], 0.0)

    def test_tagged_cost_lands_on_objective(self):
        # forward-compat: a cost-bearing line that CARRIES the stamp
        rows = [
            _row(source="usage-audit", provider=None, cost=0.5,
                 objective="OBJ-05"),
            _row(source="nanogpt-requests", provider="custom", cost=0.25,
                 objective="OBJ-05"),
            _row(source="usage-audit", provider=None, cost=0.1,
                 objective="unattributed"),
        ]
        doc = ob.rollup(rows, now=1000.0)
        self.assertEqual(doc["cost_lines_attributed"], 2)
        self.assertAlmostEqual(doc["objectives"]["OBJ-05"]["spent_usd"],
                               0.75, places=6)
        self.assertAlmostEqual(doc["unattributed_cost"]["spent_usd"], 0.1,
                               places=6)

    def test_tagged_event_counters(self):
        rows = [
            _row(cause="created", tid="t_1", objective="OBJ-01"),
            _row(cause="created", tid="t_x", objective="unattributed"),
            _row(source="usage-audit", provider=None, cost=0.1),
        ]
        doc = ob.rollup(rows, now=1000.0)
        self.assertEqual(doc["tagged_events"], 1)
        self.assertEqual(doc["unattributed_events"]["lines"], 1)

    def test_empty_rows(self):
        doc = ob.rollup([], now=1000.0)
        self.assertEqual(doc["objectives"], {})
        self.assertEqual(doc["unattributed_cost"]["spent_usd"], 0.0)


class TestStatusLadder(Base):
    def test_no_budget_is_open(self):
        self.assertEqual(ob.compute_status(99.0, None, 5, 1), "open")

    def test_ladder(self):
        self.assertEqual(ob.compute_status(0.0, 1.0, 5, 0), "open")
        self.assertEqual(ob.compute_status(0.69, 1.0, 5, 0), "open")
        self.assertEqual(ob.compute_status(0.70, 1.0, 5, 0), "warn")
        self.assertEqual(ob.compute_status(0.99, 1.0, 5, 0), "warn")
        self.assertEqual(ob.compute_status(1.0, 1.0, 5, 0), "exhausted")
        self.assertEqual(ob.compute_status(0.0, 1.0, 5, 5), "done")

    def test_done_never_overrides_breach(self):
        # exhausted wins even when tasks are all done (spend is spend)
        self.assertEqual(ob.compute_status(1.5, 1.0, 5, 5), "exhausted")


class TestVariance(Base):
    def test_unexercisable_without_window_price(self):
        self.assertIsNone(ob.variance_breach(1.0, 0.5, None))
        self.assertIsNone(ob.variance_breach(None, 0.5, 0.01))
        self.assertIsNone(ob.variance_breach(1.0, None, 0.01))

    def test_breach_at_3x(self):
        # est = 0.5% * $0.02/pct = $0.01; 3x = $0.03
        self.assertFalse(ob.variance_breach(0.02, 0.5, 0.02))
        self.assertTrue(ob.variance_breach(0.04, 0.5, 0.02))
        self.assertFalse(ob.variance_breach(0.03, 0.5, 0.02))  # boundary: not > 3x


class TestPersistence(Base):
    def test_write_and_load_roundtrip(self):
        doc = ob.rollup([_row()], now=1000.0)
        self.assertTrue(ob.write_budgets(doc))
        back = ob.load_existing()
        self.assertEqual(back["objectives"]["OBJ-01"]["tasks_total"], 1)
        # atomic write leaves no tmp behind
        self.assertFalse(ob.budgets_path().with_suffix(".json.tmp").exists())

    def test_preserve_human_decisions(self):
        prev = {
            "objectives": {
                "OBJ-01": {
                    "budget_usd": 1.0, "currency": "usd",
                    "warn_fraction": 0.5, "notes": [{"ts": 1, "who": "user",
                                                     "what": "extend +$1"}],
                    "spent_usd": 0.9,
                },
            }
        }
        rows = [_row(cause="created", tid="t_1", objective="OBJ-01"),
                _row(cause="completed", tid="t_1", objective="OBJ-01")]
        doc = ob.rollup(rows, now=1000.0)
        doc = ob.preserve_human_decisions(doc, prev)
        o = doc["objectives"]["OBJ-01"]
        self.assertEqual(o["budget_usd"], 1.0)        # sticky
        self.assertEqual(o["currency"], "usd")         # sticky
        self.assertEqual(o["warn_fraction"], 0.5)      # sticky
        self.assertEqual(o["spent_usd"], 0.0)          # recomputed (meter)
        self.assertEqual(len(o["notes"]), 1)           # notes survive
        # spent 0.0 < warn threshold, 1 task done -> ladder says done
        self.assertEqual(o["status"], "done")

    def test_new_objective_keeps_defaults(self):
        prev = {"objectives": {"OBJ-OLD": {"budget_usd": 2.0, "notes": []}}}
        doc = ob.rollup([_row(objective="OBJ-NEW")], now=1000.0)
        doc = ob.preserve_human_decisions(doc, prev)
        self.assertIsNone(doc["objectives"]["OBJ-NEW"]["budget_usd"])


class TestMain(Base):
    def _write_trace(self, rows):
        tp = ob.trace_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        with open(tp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_end_to_end_and_idempotence(self):
        self._write_trace([
            _row(cause="created", tid="t_1", objective="OBJ-01"),
            _row(source="usage-audit", provider=None, cost=0.25),
        ])
        rc = ob.main(["--window-days", "3650"])
        self.assertEqual(rc, 0)
        first = self.budgets_file().read_text(encoding="utf-8")
        # second run with identical trace: silent (watchdog) + byte-identical
        # modulo timestamps -> strip them for the comparison
        old_stdout = sys.stdout
        try:
            import io
            buf = io.StringIO()
            sys.stdout = buf
            rc2 = ob.main(["--window-days", "3650"])
            out2 = buf.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc2, 0)
        self.assertEqual(out2, "")  # idempotent -> silence
        second = json.loads(self.budgets_file().read_text(encoding="utf-8"))
        self.assertEqual(second["_meta"]["mode"], "observer")

    def test_missing_trace_still_writes(self):
        rc = ob.main(["--window-days", "1"])
        self.assertEqual(rc, 0)
        doc = json.loads(self.budgets_file().read_text(encoding="utf-8"))
        self.assertEqual(doc["objectives"], {})


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCRIPT).read_text(encoding="utf-8")
        banned = ["/home/iinstances", "/data/git", "/root/", "C:\\"]
        for b in banned:
            self.assertNotIn(b, src, f"absolute path leaked: {b}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
