# OBJ-28 — Budget by Objective: status (living doc)

**Date**: 2026-09-12 · **Task**: t_d540cee7 · **Status**: phase 0 (observer)
LIVE; phases 1-3 gated by the user's 7 design-§4 decisions. Nothing vetoes
anything yet.

## What this objective resolves

The objective is the aggregate container: a single `OBJ-xx` spans many
tasks, and no task-level check can bound the *sum*. Three budget layers
already exist; this one closes the third:

| Layer | Mechanism | Currency | Gate point |
|-------|-----------|----------|------------|
| Provider (OBJ-26) | `model-cost.json` window/balance | USD | dispatch |
| Task (OBJ-24 F3) | `cost:` tag vs free quota | % of window | creation + dispatch |
| **Objective (OBJ-28)** | rollup by `objective:` tag | **dual: costUsd + quota_pct** | creation + dispatch (new hook) |

Motivating case: the OBJ-26 monolith (5 crashes, ~$0.30–0.40 spent before a
human saw the pattern) — a $1 objective budget with a variance rule would
have re-scoped at crash 2.

## What is live today (phase 0, verified)

- `scripts/obs/objective-budgets.py`: pure read-only rollup over the
  OBJ-27 F0 trace → `quota-governor/objective-budgets.json` (atomic,
  idempotent, fails open; human ceilings preserved across runs).
- Tests 20/20, full obs family green; hourly no_agent cron (7d observer
  window); portal 7th page "Objetivos".
- State at birth: 44 objectives rolled up; 647 tagged vs 795 untagged task
  events; **$36.8911 / 30d** in the explicit `unattributed_cost` bucket.

## The honest limitation (shown, not hidden)

Every cost-bearing trace source (nanogpt-requests, usage-audit,
model-cost-ledger) carries **no task_id**, so per-objective `spent_usd` is
0.0 everywhere until sources learn the `objective:` stamp. Phase 0 measures
task FLOW per objective and shows the unattributed bill per provider — the
gap a future join must close. The rollup never fabricates attribution.

## Pending decisions — the 7 design-§4 answers gating phases 1-3

Defaults below are the design's recommendation, awaiting ratification
(one line each is enough to unblock calibration → enforce):

1. **Class-C default budget** — auto tiny ceiling ~$0.50 (or quota
   equivalent). B/A stay proposal-only.
2. **warn_fraction** — 0.7 (or 0.5 to match `model-cost.json`).
3. **Hard-stop behavior** — block dispatch + human gate (recommended), or
   auto-reassign to cheapest fit first.
4. **Judge chat exclusion** — absolute? (interactive chat stays outside
   objective budgets).
5. **Extension on exhaustion** — never automatic (recommended), or +50%
   once for class C.
6. **Variance factor** — >3x class estimate = human review; first breach =
   review, not punishment (anti-Goodhart).
7. **Currency rule** — gate in the assigned provider's currency;
   mixed-provider objectives keep per-provider quota sub-buckets, never
   summed.

## Where the deep docs live

- Design + cost estimate: `docs/obj28-budget-by-objective-design.md`
- Phase 0 review (evidence + decisions): `docs/obj28-phase0-review.md`
- Portal page: "Objetivos" (`objectives.html` in the obs portal)