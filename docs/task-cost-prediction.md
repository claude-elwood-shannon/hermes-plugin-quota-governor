# Per-task cost prediction — OBJ-35 (P1–P3 shipped, P4 specced)

*From reactive budgets (topes) to predictive budgets (estimates BEFORE
execution). The dream this serves, in the owner's words: "you ask for an
objective and the house answers with cost and date before spending the
first cent."*

## What shipped (2026-09-10)

| Phase | What | Files | Status |
|---|---|---|---|
| P1 observer | One training row per closed task: (objective, cost_class, clase, model, provider, tokens, real costUsd estimate, duration, crashes) | `scripts/obs/task-cost-train.py`, cron `task-cost-train` (15m) | live, 270 rows day one |
| P2 estimator | Median + p90 per (objective, cost_class, model) group — descriptive statistics, no ML — with a 3-stage fallback chain | `scripts/obs/task-cost-estimator.py` | live, first table emitted |
| P3 accuracy | Prediction-vs-real error measured per task (MAPE, in-band coverage, small-class p50 goal <30%) | `scripts/obs/task-cost-backtest.py`, cron `task-cost-backtest` (15m), `scripts/obs/task-cost-seal.py` | live; goal checkable after ~1 week of sealed predictions |
| P4 objective-level | Sum of task estimates + variance => "OBJ-x will cost X (±Y) and finish on day Z" | — | specced (below), needs accumulated history |

## Ledger

`~/.hermes/quota-governor/task-cost-train.jsonl` — append-only, one JSON
per line, three kinds:

```
{"kind":"task",     "task_id":"t_...", "objective":"OBJ-27",
 "cost_class":"small", "clase":"B", "model":"deepseek-v4-flash",
 "billing_provider":"ollama-cloud", "tokens_in":..., "tokens_out":...,
 "cache_read":..., "costUsd":0.0319, "duration_s":1800, "crashes":1,
 "attributed":true, "shared_session":false, "estimate":{...}|null, ...}
{"kind":"estimate", "task_id":"t_...", "ts":..., "estimate":{"p50":...,
 "p90":...,"stage":"...","model":"...","n":...}}          <- P3 seal
{"kind":"est-day",  "day":"2026-09-10", "n":..., "mape_pct":...,
 "in_band_pct":..., "ok_small":true|false}                 <- P3 verdict
```

### Training table (CSV) — the predictor's input

`export_csv()` writes the flat training table next to the trace at
`~/.hermes/profiles/<profile>/quota-governor/obs/task-cost-train.csv`
(one row per `kind=task` line, nested `models` list dropped — the
dominant model is the `model` column). Columns:

```
task_id, objective, cost_class, clase, assignee, resultado,
model, billing_provider, tokens_in, tokens_out, cache_read, reasoning,
costUsd, cost_source, duration_s, runs, crashes, attributed,
shared_session, session_tasks, created_at, completed_at
```

`resultado` is the task's terminal status (`done`/`archived`/...) from
kanban.db; legacy rows recorded before the field existed carry an empty
cell (append-only ledger — only new rows are stamped).

### Integrity check

`integrity_check()` verifies the ledger has no drift: the set of
`task_id`s in the ledger must equal the set of closed tasks WITH a body
in kanban.db (the observer skips no-body tasks by design). Returns
`{ledger_rows, closed_tasks, missing, extra, ok}`. Live check on
2026-09-10: 279 == 279, `ok:true`.

## Attribution path (the part that makes it real)

The F0 trace carries per-request cost only for cron-llm rows; worker
sessions bill at the window level, so per-task cost needs a join:

```
kanban.db tasks.session_id
  -> profiles/<profile>/state.db session_model_usage (task IS NULL)
  -> aggregate tokens by (model, billing_provider)
  -> costUsd = model-cost-ledger.estimate_cost (catalog, cache-read
     priced, deepseek peak-hours x2)
```

Verified live on 2026-09-10 (t_dd2eb1bb: 30 min, 57k in / 17k out,
1.19M cache-read, deepseek-v4-flash => catalog USD on the row).

## Honest limitations (gap shown, not hidden)

1. **costUsd is a catalog estimate, not a bill.** Same convention as
   every window-billed ledger in the house (known +16% bias on glm-5.2
   family; conservative upper bound — the right direction for
   budgeting).
2. **Shared sessions overstate per-task cost** (several tasks on one
   session_id). Rows carry `shared_session=true` and `session_tasks=n`;
   consumers filter or split; we never fabricate a pro-rata split.
3. **Legacy tasks (no session_id) stay unattributed** — 233 of the 270
   day-one rows (board history predates session stamping). They are
   recorded with `attributed=false, costUsd=null`; every NEW task
   carries session_id, so coverage grows with the board itself.
4. **Housekeeping usage excluded** — title_generation / approval /
   compression rows (task NOT NULL) never join to task cost.

## The P3 loop (prediction precedes outcome)

`task-cost-seal.py --task-id t_x --body <file>` appends a `kind=estimate`
line any time after creation and before closing. `task-cost-train.py`
attaches it to the closed task's row (`estimate` field), and
`task-cost-backtest.py` measures:

```
error_pct = |real - p50| / real * 100        in_band = real <= p90
goal: p50 error < 30% on SMALL tasks (n>=3)  -> ok_small
```

Until seal-capture is wired into task creation, P3 verdicts only fire
for sealed tasks; the harness is live and produces `n=0` verdicts (no
fabricated numbers).

## P4 spec (next phase, needs history)

For an objective O with n closed attributed tasks and k open children:
`E[cost(O)] = sum(p50 of open children) ; Var via group p90-p50 spread ;
finish date from measured throughput (completed/week by clase)`.
Deliverable: `objective-cost-forecast.py` printing one line per active
objective — the literal budget-dream answer — gated on P3 showing
ok_small=true for a full week.

## Cron registration (both no_agent, 15m, silent-on-normal)

```
task-cost-train    scripts/task-cost-train-cron.sh    -> obs/task-cost-train.py
task-cost-backtest scripts/task-cost-backtest-cron.sh -> obs/task-cost-backtest.py
```

Wrappers point at the repo copy (single source of truth,
budget-check-cron.sh pattern). First registration allocated ids
`1328b206a25b` / `c171e5d0a8a2` in the profile scheduler.

## Tests

`scripts/obs/test_task_cost_train.py` (19) + `scripts/obs/
test_task_cost_backtest.py` (7) — hermetic tmp homes, tag-parsing both
board layouts, join correctness, peak doubling, idempotency, shared
session flag, fallback chain, verdict math, CSV export, integrity check,
tolerant degradation.
`/usr/bin/python3.12`; all green; no regressions in test_trace,
test_morning_screen, test_budget_check, test_backtest_f2.
