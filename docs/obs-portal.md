# Obs Portal — OBJ-27 F5: the observability hyperespace

One stable URL that answers, in one place: what the house spent (counted,
real vs estimated), the board beating (30d), the future projected (burn
forecast), the past reconstructed (backfill) and the alarms awake.

> "Toma esta URL y entra en el hiperespacio de la observabilidad de
> nuestra casa." — http://localhost:8917

## The three pieces

```bash
# F5b — backfill: give the trace its past (idempotent, cursor-guarded)
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/trace-backfill.py
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/trace-backfill.py --dry-run

# F5c — portal: 7 static dark pages (works opened as a file too)
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/portal-build.py
#   -> <home>/quota-governor/obs/portal/{index,consumo,board,providers,alarms,docs}.html

# F5d — server: the stable URL, regenerating every 5 min + on trace change
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-serve.py           # http://localhost:8917
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-serve.py --headless  # adoptants: static, no server
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-serve.py --check   # supervisor probe (exit 0 alive / 1 dead)
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-serve.py --stop
```

`obs-serve.py` binds 127.0.0.1 ONLY (privacy: the hiperespacio is of the
house and in the house). Stdlib only, zero dependencies, zero tokens.

## Pages

| page      | answers                                                          |
|-----------|------------------------------------------------------------------|
| index     | KPIs (spend real/est, unattributed gap, supply_ratio, balance, done 24h, alarms), 30d spend chart, 30d board bars |
| consumo   | spend by objective/class/model/provider, each with drill-down to individual requests; search by requestId/objective/date |
| board     | created/closed/crashed 30d, active tasks with their kanban log, supply_ratio history |
| providers | window state + burn verdict (gate rule), provider % history sparklines, NanoGPT balance, closed-windows ledger |
| alarms    | trace watchdog alarms, F2 alert state, trace health (doctor), crash loops with links to the kanban log |
| docs      | living docs: trace schema (from the code itself) + the repo's obs docs |

## The backfill (why the trace has a past)

The F0 collectors only append what they see from their first run; the
backfill rebuilds history from the EXISTING sources, importing them (never
re-implementing):

- `usage-audit`: rows older than the F0 cursor (same shape, same prices).
- `model-cost-ledger`: opencode-go per-model deltas (the F0 collectors do
  not read state.db); `pr-ollama` rows are SKIPPED (already traced as real
  nanogpt-requests costUsd — no double count).
- `task-events`: created/crashed/gave_up/timed_out/spawn_failed (F0 only
  collects claimed/completed), objective-joined like F0.

Idempotence is two-layered: a per-source high-water cursor
(`obs/backfill-cursor.json`) plus natural-key dedup against the ACTIVE
trace (source, consumer_id, cause, ts) — rotation or a lost cursor can
never duplicate history. The F0 cursor (`trace-cursor.json`) is SACRED:
never read or written — except one seed on fresh adoptants (when the F0
cursor lacks a source key, the backfill's watermark seeds it so the F0
collector never re-ingests the same past).

## Wiring (cron / tick)

- `quota-governor-tick.sh` step "Portal": runs `obs/obs-serve-cron.sh`
  every tick (15 min). Silent when the URL is alive; respawns it (with one
  line of evidence) when it died. Same daemon pattern as the kanban
  daemon: PID file, idempotent (never double-binds), graceful SIGTERM.
- `trace-backfill-cron.sh`: cron every 15m, silent when nothing new.
- morning-report prompt: closes with the literal URL line (F5d).

## Sources (read-only, never modified)

trace.jsonl · forecast.json · metrics-history.jsonl · kanban.db
(task_events, task_runs, tasks) · weekly-reset-ledger.jsonl ·
model-cost-ledger.jsonl · usage_audit.jsonl

## House rules honored

- the gap is shown, not hidden (unattributed in red; estimated cost
  labeled `est` — model-cost-ledger rows are a conservative UPPER bound,
  never presented as billing truth; real balance cost is `real`).
- single source of truth: readers/thresholds/verdicts IMPORTED from
  morning-screen + obs-dashboard; alarms from trace-alarms; schema/prices
  from trace.py.
- watchdog: empty sources render elegant empty states, never fake zeros.
- no_agent: pure function of existing files, zero tokens.

## Tests

`scripts/obs/test_obj27_f5.py` — 23 tests (backfill idempotence, portal
pages, server bind/serve/check, portability). Run:

```bash
cd scripts/obs && /usr/bin/python3.12 test_obj27_f5.py
```
