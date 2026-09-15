#!/usr/bin/env bash
# otlp-cron.sh — OpenObserve OTLP export wrapper (t_88753960; no_agent,
# zero tokens). Every 5 min: ship new quota-governor trace lines to
# OpenObserve (spans -> default traces stream, cost gauge ->
# house_consumption_cost_usd) via the OBJ-27 F4 exporter (stdlib only,
# Basic auth from the shared blind copy of the OO root env — never
# printed, never versioned).
#
# Watchdog pattern: ONE timestamped tick line per run in the log below so
# cron-health-check.sh can verify liveness (a quiet log = dead cron).
TS=$(date +'%Y-%m-%d %H:%M:%S')
LOG=/home/iinstances/.hermes/logs/otlp-export.log
echo "[$TS] otlp-export: tick" >> "$LOG"

# single-instance guard (flock; non-blocking: overlapping runs skip)
exec 9>/tmp/otlp-export.lock 2>/dev/null || exec 9>/run/user/$(id -u)/otlp-export.lock
flock -n 9 || exit 0

export HERMES_HOME=/home/iinstances/.hermes/profiles/pr-ollama
export OBS_OTLP_ENDPOINT=http://192.168.1.23:5080/api/default
# Blind copy (chmod 600) made by the obs-shipper deploy (t_9e457672):
# ZO_ROOT_USER_EMAIL / ZO_ROOT_USER_PASSWORD from the services host .env.
export OBS_OTLP_AUTH_FILE=/home/iinstances/.hermes/obs-shipper/oo.env

# self-rotation: keep the log bounded (~5 MB)
SIZE=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
if [ "$SIZE" -gt 5242880 ]; then
  tail -c 1048576 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

/usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/obs/otlp_exporter.py >> "$LOG" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] otlp-export: exporter rc=$RC (cursor NOT advanced; retried next tick)" >> "$LOG"
fi
# health-check reads liveness from log mtime, not rc: converge to ok
exit 0
