# OBJ-30 — Innovation fund: autonomous leadership with a budget (living doc)

**Date**: 2026-09-12 · **Parent task**: t_57c5f529 · **Status**: pilot
delivered, mechanisms running, **contract v2 written and awaiting the
owner's signature — nothing starts without it.**

## What this objective resolves

Third stage of the house's autonomy. Stage 1 (Jul–Aug 2026): execute
prescribed tasks (warm-up-then-scale). Stage 2 (Sep 2026): execute
objectives with direction (desire → capture → budget → cascade,
OBJ-26/27/28/29). Stage 3 (OBJ-30, proposed): **initiative with
resources** — the house identifies opportunities, budgets them, executes
them, and renders accounts. The budget is the container of trust that makes
this safe.

## The pilot (t_f075c609, 10-sep 02:48→20:00 CEST, cap $3.00)

- Delivered: OBJ-27 F1 (deterministic morning-screen + 08:00
  morning-report cron), F2 consumption alarms, F3 trace retention, OBJ-29
  supply_ratio in metrics-history. Real balance cost: **$0.00** (free
  window quota).
- Honest failures that became house rules: (1) a window date mis-transcribed
  ("tomorrow 17:00" written as same-day) → rule: transcribe dates verbatim
  and re-read them; (2) **13 hours of dead board with the fund active** →
  the "active fund = living queue" rule and its mechanisms were born from
  this.

## Mechanisms already running (verified 2026-09-12)

| Mechanism | What it does | State |
|---|---|---|
| budget-check (15m cron) | NanoGPT window budget: cap $5.00, spent $1.70, WARN at 50% | ok |
| burn-watchdog (10m cron) | multi-provider burn watchdog; enforcement ON on NanoGPT | ok |
| fondo-queue-watch (15m cron) | if a fund window is active and the queue is empty >30 min: refill cascade | ok |
| tick-cola-viva (in governor-tick 15m) | with free quota and 0 workers: assign ready-no-assignee or create 1 structural class-C successor; never filler | ok |
| supply_ratio + forecast + cost-ledgers | accounting and machine-verifiable queue-drought diagnosis | ok |

Also repaired during the run: the autonomous-task-creator and
morning-report crons had been dead >24h on a 3-tuple/2-value unpack
(upstream `hermes-agent`, fixed in fork commit `3919d5b002`); the fund's
eyes and voice are crons, so their health is now watched in the
morning-report.

## The contract v2 (awaiting signature)

`OBJ-30-contrato-fondo-v2.md` (task t_57c5f529 attachment) consolidates
what the pilot proved into a signable proposal. Its shape:

- **The fund**: proposed $5.00/month of balance, split into flight windows
  (e.g. $1–2 per week). Each window opens as a board task-contract (cap,
  WARN 50%, hard STOP 100% with no auto-extension, closing date transcribed
  verbatim, account rendering ≤24h after close). Unspent budget is not
  virtue: with an open window and a living queue, spending $0 by leaving
  tasks unassigned is under-execution, not prudence.
- **The committee**: spending is not unilateral. Desire → triage capture
  (hypothesis, max cost, success criterion **measurable before spending**)
  → the owner's express word opens the window → watchdogs guard it →
  rendering closes the cycle. Class A (creds, sensitive config,
  kill-switches, external-effect crons) always requires the owner.
- **The portfolio**: max 1 large active piece (warm-up-then-scale, again);
  the rest of the flight is free quota. No filler tasks to show movement
  (OBJ-29/39 golden rule).
- **The learning**: each finished initiative moves the next month's
  ceiling — +50% with verifiable success, −50% with unexplained failure,
  no change when the failure is diagnosed and repaired.
- **No self-reform**: the fund never touches its own ceiling, thresholds,
  kill-switches, or class-A guardrails. Only the owner signs those.
- **Kill-switches**: no STOP file present today; `INNOV-STOP` (if signed)
  behaves like `PROMOTE-STOP`: one file, `touch` it, everything stops.

## Pending decisions (all owner's)

1. Sign the contract v2 → explicit amount + the express word "arranca
   OBJ-30".
2. First portfolio piece among the mapped candidates (OBJ-34 next growth
   piece; OBJ-37 S1 research phase; structural successors of delivered
   work).
3. Any amendment to the fund's monthly ceiling.

## Where the deep docs live

- Contract v2 (signable): `OBJ-30-contrato-fondo-v2.md` — attachment of
  task t_57c5f529 on the kanban board.
- Design sketch v1: body of task t_57c5f529; pilot account rendering:
  comments of task t_f075c609.
- Capability context: `docs/obj34-capability-map.md` (class C vs A-adjacent
  and the fund's measured state).