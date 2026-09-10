#!/usr/bin/python3.12
"""test_trace.py — OBJ-27 F0: canonical trace schema + collectors + doctor.

Covers (fixtures only, no network):
  1. append_trace writes a valid JSONL line under HERMES_HOME/obs/trace.jsonl
  2. parse_objective extracts 'objective:OBJ-xx' from a task body
  3. shadow_cost computes the catalog price; unknown model -> 0
  4. collect_nanogpt_requests: real costUsd + requestId, objective=unattributed
  5. collect_usage_audit: tokens + shadow costUsd, consumer_class=cron-llm
  6. collect_task_events: objective JOINED from the task body (the join key)
  7. run_collectors appends from all three sources
  8. doctor: writable + parseable + count by class
  9. privacy: no absolute host paths in the repo module (portability)

Run:  /usr/bin/python3.12 test_trace.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

GOV_DIR = "REPO"
TRACE_SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "trace.py")

_spec = importlib.util.spec_from_file_location("obs_trace", TRACE_SCRIPT)
trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trace)


def _write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trace-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def read_trace(self):
        path = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


class TestWriter(Base):
    def test_append_writes_valid_jsonl(self):
        ok = trace.append_trace({"ts_epoch_utc": 1.0, "consumer_class": "worker",
                                 "consumer_id": "t_x", "cause": "claimed",
                                 "model": None, "provider": None,
                                 "tokens_in": None, "tokens_out": None,
                                 "costUsd": None, "requestId": None,
                                 "objective": "OBJ-27", "source": "task-events",
                                 "otel": {}}, hermes_home=self.home())
        self.assertTrue(ok)
        rows = self.read_trace()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["objective"], "OBJ-27")
        self.assertEqual(rows[0]["consumer_class"], "worker")

    def test_append_creates_obs_dir(self):
        trace.append_trace({"ts_epoch_utc": 1.0, "consumer_class": "probe",
                            "consumer_id": "p", "cause": "x", "model": None,
                            "provider": None, "tokens_in": None,
                            "tokens_out": None, "costUsd": None,
                            "requestId": None, "objective": "unattributed",
                            "source": "t", "otel": {}}, hermes_home=self.home())
        self.assertTrue((Path(self.tmp) / "quota-governor" / "obs").is_dir())


class TestObjective(Base):
    def test_parses_tag(self):
        body = "objective:OBJ-27 | cost:tiny\n\nsome body"
        self.assertEqual(trace.parse_objective(body), "OBJ-27")

    def test_missing_is_unattributed(self):
        self.assertEqual(trace.parse_objective("no tag here"), "unattributed")
        self.assertEqual(trace.parse_objective(""), "unattributed")
        self.assertEqual(trace.parse_objective(None), "unattributed")

    def test_tag_after_other_header_lines(self):
        body = "cost:tiny\nobjective:OBJ-28\nmore"
        self.assertEqual(trace.parse_objective(body), "OBJ-28")

    def test_comma_metadata_on_same_line(self):
        body = "objective:OBJ-19, auto_created:true, cost:small, model:fast"
        self.assertEqual(trace.parse_objective(body), "OBJ-19")


class TestShadowPrice(Base):
    def test_known_model(self):
        cost = trace.shadow_cost("glm-5.3-flash", 1_000_000, 100_000)
        self.assertAlmostEqual(cost, 0.1571 + 0.5236 * 0.1, places=6)

    def test_unknown_model_zero(self):
        self.assertEqual(trace.shadow_cost("nope-model", 1000, 1000), 0.0)

    def test_custom_prices_override(self):
        prices = {"m": (1.0, 2.0, 0.0)}
        self.assertAlmostEqual(trace.shadow_cost("m", 1_000_000, 0, prices), 1.0)


class TestNanogptCollector(Base):
    def test_emits_real_cost_and_request_id(self):
        _write_jsonl(Path(self.tmp) / "quota-governor" / "nanogpt-requests.jsonl",
                     [{"ts": "2026-09-09T03:16:08Z", "source": "request",
                       "model": "qwen3.5-4b", "provider": "custom",
                       "requestId": "req_abc", "costUsd": 1.425e-06,
                       "paymentSource": "USD", "inputTokens": 13,
                       "outputTokens": 1}])
        rows = trace.collect_nanogpt_requests(hermes_home=self.home())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["consumer_class"], "worker")
        self.assertEqual(r["consumer_id"], "req_abc")
        self.assertEqual(r["requestId"], "req_abc")
        self.assertAlmostEqual(r["costUsd"], 1.425e-06, places=9)
        self.assertEqual(r["objective"], "unattributed")  # no task_id -> gap shows
        self.assertEqual(r["source"], "nanogpt-requests")
        self.assertEqual(r["otel"]["gen_ai.request.model"], "qwen3.5-4b")
        self.assertEqual(r["otel"]["gen_ai.usage.input_tokens"], 13)

    def test_malformed_rows_skipped(self):
        path = Path(self.tmp) / "quota-governor" / "nanogpt-requests.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("not json\n")
        self.assertEqual(trace.collect_nanogpt_requests(hermes_home=self.home()), [])

    def test_missing_file_empty(self):
        self.assertEqual(trace.collect_nanogpt_requests(hermes_home=self.home()), [])


class TestUsageAuditCollector(Base):
    def test_emits_tokens_and_shadow_cost(self):
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl",
                     [{"ts": "2026-05-01T04:23:11.123Z", "job_id": "j1",
                       "fire_id": "deadbeef", "prompt_tokens": 11894,
                       "completion_tokens": 287, "total_tokens": 12181,
                       "response_silent": False, "deliver_target": None,
                       "model": "glm-5.3-flash", "duration_ms": 4231,
                       "error": None}])
        rows = trace.collect_usage_audit(hermes_home=self.home())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["consumer_class"], "cron-llm")
        self.assertEqual(r["consumer_id"], "deadbeef")
        self.assertEqual(r["tokens_in"], 11894)
        self.assertEqual(r["tokens_out"], 287)
        # shadow price: 11894/1e6*0.1571 + 287/1e6*0.5236
        self.assertAlmostEqual(r["costUsd"],
                               11894/1e6*0.1571 + 287/1e6*0.5236, places=6)
        self.assertEqual(r["objective"], "unattributed")
        self.assertEqual(r["source"], "usage-audit")

    def test_missing_tokens_are_none(self):
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl",
                     [{"ts": "2026-05-01T04:23:11.123Z", "job_id": "j",
                       "fire_id": "f", "prompt_tokens": None,
                       "completion_tokens": None, "model": None}])
        rows = trace.collect_usage_audit(hermes_home=self.home())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["tokens_in"])
        self.assertEqual(rows[0]["costUsd"], 0.0)


class TestTaskEventsCollector(Base):
    def _make_db(self):
        db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, body TEXT)")
        con.execute("CREATE TABLE task_events "
                    "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
                    "created_at REAL)")
        con.execute("INSERT INTO tasks VALUES ('t_1', 'objective:OBJ-27 | cost:tiny')")
        con.execute("INSERT INTO tasks VALUES ('t_2', 'no objective here')")
        con.execute("INSERT INTO task_events (task_id, kind, created_at) "
                    "VALUES ('t_1', 'claimed', 1788998186)")
        con.execute("INSERT INTO task_events (task_id, kind, created_at) "
                    "VALUES ('t_1', 'completed', 1788998200)")
        con.execute("INSERT INTO task_events (task_id, kind, created_at) "
                    "VALUES ('t_2', 'claimed', 1788998300)")
        con.commit()
        con.close()
        return db

    def test_joins_objective_from_task_body(self):
        db = self._make_db()
        rows = trace.collect_task_events(hermes_home=self.home(), kanban_db=db)
        self.assertEqual(len(rows), 3)
        # t_1 has two events (claimed + completed); t_2 one (claimed)
        t1 = [r for r in rows if r["consumer_id"] == "t_1"]
        t2 = [r for r in rows if r["consumer_id"] == "t_2"]
        self.assertEqual(len(t1), 2)
        self.assertEqual(len(t2), 1)
        self.assertEqual({r["cause"] for r in t1}, {"claimed", "completed"})
        self.assertEqual(t1[0]["objective"], "OBJ-27")
        self.assertEqual(t2[0]["objective"], "unattributed")
        self.assertEqual(t1[0]["consumer_class"], "worker")
        self.assertEqual(t1[0]["source"], "task-events")

    def test_missing_db_empty(self):
        self.assertEqual(trace.collect_task_events(hermes_home=self.home()),
                         [])


class TestRunCollectors(Base):
    def test_appends_from_all_three_sources(self):
        _write_jsonl(Path(self.tmp) / "quota-governor" / "nanogpt-requests.jsonl",
                     [{"ts": "2026-09-09T03:16:08Z", "model": "qwen3.5-4b",
                       "provider": "custom", "requestId": "req_1",
                       "costUsd": 1.4e-06, "inputTokens": 13, "outputTokens": 1}])
        # usage-audit
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl",
                     [{"ts": "2026-05-01T04:23:11.123Z", "job_id": "j",
                       "fire_id": "f1", "prompt_tokens": 100,
                       "completion_tokens": 10, "model": "glm-5.3-flash"}])
        # kanban db
        db = Path(self.tmp) / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, body TEXT)")
        con.execute("CREATE TABLE task_events "
                    "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
                    "created_at REAL)")
        con.execute("INSERT INTO tasks VALUES ('t_1', 'objective:OBJ-27')")
        con.execute("INSERT INTO task_events (task_id, kind, created_at) "
                    "VALUES ('t_1', 'claimed', 1788998186)")
        con.commit()
        con.close()

        counts = trace.run_collectors(hermes_home=self.home(), kanban_db=db)
        self.assertEqual(counts["nanogpt-requests"], 1)
        self.assertEqual(counts["usage-audit"], 1)
        self.assertEqual(counts["task-events"], 1)
        rows = self.read_trace()
        self.assertEqual(len(rows), 3)
        sources = {r["source"] for r in rows}
        self.assertEqual(sources, {"nanogpt-requests", "usage-audit",
                                   "task-events"})
        # the task-events line carries the objective join
        te = [r for r in rows if r["source"] == "task-events"][0]
        self.assertEqual(te["objective"], "OBJ-27")

    def test_incremental_cursor_skips_already_traced(self):
        """Re-running run_collectors appends only NEW lines (cursor)."""
        # first run: one usage-audit line
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl",
                     [{"ts": "2026-05-01T04:23:11.123Z", "job_id": "j",
                       "fire_id": "f1", "prompt_tokens": 100,
                       "completion_tokens": 10, "model": "glm-5.3-flash"}])
        c1 = trace.run_collectors(hermes_home=self.home())
        self.assertEqual(c1["usage-audit"], 1)
        self.assertEqual(len(self.read_trace()), 1)

        # second run with no new data: nothing appended
        c2 = trace.run_collectors(hermes_home=self.home())
        self.assertEqual(c2["usage-audit"], 0)
        self.assertEqual(len(self.read_trace()), 1)

        # a NEW line (later ts) is appended
        _write_jsonl(Path(self.tmp) / "cron" / "usage_audit.jsonl",
                     [{"ts": "2026-05-02T04:23:11.123Z", "job_id": "j",
                       "fire_id": "f2", "prompt_tokens": 200,
                       "completion_tokens": 20, "model": "glm-5.3-flash"}])
        c3 = trace.run_collectors(hermes_home=self.home())
        self.assertEqual(c3["usage-audit"], 1)
        self.assertEqual(len(self.read_trace()), 2)


class TestDoctor(Base):
    def test_doctor_reports_writable_and_by_class(self):
        trace.append_trace({"ts_epoch_utc": 1.0, "consumer_class": "worker",
                            "consumer_id": "t", "cause": "claimed",
                            "model": None, "provider": None, "tokens_in": None,
                            "tokens_out": None, "costUsd": None,
                            "requestId": None, "objective": "OBJ-27",
                            "source": "task-events", "otel": {}},
                           hermes_home=self.home())
        trace.append_trace({"ts_epoch_utc": 2.0, "consumer_class": "cron-llm",
                            "consumer_id": "f", "cause": "cron-fire",
                            "model": "glm-5.3-flash", "provider": None,
                            "tokens_in": 10, "tokens_out": 1, "costUsd": 0.0,
                            "requestId": None, "objective": "unattributed",
                            "source": "usage-audit", "otel": {}},
                           hermes_home=self.home())
        d = trace.doctor(hermes_home=self.home())
        self.assertTrue(d["ok"])
        self.assertTrue(d["writable"])
        self.assertTrue(d["parseable"])
        self.assertEqual(d["lines"], 2)
        self.assertEqual(d["by_class"]["worker"], 1)
        self.assertEqual(d["by_class"]["cron-llm"], 1)

    def test_doctor_ok_on_empty_trace(self):
        d = trace.doctor(hermes_home=self.home())
        self.assertTrue(d["ok"])
        self.assertTrue(d["writable"])
        self.assertEqual(d["lines"], 0)


class TestPortability(unittest.TestCase):
    def test_no_absolute_host_paths_in_module(self):
        """Portability: the repo module must not hardcode host paths."""
        src = Path(TRACE_SCRIPT).read_text(encoding="utf-8")
        for needle in ("/home/", "/data/git", "host"):
            self.assertNotIn(needle, src,
                             f"host path leaked into trace.py: {needle}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
