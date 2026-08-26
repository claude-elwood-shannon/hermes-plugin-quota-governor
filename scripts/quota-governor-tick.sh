#!/usr/bin/env bash
# quota-governor-tick.sh — cron no_agent tick: query quota, decide action, adjust daemon.
# Zero tokens (pure bash + inline python3). Silent stdout = no change.
# One-line stdout = something changed (for cron no_agent watchdogs).
#
# Runs every 15-30 min via cron job with no_agent=True.
# Hermes cron jobs don't run as login shells, so env vars may not be set.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/pr-ollama}"
ENV_FILE="$HOME/.hermes/profiles/pr-ollama/.env"
LOG_DIR="$HOME/.hermes/logs"
LOG_FILE="$LOG_DIR/quota-governor-tick.log"
STOP_FILE="$HERMES_HOME/quota-governor/STOP"
PIDFILE="$HERMES_HOME/quota-governor-daemon.pid"
MAX_FILE="$HERMES_HOME/quota-governor-daemon.max"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

# ---------------------------------------------------------------------------
# Step 1: Source credentials from profile .env
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    log "ERROR: no .env at $ENV_FILE"
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

# Disable proxy for Ollama API calls — the proxy is for GitHub enforcement,
# not for Ollama. Ollama rejects connections from Tor exit nodes.
unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY 2>/dev/null || true

if [[ -z "${OLLAMA_API_KEY:-}" ]]; then
    log "ERROR: OLLAMA_API_KEY not set in $ENV_FILE"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Query Ollama Cloud API
# ---------------------------------------------------------------------------
RAW=$(curl -s --max-time 15 \
    -H "Authorization: Bearer $OLLAMA_API_KEY" \
    https://ollama.com/api/usage) || {
    log "ERROR: curl failed"
    exit 1
}
if [[ -z "$RAW" ]]; then
    log "ERROR: empty response from Ollama API"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3-4: Parse JSON + apply heuristic (matches quota_planner.py exactly)
# ---------------------------------------------------------------------------
# Output: action|max_workers|session_pct|weekly_pct|session_reqs|weekly_reqs|reason
RESULT=$(echo "$RAW" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('limits', {}).get('session', {})
w = d.get('limits', {}).get('weekly', {})

sp = float(s.get('usage', 0)) * 100  # 0-100
wp = float(w.get('usage', 0)) * 100
sr = sum(m.get('request_count', 0) for m in s.get('models', []))
wr = sum(m.get('request_count', 0) for m in w.get('models', []))

# ── Heuristic from quota_planner.py (Aug 2026) ──

# Weekly overrides (most restrictive wins)
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
") || {
    log "ERROR: python3 parse/decide failed"
    exit 1
}

IFS='|' read -r ACTION DESIRED_MAX SESSION_PCT WEEKLY_PCT SESSION_REQS WEEKLY_REQS REASON <<< "$RESULT"

log "session=${SESSION_PCT}% weekly=${WEEKLY_PCT}% reqs=${SESSION_REQS}/${WEEKLY_REQS} → ${ACTION} max=${DESIRED_MAX} — ${REASON}"

# ---------------------------------------------------------------------------
# Step 6: Check STOP signal
# ---------------------------------------------------------------------------
if [[ -f "$STOP_FILE" ]]; then
    log "STOP signal active — not touching daemon"
    # Kill daemon if still running (shouldn't be, but guard)
    if [[ -f "$PIDFILE" ]]; then
        OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
            kill "$OLD_PID" 2>/dev/null || true
            rm -f "$PIDFILE" "$MAX_FILE"
            log "Killed daemon (STOP signal present)"
            echo "STOP: daemon halted (session=${SESSION_PCT}% weekly=${WEEKLY_PCT}%)"
        fi
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 5 (cont): Handle "stop" decision — kill daemon if running
# ---------------------------------------------------------------------------
if [[ "$ACTION" == "stop" ]]; then
    if [[ -f "$PIDFILE" ]]; then
        OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
            kill "$OLD_PID" 2>/dev/null || true
            rm -f "$PIDFILE" "$MAX_FILE"
            log "Stopped daemon (quota exhausted: ${REASON})"
            echo "STOP: daemon halted — ${REASON} (s=${SESSION_PCT}% w=${WEEKLY_PCT}%)"
        fi
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 5 (main): Adjust or start daemon
# ---------------------------------------------------------------------------
DAEMON_NEEDS_ACTION=false

if [[ -f "$PIDFILE" ]]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [[ -z "$OLD_PID" ]] || ! kill -0 "$OLD_PID" 2>/dev/null; then
        # Stale pidfile — clean and restart
        rm -f "$PIDFILE" "$MAX_FILE"
        log "Stale pidfile cleaned (PID ${OLD_PID:-unknown})"
        DAEMON_NEEDS_ACTION=true
    else
        # Running — check if --max changed
        if [[ -f "$MAX_FILE" ]]; then
            OLD_MAX=$(cat "$MAX_FILE" 2>/dev/null || echo "")
            if [[ "$OLD_MAX" != "$DESIRED_MAX" ]]; then
                kill "$OLD_PID" 2>/dev/null || true
                rm -f "$PIDFILE"
                log "--max changed: ${OLD_MAX:-?} → ${DESIRED_MAX}, restarting"
                DAEMON_NEEDS_ACTION=true
            fi
        fi
    fi
else
    DAEMON_NEEDS_ACTION=true
fi

if $DAEMON_NEEDS_ACTION; then
    # Store desired max BEFORE starting (so a concurrent tick sees intent)
    echo "$DESIRED_MAX" > "$MAX_FILE"

    HERMES_BIN="${HERMES_BIN:-$(command -v hermes)}"
    nohup "$HERMES_BIN" kanban daemon \
        --interval 60 \
        --max "$DESIRED_MAX" \
        --pidfile "$PIDFILE" \
        --verbose \
        --force >/dev/null 2>&1 &
    disown

    # Wait for pidfile to appear (daemon writes it on start)
    slept=0
    while [[ ! -f "$PIDFILE" && $slept -lt 5 ]]; do
        sleep 1
        ((slept++)) || true
    done

    NEW_PID=$(cat "$PIDFILE" 2>/dev/null || echo "unknown")
    log "Daemon started (PID ${NEW_PID}, --max ${DESIRED_MAX})"

    # Output: one-line change summary (cron no_agent delivery)
    echo "Tick: ${ACTION} workers=${DESIRED_MAX} — ${REASON} (s=${SESSION_PCT}% w=${WEEKLY_PCT}%)"
fi

# If nothing changed, stdout is empty → silent tick
