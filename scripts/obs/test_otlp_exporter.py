#!/usr/bin/python3.12
"""test_otlp_exporter.py — OBJ-27 F4: OTLP bridge (interface, not product).

Covers (fixtures only, no external network):
  1. no-op: OBS_OTLP_ENDPOINT unset -> silent no-op, nothing sent
  2. mapping: house.* namespace + gen_ai.* verbatim, no collision
  3. payload shape: resourceSpans + resourceMetrics, stable span IDs
  4. batch export against a local HTTP fixture server (both endpoints)
  5. cursor: incremental — only new lines after the offset are sent
  6. backfill --export-once: full history, idempotent (stable IDs)
  7. failure: server down -> ok=False, trace + cursor untouched
  8. rotation: cursor offset beyond file size resets and re-exports
  9. privacy: no absolute host paths in the repo module (portability)

Run:  /usr/bin/python3.12 test_otlp_exporter.py  (or pytest)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GOV_DIR = "REPO"
SCRIPT = os.path.join(GOV_DIR, "scripts", "obs", "otlp_exporter.py")

_spec = importlib.util.spec_from_file_location("otlp_exporter", SCRIPT)
exp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exp)


def _row(ts, cid="t_x", cls="worker", cause="claimed", costUsd=0.5,
         objective="OBJ-27", model="deepseek-v4-flash", tin=10, tout=5):
    otel = {}
    if model:
        otel["gen_ai.request.model"] = model
    if tin is not None:
        otel["gen_ai.usage.input_tokens"] = tin
    if tout is not None:
        otel["gen_ai.usage.output_tokens"] = tout
    return {"ts_epoch_utc": ts, "consumer_class": cls, "consumer_id": cid,
            "cause": cause, "model": model, "provider": "ollama-cloud",
            "tokens_in": tin, "tokens_out": tout, "costUsd": costUsd,
            "requestId": None, "objective": objective, "source": "task-events",
            "otel": otel}


class FixtureServer:
    """Local OTLP/HTTP fixture: records POSTs to /v1/traces and /v1/metrics."""

    def __init__(self):
        self.received = {"traces": [], "metrics": []}
        self._fail = False

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if self.server.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                if self.path == "/v1/traces":
                    self.server.received["traces"].append(
                        json.loads(body.decode("utf-8")))
                    self.send_response(200)
                elif self.path == "/v1/metrics":
                    self.server.received["metrics"].append(
                        json.loads(body.decode("utf-8")))
                    self.send_response(200)
                else:
                    self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.received = self.received
        self.httpd.fail = self._fail
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    @property
    def fail(self):
        return self._fail

    @fail.setter
    def fail(self, value):
        self._fail = value
        self.httpd.fail = value

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="otlp-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        self.addCleanup(os.environ.pop, "OBS_OTLP_ENDPOINT", None)
        os.environ["HERMES_HOME"] = self.tmp

    def home(self):
        return self.tmp

    def _write_trace(self, rows):
        path = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _cursor(self):
        p = Path(self.tmp) / "quota-governor" / "obs" / "otlp-cursor.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text())


class NoopTest(Base):
    def test_noop_when_endpoint_unset(self):
        self._write_trace([_row(100.0)])
        rep = exp.run_export()
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["noop"])
        self.assertIn("OBS_OTLP_ENDPOINT", rep["reason"])
        # no cursor written, no trace touched
        self.assertEqual(self._cursor(), {})

    def test_noop_when_no_trace(self):
        rep = exp.run_export(endpoint="http://127.0.0.1:1")
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["noop"])


class MappingTest(Base):
    def test_house_namespace_and_genai_verbatim(self):
        row = _row(100.0, cid="t_abc", costUsd=1.25, model="glm-5.3-flash",
                   tin=100, tout=50)
        attrs = {a["key"]: a["value"] for a in exp._span_attrs(row)}
        # house.* namespace
        self.assertEqual(attrs["house.consumer_class"]["stringValue"], "worker")
        self.assertEqual(attrs["house.consumer_id"]["stringValue"], "t_abc")
        self.assertEqual(attrs["house.objective"]["stringValue"], "OBJ-27")
        self.assertEqual(attrs["house.cost_usd"]["doubleValue"], 1.25)
        self.assertEqual(attrs["house.provider"]["stringValue"], "ollama-cloud")
        self.assertEqual(attrs["house.source"]["stringValue"], "task-events")
        self.assertEqual(attrs["house.cause"]["stringValue"], "claimed")
        # gen_ai.* verbatim from the canonical otel field
        self.assertEqual(attrs["gen_ai.request.model"]["stringValue"],
                         "glm-5.3-flash")
        self.assertEqual(attrs["gen_ai.usage.input_tokens"]["intValue"], 100)
        self.assertEqual(attrs["gen_ai.usage.output_tokens"]["intValue"], 50)
        # no standard namespace is ever invaded by a house field
        for k in attrs:
            self.assertFalse(k.startswith("service."))
            self.assertFalse(k.startswith("http."))
            if k.startswith("gen_ai."):
                self.assertIn(k, ("gen_ai.request.model",
                                  "gen_ai.usage.input_tokens",
                                  "gen_ai.usage.output_tokens",
                                  "gen_ai.request.id"))

    def test_payload_shape_and_stable_ids(self):
        rows = [_row(100.0, cid="t_a"), _row(200.0, cid="t_b")]
        payload = exp.build_payload(rows)
        self.assertIn("resourceSpans", payload)
        self.assertIn("resourceMetrics", payload)
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics[0]["name"], "house.consumption.cost_usd")
        # stable span IDs: same input -> same id (idempotent re-export)
        again = exp.build_payload(rows)
        s1 = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        s2 = again["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([s["spanId"] for s in s1],
                         [s["spanId"] for s in s2])
        self.assertEqual([s["traceId"] for s in s1],
                         [s["traceId"] for s in s2])


class ExportTest(Base):
    def test_batch_export_to_fixture_server(self):
        srv = FixtureServer()
        self.addCleanup(srv.stop)
        self._write_trace([_row(100.0, cid="t_a"), _row(200.0, cid="t_b")])
        rep = exp.run_export(endpoint=srv.endpoint)
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["noop"])
        self.assertEqual(rep["exported"], 2)
        self.assertEqual(len(srv.received["traces"]), 1)
        self.assertEqual(len(srv.received["metrics"]), 1)
        spans = srv.received["traces"][0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        # cursor advanced to file size
        self.assertEqual(self._cursor().get("offset"),
                         (Path(self.tmp) / "quota-governor" / "obs"
                          / "trace.jsonl").stat().st_size)

    def test_incremental_cursor_only_new_lines(self):
        srv = FixtureServer()
        self.addCleanup(srv.stop)
        self._write_trace([_row(100.0, cid="t_a")])
        exp.run_export(endpoint=srv.endpoint)
        self.assertEqual(len(srv.received["traces"]), 1)
        # append a second line; re-run sends only the new one
        self._write_trace([_row(200.0, cid="t_b")])
        rep = exp.run_export(endpoint=srv.endpoint)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["exported"], 1)
        self.assertEqual(len(srv.received["traces"]), 2)
        spans = srv.received["traces"][1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 1)
        attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
        self.assertEqual(attrs["house.consumer_id"]["stringValue"], "t_b")

    def test_export_once_backfills_full_history(self):
        srv = FixtureServer()
        self.addCleanup(srv.stop)
        self._write_trace([_row(100.0, cid="t_a"), _row(200.0, cid="t_b")])
        rep = exp.run_export(endpoint=srv.endpoint, export_once=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["exported"], 2)
        spans = srv.received["traces"][0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        # idempotent: re-running export_once sends the same stable spans
        exp.run_export(endpoint=srv.endpoint, export_once=True)
        spans2 = srv.received["traces"][1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([s["spanId"] for s in spans],
                         [s["spanId"] for s in spans2])

    def test_failure_keeps_trace_and_cursor_untouched(self):
        srv = FixtureServer()
        self.addCleanup(srv.stop)
        srv.fail = True
        self._write_trace([_row(100.0, cid="t_a")])
        rep = exp.run_export(endpoint=srv.endpoint, max_retries=0)
        self.assertFalse(rep["ok"])
        self.assertEqual(self._cursor(), {})  # cursor not advanced
        # trace file byte-identical (only read, never written)
        p = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        before = p.read_bytes()
        rep2 = exp.run_export(endpoint=srv.endpoint, max_retries=0)
        self.assertFalse(rep2["ok"])
        self.assertEqual(p.read_bytes(), before)

    def test_rotation_resets_stale_cursor(self):
        srv = FixtureServer()
        self.addCleanup(srv.stop)
        self._write_trace([_row(100.0, cid="t_a")])
        exp.run_export(endpoint=srv.endpoint)
        # simulate F3 rotation shrinking the file below the stored offset
        p = Path(self.tmp) / "quota-governor" / "obs" / "trace.jsonl"
        orig_size = p.stat().st_size
        short = _row(300.0, cid="t_c", model=None, tin=None, tout=None)
        p.write_text(json.dumps(short) + "\n")
        self.assertLess(p.stat().st_size, orig_size)
        rep = exp.run_export(endpoint=srv.endpoint)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["exported"], 1)
        spans = srv.received["traces"][1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 1)


class PrivacyTest(Base):
    def test_no_absolute_host_paths_in_module(self):
        src = Path(SCRIPT).read_text()
        self.assertNotIn("/home/", src)
        self.assertNotIn("/data/", src)
        self.assertNotIn("host", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
