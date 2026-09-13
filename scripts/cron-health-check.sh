#!/usr/bin/env bash
# cron-health-check.sh — P3: watchdog del watchdog (zero tokens, sin LLM).
#
# Cada 15 min verifica que los crons críticos han corrido dentro de su
# ventana esperada (interval × 1.5). Si un cron está muerto:
#   1. intenta reiniciarlo (ejecutando su wrapper directamente),
#   2. loguea el reinicio,
#   3. escribe una alarma JSONL que el morning screen consume.
#
# Safety:
#   - DIRECCION-STOP activo: verifica y alarma, NO reinicia.
#   - Máx 3 reinicios/hora por cron; a la 4a falla -> CRON ZOMBIE (escala).
#   - Fail-open: un log ilegible no aborta la verificación del resto.
#   - pgrep antes de relanzar: si el proceso vive pero no loguea, se mata
#     y se relanza (nunca duplicados).
#
# Auto-vigilancia: este script escribe su propia ejecución en
# ~/.hermes/logs/cron-health-check.log; el quota-governor-tick vigila ese
# log (cadena circular de dos niveles).
set -u
TS=$(date +'%Y-%m-%dT%H:%M:%S%z')
LOG=/home/iinstances/.hermes/logs/cron-health-check.log
JSONL=/home/iinstances/.hermes/logs/cron-health-check.jsonl
ALARMS=/home/iinstances/.hermes/logs/cron-alarms.jsonl
HERMES_HOME_DIR=/home/iinstances/.hermes

# ── Lista declarativa de crons críticos ─────────────────────────────────
# name|log_path|expected_interval_s|tolerance_s|restart_cmd
CRONS=(
  "quota-governor-tick|${HERMES_HOME_DIR}/logs/quota-governor-tick.log|900|1350|bash ${HERMES_HOME_DIR}/scripts/quota-governor-tick.sh"
  "kanban-watchdog|${HERMES_HOME_DIR}/logs/kanban-watchdog.log|300|480|bash ${HERMES_HOME_DIR}/scripts/kanban-watchdog.sh"
  "morning-screen|${HERMES_HOME_DIR}/logs/morning-screen.log|86400|93600|/usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/obs/morning-screen.py > ${HERMES_HOME_DIR}/logs/morning-screen.log 2>&1"
  # obs-serve: daemon silencioso — su log solo crece en respawn, así que
  # log-mtime no es señal de vida; el wrapper toca obs-serve.heartbeat en
  # cada tick con el portal vivo (liveness file, patrón heartbeat).
  # Ruta real: el wrapper fija HERMES_HOME al perfil pr-ollama (pin documentado).
  "obs-serve|${HERMES_HOME_DIR}/profiles/pr-ollama/logs/obs-serve.heartbeat|300|480|bash ${HERMES_HOME_DIR}/scripts/obs-serve-cron.sh"
  # P4: efficiency ratio (hourly; wrapper deja linea timestamped por tick).
  "efficiency-ratio|${HERMES_HOME_DIR}/logs/efficiency-ratio.log|3600|5400|bash ${HERMES_HOME_DIR}/scripts/obs/efficiency-ratio-cron.sh"
  # P3: bridge Open WebUI (daemon HTTP en el puerto 9120, NO es cron). Es un
  # servicio silencioso: su log solo crece en respawn, así que log-mtime no
  # es señal de vida — el wrapper sondea GET /openapi.json (200 esperado) y
  # toca el heartbeat file en cada tick sano (patrón obs-serve.heartbeat).
  # El wrapper además converge el puerto a la copia canónica
  # ~/.hermes/scripts/bridge/open-webui-bridge.py (mata holders puente
  # legacy, respawnea desde la canónica). Ver docs/bridge-open-webui.md.
  "open-webui-bridge|${HERMES_HOME_DIR}/logs/open-webui-bridge.heartbeat|300|480|bash ${HERMES_HOME_DIR}/scripts/bridge/open-webui-bridge-cron.sh"
)

mkdir -p "$(dirname "$LOG")"
NOW=$(date +%s)
CHECKED=0; OK=0; DEAD=0; NEVER=0; ZOMBIE=0
ACTIONS="["

extract_last_ts() {
  # Último timestamp '[YYYY-MM-DD HH:MM:SS]' del log dado (epoch).
  # Fallback: si el fichero existe pero no contiene timestamp parseable
  # (heartbeat files vacíos, logs quietos de daemons), usa el mtime —
  # el wrapper que escribe/toca el fichero ES la señal de vida.
  /usr/bin/python3.12 - "$1" <<'PYINNER'
import sys, re, os
from datetime import datetime
path = sys.argv[1]
try:
    if not os.path.exists(path):
        print("NEVER"); sys.exit(0)
    last = None
    with open(path, 'rb') as fh:
        try: fh.seek(-8192, 2)
        except OSError: pass
        for line in fh.read().decode('utf-8', 'replace').splitlines():
            m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            if m: last = m.group(1)
    if not last:
        print(int(os.path.getmtime(path))); sys.exit(0)
    print(int(datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()))
except Exception:
    print("UNKNOWN")
PYINNER
}

for entry in "${CRONS[@]}"; do
  IFS='|' read -r name log interval tolerance restart_cmd <<< "$entry"
  CHECKED=$((CHECKED+1))
  LAST=$(extract_last_ts "$log")
  if [ "$LAST" = "NEVER" ] || [ "$LAST" = "UNKNOWN" ]; then
    NEVER=$((NEVER+1)); STATUS="NEVER_RUN"; ELAPSED=0; RESTART="yes"
  else
    ELAPSED=$((NOW - LAST))
    if [ "$ELAPSED" -gt "$tolerance" ]; then
      DEAD=$((DEAD+1)); STATUS="DEAD"; RESTART="yes"
    else
      OK=$((OK+1)); STATUS="OK"; RESTART="no"
    fi
  fi

  ACTION="none"; ACTION_RESULT="-"
  if [ "$STATUS" != "OK" ]; then
    if [ -f "${HERMES_HOME_DIR}/quota-governor/STOP" ]; then
      echo "$TS DIRECCION-STOP activo — $name $STATUS pero NO reiniciado" >> "$LOG"
      RESTART="no"
    fi
    if [ "$RESTART" = "yes" ]; then
      # Límite de reinicios: 3/h. A la 4a -> ZOMBIE.
      H=$(date +%H)
      STAMP="/tmp/chc-${name}-${H}"
      COUNT=$(cat "$STAMP" 2>/dev/null || echo 0)
      COUNT=$((COUNT+1))
      if [ "$COUNT" -gt 3 ]; then
        ZOMBIE=$((ZOMBIE+1)); STATUS="ZOMBIE"
        echo "$TS CRON ZOMBIE: $name — $COUNT reinicios esta hora, escala a humano" >> "$LOG"
      else
        echo "$COUNT" > "$STAMP"
        # pgrep del wrapper para no duplicar; si vive sin loguear, matar.
        PNAME=$(basename "${restart_cmd%% *}")
        PGID=$(pgrep -f "$restart_cmd" | head -1)
        [ -n "$PGID" ] && kill "$PGID" 2>/dev/null
        if eval "$restart_cmd" >> "$LOG" 2>&1; then
          ACTION="restart"; ACTION_RESULT="ok"
        else
          ACTION="restart"; ACTION_RESULT="failed"
        fi
        echo "$TS CRON $STATUS: $name — última ejecución hace ${ELAPSED}s — reiniciado ($ACTION_RESULT)" >> "$LOG"
      fi
    fi
  fi

  # Alarma JSONL (solo estados anómalos; el morning screen la consume).
  if [ "$STATUS" != "OK" ]; then
    /usr/bin/python3.12 - "$ALARMS" "$TS" "$name" "$STATUS" "$ELAPSED" "$ACTION" "$ACTION_RESULT" <<'PYINNER'
import sys, json
path, ts, name, status, elapsed, action, result = sys.argv[1:8]
entry = {"ts": ts, "cron": name, "status": status,
         "last_run_age_min": int(elapsed)//60 if elapsed.isdigit() else 0,
         "action": action, "action_result": result}
with open(path, "a") as fh:
    fh.write(json.dumps(entry) + "\n")
PYINNER
  fi

  # Acumular actions para el JSON de resumen.
  if [ "$ACTION" != "none" ]; then
    [ "$ACTIONS" != "[" ] && ACTIONS="$ACTIONS,"
    ACTIONS="$ACTIONS{\"cron\":\"$name\",\"action\":\"$ACTION\",\"result\":\"$ACTION_RESULT\"}"
  fi
done
ACTIONS="$ACTIONS]"

/usr/bin/python3.12 - "$JSONL" "$TS" "$CHECKED" "$OK" "$DEAD" "$NEVER" "$ZOMBIE" "$ACTIONS" <<'PYINNER'
import sys, json
path, ts, checked, ok, dead, never, zombie, actions = sys.argv[1:9]
entry = {"ts": ts, "checked": int(checked), "ok": int(ok), "dead": int(dead),
         "never_run": int(never), "zombie": int(zombie), "actions": json.loads(actions)}
with open(path, "a") as fh:
    fh.write(json.dumps(entry) + "\n")
print(f"[{ts}] cron-health-check: checked {checked} — {ok} OK, {dead} DEAD, {never} NEVER_RUN, {zombie} ZOMBIE")
PYINNER
