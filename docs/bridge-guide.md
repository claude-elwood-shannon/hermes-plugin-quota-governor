# Bridge Guide — Hermes Bridge API (port 9120)

Complete, self-contained reference for every endpoint of the Hermes Bridge
(`scripts/bridge/open-webui-bridge.py`, server version 1.9). If you read only
this file, you know how to use the bridge. Infrastructure and lifecycle
(systemd unit, cron monitor, convergence) are covered in
[`bridge-open-webui.md`](bridge-open-webui.md); this guide is about the API
surface a mediator calls.

```
mediator (Open WebUI) --HTTP 9120--> bridge --CLI--> hermes kanban / logs / restic / git / ssh
```

Stable contracts (do not change):

- **Port: 9120**, bound on `0.0.0.0` (LAN frontends reach it at
  `http://192.168.1.57:9120`).
- **API shape: v1.6 is a superset of v1.1** — same endpoints, same parameters;
  older clients keep working. New endpoints are only ever added.
- Open WebUI's External Tool Server points at `http://192.168.1.57:9120`.

All responses are JSON (`application/json`) except `/metrics-prometheus`
(Prometheus text 0.0.4) and `/openapi.json` (the spec itself). CORS is open
(`*`) so browser frontends call it directly. No authentication: this is
house-internal infrastructure on a trusted LAN.

## Getting Started

Start the server (normally it is already running as a systemd user unit —
see `bridge-open-webui.md`; the canonical runtime copy is
`~/.hermes/scripts/bridge/open-webui-bridge.py`):

```bash
~/.hermes/scripts/bridge/open-webui-bridge.py &   # binds 0.0.0.0:9120
```

First call — get the whole picture in one shot:

```bash
curl -s http://localhost:9120/bootstrap | python3 -m json.tool
```

`GET /bootstrap` consolidates the initial mediator context: plugin info,
board stats, objectives inventory, capabilities, health and the list of
bridge endpoints. It is the single starting point — everything it returns
can also be fetched individually (see the endpoint tables below).

> Deployment note: `/bootstrap` shipped in **v1.9** (task `t_a003af3e`).

Machine-readable contract and liveness probe (what the monitor curls):

```bash
curl -s http://localhost:9120/openapi.json   # OpenAPI 3.0 spec, expect 200
```

Reading a task, listing work, checking the system:

```bash
curl -s 'http://localhost:9120/task?task_id=t_a003af3e'
curl -s 'http://localhost:9120/tasks?status=triage&limit=10'
curl -s 'http://localhost:9120/objectives'
curl -s 'http://localhost:9120/backup/health'
```

## Endpoints — GET (read side)

| # | Endpoint | Parameters | Description |
|---|----------|------------|-------------|
| 1 | `GET /openapi.json` | — | OpenAPI 3.0 spec of the whole API; doubles as the health probe (200 = alive). |
| 2 | `GET /bootstrap` | — | One-call initial context: plugin info, board stats, objectives, capabilities, health (bridge + cron summary), endpoints list and short log tails. Best-effort per block: a failed source degrades to `{"error": ...}` and the rest still answers. Does NOT include parked ideas (see `GET /ideas`). |
| 3 | `GET /board` | — | Kanban stats plus active tasks (`ready`/`running`/`blocked`): `{stats, active[]}`. |
| 4 | `GET /snapshot` | — | Board + active + `triage` queue (with tags) + last lines of watchdog/tick/health/efficiency logs. The everything-at-a-glance read. |
| 5 | `GET /watchdog` | — | Last 15 lines of `logs/kanban-watchdog.log`. |
| 6 | `GET /tick` | — | Last 10 lines of `logs/quota-governor-tick.log`. |
| 7 | `GET /health` | — | Last 5 lines of `logs/cron-health-check.log`. |
| 8 | `GET /efficiency` | — | Last 3 lines of `logs/efficiency-ratio.log`. |
| 9 | `GET /file` | `path` (required) | Read a text file (100 KB cap, binaries rejected). Paths restricted to plugin-repo `scripts/`, `docs/`, `tests/`, `~/.hermes/{scripts,logs,profiles}` and `~/.hermes/config.yaml`. See security model below. |
| 10 | `GET /backup/snapshots` | — | Recent restic snapshots (newest last, capped list) + count + `last_time`. Read-only: never runs backup/restore/prune. |
| 11 | `GET /backup/stats` | — | Restic repo totals: `total_size`, `total_files`. Credentials never exposed. |
| 12 | `GET /backup/log` | `lines` (1–500, default 30) | Last N lines of `logs/backup.log`, redacted. |
| 13 | `GET /backup/health` | — | 4 checks (`last_snapshot_fresh` <3h, `log_clean_24h`, `cron_present`, `repo_size_ok`) → `healthy` boolean. Fail-open per check. |
| 14 | `GET /gpu/health` | — | GPU ml-host (192.168.1.32) snapshot over SSH + vLLM API: temp, VRAM, utilisation, active model, service state, today's rounds. Best-effort: every failure degrades to `null`/`false`. 60 s cache. |
| 15 | `GET /capabilities` | — | Inventory of modular capabilities read from `capabilities/*/manifest.yaml` (`{capabilities[], count, load_errors}`). |
| 16 | `GET /capabilities/<name>` | — | One capability's manifest payload. 404 if unknown. |
| 17 | `GET /capabilities/<name>/hosts` | — | Host inventory of a capability (`id`, `ip`, `ssh_user`, permissions, deny). 404 unknown; 503 unreadable `hosts.yaml`; empty list if it declares none. |
| 18 | `GET /capabilities/<name>/status` | — | Live SSH probe of the capability's declared hosts (reachable, vllm service state, GPU snapshot). Best-effort. |
| 19 | `GET /task` | `task_id` (required, `t_xxxxxxxx`) | Full task record by ID (body, status, assignee, all metadata) with parsed `tags`. |
| 20 | `GET /tasks` | `tag`, `status`, `created_by`, `objective`, `text`, `limit` (1–500, default 50) | Search/filter the board; all parameters optional, combined with AND. `status` must be a valid kanban status; `text` is a case-insensitive substring of title/body. Returns `{tasks[], count}` (public field projection). |
| 21 | `GET /git-log` | `limit` (1–100, default 10) | Recent commits of the plugin repo (`hash`, `short_hash`, `author`, `date`, `message`) — verify deployments landed. |
| 22 | `GET /metrics` | `kind`, `days` (1–365, default 7) | `metrics-history.jsonl` rows filtered by `kind` and time window, sorted by `ts`. |
| 23 | `GET /metrics-prometheus` | — | Prometheus text 0.0.4 (pull-only): tasks per status, objective spend/budget, session/week quota, efficiency ratio, supply ratio, cron health, GPU ml-host. Missing sources degrade silently; the endpoint never breaks. |
| 24 | `GET /objectives` | `status` (optional) | Approved-objectives inventory (id, name, budget, spend, status). 503 if the kanban DB is unavailable. |

Error convention for reads: `400` bad/missing parameters, `403` path not
allowed, `404` not found, `503` underlying source unavailable
(restic/SSH/DB). A degraded data source never returns 500 with secrets in
it — payloads pass a redaction filter first.

### `GET /file` security model

- Relative paths only: absolute paths and `..` traversal are rejected.
- Served roots: `scripts/`, `docs/`, `tests/` of the plugin repo,
  `~/.hermes/scripts`, `~/.hermes/logs`, `~/.hermes/profiles`,
  `~/.hermes/config.yaml` (symlinks resolved via realpath, must stay inside
  a root).
- Paths containing `credentials`, `secrets`, `ssh`, `id_rsa`, `id_ed25519`,
  `.env`, `token`, `apikey`/`api_key` → 403; credential-named files
  (`auth.json`, `credentials.json`, `.git-credentials`) denied everywhere.
- Directories and binaries rejected (403/400); 100 KB read cap.

## Endpoints — POST (write side)

All POST bodies are JSON. These six are the **only** write operations the
bridge exposes — everything else is read-only.

| # | Endpoint | Body fields | Description |
|---|----------|-------------|-------------|
| 1 | `POST /create-task` | `title` (req), `body` (req), `tags` (default `mediator-prompt`), `triage` (bool, default false) | Create a kanban task (`--created-by mediator`, default assignee `pr-ollama`). `triage: true` lands it in triage instead of ready. |
| 2 | `POST /move-task` | `task_id` (req), `status` (req: `todo` or `ready`) | Restricted transitions only: `triage→todo` via `specify` (LLM spec-writer, 150 s timeout), `todo/blocked→ready` via `promote`. Other pairs → 409. Returns `{moved, current_status, new_status, steps[]}`. |
| 3 | `POST /comment-task` | `task_id` (req), `comment` (req) | Append a comment to a task; response includes `ok` (true when the CLI confirms `Comment added`). |
| 4 | `POST /approve-task` | `task_id` (req), `note` (optional) | Atomic-in-intent approval: moves the task to `ready` from `triage` (specify+promote), `todo`, or `blocked`, then stamps `[APPROVAL: approved <utc-ts>] <note>` as a comment. `{moved, stamped}` reported per step; non-transactional — a failed stamp can be retried without re-moving (already-`ready` counts as moved). 409 for other statuses. |
| 5 | `POST /verify-task` | `task_id` (req) | Verify a done task's declared success criterion against recorded evidence. Verdict: `PASS`, `FAIL`, `INCONCLUSIVE`, `NO_CRITERION`, or `NOT_DONE` (plus extracted criterion and evidence found). |
| 6 | `POST /update-objective` | `id` (req), `name` (req), `budget_daily` (req, number), `description`, `success_criterion`, `status` (default `active`) | Insert or update an approved objective. Only provided fields are written; housekeeping columns (`spent_*`, `exhausted_days`) are tick-owned and never wiped. Returns the resulting row. |

`OPTIONS` is answered with open CORS headers (GET, POST, OPTIONS) for
browser pre-flights.

### Task ID and status formats

- Task IDs: `t_` + 8 hex chars (e.g. `t_a003af3e`); anything else → 400 with
  `hint: expected t_xxxxxxxx`.
- Valid statuses: `triage`, `todo`, `scheduled`, `ready`, `running`,
  `blocked`, `review`, `done`, `archived`.
- Objective IDs: `OBJ-<token>` (e.g. `OBJ-AUTODEV`, `OBJ-CODEQUALITY`,
  legacy `OBJ-0N`, or approved-objectives table IDs).

## Quality Standard

Every bridge property below is deliberate; a change that breaks one needs a
task and a version bump.

1. **Zero dependencies.** Python stdlib only (`http.server`, `subprocess`,
   `sqlite3`); it shells out to the `hermes` and `git` CLIs and `restic`.
   No pip install, no venv, no tokens burned serving reads.
2. **Read-only by default.** 25 GET endpoints vs 6 POST endpoints. The
   monitoring families (`/backup/*`, `/gpu/health`, `/capabilities/*`)
   never mutate anything — backup endpoints never run
   backup/restore/prune; capability probes only ever SSH to hosts declared
   in a checked-in inventory.
3. **Best-effort degradation.** A failed data source degrades its field to
   `null`/`false`/empty and the endpoint still answers 200 with the rest
   (`/metrics-prometheus`, `/gpu/health`, `/backup/health` fail-open per
   check). Missing data is reported as absent data, never as a crash.
4. **Secrets never leave the host.** Restic credentials go to the child
   process via environment only (never argv); every `/backup/*` payload
   passes `_backup_redact`; restic stderr never enters responses or logs;
   `/file` enforces the denylist above; the S3 endpoint/URL lives outside
   the versioned repo.
5. **Bounded work.** Subprocess timeout 15 s (single deliberate exception:
   `kanban specify` gets 150 s because it runs an LLM); restic gets 30 s;
   list endpoints clamp their limits (`/tasks` 1–500, `/git-log` 1–100,
   `/backup/log` 1–500, `/metrics` days 1–365). GPU/SSH probes cache 60 s
   so scrapes never hammer ml-host.
6. **Contract stability.** Port, endpoint paths and parameters are frozen
   contracts; new versions only add. `/openapi.json` is generated from the
   same constants the router uses.
7. **Verifiability.** Every mediator action leaves an audit trail: created
   tasks carry `--created-by mediator`, approvals stamp an
   `[APPROVAL: approved <ts>]` comment, `/git-log` exposes what code
   changed, `/verify-task` re-checks declared success criteria on demand.

## Mediator

The mediator is a human (or LLM assistant via Open WebUI) governing the
system through this bridge: reviewing work, approving tasks, watching
spending. The canonical console is Open WebUI with the External Tool Server
pointing at `http://192.168.1.57:9120`; raw `curl` works identically.

### Standing routine

1. **Orient** — `GET /bootstrap` (or `GET /snapshot` on older bridges). One
   call tells you plugin info, board state, objectives, capabilities and
   health.
2. **Review the triage queue** — `GET /tasks?status=triage`, read each
   candidate with `GET /task?task_id=...`. Approve with
   `POST /approve-task` (moves to ready and stamps the audit comment) or
   push back with `POST /comment-task`.
3. **Unblock stalled work** — `GET /tasks?status=blocked`, then
   `POST /move-task {"status": "ready"}` for tasks worth resuming.
4. **Capture ideas** — `POST /create-task` with `triage: true`; ideas wait
   in triage until reviewed (they never skip review).
5. **Check delivered work** — `GET /tasks?status=done` +
   `POST /verify-task` on candidates; verdict `PASS` with evidence before
   you trust it.
6. **Watch the money** — `GET /objectives` (budget vs spend),
   `GET /metrics`, `GET /metrics-prometheus` for the dashboard view.
7. **Health sweep** — `GET /backup/health`, `GET /gpu/health`,
   `GET /capabilities/<name>/status` when something feels slow.

### Rules of engagement

- Reads are unlimited and consequence-free; the six POSTs are the entire
  write surface — if an action is not among them, the bridge deliberately
  cannot do it (do it from a shell on the host instead).
- `/move-task` and `/approve-task` only implement the transitions that make
  governance sense (`triage→todo→ready`); anything else is refused with
  409 — do not try to force `done` or `archived` through the bridge.
- `/approve-task` is not transactional: if the move succeeded but the stamp
  failed, re-calling it on the now-`ready` task just retries the stamp.
- Approval stamps are the audit trail — always include a meaningful
  `note`.
- `POST /update-objective` never wipes spend accounting; treat budget
  edits as governance decisions, not bookkeeping.

### Version history

| Version | Added |
|---------|-------|
| v1.1 | Core: `/board`, `/snapshot`, `/watchdog`, `/tick`, `/health`, `/efficiency`, `/create-task`, `/move-task`, `/comment-task` |
| v1.2 | `GET /file` (restricted file reads) |
| v1.3 | Governance: `/task`, `/tasks`, `/git-log`, `/metrics`, `/approve-task`, `/verify-task`; `/move-task` fixed to specify/promote pairs |
| v1.4 | Backup monitoring: `/backup/snapshots`, `/backup/stats`, `/backup/log`, `/backup/health` (read-only, redacted) |
| v1.5 | Modular capabilities: `/capabilities`, `/capabilities/<name>[/hosts\|/status]` (read-only) |
| v1.6 | `GET /metrics-prometheus` (Prometheus text 0.0.4, pull-only) |
| next | `GET /bootstrap` (consolidated context — task `t_a003af3e`) |
