#!/usr/bin/env bash
# trace-backfill-cron.sh — OBJ-27 F5b: keep the trace's past complete.
#
# Runs the backfill importer on the cron cadence: the model-cost-ledger
# keeps growing (new opencode-go deltas), so the trace needs its history
# appended as the sources grow. Idempotent by design (natural keys +
# high-water cursors): silent stdout = nothing new to ingest (watchdog
# pattern). One evidence line when it actually appended history.
#
# HERMES_HOME pinned to pr-ollama (same convention as morning-screen).
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
OUT=$(/usr/bin/python3.12 "/data/git/hermes-plugin-quota-governor/scripts/obs/trace-backfill.py" 2>&1) || true
case "$OUT" in
    "backfill ok: appended=0 "*) exit 0 ;;
    "") exit 0 ;;
    *) echo "$OUT" ;;
esac