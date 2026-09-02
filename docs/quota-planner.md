# Quota Planner — Concurrency Control (OBJ-06)

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