#!/usr/bin/env bash
# fondo-queue-watch-cron.sh — OBJ-30b: no_agent wrapper for fondo-queue-watch.py.
#
# Anti-parada mechanism during budget windows: while a fondo window is active
# AND the ready+running queue has been empty >30min, apply the cascade
# (promote next phase / structural successor / assign triage / annotate+STOP).
#
# Emits one line per action taken to stdout; SILENT stdout = nothing to do
# (watchdog pattern — the cron layer delivers only non-empty output).
# Zero tokens, free quota.
#
# HERMES_HOME is pinned to pr-ollama (where the board and quota-governor state
# live) regardless of which profile runs the cron daemon. Same convention as
# morning-screen-cron.sh / trace-alarms-cron.sh.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3.12 "$SCRIPT_DIR/fondo-queue-watch.py" --execute
