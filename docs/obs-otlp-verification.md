# OTLP Bridge — E2E Verification against a real stack (OBJ-27 F4a)

Verified 10-sep-2026, local, podman 4.9.3 (amd64), ephemeral containers only
(`--rm`, no persistent volumes, nothing installed on the host beyond podman
images). No real user data — the house's own synthetic consumption trace
(`quota-governor/obs/trace.jsonl`, 1419 lines) is the fixture.

## Principle (the user's words, ratified)

> The house's effort ENDS at being compatible and connectable — what each
> person connects is their own business.

This task only verifies the standard (OTLP/OTel semconv). It adopts no tool.

## Stack 1 — Jaeger all-in-one (PASS for traces; metrics gap -> RESOLVED by F4b)

```bash
# pull + run ephemeral (OTLP receiver on 4318, UI on 16686)
podman pull docker.io/jaegertracing/all-in-one:latest
podman run -d --rm --name jaeger-otlp-test \
  -p 16686:16686 -p 4318:4318 \
  -e COLLECTOR_OTLP_ENABLED=true \
  docker.io/jaegertracing/all-in-one:latest

# health
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:16686/   # 200
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:4318/v1/traces -H "Content-Type: application/json" -d '{}'  # 200
```

### Traces path — PASS

The exporter's OTLP/HTTP JSON traces payload is accepted by real Jaeger and
the house attributes are legible through the Jaeger query API:

```
POST /v1/traces -> HTTP 200
Jaeger /api/traces?service=quota-governor returned 5 traces (6 spans)
house.* attributes present: house.cause, house.consumer_class,
  house.consumer_id, house.cost_usd, house.objective, house.source
gen_ai.* attributes present: gen_ai.request.model,
  gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
RESULT: PASS
```

The namespace rule holds in a real backend: house fields live in `house.*`,
`gen_ai.*` is taken verbatim from the canonical `otel` field, and no standard
namespace is invaded.

### Metrics path — GAP -> RESOLVED (F4b, 10-sep-2026)

> **UPDATE (F4b, 10-sep-2026): RESOLVED.** The exporter now tolerates a
> tracing-only backend: a deterministic `404` on `/v1/metrics` is treated
> as "no metrics backend here" — the metrics leg is skipped
> (`skipped-404`, no retries) and the cursor advances on traces success.
> `run_export()` against this same Jaeger stack now reports `ok:true`
> with `"metrics": "skipped-404 (tracing-only backend: /v1/metrics 404)"`.
> The strict both-endpoints semantics are kept for full backends
> (OpenTelemetry Collector, SigNoz, Grafana), and a `404` on
> `/v1/traces` (or any non-404 failure on either leg) is still a hard
> failure. Fix + fixture tests: `scripts/obs/otlp_exporter.py`,
> `TracingOnlyBackendTest` in `scripts/obs/test_otlp_exporter.py`.

Original finding (kept for the record):

Jaeger is a **tracing-only** backend: its OTLP receiver returns
`404 page not found` for `/v1/metrics` (verified directly). The exporter's
`run_export()` requires **both** `/v1/traces` and `/v1/metrics` to succeed
before advancing the cursor, so the wrapper reports `ok:false` against
Jaeger even though the traces path works perfectly.

```
POST /v1/traces  -> 200
POST /v1/metrics -> 404 page not found
run_export()     -> ok:false (exported 1419, cursor NOT advanced)
```

**This is a real compatibility gap in the F4 exporter**, not a Jaeger
problem. A full OpenTelemetry Collector (which forwards metrics to a metrics
backend) would accept both; Jaeger alone does not. The F4 doc lists Jaeger
as a supported backend, so the exporter should tolerate a tracing-only
backend (e.g. treat a 404 on `/v1/metrics` as "no metrics backend" and
advance the cursor on traces success). Tracked as a follow-up.

## Stack 2 — SigNoz / lightweight OTLP receiver

Not run. Jaeger already proves OTLP/OTel semconv conformance (the protocol
conformance is what matters); SigNoz is a large ClickHouse stack that does
not fit the verification budget. Recorded as **not-tested, not a blocker**.

## Exporter checks (checklist item 3)

| Check | Result |
|-------|--------|
| Batch export (1419 lines) | PASS — all spans built, traces accepted |
| `house.*` attributes visible remotely | PASS — via Jaeger `/api/traces` |
| Backfill idempotent (re-run no duplicate) | PASS — stable span IDs, re-run sends same 1419, cursor not advanced |
| Endpoint down → backoff + JSONL intact | PASS — `ok:false`, checksum invariant, cursor not created |
| JSONL byte-identical before/after | PASS — `sha256sum` unchanged (`7cd90717…`) across all runs |

The JSONL trace is the source of truth; OTLP is output. Every failure path
left the trace and cursor untouched.

## How to reproduce the traces verification

```bash
# 1. run Jaeger (above)
# 2. export the traces payload to Jaeger and assert house.* via the API
/usr/bin/python3.12 scripts/obs/otlp_exporter.py --endpoint http://localhost:4318 --export-once
#    -> ok:true, "metrics": "skipped-404 (...)" — F4b tolerates the
#       tracing-only backend; the cursor advances (traces are ingested)
# 3. query Jaeger for the house service
curl -s "http://localhost:16686/api/traces?service=quota-governor&limit=5"
```

## Verdict

- **Criterion met for traces**: Jaeger shows house traces with `house.*`
  attributes legible; JSONL checksum invariant; doc committed.
- **F4b (10-sep-2026): gap closed.** The exporter wrapper now tolerates a
  tracing-only backend (metrics 404): `run_export()` succeeds against
  Jaeger as documented (`ok:true`, metrics leg `skipped-404`, cursor
  advanced). Follow-up t_dd2eb1bb delivered.
