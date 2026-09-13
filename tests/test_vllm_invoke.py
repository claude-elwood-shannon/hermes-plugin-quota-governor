#!/usr/bin/env python3.12
"""test_vllm_invoke.py — contract test-suite for vllm-invoke.py (t_971bb19e).

Freezes the contract of ~/.hermes/scripts/vllm-invoke.py:
  exit codes  0 ok / 1 unreachable / 2 model-not-served / 3 invalid-response / 4 other
  audit log   one JSON line per call at $VLLM_INVOKE_LOG (never breaks the tool)
  flags       --prompt --prompt-file --model --max-tokens --temperature
              --timeout --json --task-id --hint
  env         VLLM_INVOKE_URL / VLLM_INVOKE_MODEL / VLLM_INVOKE_TIMEOUT_S / VLLM_INVOKE_LOG

Hermetic: every HTTP interaction runs against a local ThreadingHTTPServer
(127.0.0.1, ephemeral port). No LAN/GPU needed. Regression case for the
original E2E run of task t_62668ae6 included.

Run:
  <venv-with-pytest>/bin/python -m pytest test_vllm_invoke.py -v
(e.g. the venv created by the t_971bb19e hardening run: uv venv + uv pip install pytest)
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# The suite lives either next to the script (~/.hermes/scripts/) or in a
# repo checkout's tests/ directory (scripts/ is a package there, pytest
# cannot import from it). Resolve the script accordingly.
_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "vllm-invoke.py",                     # sibling (live deploy)
    _HERE.parent / "scripts" / "vllm-invoke.py",  # repo: tests/ + scripts/
]
try:
    SCRIPT = next(p for p in _CANDIDATES if p.exists())
except StopIteration:
    raise SystemExit(
        "vllm-invoke.py not found next to test_vllm_invoke.py "
        "or in ../scripts/")
SPEC = importlib.util.spec_from_file_location("vllm_invoke", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

MODEL = "liodon-ai/deepseek-coder-6.7b-instruct-FP8"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def make_body(content="hola", model=MODEL, pt=10, ct=5, usage=True):
    body = {"choices": [{"message": {"content": content}}], "model": model}
    if usage:
        body["usage"] = {"prompt_tokens": pt, "completion_tokens": ct}
    return json.dumps(body)


class FakeVLLM(BaseHTTPRequestHandler):
    """Queue-driven fake: each do_POST pops (status, body) and records the payload."""

    responses: list = []
    requests: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        FakeVLLM.requests.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            "payload": json.loads(raw.decode("utf-8")),
        })
        status, body = FakeVLLM.responses.pop(0)
        if body == "SLEEP":
            time.sleep(2)
            body = "{}"
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_vllm(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("VLLM_INVOKE_URL", f"{base}/v1/chat/completions")
    FakeVLLM.responses = []
    FakeVLLM.requests = []
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture()
def log_file(tmp_path, monkeypatch):
    log = tmp_path / "vllm-invoke.jsonl"
    monkeypatch.setenv("VLLM_INVOKE_LOG", str(log))
    return log


def entries(log_file):
    if not log_file.exists():
        return []
    return [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]


def run(argv):
    """Run main() in-process, capturing stdout/stderr. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = MOD.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- exit 0 path

def test_success_basic(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body("etiqueta1\netiqueta2"))]
    code, out, err = run(["--prompt", "clasifica", "--max-tokens", "64"])
    assert code == 0, err
    assert out == "etiqueta1\netiqueta2\n"
    (e,) = entries(log_file)
    assert e["exit_code"] == 0
    assert e["prompt_tokens"] == 10 and e["completion_tokens"] == 5
    assert e["model"] == MODEL          # served model recorded
    assert e["latency_ms"] >= 0
    assert TS_RE.match(e["ts"])
    assert e["hint_followed"] is False
    assert e["task_id"] == ""
    assert e["endpoint"] == fake_vllm   # /v1/chat/completions stripped


def test_payload_shape(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body())]
    run(["--prompt", "p", "--model", MODEL, "--max-tokens", "77",
         "--temperature", "0.3"])
    (req,) = FakeVLLM.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["content_type"] == "application/json"
    p = req["payload"]
    assert p["messages"] == [{"role": "user", "content": "p"}]
    assert p["max_tokens"] == 77 and p["temperature"] == 0.3
    assert p["stream"] is False
    assert p["model"] == MODEL


def test_served_model_used_when_no_model_arg(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body(model=MODEL))]
    run(["--prompt", "p"])
    assert entries(log_file)[0]["model"] == MODEL


def test_model_arg_not_overridden_by_served(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body(model="otro-servido"))]
    run(["--prompt", "p", "--model", MODEL])
    assert entries(log_file)[0]["model"] == MODEL


def test_usage_missing_tokens_none(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body(usage=False))]
    code, _, err = run(["--prompt", "p"])
    assert code == 0, err
    e = entries(log_file)[0]
    assert e["prompt_tokens"] is None and e["completion_tokens"] is None


def test_hint_and_task_id_recorded(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body("OK"))]
    run(["--prompt", "p", "--hint", "--task-id", "t_62668ae6"])
    e = entries(log_file)[0]
    assert e["hint_followed"] is True
    assert e["task_id"] == "t_62668ae6"


def test_stdin_prompt(fake_vllm, log_file, monkeypatch):
    FakeVLLM.responses = [(200, make_body("ok"))]
    fake_stdin = io.StringIO("prompt via stdin")
    fake_stdin.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", fake_stdin)
    code, out, _ = run([])
    assert code == 0 and out == "ok\n"
    assert FakeVLLM.requests[0]["payload"]["messages"][0]["content"] == \
        "prompt via stdin"


def test_prompt_file_ok(fake_vllm, log_file, tmp_path):
    pf = tmp_path / "prompt.txt"
    pf.write_text("prompt de fichero", encoding="utf-8")
    FakeVLLM.responses = [(200, make_body("ok"))]
    code, _, err = run(["--prompt-file", str(pf)])
    assert code == 0, err
    assert FakeVLLM.requests[0]["payload"]["messages"][0]["content"] == \
        "prompt de fichero"


def test_log_append_preserves_previous(fake_vllm, log_file):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    prev = {"ts": "2026-09-13T19:31:04Z", "exit_code": 0, "old": True}
    log_file.write_text(json.dumps(prev) + "\n")
    FakeVLLM.responses = [(200, make_body("ok"))]
    run(["--prompt", "p"])
    es = entries(log_file)
    assert len(es) == 2 and es[0] == prev


def test_log_failure_silent(fake_vllm, tmp_path, monkeypatch):
    # log path nested under a FILE -> mkdir fails; observability must not break
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("VLLM_INVOKE_LOG", str(blocker / "sub" / "log.jsonl"))
    FakeVLLM.responses = [(200, make_body("ok"))]
    code, out, err = run(["--prompt", "p"])
    assert code == 0 and out == "ok\n"


# ---------------------------------------------------------------- --json path

def test_json_ok(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body('{"labels": ["OK", "CLIENT_ERROR"]}'))]
    code, out, err = run(["--prompt", "p", "--json"])
    assert code == 0, err
    assert json.loads(out) == {"labels": ["OK", "CLIENT_ERROR"]}
    assert FakeVLLM.requests[0]["payload"]["response_format"] == \
        {"type": "json_object"}


def test_json_broken_content_exit3(fake_vllm, log_file):
    FakeVLLM.responses = [(200, make_body("esto no es json"))]
    code, out, err = run(["--prompt", "p", "--json"])
    assert code == 3 and "invalid response" in err
    assert entries(log_file)[0]["exit_code"] == 3


# ---------------------------------------------------------------- failure paths

def test_connection_refused_exit1(fake_vllm, log_file):
    # dead port: bind, note port, close -> connections refused there
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    s.close()
    monkey_env_url = f"{dead}/v1/chat/completions"
    FakeVLLM.responses = []  # untouched: request must never arrive
    import os
    old = os.environ.get("VLLM_INVOKE_URL")
    os.environ["VLLM_INVOKE_URL"] = monkey_env_url
    try:
        code, _, err = run(["--prompt", "p"])
    finally:
        if old is None:
            os.environ.pop("VLLM_INVOKE_URL", None)
        else:
            os.environ["VLLM_INVOKE_URL"] = old
    assert code == 1 and "unreachable" in err.lower()
    (e,) = entries(log_file)
    assert e["exit_code"] == 1 and e["latency_ms"] is None


def test_timeout_exit1(fake_vllm, log_file):
    FakeVLLM.responses = [(200, "SLEEP")]
    code, _, err = run(["--prompt", "p", "--timeout", "0.2"])
    assert code == 1 and "unreachable" in err.lower()
    assert entries(log_file)[0]["exit_code"] == 1


def test_http500_exit4(fake_vllm, log_file):
    FakeVLLM.responses = [(500, '{"error": "boom"}')]
    code, _, err = run(["--prompt", "p"])
    assert code == 4 and "HTTP error 500" in err
    assert entries(log_file)[0]["exit_code"] == 4


def test_404_model_detail_exit2(fake_vllm, log_file):
    FakeVLLM.responses = [(404, '{"error": "model no-existe-xyz not found"}')]
    code, _, err = run(["--prompt", "p", "--model", "no-existe-xyz"])
    assert code == 2 and "model not served" in err
    assert entries(log_file)[0]["exit_code"] == 2


def test_404_plain_exit2(fake_vllm, log_file):
    FakeVLLM.responses = [(404, '{"detail": "Not Found"}')]
    code, _, err = run(["--prompt", "p"])
    assert code == 2
    assert entries(log_file)[0]["exit_code"] == 2


def test_200_invalid_body_exit3(fake_vllm, log_file):
    FakeVLLM.responses = [(200, '{"no_choices": true}')]
    code, _, err = run(["--prompt", "p"])
    assert code == 3 and "invalid response" in err
    assert entries(log_file)[0]["exit_code"] == 3


def test_empty_prompt_tty_exit4_no_network_no_log(fake_vllm, log_file,
                                                  monkeypatch):
    # TTY stdin (isatty True): no stdin read attempted, straight to exit 4
    fake_stdin = io.StringIO("")
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", fake_stdin)
    code, _, err = run(["--prompt", ""])
    assert code == 4 and "empty prompt" in err
    assert FakeVLLM.requests == [] and entries(log_file) == []


def test_stdin_read_raises_exit4(fake_vllm, log_file, monkeypatch):
    # unreadable stdin (e.g. capture) must fall through to exit 4, no traceback
    class BrokenStdin:
        def isatty(self):
            return False

        def read(self):
            raise OSError("stdin unreadable")

    monkeypatch.setattr("sys.stdin", BrokenStdin())
    code, _, err = run([])
    assert code == 4 and "empty prompt" in err
    assert FakeVLLM.requests == [] and entries(log_file) == []


def test_whitespace_prompt_exit4(fake_vllm, log_file):
    code, _, _ = run(["--prompt", "   \n\t "])
    assert code == 4


def test_prompt_file_unreadable_exit4(fake_vllm, log_file, tmp_path):
    code, _, err = run(["--prompt-file", str(tmp_path / "no-existe.txt")])
    assert code == 4 and "prompt-file unreadable" in err
    assert FakeVLLM.requests == []


# ---------------------------------------------------------------- regression

def test_regression_t62668ae6(fake_vllm, log_file):
    """The original E2E: classify 3 probe lines, hint flagged, task tagged."""
    lines = "GET /health 200 12ms\nPOST /api 500 40ms\nGET /x 404 8ms"
    prompt = (f"Clasifica estas lineas: {lines}\n"
              "Criterio: 2xx/3xx=OK, 4xx=CLIENT_ERROR, 5xx=SERVER_ERROR. "
              "Una etiqueta por linea.")
    FakeVLLM.responses = [(200, make_body("OK\nSERVER_ERROR\nCLIENT_ERROR"))]
    code, out, err = run([
        "--prompt", prompt, "--task-id", "t_62668ae6", "--hint"])
    assert code == 0, err
    assert out.splitlines() == ["OK", "SERVER_ERROR", "CLIENT_ERROR"]
    (e,) = entries(log_file)
    assert e["task_id"] == "t_62668ae6" and e["hint_followed"] is True
    assert e["exit_code"] == 0


# ------------------------------------------------- hardening t_ef2376bd

def test_prompt_file_non_utf8_exit4(fake_vllm, log_file, tmp_path):
    """Non-UTF-8 prompt-file bytes must give exit 4, never a traceback
    outside the 0-4 contract (UnicodeDecodeError is a ValueError)."""
    pf = tmp_path / "bin.txt"
    pf.write_bytes(b"\xff\xfe\x00bin")
    code, _, err = run(["--prompt-file", str(pf)])
    assert code == 4 and "prompt-file unreadable" in err
    assert FakeVLLM.requests == []
    es = entries(log_file)
    assert len(es) == 1 and es[0]["exit_code"] == 4


def test_stdin_none_with_prompt_ok(fake_vllm, log_file, monkeypatch):
    """Cron/daemons can run with sys.stdin=None: --prompt path must not
    touch stdin at all (no AttributeError)."""
    monkeypatch.setattr("sys.stdin", None)
    FakeVLLM.responses = [(200, make_body("ok"))]
    code, out, err = run(["--prompt", "p"])
    assert code == 0 and out == "ok\n", err


def test_stdin_none_without_prompt_exit4(fake_vllm, log_file, monkeypatch):
    """sys.stdin=None and no prompt: falls through to empty-prompt exit 4."""
    monkeypatch.setattr("sys.stdin", None)
    code, _, err = run([])
    assert code == 4 and "empty prompt" in err
    assert FakeVLLM.requests == []


def test_argparse_usage_error_exit2_documented():
    """argparse usage errors SystemExit(2) — same code as model-not-served.
    Frozen as documented behavior: script against this tool with an explicit
    prompt source and exit 2 always means 'model not served'."""
    with pytest.raises(SystemExit) as ei:
        run(["--max-tokens"])  # flag without value
    assert ei.value.code == 2
