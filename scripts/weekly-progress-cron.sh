#!/bin/bash
# weekly-progress-cron.sh — OBJ-08: wrapper for weekly-progress.py (no_agent cron).
#
# Sundays ~23:00 UTC. Regenerates objective-progress.json AND writes the ISO
# week markdown into docs/weekly-reports/. The script's stdout (a one-line
# summary) is the notification delivered by the cron layer; stderr surfaces
# failures (e.g. kanban.db unreadable -> exit 1 -> no partial writes).
#
# PIT (repo-sync-check precedent): the cron daemon env may carry a stale
# HERMES_KANBAN_DB; weekly-progress.py reads WP_KANBAN_DB which we pin here.
#
# Registration (run ONCE by the orchestrator/user):
#   hermes cron create weekly-progress --name weekly-progress \
#       --script weekly-progress-cron.sh --no-agent --deliver local "0 23 * * 0"

export WP_KANBAN_DB="${WP_KANBAN_DB:-$HOME/.hermes/kanban.db}"
exec /usr/bin/python3.12 "$(dirname "$0")/weekly-progress.py" --execute
