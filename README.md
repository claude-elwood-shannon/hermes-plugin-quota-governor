# self-govern

[![tests](https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor/actions/workflows/tests.yml/badge.svg)](https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org)

> **Hermes puede hacer cualquier cosa, y self-govern le ayuda a hacerlo de forma autónoma, con presupuesto, con calidad, y con observabilidad.**

A Hermes Agent plugin that turns an agent runtime into a self-governing
workspace: a kanban board where workers claim and close tasks on their own,
a quota governor that decides how much work the house can afford right now,
a mediator bridge so a human steers without babysitting, and an
observability stack so every decision leaves evidence.

It is installed as the Hermes plugin `self-govern` (repo:
`hermes-plugin-quota-governor`; older checkouts name the plugin
`quota-governor` in `plugin.yaml`).

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
and we needed most.

## The usage model: human + mediator, an iterative cycle

One human, a bounded budget, and agents that keep working between looks.
The loop that makes that work:

```
        ┌────────────────────────────────────────────────────────┐
        │                                                        │
        ▼                                                        │
  HUMAN (direction, taste, budget)                               │
    │  ratifies objectives, approves budgets, arbitrates         │
    ▼                                                            │
  MEDIATOR (Open WebUI or any frontend)                          │
    │  writes tasks into triage via the bridge HTTP API          │
    ▼                                                            │
  BOARD (kanban) ──► DISPATCHER assigns by quota & cost class    │
    │                    │                                       │
    ▼                    ▼                                       │
  WORKERS claim → heartbeat → work → close with evidence         │
    │                    │                                       │
    ▼                    ▼                                       │
  GOVERNOR observes quota & lifecycle ──► scales/starts/stops    │
    │                                                            │
    ▼                                                            │
  OBSERVABILITY (portal, morning report, ledgers, forecasts) ────┘
       every cycle returns evidence for the next human decision
```

- **The human** owns direction: objectives (OBJ), budgets, and anything
  tagged as needing approval. Governance guardrails (GR1–GR11,
  `docs/guardrails-autonomous-objectives.md`) bound what the system may
  propose by itself — nothing outside the plugin repo or `~/.hermes/`,
  no new credentials without approval.
- **The mediator** is the human's hands between visits: it creates tasks,
  moves and comments on them through the bridge
  (`docs/bridge-open-webui.md`). A single `GET /bootstrap` call hands any
  newcomer the whole context: plugin info, board stats, objectives,
  capabilities, health.
- **The cycle is iterative**: every closure leaves evidence (tests, docs,
  metrics); every morning the board reports its night unasked; the next
  wave of tasks grows from that evidence, not from filler.

The flight constitution, ratified three times by the owner:
free quota available → fly; no free quota but approved budget → fly with a
cap; neither → stop, and only after verifying reality — a "we're out of
quota" claim must be earned by a check, never recited.

## What it does

Capabilities, each with its own doc where one exists:

- **Quota governance** — multi-provider quota polling (Ollama Cloud,
  NanoGPT, OpenRouter, OpenCode Go), a three-state decision model
  (`run` / `paying` / `stop`), and a cron tick (zero tokens) that starts,
  scales or stops the worker daemon. Observer-only plugin hooks record the
  lifecycle; the cron layer acts.
- **Autonomous objectives** — the system proposes, budgets and closes its
  own improvement objectives (`OBJ-*`) inside guardrails; per-objective
  budgets aggregate real spend (`docs/objectives.md`,
  `docs/obj28-budget-by-objective-design.md`).
- **Per-model cost ledger** — append-only per-session×model×task cost rows
  (Hermes does not persist API cost; the ledger estimates from tokens ×
  published prices, a conservative upper bound), auto-synced on every gate
  run. `python3 scripts/model-cost-ledger.py report [--json] [--last 24h]`.
- **Predictive quota** — time-series collector, EMA burn-rate forecaster
  with ETA to the 90% milestone, per-task budget checks, and a backtest
  harness that measures whether the forecast actually hits
  (`docs/quota-planner.md`). All `no_agent` cron — the predictive layer
  never spends tokens.
- **Deterministic zombie guard** — a running task without a live heartbeat
  past the threshold silences the task creator, enforced in the gate
  snapshot, not in a prompt.
- **Mediator bridge** — zero-dependency HTTP server (port 9120) exposing
  board read/write operations to Open WebUI or any frontend, with an
  OpenAPI description at `/openapi.json` (`docs/bridge-open-webui.md`).
- **Modular capabilities** — optional capability packs under
  `capabilities/` (e.g. `sysadmin-lan` for LAN sysadmin work) with
  fail-closed manifest guards: operations outside the manifest are blocked
  and logged (`capabilities/README.md`).
- **Observability** — local trace JSONL with OpenTelemetry semantic
  conventions, optional OTLP export, local portal on :8917, morning report
  at 08:00, efficiency-ratio and burn watchdogs
  (`docs/obs-portal.md`, `docs/obs-otlp.md`).
- **Weekly objective progress** — regenerates per-objective progress and
  renders the ISO-week report from `objective:OBJ-NN` tags alone.
- **GPU health** — the portal's GPU page shows temperature, VRAM,
  utilization and the active vLLM model, with thermal guardrails
  (`docs/gpu-health-config.md`).
- **Per-request billing visibility** — NanoGPT per-request rows expose
  subscription-covered vs balance-draining spend, always as separate
  accumulators, never merged with probe-derived meters.

## Providers

| Provider | Endpoint | Metric | Drives decision? |
|----------|----------|--------|-------------------|
| Ollama Cloud | `GET /api/usage` | Session % + Weekly % | Yes (primary) |
| NanoGPT | `GET /api/subscription/v1/usage` | Daily % + Weekly tokens % | Informational |
| OpenRouter | `GET /api/v1/key` | USD (weekly/monthly) | Informational |
| OpenCode Go | `GET /zen/go/v1/usage` | Rolling 5h + Weekly + Monthly % | Yes (pr-opencode) |

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

**`paying` state:** when session usage hits 100% and `activity.cost` is
rising, the governor warns but allows 1 worker to continue — spending real
money. It transitions to `stop` when `activity.cost >=
QUOTA_GOVERNOR_MAX_SPEND` or weekly quota is critical (> 90%).

Weekly override (most restrictive wins): weekly > 75% → tiny tasks only;
weekly > 90% → stop entirely.

## Installation

Requirements:

- Hermes Agent with kanban support
- API keys in `~/.hermes/.env` (or `~/.hermes/profiles/<name>/.env`):
  - `OLLAMA_API_KEY` (required — drives the decision)
  - `NANO_GPT_API_KEY` (optional — informational)
  - `OPENROUTER_API_KEY` (optional — informational)
- Optional: Tor for GitHub interactions (this deployment enforces it; see
  the `github-tor-privacy` skill)
- Optional env var: `QUOTA_GOVERNOR_MAX_SPEND` (default `5.00`) — maximum
  cumulative pay-as-you-go spend in USD before `paying` becomes `stop`.
  Set to `0` to disable the cap (weekly quota limits still apply).

```bash
# clone and run the suite (1,000+ tests, no network needed; each file runs
# directly — some module basenames repeat across dirs, so `unittest
# discover` is not usable)
git clone https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor
cd hermes-plugin-quota-governor
failed=0
for t in $(find . -name 'test_*.py' -not -path './.git/*'); do
  python3 "$t" >/dev/null 2>&1 || { echo "FAIL: $t"; failed=1; }
done
[ "$failed" -eq 0 ] && echo "suite green"

# install as a Hermes plugin (installs AND enables)
hermes plugins install claude-elwood-shannon/hermes-plugin-quota-governor --enable
hermes plugins list        # verify it shows self-govern (or quota-governor)

# run the tests inside an agent session: the skill is loaded by name
# (see docs/getting-started.md for the full first-flight walkthrough)
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
/quota-governor set-limit [V]   — show or set the spending limit (USD)
/quota-governor health          — fast burn, zombie workers, silent plugin
```

## Architecture

```
  Plugin hooks (observer-only)
    │
    ├── kanban_task_claimed   → query quota → record → maybe write STOP signal
    ├── kanban_task_completed → record cost → re-evaluate
    ├── kanban_task_blocked   → record for audit
    ├── post_tool_call        → periodic sample (every 50 calls)
    ├── on_session_end        → final snapshot
    └── on_kanban_dispatch_tick → housekeeping (privacy router, assignee
                                  fix, approval-ready fix)
    │
    ▼
  State file: $HERMES_HOME/quota-governor/observations.jsonl
  Stop signal: $HERMES_HOME/quota-governor/STOP
    │
    ▼
  Cron (no_agent script, zero tokens)
    ├── reads STOP signal → if present, skip tick
    ├── queries quota → applies heuristic
    ├── if quota OK → start/adjust daemon with --max N
    └── if quota critical → stop daemon, write STOP signal

  Mediator (Open WebUI) ──HTTP 9120──► bridge ──CLI──► hermes kanban / logs
  Observability: trace JSONL ──► portal :8917 / OTLP / morning report
```

## Repository map

```
hermes-plugin-quota-governor/
├── plugin.yaml           — manifest (name, hooks, metadata)
├── __init__.py           — hook registration + /quota-governor slash command
├── providers.py          — multi-provider quota query
├── quota_planner.py      — decision heuristic
├── quota_governor.py     — core logic: state, observations, daemon control
├── concurrency_guard.py  — concurrency + health checks for the daemon
├── health_checks.py      — fast burn / zombie / silent-plugin checks
├── capabilities/         — modular capability packs (manifest + guard)
├── scripts/              — cron scripts: tick, gate, ledger, forecast,
│                           bridge, watchdogs, obs stack
├── docs/                 — design records, calibration notes, guides
└── tests/                — offline-first test suites
```

Key docs:

| Doc | What it covers |
|-----|----------------|
| [`docs/getting-started.md`](docs/getting-started.md) | first flight: install → enable → bootstrap → mediator |
| [`docs/bridge-open-webui.md`](docs/bridge-open-webui.md) | the mediator bridge: endpoints, deployment, security |
| [`docs/guardrails-autonomous-objectives.md`](docs/guardrails-autonomous-objectives.md) | the 11 governance guardrails |
| [`docs/objectives.md`](docs/objectives.md) | objectives index (OBJ-*) |
| [`docs/quota-planner.md`](docs/quota-planner.md) | quota decision machinery |
| [`docs/obs-portal.md`](docs/obs-portal.md) | the local observability portal |
| [`docs/coding-standards.md`](docs/coding-standards.md) | code conventions for contributions |

## Additional features

- **pr-vllm** local worker profile now integrated; see `docs/quota-planner.md` for routing rules.
- **supply_ratio** knob in the planner controls perpetual supply; documented in `docs/obj29-perpetual-supply.md`.
- **innovation fund (OBJ-30)** — an autonomous innovation fund with its own budget: pilot delivered, mechanisms running, and the funding contract awaits the owner's signature before any window opens; documented in `docs/obj30-innovation-fund.md`.

## Contributing

Run the offline suite before proposing changes (see Installation). Follow
`docs/coding-standards.md`. Commit messages in English, `type(scope)`
convention, with `Co-authored-by: Hermes Agent <agent@nousresearch.com>`
when an agent did the work.

## License

MIT
