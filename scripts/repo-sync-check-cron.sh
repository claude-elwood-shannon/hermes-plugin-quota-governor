#!/bin/bash
# repo-sync-check-cron.sh — OBJ-13: wrapper for repo-sync-check.py
#
# Runs repo-sync-check.py with --execute enabled for the no_agent cron job.
# The Python script's stdout (when non-empty) is the notification delivered
# by the cron layer. On empty stdout (repo is synced), the cron stays silent.

export REPO_SYNC_EXECUTE=1
# Explicitly set HERMES_KANBAN_DB — cron daemon env may have a stale/wrong value
# (past runs logged "Kanban DB not found at /nonexistent.db").
export HERMES_KANBAN_DB="$HOME/.hermes/kanban.db"
exec /usr/bin/python3.12 "$(dirname "$0")/repo-sync-check.py" --execute
