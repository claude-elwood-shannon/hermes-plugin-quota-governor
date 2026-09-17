#!/usr/bin/env bash
# crontab-guard.sh — fuse-vs-REPLACE protection for the user crontab (t_da32e7f9).
#
# Incident (2026-09-15 01:25:05 CEST): an ad-hoc deploy (obs-shipper, work
# under t_a9319828) replaced the whole user crontab with a single-job file —
# syslog `crontab[2718674] (iinstances) REPLACE (iinstances)`. 9 of 10 jobs
# vanished (kanban-watchdog, obs-serve, cron-health-check, efficiency-ratio,
# open-webui-bridge, hermes-backup, space_monitor, cleanup,
# approval-ready-fix): 32 min without watchdog or health-check, efficiency
# ratio stalled 105 min. Restored by manual REPLACEs at 02:11–02:13.
#
# Why this guard runs OUTSIDE the user crontab: it is invoked from the
# quota-governor tick (Hermes cron ticker, profile jobs.json) — the only
# executor that kept running through the incident, because every crontab job
# was gone. The tripwire must not share fate with what it guards.
#
# Subcommands:
#   snapshot        — save a pre-deploy baseline: $BACKUP_DIR/<ts>.crontab
#                     plus $BACKUP_DIR/pre-deploy.crontab (+ its job count).
#   install <file>  — MERGE deploy: snapshot, then install existing crontab
#                     plus any MISSING active lines from <file>. Never removes
#                     or reorders existing lines, never duplicates. Verifies
#                     every line of <file> landed; alarms and exits 1 if not.
#   canary          — tripwire: compares current active-job count against the
#                     pre-deploy baseline (or, failing that, the newest
#                     timestamped backup). If the count shrank: alarm to
#                     cron-alarms.jsonl and RESTORE = baseline UNION every
#                     active line that survived in the current crontab (the
#                     surviving lines are the deployer's own additions — the
#                     2026-09-15 manual recovery restored old jobs AND the
#                     new shipper line). Additions never trigger the canary.
#                     With no baseline and no backups, takes the first
#                     snapshot (bootstrap) and exits 0. Healthy runs refresh
#                     the hourly timestamped snapshot without moving the
#                     pre-deploy baseline.
#
# Safety:
#   - Never runs `crontab -r`.
#   - Restore fires ONLY when the active-job count strictly decreases.
#     Additions never trigger it.
#   - DIRECCION-STOP respected: with the STOP file present the canary alarms
#     but does NOT restore (verification-only mode; use it when a job was
#     removed intentionally).
#   - Fail-open: any internal failure exits non-zero without touching the
#     crontab; the caller (tick) must not break.
#   - Backups pruned to the last $KEEP_BACKUPS timestamped snapshots.
set -u

HOME_REAL="${CRONTAB_GUARD_HOME:-$HOME}"
BACKUP_DIR="${CRONTAB_BACKUP_DIR:-$HOME_REAL/.hermes/backups/crontab}"
ALARMS="${CRONTAB_ALARM_FILE:-$HOME_REAL/.hermes/logs/cron-alarms.jsonl}"
CRONTAB_BIN="${CRONTAB_BIN:-/usr/bin/crontab}"
KEEP_BACKUPS="${CRONTAB_KEEP_BACKUPS:-40}"
PRE="$BACKUP_DIR/pre-deploy.crontab"
PRE_COUNT="$BACKUP_DIR/pre-deploy.count"
STOP_FILE="${CRONTAB_GUARD_STOP_FILE:-$HOME_REAL/.hermes/profiles/pr-ollama/quota-governor/STOP}"

ts() { date +'%Y-%m-%dT%H:%M:%S%z'; }
stamp() { date +'%Y%m%d-%H%M%S'; }

dump() { "$CRONTAB_BIN" -l 2>/dev/null || true; }

count_active_file() { grep -cEv '^[[:space:]]*(#|$)' "$1" 2>/dev/null || true; }
count_jobs() { dump | grep -cEv '^[[:space:]]*(#|$)' || true; }

alarm() { # alarm <status> <detail>
  mkdir -p "$(dirname "$ALARMS")" 2>/dev/null || true
  /usr/bin/python3.12 - "$ALARMS" "$(ts)" "$1" "$2" <<'PY'
import json, sys
path, ts, status, detail = sys.argv[1:5]
entry = {"ts": ts, "cron": "crontab-guard", "status": status, "detail": detail}
try:
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
except Exception:
    pass
PY
}

prune_backups() {
  ls -1t "$BACKUP_DIR"/2*.crontab 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" \
    | xargs -r rm -f
}

write_baseline() { # write_baseline <content-file>
  cp "$1" "$PRE"
  count_active_file "$1" > "$PRE_COUNT"
}

cmd_snapshot() {
  mkdir -p "$BACKUP_DIR"
  local s; s="$(stamp)"
  dump > "$BACKUP_DIR/$s.crontab"
  write_baseline "$BACKUP_DIR/$s.crontab"
  prune_backups
  echo "crontab-guard: snapshot $s.crontab ($(count_active_file "$PRE") active jobs, baseline updated)"
}

cmd_install() {
  local file="$1"
  if [ ! -f "$file" ]; then
    echo "crontab-guard: install: file not found: $file" >&2
    return 2
  fi
  mkdir -p "$BACKUP_DIR"
  # Pre-deploy baseline ALWAYS, even if the merge turns out to be a no-op:
  # this is the restore point the canary will use if a later deploy misfires.
  local s; s="$(stamp)"
  dump > "$BACKUP_DIR/$s.crontab"
  write_baseline "$BACKUP_DIR/$s.crontab"
  prune_backups
  # Merge = current crontab verbatim + missing active lines from the deploy
  # file (dedup keeps the first occurrence: existing lines win, order kept).
  local merged="$BACKUP_DIR/.merge.candidate"
  { dump; grep -Ev '^[[:space:]]*(#|$)' "$file"; } | awk '!seen[$0]++' > "$merged"
  if ! "$CRONTAB_BIN" "$merged"; then
    alarm "INSTALL_FAILED" "crontab write failed; crontab untouched (snapshot $s.crontab)"
    echo "crontab-guard: install FAILED (crontab write rejected)" >&2
    return 1
  fi
  # Verify every active line of the deploy file is present now.
  local want have missing
  want=$(mktemp); have=$(mktemp)
  grep -Ev '^[[:space:]]*(#|$)' "$file" | sort > "$want"
  dump | grep -Ev '^[[:space:]]*(#|$)' | sort > "$have"
  missing=$(comm -23 "$want" "$have" | head -n 5 | tr '\n' ';')
  rm -f "$want" "$have"
  if [ -n "$missing" ]; then
    alarm "INSTALL_INCOMPLETE" "lines missing after install: $missing"
    echo "crontab-guard: install INCOMPLETE (missing: $missing)" >&2
    return 1
  fi
  rm -f "$merged"
  echo "crontab-guard: merged install ok ($(count_jobs) active jobs; snapshot $s.crontab)"
}

cmd_canary() {
  mkdir -p "$BACKUP_DIR" 2>/dev/null || true
  local now
  now=$(count_jobs)
  now=$((now + 0))
  local before source
  if [ -f "$PRE_COUNT" ]; then
    before=$(cat "$PRE_COUNT" 2>/dev/null || echo "$now")
    source="$PRE"
  else
    # No pre-deploy baseline: fall back to the newest timestamped backup so
    # unannounced deploys (the 2026-09-15 incident shape) are still caught
    # within one tick interval.
    source=$(ls -1t "$BACKUP_DIR"/2*.crontab 2>/dev/null | head -n 1)
    if [ -z "$source" ] || [ ! -f "$source" ]; then
      local s; s="$(stamp)"
      dump > "$BACKUP_DIR/$s.crontab"
      write_baseline "$BACKUP_DIR/$s.crontab"
      echo "crontab-guard: no baseline — first snapshot taken ($s.crontab)"
      return 0
    fi
    before=$(count_active_file "$source")
  fi
  case "$before" in ''|*[!0-9]*) before=$now;; esac
  before=$((before + 0))
  if [ "$now" -ge "$before" ]; then
    # Healthy: refresh the forensic snapshot (baseline/pre-deploy untouched).
    local s; s="$(stamp)"
    dump > "$BACKUP_DIR/$s.crontab"
    prune_backups
    echo "crontab-guard: canary OK ($now >= $before active jobs)"
    return 0
  fi
  alarm "JOBS_LOST" "active jobs $before -> $now; restore source: $source"
  echo "crontab-guard: JOBS LOST ($before -> $now) — restoring from $source"
  # Restore = baseline UNION surviving active lines (dedup, baseline order
  # first). A surviving line is the deployer's own addition: the 2026-09-15
  # manual recovery restored the old jobs AND the new shipper line, and a
  # blind baseline restore would re-delete the deploy target.
  local union="$BACKUP_DIR/.restore.union"
  { cat "$source"; dump | grep -Ev '^[[:space:]]*(#|$)'; } \
    | awk '!seen[$0]++' > "$union"
  if [ -f "$STOP_FILE" ]; then
    echo "crontab-guard: DIRECCION-STOP active — alarmed, NOT restoring"
    rm -f "$union"
    return 0
  fi
  if "$CRONTAB_BIN" "$union"; then
    alarm "RESTORED" "crontab restored from $source (baseline union survivors; jobs $before -> $(count_jobs))"
    echo "crontab-guard: restored from $source"
    rm -f "$union"
  else
    alarm "RESTORE_FAILED" "could not install $source — MANUAL ACTION REQUIRED (backups: $BACKUP_DIR)"
    echo "crontab-guard: RESTORE FAILED — manual action required" >&2
    rm -f "$union"
    return 1
  fi
}

usage() {
  echo "usage: crontab-guard.sh snapshot | install <file> | canary" >&2
  exit 2
}

case "${1:-}" in
  snapshot) cmd_snapshot ;;
  install)
    shift
    [ "${1:-}" != "" ] || usage
    cmd_install "$1"
    ;;
  canary) cmd_canary ;;
  *) usage ;;
esac
