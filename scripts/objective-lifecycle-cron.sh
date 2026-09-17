#!/usr/bin/env bash
# objective-lifecycle-cron.sh — Governance P2 wrapper (no_agent, zero tokens).
#
# Hourly: writes ONE timestamped tick line to the log (liveness signal for
# cron-health-check.sh — the script itself is quiet on success, watchdog
# pattern; a quiet log would false-positive DEAD/ZOMBIE), then runs the
# lifecycle engine. The engine appends its own tick line too; the wrapper's
# line guards the case where the engine dies before its first print.
#
# Tick de liveness: SIEMPRE al log canonico compartido que vigila
# cron-health-check.sh (HERMES_HOME_DIR hardcodeado alli). No usar
# HERMES_HOME: bajo un perfil vale <perfil> y el tick aterriza en un log
# que el health-check no lee (2026-09-17: eso provocó un restart fantasma
# y un doble tick a los 15s). El engine resuelve su JSONL igual (~/.hermes),
# así que wrapper y engine siempre escriben al mismo árbol.
LOG=/home/iinstances/.hermes/logs/objective-lifecycle.log
TS=$(date +'%Y-%m-%d %H:%M:%S')
echo "[$TS] objective-lifecycle: tick" >> "$LOG"
echo "[$TS] objective-lifecycle: tick"
exec /usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/objective-lifecycle.py --verbose
