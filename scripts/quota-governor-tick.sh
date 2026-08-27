#!/usr/bin/env bash
# quota-governor-tick.sh — cron no_agent tick: query quota, decide, adjust daemon.
# Zero tokens (pure bash + inline python3). Silent stdout = no change.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/pr-ollama}"
ENV_FILE="$HOME/.hermes/profiles/pr-ollama/.env"
LOG_DIR="$HOME/.hermes/logs"
LOG_FILE="$LOG_DIR/quota-governor-tick.log"
STOP_FILE="$HERMES_HOME/quota-governor/STOP"
PIDFILE="$HERMES_HOME/quota-governor-daemon.pid"
MAX_FILE="$HERMES_HOME/quota-governor-daemon.max"

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

if [[ ! -f "$ENV_FILE" ]]; then
    log "ERROR: no .env at $ENV_FILE"
    exit 0  # exit 0 so cron doesn't alert
fi
source "$ENV_FILE"

# Export key vars so python3 subprocesses can see them
# (the .env uses VAR=value without export, so source loads them as shell
# variables but they don't reach child processes via os.environ)
export OLLAMA_API_KEY 2>/dev/null || true

if [[ -z "${OLLAMA_API_KEY:-}" ]]; then
    log "ERROR: OLLAMA_API_KEY not set in $ENV_FILE"
    exit 0
fi

# Check STOP signal
if [[ -f "$STOP_FILE" ]]; then
    log "STOP signal active — not touching daemon"
    # Try to kill daemon if running
    if [[ -f "$PIDFILE" ]]; then
        OLDPID=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
            kill "$OLDPID" 2>/dev/null || true
            log "Killed daemon (STOP signal, PID $OLDPID)"
        fi
        rm -f "$PIDFILE"
    fi
    exit 0
fi

# Query + decide in one python3 call (avoids proxy issues with curl)
RESULT=$(python3 -c "
import json, os, urllib.request, sys

# Disable ALL proxy vars — Ollama rejects Tor
for v in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']:
    os.environ.pop(v, None)

api_key = os.environ.get('OLLAMA_API_KEY', '')
if not api_key:
    print('ERROR|0|0|0|0|0|no API key')
    sys.exit(0)

try:
    req = urllib.request.Request(
        'https://ollama.com/api/usage',
        headers={'Authorization': f'Bearer {api_key}'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
except Exception as e:
    print(f'ERROR|0|0|0|0|0|{e}')
    sys.exit(0)

s = data.get('limits',{}).get('session',{})
w = data.get('limits',{}).get('weekly',{})
sp = float(s.get('usage',0))*100
wp = float(w.get('usage',0))*100
sr = sum(m.get('request_count',0) for m in s.get('models',[]))
wr = sum(m.get('request_count',0) for m in w.get('models',[]))

if wp > 90:
    act, mw, rsn = 'stop', 0, f'weekly critical ({wp:.0f}%)'
elif wp > 75:
    if sp > 80:
        act, mw, rsn = 'stop', 0, f'both critical (s={sp:.0f}% w={wp:.0f}%)'
    else:
        act, mw, rsn = 'caution', 1, f'weekly high ({wp:.0f}%)'
elif sp > 95:
    act, mw, rsn = 'stop', 0, f'session exhausted ({sp:.0f}%)'
elif sp > 80:
    act, mw, rsn = 'caution', 1, f'session very high ({sp:.0f}%)'
elif sp > 60:
    act, mw, rsn = 'caution', 1, f'session high ({sp:.0f}%)'
elif sp > 30:
    act, mw, rsn = 'run', 1, f'session moderate ({sp:.0f}%)'
else:
    mw = 2 if wp < 50 else 1
    act, rsn = 'run', f'healthy'

print(f'{act}|{mw}|{sp:.1f}|{wp:.1f}|{sr}|{wr}|{rsn}')
" 2>&1) || {
    log "ERROR: python3 query/decide failed"
    exit 0
}

IFS='|' read -r ACTION DESIRED_MAX SESSION_PCT WEEKLY_PCT SESSION_REQS WEEKLY_REQS REASON <<< "$RESULT"

if [[ "$ACTION" == "ERROR" ]]; then
    log "ERROR: $REASON"
    exit 0
fi

log "session=${SESSION_PCT}% weekly=${WEEKLY_PCT}% reqs=${SESSION_REQS}/${WEEKLY_REQS} -> ${ACTION} max=${DESIRED_MAX} -- ${REASON}"

# Daemon management
DAEMON_NEEDS_ACTION=false

if [[ -f "$PIDFILE" ]]; then
    OLDPID=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
        # Daemon running — check if max needs change
        CURRENT_MAX=$(cat "$MAX_FILE" 2>/dev/null || echo "0")
        if [[ "$ACTION" == "stop" ]]; then
            kill "$OLDPID" 2>/dev/null || true
            rm -f "$PIDFILE" "$MAX_FILE"
            log "Killed daemon (quota stop, PID $OLDPID)"
            echo "Tick: stopped daemon — quota critical (s=${SESSION_PCT}% w=${WEEKLY_PCT}%)"
            exit 0
        elif [[ "$CURRENT_MAX" != "$DESIRED_MAX" ]]; then
            kill "$OLDPID" 2>/dev/null || true
            rm -f "$PIDFILE"
            DAEMON_NEEDS_ACTION=true
        fi
    else
        rm -f "$PIDFILE" "$MAX_FILE"
        DAEMON_NEEDS_ACTION=true
    fi
else
    if [[ "$ACTION" != "stop" ]]; then
        DAEMON_NEEDS_ACTION=true
    fi
fi

if [[ "$DAEMON_NEEDS_ACTION" == "true" && "$ACTION" != "stop" ]]; then
    HERMES_BIN="${HERMES_BIN:-$(command -v hermes)}"
    if [[ -z "$HERMES_BIN" ]]; then
        # Try common locations
        for p in "$HOME/.local/bin/hermes" "~/.local/bin/hermes"; do
            if [[ -x "$p" ]]; then HERMES_BIN="$p"; break; fi
        done
    fi
    if [[ -n "$HERMES_BIN" ]]; then
        nohup "$HERMES_BIN" kanban daemon \
            --interval 60 \
            --max "$DESIRED_MAX" \
            --pidfile "$PIDFILE" \
            --verbose \
            --force > /dev/null 2>&1 &
        echo "$DESIRED_MAX" > "$MAX_FILE"
        log "Daemon started (PID $!, --max $DESIRED_MAX)"
        echo "Tick: ${ACTION} workers=${DESIRED_MAX} — ${REASON}"
    else
        log "ERROR: hermes binary not found"
    fi
fi

exit 0
