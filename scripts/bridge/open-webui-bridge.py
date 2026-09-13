#!/usr/bin/env python3
"""Hermes Bridge API — expone operaciones de Hermes para Open WebUI."""
import json
import subprocess
import os
import posixpath
from urllib.parse import parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

HERMES_HOME = os.path.expanduser("~/.hermes")
PLUGIN_REPO = "/data/git/hermes-plugin-quota-governor"
PORT = 9120
MAX_FILE_BYTES = 100 * 1024  # 100 KB por fichero

# raíces autorizadas (realpath de las rutas permitidas por la spec)
_PLUGIN_REPO_REAL = os.path.realpath(PLUGIN_REPO)
_ALLOWED_ROOTS = [
    os.path.join(HERMES_HOME, "scripts"),
    os.path.join(HERMES_HOME, "logs"),
    os.path.join(HERMES_HOME, "profiles"),
    _PLUGIN_REPO_REAL,
]
# config.yaml / config.yml viven directamente en ~/.hermes/
_ALLOWED_CONFIG_BASENAMES = {"config.yaml", "config.yml"}

# subcadenas prohibidas en el path (spec del mediador)
_DENIED_SUBSTRINGS = (
    "credentials", "secrets", "ssh", "id_rsa", "id_ed25519",
    ".env", "token", "apikey", "api_key",
)
# prefixes de repositorio tratados como rutas del plugin repo
_REPO_PREFIXES = ("scripts/", "docs/", "tests/")
# contienen bytes NUL => binario (heurística estándar)
_SNIFF_BYTES = 8192

OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Hermes Bridge", "version": "1.2.0",
             "description": "Bridge to Hermes Agent kanban and observability"},
    "servers": [{"url": f"http://localhost:{PORT}"}],
    "paths": {
        "/board": {"get": {"summary": "Get kanban board status", "description": "Returns kanban stats and active tasks", "operationId": "get_board", "responses": {"200": {"description": "Board status", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/snapshot": {"get": {"summary": "Get full system snapshot", "description": "Returns board, active tasks, triage, logs", "operationId": "get_snapshot", "responses": {"200": {"description": "Full snapshot", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/watchdog": {"get": {"summary": "Get watchdog log", "description": "Returns last 15 lines of kanban-watchdog.log", "operationId": "get_watchdog", "responses": {"200": {"description": "Watchdog log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/tick": {"get": {"summary": "Get tick log", "description": "Returns last 10 lines of quota-governor-tick.log", "operationId": "get_tick", "responses": {"200": {"description": "Tick log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/health": {"get": {"summary": "Get cron health-check log", "description": "Returns last 5 lines of cron-health-check.log", "operationId": "get_health", "responses": {"200": {"description": "Health check log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/efficiency": {"get": {"summary": "Get efficiency ratio", "description": "Returns last 3 lines of efficiency-ratio.log", "operationId": "get_efficiency", "responses": {"200": {"description": "Efficiency ratio", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/file": {"get": {"summary": "Read a system file", "description": "Reads a file from the Hermes system (scripts, config, docs, logs). Paths are restricted to ~/.hermes/ and the plugin repo. Security: credentials, secrets, SSH keys, and system paths are blocked.", "operationId": "get_file", "parameters": [{"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Relative path to file (e.g. scripts/ttl_blocked.py, config.yaml)"}], "responses": {"200": {"description": "File content", "content": {"application/json": {"schema": {"type": "object"}}}}, "404": {"description": "File not found"}, "403": {"description": "Path not allowed"}, "400": {"description": "Binary file"}}}},
        "/create-task": {"post": {"summary": "Create a kanban task", "description": "Creates a task in the Hermes kanban board. Default status ready, assignee pr-ollama.", "operationId": "create_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "string", "default": "mediator-prompt"}, "triage": {"type": "boolean", "default": False, "description": "Create in triage instead of ready"}}, "required": ["title", "body"]}}}}, "responses": {"200": {"description": "Task created", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/move-task": {"post": {"summary": "Move task to a new status", "description": "Moves a kanban task to a new status (e.g. triage to ready, ready to archived).", "operationId": "move_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "description": "New status: ready, triage, blocked, archived, done"}}, "required": ["task_id", "status"]}}}}, "responses": {"200": {"description": "Task moved", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/comment-task": {"post": {"summary": "Add a comment to a task", "description": "Adds a comment to a kanban task body.", "operationId": "comment_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "comment": {"type": "string"}}, "required": ["task_id", "comment"]}}}}, "responses": {"200": {"description": "Comment added", "content": {"application/json": {"schema": {"type": "object"}}}}}}}
    }
}


def _resolve_allowed(rel_path):
    """Resuelve un path relativo y devuelve (realpath | None, errores).

    Devuelve el realpath si el path cae bajo una raíz permitida; None con
    razón 'forbidden' (400/403) o 'invalid' (400) si no.
    """
    if not rel_path:
        return None, ("invalid", "missing path")
    # ruta absoluta => prohibida por diseño
    if os.path.isabs(rel_path):
        return None, ("forbidden", "absolute paths not allowed")
    # regla sintáctica: ningún segmento '..' en el path original (bloquea
    # 'base/../../etc/passwd' aunque normpath lo deje dentro de una raíz)
    if any(seg == ".." for seg in rel_path.split("/")):
        return None, ("forbidden", "path traversal not allowed")
    # normalizar con semántica POSIX; red de contención real abajo
    norm = posixpath.normpath(rel_path)
    if norm in (".", ""):
        return None, ("forbidden", "path traversal not allowed")
    low = norm.lower()
    if any(s in low for s in _DENIED_SUBSTRINGS):
        return None, ("forbidden", "denied substring in path")

    # config.yaml / config.yml: hijos directos de ~/.hermes (excepción a la
    # contención por directorio, permitidos explícitamente por la spec)
    if norm in _ALLOWED_CONFIG_BASENAMES:
        return os.path.realpath(os.path.join(HERMES_HOME, norm)), None

    # mapear el path relativo a una raíz permitida
    if norm.startswith("scripts/") or norm.startswith("docs/") or norm.startswith("tests/"):
        base = _PLUGIN_REPO_REAL
    elif norm.startswith("profiles/"):
        base = HERMES_HOME
    elif norm.startswith("logs/"):
        base = HERMES_HOME
    else:
        # fuera de los prefixes conocidos: 404 (no revela la política)
        return None, ("notfound", "no allowed root for this path")

    full = os.path.join(base, norm)
    real = os.path.realpath(full)
    # contención real (sobrevive symlinks que apunten fuera)
    allowed_dirs = [os.path.realpath(r) for r in _ALLOWED_ROOTS]
    if real != HERMES_HOME and not any(
        real == d or real.startswith(d + os.sep) for d in allowed_dirs
    ):
        return None, ("forbidden", "resolved path escapes allowed roots")
    return real, None


def _read_text_file(real_path):
    """Lee un fichero de texto con límite de 100KB.

    Devuelve (dict | None, http_code). dict = respuesta de error ya lista.
    """
    try:
        size = os.path.getsize(real_path)
        with open(real_path, "rb") as f:
            head = f.read(min(size, _SNIFF_BYTES))
        if b"\x00" in head:
            return {"error": "binary file"}, 400
        with open(real_path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except (PermissionError, OSError):
        return {"error": "file not found"}, 404

    truncated = len(raw) > MAX_FILE_BYTES
    if truncated:
        raw = raw[:MAX_FILE_BYTES]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": "binary file"}, 400

    return {
        "content": text,
        "size": size,
        "lines": text.count("\n") + (0 if text.endswith("\n") else (1 if text else 0)),
        **({"truncated": True} if truncated else {}),
    }, 200


class HermesBridge(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _run(self, cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return r.stdout if r.returncode == 0 else f"ERROR: {r.stderr}"
        except Exception as e:
            return f"ERROR: {e}"

    def _read_log(self, path, lines=15):
        full = os.path.join(HERMES_HOME, path)
        if os.path.exists(full):
            with open(full) as f:
                return "".join(f.readlines()[-lines:])
        return f"(no log: {path})"

    def _handle_file(self, query):
        params = parse_qs(query, keep_blank_values=True)
        rel = (params.get("path") or [""])[0]
        if not rel:
            return self._send_json({"error": "path not allowed", "path": rel}, 403)

        real, err = _resolve_allowed(rel)
        if err:
            reason, _msg = err
            if reason == "forbidden":
                return self._send_json({"error": "path not allowed", "path": rel}, 403)
            if reason == "notfound":
                return self._send_json({"error": "file not found", "path": rel}, 404)
            return self._send_json({"error": "path not allowed", "path": rel}, 400)
        if os.path.isdir(real):
            return self._send_json({"error": "path not allowed", "path": rel}, 403)
        if not os.path.isfile(real):
            return self._send_json({"error": "file not found", "path": rel}, 404)

        resp, code = _read_text_file(real)
        if code != 200:
            return self._send_json(resp, code)
        resp_out = {"path": rel}
        resp_out.update(resp)
        return self._send_json(resp_out, 200)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/openapi.json":
            self._send_json(OPENAPI_SPEC)
        elif path == "/file":
            self._handle_file(query)
        elif path == "/board":
            stats = self._run(["hermes", "kanban", "stats", "--json"])
            tasks = self._run(["hermes", "kanban", "list", "--json"])
            active = []
            try:
                for t in json.loads(tasks):
                    if t.get("status") in ("ready", "running", "blocked"):
                        active.append({"id": t.get("id"), "status": t.get("status"), "title": t.get("title", "")[:80]})
            except: pass
            self._send_json({"stats": stats, "active": active})
        elif path == "/snapshot":
            stats = self._run(["hermes", "kanban", "stats", "--json"])
            tasks = self._run(["hermes", "kanban", "list", "--json"])
            active, triage = [], []
            try:
                for t in json.loads(tasks):
                    s = t.get("status")
                    if s in ("ready", "running", "blocked"):
                        active.append({"id": t.get("id"), "status": s, "title": t.get("title", "")[:80]})
                    elif s == "triage":
                        triage.append({"id": t.get("id"), "tags": t.get("tags", []), "title": t.get("title", "")[:80]})
            except: pass
            self._send_json({"stats": stats, "active": active, "triage": triage,
                "watchdog": self._read_log("logs/kanban-watchdog.log", 10),
                "tick": self._read_log("logs/quota-governor-tick.log", 5),
                "health": self._read_log("logs/cron-health-check.log", 5),
                "efficiency": self._read_log("logs/efficiency-ratio.log", 3)})
        elif path == "/watchdog":
            self._send_json({"log": self._read_log("logs/kanban-watchdog.log", 15)})
        elif path == "/tick":
            self._send_json({"log": self._read_log("logs/quota-governor-tick.log", 10)})
        elif path == "/health":
            self._send_json({"log": self._read_log("logs/cron-health-check.log", 5)})
        elif path == "/efficiency":
            self._send_json({"log": self._read_log("logs/efficiency-ratio.log", 3)})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/create-task":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            title = data.get("title", "Mediator prompt")
            body = data.get("body", "")
            tags = data.get("tags", "mediator-prompt")
            use_triage = data.get("triage", False)
            full_body = f"[tags: {tags}]\n\n{body}" if tags else body
            cmd = ["hermes", "kanban", "create", title, "--body", full_body,
                   "--assignee", "pr-ollama", "--created-by", "mediator", "--json"]
            if use_triage:
                cmd.append("--triage")
            result = self._run(cmd)
            self._send_json({"result": result})
        elif self.path == "/move-task":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            task_id = data.get("task_id", "")
            status = data.get("status", "")
            result = self._run(["hermes", "kanban", "move", task_id, status, "--json"])
            self._send_json({"result": result})
        elif self.path == "/comment-task":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            task_id = data.get("task_id", "")
            comment = data.get("comment", "")
            result = self._run(["hermes", "kanban", "comment", task_id, comment, "--json"])
            self._send_json({"result": result})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silencioso: el bridge corre en primer plano sin log dedicado


def main():
    print(f"Hermes Bridge API v1.2 on http://0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), HermesBridge).serve_forever()


if __name__ == "__main__":
    main()
