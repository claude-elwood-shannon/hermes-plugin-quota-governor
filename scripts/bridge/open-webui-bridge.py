#!/usr/bin/env python3
"""Hermes Bridge API — expone operaciones de Hermes para Open WebUI.

v1.3.0 — fusión de dos tareas mediador sobre la misma base v1.2.0:
  * t_e81f0811: GET /file (lectura restringida de ficheros, preservada intacta)
  * t_3cafd196 (esta): 6 endpoints de gobernanza
      GET  /task          — body completo de una tarea por ID
      GET  /tasks         — búsqueda/filtrado (tag, status, created_by, objective, text)
      GET  /git-log       — commits recientes del plugin repo
      GET  /metrics       — metrics-history.jsonl filtrado por kind/days
      POST /approve-task  — specify+promote triage->ready + sello [APPROVAL]
      POST /verify-task   — verificación de success criterion de una tarea done
    Fix: /move-task llamaba `hermes kanban move` (subcomando inexistente en el
    CLI actual) — ahora triage->todo vía `specify`, todo/blocked->ready vía
    `promote`; otros pares se rechazan.

v1.4.0 — t_6c5233a8 (MEDIATOR 2026-09-14): 4 endpoints GET de monitorización
    de backups, SOLO LECTURA (nunca ejecutan backup/restore/prune):
      GET /backup/snapshots — lista de snapshots restic
      GET /backup/stats     — tamaño/nº ficheros del repositorio
      GET /backup/log       — últimas N líneas de backup.log (?lines=)
      GET /backup/health    — snapshot fresco (<3h), log sin ERROR (24h),
                              cron presente, tamaño del repo
    Seguridad: stderr de restic NUNCA entra en respuestas ni logs (puede
    ecoar configuración del repo); filtro final _backup_redact sobre todo
    payload /backup/* (sin endpoint S3, bucket ni credenciales); timeout
    restic 30s; credenciales por env del subprocess, nunca argv.

Restricciones: solo stdlib + hermes CLI + git CLI. Timeout 15s en subprocess
(única excepción deliberada: `kanban specify`, que lanza el LLM especificador
auxiliar con ventana propia de 120s — se le da 150s). Path traversal bloqueado.
CORS abierto para Open WebUI.
"""
import json
import os
import posixpath
import re
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

HERMES_HOME = os.path.expanduser("~/.hermes")

# --- approved_objectives (MEDIATOR 2026-09-14) -------------------------------
# kanban.db lives at the ROOT ~/.hermes (shared board); read for GET,
# write for POST /update-objective. SQLite locks serialize both writers
# (bridge + tick). env override for tests.
_AO_DB = os.environ.get("AO_KANBAN_DB") or os.path.join(HERMES_HOME, "kanban.db")
_AO_VALID_STATUSES = {"active", "achieved", "paused", "discarded"}


def _ao_list(status=None):
    import sqlite3
    if not os.path.exists(_AO_DB):
        return None
    con = sqlite3.connect(f"file:{_AO_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM approved_objectives WHERE status=? ORDER BY id",
                (status,)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM approved_objectives ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _ao_upsert(data):
    """POST /update-objective: INSERT or UPDATE of PROVIDED fields only.
    Declared columns for the bridge: id/name/budget_daily/description/
    status/success_criterion. Housekeeping (spent_*, exhausted_days) is
    tick-owned and never wiped."""
    import sqlite3
    import time as _t
    oid = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    budget = data.get("budget_daily")
    if not oid or not name or not isinstance(budget, (int, float)) \
            or isinstance(budget, bool):
        return 400, {"error": "id, name and numeric budget_daily are required"}
    status = (data.get("status") or "active").strip()
    if status not in _AO_VALID_STATUSES:
        return 400, {"error": f"invalid status {status!r} "
                              f"(valid: {sorted(_AO_VALID_STATUSES)})"}
    if not os.path.exists(_AO_DB):
        return 500, {"error": "kanban.db not found"}
    now = _t.time()
    con = None
    try:
        con = sqlite3.connect(_AO_DB)
        con.execute(
            "CREATE TABLE IF NOT EXISTS approved_objectives ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, budget_daily REAL "
            "NOT NULL DEFAULT 0.0, description TEXT, status TEXT NOT NULL "
            "DEFAULT 'active', success_criterion TEXT, spent_today REAL "
            "DEFAULT 0.0, spent_total REAL DEFAULT 0.0, created_at REAL, "
            "updated_at REAL, updated_by TEXT, exhausted_days INTEGER "
            "DEFAULT 0, last_exhausted_day TEXT)")
        row = con.execute("SELECT id FROM approved_objectives WHERE id=?",
                          (oid,)).fetchone()
        if row:
            sets, vals = ["updated_at=?", "updated_by=?"], [now, "mediator"]
            for f in ("name", "budget_daily", "description",
                      "success_criterion", "status"):
                if f in data and data[f] is not None:
                    sets.append(f"{f}=?")
                    vals.append(data[f])
            vals.append(oid)
            con.execute(f"UPDATE approved_objectives SET {', '.join(sets)} "
                        "WHERE id=?", vals)
            action = "updated"
        else:
            con.execute(
                "INSERT INTO approved_objectives (id, name, budget_daily, "
                "description, status, success_criterion, created_at, "
                "updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (oid, name, float(budget), data.get("description"), status,
                 data.get("success_criterion"), now, now, "mediator"))
            action = "created"
        con.commit()
        con.row_factory = sqlite3.Row
        out = con.execute("SELECT * FROM approved_objectives WHERE id=?",
                          (oid,)).fetchone()
        con.close()
        return 200, {"ok": True, "action": action,
                     "objective": dict(out) if out else None}
    except sqlite3.Error as exc:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        return 500, {"error": str(exc)}


# --------------------------------------------------- /backup/* (t_6c5233a8)

# Env del subprocess restic: credenciales SOLO por entorno del hijo, nunca
# argv (los argv del proceso son visibles en /proc/<pid>/cmdline). El env
# file (~/.config/restic-hermes/env, chmod 600) exporta las claves S3,
# RESTIC_PASSWORD y RESTIC_REPOSITORY — la URL del endpoint S3 vive ahí y
# no en scripts versionados (el repo GitHub es público).
_RESTIC_ENV_FILE = os.path.join(os.path.expanduser("~"), ".config",
                                "restic-hermes", "env")
_RESTIC_TIMEOUT = 30  # spec: comandos restic pueden tardar por red/S3
_BACKUP_LOG = os.path.join(HERMES_HOME, "logs", "backup.log")
_BACKUP_REJECT = ("restic_password", "aws_secret", "aws_access",
                  "s3:", "dream.io", "dreamhost")
_BACKUP_REPO_SIZE_LIMIT_MB = 100  # umbral de health (spec del mediador)
_BACKUP_MAX_SNAPSHOTS = 50        # cap de lista en /backup/snapshots


def _restic_env():
    """Carga el env file privado y devuelve dict puro para el subprocess.

    El bridge corre con env -i limpio (wrapper §12): las credenciales no
    existen en su entorno y hay que leerlas del fichero. Devuelve None si
    falta el fichero o no define RESTIC_REPOSITORY.
    """
    if not os.path.isfile(_RESTIC_ENV_FILE):
        return None
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}
    try:
        with open(_RESTIC_ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("export "):
                    continue
                key, _, val = line[len("export "):].partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    env[key] = val
    except OSError:
        return None
    if not env.get("RESTIC_REPOSITORY"):
        return None
    return env


def _run_restic(args):
    """restic --json -> (datos | None, detail). El stderr de restic NUNCA
    se devuelve ni se loguea (puede ecoar configuración del repo); en
    fallo, detail describe la causa sin contenido del stderr."""
    env = _restic_env()
    if env is None:
        return None, "restic environment not available"
    try:
        r = subprocess.run(["restic", *args], capture_output=True,
                           text=True, timeout=_RESTIC_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return None, f"restic timed out after {_RESTIC_TIMEOUT}s"
    except FileNotFoundError:
        return None, "restic binary not found"
    except Exception as e:
        return None, f"restic execution error: {type(e).__name__}"
    if r.returncode != 0 or not r.stdout.strip():
        return None, f"restic exited with code {r.returncode}"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, "restic returned invalid JSON"


def _backup_redact(data):
    """Filtro final de TODO payload /backup/*: elimina recursivamente claves
    sensibles y reemplaza cualquier substring de configuración del repo en
    strings (defensa en profundidad; el stderr de restic jamás entra)."""
    def clean_str(s):
        low = s.lower()
        if any(tok in low for tok in _BACKUP_REJECT):
            return "[REDACTED]"
        return s

    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in _BACKUP_REJECT:
                continue
            out[k] = _backup_redact(v)
        return out
    if isinstance(data, list):
        return [_backup_redact(v) for v in data]
    if isinstance(data, str):
        return clean_str(data)
    return data


def _backup_parse_ts(v):
    """Parsea timestamps ISO8601 de restic ('...Z' u offset '+02:00',
    fracción de 9 dígitos) a epoch seconds. A diferencia de _parse_ts,
    maneja offsets no-Z (restic 0.16 emite +02:00) -> si no, el health
    vería age=None y last_snapshot_fresh=False siempre."""
    if not isinstance(v, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            v.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# raíz del plugin repo: derivada del script (copia repo: <repo>/scripts/bridge/
# -> <repo>). Adoptantes que corren una copia desplegada fuera del repo fijan
# BRIDGE_PLUGIN_REPO (el wrapper del house la exporta).
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_REPO = os.environ.get("BRIDGE_PLUGIN_REPO") or os.path.dirname(
    os.path.dirname(_PLUGIN_DIR)
)
PORT = 9120
MAX_FILE_BYTES = 100 * 1024  # 100 KB por fichero
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
METRICS_JSONL = os.path.join(
    HERMES_HOME, "profiles", "pr-ollama", "quota-governor", "metrics-history.jsonl")
DEFAULT_GIT_REPO = PLUGIN_REPO
VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running",
                  "blocked", "review", "done", "archived"}
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
OBJ_RE = re.compile(r"OBJ-\d+", re.I)

CRITERION_RE = re.compile(
    r"(?im)^[#*\s]*\**(?:criterio de (?:é|e)xito|success criterion|"
    r"criterio de completitud|criterios de aceptaci[oó]n|criterio de cierre)\**\s*:?\s*(.*)$")

# raíces autorizadas (realpath de las rutas permitidas por la spec) — /file
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
# ficheros de credenciales por nombre (denegados en TODAS las raíces:
# p.ej. profiles/pr-ollama/auth.json es el store de credenciales de Hermes)
_DENIED_BASENAMES = {"auth.json", "credentials.json", ".git-credentials"}
# prefixes de repositorio tratados como rutas del plugin repo
_REPO_PREFIXES = ("scripts/", "docs/", "tests/")
# contienen bytes NUL => binario (heurística estándar)
_SNIFF_BYTES = 8192

OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Hermes Bridge", "version": "1.4.0",
             "description": "Bridge to Hermes Agent kanban and observability"},
    "servers": [{"url": f"http://localhost:{PORT}"}],
    "paths": {
        # ---- legacy v1.1 (sin cambios de contrato) ----
        "/board": {"get": {"summary": "Get kanban board status", "description": "Returns kanban stats and active tasks", "operationId": "get_board", "responses": {"200": {"description": "Board status", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/snapshot": {"get": {"summary": "Get full system snapshot", "description": "Returns board, active tasks, triage, logs", "operationId": "get_snapshot", "responses": {"200": {"description": "Full snapshot", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/watchdog": {"get": {"summary": "Get watchdog log", "description": "Returns last 15 lines of kanban-watchdog.log", "operationId": "get_watchdog", "responses": {"200": {"description": "Watchdog log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/tick": {"get": {"summary": "Get tick log", "description": "Returns last 10 lines of quota-governor-tick.log", "operationId": "get_tick", "responses": {"200": {"description": "Tick log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/health": {"get": {"summary": "Get cron health-check log", "description": "Returns last 5 lines of cron-health-check.log", "operationId": "get_health", "responses": {"200": {"description": "Health check log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/efficiency": {"get": {"summary": "Get efficiency ratio", "description": "Returns last 3 lines of efficiency-ratio.log", "operationId": "get_efficiency", "responses": {"200": {"description": "Efficiency ratio", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        # ---- t_e81f0811: /file (preservado intacto) ----
        "/file": {"get": {"summary": "Read a system file", "description": "Reads a file from the Hermes system (scripts, config, docs, logs). Paths are restricted to ~/.hermes/ and the plugin repo. Security: credentials, secrets, SSH keys, and system paths are blocked.", "operationId": "get_file", "parameters": [{"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Relative path to file (e.g. scripts/ttl_blocked.py, config.yaml)"}], "responses": {"200": {"description": "File content", "content": {"application/json": {"schema": {"type": "object"}}}}, "404": {"description": "File not found"}, "403": {"description": "Path not allowed"}, "400": {"description": "Binary file"}}}},
        "/create-task": {"post": {"summary": "Create a kanban task", "description": "Creates a task in the Hermes kanban board. Default status ready, assignee pr-ollama.", "operationId": "create_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "string", "default": "mediator-prompt"}, "triage": {"type": "boolean", "default": False, "description": "Create in triage instead of ready"}}, "required": ["title", "body"]}}}}, "responses": {"200": {"description": "Task created", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/move-task": {"post": {"summary": "Move task between specific status pairs", "description": "triage->todo via specify; todo/blocked->ready via promote. Other pairs refused (the hermes CLI has no generic move).", "operationId": "move_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "description": "Target status: todo (from triage) or ready (from todo/blocked)"}}, "required": ["task_id", "status"]}}}}, "responses": {"200": {"description": "Task moved", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/comment-task": {"post": {"summary": "Add a comment to a task", "description": "Adds a comment to a kanban task.", "operationId": "comment_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "comment": {"type": "string"}}, "required": ["task_id", "comment"]}}}}, "responses": {"200": {"description": "Comment added", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        # ---- t_3cafd196: 6 herramientas de gobernanza ----
        "/task": {"get": {"summary": "Get full task details by ID", "description": "Returns the complete task body, status, assignee, and all metadata for a specific task.", "operationId": "get_task", "parameters": [{"name": "task_id", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Task ID (e.g. t_ddf98fc2)"}], "responses": {"200": {"description": "Task details", "content": {"application/json": {"schema": {"type": "object"}}}}, "404": {"description": "Task not found"}}}},
        "/tasks": {"get": {"summary": "Search and filter tasks", "description": "Filter tasks by tag, status, created_by, objective, or text search. All parameters optional and combined with AND.", "operationId": "search_tasks", "parameters": [{"name": "tag", "in": "query", "required": False, "schema": {"type": "string"}}, {"name": "status", "in": "query", "required": False, "schema": {"type": "string"}}, {"name": "created_by", "in": "query", "required": False, "schema": {"type": "string"}}, {"name": "objective", "in": "query", "required": False, "schema": {"type": "string"}, "description": "Filter by OBJ-XX pattern in title"}, {"name": "text", "in": "query", "required": False, "schema": {"type": "string"}, "description": "Case-insensitive substring in title or body"}, {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 50}}], "responses": {"200": {"description": "Filtered tasks", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/git-log": {"get": {"summary": "Get recent git commits from plugin repo", "description": "Returns recent commits from the Hermes plugin repository to verify deployments and changes.", "operationId": "get_git_log", "parameters": [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 10}}], "responses": {"200": {"description": "Git log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/metrics": {"get": {"summary": "Get metrics history", "description": "Reads metrics-history.jsonl and filters by kind and time window.", "operationId": "get_metrics", "parameters": [{"name": "kind", "in": "query", "required": False, "schema": {"type": "string"}}, {"name": "days", "in": "query", "required": False, "schema": {"type": "integer", "default": 7}}], "responses": {"200": {"description": "Metrics history", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/approve-task": {"post": {"summary": "Atomically approve a triage task (move to ready + stamp approval)", "description": "Moves triage->todo (specify) then todo->ready (promote), then stamps an [APPROVAL: approved <ts>] comment. Reports moved/stamped booleans per step; non-transactional (a failed comment can be retried).", "operationId": "approve_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "note": {"type": "string", "description": "Optional approval note"}}, "required": ["task_id"]}}}}, "responses": {"200": {"description": "Approval result", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/verify-task": {"post": {"summary": "Verify task success criterion against evidence", "description": "Reads a completed task's body, extracts the declared success criterion, searches for evidence of fulfillment, and returns a verdict (PASS|FAIL|INCONCLUSIVE|NO_CRITERION|NOT_DONE).", "operationId": "verify_task", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}}}, "responses": {"200": {"description": "Verification result", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/objectives": {"get": {"summary": "Get approved objectives inventory", "description": "Returns all approved objectives with their status, budget, and spending. Filter by status with optional parameter.", "operationId": "get_objectives", "parameters": [{"name": "status", "in": "query", "required": False, "schema": {"type": "string"}}], "responses": {"200": {"description": "Objectives list", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/update-objective": {"post": {"summary": "Create or update an approved objective", "description": "Inserts a new objective or updates an existing one. Used by mediator to manage the objectives inventory.", "operationId": "update_objective", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "budget_daily": {"type": "number"}, "description": {"type": "string"}, "success_criterion": {"type": "string"}, "status": {"type": "string", "default": "active"}}, "required": ["id", "name", "budget_daily"]}}}}, "responses": {"200": {"description": "Objective created or updated", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        # ---- t_6c5233a8: backup monitoring (read-only) ----
        "/backup/snapshots": {"get": {"summary": "Get recent backup snapshots", "description": "Returns recent restic snapshots. Does not expose credentials or repository URL.", "operationId": "get_backup_snapshots", "responses": {"200": {"description": "Snapshots list", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/backup/stats": {"get": {"summary": "Get backup repository stats", "description": "Returns restic repo size and file count. Does not expose credentials.", "operationId": "get_backup_stats", "responses": {"200": {"description": "Repo stats", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/backup/log": {"get": {"summary": "Get backup log", "description": "Returns last N lines of backup.log", "operationId": "get_backup_log", "parameters": [{"name": "lines", "in": "query", "required": False, "schema": {"type": "integer", "default": 30}}], "responses": {"200": {"description": "Backup log", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/backup/health": {"get": {"summary": "Check backup system health", "description": "Verifies last snapshot age, log errors, cron presence, and repo size.", "operationId": "get_backup_health", "responses": {"200": {"description": "Health status", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
    }
}


# ------------------------------------------------- /file (t_e81f0811, intacto)

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
    if posixpath.basename(low) in _DENIED_BASENAMES:
        return None, ("forbidden", "denied credential filename")

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


# ------------------------------------ kanban (t_3cafd196): acceso a datos

def _load_tasks():
    """Tasks from `hermes kanban list --json`; falls back to a read-only
    SQLite query (same board) when the CLI is blocked/unavailable."""
    try:
        r = subprocess.run(["hermes", "kanban", "list", "--json"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return _load_tasks_sqlite()


def _load_tasks_sqlite():
    # import perezoso y opcional: sqlite3 es stdlib pero algunas builds
    # (p.ej. pyenv 3.10 de este host) se compilan sin _sqlite3; que no
    # tumbe el bridge entero — el fallback simplemente devuelve [].
    try:
        import sqlite3
    except Exception:
        return []
    try:
        conn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, title, body, assignee, status, priority, created_by, "
                "created_at, started_at, completed_at, result "
                "FROM tasks").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _find_task(task_id):
    try:
        r = subprocess.run(["hermes", "kanban", "show", task_id, "--json"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            if isinstance(data, dict) and data.get("task"):
                return data["task"]
            if isinstance(data, dict) and data.get("id"):
                return data
    except Exception:
        pass
    for t in _load_tasks_sqlite():
        if t.get("id") == task_id:
            return t
    return None


def _extract_tags(task):
    """La board no tiene columna tags estructurada: las tags viven en la
    primera línea del body como `[tags: a, b]`."""
    tags = task.get("tags")
    if isinstance(tags, list) and tags:
        return [str(x).strip() for x in tags if str(x).strip()]
    body_head = (task.get("body") or "")[:200]
    m = re.match(r"^\s*\[tags?:\s*([^\]]+)\]", body_head, re.I)
    if m:
        return [x.strip() for x in m.group(1).split(",") if x.strip()]
    return []


def _task_public(t):
    return {
        "id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
        "assignee": t.get("assignee"), "created_by": t.get("created_by"),
        "created_at": t.get("created_at"), "tags": _extract_tags(t),
    }


def _filter_tasks(tasks, tag=None, status=None, created_by=None, objective=None,
                  text=None, limit=50):
    out = []
    for t in tasks:
        if tag is not None and tag not in _extract_tags(t):
            continue
        if status is not None and (t.get("status") or "").lower() != status.lower():
            continue
        if created_by is not None and (t.get("created_by") or "").lower() != created_by.lower():
            continue
        if objective is not None:
            objs = {o.upper() for o in OBJ_RE.findall(t.get("title") or "")}
            if objective.upper() not in objs:
                continue
        if text is not None:
            hay = ((t.get("title") or "") + "\n" + (t.get("body") or "")).lower()
            if text.lower() not in hay:
                continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _parse_ts(v):
    """ISO8601 ('...Z' o offset) o epoch numérico -> epoch seconds, si no None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            try:
                from datetime import datetime
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        try:
            return float(s)
        except Exception:
            return None
    return None


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _safe_join(base, name):
    """Resuelve base/name y rechaza cualquier cosa que escape de base."""
    base = os.path.realpath(base)
    p = os.path.realpath(os.path.join(base, name))
    if p != base and not p.startswith(base + os.sep):
        return None
    return p


# --------------------------------------- verify-task (t_3cafd196): lógica

def _verify_logic(task):
    status = (task.get("status") or "").lower()
    if status != "done":
        return {"verdict": "NOT_DONE", "status": status or None}
    body = task.get("body") or ""
    result = task.get("result") or ""
    body_l = body.lower()
    result_l = result.lower()

    criterion = None
    m = CRITERION_RE.search(body)
    if m:
        parts = [m.group(1).strip()]
        for ln in body[m.end():].splitlines():
            s = ln.strip()
            if not s:
                if parts and parts[-1]:
                    break
                continue
            if s.startswith("#") or re.match(r"^-{3,}$", s):
                break
            parts.append(s)
            if sum(len(x) for x in parts) > 300 or len(parts) >= 5:
                break
        criterion = " ".join(p for p in parts if p).strip() or None
    if criterion is None:
        for ln in body.splitlines():
            ll = ln.lower()
            if "criterio" in ll or "criterion" in ll:
                criterion = ln.strip() or None
                break

    if criterion is None:
        return {"verdict": "NO_CRITERION"}

    strong = ("verificado", "verified", "completado", "completed", "passing",
              "tests pass", "pytest", "exit_code 0", "exit 0", "sha256", "md5")
    weak = ("pass", "done")
    hay = body_l + "\n" + result_l
    strong_hits = [w for w in strong if w in hay]
    links = re.findall(r"https?://\S+|/[a-z0-9_./-]{4,}\.(?:log|json|md|txt)",
                       body_l + "\n" + result_l)
    has_marker = "[done-verify-skip]" in body_l

    evidence = []
    if has_marker:
        evidence.append("done-verify-skip marker")
    if strong_hits:
        evidence.append("completion words: " + ", ".join(sorted(set(strong_hits))[:5]))
    if links:
        evidence.append(f"{len(links)} link(s) to tests/logs/artifacts")

    if not evidence:
        weak_hits = [w for w in weak if w in hay]
        verdict = "FAIL" if not weak_hits else "INCONCLUSIVE"
    elif has_marker or strong_hits:
        verdict = "PASS"
    else:
        verdict = "INCONCLUSIVE"

    return {
        "task_id": task.get("id"), "status": "done",
        "success_criterion": criterion[:300],
        "evidence_found": ("; ".join(evidence))[:300] or None,
        "verdict": verdict,
    }


# ------------------------------------------------------------------- server

class HermesBridge(BaseHTTPRequestHandler):
    server_version = "HermesBridge/1.4"

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _run(self, cmd, timeout=15):
        # 15s por defecto (restricción de tarea); única excepción deliberada:
        # `kanban specify`, que lanza el LLM especificador auxiliar (ventana
        # propia de 120s) — un cap de 15s lo aborta a mitad de operación.
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else f"ERROR: {r.stderr}"
        except Exception as e:
            return f"ERROR: {e}"

    def _read_log(self, path, lines=15):
        full = _safe_join(HERMES_HOME, path)
        if full and os.path.isfile(full):
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    return "".join(f.readlines()[-lines:])
            except Exception as e:
                return f"(error reading {path}: {e})"
        return f"(no log: {path})"

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

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
        qs = parse_qs(query)
        if path == "/openapi.json":
            self._send_json(OPENAPI_SPEC)
        elif path == "/file":
            self._handle_file(query)
        elif path == "/board":
            stats = self._run(["hermes", "kanban", "stats", "--json"])
            tasks = _load_tasks()
            active = [{"id": t.get("id"), "status": t.get("status"),
                       "title": (t.get("title") or "")[:80]}
                      for t in tasks
                      if t.get("status") in ("ready", "running", "blocked")]
            self._send_json({"stats": stats, "active": active})
        elif path == "/snapshot":
            stats = self._run(["hermes", "kanban", "stats", "--json"])
            tasks = _load_tasks()
            active, triage = [], []
            for t in tasks:
                s = t.get("status")
                if s in ("ready", "running", "blocked"):
                    active.append({"id": t.get("id"), "status": s,
                                   "title": (t.get("title") or "")[:80]})
                elif s == "triage":
                    triage.append({"id": t.get("id"), "tags": _extract_tags(t),
                                   "title": (t.get("title") or "")[:80]})
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
        elif path == "/backup/snapshots":
            self._handle_backup_snapshots()
        elif path == "/backup/stats":
            self._handle_backup_stats()
        elif path == "/backup/log":
            self._handle_backup_log(qs)
        elif path == "/backup/health":
            self._handle_backup_health()
        elif path == "/task":
            self._handle_get_task(qs)
        elif path == "/tasks":
            self._handle_search_tasks(qs)
        elif path == "/git-log":
            self._handle_git_log(qs)
        elif path == "/metrics":
            self._handle_metrics(qs)
        elif path == "/objectives":
            status = (qs.get("status") or [None])[0]
            rows = _ao_list(status)
            if rows is None:
                self._send_json({"error": "approved_objectives unavailable "
                                          "(kanban.db missing or table absent)"}, 503)
            else:
                self._send_json({"objectives": rows, "count": len(rows)})
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- v1.3 GET: gobernanza ----

    def _handle_get_task(self, qs):
        task_id = (qs.get("task_id") or [""])[0].strip()
        if not task_id:
            return self._send_json({"error": "missing task_id"}, 400)
        if not TASK_ID_RE.match(task_id):
            return self._send_json({"error": "invalid task_id format",
                                    "hint": "expected t_xxxxxxxx"}, 400)
        t = _find_task(task_id)
        if not t:
            return self._send_json({"error": "task not found",
                                    "task_id": task_id}, 404)
        t = dict(t)
        t["tags"] = _extract_tags(t)
        self._send_json(t)

    def _handle_search_tasks(self, qs):
        def q1(name):
            v = (qs.get(name) or [""])[0].strip()
            return v or None

        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except Exception:
            limit = 50
        limit = max(1, min(limit, 500))
        status = q1("status")
        if status and status.lower() not in VALID_STATUSES:
            return self._send_json({"error": f"invalid status: {status}",
                                    "valid": sorted(VALID_STATUSES)}, 400)
        tasks = _load_tasks()
        filtered = _filter_tasks(
            tasks, tag=q1("tag"), status=status, created_by=q1("created_by"),
            objective=q1("objective"), text=q1("text"), limit=limit)
        self._send_json({"tasks": [_task_public(t) for t in filtered],
                         "count": len(filtered)})

    def _handle_git_log(self, qs):
        try:
            limit = int((qs.get("limit") or ["10"])[0])
        except Exception:
            limit = 10
        limit = max(1, min(limit, 100))
        repo = DEFAULT_GIT_REPO
        if not os.path.isdir(repo):
            return self._send_json({"error": f"repo not found: {repo}",
                                    "commits": [], "count": 0}, 404)
        fmt = {"hash": "%H", "short_hash": "%h", "author": "%an",
               "date": "%ad", "message": "%s"}
        cmd = ["git", "-C", repo, "log", f"--pretty=format:{json.dumps(fmt)}",
               f"-{limit}"]
        out = self._run(cmd)
        if out.startswith("ERROR:"):
            return self._send_json({"error": out, "commits": [], "count": 0}, 500)
        commits = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                commits.append(json.loads(line))
            except Exception:
                continue
        self._send_json({"commits": commits, "count": len(commits)})

    def _handle_metrics(self, qs):
        try:
            days = int((qs.get("days") or ["7"])[0])
        except Exception:
            days = 7
        days = max(1, min(days, 365))
        kind = (qs.get("kind") or [None])[0]
        cutoff = time.time() - days * 86400
        rows = []
        for o in _read_jsonl(METRICS_JSONL):
            if kind is not None and o.get("kind") != kind:
                continue
            ts = _parse_ts(o.get("ts"))
            if ts is None or ts < cutoff:
                continue
            rows.append(o)
        rows.sort(key=lambda o: _parse_ts(o.get("ts")) or 0)
        self._send_json({"metrics": rows, "count": len(rows),
                         "kind": kind, "days": days})

    # ---- v1.4 GET: backup monitoring (t_6c5233a8, SOLO LECTURA) ----
    # Nunca ejecutan backup/restore/prune; stderr de restic jamás entra
    # en las respuestas; todo payload pasa por _backup_redact.

    def _handle_backup_snapshots(self):
        snaps, err = _run_restic(["snapshots", "--json"])
        if err:
            return self._send_json({"error": "restic snapshots unavailable",
                                    "detail": err}, 503)
        if not isinstance(snaps, list):
            return self._send_json({"error": "restic snapshots unavailable",
                                    "detail": "unexpected restic output"}, 503)
        snaps = [s for s in snaps if isinstance(s, dict)]
        snaps.sort(key=lambda s: _backup_parse_ts(s.get("time")) or 0)
        last = snaps[-1] if snaps else None
        payload = {"snapshots": snaps[-_BACKUP_MAX_SNAPSHOTS:],
                   "count": len(snaps),
                   "last_time": (last or {}).get("time")}
        self._send_json(_backup_redact(payload))

    def _handle_backup_stats(self):
        stats, err = _run_restic(["stats", "--json"])
        if err or not isinstance(stats, dict):
            return self._send_json({"error": "restic stats unavailable",
                                    "detail": err or "unexpected restic output"}, 503)
        payload = {"stats": stats,
                   "total_size": stats.get("total_size"),
                   "total_files": stats.get("total_file_count")}
        self._send_json(_backup_redact(payload))

    def _handle_backup_log(self, qs):
        try:
            lines = int((qs.get("lines") or ["30"])[0])
        except Exception:
            lines = 30
        lines = max(1, min(lines, 500))
        self._send_json(_backup_redact(
            {"log": self._read_log("logs/backup.log", lines),
             "lines": lines}))

    def _handle_backup_health(self):
        checks = {}
        # 1. ¿último snapshot en las últimas 3h?
        snaps, err = _run_restic(["snapshots", "--json"])
        last_time, age = None, None
        if not err and isinstance(snaps, list):
            best_ts = None
            for s in snaps:
                if not isinstance(s, dict):
                    continue
                ts = _backup_parse_ts(s.get("time"))
                if ts is not None and (best_ts is None or ts > best_ts):
                    best_ts, last_time = ts, s.get("time")
            if best_ts is not None:
                age = round((time.time() - best_ts) / 3600.0, 2)
        checks["last_snapshot_fresh"] = bool(
            age is not None and age <= 3.0)
        # 2. ¿log sin ERROR en las últimas 24h? (ERROR bajo cabecera datada)
        cutoff = time.time() - 24 * 3600
        log_errors = 0
        header_ts = None
        try:
            with open(_BACKUP_LOG, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(
                        r"=== Backup (?:started|completed): "
                        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        try:
                            from datetime import datetime
                            header_ts = datetime.strptime(
                                m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                        except Exception:
                            pass
                        continue
                    if "ERROR" in line and header_ts is not None \
                            and header_ts >= cutoff:
                        log_errors += 1
        except OSError:
            pass
        checks["log_clean_24h"] = (log_errors == 0)
        # 3. ¿cron presente en crontab?
        cron_present = False
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True,
                               text=True, timeout=10)
            cron_present = (r.returncode == 0
                            and "hermes-backup" in (r.stdout or ""))
        except Exception:
            pass
        checks["cron_present"] = cron_present
        # 4. ¿repo < límite? (raw-data: lo que el bucket almacena de verdad;
        # dominado por state.db — el umbral de 100MB de la spec es antiguo)
        stats, err2 = _run_restic(["stats", "--mode", "raw-data", "--json"])
        repo_mb = None
        if isinstance(stats, dict) \
                and isinstance(stats.get("total_size"), (int, float)):
            repo_mb = round(stats["total_size"] / (1024 * 1024), 1)
        checks["repo_size_ok"] = bool(
            repo_mb is not None and repo_mb < _BACKUP_REPO_SIZE_LIMIT_MB)
        payload = {"healthy": all(checks.values()),
                   "checks": checks,
                   "last_snapshot": last_time,
                   "last_snapshot_age_hours": age,
                   "log_errors": log_errors,
                   "cron_present": cron_present,
                   "repo_size_mb": repo_mb,
                   "repo_size_limit_mb": _BACKUP_REPO_SIZE_LIMIT_MB}
        self._send_json(_backup_redact(payload))

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path == "/create-task":
            data = self._read_body()
            title = data.get("title", "Mediator prompt")
            body = data.get("body", "")
            tags = data.get("tags", "mediator-prompt")
            use_triage = data.get("triage", False)
            full_body = f"[tags: {tags}]\n\n{body}" if tags else body
            cmd = ["hermes", "kanban", "create", title, "--body", full_body,
                   "--assignee", "pr-ollama", "--created-by", "mediator", "--json"]
            if use_triage:
                cmd.append("--triage")
            self._send_json({"result": self._run(cmd)})
        elif path == "/move-task":
            self._handle_move_task(self._read_body())
        elif path == "/comment-task":
            data = self._read_body()
            # `kanban comment` no acepta --json; la salida de éxito es
            # "Comment added to <id>"
            result = self._run(["hermes", "kanban", "comment",
                                data.get("task_id", ""), data.get("comment", "")])
            self._send_json({"result": result,
                             "ok": result.startswith("Comment added")})
        elif path == "/approve-task":
            self._handle_approve_task(self._read_body())
        elif path == "/verify-task":
            self._handle_verify_task(self._read_body())
        elif path == "/update-objective":
            code, resp = _ao_upsert(self._read_body())
            self._send_json(resp, code)
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- v1.3 POST: move-task fix + gobernanza ----

    def _handle_move_task(self, data):
        task_id = (data.get("task_id") or "").strip()
        status = (data.get("status") or "").strip().lower()
        if not TASK_ID_RE.match(task_id):
            return self._send_json({"moved": False,
                                    "error": "invalid task_id format",
                                    "hint": "expected t_xxxxxxxx"}, 400)
        if status not in ("todo", "ready"):
            return self._send_json(
                {"moved": False, "task_id": task_id, "status": status,
                 "error": "unsupported target status; supported: "
                          "triage->todo (specify), todo/blocked->ready (promote)"},
                400)
        results = {"task_id": task_id, "requested": status, "steps": []}
        cur = (_find_task(task_id) or {}).get("status")
        results["current_status"] = cur
        if status == "todo":
            if cur != "triage":
                return self._send_json({"moved": False, "task_id": task_id,
                                        "error": f"specify requires triage, task is {cur!r}"}, 409)
            out = self._run(["hermes", "kanban", "specify", task_id,
                             "--author", "mediator", "--json"], timeout=150)
            results["steps"].append({"step": "specify", "output": out[:300]})
        else:  # ready
            if cur not in ("todo", "blocked"):
                return self._send_json({"moved": False, "task_id": task_id,
                                        "error": f"promote requires todo/blocked, task is {cur!r}"}, 409)
            out = self._run(["hermes", "kanban", "promote", task_id,
                             "moved via bridge /move-task", "--json"])
            results["steps"].append({"step": "promote", "output": out[:300]})
        new = (_find_task(task_id) or {}).get("status")
        results["moved"] = (new == status)
        results["new_status"] = new
        self._send_json(results)

    def _handle_approve_task(self, data):
        task_id = (data.get("task_id") or "").strip()
        note = (data.get("note") or "").strip()
        if not TASK_ID_RE.match(task_id):
            return self._send_json({"task_id": task_id, "moved": False,
                                    "stamped": False,
                                    "error": "invalid task_id format",
                                    "hint": "expected t_xxxxxxxx"}, 400)
        t = _find_task(task_id)
        if not t:
            return self._send_json({"task_id": task_id, "moved": False,
                                    "stamped": False,
                                    "error": "task not found"}, 404)
        moved = False
        move_err = None
        cur = t.get("status")
        if cur == "ready":
            moved = True
        elif cur == "triage":
            out = self._run(["hermes", "kanban", "specify", task_id,
                             "--author", "mediator", "--json"], timeout=150)
            if out.startswith("ERROR:"):
                move_err = f"specify failed: {out[:200]}"
            else:
                t2 = _find_task(task_id) or {}
                if t2.get("status") == "todo":
                    out2 = self._run(["hermes", "kanban", "promote", task_id,
                                      "approved via bridge /approve-task", "--json"])
                    if not out2.startswith("ERROR:"):
                        t3 = _find_task(task_id) or {}
                        moved = (t3.get("status") == "ready")
                        if not moved:
                            move_err = "promote did not reach ready"
                    else:
                        move_err = f"promote failed: {out2[:200]}"
                else:
                    move_err = "specify did not reach todo"
        elif cur in ("todo", "blocked"):
            out = self._run(["hermes", "kanban", "promote", task_id,
                             "approved via bridge /approve-task", "--json"])
            if out.startswith("ERROR:"):
                move_err = f"promote failed: {out[:200]}"
            else:
                t2 = _find_task(task_id) or {}
                moved = (t2.get("status") == "ready")
                if not moved:
                    move_err = "promote did not reach ready"
        else:
            return self._send_json({"task_id": task_id, "moved": False,
                                    "stamped": False,
                                    "error": f"task status is {cur!r}; "
                                             "approve applies to triage/todo/blocked/ready"}, 409)
        if not moved:
            return self._send_json({"task_id": task_id, "moved": False,
                                    "stamped": False, "error": move_err}, 500)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        stamp = f"[APPROVAL: approved {ts}]" + (f" {note}" if note else "")
        # `kanban comment` no tiene --json (solo create/list/show/etc.)
        out = self._run(["hermes", "kanban", "comment", task_id, stamp,
                         "--author", "mediator"])
        stamped = out.startswith("Comment added")
        resp = {"task_id": task_id, "moved": True, "stamped": stamped,
                "stamp": stamp if stamped else None,
                "result": "approved and moved to ready"}
        if not stamped:
            resp["error"] = f"comment failed: {out[:200]}"
        self._send_json(resp)

    def _handle_verify_task(self, data):
        task_id = (data.get("task_id") or "").strip()
        if not TASK_ID_RE.match(task_id):
            return self._send_json({"task_id": task_id, "verdict": "INCONCLUSIVE",
                                    "error": "invalid task_id format",
                                    "hint": "expected t_xxxxxxxx"}, 400)
        t = _find_task(task_id)
        if not t:
            return self._send_json({"task_id": task_id,
                                    "verdict": "INCONCLUSIVE",
                                    "error": "task not found"}, 404)
        self._send_json(_verify_logic(t))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silencioso: el bridge corre en primer plano sin log dedicado


def main():
    print(f"Hermes Bridge API v1.4 on http://0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), HermesBridge).serve_forever()


if __name__ == "__main__":
    main()
