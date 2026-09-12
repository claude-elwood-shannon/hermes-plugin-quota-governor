# OBJ-29 — Perpetual objective supply (t_99e3b849)

**Date**: 2026-09-12 · **Status**: mechanisms live and verified; acceptance window (7 days at ratio >= 1.0) starts now.

## What this objective is

The user's fear (10-sep, 02:30): "no me voy sin la certeza de que no faltarán
objetivos que cumplir". OBJ-29 makes objective supply a SYSTEM, not luck:
three layers (extraction, mining, frontier) with an audited metric, so the
board can rest legitimately — but only with evidence, never by neglect.

## The three layers, as they exist today (verified this run)

### Layer 1 — EXTRACTION (audit) — DONE this run

`scripts/quota-metrics.py` appends `supply_created_24h`, `supply_closed_24h`,
`supply_ratio` to `quota-governor/metrics-history.jsonl` every 15 min
(no_agent, zero tokens). Sustained ratio < 1.0 with free quota = supply
deficit; alarm consumer is OBJ-27 F2.

**Incident found and fixed while verifying (this is why the audit matters):**
the collector had produced ZERO rows since 2026-09-10 15:15 UTC. Root cause:
the cron env sets `HERMES_HOME` to the profile home
(`~/.hermes/profiles/pr-ollama`) while the board lives at the root
(`~/.hermes/kanban.db`); the script resolved the board as
`$HERMES_HOME/kanban.db` → nonexistent → "board ilegible" → silent skip.
The same wrong resolution made the cola-viva cascade and fondo-queue-watch
board-blind: their step-4 drought verdict ("cola seca legitima") was being
issued from a NULL board. Fixed in `ffcc374` (shared-root resolution,
mirroring `health_checks._get_hermes_root`); verified under the exact cron
env: rows append again, and the cascade now sees ready work it was blind to.

### Layer 2 — MINING (structural successors) — DONE (OBJ-39 + OBJ-30b)

Every closure engenders its small successor, by two mechanisms wired into
the tick cascade:

- `scripts/tick_body_parts.py` (OBJ-39): a closed task whose body declares
  numbered parts (R1/R2/…, Fase N, Paso N) with parts lacking affirmative
  evidence spawns ONE successor carrying the pending parts verbatim. Class
  C parents auto-create; class A/B get an audit comment (user decisions).
  Escape hatch: `[done-verify-skip] R<N>: <why>` comment waives a part.
- `scripts/tick-cola-viva.py` step 3: ONE structural class-C successor
  (test/doc/hardening) of the most recent done task (<24h) with no open
  successor. Idempotent via open-successor check.

### Layer 3 — FRONTIER (user objectives) — formalized, not automatable

Class A/B objectives stay user decisions by design. The extraction half is
the OBJ-28 phase plan: every approved phase-plan task carries its next
phases in the body; when phase n closes, `tick_body_parts` detects the
undeclared-pending pattern and promotes/queues the next with the same
budget rules. OBJ-28 phase 0 (t_d540cee7) landed the costed-phase
substrate; phases 1-3 await the user's 7 §4 decisions
(`docs/obj28-phase0-review.md`).

## The golden rule (anti-Goodhart)

Perpetual supply does NOT mean filler. The board may rest only when
(a) quota is exhausted, (b) the human gate is active, or (c) supply is dry
AND the week's supply_ratio is audited. Never tasks to "show movement".
Enforced by `fondo-queue-watch.py` step 4 (STOP + supply_ratio annotation)
and the `tick-cola-viva.py` drought verdict — which, after the shared-root
fix, now reads a REAL board instead of a null one.

## Weekly supply audit (rolling, from task_events)

| day | created | closed | ratio |
|---|---|---|---|
| 2026-09-12 (partial) | 6 | 5 | 1.2 |
| 2026-09-11 | 9 | 10 | 0.9 |
| 2026-09-10 | 42 | 35 | 1.2 |
| 2026-09-09 | 21 | 21 | 1.0 |
| 2026-09-08 | 61 | 55 | 1.109 |
| 2026-09-07 | 104 | 49 | 2.122 |

Deficit days since the mechanism landed: 1 (09-11, within noise while the
board drained a backlog wave). No filler suspected: every closure has an
objective tag or a successor chain.

## Cron plumbing incident (same run, upstream hermes-agent)

`autonomous-task-creator` (the layer-1 signal-to-task extractor) had failed
68 consecutive fires since Sep 9 with `ValueError: too many values to
unpack (expected 2)` — upstream commit `7e4d02fef5` removed the model-drift
guard from `_resolve_job_runtime` but left the happy path returning a
3-tuple against a 2-value unpack at `cron/scheduler.py:2157`. Every
agent-path cron job died before its model resolved (morning-report F1 too).
Fixed in `3919d5b002` (2-tuple happy path), pushed to the fork as
`fix/cron-2tuple-unpack`; verified live: creator fired 11:02 ok
(streak 68 → 0).

## Acceptance status

- [x] supply_ratio in metrics-history (zero tokens, cron existing)
- [x] Structural successor rule in verify/cola-viva (proposes, only class C auto)
- [x] Phase-next extraction for objectives with approved phase plans (OBJ-39 + OBJ-28 substrate)
- [x] This doc (EN, publishable)
- [ ] 7 consecutive days with supply_ratio >= 1.0 without filler — **window starts 2026-09-12**; verified again by the weekly-progress cron or the next OBJ-29 audit task.

## Files

- `scripts/quota-metrics.py` (layer-1 collector + shared-root fix)
- `scripts/tick_body_parts.py`, `scripts/tick-cola-viva.py`,
  `scripts/fondo-queue-watch.py` (layers 2/3 + shared-root fix)
- `scripts/obs/morning-screen.py` (F1 morning report shows the daily ratio)
- Upstream: `hermes-agent` fork branch `fix/cron-2tuple-unpack` (3919d5b002)
