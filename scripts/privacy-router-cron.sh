#!/usr/bin/env bash
# privacy-router-cron.sh — no-agent cron wrapper for privacy-router-fix.py
#
# Runs privacy-router-fix.py deterministically (no LLM involved).
# Silent stdout = no misrouted tasks found (watchdog pattern).
# Non-empty stdout = reassigned task(s) — delivered by the cron layer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIX_SCRIPT="$SCRIPT_DIR/privacy-router-fix.py"

if [[ ! -f "$FIX_SCRIPT" ]]; then
  echo "ERROR: privacy-router-fix.py not found at $FIX_SCRIPT" >&2
  exit 0
fi

# Source .env for API keys (privacy-gate.sh needs them via quota-gate.py)
ENV_FILE="$HOME/.hermes/profiles/pr-ollama/.env"
if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

exec python3 "$FIX_SCRIPT"