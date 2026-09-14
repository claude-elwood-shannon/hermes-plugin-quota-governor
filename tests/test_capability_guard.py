#!/usr/bin/env python3
"""Test de la capacidad modular sysadmin-lan y de los endpoints /capabilities
del bridge (v1.5, t_2c9322f1).

Cubre el success criterion de la tarea:
  1. capabilities/sysadmin-lan/ existe con manifest.yaml, skill.md, hosts.yaml
  2. el trigger [capability: sysadmin-lan] (y title/objective) se detecta
  3. operación SSH con permiso → allowed
  4. operación SSH con deny (edit_credentials, ...) → denied
  5. operación SSH a host fuera del inventario → blocked
  6. OBJ-SYSADMIN en approved_objectives con budget 1.00 (si kanban.db local)
  7. GET /capabilities → lista de capacidades
  8. GET /capabilities/sysadmin-lan/hosts → inventario
  9. capabilities/self-governance/ existe con manifest

Además: conformance del parser _miniyaml contra PyYAML cuando está
disponible (en el bridge y el guard está PROHIBIDO usar PyYAML — regla
zero-dependencies — pero como oráculo de test es válido), y fail-closed
ante manifest roto.

Levanta el handler del bridge en un puerto temporal (convención de
test_bridge_governance_endpoints.py); no toca el 9120. El probe /status es
best-effort: valida estructura, no exige que el host esté vivo.

Uso:
  /usr/bin/python3.12 -m pytest tests/test_capability_guard.py -x
  /usr/bin/python3.12 tests/test_capability_guard.py   (fallback unittest)
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUARD = os.path.join(REPO, "capabilities", "sysadmin-lan", "guard.py")
CAPS = os.path.join(REPO, "capabilities")
BRIDGE = os.path.join(REPO, "scripts", "bridge", "open-webui-bridge.py")

spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

guard_spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
assert guard_spec is not None and guard_spec.loader is not None
guard = importlib.util.module_from_spec(guard_spec)
guard_spec.loader.exec_module(guard)

srv = HTTPServer(("127.0.0.1", 0), bridge.HermesBridge)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


def req(path_url):
    r = urllib.request.Request(BASE + path_url)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def guard_run(*args):
    r = subprocess.run([sys.executable, GUARD, *args],
                       capture_output=True, text=True, timeout=60)
    try:
        return r.returncode, json.loads(r.stdout.strip())
    except Exception:
        return r.returncode, {"raw": r.stdout, "err": r.stderr}


class TestCapabilityStructure(unittest.TestCase):
    """SC1/SC9: estructura de directorios de las capacidades."""

    def test_sysadmin_lan_files(self):
        for f in ("manifest.yaml", "skill.md", "hosts.yaml", "guard.py"):
            self.assertTrue(
                os.path.isfile(os.path.join(CAPS, "sysadmin-lan", f)),
                f"falta capabilities/sysadmin-lan/{f}")

    def test_self_governance_files(self):
        self.assertTrue(
            os.path.isfile(
                os.path.join(CAPS, "self-governance", "manifest.yaml")),
            "falta capabilities/self-governance/manifest.yaml")
        self.assertTrue(
            os.path.isfile(
                os.path.join(CAPS, "self-governance", "skill.md")))

    def test_miniyaml_module_shared(self):
        self.assertTrue(os.path.isfile(os.path.join(CAPS, "_miniyaml.py")))


class TestGuardOperations(unittest.TestCase):
    """SC3/SC4/SC5: allowed / denied / blocked."""

    def test_allowed_read_status(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "nvidia-smi --query-gpu=temperature.gpu")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "read_status")

    def test_allowed_restart_vllm(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "systemctl --user restart vllm")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "restart_services")
        self.assertEqual(d["dest"], "hermesuser@192.168.1.32")

    def test_allowed_read_files(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "tail -n 300 /data/ml/data/hermes/logs/rounds.jsonl")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))

    def test_allowed_manage_models(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "huggingface-cli download Qwen/Qwen2.5-14B-Instruct-AWQ")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "manage_models")

    def test_allowed_manage_packages(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "pip install -U vllm")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "manage_packages")

    def test_allowed_edit_vllm_config(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "sed -i 's/max-model-len 8192/max-model-len 16384/'"
                          " /data/ml/vllm/config.yaml")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "edit_vllm_config")

    def test_denied_edit_credentials(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "cat ~/.ssh/id_ed25519")
        self.assertEqual((rc, d["verdict"]), (1, "denied"))
        self.assertEqual(d["rule"], "edit_credentials")

    def test_denied_authorized_keys(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "echo 'ssh-ed25519 AAAA' >> ~/.ssh/authorized_keys")
        self.assertEqual((rc, d["verdict"]), (1, "denied"))

    def test_denied_token_login(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "huggingface-cli login --token hf_xxx")
        self.assertEqual((rc, d["verdict"]), (1, "denied"))

    def test_denied_system_packages(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "sudo apt install nvidia-driver")
        self.assertEqual((rc, d["verdict"]), (1, "denied"))
        self.assertEqual(d["rule"], "system_packages")

    def test_denied_change_network(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host", "--command",
                          "sudo iptables -L -n")
        self.assertEqual((rc, d["verdict"]), (1, "denied"))
        self.assertEqual(d["rule"], "change_network")

    def test_denied_delete_data(self):
        for cmd in ("rm -rf /data/ml/tmp",
                    "dd if=/dev/zero of=/dev/sda"):
            rc, d = guard_run("--check-op", "--host", "gpu-host",
                              "--command", cmd)
            self.assertEqual((rc, d["verdict"]), (1, "denied"), cmd)
            self.assertEqual(d["rule"], "delete_data", cmd)

    def test_denied_write_root_disk(self):
        # t_5d573ffc: host_rules services-host — respetar / y usar /data.
        # Cualquier destino de escritura absoluto fuera de /data y del
        # arbol compose ~/git/docker-compose → denied (deny prioritario).
        for cmd in ("cp /tmp/x.yml /opt/miapp/config.yml",
                    "tee /etc/motd",
                    "tee -a /var/backups/db.sql",
                    "sed -i 's/a/b/' /etc/hosts",
                    "mv /tmp/a /srv/nginx.conf",
                    "echo hola > /root/leeme.txt"):
            rc, d = guard_run("--check-op", "--host", "services-host",
                              "--command", cmd)
            self.assertEqual((rc, d["verdict"]), (1, "denied"), cmd)
            self.assertEqual(d["rule"], "write_root_disk", cmd)

    def test_allowed_data_and_compose_writes(self):
        # /data y ~/git/docker-compose estan en el disco de datos: el extractor
        # de write_root_disk NO los marca (la regla no bloquea /data). El
        # permiso de escritura si cabe es edit_compose_files; escribir en
        # /data/* suelto queda fuera del manifest (blocked, como hoy).
        for cmd in ("echo datos > /data/ml/app.conf",
                    "echo x > /data/docker/volumes/prueba/_data/f"):
            self.assertEqual(guard._root_disk_write_paths(cmd), [], cmd)
        rc, d = guard_run("--check-op", "--host", "services-host",
                          "--command",
                          "cp /tmp/n.yml "
                          "/home/iinstances/git/docker-compose/x/x.yml")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "edit_compose_files")
        rc, d = guard_run("--check-op", "--host", "services-host",
                          "--command", "docker volume create obs_prueba")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))
        self.assertEqual(d["rule"], "manage_volumes")

    def test_input_redirect_is_not_a_write(self):
        # "< fichero" es redireccion de ENTRADA: nunca cuenta como destino.
        rc, d = guard_run("--check-op", "--host", "services-host",
                          "--command",
                          "tee -a /home/iinstances/git/docker-compose/"
                          "x.yml < /tmp/patch")
        self.assertEqual((rc, d["verdict"]), (0, "allowed"))

    def test_blocked_unknown_host(self):
        for host in ("other-host", "db-host"):
            rc, d = guard_run("--check-op", "--host", host,
                              "--command", "nvidia-smi")
            self.assertEqual((rc, d["verdict"]), (1, "blocked"), host)
            self.assertIn("not in", d["reason"])

    def test_blocked_unknown_ip(self):
        rc, d = guard_run("--check-op", "--host", "10.9.9.9",
                          "--command", "nvidia-smi")
        self.assertEqual((rc, d["verdict"]), (1, "blocked"))

    def test_blocked_not_in_manifest(self):
        for cmd in ("pip install requests",          # pip sí, pero no vllm
                    "echo hello",                     # nada que lo cubra
                    "systemctl --user mask vllm",     # mask no es reversible
                    "sed -i 's/x/y/' /etc/fstab"):    # /etc jamás
            rc, d = guard_run("--check-op", "--host", "gpu-host",
                              "--command", cmd)
            self.assertEqual((rc, d["verdict"]), (1, "blocked"), cmd)

    def test_host_by_ip_and_name(self):
        for ident in ("192.168.1.32", "ml-host"):
            rc, d = guard_run("--check-op", "--host", ident,
                              "--command", "df -h")
            self.assertEqual((rc, d["verdict"]), (0, "allowed"), ident)


class TestGuardTriggers(unittest.TestCase):
    """SC2: detección del trigger y carga del skill."""

    def test_trigger_body(self):
        rc, d = guard_run("--detect-trigger", "--title", "cosa", "--body",
                          "tarea con [capability: sysadmin-lan] dentro")
        self.assertEqual(rc, 0)
        self.assertTrue(d["active"])
        self.assertTrue(os.path.isfile(d["skill_path"]))

    def test_trigger_title_gpu_host(self):
        rc, d = guard_run("--detect-trigger", "--title",
                          "Revisar gpu-host vLLM", "--body", "nada")
        self.assertEqual(rc, 0)
        self.assertTrue(d["active"])

    def test_trigger_title_ml_host(self):
        rc, d = guard_run("--detect-trigger", "--title",
                          "ml-host: temperatura", "--body", "-")
        self.assertEqual(rc, 0)
        self.assertTrue(d["active"])

    def test_trigger_objective(self):
        rc, d = guard_run("--detect-trigger", "--title", "x", "--body", "y",
                          "--objective", "OBJ-SYSADMIN")
        self.assertEqual(rc, 0)
        self.assertTrue(d["active"])

    def test_no_trigger(self):
        rc, d = guard_run("--detect-trigger", "--title", "Sin relación",
                          "--body", "nada que ver")
        self.assertEqual(rc, 1)
        self.assertFalse(d["active"])


class TestGuardFailClosed(unittest.TestCase):
    """Manifest roto / ausente → blocked, nunca allowed."""

    def test_broken_manifest_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "manifest.yaml"), "w") as fh:
                fh.write("name: roto\n  mala indent: [\n")
            rc, d = guard_run("--check-op", "--host", "gpu-host",
                              "--command", "nvidia-smi", "--cap-dir", td)
            self.assertEqual((rc, d["verdict"]), (1, "blocked"))

    def test_missing_dir_blocks(self):
        rc, d = guard_run("--check-op", "--host", "gpu-host",
                          "--command", "nvidia-smi",
                          "--cap-dir", "/tmp/no-existe-xyz")
        self.assertEqual((rc, d["verdict"]), (1, "blocked"))


class TestMiniyamlConformance(unittest.TestCase):
    """El subset propio debe replicar PyYAML en los manifests del repo
    (oráculo de test; en runtime PyYAML está PROHIBIDO en bridge/guard)."""

    def test_manifests_match_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML no disponible en este entorno")
        sys.path.insert(0, CAPS)
        import _miniyaml
        for rel in ("sysadmin-lan/manifest.yaml",
                    "sysadmin-lan/hosts.yaml",
                    "self-governance/manifest.yaml"):
            p = os.path.join(CAPS, rel)
            self.assertEqual(_miniyaml.load(p), yaml.safe_load(open(p)),
                             f"divergencia con PyYAML en {rel}")


class TestApprovedObjective(unittest.TestCase):
    """SC6: OBJ-SYSADMIN en approved_objectives con $1.00/día."""

    DB = os.path.join(os.path.expanduser("~"), ".hermes", "kanban.db")

    def test_obj_sysadmin(self):
        if not os.path.isfile(self.DB):
            self.skipTest("kanban.db no presente (CI)")
        c = sqlite3.connect(self.DB)
        row = c.execute(
            "SELECT budget_daily, status FROM approved_objectives "
            "WHERE id='OBJ-SYSADMIN'").fetchone()
        c.close()
        self.assertIsNotNone(row, "OBJ-SYSADMIN no existe")
        self.assertEqual(row[0], 1.00)
        self.assertEqual(row[1], "active")


class TestBridgeCapabilitiesEndpoints(unittest.TestCase):
    """SC7/SC8 + negative paths. /status valida estructura (best-effort)."""

    def test_openapi_v16_lists_capabilities(self):
        code, body = req("/openapi.json")
        self.assertEqual(code, 200)
        self.assertEqual(body["info"]["version"], "1.6.0")
        self.assertIn("/capabilities", body["paths"])
        self.assertIn("/capabilities/{name}/hosts", body["paths"])
        self.assertIn("/capabilities/{name}/status", body["paths"])

    def test_list_capabilities(self):
        code, body = req("/capabilities")
        self.assertEqual(code, 200)
        names = [c["name"] for c in body["capabilities"]]
        self.assertIn("sysadmin-lan", names)
        self.assertIn("self-governance", names)
        self.assertGreaterEqual(body["count"], 2)
        self.assertEqual(body["load_errors"], [])

    def test_sysadmin_lan_hosts(self):
        code, body = req("/capabilities/sysadmin-lan/hosts")
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)  # gpu-host + services-host (t_74f5f315)
        h = body["hosts"][0]
        self.assertEqual(h["id"], "gpu-host")
        self.assertEqual(h["ip"], "192.168.1.32")
        self.assertEqual(h["ssh_user"], "hermesuser")
        self.assertIn("edit_credentials", h["deny"])
        h2 = body["hosts"][1]
        self.assertEqual(h2["id"], "services-host")
        self.assertEqual(h2["ip"], "192.168.1.23")
        self.assertEqual(h2["ssh_user"], "iinstances")
        self.assertIn("system_packages", h2["deny"])

    def test_self_governance_no_hosts(self):
        code, body = req("/capabilities/self-governance/hosts")
        self.assertEqual(code, 200)
        self.assertEqual(body["hosts"], [])
        self.assertIn("note", body)

    def test_capability_status_structure(self):
        code, body = req("/capabilities/sysadmin-lan/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["capability"], "sysadmin-lan")
        self.assertEqual(len(body["hosts"]), 2)
        h = body["hosts"][0]
        self.assertEqual(h["id"], "gpu-host")
        self.assertIsInstance(h["reachable"], bool)
        for k in ("vllm_service", "vllm_rounds_timer", "gpu"):
            self.assertIn(k, h)

    def test_unknown_capability_404(self):
        code, body = req("/capabilities/no-existe/hosts")
        self.assertEqual(code, 404)
        code, _ = req("/capabilities/no-existe")
        self.assertEqual(code, 404)

    def test_traversal_and_garbage_404(self):
        for p in ("/capabilities/../../etc/passwd",
                  "/capabilities/sysadmin-lan/unknown",
                  "/capabilities/Sysadmin-Lan/hosts"):
            code, _ = req(p)
            self.assertEqual(code, 404, p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
