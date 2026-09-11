#!/usr/bin/env bash
# obs-serve-cron.sh — OBJ-27 F5d: respawn the hyperespace portal server.
#
# Supervisor probe (daemon pattern, same family as the kanban daemon):
#   - server alive  -> SILENT stdout (watchdog pattern; the cron layer
#                      delivers only non-empty output)
#   - server dead   -> respawn it (nohup, PID file guarded) and print one
#                      line of evidence
#
# The URL is NOT printed on the healthy path: the morning report carries
# it (one place, daily). Zero tokens, localhost only.
#
# HERMES_HOME is pinned to pr-ollama (where the trace and the portal live)
# regardless of which profile runs the cron daemon.
export HERMES_HOME="$HOME/.hermes/profiles/pr-ollama"
SERVE="/data/git/hermes-plugin-quota-governor/scripts/obs/obs-serve.py"
PORT="${OBS_PORTAL_PORT:-8917}"

if /usr/bin/python3.12 "$SERVE" --port "$PORT" --check >/dev/null 2>&1; then
    exit 0  # alive — silence
fi

# dead: respawn (obs-serve is itself idempotent: it exits silently if the
# port is already serving, so a race between ticks can never double-bind)
nohup /usr/bin/python3.12 "$SERVE" --port "$PORT" \
    >> "$HERMES_HOME/quota-governor/obs/obs-serve.log" 2>&1 &
sleep 2

if /usr/bin/python3.12 "$SERVE" --port "$PORT" --check >/dev/null 2>&1; then
    echo "obs-portal respawned: http://localhost:$PORT (estaba caido)"
else
    echo "obs-portal NO pudo levantarse en el puerto $PORT — revisar $HERMES_HOME/quota-governor/obs/obs-serve.log"
fi