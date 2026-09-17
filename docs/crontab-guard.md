# Crontab Deploy Guard — fuse-vs-REPLACE + anti-stall canary (t_da32e7f9)

> **Document:** Protection against destructive `crontab` deploys: merge-based
> install, pre-deploy snapshots, and a post-deploy canary that alarms and
> auto-restores when the user-job count drops.
> **Status:** Implemented and verified live (Sep 2026)
> **Priority:** P2 — hardening after the 2026-09-15 crontab outage
> **Created:** 2026-09-17
> **Code conventions:** [coding-standards.md](coding-standards.md)

---

## Incident this guard exists for

2026-09-15 01:25:05 CEST — an ad-hoc observability deploy (obs-shipper, work
under t_a9319828) installed a single-job file with `crontab <file>` — a
REPLACE that dropped 9 of 10 user jobs (`crontab[2718674] (iinstances)
REPLACE (iinstances)` in syslog). kanban-watchdog, obs-serve,
cron-health-check, efficiency-ratio and open-webui-bridge all died together
for 32 minutes; the efficiency ratio stalled 105 min. Manual REPLACEs at
02:11–02:13 restored the crontab. The deploy step was never persisted as a
script, so there was nothing to patch — the guard is the standing protection
instead (task t_da32e7f9, from audit t_1a895159 finding 1, CRITICAL).

## Components

- `scripts/crontab-guard.sh` — the whole guard. Subcommands:
  - `snapshot` — save `$HOME/.hermes/backups/crontab/<ts>.crontab` and pin it
    as the pre-deploy baseline (`pre-deploy.crontab` + `pre-deploy.count`).
  - `install <file>` — MERGE deploy: snapshot first, then install the current
    crontab plus any MISSING active lines from `<file>`. Existing lines win,
    order preserved, no duplicates. Verifies every line of `<file>` landed;
    alarms `INSTALL_FAILED` / `INSTALL_INCOMPLETE` and exits non-zero on
    failure (crontab untouched in the failed-write case).
  - `canary` — compare current active-job count against the baseline
    (fallback: newest timestamped backup; bootstrap: take the first snapshot).
    On a strict decrease: alarm `JOBS_LOST` to
    `~/.hermes/logs/cron-alarms.jsonl` (same file the cron-health-check uses,
    consumed by the morning screen) and RESTORE = baseline UNION every active
    line that survived in the current crontab. Alarm `RESTORED` on success,
    `RESTORE_FAILED` otherwise.
- `scripts/quota-governor-tick.sh` — calls `crontab-guard.sh canary` at the
  end of every tick (fail-open, never breaks the tick).
- `tests/test_crontab_guard.py` — 7 sandbox tests (fake `crontab` binary +
  redirected dirs; never touches the host crontab) covering the acceptance
  criteria of t_da32e7f9.

## Design decisions

1. **The canary lives OUTSIDE the user crontab.** It runs from the
   quota-governor tick (Hermes cron ticker, profile `jobs.json`), which kept
   running through the incident — every crontab job was gone. A tripwire must
   not share fate with what it guards.
2. **Restore is a UNION, not a blind restore.** A surviving line after a
   destructive deploy is the deployer's own addition; the 2026-09-15 manual
   recovery restored the old jobs AND kept the new shipper line. A blind
   baseline restore would re-delete the deploy target.
3. **Only strict decreases trigger.** Additions never alarm; the restore can
   never fight a legitimate deploy that only adds jobs.
4. **Baseline pinned until the next `snapshot`/`install`.** Healthy canary
   runs refresh only the forensic timestamped snapshots, so job-count
   oscillation cannot re-arm the baseline above the true state.
5. **DIRECCION-STOP respected:** with `STOP` present the canary alarms but
   does NOT restore (verification-only mode; use it when a job was removed
   intentionally).
6. **Fail-open everywhere:** any internal failure exits non-zero without
   touching the crontab; the tick logs and moves on.
7. **The real deploy discipline for humans/agents** (what to run instead of
   `crontab <file>`):

   ```bash
   # old (destructive):          crontab my-job.crontab
   bash ~/.hermes/scripts/crontab-guard.sh install my-job.crontab
   ```

## Backup layout

```
~/.hermes/backups/crontab/
├── pre-deploy.crontab      # restore source (pinned baseline)
├── pre-deploy.count        # active-job count of the baseline
└── <ts>.crontab            # timestamped snapshots (hourly via tick; keep 40)
```

## Verification (2026-09-17, live on this host)

- Sandbox: 7/7 tests pass (`pytest tests/test_crontab_guard.py`).
- Live restore E2E: applied the incident shape for real (crontab 12→1 jobs),
  canary alarmed `JOBS_LOST` + `RESTORED`, crontab byte-identical to the
  original afterwards.
- Live install E2E: merge-install of an already-present line = no-op, 12
  jobs, verified ok, no alarms.

## Deployed copies

Byte-identical rule as for every script (`tests/test_crontab_guard.py` and
`tests/test_deployed_copies_sync.py` enforce it): repo, `~/.hermes/scripts/`,
`~/.hermes/profiles/pr-ollama/scripts/`. The tick copies were redeployed
together with the guard (they carry the canary call).
