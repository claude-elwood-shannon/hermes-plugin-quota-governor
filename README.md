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

Only Ollama Cloud's session/weekly windows map cleanly to the "should I
keep spawning workers?" decision. NanoGPT and OpenRouter are reported as
context.

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