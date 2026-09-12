# OBJ-28 Phase 0 — review report and the decisions that unblock phases 1-3

**Date**: 2026-09-12 · **Task**: t_d540cee7 · **Branch**: `feat/obj28-budget-2026-09` (679866b + dd2e79c)

## What phase 0 delivers (all verified live)

| Piece | Evidence |
|---|---|
| Rollup `scripts/obs/objective-budgets.py` | pure read-only function over the OBJ-27 F0 trace → `quota-governor/objective-budgets.json` (atomic write, idempotent, fails open) |
| Tests | `test_objective_budgets.py` 20/20; full obs family green (trace 21, F5 23, morning-screen, dashboard, alarms, retention, backtest) |
| Cron | `objective-budgets` job 3544748acd22, hourly no_agent, 168 fires (the 7d observer window); manual trigger returned `last_status: ok` |
| Portal | 7th page "Objetivos" at `http://localhost:8917/objectives.html` (HTTP 200, KPIs render) |
| Live state at birth | 44 objectives rolled up; 647 tagged vs 795 untagged task events; **$36.8911 / 30d in the explicit `unattributed_cost` bucket** |

## The honest limitation (shown, not hidden)

Every cost-bearing trace source (nanogpt-requests, usage-audit,
model-cost-ledger) carries **no task_id**, so per-objective `spent_usd` is
0.0 everywhere until sources learn the `objective:` stamp. Phase 0
therefore measures task FLOW per objective and shows the unattributed bill
per provider — the gap a future join must close. The rollup never
fabricates attribution.

## The 7 design-§4 decisions that gate phases 1-3

Answer these (one line each is enough) and calibration/enforce tasks can
be created. Defaults below are the design's recommendation, awaiting
ratification.

1. **Class-C default budget** — auto tiny ceiling ~$0.50 (or quota equivalent). B/A stay proposal-only.
2. **warn_fraction** — 0.7 (or 0.5 to match provider-level `model-cost.json`).
3. **Hard-stop behavior** — (a) block dispatch + human gate (recommended), or (b) auto-reassign to cheapest fit first.
4. **Judge chat exclusion** — absolute? (interactive chat stays outside objective budgets).
5. **Extension on exhaustion** — never automatic (recommended), or +50% once for class C.
6. **Variance factor** — >3x class estimate = human review, first breach = review not punishment.
7. **Currency rule** — gate asks in the assigned provider's currency; mixed-provider objectives keep per-provider quota sub-buckets, never summed.

## Known follow-ups (not done here, by design)

- Cost→objective join: teach the three cost sources to carry task_id/objective (phase 1 calibration substrate).
- Morning report + obs-dashboard surfacing of the rollup (portal page exists; the 8am digest does not mention it yet).
- First commit (679866b) lacks the `Co-authored-by:` trailer — amending would have required a force-push over published history, declined per house rule (Sep-7 incident). Later commits carry it.
