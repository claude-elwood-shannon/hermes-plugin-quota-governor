#!/usr/bin/python3.12
"""test_trace_retention.py — OBJ-27 F3: retention/rotation of the trace.

Covers (fixtures only, no network):
  1. no-op when every row is inside the retention window (file untouched)
  2. window eviction: old rows moved to a gzip archive, recent rows kept
  3. max-lines cap: oldest rows archived when the cap is exceeded
  4. corrupt (non-JSON) lines are preserved verbatim in the active file
  5. rows without ts are never window-evicted (kept in the active file)
  6. archive GC: oldest archives deleted beyond keep_archives
  7. idempotency: a second run right after is a no-op
  8. rotation never touches the incremental cursor file (F0 idempotency)
  9. collect -> rotate -> collect does not duplicate or lose lines
 10. doctor reports size / age / archives / needs_rotation
 11. missing trace -> ok, no-op
 12. dry-run plans the eviction without writing anything
 13. privacy: no absolute host paths in the repo module

Run:  /usr/bin/python3.12 test_trace_retention.py  (or pytest)
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

GOV_DIR = os.environ.get("GOV_DIR", "REPO")
TRACE_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "trace.py")

_spec = importlib.util.spec_from_file_location("obs_trace_f3", TRACE_SCRIPT)
trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trace)

DAY = 86400.0


def _row(ts, i=0, objective="OBJ-27", source="task-events",
         consumer_class="worker"):
    return {
        "ts_epoch_utc": ts,
        "consumer_class": consumer_class,
        "consumer_id": f"t_{i}",
        "cause": "claimed",
        "model": None,
        "provider": None,
        "tokens_in": None,
        "tokens_out": None,
        "costUsd": None,
        "requestId": None,
        "objective": objective,
        "source": source,
        "otel": {},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trace-retention-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        for var in ("QUOTA_GOVERNOR_TRACE_KEEP_DAYS",
                    "QUOTA_GOVERNOR_TRACE_MAX_LINES",
                    "QUOTA_GOVERNOR_TRACE_KEEP_ARCHIVES"):
            self.addCleanup(os.environ.pop, var, None)
            os.environ.pop(var, None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def trace_file(self) -> Path:
        return Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"

    def archive_dir(self) -> Path:
        return Path(self.tmp) / "quota-governor" / "obs" / "archive"

    def cursor_file(self) -> Path:
        return Path(self.tmp) / "quota-governor" / "obs" / "trace-cursor.json"

    def write_trace(self, rows, extra_bad_line=False):
        p = self.trace_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            if extra_bad_line:
                fh.write("not json at all\n")

    def read_trace(self):
        p = self.trace_file()
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass  # corrupt line: counted via raw text assertions
        return rows

    def read_archives(self):
        out = []
        d = self.archive_dir()
        if not d.exists():
            return out
        for f in sorted(d.glob("trace-*.jsonl.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                out.append((f.name,
                            [json.loads(l) for l in fh if l.strip()]))
        return out


class TestWindowEviction(Base):
    def test_noop_when_all_rows_inside_window(self):
        now = time.time()
        rows = [_row(now - 100, 1), _row(now - 50, 2)]
        self.write_trace(rows)
        before = self.trace_file().read_bytes()
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["rotated"])
        self.assertEqual(rep["reason"], "none")
        self.assertEqual(rep["kept"], 2)
        self.assertEqual(rep["archived"], 0)
        # file untouched (byte-identical -> truly no-op)
        self.assertEqual(self.trace_file().read_bytes(), before)

    def test_old_rows_archived_recent_kept(self):
        now = time.time()
        rows = [_row(now - 20 * DAY, 1),   # older than 14d -> archive
                _row(now - 15 * DAY, 2),   # older than 14d -> archive
                _row(now - 2 * DAY, 3),    # keep
                _row(now - 3600, 4)]       # keep
        self.write_trace(rows)
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["rotated"])
        self.assertEqual(rep["reason"], "window")
        self.assertEqual(rep["archived"], 2)
        self.assertEqual(rep["kept"], 2)
        kept_ids = {r["consumer_id"] for r in self.read_trace()}
        self.assertEqual(kept_ids, {"t_3", "t_4"})
        archives = self.read_archives()
        self.assertEqual(len(archives), 1)
        name, arows = archives[0]
        self.assertTrue(name.startswith("trace-") and name.endswith(
            ".jsonl.gz"), name)
        self.assertEqual({r["consumer_id"] for r in arows}, {"t_1", "t_2"})
        self.assertTrue(rep["archive_path"])

    def test_nothing_is_lost_archived_plus_kept_equals_total(self):
        now = time.time()
        rows = [_row(now - (i + 1) * DAY, i) for i in range(20)]
        self.write_trace(rows)
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=7, max_lines=100000,
                                      now=now)
        total = rep["archived"] + rep["kept"]
        self.assertEqual(total, 20)
        kept_ids = {r["consumer_id"] for r in self.read_trace()}
        arch_ids = set()
        for _n, arows in self.read_archives():
            arch_ids |= {r["consumer_id"] for r in arows}
        self.assertEqual(kept_ids | arch_ids,
                         {f"t_{i}" for i in range(20)})
        self.assertFalse(kept_ids & arch_ids)  # no duplicates


class TestMaxLinesCap(Base):
    def test_cap_archives_oldest(self):
        now = time.time()
        rows = [_row(now - 3600, i) for i in range(10)]  # all recent
        self.write_trace(rows)
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=4,
                                      now=now)
        self.assertTrue(rep["rotated"])
        self.assertEqual(rep["reason"], "lines")
        self.assertEqual(rep["kept"], 4)
        self.assertEqual(rep["archived"], 6)
        # the KEPT rows are the newest ones (highest consumer_id here)
        kept_ids = sorted(r["consumer_id"] for r in self.read_trace())
        self.assertEqual(kept_ids,
                         ["t_6", "t_7", "t_8", "t_9"])
        _n, arows = self.read_archives()[0]
        self.assertEqual({r["consumer_id"] for r in arows},
                         {f"t_{i}" for i in range(6)})


class TestUnsafeRows(Base):
    def test_corrupt_line_preserved_in_active(self):
        now = time.time()
        rows = [_row(now - 20 * DAY, 1), _row(now - 3600, 2)]
        self.write_trace(rows, extra_bad_line=True)
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        self.assertTrue(rep["rotated"])
        raw = self.trace_file().read_text(encoding="utf-8")
        self.assertIn("not json at all", raw)  # never silently dropped
        # read_trace() skips corrupt lines; the recent row survived
        self.assertEqual(len(self.read_trace()), 1)
        self.assertEqual(self.read_trace()[0]["consumer_id"], "t_2")
        self.assertEqual(rep["archived"], 1)

    def test_rows_without_ts_kept(self):
        now = time.time()
        r_old = _row(now - 20 * DAY, 1)
        r_nots = _row(None, 2)
        r_new = _row(now - 3600, 3)
        self.write_trace([r_nots, r_old, r_new])
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        kept_ids = {r["consumer_id"] for r in self.read_trace()}
        self.assertEqual(kept_ids, {"t_2", "t_3"})
        self.assertEqual(rep["archived"], 1)


class TestArchiveGC(Base):
    def test_old_archives_deleted_beyond_keep_archives(self):
        now = time.time()
        d = self.archive_dir()
        d.mkdir(parents=True, exist_ok=True)
        # 4 pre-existing archives; keep_archives=2 -> oldest 2 deleted
        for day in (1, 2, 3, 4):
            ts = now - day * DAY
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))
            with gzip.open(d / f"trace-{stamp}.jsonl.gz", "wt",
                           encoding="utf-8") as fh:
                fh.write(json.dumps(_row(ts, day)) + "\n")
        self.write_trace([_row(now - 3600, 99)])
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      keep_archives=2, now=now)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["dropped_archives"], 2)
        remaining = sorted(f.name for f in d.glob("trace-*.jsonl.gz"))
        self.assertEqual(len(remaining), 2)
        # the newest survive
        self.assertIn(remaining[0].split("-")[1][:4],
                      (remaining[1].split("-")[1][:4],))
        # sorted names: last two are day-1 and day-2 archives
        self.assertTrue(remaining[0] < remaining[1])


class TestIdempotency(Base):
    def test_second_run_is_noop(self):
        now = time.time()
        rows = [_row(now - 20 * DAY, 1), _row(now - 3600, 2)]
        self.write_trace(rows)
        r1 = trace.enforce_retention(hermes_home=self.home(),
                                     keep_days=14, max_lines=100000,
                                     now=now)
        self.assertTrue(r1["rotated"])
        after = self.trace_file().read_bytes()
        r2 = trace.enforce_retention(hermes_home=self.home(),
                                     keep_days=14, max_lines=100000,
                                     now=now)
        self.assertFalse(r2["rotated"])
        self.assertEqual(r2["archived"], 0)
        self.assertEqual(self.trace_file().read_bytes(), after)

    def test_cursor_file_untouched_by_rotation(self):
        now = time.time()
        self.write_trace([_row(now - 20 * DAY, 1), _row(now - 3600, 2)])
        cursor = {"nanogpt-requests": 1.0, "task-events": now - 3600,
                  "usage-audit": 2.5}
        self.cursor_file().parent.mkdir(parents=True, exist_ok=True)
        self.cursor_file().write_text(json.dumps(cursor, indent=2,
                                                 sort_keys=True),
                                      encoding="utf-8")
        before = self.cursor_file().read_bytes()
        trace.enforce_retention(hermes_home=self.home(), keep_days=14,
                                max_lines=100000, now=now)
        self.assertEqual(self.cursor_file().read_bytes(), before)

    def test_collect_rotate_collect_no_duplication(self):
        """F0 idempotency survives rotation: no lines lost or duplicated."""
        now = time.time()
        # source with one OLD row and one NEW row
        src = Path(self.tmp) / "quota-governor" / "nanogpt-requests.jsonl"
        src.parent.mkdir(parents=True, exist_ok=True)
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(now - 20 * DAY))
        new_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": old_ts, "model": "m",
                                 "provider": "p", "requestId": "req_old",
                                 "costUsd": 0.001, "inputTokens": 10,
                                 "outputTokens": 1}) + "\n")
            fh.write(json.dumps({"ts": new_ts, "model": "m",
                                 "provider": "p", "requestId": "req_new",
                                 "costUsd": 0.002, "inputTokens": 20,
                                 "outputTokens": 2}) + "\n")
        c1 = trace.run_collectors(hermes_home=self.home())
        self.assertEqual(c1["nanogpt-requests"], 2)
        self.assertEqual(len(self.read_trace()), 2)

        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        self.assertTrue(rep["rotated"])
        self.assertEqual(rep["archived"], 1)   # req_old archived
        self.assertEqual(rep["kept"], 1)       # req_new stays

        # re-collect: cursor already past both -> NOTHING re-appended
        c2 = trace.run_collectors(hermes_home=self.home())
        self.assertEqual(c2["nanogpt-requests"], 0)
        rows = self.read_trace()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requestId"], "req_new")
        # archived copy intact
        _n, arows = self.read_archives()[0]
        self.assertEqual(arows[0]["requestId"], "req_old")


class TestDoctor(Base):
    def test_doctor_reports_size_age_archives_rotation(self):
        now = time.time()
        self.write_trace([_row(now - 20 * DAY, 1),
                          _row(now - 3600, 2),
                          _row(now - 60, 3)])
        d = trace.doctor(hermes_home=self.home())
        self.assertTrue(d["ok"])
        self.assertGreater(d["trace_bytes"], 0)
        self.assertEqual(d["lines"], 3)
        self.assertAlmostEqual(d["age_days"], 20.0, delta=0.01)
        self.assertAlmostEqual(d["oldest_epoch"], now - 20 * DAY, delta=1)
        self.assertAlmostEqual(d["newest_epoch"], now - 60, delta=1)
        self.assertTrue(d["needs_rotation"])
        self.assertEqual(d["archives"]["count"], 0)
        self.assertEqual(d["retention"]["keep_days"], 14.0)
        self.assertEqual(d["retention"]["max_lines"], 100000)

        # rotate -> needs_rotation flips, archives counted
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      now=now)
        self.assertTrue(rep["ok"])
        d2 = trace.doctor(hermes_home=self.home())
        self.assertFalse(d2["needs_rotation"])
        self.assertEqual(d2["archives"]["count"], 1)
        self.assertGreater(d2["archives"]["bytes"], 0)
        # backwards-compatible keys still present
        for key in ("path", "writable", "parseable", "lines", "by_class"):
            self.assertIn(key, d2)

    def test_doctor_ok_on_missing_trace(self):
        d = trace.doctor(hermes_home=self.home())
        self.assertTrue(d["ok"])
        self.assertEqual(d["trace_bytes"], 0)
        self.assertEqual(d["lines"], 0)
        self.assertFalse(d["needs_rotation"])


class TestEdgeCases(Base):
    def test_missing_trace_noop(self):
        rep = trace.enforce_retention(hermes_home=self.home())
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["rotated"])
        self.assertEqual(rep["archived"], 0)

    def test_empty_trace_noop(self):
        self.write_trace([])
        rep = trace.enforce_retention(hermes_home=self.home())
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["rotated"])

    def test_dry_run_writes_nothing(self):
        now = time.time()
        rows = [_row(now - 20 * DAY, 1), _row(now - 3600, 2)]
        self.write_trace(rows)
        before = self.trace_file().read_bytes()
        rep = trace.enforce_retention(hermes_home=self.home(),
                                      keep_days=14, max_lines=100000,
                                      dry_run=True, now=now)
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["archived"], 1)   # plan reported...
        self.assertEqual(rep["kept"], 1)
        self.assertFalse(self.archive_dir().exists())  # ...nothing written
        self.assertEqual(self.trace_file().read_bytes(), before)

    def test_env_overrides(self):
        now = time.time()
        self.write_trace([_row(now - 20 * DAY, 1), _row(now - 3600, 2)])
        os.environ["QUOTA_GOVERNOR_TRACE_KEEP_DAYS"] = "30"
        rep = trace.enforce_retention(hermes_home=self.home(), now=now)
        self.assertFalse(rep["rotated"])  # 20d row inside 30d window
        self.assertEqual(rep["kept"], 2)
        self.assertEqual(rep["cutoff_epoch"], now - 30 * DAY)


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(TRACE_SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data/git", "host"):
            self.assertNotIn(needle, src,
                             f"host path leaked into trace.py: {needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
