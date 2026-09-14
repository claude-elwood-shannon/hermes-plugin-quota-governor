#!/usr/bin/env python3
"""sysadmin-lan — motor de guard y CLI de validación (capability guard).

Verifica cada operación SSH contra el manifest de la capacidad ANTES de
ejecutarla. Orden de evaluación (fail-closed):

  1. ¿El host está en hosts.yaml?           no → blocked (host_not_in_inventory)
  2. ¿El comando toca un `deny` del host?   sí → denied  (deny tiene prioridad)
  3. ¿Encaja en un `permissions` del host?  sí → allowed
  4. Nada encaja                            → blocked (operation not permitted
                                               by sysadmin-lan manifest)

Reglas de diseño:
- Zero dependencies: parsea el subset YAML de manifest.yaml/hosts.yaml con un
  parser propio (stdlib only), misma regla de la casa que el bridge.
- Fail-closed: cualquier error de parseo/esquema bloquea, nunca permite.
- deny tiene prioridad sobre cualquier permission: `cat ~/.ssh/id_ed25519`
  encajaría en read_files, pero edit_credentials lo deniega antes.
- Sin permiso explícito no hay acción: `pip install requests` NO está deniado
  pero tampoco permitido → blocked (distinto de denied).

CLI:
  guard.py --check-op --host gpu-host --command "systemctl --user restart vllm"
  guard.py --check-op --dest hermesuser@192.168.1.32 --command "nvidia-smi"
  guard.py --detect-trigger --title "..." --body "[capability: sysadmin-lan] ..."
  guard.py --hosts

Salida: JSON en stdout. Exit 0 = allowed, 1 = denied/blocked, 2 = error.
"""
import argparse
import json
import os
import re
import sys

# Fuente única del parser subset-YAML (compartida con el bridge).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _miniyaml  # noqa: E402

CAPABILITY_NAME = "sysadmin-lan"
CAP_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ mini YAML
# El parser vive en capabilities/_miniyaml.py (fuente única, cero deps):
# subset documentado en capabilities/README.md y tests/test_capability_guard.py.

# ------------------------------------------------------- clasificación de ops
# Deny primero (fail-closed); luego permissions; si nada encaja → blocked.

_DENY_PATTERNS = [
    ("edit_credentials", re.compile(
        r"authorized_keys|known_hosts|ssh-keygen|ssh-add|"
        r"\.ssh/id_|\.ssh/config|\bid_(rsa|ed25519|ecdsa|dsa)(\.pub)?\b|"
        r"\.gnupg|\.netrc|git-credential|\.git-credentials|/etc/shadow|"
        r"(^|\s)\.env\b|huggingface-cli\s+login|\bhf\s+auth\s+login\b|"
        r"--token(=|\s)")),
    ("change_network", re.compile(
        r"\b(iptables|ip6tables|nft|nftables|ufw|firewall-cmd|nmcli|nmtui|"
        r"netplan|resolvectl|ebtables)\b|"
        r"\bip\s+(link|addr|route|rule|tunnel|netns)\b|sysctl\s+-w|"
        r"\bbrctl\b|\btc\s+qdisc|"
        r"systemctl\s+(--user\s+)?(restart|stop|start|reload|enable|disable|"
        r"mask|edit)\s+(NetworkManager|systemd-networkd|systemd-resolved|"
        r"networking)\b")),
    ("system_packages", re.compile(
        r"(^|[;&|]|\$\(|`)\s*(sudo\s+|doas\s+)?(apt|apt-get|aptitude|dpkg|"
        r"snap|dnf|yum|zypper|pacman)\b|"
        r"\b(sudo|doas)\s+(apt|apt-get|aptitude|dpkg|snap|dnf|yum|zypper|"
        r"pacman)\b")),
    ("delete_data", re.compile(
        r"(^|[;&|]|\$\(|`|\bexec\s+|\bsudo\s+|\bdoas\s+)\s*rm\b|"
        r"\brm\s+-|\bshred\b|\bmkfs(\.\w+)?\b|\bdd\s+(if|of)=|"
        r"\btruncate\s|\bfind\b[^|;&]*(\s-delete\b|-exec\s+rm\b)|"
        r"\bsrm\b")),
]

_PERM_PATTERNS = [
    ("read_status", re.compile(
        r"\bnvidia-smi\b|\bnvidia-debugdump\b|\bnvcc\b|"
        r"\bsystemctl\s+(--user\s+)?(status|is-active|is-enabled|is-failed|"
        r"show|list-units|list-timers|list-dependencies|cat)\b|"
        r"\bjournalctl\b|\buptime\b|\bfree\b|\bdf\b|\bps\b|\btop\s+-b\b|"
        r"\bpstree\b|\bsensors\b|\blspci\b|\blsusb\b|\buname\b|"
        r"\bhostnamectl\b|\btimedatectl\b|\biostat\b|\bvmstat\b|"
        r"\bdocker\s+(ps|images|image\s+ls|info|version)\b|"
        r"\bdocker\s+volume\s+ls\b|\bss\s+-tln\b|\bcat\s+/proc/sys/")),
    ("read_files", re.compile(
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*"
        r"(cat|head|tail|less|more|grep|egrep|fgrep|rg|ls|stat|wc|file|diff|"
        r"tree|du|readlink|realpath|sha256sum|md5sum|find|"
        r"sed\b(?![^|;&]*\s-i\b)|awk\b(?![^|;&]*\s-i\b))\b")),
    ("restart_services", re.compile(
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*systemctl\s+"
        r"(--user\s+)?(restart|stop|start|reload|try-restart|enable|disable)"
        r"\s+(vllm|vllm\.service|vllm-rounds(\.timer|\.service)?)\b")),
    ("manage_models", re.compile(
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*(huggingface-cli|hf)\s+"
        r"(download|scan-cache|list)\b|"
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*vllm\b")),
    ("manage_packages", re.compile(
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*pip3?\s+"
        r"(install|download|show|list|uninstall|cache)\b[^;&|]*\bvllm\b")),
    # --- services-host (t_74f5f315): docker/compose ---
    ("restart_containers", re.compile(
        r"(^|[;&|]|\$\(|`|\bsudo\s+|\bdoas\s+)\s*docker\s+"
        r"(restart|stop|start|pause|unpause)\s+\S|"
        r"\bdocker\s+compose\s+(restart|stop|start)\b")),
    ("manage_compose", re.compile(
        r"\bdocker\s+compose\s+(up|down|stop|start|restart|pull|build|ps|"
        r"config|logs|events|top|port)\b")),
    ("deploy_services", re.compile(
        r"\bdocker\s+compose\s+(up|pull|down)\b")),
    ("manage_volumes", re.compile(
        r"\bdocker\s+volume\s+(create|ls|list|inspect)\b")),
    ("read_logs", re.compile(
        r"\bdocker\s+(logs|inspect|stats)\b|\bjournalctl\b")),
    # read_status (nvidia-smi/systemctl/df/free/ps...) ya cubre la lectura
    # básica; docker ps/images/info/version también es lectura de estado.
    # edit_vllm_config y edit_compose_files se evalúan a nivel de función.
]

# Escritura en /etc, /usr, /boot: jamás (edit_vllm_config las excluye).
_SYSTEM_WRITE_RE = re.compile(r"(/etc\b|/usr\b|/boot\b|/var/lib\b)")

_WRITE_MECH_RE = re.compile(
    r"\bsed\s+-i\b|\btee\s*(-a\s*)?\b|>>?|\bscp\b|\brsync\b|\bpatch\b|"
    r"\bcp\s+[^;&|]+\s+\S|\bmv\s+[^;&|]+\s+\S|\binstall\s")

_VLLM_TARGET_RE = re.compile(r"vllm|/data/ml/", re.IGNORECASE)


def _edit_vllm_ok(cmd):
    """edit_vllm_config: mecanismo de escritura + objetivo vLLM (o /data/ml/)
    + sin rutas de sistema."""
    return bool(_WRITE_MECH_RE.search(cmd)) \
        and bool(_VLLM_TARGET_RE.search(cmd)) \
        and not _SYSTEM_WRITE_RE.search(cmd)


# edit_compose_files (services-host, t_74f5f315): mecanismo de escritura +
# objetivo docker-compose (~/git/docker-compose/**) + sin rutas de sistema.
_COMPOSE_TARGET_RE = re.compile(
    r"docker-compose|git/docker-compose", re.IGNORECASE)
_COMPOSE_SYSTEM_RE = _SYSTEM_WRITE_RE


def _edit_compose_ok(cmd):
    return bool(_WRITE_MECH_RE.search(cmd)) \
        and bool(_COMPOSE_TARGET_RE.search(cmd)) \
        and not _COMPOSE_SYSTEM_RE.search(cmd)


def check_command(cmd, permissions, deny):
    """Devuelve {'allowed', 'verdict', 'rule', 'reason'} para un comando
    REMOTO (sin el prefijo ssh). deny primero, luego permissions, luego
    blocked."""
    for rule, rx in _DENY_PATTERNS:
        if rule in deny and rx.search(cmd):
            return {"allowed": False, "verdict": "denied", "rule": rule,
                    "reason": f"touches deny rule '{rule}' "
                              f"(sysadmin-lan hosts.yaml)"}
    for rule, rx in _PERM_PATTERNS:
        if rule in permissions and rx.search(cmd):
            return {"allowed": True, "verdict": "allowed", "rule": rule,
                    "reason": f"matches permission '{rule}'"}
    if "edit_vllm_config" in permissions and _edit_vllm_ok(cmd):
        return {"allowed": True, "verdict": "allowed",
                "rule": "edit_vllm_config",
                "reason": "write to vLLM surface (no system paths)"}
    if "edit_compose_files" in permissions and _edit_compose_ok(cmd):
        return {"allowed": True, "verdict": "allowed",
                "rule": "edit_compose_files",
                "reason": "write to compose surface (no system paths)"}
    return {"allowed": False, "verdict": "blocked", "rule": None,
            "reason": "operation not permitted by sysadmin-lan manifest"}


# --------------------------------------------------------------- inventario

def load_capability(cap_dir=None):
    """Carga y valida manifest.yaml + hosts.yaml. Lanza ValueError si el
    esquema no encaja (fail-closed)."""
    cap_dir = cap_dir or CAP_DIR
    manifest = _miniyaml.load(os.path.join(cap_dir, "manifest.yaml"))
    for key in ("name", "version", "objective", "permissions", "triggers"):
        if key not in manifest:
            raise ValueError(f"manifest.yaml sin clave requerida: {key}")
    hosts_file = manifest.get("hosts_file", "hosts.yaml")
    inv = _miniyaml.load(os.path.join(cap_dir, hosts_file))
    hosts = inv.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("hosts.yaml sin lista 'hosts'")
    for h in hosts:
        for key in ("id", "ip", "ssh_user"):
            if key not in h:
                raise ValueError(f"host sin clave requerida '{key}': {h}")
    return {"manifest": manifest, "hosts": hosts,
            "skill_path": os.path.join(cap_dir, "skill.md")}


def find_host(cap, ident):
    """Busca por id, ip o name. `ident` también acepta user@ip."""
    cand = ident.strip()
    if "@" in cand:
        cand = cand.split("@", 1)[1]
    for h in cap["hosts"]:
        if cand in (h.get("id"), h.get("ip"), h.get("name")):
            return h
    return None


def host_dest(h):
    return f"{h.get('ssh_user')}@{h['ip']}"


def check_triggers(cap, title="", body="", task_objective=""):
    """Evalúa los triggers del manifest contra título/cuerpo/objetivo de la
    TAREA. El trigger `objective: X` casa cuando el objetivo de la tarea es X
    (no el del manifest, que es solo metadato de la capacidad)."""
    matched = []
    for t in cap["manifest"].get("triggers") or []:
        if not isinstance(t, dict):
            continue
        v = t.get("task_body_contains")
        if v and v in (body or ""):
            matched.append(f"task_body_contains:{v}")
        v = t.get("task_title_contains")
        if v and v in (title or ""):
            matched.append(f"task_title_contains:{v}")
        v = t.get("objective")
        if v and v == (task_objective or ""):
            matched.append(f"objective:{v}")
    return {"active": bool(matched), "matched": matched,
            "skill_path": cap.get("skill_path")}


# --------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Guard de la capacidad sysadmin-lan (fail-closed).")
    ap.add_argument("--check-op", action="store_true",
                    help="validar una operación SSH contra el manifest")
    ap.add_argument("--detect-trigger", action="store_true",
                    help="detectar el trigger de la capacidad en una tarea")
    ap.add_argument("--hosts", action="store_true",
                    help="listar el inventario de hosts")
    ap.add_argument("--host", help="id, ip o name del host (o user@ip)")
    ap.add_argument("--command", help="comando REMOTO a validar")
    ap.add_argument("--title", default="", help="título de la tarea")
    ap.add_argument("--body", default="", help="cuerpo de la tarea")
    ap.add_argument("--objective", default="",
                    help="objetivo de la tarea (p. ej. OBJ-SYSADMIN)")
    ap.add_argument("--cap-dir", default=CAP_DIR)
    args = ap.parse_args(argv)

    try:
        cap = load_capability(args.cap_dir)
    except Exception as e:  # fail-closed: error de esquema = bloqueo
        print(json.dumps({"allowed": False, "verdict": "blocked",
                          "rule": None, "reason": f"capability load error: {e}"}))
        return 1

    if args.hosts:
        for h in cap["hosts"]:
            print(json.dumps({"id": h["id"], "ip": h["ip"],
                              "name": h.get("name"), "dest": host_dest(h),
                              "permissions": h.get("permissions"),
                              "deny": h.get("deny")}))
        return 0

    if args.detect_trigger:
        res = check_triggers(cap, args.title, args.body, args.objective)
        print(json.dumps(res))
        return 0 if res["active"] else 1

    if args.check_op:
        if not args.host or not args.command:
            print(json.dumps({"allowed": False, "verdict": "blocked",
                              "rule": None,
                              "reason": "--check-op requiere --host y "
                                        "--command"}))
            return 2
        h = find_host(cap, args.host)
        if h is None:
            print(json.dumps({
                "allowed": False, "verdict": "blocked", "rule": None,
                "reason": f"host '{args.host}' not in sysadmin-lan inventory "
                          f"(host_not_in_inventory)"}))
            return 1
        verdict = check_command(args.command, h.get("permissions") or [],
                                h.get("deny") or [])
        verdict["host"] = h["id"]
        verdict["dest"] = host_dest(h)
        print(json.dumps(verdict))
        return 0 if verdict["allowed"] else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
