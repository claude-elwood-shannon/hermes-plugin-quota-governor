#!/usr/bin/env bash
# quota-governor-tick.sh — cron no_agent tick: query quota, decide, adjust daemon.
# Zero tokens (pure bash + inline python3). Silent stdout = no change.
#
# Three-state model (Aug 2026): run / paying / stop.
# paying = session exhausted but pay-as-you-go balance is being consumed (warn).
# stop  = balance exhausted, spending limit hit, or weekly quota critical (halt).
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/pr-ollama}"
ENV_FILE="$HOME/.hermes/profiles/pr-ollama/.env"
LOG_DIR="$HOME/.hermes/logs"
LOG_FILE="$LOG_DIR/quota-governor-tick.log"
STOP_FILE="$HERMES_HOME/quota-governor/STOP"
PIDFILE="$HERMES_HOME/quota-governor-daemon.pid"
MAX_FILE="$HERMES_HOME/quota-governor-daemon.max"
OBS_FILE="$HERMES_HOME/quota-governor/observations.jsonl"

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

# Spending limit for pay-as-you-go (matches quota_planner.py / design doc §4.1)
SPENDING_LIMIT="${QUOTA_GOVERNOR_MAX_SPEND:-5.00}"
export SPENDING_LIMIT

# Read previous activity.cost from observations.jsonl (for cost-delta detection)
PREV_COST=$(python3 -c "
import json, os
obs = os.path.join(os.environ.get('HERMES_HOME', ''), 'quota-governor', 'observations.jsonl')
try:
    with open(obs) as f:
        lines = f.readlines()
    if lines:
        d = json.loads(lines[-1])
        q = d.get('quota', {})
        # Prefer canonical 'activity_cost' (design §5.4), fall back to legacy 'ollama_activity_cost'
        cost = q.get('activity_cost')
        if cost is None:
            cost = q.get('ollama_activity_cost', 0)
        print(cost or 0)
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null) || PREV_COST="0"
export PREV_COST

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
# Output: action|max_workers|session_pct|weekly_pct|session_reqs|weekly_reqs|cost|write_stop|reason
RESULT=$(python3 -c "
import json, os, urllib.request, sys

# Disable ALL proxy vars — Ollama rejects Tor
for v in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']:
    os.environ.pop(v, None)

api_key = os.environ.get('OLLAMA_API_KEY', '')
if not api_key:
    print('ERROR|0|0|0|0|0|0.0000|0|no API key')
    sys.exit(0)

try:
    req = urllib.request.Request(
        'https://ollama.com/api/usage',
        headers={'Authorization': f'Bearer {api_key}'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
except Exception as e:
    print(f'ERROR|0|0|0|0|0|0.0000|0|{e}')
    sys.exit(0)

s = data.get('limits',{}).get('session',{})
w = data.get('limits',{}).get('weekly',{})
sp = float(s.get('usage',0))*100
wp = float(w.get('usage',0))*100
sr = sum(m.get('request_count',0) for m in s.get('models',[]))
wr = sum(m.get('request_count',0) for m in w.get('models',[]))

# Pay-as-you-go cost (activity.cost from /api/usage)
try:
    cost = float(data.get('activity',{}).get('cost', 0))
except (TypeError, ValueError):
    cost = 0.0

prev_cost = float(os.environ.get('PREV_COST', 0) or 0)
spending_limit = float(os.environ.get('SPENDING_LIMIT', 5.0) or 0)

# ── Three-state heuristic (matches quota_planner.py decide(), design §3.2, §6) ──
write_stop = 0

# Weekly override (most restrictive wins)
if wp > 90:
    act, mw, rsn = 'stop', 0, f'weekly critical ({wp:.0f}%)'
elif wp > 75:
    if sp > 80:
        act, mw, rsn = 'stop', 0, f'both critical (s={sp:.0f}% w={wp:.0f}%)'
    else:
        act, mw, rsn = 'run', 1, f'weekly high ({wp:.0f}%)'
# Pay-as-you-go detection: session EXACTLY 100% + balance being consumed
elif sp >= 100 and cost > 0 and cost > prev_cost:
    if spending_limit > 0 and cost >= spending_limit:
        act, mw, rsn = 'stop', 0, f'spending limit reached (\${cost:.2f} >= \${spending_limit:.2f})'
        write_stop = 1
    else:
        act, mw, rsn = 'paying', 1, f'session exhausted, pay-as-you-go active (\${cost:.2f} spent)'
# Session at 100% but cost flat (no balance, or balance exhausted)
elif sp >= 100 and not (cost > 0 and cost > prev_cost):
    act, mw, rsn = 'stop', 0, f'session exhausted, no pay-as-you-go (s={sp:.0f}% cost={cost:.4f})'
# Session >95% but <100%: still in run, heavily throttled (was 'stop' in old script)
elif sp > 95:
    act, mw, rsn = 'run', 1, f'session near limit ({sp:.0f}%)'
elif sp > 80:
    act, mw, rsn = 'run', 1, f'session very high ({sp:.0f}%)'
elif sp > 60:
    act, mw, rsn = 'run', 1, f'session high ({sp:.0f}%)'
elif sp > 30:
    act, mw, rsn = 'run', 1, f'session moderate ({sp:.0f}%)'
else:
    mw = 2 if wp < 50 else 1
    act, rsn = 'run', f'healthy'

print(f'{act}|{mw}|{sp:.1f}|{wp:.1f}|{sr}|{wr}|{cost:.4f}|{write_stop}|{rsn}')
" 2>&1) || {
    log "ERROR: python3 query/decide failed"
    exit 0
}

IFS='|' read -r ACTION DESIRED_MAX SESSION_PCT WEEKLY_PCT SESSION_REQS WEEKLY_REQS COST WRITE_STOP REASON <<< "$RESULT"

if [[ "$ACTION" == "ERROR" ]]; then
    log "ERROR: $REASON"
    exit 0
fi

log "session=${SESSION_PCT}% weekly=${WEEKLY_PCT}% reqs=${SESSION_REQS}/${WEEKLY_REQS} cost=\$${COST} -> ${ACTION} max=${DESIRED_MAX} -- ${REASON}"

# Paying-mode warning
if [[ "$ACTION" == "paying" ]]; then
    log "⚠ PAY-AS-YOU-GO: spending balance at \$${COST} (limit \$${SPENDING_LIMIT})"
fi

# Write STOP signal for spending-limit stop (keeps daemon halted until manual clear)
if [[ "$ACTION" == "stop" && "$WRITE_STOP" == "1" ]]; then
    mkdir -p "$(dirname "$STOP_FILE")"
    echo "spending limit reached: ${REASON}" > "$STOP_FILE"
    log "STOP signal written (spending limit: \$${COST} >= \$${SPENDING_LIMIT})"
fi

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
            echo "Tick: stopped daemon — ${REASON} (s=${SESSION_PCT}% w=${WEEKLY_PCT}% cost=\$${COST})"
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
        echo "Tick: ${ACTION} workers=${DESIRED_MAX} — ${REASON} (s=${SESSION_PCT}% w=${WEEKLY_PCT}% cost=\$${COST})"
    else
        log "ERROR: hermes binary not found"
    fi
fi

# ── Health checks (OBJ-09): fast burn, zombie workers, silent plugin ──
# Runs the three health detections and includes any new alerts in stdout.
# Alerts are also persisted to ~/.hermes/logs/quota-governor-alerts.log.
PLUGIN_DIR="${PLUGIN_DIR:-REPO}"
HEALTH_OUTPUT=$(HERMES_HOME="$HERMES_HOME" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PLUGIN_DIR', 'REPO'))
try:
    from health_checks import run_all_health_checks, format_alerts_for_stdout
    alerts = run_all_health_checks()
    if alerts:
        print(format_alerts_for_stdout(alerts))
except Exception as e:
    # Health checks should never break the tick
    print(f'Health check error: {e}', file=sys.stderr)
" 2>/dev/null) || true

if [[ -n "$HEALTH_OUTPUT" ]]; then
    echo "$HEALTH_OUTPUT"
    log "Health alerts: $(echo "$HEALTH_OUTPUT" | wc -l) alert(s) detected"
fi

exit 0