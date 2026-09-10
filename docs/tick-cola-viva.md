# tick-cola-viva.py — the constitution of flight, in code

**OBJ-30b-IMPL (t_31b9c1d2)** · links to the constitution of flight
**t_cfe8060b** (ratified 3x by the user).

The constitution says: *with free quota the house does NOT stop.* This
script mechanizes that clause inside the governor tick. It is the
**observer layer** of the anti-stall mechanism — it only CREATES/ASSIGNS
work, never kills workers, never stops the daemon, never touches triage
clase A/B (user decisions), never fabricates data.

## Why it exists

On the night of 2026-09-10 the doctrine without a mechanism did not fly:
the queue sat empty ~1h with the session at 2.6% free quota. The board was
idle, quota was free, and nothing refilled the queue. This script closes
that gap.

## Where it runs

Inside `quota-governor-tick.sh` (the 15-min no_agent cron tick), right
after the concurrency guard. Zero tokens. It is NOT a new cron job.

## The cascade (max 1 action per tick, idempotent)

All gates must hold before any action:

- **live_workers == 0** (board idle)
- **session_pct < 80 AND weekly_pct < 80** (free quota)
- **no STOP signal** file

Then, in order:

1. **Assign a profile** to the first `ready` task with no assignee — the
   fix for the silent stop: a ready task without an assignee is claimed by
   nobody, so it sits forever.
2. **Queue alive** — if `ready` tasks WITH an assignee exist, the
   dispatcher claims them within ~60s; log and skip, no new work.
3. **Structural class-C successor** — create ONE successor of the most
   recent `done` task (<24h, body carries `clase:C`) that has no open
   successor yet. Pattern: *docs of the undocumented, test of the new,
   hardening of the fragile*. assignee pr-ollama, cost tiny/small.
4. **Cola seca legitima** — nothing legitimate exists; log it and create
   nothing. **Golden rule: never filler.**

## Idempotency

A parent with an open (non-archived) successor referencing it is skipped,
so the same successor is never created twice. The ledger
(`~/.hermes/quota-governor/cola-viva.jsonl`) records every decision.

## Observability

One line per decision on the tick's stdout (which the cron layer logs):
`cola viva: <action>` or `cola seca: <reason>`. The tick log
(`~/.hermes/logs/quota-governor-tick.log`) carries the same line prefixed
`Cola viva:`.

## Integration with supply_ratio (OBJ-29)

`supply_ratio < 1.0` sustained + cola seca = alarm case F2 (out of scope
here). This script is the supply-side refill; the F2 alarm is a separate
consumer of the metrics.

## Usage

```bash
# dry-run (default): print decisions, no mutation
python3 tick-cola-viva.py --session-pct 2.6 --weekly-pct 54.7 --live-workers 0

# execute (the tick calls this when ACTION == run)
python3 tick-cola-viva.py --session-pct 2.6 --weekly-pct 54.7 --live-workers 0 --execute
```

Exit code 0 always — the tick must never break on this.
