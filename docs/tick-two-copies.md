"""Docs: tick.sh two-copies rule — root cause of the deployed-copy drift.

The tick has THREE copies on this host:

  repo (source of truth):  REPO/scripts/quota-governor-tick.sh
  shared deployed:         ~/.hermes/scripts/quota-governor-tick.sh
  profile deployed:        ~/.hermes/profiles/pr-ollama/scripts/quota-governor-tick.sh

Which copy actually RUNS: the gateway's multiplex cron ticker ticks EACH
profile store inside `_profile_cron_scope(home)`, which sets the hermes-home
override for the block — so `_resolve_script_path` resolves
`script: quota-governor-tick.sh` against <profile>/scripts/ FIRST (verified
2026-09-10 by replicating the override + join with hermes_constants). The
shared ~/.hermes/scripts/ copy is the default-home fallback; with an active
profile store both exist and BOTH are candidates, so keep them identical.

This is what kept the argv bug alive in production after the repo fix:
deploying only ~/.hermes/scripts/ left the profile copy stale
(2026-09-02 -> 2026-09-10: deployed md5 e8858c26 carried the
CONCURRENCY_DESIRED_MAX argv bug; the 19:13 tick still logged
desired=1 hard=3 after the shared copy was fixed).

RULES:
1. Edit the REPO copy only (it is versioned and published to GitHub).
2. After commit, deploy to BOTH deployed copies:
     cp <repo>/scripts/quota-governor-tick.sh ~/.hermes/scripts/quota-governor-tick.sh
     cp <repo>/scripts/quota-governor-tick.sh \
        ~/.hermes/profiles/pr-ollama/scripts/quota-governor-tick.sh
   (preserves 0775 exec bits via cp).
3. Verify all three md5sums match; bash -n each deployed copy.
4. No gateway restart needed: cron launches the script fresh every tick.
   The fix applies on the NEXT tick (~15 min cadence).

The PLUGIN_DIR default inside tick.sh now resolves from BASH_SOURCE, so any
deployed copy automatically keeps pointing at the repo's concurrency_guard /
health_checks / tick-observation modules — repo remains the single source
of truth for the python blocks too.

Operational override (hardening commit, 2026-09-10): pin
QUOTA_GOVERNOR_HARD_LIMIT in the profile .env to clamp the guard's hard cap
independently of desired_max; tick.sh exports it explicitly so it reaches
concurrency_guard.check_concurrency().

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
