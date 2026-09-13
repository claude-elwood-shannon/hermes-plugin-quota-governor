#!/usr/bin/env python3
"""Test del endpoint GET /file del bridge (server.py v1.2).

Levanta el handler en un puerto temporal y ejecuta los 6 criterios de éxito
de la tarea + casos extra de seguridad. No toca el puerto real 9120.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "scripts", "bridge", "open-webui-bridge.py")
HERMES_HOME = os.path.expanduser("~/.hermes")

spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

srv = HTTPServer(("127.0.0.1", 0), bridge.HermesBridge)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def get(path_url):
    req = urllib.request.Request(BASE + path_url)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")


# Criterio 1: config.yaml
code, body = get("/file?path=config.yaml")
check("1 config.yaml 200", code == 200 and isinstance(body.get("content"), str)
      and len(body["content"]) > 0, f"code={code} keys={list(body)}")
check("1b config.yaml campos", body.get("path") == "config.yaml"
      and isinstance(body.get("size"), int) and isinstance(body.get("lines"), int),
      f"{body.get('path')} size={body.get('size')} lines={body.get('lines')}")

# Criterio 2: scripts/ttl_blocked.py (plugin repo)
code, body = get("/file?path=scripts/ttl_blocked.py")
check("2 scripts/ttl_blocked.py 200", code == 200 and "def " in body.get("content", ""),
      f"code={code} len={len(body.get('content',''))}")

# Criterio 3: traversal
code, body = get("/file?path=../../etc/passwd")
check("3 ../../etc/passwd 403", code == 403 and body.get("error") == "path not allowed",
      f"code={code} body={body}")

# Criterio 4: credentials.json
code, body = get("/file?path=credentials.json")
check("4 credentials.json 403", code == 403 and body.get("error") == "path not allowed",
      f"code={code} body={body}")

# Criterio 5: inexistente
code, body = get("/file?path=nonexistent.py")
check("5 nonexistent.py 404", code == 404 and body.get("error") == "file not found",
      f"code={code} body={body}")

# Extra: docs/ y tests/ del repo, profiles/, logs/
code, body = get("/file?path=docs/tick-cola-viva.md")
check("x1 docs/*.md 200", code == 200 and len(body.get("content", "")) > 100,
      f"code={code}")
_tests = sorted(f for f in os.listdir(os.path.join(bridge.PLUGIN_REPO, "tests"))
                if f.startswith("test_") and f.endswith(".py"))
code, body = get("/file?path=tests/" + _tests[0])
check("x2 tests/* 200", code == 200, f"code={code} body={str(body)[:120]}")
code, body = get("/file?path=config.yml")
check("x3 config.yml 200|404", code in (200, 404), f"code={code}")

# Extra: seguridad
code, _ = get("/file?path=/etc/passwd")
check("x4 /etc/passwd 403", code == 403, f"code={code}")
code, _ = get("/file?path=scripts/../../etc/passwd")
check("x5 scripts/../.. 403", code == 403, f"code={code}")
code, _ = get("/file?path=..%2F..%2Fetc%2Fpasswd")
check("x6 %2e%2e encoded 403", code == 403, f"code={code}")
code, _ = get("/file?path=secrets.yaml")
check("x7 secrets.yaml 403", code == 403, f"code={code}")
code, _ = get("/file?path=.env")
check("x8 .env 403", code == 403, f"code={code}")
code, _ = get("/file?path=profiles/pr-ollama/.env")
check("x9 profiles .env 403", code == 403, f"code={code}")
code, _ = get("/file?path=scripts/../logs/agent.log")
check("x10 traversal con base repo 403", code == 403, f"code={code}")
code, _ = get("/file")
check("x11 sin path 403", code == 403, f"code={code}")
code, _ = get("/file?path=auth.json")
check("x12 auth.json fuera de raices 403|404", code in (403, 404), f"code={code}")
code, _ = get("/file?path=scripts/obs")
check("x13 directorio bajo prefix 403", code == 403, f"code={code}")
code, _ = get("/file?path=scripts")
check("x13b bare prefix (dir) 404", code == 404, f"code={code}")

code, _ = get("/file?path=profiles/pr-ollama/auth.json")
check("x17 auth.json 403", code == 403, f"code={code}")

# OpenAPI spec contiene /file con operationId get_file
code, spec_json = get("/openapi.json")
check("x14 openapi /file", code == 200 and "/file" in spec_json.get("paths", {})
      and spec_json["paths"]["/file"]["get"]["operationId"] == "get_file", f"code={code}")

# Regresión: endpoints existentes siguen vivos
code, _ = get("/board")
check("x15 /board vivo", code == 200, f"code={code}")

# logs vía /file
code, body = get("/file?path=logs/quota-governor-tick.log")
check("x16 logs/tick 200", code == 200 and len(body.get("content", "")) > 0, f"code={code}")

print(f"\n== {len(passed)} PASS, {len(failed)} FAIL ==")
if failed:
    print("FAILED:", ", ".join(failed))
sys.exit(1 if failed else 0)
