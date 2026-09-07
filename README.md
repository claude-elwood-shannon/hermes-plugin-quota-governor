# hermes-plugin-quota-governor

> Hermes Agent plugin: self-governance by quota

A Hermes Agent plugin that observes the kanban lifecycle, queries provider
quota in real time, and coordinates the kanban daemon accordingly — so the
agent can self-organise work based on available quota without human
intervention.

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

## Install

```bash
hermes plugins install claude-elwood-shannon/hermes-plugin-quota-governor --enable
```

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