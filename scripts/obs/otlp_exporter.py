#!/usr/bin/python3.12
"""otlp_exporter.py — OBJ-27 F4: optional OTLP bridge (interface, not product).

WHAT IT IS
----------
The F0 trace (quota-governor/obs/trace.jsonl) is the house's canonical
consumption ledger. This module is the STANDARD OUTPUT: it reads the trace
and exports spans + metrics to any OTLP/HTTP endpoint (Jaeger, SigNoz,
Grafana Cloud, Langfuse, ...) using the OTLP/HTTP JSON encoding — pure
stdlib, no protobuf, no dependencies.

THE GOLDEN RULE (OBJ-27 design): we adopt INTERFACES (OTLP / OTel
semconv), not PRODUCTS. This module installs nothing, requires nothing,
runs nothing by default. It only OPENS THE DOOR:

  - If OBS_OTLP_ENDPOINT is unset -> silent no-op (zero deps, zero
    services by default). The house that wants Jaeger sets the var and
    it just works; the house that does not notices no difference.
  - The JSONL trace is the SOURCE OF TRUTH. OTLP is OUTPUT, not storage.
    An exporter failure NEVER degrades the local JSONL (the exporter only
    READS the trace and writes its own cursor).

SEMANTIC-CONVENTION MAPPING (the namespace rule)
------------------------------------------------
House fields map to the house's OWN namespace (house.*) and NEVER invade
standard namespaces (gen_ai.*, service.*, http.*):

  consumer_class -> house.consumer_class
  consumer_id    -> house.consumer_id
  objective      -> house.objective
  costUsd        -> house.cost_usd
  provider       -> house.provider
  source         -> house.source
  cause          -> house.cause

The gen_ai.* attributes (gen_ai.request.model, gen_ai.usage.input_tokens,
gen_ai.usage.output_tokens, gen_ai.request.id) are taken verbatim from the
trace line's canonical 'otel' field — the exporter never re-derives them
and never lets a house.* attribute overwrite a gen_ai.* one. Zero
collision with upstream semconv evolution.

CURSOR (own, byte-offset, idempotent)
-------------------------------------
The exporter keeps its OWN cursor (obs/otlp-cursor.json) as a byte offset
into the append-only trace. On each run it exports only the lines after
the offset and, on success, advances the offset to the current file size.
A byte offset is exact for an append-only file (handles equal timestamps
correctly). If the F3 rotation shrinks the active file below the stored
offset, the cursor resets to 0 and the current file is re-exported — safe,
because OTLP is output and span IDs are stable (derived from consumer_id),
so collectors dedupe re-sent spans.

--export-once (backfill) ignores the cursor and exports the full current
history, then advances the cursor to the end. Idempotent: stable span IDs
mean re-running it never duplicates at the collector.

FAIL-OPEN: every public helper never raises into a caller; a network
failure returns ok=False and leaves the trace and cursor untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "quota-governor"
SCOPE_NAME = "quota-governor.obs"

# House fields -> house.* attribute keys. The house namespace NEVER invades
# standard namespaces (gen_ai.*, service.*, http.*).
HOUSE_ATTRS = {
    "consumer_class": "house.consumer_class",
    "consumer_id": "house.consumer_id",
    "objective": "house.objective",
    "costUsd": "house.cost_usd",
    "provider": "house.provider",
    "source": "house.source",
    "cause": "house.cause",
}

_ENV_ENDPOINT = "OBS_OTLP_ENDPOINT"
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 0.5  # seconds; backoff = base * 2**attempt


def get_hermes_home() -> Path:
    """Active HERMES_HOME, else ~/.hermes. Never absolute in the repo."""
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def obs_dir(hermes_home=None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor" / "obs"


def trace_path(hermes_home=None) -> Path:
    return obs_dir(hermes_home) / "trace.jsonl"


def cursor_path(hermes_home=None) -> Path:
    return obs_dir(hermes_home) / "otlp-cursor.json"


def _load_cursor(hermes_home=None) -> dict:
    try:
        with open(cursor_path(hermes_home), encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cursor(cursor: dict, hermes_home=None) -> None:
    try:
        path = cursor_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cursor, fh, indent=2, sort_keys=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# OTLP payload builders (pure, testable without network)
# ---------------------------------------------------------------------------

def _attr(key: str, value):
    """OTLP AnyValue: bool / int / double / string. Never raises."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _hex(s: str, nbytes: int) -> str:
    """Stable hex id from a string (idempotent re-exports)."""
    return hashlib.sha256(s.encode("utf-8")).digest()[:nbytes].hex()


def _span_attrs(row: dict) -> list:
    """gen_ai.* (verbatim from the canonical 'otel' field) + house.*.

    The namespace rule: gen_ai.* is taken as-is and house.* is added for
    the house's own fields. A house.* key can never collide with a
    gen_ai.* key because the namespaces are disjoint by construction.
    """
    attrs = []
    for k, v in (row.get("otel") or {}).items():
        if v is not None:
            attrs.append(_attr(k, v))
    for src_key, semconv in HOUSE_ATTRS.items():
        v = row.get(src_key)
        if v is not None:
            attrs.append(_attr(semconv, v))
    return attrs


def _ts_ns(row: dict) -> str:
    ts = row.get("ts_epoch_utc")
    if ts is None:
        return "0"
    try:
        return str(int(float(ts) * 1e9))
    except (TypeError, ValueError):
        return "0"


def _span(row: dict) -> dict:
    """One OTLP span per trace line (a consumption event)."""
    cid = str(row.get("consumer_id") or "")
    ts = row.get("ts_epoch_utc")
    name = "{}.{}".format(row.get("consumer_class") or "unknown",
                          row.get("cause") or "event")
    ts_ns = _ts_ns(row)
    return {
        "traceId": _hex(cid, 16),
        "spanId": _hex(cid + "|" + str(ts), 8),
        "name": name,
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": ts_ns,
        "endTimeUnixNano": ts_ns,
        "attributes": _span_attrs(row),
        "status": {"code": 0},  # STATUS_CODE_UNSET
    }


def _metric(row: dict) -> dict:
    """One cost gauge per trace line (house.consumption.cost_usd)."""
    cost = row.get("costUsd")
    attrs = [a for a in _span_attrs(row) if a["key"] != "house.cost_usd"]
    return {
        "name": "house.consumption.cost_usd",
        "unit": "USD",
        "gauge": {
            "dataPoints": [{
                "timeUnixNano": _ts_ns(row),
                "asDouble": float(cost) if cost is not None else 0.0,
                "attributes": attrs,
            }],
        },
    }


def _resource() -> dict:
    return {
        "resource": {
            "attributes": [
                {"key": "service.name",
                 "value": {"stringValue": SERVICE_NAME}},
            ],
        },
        "scopeSpans": [{
            "scope": {"name": SCOPE_NAME},
            "spans": [],
        }],
    }


def build_payload(rows: list) -> dict:
    """Full OTLP/HTTP JSON payload: resourceSpans + resourceMetrics."""
    spans = [_span(r) for r in rows]
    metrics = [_metric(r) for r in rows]
    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": SERVICE_NAME}},
                ],
            },
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME},
                "spans": spans,
            }],
        }],
        "resourceMetrics": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": SERVICE_NAME}},
                ],
            },
            "scopeMetrics": [{
                "scope": {"name": SCOPE_NAME},
                "metrics": metrics,
            }],
        }],
    }


# ---------------------------------------------------------------------------
# Transport (batch + retries with backoff; never raises)
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _post_payload(payload: dict, endpoint: str,
                  max_retries: int = _DEFAULT_MAX_RETRIES,
                  base_delay: float = _DEFAULT_BASE_DELAY) -> bool:
    """POST to /v1/traces and /v1/metrics with exponential backoff.

    Returns True only when BOTH endpoints succeed. On any failure the
    cursor is NOT advanced, so the next run re-exports (idempotent via
    stable span IDs). Never raises.
    """
    base = endpoint.rstrip("/")
    for path in ("/v1/traces", "/v1/metrics"):
        ok = False
        for attempt in range(max_retries + 1):
            try:
                _post(base + path, payload)
                ok = True
                break
            except Exception:
                if attempt < max_retries:
                    time.sleep(base_delay * (2 ** attempt))
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_export(hermes_home=None, endpoint=None, export_once=False,
               max_retries=None, base_delay=None) -> dict:
    """Export new trace lines to the OTLP endpoint. Never raises.

    - endpoint: OBS_OTLP_ENDPOINT env, or the --endpoint override. Unset
      -> silent no-op (the golden rule: nothing runs by default).
    - export_once: ignore the cursor and backfill the full current history
      (idempotent via stable span IDs), then advance the cursor to the end.
    - The trace is only READ; the exporter writes only its own cursor.
      A network failure returns ok=False and leaves trace + cursor intact.
    """
    endpoint = (endpoint or os.environ.get(_ENV_ENDPOINT, "")).strip()
    if not endpoint:
        return {"ok": True, "noop": True,
                "reason": "OBS_OTLP_ENDPOINT unset (silent no-op)"}

    path = trace_path(hermes_home)
    if not path.exists():
        return {"ok": True, "noop": True, "reason": "no trace yet"}

    try:
        size = path.stat().st_size
    except OSError:
        return {"ok": False, "noop": True, "reason": "trace unreadable"}

    cursor = _load_cursor(hermes_home)
    offset = 0 if export_once else int(cursor.get("offset", 0))
    if offset > size:
        # F3 rotation shrank the active file below the stored offset:
        # reset and re-export the current file (OTLP is output, JSONL is truth).
        offset = 0

    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return {"ok": False, "noop": True, "reason": "trace unreadable"}

    lines = [l for l in data.decode("utf-8", "replace").splitlines() if l.strip()]
    if not lines:
        return {"ok": True, "noop": True, "reason": "nothing new",
                "offset": offset}

    rows = []
    for l in lines:
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    if not rows:
        return {"ok": True, "noop": True, "reason": "no parseable rows",
                "offset": offset}

    payload = build_payload(rows)
    ok = _post_payload(payload, endpoint,
                       max_retries=max_retries if max_retries is not None
                       else _DEFAULT_MAX_RETRIES,
                       base_delay=base_delay if base_delay is not None
                       else _DEFAULT_BASE_DELAY)
    if ok:
        _save_cursor({"offset": size}, hermes_home)
    return {
        "ok": ok,
        "noop": False,
        "exported": len(rows),
        "offset": size,
        "endpoint": endpoint,
        "export_once": bool(export_once),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="OBJ-27 F4: export the trace to an OTLP/HTTP endpoint "
                    "(interface, not product). Silent no-op unless "
                    "OBS_OTLP_ENDPOINT is set.")
    p.add_argument("--endpoint", default=None,
                   help="OTLP/HTTP base URL (overrides OBS_OTLP_ENDPOINT), "
                        "e.g. http://localhost:4318")
    p.add_argument("--export-once", action="store_true",
                   help="backfill the full current history (idempotent), "
                        "then advance the cursor to the end")
    args = p.parse_args(argv)
    rep = run_export(endpoint=args.endpoint, export_once=args.export_once)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
