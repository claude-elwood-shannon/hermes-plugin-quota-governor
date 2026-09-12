# hermes-plugin-quota-governor

## Objectives status
- 1: Initial plugin setup and documentation
- 2: Add basic quota governor mechanism
- 3: Implement observation hooks for task lifecycle
- 4: Add per-model cost ledger
- 5: Add quota time-series sampling
- 6: Implement privacy routing matrix
- 7: Add disaster recovery procedures
- 8: Implement weekly objective progress reporting
- 9: Add zombie guard for stale tasks
- 10: Implement predictive quota forecasting
- 11: Add per-request billing visibility for NanoGPT
- 12: Add dynamic task generation for autonomous plugins
- 13: Add autonomous objectives tracking
- 14: Add observer dashboards
- 15: Add task cost estimation
- 16: Add model matrix documentation
- 17: Add privacy by provider design
- 18: Add opencode go reset semantics
- 19: Add dynamic task generation
- 20: Add autonomous-objectives.md
- 21: Add privacy-routing-matrix.md
- 22: Add rust-projects-to-contribute.md
- 23: Add skills-discovery.md
- 24: Add obj-07-audit.md
- 25: Add disaster-recovery.md
- 26: Objective 26: Description placeholder
- 27: Objective 27: Description placeholder
- 28: Objective 28: Description placeholder
- 29: Objective 29: Description placeholder
- 30: Objective 30: Description placeholder
- 31: Objective 31: Description placeholder
- 32: Objective 32: Description placeholder
- 33: Objective 33: Description placeholder
- 34: Objective 34: Description placeholder
- 35: Objective 35: Description placeholder
- 36: Objective 36: Description placeholder
- 37: Objective 37: Description placeholder
- 38: Objective 38: Description placeholder
- 39: Objective 39: Description placeholder
- 40: Objective 40: Description placeholder
- 41: Objective 41: Description placeholder
- 42: Objective 42: Description placeholder
- 43: Objective 43: Description placeholder
- 44: Objective 44: Description placeholder

[![tests](https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor/actions/workflows/tests.yml/badge.svg)](https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org)

> Hermes Agent plugin: self-governance by quota

A Hermes Agent plugin that observes the kanban lifecycle, queries provider
quota in real time, and coordinates the kanban daemon accordingly — so the
agent can self-organise work based on available quota without human
intervention.

## Why this exists

This plugin came out of a real failure: a single model silently consumed
82% of a $12 rolling spend window while the agent kept dispatching work —
nothing was watching the money. Providers expose quota *numbers*; nothing
tied those numbers to the *decision* of whether an autonomous agent should
keep spawning workers.

The governor closes that loop: observe (kanban lifecycle + provider quota)
→ decide (a deterministic heuristic, no LLM in the loop) → act
(start/stop/scale the worker daemon). It has governed a multi-agent
workspace continuously since then, and grew the pieces nobody publishes
and we needed most:

- a **per-model cost ledger** calibrated against real provider billing —
  Hermes does not persist the API cost field, so the ledger estimates from
  token counts × published prices as a conservative upper bound;
- a **quota time-series** sampled with zero extra API calls, driving
  threshold forecasts;
- **per-task cost prediction** (median/p90 by objective) trained on its own
  observation data.

`docs/` carries the calibration notes, the per-model matrix and the design
records — the measurements are the point, not an afterthought.

## What it does

The governor has five behaviours, wired as plugin hooks:

| Hook | Fires in | What it does |
|------|----------|-------------|
| `kanban_task_claimed` | Dispatcher | Queries quota before a worker spawns; writes stop-signal if critical |
| `kanban_task_completed` | Worker | Records task cost; re-evaluates quota after completion |
| `kanban_task_blocked` | Worker | Records blocked tasks for audit |
| `post_tool_call` | Any session | Lightweight quota sampling every 50 tool calls |
| `on_session_end` | Any session | Final quota snapshot when session closes |

All hooks are **observer-only** — they record and signal, never veto. The
cron layer (a `no_agent=True` script) reads the signals and controls the
daemon.

## Providers

| Provider | Endpoint | Metric | Drives decision? |
|----------|----------|--------|-------------------|
| Ollama Cloud | `GET /api/usage` | Session % + Weekly % | Yes (primary) |
| NanoGPT | `GET /api/subscription/v1/usage` | Daily % + Weekly tokens % | Informational |
| OpenRouter | `GET /api/v1/key` | USD (weekly/monthly) | Informational |
| OpenCode Go | `GET /zen/go/v1/usage` | Rolling 5h + Weekly + Monthly % | Yes (pr-opencode) |

Only Ollama Cloud's session/weekly windows map cleanly to the "should I
keep spawning workers?" decision. NanoGPT and OpenRouter are reported as
context.

## Per-model cost ledger (MULTI-PROV-09)

One model can silently burn most of an OpenCode Go 5h window (the Sep 7
2026 incident: glm-5.2 alone took 82% of the $12 window). The
`model-cost-ledger.py` script makes that visible without opening the
console:

- **Ledger**: `~/.hermes/quota-governor/model-cost-ledger.jsonl`
  (append-only, one JSON row per session×model×task usage *delta*):
  `{ts, window, profile, model, cost, request_count, tokens{in,out,cache_read}, session_id, task}`.
  `window` is the ISO UTC start of the rolling 5h window, anchored to the
  live `rolling.resetsAt` (windows roll; they are NOT fixed clock hours).
- **Source of truth**: Hermes does not persist the API `cost` field
  (`session_model_usage.estimated_cost_usd` is always 0 — verified Sep 7),
  so the ledger estimates `cost = tokens × published prices`
  (input, output+reasoning, cache_read; cache_write excluded). Calibrated
  live Sep 7 2026 against the console's per-model table, the estimate is a
  CONSERVATIVE UPPER BOUND (+16%..+125%: Hermes records the full prompt as
  input on every call while the provider meters cached context at the
  cheaper cache-read rate) — warnings fire early, never late; request and
  token counts per model are exact. Recalibrate the price triples after
  ~1 week of data via `~/.hermes/quota-governor/model-cost.json`
  (`{"prices": {model: [in, out, cache]}, "window_usd": 12.0,
  "warn_fraction": 0.5}`) — no code change needed.
- **Automatic sync**: quota-gate.py opportunistically syncs the ledger on
  every cron run (throttled to ≤1 sync/20 min; the gate cron cadence is
  30 min) — zero extra API calls, zero tokens. The FIRST sync after
  deployment is a historical catch-up batch (whole pre-existing sessions
  land in the window of their last activity); from then on 30-min deltas
  track the right window closely.
- **Gate integration**: the snapshot's `context.model_cost` carries the
  current-window per-model shares, and any model over `warn_fraction`
  (default 50%) of the window budget injects a
  `WARNING: <model> consumed NN% of the OpenCode Go 5h window…` line into
  `context.warning` for the task creator. All ledger failures degrade to
  silence — observability never breaks the gate.
- **Queries**: `python3 scripts/model-cost-ledger.py report [--json] [--last 24h]`
  reproduces the console's per-model consumption table; `sync` forces an
  immediate accumulation pass.
- **Re-baselining**: delete BOTH `model-cost-ledger.jsonl` and
  `model-cost-ledger.cursor.json` together (deleting only the ledger
  would re-count from the cursor and lose history; deleting only the
  cursor would double-count).

## Weekly objective progress (OBJ-08)

`scripts/weekly-progress.py` regenerates
`~/.hermes/quota-governor/objective-progress.json` in the multi-objective
structure of `autonomous-objectives.md` §4 (one entry per `OBJ-NN`: status,
`last_task_created`, `tasks_completed`) purely from the `objective:OBJ-NN`
header tags in kanban.db, and renders the ISO-week report §7 calls for into
`~/.hermes/profiles/pr-ollama/docs/weekly-reports/YYYY-WW-weekly-summary.md`
(objectives completed vs in progress, tasks completed in the window, quota
consumed per provider from the model-cost + burn ledgers).

- Dry-run by default; `--execute` writes (atomic tmp+rename, fully
  regenerated -> idempotent).
- `awaiting_human_verification` for OBJ-01 (§4: human check required);
  `needs_attention` when tagged tasks were lost (archived without
  `completed_at`).
- Cron (register once):
  `hermes cron create weekly-progress --name weekly-progress --script weekly-progress-cron.sh --no-agent --deliver local "0 23 * * 0"`

## GPU health section (OBJ-27)

The observability portal's **GPU ml-host** page
(`scripts/obs/portal-build.py` → `read_gpu_health` / `page_gpu`) shows
temperature, VRAM, utilization, the active vLLM model, daily rounds, service
state and a thermal sparkline. Its configuration surface is documented in
[`docs/gpu-health-config.md`](docs/gpu-health-config.md) — note there is
currently **no dynamic config surface**: every option is a hardcoded module
constant (`_GPU_HOST`, `_GPU_API`, `_GPU_CACHE`, `_GPU_CACHE_TTL`), the
section is always rendered, and unreachable-host cases degrade to empty
states (`n/d`, `unknown`) rather than fake zeros.

## Deterministic zombie guard (OBJ-21)

Guardrail G3 of the autonomous-task-creator prompt ("any running task older
than 45 minutes → `[SILENT]`") used to live only in the prompt, so its
enforcement was stochastic — a tick could mis-read the board and feed work
anyway. The gate snapshot now carries `context.zombie_check`:

```json
{"has_zombie": false, "count": 0, "threshold_minutes": 45.0, "tasks": []}
```

- **Age base**: `last_heartbeat_at` (worker liveness), falling back to
  `started_at`; only `status='running'` rows are examined — measuring the
  age of a COMPLETED task is meaningless (that exact mistake produced the
  false "87 min zombie" report behind OBJ-21).
- **Decisional, not informational**: when `has_zombie` is true the gate
  forces `wakeAgent:false` and appends a `zombie_guard:` line to
  `context.warning` (creator silenced regardless of what the LLM sees).
  The recommendation fields stay visible in the context for auditability.
- **Prompt kept as second line of defence**: G3 remains in the creator
  prompt (reworded as "defense in depth") — the gate is the enforcement.
- Tests: `test_zombie_check.py` (19 cases incl. the done-task regression),
  `test_cron_prompt_zombie.py` (active-prompt + deploy-drift regression),
  `e2e_zombie_live.py` (live gate vs the real board + injected-zombie copy).

## Predictive quota system (OBJ-24)

Three no_agent pieces that observe and forecast WITHOUT burning tokens
(the LLM workers keep burning where they already burn; the predictive
layer never spends tokens):

**F1 — time-series collector** (`quota-metrics.py`, cron every 15m): samples
board counts + all providers' last-good pct and appends to
`~/.hermes/profiles/pr-ollama/quota-governor/metrics-history.jsonl`. Zero
extra API calls (the tick's last-good IS the sample). Since the OBJ-26a
follow-up it also carries `nanogpt_request_balance_usd` /
`nanogpt_request_covered_usd` (see below). Tests: `test_quota_metrics.py`.

## Per-request billing visibility (OBJ-26a follow-up, t_92d7f0d6)

The in-process capture (hermes-agent `nanogpt_pricing_capture`) appends one
row per NanoGPT request to `nanogpt-requests.jsonl` under the CAPTURING
process's HERMES_HOME. Three read paths expose the window accumulators —
always as SEPARATE fields, never merged with probe-derived spend meters
(`window_spent_usd`, `activity_cost`, burn-watchdog `spent_usd`):

- `nanogpt-balance-ledger.request_window_totals_all_homes()`: merges the
  per-request rows across every HERMES root (`~/.hermes` + all profiles;
  `QUOTA_GOVERNOR_PROFILE_HOMES` os.pathsep override). `homes_read == 0`
  means "no capture data anywhere" — consumers report None, not 0.0.
- `quota-governor-tick.sh` → `tick-observation.py`: the tick now PERSISTS a
  `quota_tick` row in `observations.jsonl` (it decided but never wrote one;
  in-process hooks don't fire in no_agent cron) with
  `quota.request_balance_usd` / `quota.request_covered_usd`.
- Core `record_observation()`: every hook row (`task_claimed`,
  `session_end`, `periodic_sample`, …) carries the same two fields.

Privacy:low — only USD aggregates at NanoGPT scale (1e-06); no per-request
rows, prompts, or model names reach observations.jsonl.

**F2 — EMA predictor** (`quota-forecast.py`, cron every 15m after metrics):
for each provider, exponential moving average of the weekly burn rate
(%/min) over a 6h window (alpha 0.3), with pairs closer than 120s discarded
(unsynchronised last-good refreshes produce absurd rates). Projects the
90% milestone (governor stop) and 100% (exhaustion) for the WEEKLY window
(all three providers reset Monday ~02:00 CEST). Output `forecast.json`:
`{providers: {provider: {pct_now, burn_rate_pct_per_min, eta_90_iso,
eta_100_iso, eta_90_hours, confidence, samples, pairs_used}}}` plus
`next_weekly_reset_iso` / `hours_to_reset`. The per-provider data is
NESTED under `providers` — that is the shape the gate (`--suggest`) and
`budget_check.py` consume (OBJ-24 contract; top-level provider keys were
a writer bug and are gone). Tests: `test_quota_forecast.py`.

Gate integration (`--suggest` mode, wrapper `forecast-gate.sh`): injects
`context.forecast_warning` with the fired decision rules —
- `eta_90 < 1h` → board off (wakeAgent stays as-is in suggest mode),
- `eta_90 < hours_to_reset - 2h` (COLCHÓN) → max_workers=1 + cost cap.
The plain gate (no flags) produces byte-identical output as before —
zero regression. Real veto only with `--enforce` after 7 days of
backtest (flag already parsed; wired in `forecast_context`). Tests:
`test_forecast_gate.py`.

**F3 — per-task budget** (`budget_check.py`, cron every 15m + observer
spawn from `kanban_task_claimed`): crosses (a) the OBJ-07 calibration
(`docs/quota-planner.md` §2.4: tiny 0.5%, small 2.1%, medium 4%,
complex 24.8% of the Ollama window; micro 0.25% by order of magnitude),
(b) the provider's free quota from the F2 forecast, (c) the F2 ETA.
If the task's cost class exceeds 10% of its provider's remaining free
quota, it logs `reassign` (to the provider with most headroom) or
`triage` (no alternative has room). Phase 3.0 is OBSERVER-only (log
pattern); the real veto runs with `--enforce` after a week without
false positives. Tests: `test_budget_check.py`, `test_budget_hook.py`.

**F2 backtest harness** (`backtest-f2.py`, cron every 15m after forecast):
measures whether F2 actually hits the close criterion. Each run (1) SNAPS
the current `forecast.json` into an append-only ledger
(`~/.hermes/quota-governor/forecast-backtest.jsonl`) — the snapshot mode
that survives forecast.json's per-tick overwrite; (2) EVALUATES every open
snapshot/provider against the real crossing time in metrics-history:
`error_pct = |t_cross_actual − eta_90_pred| / margin_to_reset × 100`
(<20 → OK, else FAIL); before the milestone is crossed the error stays
OPEN (never counted as failure), and snapshots past the milestone, without
a usable ETA, or whose weekly window elapsed with no crossing resolve to
NA. (3) Appends a daily OK/FAIL/OPEN verdict per provider whenever it
changes; stdout announces only a NEW FAIL (watchdog pattern). Gate
`--enforce` unlocks after 7 consecutive days of this ledger with no FAIL.
Zero API calls — reads only forecast.json + metrics-history.jsonl. Tests:
`test_backtest_f2.py` (15 cases incl. the tolerant-degradation set).
Register:
`HERMES_HOME=~/.hermes/profiles/pr-ollama hermes cron create "every 15m" --name backtest-f2 --script backtest-f2-cron.sh --no-agent --deliver local`

Completeness criterion (from the task): with 24h of history the forecast
must hit the 90% milestone with <20% error at reset time; zero quota
wasted (board active while quota > margin, board self-off when
eta_90 < 1h); the 15m tick, gate and burn-watchdog keep operating
unchanged (metrics/forecast are additive and no_agent).

## Decision heuristic

The governor uses a **three-state model** (`run`, `paying`, `stop`) that
accounts for Ollama Cloud's two-layer billing: included quota (free) and
pay-as-you-go balance (charged when included quota is exhausted).

Based on real cost data (Aug 2026):

| State | Sub-level | Session % | Weekly % | Workers | Max task |
|-------|-----------|-----------|----------|---------|----------|
| `run` | healthy | < 30% | < 50% | 2 | any |
| `run` | moderate | 30–60% | < 75% | 1 | medium |
| `run` | cautious | 60–95% | < 75% | 1 | small |
| `paying` | — | 100% | < 90% | 1 | small |
| `stop` | — | any | > 90% | 0 | none |
| `stop` | — | any | — | 0 | none (spending limit exceeded) |

**`paying` state:** When session usage hits 100% and `activity.cost` is
rising (balance is being consumed), the governor enters `paying` mode. It
warns but allows 1 worker to continue — spending real money. The governor
transitions to `stop` when `activity.cost >= QUOTA_GOVERNOR_MAX_SPEND` or
weekly quota is critical (> 90%).

Weekly override (most restrictive wins):
- Weekly > 75% → tiny tasks only
- Weekly > 90% → stop entirely

## Quickstart

```bash
# run the suite (1,000+ tests, no network needed; each file runs directly —
# some module basenames repeat across dirs, so `unittest discover` is not usable)
git clone https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor
cd hermes-plugin-quota-governor
failed=0
for t in $(find . -name 'test_*.py' -not -path './.git/*'); do
  python3 "$t" >/dev/null 2>&1 || { echo "FAIL: $t"; failed=1; }
done
[ "$failed" -eq 0 ] && echo "suite green"

# install as a Hermes plugin
hermes plugins install claude-elwood-shannon/hermes-plugin-quota-governor --enable
```

Two tests exercise *deployed* scripts that live outside the repo
(`~/.hermes/scripts/*`); they skip automatically on a fresh clone.

## Slash command

```
/quota-governor status          — current quota + decision
/quota-governor decision        — what the governor would decide now
/quota-governor history         — recent observations
/quota-governor daemon          — daemon status
/quota-governor daemon-start    — start daemon with quota-aware --max
/quota-governor daemon-stop     — stop daemon gracefully
/quota-governor clear-signals   — remove stop-signal files
```

## Status example

When the system is in `paying` mode (included quota exhausted, spending
pay-as-you-go balance), `/quota-governor status` shows `Mode` and `Cost`:

```
=== Quota Governor Status ===

Ollama Cloud (primary):
  Session:  100.0%  (337 requests)
  Weekly:    43.5%  (1073 requests)
  Mode:      PAYING  ⚠ spending pay-as-you-go balance
  Cost:      $1.25  (limit: $5.00)

Decision: PAYING
  Workers: 1
  Max task: small
  Reason:  session exhausted, spending pay-as-you-go balance ($1.25 / $5.00)
```

In `run` mode, `Mode` shows `RUN` and `Cost` is omitted (no pay-as-you-go
spend). In `stop` mode, `Mode` shows `STOP` with the stop signal active.

## Architecture

```
  Plugin hooks (observer-only)
    │
    ├── kanban_task_claimed  → query quota → record → maybe write STOP signal
    ├── kanban_task_completed → record cost → re-evaluate
    ├── post_tool_call       → periodic sample (every 50 calls)
    └── on_session_end       → final snapshot
    │
    ▼
  State file: $HERMES_HOME/quota-governor/observations.jsonl
  Stop signal: $HERMES_HOME/quota-governor/STOP
    │
    ▼
  Cron (no_agent script, zero tokens)
    │
    ├── reads STOP signal → if present, skip tick
    ├── queries quota → applies heuristic
    ├── if quota OK → start/adjust daemon with --max N
    └── if quota critical → stop daemon, write STOP signal
```

## Files

```
hermes-plugin-quota-governor/
├── plugin.yaml          — manifest (hooks, metadata)
├── __init__.py           — hook registration + slash command
├── providers.py          — multi-provider quota query (Ollama, NanoGPT, OpenRouter)
├── quota_planner.py      — decision heuristic
├── quota_governor.py     — core logic: state, observations, daemon control
├── LICENSE
└── README.md
```

## Requirements

- Hermes Agent with kanban support
- Tor (for GitHub interactions — see `github-tor-privacy` skill)
- API keys in `~/.hermes/.env` or `~/.hermes/profiles/<name>/.env`:
  - `OLLAMA_API_KEY` (required — drives the decision)
  - `NANO_GPT_API_KEY` (optional — informational)
  - `OPENROUTER_API_KEY` (optional — informational)
- Optional env vars:
  - `QUOTA_GOVERNOR_MAX_SPEND` (default: `5.00`) — Maximum cumulative
    pay-as-you-go spend in USD (`activity.cost` from Ollama's `/api/usage`)
    before the governor transitions from `paying` to `stop`. Set to `0` to
    disable the spending limit (warn-only, never stop on cost; weekly quota
    limits still apply).

## License

MIT