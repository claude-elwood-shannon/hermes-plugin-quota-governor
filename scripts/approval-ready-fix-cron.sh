#!/usr/bin/env bash
# approval-ready-fix cron wrapper (P5, 2026-09-13).
# Backup path: the plugin hook (on_kanban_dispatch_tick) is the primary
# enforcement point; this runs every 30 min in case a proposal lands when
# no dispatch tick fires. Dry-run default inside the script; --execute here.
exec /usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/approval-ready-fix.py --execute
