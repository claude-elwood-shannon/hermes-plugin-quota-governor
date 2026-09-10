# Quota Planner — Concurrency Control (OBJ-06) & Cost Calibration (OBJ-02)

## Real cost calibration (OBJ-02, 2026-09-03)

Measured from 132 task_runs (kanban.db) and 768 observations
(observations.jsonl) across the pr-ollama profile. Data collected
2026-08-26 through 2026-09-03.

### Per-task cost by category

| Category | n (wall) | Wall mean | Wall median | Wall min | Wall max | n (calls) | Calls mean | Calls median | Calls min | Calls max |
|----------|---------|-----------|-------------|----------|----------|-----------|------------|--------------|-----------|-----------|
| micro    | 4       | 203 s     | 168 s       | 51 s     | 427 s    | —         | —          | —            | —         | —         |
| tiny     | 29      | 290 s     | 242 s       | 31 s     | 831 s    | 18        | 19.9       | 19.0         | 2         | 45        |
| small    | 43      | 311 s     | 270 s       | 18 s     | 722 s    | 38        | 25.7       | 27.0         | 1         | 50        |
| medium   | 6       | 396 s     | 420 s       | 241 s    | 484 s    | 6         | 22.7       | 19.5         | 5         | 39        |
| complex  | —       | —         | —           | —        | —        | —         | —          | —            | —         | —         |

Key findings:

- **Wall-clock scales with cost category** as expected: micro≈3 min,
  tiny≈4 min, small≈4.5 min, medium≈7 min (medians).
- **API calls per task** are less differentiated than expected.
  tiny (median 19) and medium (median 19.5) are nearly identical.
  small (median 27) is the highest. This is because "tiny" verification
  tasks often involve multi-step evidence gathering, while some "small"
  tasks complete in a single LLM call (e.g. quick archiving).
- **No complex tasks** have been executed yet — no data for that
  category.
- **High variance** within each category (min 1, max 50 calls) reflects
  the wide range of work within a single cost bucket.

### Session and weekly capacity

| Metric              | Previous estimate | Real (median) | Deviation | Action     |
|---------------------|-------------------|---------------|-----------|------------|
| Calls to fill session | 137             | 510           | 272%      | ⚠️ Updated |
| Calls to fill weekly  | 769             | 2988          | 289%      | ⚠️ Updated |

The previous estimates (137 session, 769 weekly) were far too
optimistic. Real data from 12 reliable sessions (>20% range) shows
the session fills at ~510 calls (median), and the weekly cap fills at
~2988 calls (median, from 5 reliable deltas >5%).

**Per-call cost (updated):**
- ~510 calls fill an Ollama session to 100%
- ~2988 calls fill the weekly cap to 100%
- Per call: ~0.0020 session, ~0.00034 weekly

These figures are 3-4× larger than the previous estimates, meaning the
governor has more headroom than previously assumed. The `decide()`
heuristics don't need threshold changes (they operate on percentages,
not absolute counts), but the per-call cost comments in
`quota_planner.py` have been updated for documentation accuracy.

### Data sources and methodology

- **Wall-clock**: `task_runs` table in `~/.hermes/kanban.db`, filtered
  to `status='done'` with valid start/end timestamps. Cost tag parsed
  from task body (`cost:micro|tiny|small|medium`).
- **API calls per task**: `task_completed` events in
  `observations.jsonl` with cumulative `ollama_session_requests`.
  Per-task calls = delta between consecutive observations bracketing
  the `task_completed` event.
- **Session capacity**: Detected session boundaries (pct drop >10% or
  request counter reset), then computed `total_calls / pct_range` for
  sessions spanning >20% range (12 of 40 sessions qualified).
- **Weekly capacity**: Computed `delta_calls / delta_pct` for
  observation pairs where `delta_pct > 5%` (5 deltas qualified).
- **Token in/out data**: Not available. Ollama Cloud API does not
  return token counts in the response, and request dumps only capture
  error cases. Session/weekly percentages are the only token proxy.

---

## USD cost calibration (OBJ-02, 2026-09)

Extends the 2026-09-03 wall-time/call calibration above with REAL USD,
now that `model-cost-ledger.jsonl` (MULTI-PROV-09, t_5bdd7cfa) meters
per-model token consumption. Snapshot: 2026-09-06 13:45Z through
2026-09-07 04:40Z CEST, excluding this task's own analysis sessions —
52 ledger rows, $17.77 across 3 windows ($0.04 / $14.01 / $3.73; the
23:45Z window was still open at snapshot). The ledger live-updates every
20-min gate sync, so counts below are as-of-snapshot.

**Verdict up front: INCONCLUSIVE per category.** 27 of 52 ledger rows
are attributable to a kanban task run (10 runs / 9 tasks), but only 3
of those tasks carry a `cost:` tag (micro n=2 — one crashed — and
small n=1). tiny and medium have ZERO attributable rows in this
window. Single-point figures below
are upper bounds (ledger estimate is +16% vs console, see
model-cost-ledger.py docstring). Do NOT feed these into
`planner.decide()` thresholds until >=1 week of tagged data accrues.

### Methodology (join key)

The ledger has no task_id; it keys on `session_id`
(`<YYYYMMDD>_<HHMMSS>_<hash>`, local CEST) created when a kanban worker
session starts. Attribution = match session-start timestamp into a
`task_runs` window (same `profile`, `started_at-10s <= session_start <=
ended_at+120s`, nearest run wins). Rows with a non-empty ledger `task`
tag (title_generation / approval / background_review / compression) are
Hermes auxiliary calls, NOT worker work — counted separately. Worker rows
are the ones with empty `task`. Ledger is opencode-go-only by design:
runs whose worker used another provider (pr-nanogpt zai-org/glm-5.2,
ollama-cloud) are invisible here, and sessions that crashed before the
first 20-min sync contribute nothing (that is why the 3 crashed
`cost:medium` runs of t_d3854101 have no rows).

### USD per task by category (worker rows, upper bound)

| Category | n done (attrib.) | USD/task | calls/task | wall | model observed |
|----------|------------------|----------|------------|------|----------------|
| micro    | 1 (+1 crashed)   | $0.43 done; $0.09 crashed | 72; 5 | 18 min; 1 min | qwen3.8-flash; glm-5.2 |
| tiny     | 0                | —        | —          | —    | — (only t_3b401256 in flight at snapshot) |
| small    | 1                | $5.20 (outlier — multi-file feature + tests, ran on glm-5.2) | 159 | 20 min | glm-5.2 |
| medium   | 0                | —        | —          | —    | — (all 3 runs crashed pre-sync) |
| untagged (reference) | 6 | mean $1.00 / median $0.74 (min $0.19, max $2.90) | mean 75 | mean 25 min | mostly qwen3.8-flash |

Per-call rate by model (attributed worker rows, exact request counts):

| Model | USD | calls | USD/call |
|-------|------|-------|----------|
| glm-5.2 | $9.07 | 295 | $0.0307 |
| qwen3.8-flash | $2.66 | 394 | $0.0068 |

Ratio glm-5.2 / qwen3.8-flash = **4.6x per call**. The cleanest natural
experiment: t_fd3e1763 reclaimed attempt on glm-5.2 burned $2.57/85c;
its successful redo on qwen3.8-flash cost $0.33/64c — **7.8x** for the
same task.

Auxiliary Hermes calls per task-run (not attributable to one task):
title_generation median $0.0002 (negligible), approval mean $0.025,
compression $0.015, **background_review mean $0.41** — one review cycle
costs as much as an entire micro task and must be budgeted.

### Deviation vs planner.decide() / quota-gate heuristics (>20%)

`decide()` caps by CATEGORY from percentage headroom, and the Sep-3
calibration assumes category≈calls is model-agnostic. Real data breaks
that in three ways:

1. **Call counts mispredict USD within category (up to +489%).** small
   median expectation = 27 calls; the observed small task used 159
   (+489%). micro done used 72 calls vs the tiny median 19 the tier
   ladder implies (+279%). High-variance categories cannot be budgeted
   in calls; USD spread per task is $0.09-$5.20 (57x).
2. **Model mix is invisible to the tier heuristic (up to +350%).** 27
   calls costs $0.18 on qwen3.8-flash but $0.83 on glm-5.2 — same tier,
   4.6x apart. The PROFILE_WORKER_MODELS enforcement (G7 fix, same
   window) is the real cost control; tiering without pinning the worker
   model is not.
3. **Window budget pressure at current dispatch rate.** The 18:45Z
   window metered $14.01 ledger-estimated vs $12 budget (console
   confirmed 100.2% — the +16% upper bound). One glm-5.2 small task
   alone = 43% of a window; at the observed untagged mean ($1.00/task
   on the cheap model) ~12 tasks/window fits the $12 budget, but only
   ~2/window on the quality model. The gate's warn_fraction (50% of
   $12 per model) would have fired correctly: glm-5.2 hit $5.20+ in
   that window.

### Recommended follow-ups (not implemented here)

- Task creator MUST emit `cost:` tags: 6 of 9 attributable tasks were
  untagged — that is the data bottleneck, not row volume.
- Re-run this analysis after ~2 weeks; target >=5 done tasks per
  category per model, then publish median USD/task per
  (category, model) and let `bottleneck_to_max_cost` consume it.
- Recalibrate price triples in `model-cost.json` against console fully-
  closed windows (per ledger docstring), then drop the +16% caveat.

Evidence scripts + per-task JSON: kanban scratch workspace
`t_3b401256/` (attribute2.py, stats.py, per_task.json).

---

## Concurrency Control (OBJ-06)

## Problem

The daemon `--max N` flag limits **new spawns per tick**, but without a
hard concurrency limit, workers from previous ticks can accumulate.
With a 60-second tick interval and `--max 2`, the system could grow
concurrency by 2 every minute on a busy board, since `running` tasks
aren't reclaimed by completion alone — they sit in `status='running'`
until the worker calls `kanban_complete` or the dispatcher TTL-reclaims
them.

## Solution

Two layers of concurrency control:

### Layer 1: Core dispatcher (Hermes upstream)

The Hermes core `dispatch_once()` in `kanban_db.py` already treats
`--max N` as a **live concurrency cap**, not a per-tick spawn budget.
It counts tasks in `status='running'` against the limit before spawning
new workers. So `--max 4` means "at most 4 workers running at any time
across the whole board" (see `kanban_db.py` docstring at line 9947).

### Layer 2: Quota-governor tick script (this plugin)

The tick script (`quota-governor-tick.sh`) adds an independent
concurrency guard **before** daemon management:

1. **Live worker count** — `concurrency_guard.py` queries the kanban
   DB for `status='running'` tasks whose `worker_pid` is still alive
   (`kill(pid, 0)` succeeds). Tasks with `NULL` `worker_pid` but
   `running` status are also counted (the dispatcher may not have
   recorded the PID yet).

2. **Soft cap** — if `live_workers >= desired_max`, the tick skips
   daemon restart. The existing daemon (if running) already prevents
   new spawns via the core's concurrency cap. Workers drain naturally
   as they complete.

3. **Hard cap** — if `live_workers > hard_limit`, the oldest workers
   (by `started_at` ascending) are killed via SIGTERM. This prevents
   unbounded accumulation when workers are slow or stuck.

### Configuration

| Env var | Default | Description |
|---|---|---|
| `QUOTA_GOVERNOR_HARD_LIMIT` | `desired_max + 2` | Hard concurrency cap. Workers exceeding this are killed oldest-first. |

### Log output

The tick log (`~/.hermes/logs/quota-governor-tick.log`) records:

```
[2026-09-02 21:37:00] Concurrency: live=3 | desired=2 | hard=4 | spawn=skip(soft_cap)
[2026-09-02 21:37:00] [CONCURRENCY] killed: task=t_old_01 pid=12345 assignee=pr-ollama (hard cap exceeded)
[2026-09-02 21:37:00] Daemon started (PID 1234, --max 2) [live_workers=1]
```

### Verification scenario

**Criterion:** with 5 slow workers and `--max 2`, the system does not
exceed 5 workers simultaneously.

- `--max 2` means the core dispatcher allows at most 2 concurrent workers.
- The tick script's soft cap skips daemon restart when `live >= 2`.
- The hard cap (default `2 + 2 = 4`) kills the oldest worker if 5 are
  somehow alive (e.g. daemon was previously running with `--max 5`).
- The kill is logged to `~/.hermes/logs/quota-governor-tick.log` and
  printed to tick stdout as `[CONCURRENCY] killed: ...`.

### Files

| File | Role |
|---|---|
| `concurrency_guard.py` | Live worker counter, concurrency decision, kill logic |
| `scripts/quota-governor-tick.sh` | Tick script with concurrency guard section |
| `test_concurrency_guard.py` | Test suite (23 tests, all passing) |

### Test

```bash
cd <plugin-repo-checkout>
python3 test_concurrency_guard.py
```

Expected: 23 passed, 0 failed.