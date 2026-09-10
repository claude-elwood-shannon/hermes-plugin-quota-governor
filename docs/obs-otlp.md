# OTLP Bridge — OBJ-27 F4

The F0 trace (`quota-governor/obs/trace.jsonl`) is the house's canonical
consumption ledger. F4 adds the **standard output**: an optional exporter
that reads the trace and ships spans + metrics to any OTLP/HTTP endpoint
(Jaeger, SigNoz, Grafana Cloud, Langfuse, ...) using the OTLP/HTTP JSON
encoding — pure stdlib, no protobuf, no dependencies.

## The golden rule: interface, not product

We adopt **interfaces** (OTLP / OTel semantic conventions), not products.
This module installs nothing, requires nothing, runs nothing by default.
It only **opens the door**:

- If `OBS_OTLP_ENDPOINT` is unset → **silent no-op** (zero deps, zero
  services by default). The house that wants Jaeger sets the var and it
  just works; the house that does not notices no difference.
- The JSONL trace is the **source of truth**. OTLP is **output, not
  storage**. An exporter failure never degrades the local JSONL — the
  exporter only *reads* the trace and writes its own cursor.

## Usage

```bash
# incremental: export only new lines since the last run
OBS_OTLP_ENDPOINT=http://localhost:4318 \
  HERMES_HOME=~/.hermes/profiles/pr-ollama \
  python3 scripts/obs/otlp_exporter.py

# backfill the full current history (idempotent), then advance the cursor
OBS_OTLP_ENDPOINT=http://localhost:4318 \
  HERMES_HOME=~/.hermes/profiles/pr-ollama \
  python3 scripts/obs/otlp_exporter.py --export-once

# --endpoint overrides the env var for one-off runs
python3 scripts/obs/otlp_exporter.py --endpoint http://localhost:4318
```

The exporter POSTs two OTLP/HTTP JSON payloads per run:
`/v1/traces` (spans) and `/v1/metrics` (cost gauge). Full backends
(OpenTelemetry Collector, SigNoz, Grafana) require both to succeed for the
cursor to advance. A **tracing-only** backend (Jaeger) has no
`/v1/metrics`: its deterministic `404` is treated as "no metrics backend
here" — the metrics leg is skipped (reported as `skipped-404`, without
retries) and the cursor advances on traces success. A `404` on
`/v1/traces`, or any non-404 failure on either leg, still fails the run
(`ok:false`) and leaves the trace + cursor untouched, so the next run
re-exports (idempotent via stable span IDs).

## Semantic-convention mapping (the namespace rule)

House fields map to the house's **own** namespace (`house.*`) and **never
invade** standard namespaces (`gen_ai.*`, `service.*`, `http.*`):

| Trace field      | OTLP attribute        |
|------------------|-----------------------|
| `consumer_class` | `house.consumer_class`|
| `consumer_id`    | `house.consumer_id`   |
| `objective`      | `house.objective`     |
| `costUsd`        | `house.cost_usd`      |
| `provider`       | `house.provider`      |
| `source`         | `house.source`        |
| `cause`          | `house.cause`         |

The `gen_ai.*` attributes (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.request.id`) are taken **verbatim** from the trace line's canonical
`otel` field — the exporter never re-derives them and never lets a
`house.*` attribute overwrite a `gen_ai.*` one. Zero collision with
upstream semconv evolution.

One span is emitted per trace line (a consumption event), named
`<consumer_class>.<cause>`, with a stable `spanId`/`traceId` derived from
`consumer_id` + timestamp. One gauge metric
(`house.consumption.cost_usd`, unit USD) is emitted per line.

## Cursor (own, byte-offset, idempotent)

The exporter keeps its **own** cursor (`obs/otlp-cursor.json`) as a byte
offset into the append-only trace. On each run it exports only the lines
after the offset and, on success, advances the offset to the current file
size. A byte offset is exact for an append-only file (handles equal
timestamps correctly).

If F3 rotation shrinks the active file below the stored offset, the cursor
resets to 0 and the current file is re-exported — safe, because OTLP is
output and span IDs are stable, so collectors dedupe re-sent spans.

`--export-once` ignores the cursor and exports the full current history,
then advances the cursor to the end. Idempotent: stable span IDs mean
re-running it never duplicates at the collector.

## Pointing it at a real backend

The exporter speaks OTLP/HTTP JSON, which every major backend accepts.
Set `OBS_OTLP_ENDPOINT` to the base URL of the collector:

| Backend        | Endpoint example                          | Notes |
|----------------|-------------------------------------------|-------|
| **Jaeger**     | `http://localhost:4318`                   | Tracing-only: `/v1/metrics` 404 is tolerated (`skipped-404`), cursor advances on traces success; view at `http://localhost:16686` |
| **SigNoz**     | `http://localhost:4318`                   | Self-hosted OTLP collector; or the cloud ingest URL |
| **Grafana Cloud** | `https://otlp-gateway-<region>.grafana.net` | Add `X-OTLP-...` auth headers via a gateway/proxy |
| **Langfuse**   | `https://cloud.langfuse.com/api/public/otel` | OTLP ingest endpoint; add `Authorization: Bearer <pk-lf-...>` |
| **OpenTelemetry Collector** | `http://<collector>:4318` | The standard OTLP/HTTP receiver; both endpoints expected |

> **Auth headers.** The exporter sends only `Content-Type: application/json`
> (the OTLP/HTTP JSON contract). Backends that require auth (Grafana Cloud,
> Langfuse) expect extra headers — put a small reverse proxy in front of the
> exporter that injects them, or extend `_post()` in the module. The exporter
> stays dependency-free and auth-agnostic by design.

## Failure semantics

- **Never blocks the writer.** The exporter only *reads* the trace; the
  JSONL writer (F0) is never touched. A slow or down endpoint delays only
  the exporter's own run.
- **Never degrades the JSONL.** The trace is the source of truth; OTLP is
  output. A failed export leaves the trace and cursor byte-identical.
- **Retries with backoff.** Each leg is retried up to 3 times with
  exponential backoff (`0.5s * 2^attempt`). A `404` is deterministic and
  is never retried: on `/v1/metrics` it means "tracing-only backend"
  (metrics skipped, cursor advances on traces success); on `/v1/traces`
  it is a hard failure. Every other failure on either leg fails the run.

## Tests

`scripts/obs/test_otlp_exporter.py` — 14 tests, fixtures only, no external
network, `/usr/bin/python3.12`: silent no-op when the endpoint is unset,
house.*/gen_ai.* namespace mapping with zero collision, payload shape and
stable span IDs, batch export against a local HTTP fixture server,
incremental cursor (only new lines), `--export-once` backfill idempotency,
failure keeps trace + cursor untouched, rotation resets a stale cursor,
privacy (no absolute host paths in the module), and the tracing-only
backend contract (metrics 404 → `ok:true` + cursor advances + no retry on
the 404; traces 404 and non-404 metrics failures stay hard failures).
