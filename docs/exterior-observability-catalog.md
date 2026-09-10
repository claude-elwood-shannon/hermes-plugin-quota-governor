# Exterior Observability Catalog — OBJ-36

> **Status:** catalog, not adoption. The house rule is ratified: *compatible and
> connectable, not adopt*. This document is the map of the outside world of
> open-source observability — the options with the strongest community and
> support — so that the day someone wants to connect, the decision is already
> made.
>
> **Data verified at time of writing (2026-09-10).** Stars, last push, and
> latest release were fetched live from GitHub, not from memory. Activity
> figures will drift; re-verify before any adoption decision.

## House context (the lens every option is judged through)

- **Single host**, one tenant, Podman available.
- **Portability is a value**: the house prefers a light all-in-one over a farm.
- **OTLP-native first**: the house already speaks OTLP/HTTP JSON (OBJ-27 F4
  bridge, verified against Jaeger in F4a). The natural receiver is anything
  that accepts OTLP.
- **LLM observability is the specific load**: `gen_ai.*` semconv, `costUsd`,
  prompts (OBJ-26/35 territory).
- **The cube is unattributed**: logs are the least-justified category today.

---

## 1. Tracing / Telemetry backends

The natural receiver of the house's OTLP output. F4/F4a already verify against
Jaeger.

### Jaeger — CNCF distributed tracing platform
- **What:** the reference OTLP-native tracing backend. CNCF graduated.
- **License:** Apache-2.0.
- **Activity:** 23,198 stars; last push 2026-09-10; latest release v2.20.0
  (2026-07-20). Very active.
- **OTLP:** native (OTLP/HTTP + gRPC receiver).
- **Deployment:** single binary, `all-in-one` mode — trivially Podman-viable.
- **Community:** CNCF, huge, mature docs.
- **Why YES for the house:** already the verified receiver (F4a PASS). Lightest
  path to "see the trace". Tracing-only (no metrics/logs) — that is its
  simplicity, not a gap for a single host.
- **Why NO:** nothing — it is the default first connection.

### SigNoz — OpenTelemetry-native observability platform
- **What:** full-stack (traces + metrics + logs) OTel-native platform, APM +
  AI-agent observability.
- **License:** MIT Expat core with an `ee/` enterprise directory under a
  separate license (open-core).
- **Activity:** 32,069 stars; last push 2026-09-10; latest release v0.141.1
  (2026-09-09). Very active.
- **OTLP:** native (OTLP collector).
- **Deployment:** self-hosted OTLP collector + backend; heavier than Jaeger —
  a real stack (ClickHouse behind it).
- **Community:** strong, fast-moving, good docs.
- **Why YES:** the "one tool for everything" OTel-native option; the natural
  upgrade if the house ever wants metrics+logs in the same place.
- **Why NO today:** a farm for a single host. Overkill while the house only
  needs traces.

### Grafana Tempo (+ Grafana stack)
- **What:** high-volume, minimal-dependency distributed tracing backend from
  Grafana Labs.
- **License:** AGPL-3.0.
- **Activity:** 5,471 stars; last push 2026-09-10; latest release v3.0.3
  (2026-08-13). Active.
- **OTLP:** native (OTLP receiver).
- **Deployment:** single binary, but pairs with Grafana for visualization —
  the stack is the point.
- **Community:** Grafana ecosystem, strong.
- **Why YES:** if the house already runs Grafana for metrics, Tempo slots in.
- **Why NO:** Tempo alone is a backend without a face; the value is the whole
  Grafana stack, which OBJ-32 already evaluated and rejected as a farm for a
  single tenant.

### OpenObserve — logs, metrics, traces, RUM, LLM observability
- **What:** all-in-one observability platform, single binary, positioned as a
  Datadog/Splunk/Elastic alternative with much lower storage cost.
- **License:** AGPL-3.0.
- **Activity:** 21,713 stars; last push 2026-09-10; latest release
  v1.0.0-rc5 (2026-09-10). Very active (still pre-1.0).
- **OTLP:** native.
- **Deployment:** single binary, all-in-one — very Podman-friendly.
- **Community:** strong, fast-moving, good docs.
- **Why YES:** the best "all-in-one light" candidate — covers traces, metrics,
  logs, and LLM observability in one binary. Directly matches the house's
  portability value.
- **Why NO:** AGPL-3.0 (copyleft — fine for internal use, a consideration if
  anything is ever distributed); still rc (pre-1.0).

### Uptrace — OpenTelemetry APM
- **What:** OTel-native APM: traces, metrics, logs.
- **License:** AGPL-3.0.
- **Activity:** 4,282 stars; last push 2026-08-13; latest release
  v2.1.0-beta.8 (2026-08-13). Active but smaller community.
- **OTLP:** native.
- **Deployment:** single binary, all-in-one.
- **Community:** smaller; docs good.
- **Why YES:** light all-in-one, OTLP-native.
- **Why NO:** smaller community than the alternatives; beta release line; the
  same niche OpenObserve fills with more momentum.

---

## 2. LLM observability

The category specific to the house's load (`gen_ai.*` semconv, `costUsd`,
prompts).

### Langfuse — open-source AI engineering platform
- **What:** LLM evals, observability, metrics, prompt management, playground,
  datasets. Integrates with OpenTelemetry, LangChain, OpenAI SDK, LiteLLM.
- **License:** MIT Expat core with an `ee/` enterprise directory (open-core).
- **Activity:** 34,443 stars; last push 2026-09-10; latest release v4.33.0
  (2026-09-09). Very active.
- **OTLP:** native (OTLP ingest endpoint).
- **Deployment:** self-hosted (Docker/Podman) or cloud; a real app (Postgres +
  app), not a single binary.
- **Community:** very strong, YC W23, fast releases.
- **Why YES:** the de-facto standard for LLM observability; OTLP ingest means
  the house's bridge can point at it directly (already listed in obs-otlp.md).
- **Why NO:** heavier than a single binary (needs Postgres); more than the
  house needs today.

### Phoenix (Arize) — AI observability & evaluation
- **What:** AI observability and evaluation platform from Arize.
- **License:** open source (Elastic-2.0; repo shows NOASSERTION).
- **Activity:** 11,408 stars; last push 2026-09-10; latest release 2.5.0
  (2026-09-09). Very active.
- **OTLP:** native (OTLP receiver).
- **Deployment:** single binary / Python package — light, Podman-friendly.
- **Community:** strong, Arize-backed.
- **Why YES:** light, OTLP-native, strong on evals (not just tracing).
- **Why NO:** Elastic-2.0 license (source-available, not OSI open source) — a
  licensing consideration.

### OpenLLMetry (traceloop) — GenAI observability SDK
- **What:** open-source observability for GenAI/LLM apps, built on
  OpenTelemetry. It is an **SDK/instrumentation**, not a backend.
- **License:** Apache-2.0.
- **Activity:** 7,427 stars; last push 2026-08-10; latest release 0.62.3
  (2026-08-10). Active.
- **OTLP:** native (it *is* OTel — emits OTLP).
- **Deployment:** library, not a service.
- **Community:** good, traceloop-backed.
- **Why YES:** the reference for how to emit `gen_ai.*` semconv correctly —
  worth reading for the house's own bridge.
- **Why NO as a product:** it is instrumentation, not a receiver; the house
  already has its own OTLP bridge and does not need another SDK.

### Helicone — open-source LLM observability
- **What:** LLM observability platform; one line of code to monitor, evaluate,
  experiment.
- **License:** Apache-2.0.
- **Activity:** 6,142 stars; last push 2026-08-31; latest release
  v2025.08.21-1 (2025-08-21) — **release line ~1 year stale** (activity is in
  the main branch, but releases lag).
- **OTLP:** via proxy (intercepts LLM calls), not a native OTLP receiver.
- **Deployment:** self-hosted (Docker) or cloud.
- **Community:** YC W23, decent.
- **Why YES:** Apache-2.0, simple.
- **Why NO:** stale release cadence; proxy-based (not OTLP-native) — the house
  already emits OTLP and does not need a call-interception layer.

---

## 3. Metrics / Dashboards

For the day the house goes multi-host.

### Prometheus + Grafana — the standard
- **What:** Prometheus is the de-facto metrics/TSDB standard; Grafana the
  visualization layer.
- **License:** Prometheus Apache-2.0; Grafana AGPL-3.0.
- **Activity:** Prometheus 66,021 stars, last push 2026-09-10, latest 3.13.3
  (2026-09-07). Grafana 76,675 stars, latest 13.2.1 (2026-09-02). Both very
  active.
- **OTLP:** Prometheus has a native OTLP receiver; Grafana queries it.
- **Deployment:** Prometheus single binary; Grafana a server. Together a
  two-service stack.
- **Community:** the largest in the space.
- **Why YES:** the standard; if the house ever needs real dashboards, this is
  the default.
- **Why NO today:** OBJ-32 already evaluated and rejected the full Grafana
  stack for a single tenant — a farm that contradicts portability. The house
  built a stdlib static dashboard instead.

### VictoriaMetrics — lightweight Prometheus alternative
- **What:** fast, cost-effective time-series DB; drop-in Prometheus
  replacement, also a Grafana datasource.
- **License:** Apache-2.0.
- **Activity:** 17,689 stars; latest release v1.151.0 (2026-08-31). Active.
- **OTLP:** native OTLP metrics format.
- **Deployment:** single binary, very light — the most Podman-friendly metrics
  option.
- **Community:** strong, fast-moving.
- **Why YES:** the light alternative to Prometheus; single binary, OTLP-native,
  Apache-2.0 — best fit for the house's portability value.
- **Why NO:** still needs a dashboard layer (Grafana) to be useful.

### Netdata — self-contained real-time monitoring
- **What:** real-time infrastructure monitoring, per-second metrics, built-in
  ML anomaly detection, auto-generated dashboards.
- **License:** GPL-3.0.
- **Activity:** 80,474 stars; latest release v2.11.0 (2026-08-12). Very active.
- **OTLP:** via exporter (not a native receiver).
- **Deployment:** single agent, self-contained, zero-config — the most
  "self-contained" of the three.
- **Community:** very strong, CNCF member.
- **Why YES:** the most self-contained option — agent + dashboard in one, no
  separate Grafana.
- **Why NO:** GPL-3.0; its value is real-time infra monitoring, which a single
  host barely needs; the house's own dashboard already covers the human face.

---

## 4. Logs / aggregation

The unattributed cube — evaluate whether it is ever worth it.

### Loki — like Prometheus, but for logs
- **What:** horizontally-scalable, multi-tenant log aggregation; indexes labels,
  not log content.
- **License:** AGPL-3.0.
- **Activity:** 28,861 stars; latest release v3.7.7 (2026-08-27). Active.
- **OTLP:** native OTLP receiver.
- **Deployment:** single binary (single-host mode) or a stack (Alloy agent +
  Loki + Grafana).
- **Community:** strong, Grafana-backed.
- **Why YES:** the standard log aggregator; OTLP-native.
- **Why NO:** the house's logs are the unattributed cube — there is no
  multi-host fleet generating logs worth aggregating today. Loki solves a
  problem the house does not have.

### OpenObserve — (dual function, see §1)
- Covers logs too, in the same single binary. If logs ever matter, this is the
  lightest way to get them alongside traces/metrics.

### Quickwit — cloud-native search engine for observability
- **What:** search engine for logs and traces, Elasticsearch-compatible API,
  Jaeger-native.
- **License:** Apache-2.0.
- **Activity:** 11,577 stars; latest release v0.9.0 (2026-07-25). Active.
- **OTLP:** via Jaeger-native / ES-compatible API (not a direct OTLP receiver).
- **Deployment:** cloud-native, decoupled compute/storage — designed for
  object storage (S3 etc.), overkill for a single host.
- **Community:** good, focused.
- **Why YES:** Apache-2.0, fast search.
- **Why NO:** built for cloud storage and scale; the opposite of the house's
  single-host portability value.

---

## 5. LLM cost management (SaaS-open)

What others build on the same idea as OBJ-26/35 — compare approaches, steal
ideas.

### OpenCost — Kubernetes cost monitoring
- **What:** cost monitoring for Kubernetes workloads and cloud costs.
- **License:** Apache-2.0.
- **Activity:** ~6.7k stars; latest release v1.121.1 (2026-08-05). Active.
- **OTLP:** n/a (Kubernetes-native, not OTLP).
- **Deployment:** Kubernetes operator — requires a cluster.
- **Community:** CNCF, strong.
- **Why YES:** the reference for cost attribution; worth reading its model.
- **Why NO:** Kubernetes-only; the house is a single host, not a cluster.

### OpenLIT — observability & evaluation for AI/coding agents
- **What:** open-source observability & evaluation platform for AI agents and
  coding agents; traces LLMs, tools, prompts, **costs**, and agent workflows
  with OpenTelemetry.
- **License:** Apache-2.0.
- **Activity:** ~2.8k stars; latest release 2.1.0 (2026-09-10). Active.
- **OTLP:** native (it is OTel-based).
- **Deployment:** self-hosted, OTel-native.
- **Community:** growing, smaller.
- **Why YES:** the closest external analogue to the house's own cost-trace
  idea (OBJ-26/35) — OTel-native cost tracking for agents. Directly comparable
  to steal ideas from.
- **Why NO:** smaller community; the house already has its own cost ledger and
  OTLP bridge — this is a reference, not a replacement.

---

## Recommendations — the day someone wants to connect

| Category | First option | Why |
|----------|--------------|-----|
| **Tracing/telemetry** | **Jaeger** | Already verified (F4a PASS), lightest single-binary OTLP receiver, Apache-2.0. The default first connection. |
| **LLM observability** | **Langfuse** | De-facto standard, OTLP ingest (bridge points at it directly), strongest community. |
| **Metrics/dashboards** | **VictoriaMetrics** | Lightest OTLP-native metrics DB, Apache-2.0, single binary — best portability fit. (Pair with Grafana only if a real dashboard is ever needed.) |
| **Logs** | **OpenObserve** | The only light all-in-one that covers logs alongside traces/metrics in one binary — if logs ever justify it. Otherwise: skip. |
| **LLM cost** | **OpenLIT** | The closest OTel-native analogue to the house's own cost idea — reference for ideas, not adoption. |

**Bottom line for the house:** the only connection that is *already* justified
is **Jaeger** (verified, light, done). Everything else is a map for the day a
real need appears — and the house's portability value consistently points to
single-binary, OTLP-native, permissive-license options (Jaeger, VictoriaMetrics,
OpenObserve) over the full stacks (SigNoz, Grafana, Loki) that a single tenant
does not need.
