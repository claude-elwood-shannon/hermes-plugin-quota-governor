#!/usr/bin/env bash
# task-cost-train-cron.sh — OBJ-35 P1: wrapper no_agent para
# task-cost-train.py (per-task cost observer). Silent stdout = nothing
# new (watchdog pattern). Points at the REPO copy (single source of
# truth, budget-check-cron.sh pattern — avoid the two-copies drift).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
exec /usr/bin/python3.12 \
  "$SCRIPT_DIR/obs/task-cost-train.py"
