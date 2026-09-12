# Incident 2026-09-12: dual-dispatcher war + watchdog burst

## Summary

An external `hermes kanban daemon --force` (PID 2692677, up 1d00h32m at
discovery) and the gateway-embedded kanban dispatcher (gateway PID 1380508)
claimed tasks under different locks simultaneously. Each side's reap loop
collected the other side's workers ~60s after spawn. Symptoms by 13:00 CEST:

- 368 `reaped N zombie worker(s)` lines in `gateway.log` since Aug 26
  (first: 2026-08-26 22:34). Dozens on 2026-09-12 alone.
- Task burst: 10 identical `Watchdog: audit automatica` tasks created
  13:06-13:17 (9 done, 1 blocked, this audit's parent t_66abe2b5 included)
  — every burst entry was a refill after the war blinked the board to
  0 running / 0 ready.
- 3 worker crashes 13:18 on t_33a26e4c / t_3de9e23f / t_f4c24cca within
  ~60s of spawn each (`gave_up`, `pid ... not alive`).
- The governor's concurrency guard (live=4 > hard=3) SIGTERMed worker
  PID 3842455 at 13:51:36 — killing a symptom while the war continued.
- 7 tasks blocked 13:00:49 with no `blocked` event and no recorded reason
  (t_fdbba03c, t_03352224, t_33a26e4c, t_b128cb7d, t_f4c24cca, t_3de9e23f,
  t_61fed817) — direct DB writes by an unknown author; several were
  auto-unblocked at 13:17:19 by the watchdog classifier sweep and
  re-crashed into gave_up within minutes (the war was still active).

## Root cause chain

1. An external kanban daemon had been running since ~2026-09-11 13:19
   (etime 1d00h32m at 13:51), spawned from the tick's respawn block with
   `--pidfile ~/.hermes/profiles/pr-ollama/quota-governor-daemon.pid`.
2. The gateway's embedded dispatcher kept claiming tasks (lock
   `crypto-space:1380508` = gateway PID); the external daemon claimed the
   same tasks under lock `crypto-space:2692677` = its own PID.
3. Both reap loops collected the other's children as zombies ~60s after
   spawn → mass worker deaths, board flickering to 0/0, watchdog refills,
   circuit-breaker gave_up blocks.

Pitfall 24g (hermes-quota-aware-dispatch skill) documented this exact
failure mode previously; the respawn block reintroduced the daemon.

## Fixes applied 2026-09-12 (t_66abe2b5)

1. **Respawn guard** in `scripts/quota-governor-tick.sh` (both deployed
   copies + repo): the tick's "start daemon" branch now checks
   `pgrep -f 'kanban daemon'` — if any kanban daemon process exists it
   logs `Dual-dispatcher guard: kanban daemon process detected — NOT
   respawning` and refuses to start another. The gateway-embedded
   dispatcher is the only claimer while the gateway lives.
2. **External daemon killed** (see Verification below). Its pidfile removed.
3. **Watchdog burst guard** in `~/.hermes/scripts/kanban-watchdog.sh`:
   (a) running/ready counts now come from the canonical
   `hermes kanban stats --json` counter instead of grepping `kanban list`
   output glyphs; (b) rate limit: at most 1 desert-refill successor per
   2h (stamp file `/tmp/wd-desert-stamp`), so a flicker can no longer
   mint a burst of identical audit tasks.
4. **Sync debt fixed**: the deployed tick copy carried an undeployed
   hotfix (Portal de observabilidad block, OBJ-27 F5d, from ~Sep 11) that
   the repo copy never contained (verified `git log -S PORTAL_OUTPUT` →
   empty). Repo now carries the block; `test_deployed_copies_sync.py`
   passes 7/7 after redeploy. Backup of the pre-sync repo copy:
   /tmp/tick-repo-bak.sh (transient).

## Verification

- Before the kill: `ps -p 2692677` alive; pidfile content matched.
- After: `pgrep -f 'kanban daemon'` returns nothing; the next governor
  tick (10m cadence) logs the dual-dispatcher guard line instead of
  respawning.
- Worker survival: successors spawned after the kill must live >5 min
  (the war's death window was ~60s). Watch `gateway.log` for new
  `reaped ... zombie` lines — the daily count should collapse to the
  single-digit background level.

## Open items (follow-ups, not done here)

- Classify the 7 no-event blocked tasks properly (several are genuine
  human-gated: t_03352224 OBJ-33 PAT scope, t_b128cb7d OBJ-40-RES).
- Identify the author of the 13:00:49 no-event blocks (direct DB writes
  bypass the event log — a watchdog-layer regression against 24g-c(3)).
- The watchdog v3 deploy + manual test burst (~12:57-13:17) was likely a
  same-day worker hotfix; its burst entries are this incident's symptom,
  not a separate bug.