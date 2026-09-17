#!/usr/bin/env python3.12
"""vllm-invoke.py — vLLM hybrid-tool directive (2026-09-13).

Workers cloud (pr-ollama/pr-nanogpt/pr-opencode) call this to delegate
schema-bound subtasks to the LOCAL vLLM endpoint ($0.00/token, LAN-only):
classification, extraction, summarization, changelog drafting (night
marathon R1-R6 niche). NEVER for multi-step reasoning, temporal-state
decisions, code writing, or >32K context (see skill vllm-delegate).

Fail-open by design: any failure is a non-zero exit code and a short
stderr message — the worker then does the subtask itself. The system
NEVER depends on the GPU being alive.

Exit codes (directive §2):
  0  ok (stdout: the message content; with --json: the parsed JSON)
  1  "vLLM unreachable" (connection refused / timeout / DNS)
  2  "model not served" (404 from the server)
  3  "invalid response" (HTTP 200 but body unparseable / --json broken)
  4  any other error (empty prompt, unreadable prompt-file, HTTP != 404)

Usage/argument errors from argparse also exit 2; when scripting against
this contract pass the prompt via --prompt/--prompt-file, then argparse
errors are impossible and exit 2 always means "model not served".

Usage:
  vllm-invoke.py --prompt "clasifica estas lineas: ..." [--json]
  vllm-invoke.py --prompt-file /tmp/prompt.txt --max-tokens 2048
  echo "..." | vllm-invoke.py            # stdin as prompt
  vllm-invoke.py --task-id t_abc123      # tag the audit log entry

Every call appends one line to ~/.hermes/logs/vllm-invoke.jsonl:
  {ts, task_id, model, prompt_tokens, completion_tokens, latency_ms,
   exit_code, hint_followed, endpoint}
hint_followed=True when --hint is passed (the caller detected a
[vllm-hint: ...] marker in the task body) — consumed by P4's efficiency
ratio to measure hint adoption.

Config: VLLM_INVOKE_URL (default http://192.168.1.32:8000/v1/chat/completions),
VLLM_INVOKE_MODEL (default: omit -> server-served model), VLLM_INVOKE_TIMEOUT_S
(default 30). stdlib only (urllib); no API keys, no egress beyond the LAN.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://192.168.1.32:8000/v1/chat/completions"


def log_path() -> Path:
    """Audit log path: $VLLM_INVOKE_LOG, else ~/.hermes/logs/vllm-invoke.jsonl."""
    return Path(os.environ.get(
        "VLLM_INVOKE_LOG",
        os.path.expanduser("~/.hermes/logs/vllm-invoke.jsonl")))


def log_call(entry: dict) -> None:
    """Append one JSON line to the audit log; never raises (observability must not break the tool)."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # observability must never break the tool


def fail(code: int, msg: str, entry: dict) -> int:
    """Stamp the entry with `code`, log it, print `msg` to stderr, and return `code`."""
    entry["exit_code"] = code
    log_call(entry)
    print(msg, file=sys.stderr)
    return code


class _PromptError(Exception):
    """Unrecoverable prompt-source failure (exit 4)."""


def _load_prompt(args) -> str:
    """Return the prompt string from args or stdin.

    Raises _PromptError when --prompt-file cannot be read.
    """
    if args.prompt:
        return args.prompt
    if args.prompt_file:
        try:
            return Path(args.prompt_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError (non-UTF-8 prompt file)
            raise _PromptError(f"prompt-file unreadable: {exc}") from exc
    if sys.stdin is not None and not sys.stdin.isatty():
        try:
            return sys.stdin.read()
        except (OSError, ValueError):
            return ""  # unreadable stdin -> fall through to empty-prompt error
    return ""


def _build_payload(prompt, args):
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": False,
    }
    if args.model:
        payload["model"] = args.model
    if args.json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _call_vllm(url, payload, timeout):
    """POST the payload; return (body, err_code, http_status).

    Success: (body, None, None).  Failure: (None, exit_code, status_or_None).
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace"), None, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if "model" in detail.lower() or "not found" in detail.lower():
                return None, 2, None
            return None, 2, None
        return None, 4, exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, 1, None


def _process_response(body, args, entry):
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        entry["prompt_tokens"] = usage.get("prompt_tokens")
        entry["completion_tokens"] = usage.get("completion_tokens")
        served = data.get("model")
        if served and not entry["model"]:
            entry["model"] = served
    except Exception as exc:
        return fail(3, f"invalid response: {exc}", entry)
    if args.json:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            return fail(3, f"invalid response: model JSON broken ({exc})",
                        entry)
        entry["exit_code"] = 0
        log_call(entry)
        print(json.dumps(parsed, ensure_ascii=False))
        return 0
    entry["exit_code"] = 0
    log_call(entry)
    print(content)
    return 0


def main(argv=None) -> int:
    """Run the CLI: load the prompt, call the local vLLM endpoint, and log/print per the exit-code contract."""
    ap = argparse.ArgumentParser(description="Delegate a subtask to local vLLM.")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--model", default=os.environ.get("VLLM_INVOKE_MODEL", ""))
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("VLLM_INVOKE_TIMEOUT_S", "30")))
    ap.add_argument("--json", action="store_true",
                    help="force response_format json_object and validate")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--hint", action="store_true",
                    help="call was motivated by a [vllm-hint: ...] marker")
    args = ap.parse_args(argv)

    try:
        prompt = _load_prompt(args)
    except _PromptError as exc:
        return fail(4, str(exc),
                    {"ts": _now(), "exit_code": 4, "hint_followed": False})
    if not prompt.strip():
        print("empty prompt (use --prompt, --prompt-file or stdin)",
              file=sys.stderr)
        return 4

    url = os.environ.get("VLLM_INVOKE_URL", DEFAULT_URL)
    payload = _build_payload(prompt, args)

    entry = {
        "ts": _now(),
        "task_id": args.task_id,
        "model": args.model or None,
        "prompt_tokens": None, "completion_tokens": None,
        "latency_ms": None,
        "exit_code": None,
        "hint_followed": bool(args.hint),
        "endpoint": url.split("/v1")[0],
    }
    t0 = time.monotonic()
    body, err_code, http_status = _call_vllm(url, payload, args.timeout)
    if body is None:
        if err_code == 1:
            return fail(1, "vLLM unreachable", entry)
        if err_code == 2:
            return fail(2, "model not served", entry)
        return fail(4, f"HTTP error {http_status}", entry)
    entry["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return _process_response(body, args, entry)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    raise SystemExit(main())
