#!/usr/bin/env bash
# backtest-f2-cron.sh — OBJ-24 F2 backtest harness: no_agent wrapper for
# backtest-f2.py. Silent stdout = no new FAIL verdict (watchdog pattern).
# HERMES_HOME is pinned to pr-ollama (where F1/F2 keep metrics-history
# and forecast.json) regardless of which profile runs the cron daemon.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3.12 "$SCRIPT_DIR/backtest-f2.py"
