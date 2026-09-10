# OBJ-06 Closure Audit — concurrency guard (hard cap on live workers)

Date: 2026-09-08 · Auditor: pr-opencode (task t_7c51d12a)
Verdict: **OBJ-06 is COMPLETE.** Two residual caveats are accounting/test-coverage
limitations, not missing work (documented in §5).

## 1. What the objective required

Per `docs/autonomous-objectives.md` OBJ-06: with several slow workers and `--max 2`,
the system must never exceed the hard cap, and every kill must be logged.

## 2. Code evidence (plugin repo)

- Commit `7495a05` ("feat: OBJ-06 concurrency guard — live worker count, soft/hard
  caps for tick script", 2026-09-02) adds:
  - `concurrency_guard.py` (new module): live worker count from kanban.db claim
    locks; `soft_limit` (`--max`, skip spawn) and
    hard cap (`live > hard_limit`, default `desired_max+2` → SIGTERM oldest-first).
  - `scripts/quota-governor-tick.sh` (+100 lines): guard wired before daemon
    management; kill notice logged via `log "$KILL_NOTICE"` (tick.sh L255–257).
  - `test_concurrency_guard.py` (424 lines): Test 4 asserts hard cap
    "5 live > hard 3 → kill 2 oldest" and that the newest workers are NOT
    selected; Test 5 env override; Test 6 default hard limit `3+2`; Test 7
    `kill_worker` SIGTERM returns True. Re-run at audit time:
    `python3 -m unittest test_concurrency_guard` → **23 passed, 0 failed**.
- Deployed copy matches repo: `md5sum` of `scripts/quota-governor-tick.sh` and
  the deployed `~/.hermes/scripts/quota-governor-tick.sh` both
  `e8858c2656ccd46795ae05664e354344` at audit time (deploy done by t_ae7106cd on
  2026-09-08; the guard resolves `concurrency_guard.py` from the plugin dir,
  tick.sh L197).

## 3. Production evidence (`~/.hermes/logs/quota-governor-tick.log`)

Guard active in the cron tick (job 660d1cf8f994, 15 min) since deploy; every tick
logs its decision — 6 `Concurrency:` lines at audit time, e.g.:

```
[2026-09-08 06:49:10] Concurrency: live=2 | desired=1 | hard=3 | spawn=skip(soft_cap)
[2026-09-08 06:54:46] Concurrency: live=1 | desired=1 | hard=3 | spawn=skip(soft_cap)
[2026-09-08 07:09:49] Concurrency: live=0 | desired=1 | hard=3 | spawn=ok
[2026-09-08 07:39:57] Concurrency: live=1 | desired=1 | hard=3 | spawn=skip(soft_cap)
```

Live count never exceeded the hard limit (max observed 2 ≤ 3). `ps` at audit time:
exactly one daemon (PID 2784637, `--max 2`) and no orphaned workers. Soft cap
correctly skipped daemon restart (daemon elapsed continuous across ticks, per
t_ae7106cd run 431).

## 4. The two "lost" tasks (objective-progress.json `tasks_lost: 2`)

Investigated in t_9f687fc3 (done, run 430) with card-thread comment as record:

- `t_0c59b30b`: spawn_failed → gave_up ×2 from invalid task config
  (`workspace_kind=worktree` without `workspace_path`); archived 2026-08-31
  without `completed_at`. Predates the guard (commit 7495a05 is 2026-09-02).
- `t_94141677`: did run (run 18) and its conclusion is preserved — it proved the
  hypothesized bug did not exist (Hermes core already treats `--max` as a live
  concurrency cap). Its run was orphaned `running` and the card archived without
  `completed_at` → counted as lost.

Neither loss was caused by the guard; both predate it. They are bookkeeping
artifacts of the 2026-08-31 manual cleanup.

## 5. Residual caveats (explicit, so the verdict is not oversold)

1. **No real kill observed in production.** Hard-cap SIGTERM firing is proven by
   unit tests (kill_worker=True) and an isolated e2e with real PIDs where kills
   were reaped-verified (t_9f687fc3: "hard cap SIGTERM oldest-first really kills
   (verified with wait/reap)"), and the kill-to-log path exists at
   tick.sh L255–257 — but production load has never exceeded the hard limit, so
   no `Concurrency: ... kill` log line exists in the real tick log yet.
2. **`objective-progress.json` will keep OBJ-06 at `needs_attention`.**
   weekly-progress.py marks any objective lost when `tasks_lost > 0`, and the two
   archived-without-`completed_at` rows are permanent. This is a tracker design
   limit (noted in t_9f687fc3), not a regression of the work.

## 6. Conclusion

Implementation, tests, deployment, and production observation all hold: OBJ-06
is **closed**. Task chain: t_8511285d (impl) → t_42c5667a → t_e33174a7 (found
stale deploy gap) → t_9f687fc3 (root-caused losses, e2e proof) → t_ae7106cd
(deploy + 2 consecutive cron ticks with `Concurrency:` lines) → this audit.
