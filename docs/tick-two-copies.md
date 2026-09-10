"""Docs: tick.sh two-copies rule — root cause of the deployed-copy drift.

The tick has TWO copies on this host:

  repo (source of truth):  <repo>/scripts/quota-governor-tick.sh
  deployed (cron runs this): ~/.hermes/scripts/quota-governor-tick.sh

Cron job quota-governor-tick (660d1cf8f994, no_agent, 15 min) executes the
DEPLOYED copy by filename (`script: quota-governor-tick.sh`), which resolves
to ~/.hermes/scripts/. The repo copy is NOT auto-synced; if you edit only
the repo copy, production never sees the change (this happened 2026-09-02
-> 2026-09-10: deployed md5 e8858c26 = HEAD~N without the tick-observation
block AND carrying the CONCURRENCY_DESIRED_MAX argv bug).

RULES:
1. Edit the REPO copy only (it is versioned and published to GitHub).
2. After commit, deploy:  cp <repo>/scripts/quota-governor-tick.sh \
      ~/.hermes/scripts/quota-governor-tick.sh
   (preserves 0775 exec bits via cp).
3. Verify both md5sums match; bash -n the deployed copy.
4. No gateway restart needed: cron launches the script fresh every tick.
   The fix applies on the NEXT tick (~15 min cadence).

The PLUGIN_DIR default inside tick.sh now resolves from BASH_SOURCE, so a
deployed copy automatically keeps pointing at the repo's concurrency_guard /
health_checks / tick-observation modules — repo remains the single source
of truth for the python blocks too.

Design note (task body item 3, from t_139854c3 diagnosis):
- The guard's hard cap kills the OLDEST workers with no age/grace floor;
  workers with no recorded worker_pid count as live and (age 0) are culled
  first. That is intentional (un-ageable workers are the suspicious ones),
  but it also means the guard is the last line of defense, not the
  mechanism: the gateway dispatcher is NOT bounded by the daemon's
  `kanban daemon --max N` flag and can over-spawn bursts of 4-6 workers
  while quota is healthy. If kills recur with correct env passing, the
  next lever is a dispatcher-side spawn cap, not a higher hard limit.
"""
