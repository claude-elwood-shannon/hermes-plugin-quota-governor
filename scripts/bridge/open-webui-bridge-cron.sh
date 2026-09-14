#!/usr/bin/env bash
# open-webui-bridge-cron.sh — monitor del bridge Open WebUI (puerto 9120).
#
# §13 (MEDIATOR 2026-09-14, tarea t_800f9764): desde la llegada de la unidad
# systemd de USUARIO hermes-bridge, el ciclo de vida del bridge es de systemd
# (Restart=always, RestartSec=5, enable para boot con Linger=yes). Este
# wrapper DEJA DE RESPANNEAR: queda como
#   1. liveness probe HTTP (GET /openapi.json, 200 esperado) + heartbeat
#      file — contrato con cron-health-check.sh (P3), que NO cambia;
#   2. convergencia: si el titular del puerto es una copia puente NO
#      canónica, lo mata y empuja systemd a reconstruirlo;
#   3. empujón a systemd (nudge) si la unidad está instalada y el bridge
#      está caído — sin ella, mantiene el respawn nohup legado como
#      fallback para adoptantes sin systemd --user.
#
# Estado del servicio: HEALTHY (200 y titular canónico) |
# WRONG_HOLDER:<pids> (titular puente no canónico) | FOREIGN:<pids> (titular
# no puente: NO se toca, escalación humana) | DEAD (sin 200).
#
# Liveness = HTTP, no log-mtime: el bridge es silencioso (su log sólo crece
# en respawn), así que el wrapper toca el heartbeat file en cada tick sano
# y cron-health-check vigila el mtime del fichero (patrón obs-serve).
#
# Ruta canónica: ~/.hermes/scripts/bridge/open-webui-bridge.py
# (regla de las tres copias: repo + shared + perfil; ver docs/bridge-open-webui.md)
# Zero tokens, localhost only.

PY=/usr/bin/python3.12
BRIDGE="$HOME/.hermes/scripts/bridge/open-webui-bridge.py"
UNIT=hermes-bridge.service
PORT=9120
# El bridge deriva la raíz del plugin repo de su __file__ (portable); en una
# copia desplegada fuera del repo hay que pinarla (convención house: los
# wrappers pinnean la ruta del script del repo — portable adoptants
# regeneran el wrapper desde su propio checkout).
export BRIDGE_PLUGIN_REPO="/data/git/hermes-plugin-quota-governor"
HEARTBEAT="$HOME/.hermes/logs/open-webui-bridge.heartbeat"
LOG="$HOME/.hermes/logs/open-webui-bridge.log"

# ¿La unidad de usuario está instalada para este usuario? (cacheado por tick)
unit_installed() {
    [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT" ]
}

# systemctl --user operable desde cron/Hermes (sin sesión gráfica la sesión
# de usuario existe igualmente gracias a Linger=yes, pero el bus requiere
# XDG_RUNTIME_DIR y DBUS_SESSION_BUS_ADDRESS explícitos).
systemctl_user() {
    if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
        XDG_RUNTIME_DIR="/run/user/$(id -u)" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
            /usr/bin/systemctl --user "$@"
    else
        DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}" \
            /usr/bin/systemctl --user "$@"
    fi
}

# Estado del titular del puerto (misma clasificación que siempre).
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
    # titular puente no canónico: matar; systemd (o el fallback) reconstruye
    # desde la canónica.
    for pid in ${STATE#WRONG_HOLDER:}; do kill "$pid" 2>/dev/null; done
    sleep 1
    ;;
esac

# DEAD (o WRONG_HOLDER ya limpiado): reconstruir el bridge.
if unit_installed && systemctl_user is-active --quiet "$UNIT" 2>/dev/null; then
    # Unidad instalada y "active" pero sin 200: cuelgue silencioso — restart.
    echo "open-webui-bridge: unidad $UNIT active sin responder — systemctl --user restart"
    systemctl_user restart "$UNIT"
elif unit_installed; then
    # Unidad instalada e inactiva (o fallida): empujón — systemd reconstruye
    # y a partir de aquí REINICIA SOLO (Restart=always).
    echo "open-webui-bridge: unidad $UNIT inactiva — systemctl --user start"
    systemctl_user start "$UNIT"
else
    # Fallback legacy (sin unidad): respawn nohup desde la ruta canónica.
    # §11 (MEDIATOR 2026-09-14): contexto LIMPIO — el respawn desde el
    # health-check hereda el entorno restringido de Hermes (HERMES_* apuntan
    # al perfil del worker y el hijo no puede mutar el kanban por CLI:
    # "delegate_task child contexts cannot mutate Kanban tasks"). Aquí se
    # limpian TODAS las HERMES_* salvo las que el bridge necesita de verdad
    # (BRIDGE_PLUGIN_REPO se re-pina) y se lanza con setsid: sesión propia,
    # independiente del proceso padre. El bridge resultante puede ejecutar
    # `hermes kanban create` sin restricciones de contexto delegado.
    # §12 (MEDIATOR 2026-09-14): PATH mínimo RECONSTRUIDO, nunca PATH="$PATH".
    # El health-check corre desde cron (PATH=/usr/bin:/bin) o desde el
    # contexto restringido de Hermes (PATH sin ~/.local/bin). El bridge lanza
    # `hermes` por nombre desnudo (subprocess.run(["hermes", ...])) y hermes
    # vive en $HOME/.local/bin/hermes: con PATH="$PATH" el respawn heredaba
    # un PATH sin ~/.local/bin y TODOS los subprocess del bridge fallaban
    # con FileNotFoundError. env -i limpia el resto (HERMES_* del worker,
    # PYTHONPATH, AO_KANBAN_DB, ...); LANG se conserva si existe.
    mkdir -p "$(dirname "$LOG")"
    setsid nohup env -i \
        HOME="$HOME" \
        PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        LANG="${LANG:-C.UTF-8}" \
        HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
        BRIDGE_PLUGIN_REPO="/data/git/hermes-plugin-quota-governor" \
        "$PY" "$BRIDGE" >> "$LOG" 2>&1 &
fi

sleep 3

if "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen(f"http://localhost:{sys.argv[1]}/openapi.json", timeout=5).status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
then
    touch_heartbeat
    echo "open-webui-bridge reconstruido: http://localhost:$PORT (estaba caido)"
else
    echo "open-webui-bridge NO pudo levantarse en el puerto $PORT — revisar $LOG y 'systemctl --user status hermes-bridge'"
    exit 1
fi
