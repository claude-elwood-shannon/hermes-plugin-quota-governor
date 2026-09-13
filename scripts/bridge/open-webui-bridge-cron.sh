#!/usr/bin/env bash
# open-webui-bridge-cron.sh — supervisor del bridge Open WebUI (puerto 9120).
#
# Daemon pattern, misma familia que obs-serve-cron.sh:
#   - bridge vivo y canónico -> SILENCIO en stdout + touch del heartbeat
#     (liveness file para cron-health-check.sh; el bridge es silencioso: su
#     log solo crece en respawn, mtime NO es señal de vida).
#   - bridge caido           -> respawn desde la ruta canónica (nohup).
#   - puerto ocupado por una copia puente NO canónica (p.ej. la vieja
#     ~/git/hermes-bridge/server.py o la copia del repo) -> se mata al
#     titular y se respawnEA desde la canónica: convergencia automática a
#     la ruta canónica en cada tick, sin intervención humana.
#   - puerto ocupado por un proceso NO puente -> NO se mata: reporta y
#     sale 1 (escalación humana; el puerto 9120 es del bridge por diseño).
#
# Liveness = HTTP: GET /openapi.json debe devolver 200. Un proceso vivo que
# no responde cuenta como muerto (el titular del puerto impide rebindear).
#
# Ruta canónica: ~/.hermes/scripts/bridge/open-webui-bridge.py
# (regla de las tres copias: repo + shared + perfil; ver docs/bridge-open-webui.md)
# Zero tokens, localhost only.

PY=/usr/bin/python3.12
BRIDGE="$HOME/.hermes/scripts/bridge/open-webui-bridge.py"
PORT=9120
# El bridge deriva la raíz del plugin repo de su __file__ (portable); en una
# copia desplegada fuera del repo hay que pinarla (convención house: los
# wrappers pinnean la ruta del script del repo — portable adoptants
# regeneran el wrapper desde su propio checkout).
export BRIDGE_PLUGIN_REPO="/data/git/hermes-plugin-quota-governor"
HEARTBEAT="$HOME/.hermes/logs/open-webui-bridge.heartbeat"
LOG="$HOME/.hermes/logs/open-webui-bridge.log"

# Estado del servicio: HEALTHY (200 y el titular del puerto es la copia
# canónica) | WRONG_HOLDER:<pids> (titular puente no canónico) |
# FOREIGN:<pids> (titular no puente) | DEAD (sin 200).
STATE=$("$PY" - "$PORT" "$BRIDGE" <<'PYEOF'
import os, sys
port, bridge = int(sys.argv[1]), os.path.realpath(sys.argv[2])

# pids que sostienen el socket LISTEN del puerto (IPv4, /proc/net/tcp)
inodes = set()
try:
    with open("/proc/net/tcp") as f:
        for line in f.readlines()[1:]:
            p = line.split()
            if len(p) > 9 and p[3] == "0A" and int(p[1].split(":")[1], 16) == port:
                inodes.add(p[9])
except OSError:
    pass
holders = []
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        continue
    for fd in fds:
        try:
            tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
            holders.append(int(pid))
            break

def script_paths(pid):
    """Rutas .py del cmdline del pid, RESUELTAS (los lanzamientos con ruta
    relativa, p.ej. 'python3 server.py', se resuelven contra /proc/pid/cwd)."""
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return []
    try:
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
    except OSError:
        cwd = "/"
    out = []
    for tok in raw.split(b"\0"):
        t = tok.decode("utf-8", "replace")
        if t.endswith(".py"):
            out.append(t if os.path.isabs(t) else os.path.realpath(os.path.join(cwd, t)))
    return out

# basenames de script puente conocidos y el nombre de directorio legacy
# (~/git/hermes-bridge, /data/git/hermes-bridge — el script vivió ahí antes
# de integrarse al plugin repo)
BRIDGE_BASENAMES = ("open-webui-bridge.py", "server.py")

def kind(pid):
    paths = script_paths(pid)
    if not paths:
        return "foreign"  # no identificable: no tocar
    if any(p == bridge or p.startswith(os.path.dirname(bridge) + os.sep) for p in paths):
        return "canonical"
    # copia puente lanzada desde fuera de la ruta canónica (legacy)
    if any(os.path.basename(p) in BRIDGE_BASENAMES
           and os.path.basename(os.path.dirname(p)) == "hermes-bridge"
           for p in paths):
        return "bridge"
    return "foreign"

try:
    import urllib.request
    code = urllib.request.urlopen(f"http://localhost:{port}/openapi.json", timeout=5).status
except Exception:
    code = None

if code == 200 and holders and all(kind(p) == "canonical" for p in holders):
    print("HEALTHY")
elif holders:
    kinds = [kind(p) for p in holders]
    if "foreign" in kinds:
        print("FOREIGN:" + ",".join(map(str, holders)))
    else:
        print("WRONG_HOLDER:" + ",".join(map(str, holders)))
else:
    print("DEAD")
PYEOF
)

touch_heartbeat() { mkdir -p "$(dirname "$HEARTBEAT")"; touch "$HEARTBEAT"; }

case "$STATE" in
  HEALTHY)
    touch_heartbeat
    exit 0  # vivo y canónico — silencio
    ;;
  FOREIGN:*)
    echo "open-webui-bridge: puerto $PORT ocupado por proceso NO puente (${STATE#FOREIGN:}) — NO tocado, escalación humana"
    exit 1
    ;;
  WRONG_HOLDER:*)
    # titular puente no canónico: matar y respawnear desde la canónica
    for pid in ${STATE#WRONG_HOLDER:}; do kill "$pid" 2>/dev/null; done
    sleep 1
    ;;
esac

# DEAD (o WRONG_HOLDER ya limpiado): respawn desde la ruta canónica.
# §11 (MEDIATOR 2026-09-14): contexto LIMPIO — el respawn desde el
# health-check hereda el entorno restringido de Hermes (HERMES_* apuntan
# al perfil del worker y el hijo no puede mutar el kanban por CLI:
# "delegate_task child contexts cannot mutate Kanban tasks"). Aquí se
# limpian TODAS las HERMES_* salvo las que el bridge necesita de verdad
# (BRIDGE_PLUGIN_REPO se re-pina) y se lanza con setsid: sesión propia,
# independiente del proceso padre. El bridge resultante puede ejecutar
# `hermes kanban create` sin restricciones de contexto delegado.
mkdir -p "$(dirname "$LOG")"
setsid nohup env -i \
    HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
    BRIDGE_PLUGIN_REPO="/data/git/hermes-plugin-quota-governor" \
    "$PY" "$BRIDGE" >> "$LOG" 2>&1 &
sleep 2

if "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen(f"http://localhost:{sys.argv[1]}/openapi.json", timeout=5).status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
then
    touch_heartbeat
    echo "open-webui-bridge respawned: http://localhost:$PORT (estaba caido)"
else
    echo "open-webui-bridge NO pudo levantarse en el puerto $PORT — revisar $LOG"
    exit 1
fi
