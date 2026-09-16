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
the offset and, on success, advances the cursor to the current file size.
A byte offset is exact for an append-only file (handles equal timestamps
correctly). If the F3 rotation shrinks the active file below the stored
offset, the cursor resets to 0 and the current file is re-exported — safe,
because OTLP is output and span IDs are stable (derived from consumer_id),
so collectors dedupe re-sent spans.

--export-once (backfill) ignores the cursor and exports the full current
history, then advances the cursor to the end. Idempotent: stable span IDs
mean re-running it never duplicates at the collector.

TRACING-ONLY BACKENDS (Jaeger, ...)
-----------------------------------
Some OTLP endpoints are traces-only: they answer `404` for `/v1/metrics`.
That is NOT a failure — it means "no metrics backend here". The exporter
skips the metrics leg immediately (a 404 is deterministic; no retries)
and advances its cursor when `/v1/traces` succeeded, reporting
"metrics": "skipped-404". Full backends (OpenTelemetry Collector,
SigNoz, Grafana) keep the strict both-endpoints semantics.

FAIL-OPEN: every public helper never raises into a caller; a network
failure returns ok=False and leaves the trace and cursor untouched.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
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
_ENV_AUTH = "OBS_OTLP_AUTH"          # inline "user:pass" (Basic auth)
_ENV_AUTH_FILE = "OBS_OTLP_AUTH_FILE"  # chmod-600 file: "user:pass" or
                                        # KEY=VALUE lines (KEY Zo_ROOT_USER_*)
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 0.5  # seconds; backoff = base * 2**attempt

# ---------------------------------------------------------------------------
# Resolve authentication credentials
# ---------------------------------------------------------------------------


def resolve_auth() -> str | None:
    """Basic-auth credential for the OTLP endpoint, or None.

    Sources, in order (first hit wins):
      1. OBS_OTLP_AUTH            — inline "user:pass"
      2. OBS_OTLP_AUTH_FILE       — a chmod-600 file holding either a bare
        "user:pass" line or KEY=VALUE lines with ZO_ROOT_USER_EMAIL /
        ZO_ROOT_USER_PASSWORD (the OpenObserve root-env format).
    Fail-open: any unreadable/malformed source degrades to None (no auth),
    never into the caller — the house never trades availability for auth.
    """
    inline = os.environ.get(_ENV_AUTH, "").strip()
    if inline:
        return inline
    path = os.environ.get(_ENV_AUTH_FILE, "").strip()
    if not path:
        return None
    email = password = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("ZO_ROOT_USER_EMAIL="):
                    email = line.split("=", 1)[1].strip()
                elif line.startswith("ZO_ROOT_USER_PASSWORD="):
                    password = line.split("=", 1)[1].strip()
                elif ":" in line:
                    return line
    except OSError:
        return None
    if email and password:
        return f"{email}:{password}"
    return None


# ---------------------------------------------------------------------------
# Paths to trace and cursor
# ---------------------------------------------------------------------------


def get_hermes_home() -> Path:
    """Active HERMES_HOME, else ~/.hermes. Never absolute in the repo."""
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


def obs_dir(hermes_home: Path | None = None) -> Path:
    base = Path(hermes_home) if hermes_home else get_hermes_home()
    return base / "quota-governor" / "obs"


def trace_path(hermes_home: Path | None = None) -> Path:
    return obs_dir(hermes_home) / "trace.jsonl"


def cursor_path(hermes_home: Path | None = None) -> Path:
    return obs_dir(hermes_home) / "otlp-cursor.json"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _load_cursor(hermes_home: Path | None = None) -> dict:
    try:
        with open(cursor_path(hermes_home), encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cursor(cursor: dict, hermes_home: Path | None = None) -> None:
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
    """OTLP AnyValue for span/dataPoint attributes: bool / int / double /
    string. Never raises.

    FLOAT NORMALIZATION (t_88753960, verified live): the OpenObserve trace
    ingester (image pulled 2026-09-11) rejects AnyValue `doubleValue` maps
    in span attributes ("invalid type: map, expected f64") while accepting
    int/string/bool maps — and accepts `asDouble` on metric dataPoints.
    So float attributes are normalized: integral floats become `intValue`,
    anything else a full-precision `stringValue`. This is valid OTLP/JSON
    on every backend (Jaeger, SigNoz, Collector included); numeric doubles
    for aggregation ride the `house.consumption.cost_usd` metric instead.
    """
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        if value.is_integer():
            return {"key": key, "value": {"intValue": int(value)}}
        return {"key": key, "value": {"stringValue": repr(value)}}
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


def _post(url: str, payload: dict, auth: str | None = None) -> int:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(
            auth.encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


class _Endpoint404(Exception):
    """The endpoint answered 404: the path does not exist there."""


def _post_retry(url: str, payload: dict, max_retries: int,
                base_delay: float, auth: str | None = None) -> bool:
    """POST with exponential backoff. False when retries are exhausted.

    A 401/403 from a Basic-auth-protected backend is treated like a 404:
    deterministic, no retries (wrong or missing credentials will not fix
    themselves between attempts).
    """
    for attempt in range(max_retries + 1):
        try:
            _post(url, payload, auth=auth)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise _Endpoint404(url) from exc
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** attempt))
        except Exception:
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** attempt))
    return False


def _post_payload(payload: dict, endpoint: str, auth: str | None = None,
                  max_retries: int = _DEFAULT_MAX_RETRIES,
                  base_delay: float = _DEFAULT_BASE_DELAY) -> dict:
    """POST to /v1/traces and /v1/metrics with exponential backoff.

    Returns a dict of per-leg outcomes: ``{"traces": bool,
    "metrics": bool | "skipped-404"}``. A 404 on /v1/metrics is the
    tracing-only-backend contract (Jaeger): the metrics leg is skipped
    (no retries — the 404 is deterministic) and the run counts as ok
    when /v1/traces succeeded. A 404 — or 401/403 (auth wall) — on
    /v1/traces is a real failure (a backend that takes no traces, or
    that refuses our credentials, is no backend at all). On any other
    failure the cursor is NOT advanced, so the next run re-exports
    (idempotent via stable span IDs). Never raises.
    """
    base = endpoint.rstrip("/")
    out: dict = {"traces": False, "metrics": False}
    try:
        out["traces"] = _post_retry(base + "/v1/traces", payload,
                                    max_retries, base_delay, auth=auth)
    except _Endpoint404:
        # A backend that answers 404 (or 401/403) for traces cannot take
        # the trace: that is a hard failure, not a degraded one.
        return out
    try:
        leg = _post_retry(base + "/v1/metrics", payload,
                          max_retries, base_delay, auth=auth)
        out["metrics"] = leg
    except _Endpoint404:
        # Tracing-only backend (Jaeger) OR an auth wall on the metrics
        # leg: deterministic no — degrade to a metrics no-op and let the
        # traces success carry the cursor.
        out["metrics"] = "skipped-404"
    return out

# ---------------------------------------------------------------------------
# Helper functions for orchestrator (each < 50 lines)
# ---------------------------------------------------------------------------

def _compute_offset(size: int, cursor_offset: int, export_once: bool) -> int:
    """Return effective read offset.
    If export_once, always return 0. If the stored offset is beyond EOF, reset
    to 0. The function keeps the core arithmetic isolated and trivial.
    """
    effective = 0 if export_once else cursor_offset
    return 0 if effective > size else effective

def _read_trace(path: Path, offset: int) -> tuple[list[dict], int] | None:
    """Read trace lines from *path* after *offset*.
    Returns a list of parsed JSON rows and the file size, or None when the
    file is unreadable (OSError) so the caller can report a hard failure.
    Handles binary undecodable data with ``replace``; skips lines that
    cannot parse.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return None  # unreadable trace (distinct from an empty read)
    rows: list[dict] = []
    for l in data.decode("utf-8", "replace").splitlines():
        if not l.strip():
            continue
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    return rows, size

def _handle_success(ok: bool, rows: list[dict], size: int,
                    hermes_home: Path | None) -> None:
    """Persist the cursor only when the export succeeded (ok=True).
    ``rows`` allows a quick guard against empty payloads. On failure the
    cursor stays untouched so the next run re-exports the same lines.
    """
    if ok and rows:
        _save_cursor({"offset": size}, hermes_home)

def _build_response(
    ok: bool,
    exported: int,
    offset: int,
    endpoint: str,
    auth_present: bool,
    export_once: bool,
    legs: dict,
    size: int,
) -> dict:
    """Construct the public API response dictionary.
    ``size`` is the trace size at send time; used for the ``offset`` key.
    """
    rep = {
        "ok": ok,
        "noop": False,
        "exported": exported,
        "offset": size if ok else offset,
        "endpoint": endpoint,
        "auth": auth_present,
        "export_once": export_once,
        "legs": legs,
    }
    if legs.get("metrics") == "skipped-404":
        rep["metrics"] = "skipped-404 (tracing-only backend: /v1/metrics 404)"
    return rep

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_export(hermes_home=None, endpoint=None, export_once=False,
                max_retries=None, base_delay=None) -> dict:
    """Export new trace lines to the OTLP endpoint. Never raises.
    The heavy lifting is delegated to small helpers.
    """
    endpoint = (endpoint or os.environ.get(_ENV_ENDPOINT, "")).strip()
    if not endpoint:
        return {"ok": True, "noop": True,
                "reason": "OBS_OTLP_ENDPOINT unset (silent no-op)"}

    path = trace_path(hermes_home)
    if not path.exists():
        return {"ok": True, "noop": True, "reason": "no trace yet"}

    cursor = _load_cursor(hermes_home)
    stored = int(cursor.get("offset", 0))
    try:
        size = path.stat().st_size
    except OSError:
        return {"ok": False, "noop": True, "reason": "trace unreadable"}
    offset = _compute_offset(size=size, cursor_offset=stored,
                             export_once=export_once)
    read = _read_trace(path, offset)
    if read is None:
        return {"ok": False, "noop": True, "reason": "trace unreadable"}
    rows, size = read
    if not rows:
        return {"ok": True, "noop": True, "reason": "nothing new",
                "offset": offset}

    payload = build_payload(rows)
    auth = resolve_auth()
    legs = _post_payload(
        payload, endpoint, auth=auth,
        max_retries=max_retries if max_retries is not None else _DEFAULT_MAX_RETRIES,
        base_delay=base_delay if base_delay is not None else _DEFAULT_BASE_DELAY,
    )
    metrics_ok = legs.get("metrics") is not False
    ok = bool(legs.get("traces")) and metrics_ok
    _handle_success(ok, rows, size, hermes_home)
    return _build_response(
        ok=ok,
        exported=len(rows),
        offset=offset,
        endpoint=endpoint,
        auth_present=bool(auth),
        export_once=export_once,
        legs=legs,
        size=size,
    )

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
