#!/usr/bin/env bash
# objective-budgets-cron.sh — OBJ-28 phase 0 (observer): rollup by objective.
#
# no_agent wrapper for scripts/obs/objective-budgets.py. The rollup is a
# pure read-only function over the OBJ-27 F0 trace -> quota-governor/
# objective-budgets.json. Idempotent: silent stdout = nothing changed
# (watchdog pattern), one evidence line when the state moved.
#
# The repo path is PINNED (anti-drift, same rule as budget-check-cron.sh):
# repo = source of truth, no deployed-copy drift possible.
# HERMES_HOME is pinned to pr-ollama (where the trace and the budget state
# live) regardless of which profile runs the cron daemon.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
exec /usr/bin/python3.12 "/data/git/hermes-plugin-quota-governor/scripts/obs/objective-budgets.py"
