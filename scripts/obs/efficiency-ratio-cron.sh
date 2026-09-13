#!/usr/bin/env bash
# efficiency-ratio-cron.sh — P4 wrapper (no_agent, zero tokens).
# Hourly: appends one efficiency_ratio line to metrics-history.jsonl and
# ONE timestamped tick line to the log (liveness signal for
# cron-health-check.sh — the script itself is silent on success,
# watchdog pattern; a quiet log would false-positive DEAD/ZOMBIE).
TS=$(date +'%Y-%m-%d %H:%M:%S')
echo "[$TS] efficiency-ratio: tick"
exec /usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/obs/efficiency-ratio.py --verbose
