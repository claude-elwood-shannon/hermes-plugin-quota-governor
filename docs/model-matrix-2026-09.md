# Need → model matrix — Sep 2026 (MULTI-PROV-10.4)

**Source tasks**: 10.1 catalog/prices (t_9c61f54e), 10.2 callability probes
(t_514c568e, micro-test t_154b29f2, worker migration t_ffd28494), 10.3 real
cost measured (t_f94f8ef2). Gate state: `scripts/quota-gate.py`
(`PRIVACY_CAPABILITIES`, `PROFILE_MODELS`, `PROFILE_WORKER_MODELS`,
`PRIVACY_PROVIDER_PREFERENCE`, `GENERAL_PROVIDER_PREFERENCE`).
**Status**: DRAFT pending user approval. The implementation (10.5) does not
start without an explicit verdict. No new probes were run: all data comes
from the 2026-09-07/08 measurements already recorded.

---

## 1. Gate rules framing the matrix

- `PRIVACY_CAPABILITIES` (not modified in this phase):
  - `public` → ollama-cloud, nanogpt, openrouter, opencode-go, custom
  - `sensitive` → ollama-cloud, nanogpt, custom (opencode-go EXCLUDED:
    retention/training not audited)
  - `confidential` → custom/local only (no cloud provider applies)
- Worker cost tiers: `micro/tiny/small` use `PROFILE_WORKER_MODELS`;
  `medium+` may use `PROFILE_MODELS` (interactive/quality).
- Routing (OBJ-26, current): preference-first with nanogpt first in general
  (`GENERAL_PROVIDER_PREFERENCE`); under `sensitive`,
  `PRIVACY_PROVIDER_PREFERENCE` rules (nanogpt 0, ollama 1). Inside nanogpt:
  covered-first with a balance budget (5 USD/window,
  `nanogpt_max_balance_spend_usd`).
- OpenRouter has a gate pin (`z-ai/glm-5.2:free`) but was NOT cataloged or
  probed in 10.1–10.3 ⇒ outside the matrix until measured.

## 2. Need × profile matrix

Fields: need | profile | primary model | list price ($/1M in/out) |
real cost measured | probe OK | alternative/fallback.
"Real cost measured" = USD per measured micro-call (10.3 §5.2) and, where it
exists, USD per call/task under real worker load (ledger 10.3 §5.3, known
+16% bias ⇒ upper bound). Sources per row: catalog 10.1
(model-catalog-2026-09.md), probes 10.2 (§4 of the same source),
cost 10.3 (§5).

### 2.1 Cheap worker (`cost:micro/tiny/small`) — current gate pins

| Need | Profile | Primary (gate pin) | Price $/1M | Real cost measured | Probe OK | Alternative / fallback |
|---|---|---|---|---|---|---|
| Cheap worker | pr-opencode (opencode-go) | `qwen3.8-flash` | 0.15 / 0.47 | $0.000051/micro-call; $0.0038–0.0087/call on real kanban tasks (~$0.08–0.20/task) | yes (200, 1801 ms, cost=0, 10.2 §4.1) | `glm-5.3-flash` (0.15/0.50; $0.000027/call; 200 OK) |
| Cheap worker | pr-ollama (ollama-cloud) | `deepseek-v4-flash:0731` | 0.22 / 0.66 off-peak; **peak ×2 12–18 UTC Mon–Fri** (0.44/1.32) | $0.000048/micro-call (measured off-peak) | yes (200, 1058 ms, 10.2 §4.2) | `gpt-oss:20b` (0.07/0.30; $0.000028; 200 OK, no peak); `glm-5.3-flash`@ollama ($0.000031) |
| Cheap worker | pr-nanogpt (nanogpt) | `z-ai/glm-5.3-flash` | 0.075 / 0.25 | $0.000030/micro-call, covered (costUsd=0) | yes (200 + cost=0, 10.2 §4.3; end-to-end on a real worker: t_154b29f2 and migration verified t_ffd28494) | `deepseek/deepseek-v4-flash` (0.14/0.28, cost=0); `qwen/qwen3.5-9b` (0.05/0.15, cost=0) |

Row notes:

- pr-opencode: `qwen3.8-flash` is the plan's model (always on, no toggle,
  $30 included/month). The current pin assigns it to the worker and leaves
  `glm-5.3-flash` interactive — the inverse of the previous 10.4 draft
  (t_bef3cbf0), whose proposal is still pending approval (§6).
- pr-ollama: the gate pins `deepseek-v4-flash` (real ollama-cloud ID:
  `deepseek-v4-flash:0731`). Its ×2 peak lands on European working hours
  (12–18 UTC Mon–Fri): in that window the alternative `gpt-oss:20b` is 3×
  cheaper and peak-free. Cost measured off-peak (10.3 §5.2).
- pr-nanogpt: stable covered worker (costUsd=0 on all measured calls).
  Real window: 60M INPUT tokens/week, exactly metered; ~40–60 Hermes
  tasks/week with large prompts. Coverage verified at three layers
  (gate + profile config + direct chat, t_ffd28494).

### 2.2 Interactive / session (`PROFILE_MODELS` of the gate)

| Need | Profile | Primary (gate pin) | Price $/1M | Real cost measured | Probe OK | Alternative |
|---|---|---|---|---|---|---|
| Interactive/session | pr-opencode | `glm-5.3-flash` | 0.15 / 0.50 | $0.000027/micro-call; ~$0.004/call on a real session | yes (200, 5148 ms, cost=0, 10.2 §4.1) | `qwen3.8-flash` (0.15/0.47; $0.000051; 200 OK) |
| Interactive/session | pr-ollama | `glm-5.2` | 1.40 / 4.40 (ledger-calibrated: 1.18/3.72) | $0.000559/micro-call; **$0.0426/call on real agent sessions** (activity.cost: $4.978/117 req) | yes (200, 1582 ms, 10.2 §4.2) | `glm-5.3` (same rate; $0.000273/call) |
| Interactive/session | pr-nanogpt | `zai-org/glm-5.2` | 0.42 / 1.32 | $0.000019/micro-call; **DYNAMIC coverage**: cost>0 at 03:24 and cost=0 at 03:39 on 2026-09-07 (10.3 §5.1) | yes (200, 10.2 §4.3) | `z-ai/glm-5.3-flash` (stable covered, 4–5× cheaper) |
| Interactive/session | pr-openrouter | `z-ai/glm-5.2:free` | free tier | unmeasured (outside 10.1–10.3) | no probe | — |

Warnings:

- glm-5.2 was banished from interactive on pr-opencode after burning 82% of
  the 5h window alone ($9.84/$12); evidence in the gate itself
  (INTERACTIVE MODEL WARNING comment) and in the epic's motivation.
- `zai-org/glm-5.2` on nanogpt is on the subscription list but charges the
  balance intermittently ("being on the list" ≠ "covered"; the reliable
  discriminator is `x_nanogpt_pricing.costUsd`). Verify before pinning long
  sessions; the previous draft proposes replacing it (§6).

### 2.3 Quality (`cost:medium+`: review, complex code, design)

| Need | Profile | Primary | Price $/1M | Real cost measured | Probe OK | Alternative |
|---|---|---|---|---|---|---|
| Quality medium+ | pr-opencode | `kimi-k2.7-code` | 0.95 / 4.00 | $0.000334/call | yes (200, 10.2 §4.1) | `qwen3.8-max` (2.00/6.00; $0.000584) |
| Quality medium+ | pr-ollama | `kimi-k2.7-code` | 0.95 / 4.00 | $0.000321/call | yes (200, 10.2 §4.2) | `glm-5.3` (1.40/4.40; $0.000273); cap `kimi-k3` (3.00/15.00; $0.001672) |
| Quality medium+ | pr-nanogpt | `zai-org/glm-5.2` (dynamic coverage ⚠️) | 0.42 / 1.32 | $0.000019/call | yes (200, 10.2 §4.3) | no second quality model verified on nanogpt ⇒ degrade to ollama `glm-5.3` (sensitive-safe) |

### 2.4 Privacy: public (`privacy:public`, default)

No provider restriction: rows 2.1–2.3 apply. Current routing (OBJ-26):
preference-first with nanogpt first, ollama second, opencode third; inside
nanogpt, covered-first with a 5 USD/window balance budget
(`nanogpt_max_balance_spend_usd`, implemented in OBJ-26/26a).

### 2.5 Privacy: sensitive (`privacy:sensitive` — opencode-go excluded by the gate)

| Need | Profile | Primary | Price $/1M | Real cost measured | Probe OK | Alternative |
|---|---|---|---|---|---|---|
| Sensitive worker (preference 0) | pr-nanogpt | `z-ai/glm-5.3-flash` | 0.075 / 0.25 | $0.000030/call, covered | yes (200 + cost=0) | `deepseek/deepseek-v4-flash` (0.14/0.28, cost=0) |
| Sensitive worker (preference 1) | pr-ollama | `gpt-oss:20b` | 0.07 / 0.30 | $0.000028/call | yes (200) | `glm-5.3-flash`@ollama ($0.000031) |
| Sensitive quality | pr-nanogpt | `zai-org/glm-5.2` | 0.42 / 1.32 | $0.000019/call (dynamic coverage ⚠️) | yes (200) | ollama `glm-5.3` (1.40/4.40; $0.000273); verify costUsd before pinning |
| Confidential | custom/local only | — | — | — | — | no cloud fallback: the gate blocks (correct, untouched) |

### 2.6 Registered exclusions (cataloged models that do NOT qualify)

- opencode-go: the full deepseek-v4 family (403 RegionError — China only),
  minimax-m2.7 and gpt-5.6-luna (500), grok-4.6 (401),
  muse-spark-*-contributor (403 DataPolicy — they train on your data),
  7 residual IDs without published price.
- nanogpt: `meta/muse-spark-1.3-contributor` (503 ×2), `qwen3.5-4b`
  (402 — not covered, drains balance).
- ollama-cloud: no exclusions (19/19 callable); `nemotron-3-ultra` kept off
  workers for latency (11.9 s in probe).
- OpenRouter: no 10.1–10.3 data ⇒ outside the matrix.

## 3. Real window necks (they decide, not the list price)

| Profile | Limiting window | Effect on the matrix |
|---|---|---|
| pr-opencode | $12/5h rolling + $30 week + $60 month | ~70–100 worker tasks/window with flash; the neck is the window, not the rate |
| pr-nanogpt | 60M INPUT tokens/week (soft cap: past the limit it keeps serving and burns balance) | ~40–60 Hermes tasks/week; per-model coverage checkable via costUsd |
| pr-ollama | 5h session + weekly (fraction without USD per request) | ~137 large calls fill the session; `activity.cost` lags ~35 min — unfit for live control |

## 4. Cost of the current window (calibrated ledger + governor snapshot)

- Prices calibrated against the console (docs/calibration-2026-09-08.md,
  windows closed with ±0.1% error): glm-5.2 1.1824/3.7162,
  glm-5.3-flash 0.1571/0.5236, qwen3.8-flash 0.15/0.47 (in/out $/1M). The
  ledger remains an upper bound (+16%) until more closed windows accumulate.
- Snapshot 2026-09-09 (governor observations): nanogpt weekly 100.05%
  (exhausted ⇒ blend to balance per OBJ-26, with a 5 USD/window budget),
  opencode-go rolling 7% / weekly 98% / monthly 69%, ollama weekly 51.8%.
  Reading: this week the weight fell on ollama and nanogpt balance —
  consistent with the covered-first matrix.

## 5. Measurement lessons that condition the matrix

1. Real cost per micro-call is governed by verbosity, not the rate:
   `qwen3.7-plus` cost 22× more than `glm-5.3-flash` on the same trivial
   task. Decision metric: measured $/task-type, not $/1M.
2. "Being on the subscription list" ≠ "covered" on nanogpt: the
   discriminator is the response's `cost`/`x_nanogpt_pricing.costUsd` field
   (glm-5.2 oscillated between charging and covering within 20 minutes).
3. opencode-go's `cost` field reads "0" under subscription: useless as a
   counter; cost is estimated with tokens × list price (ledger, known +16%
   bias) and calibrated against the console on closed windows.

## 6. Proposed changes NOT applied (pending the user's verdict)

Inherited from the previous 10.4 draft (t_bef3cbf0), still in triage without
approval. This document's matrix uses the CURRENT gate pins as primaries;
these proposals would only be implemented in 10.5 with the OK:

1. Worker pr-ollama: `deepseek-v4-flash` → `gpt-oss:20b` (3× cheaper and
   peak-free during European working hours).
2. Interactive pr-nanogpt: `zai-org/glm-5.2` → `z-ai/glm-5.3-flash`
   (stable covered, 4–5× cheaper).
3. Role swap on pr-opencode: `qwen3.8-flash` to interactive (plan's model,
   $30 included) and `glm-5.3-flash` to worker.
4. No change to `PRIVACY_CAPABILITIES`.

## 7. Sources

| Data | Source |
|---|---|
| Catalog, prices, windows and per-provider coverage (10.1) | t_9c61f54e → model-catalog-2026-09.md (probes and official sources accessed 2026-09-07) |
| Per-model callability (10.2) | t_514c568e → §4 of the same source; t_154b29f2 (flash micro-test end-to-end) |
| Real cost measured (10.3) | t_f94f8ef2 → §5 of the same source (140 requests, 50 models); raw sheets in that task's workspace |
| pr-nanogpt worker migration and verification | t_ffd28494 (gate + profile + direct chat; 3-layer audit) |
| Current pins and rules | scripts/quota-gate.py: PRIVACY_CAPABILITIES, PROFILE_MODELS, PROFILE_WORKER_MODELS, PRIVACY_PROVIDER_PREFERENCE, GENERAL_PROVIDER_PREFERENCE |
| Calibrated ledger prices | docs/calibration-2026-09-08.md + model-cost.json |
| Balance budget and covered-first (OBJ-26/26a) | commits bb5e99b/9e3ff1c; governor observations (request_balance_usd / request_covered_usd) |
