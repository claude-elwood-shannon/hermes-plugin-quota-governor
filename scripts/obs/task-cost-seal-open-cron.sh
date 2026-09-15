#!/usr/bin/env bash
# task-cost-seal-open-cron.sh — OBJ-METRICS P3: wrapper no_agent para
# task-cost-seal-open.py (sella estimaciones en tareas AÚN ABIERTAS).
# Silent stdout = nothing new sealed (watchdog pattern). REPO copy,
# task-cost-backtest-cron pattern.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
exec /usr/bin/python3.12 \
  "$SCRIPT_DIR/task-cost-seal-open.py"
