#!/usr/bin/env bash
# triage-bridge-cron.sh — no-agent cron wrapper for triage-bridge.py (OBJ-21)
#
# Deterministic triage->todo bridge: promotes at most 1 fully-tagged task per
# tick (header objective:OBJ- + cost in {micro,tiny,small,medium}, no human
# approval gate). Silent stdout = nothing promoted (watchdog pattern); the
# only non-empty line is "TRIAGE-BRIDGE: <id> -> todo (...)" when something
# actually promotes. No LLM, zero tokens.
#
# Kill switch: touch ~/.hermes/quota-governor/PROMOTE-STOP
set -euo pipefail

SCRIPT="$HOME/.hermes/scripts/triage-bridge.py"
if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: triage-bridge.py not found at $SCRIPT" >&2
  exit 0  # exit 0 so cron doesn't alert on a deploy gap; doctor surfaces it
fi

exec /usr/bin/python3.12 "$SCRIPT" --execute
