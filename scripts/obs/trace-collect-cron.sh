#!/usr/bin/env bash
# trace-collect-cron.sh — OBJ-31 follow-up (t_c093299e): the missing BASE collector.
#
# Until now trace.jsonl was only fed by the backfill importer (trace-backfill)
# plus manual runs; `trace.py collect` — the base incremental collector that
# ingests usage-audit / nanogpt-requests / task-events rows — had NO cron.
# This wrapper runs it on the 15m cadence so the live trace never depends on
# backfills to stay current.
#
# Watchdog pattern (same as every no_agent cron in this house): SILENT stdout
# when the collect appended nothing (all sources 0); one evidence line
# "collect ok: appended=N {...}" when it actually ingested rows; explicit
# error line + exit 1 if the collector itself failed.
#
# HERMES_HOME is pinned to pr-ollama (where the trace and its cursor live)
# regardless of which profile runs the cron daemon. Same convention as
# trace-backfill-cron.sh / trace-alarms-cron.sh. Zero tokens, free quota.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
OUT=$(/usr/bin/python3.12 "/data/git/hermes-plugin-quota-governor/scripts/obs/trace.py" collect 2>&1) \
    || { echo "trace-collect: ERROR $OUT"; exit 1; }
printf '%s' "$OUT" | /usr/bin/python3.12 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    sys.exit(0)  # non-JSON with exit 0: nothing actionable
app = sum(int(v) for v in d.values() if isinstance(v, (int, float)))
if app > 0:
    print(f"collect ok: appended={app} {d}")
'
