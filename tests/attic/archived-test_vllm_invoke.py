#!/usr/bin/python3.12
"""Tests for vllm-invoke.py — offline: HTTP served by a local fake server.
Covers directive tests 1-5 and 9 (6/7/8 are worker-behaviour, exercised by
the E2E hint task, not unit-testable here)."""
from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_path = _HERE / "scripts" / "vllm-invoke.py"
spec = importlib.util.spec_from_file_location("vllm_invoke", _path)
vi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vi)


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.last_payload = body  # type: ignore[attr-defined]
        mode = self.server.mode          # type: ignore[attr-defined]
        content = self.server.content    # type: ignore[attr-defined]
        if mode == "ok":
            out = json.dumps({
                "model": "Qwen2.5-7B-Instruct-FP8",
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
            code = 200
        elif mode == "404":
            out = json.dumps({"error": "model not found"})
            code = 404
        elif mode == "500":
            out = "boom"
            code = 500
        else:  # broken body (HTTP 200 but not JSON)
            out = "this is not json"
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out.encode())


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "vllm-invoke.jsonl"
        os.environ["VLLM_INVOKE_LOG"] = str(self.log)
        self._prev_url = os.environ.get("VLLM_INVOKE_URL")
        self.srv: http.server.ThreadingHTTPServer = \
            http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.srv.mode = "ok"           # type: ignore[attr-defined]
        self.srv.content = "ok: respuesta"  # type: ignore[attr-defined]
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.url = ("http://127.0.0.1:"
                    f"{self.srv.server_address[1]}/v1/chat/completions")
        os.environ["VLLM_INVOKE_URL"] = self.url

    def tearDown(self):
        os.environ.pop("VLLM_INVOKE_LOG", None)
        if self._prev_url is None:
            os.environ.pop("VLLM_INVOKE_URL", None)
        else:
            os.environ["VLLM_INVOKE_URL"] = self._prev_url

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = vi.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def set(self, mode, content="ok: respuesta"):
        self.srv.mode = mode    # type: ignore[attr-defined]
        self.srv.content = content  # type: ignore[attr-defined]


class TestVllmInvoke(Harness):

    def test_1_active_returns_content_exit0(self):
        self.set("ok", "clasificacion: A")
        rc, out, err = self.run_main(
            ["--prompt", "clasifica", "--task-id", "t_test0001"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "clasificacion: A")
        rec = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(rec["exit_code"], 0)
        self.assertEqual(rec["task_id"], "t_test0001")
        self.assertFalse(rec["hint_followed"])
        self.assertEqual(rec["prompt_tokens"], 10)
        self.assertIsNotNone(rec["latency_ms"])

    def test_2_unreachable_exit1(self):
        os.environ["VLLM_INVOKE_URL"] = \
            "http://127.0.0.1:1/v1/chat/completions"  # port 1 = refused
        rc, out, err = self.run_main(["--prompt", "x", "--timeout", "2"])
        self.assertEqual(rc, 1)
        self.assertIn("vLLM unreachable", err)
        rec = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(rec["exit_code"], 1)

    def test_3_model_not_served_exit2(self):
        self.set("404")
        rc, out, err = self.run_main(["--prompt", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("model not served", err)

    def test_4_json_valid(self):
        self.set("ok", '{"clase": "A", "n": 3}')
        rc, out, err = self.run_main(["--prompt", "x", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"clase": "A", "n": 3})

    def test_5_json_broken_exit3(self):
        self.set("ok", "esto no es json {")
        rc, out, err = self.run_main(["--prompt", "x", "--json"])
        self.assertEqual(rc, 3)
        self.assertIn("invalid response", err)

    def test_9_hint_followed_logged(self):
        self.set("ok", "resumen: 3 lineas")
        rc, out, err = self.run_main(
            ["--prompt", "resume", "--hint", "--task-id", "t_test0002"])
        self.assertEqual(rc, 0)
        rec = json.loads(self.log.read_text().splitlines()[-1])
        self.assertTrue(rec["hint_followed"])

    def test_payload_json_format_when_flag(self):
        self.set("ok", "{}")
        self.run_main(["--prompt", "x", "--json"])
        self.assertEqual(self.srv.last_payload.get("response_format"),  # type: ignore
                         {"type": "json_object"})

    def test_http_500_is_exit4(self):
        self.set("500")
        rc, out, err = self.run_main(["--prompt", "x"])
        self.assertEqual(rc, 4)

    def test_empty_prompt_exit4(self):
        rc, out, err = self.run_main(["--prompt", "   "])
        self.assertEqual(rc, 4)


if __name__ == "__main__":
    sys.exit(unittest.main())
