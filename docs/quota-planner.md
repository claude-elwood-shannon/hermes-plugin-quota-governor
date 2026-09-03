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
cd REPO
python3 test_concurrency_guard.py
```

Expected: 23 passed, 0 failed.