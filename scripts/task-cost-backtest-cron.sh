#!/usr/bin/env bash
# task-cost-backtest-cron.sh — OBJ-35 P3: wrapper no_agent para
# task-cost-backtest.py (estimator accuracy). Silent stdout = no new
# verdict (watchdog pattern). REPO copy, budget-check-cron pattern.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
exec /usr/bin/python3.12 \
  "REPO/scripts/obs/task-cost-backtest.py"
