# Bridge Open WebUI — HTTP API for the mediator flow

A zero-dependency HTTP server (Python stdlib only, zero tokens) that exposes
Hermes operations as an OpenAPI-described API, so Open WebUI (or any other
frontend) can interact with the kanban board and the observability logs.

It is critical infrastructure of the governance flow:

```
mediator (Open WebUI)  --HTTP 9120-->  bridge  --CLI-->  hermes kanban / logs
```

The mediator creates tasks, moves tasks and reads the state of the system
through this bridge. Port and API are stable contracts:

- **Port: 9120** (does not change).
- **API: same endpoints, same parameters** (v1.2 superset; v1.1 clients keep working).
- Open WebUI's External Tool Server points at `http://192.168.1.57:9120` —
  do not change it; only the host side of that contract is managed here.

## The script and the three-copies rule

Source of truth (versioned): `scripts/bridge/open-webui-bridge.py` in this
repo. Deployed copies (same rule as quota-governor-tick.sh):

| copy | path | mode |
|------|------|------|
| repo (source of truth) | `scripts/bridge/open-webui-bridge.py` | 664 |
| shared deployed | `~/.hermes/scripts/bridge/open-webui-bridge.py` | 664 |
| profile deployed | `~/.hermes/profiles/pr-ollama/scripts/bridge/open-webui-bridge.py` | 775 |

All three copies must be byte-identical (`md5sum` must match). The
**canonical runtime path** is the shared copy `~/.hermes/scripts/bridge/open-webui-bridge.py`:
that is where the supervisor restarts the service from. The old loose file
`~/git/hermes-bridge/server.py` is legacy and is NOT a valid runtime
location — if something starts the bridge from there, the supervisor kills
it and respawns from the canonical path (see Convergence below).

## How it runs

The bridge is a **daemon, not a cron**: it serves until killed. Since the
systemd migration (MEDIATOR 2026-09-14) the lifecycle is owned by a
**systemd user unit**, `scripts/bridge/hermes-bridge.service` in this repo:

```bash
cp scripts/bridge/hermes-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-bridge   # boot survival: needs Linger=yes
```

The unit pins `BRIDGE_PLUGIN_REPO` to the plugin repo checkout, exports a
reconstructed minimal `PATH` (the bridge spawns a bare `hermes`, which lives
in `~/.local/bin`), runs with `Restart=always` / `RestartSec=5`, and logs to
the user journal (`journalctl --user -u hermes-bridge`). Adoptants running
the bridge outside this repo must adjust `BRIDGE_PLUGIN_REPO` in the unit.

The 5-min cron layer (crontab of the host) remains as **monitor, not
owner**:

```cron
# monitor: HTTP probe + heartbeat + nudge systemd, every 5 min
*/5 * * * * ~/.hermes/scripts/bridge/open-webui-bridge-cron.sh >> ~/.hermes/logs/open-webui-bridge.log 2>&1
# watchdog of the watchdog: verifies the heartbeat, every 15 min (already wired)
*/15 * * * * /home/iinstances/.hermes/scripts/cron-health-check.sh >> /home/iinstances/.hermes/logs/cron-health-check.log 2>&1
```

The monitor (`scripts/bridge/open-webui-bridge-cron.sh`, 3 copies like
the server, mode 755) probes the service, touches the heartbeat when
healthy, kills non-canonical bridge holders (convergence, unchanged), and
when the bridge is down it **nudges systemd** (`systemctl --user start`
or `restart` hermes-bridge) instead of respawning it. It is also the
restart command that `cron-health-check.sh` executes when it finds the
heartbeat stale. The legacy `nohup` respawn from the canonical path is
kept only as a fallback for adoptants without the unit installed.

Liveness is HTTP-based, not log-mtime-based: the bridge is silent (its log
only grows on respawn), so the supervisor probes
`GET http://localhost:9120/openapi.json` (expect 200) and touches
`~/.hermes/logs/open-webui-bridge.heartbeat` on every healthy tick — the
heartbeat file is the liveness signal that `cron-health-check.sh` watches
(same pattern as `obs-serve.heartbeat`). Expected window: 300 s tick,
480 s tolerance.

## Endpoints

GET (read side):

| endpoint | returns |
|----------|---------|
| `/board` | kanban stats + active tasks (ready/running/blocked) |
| `/snapshot` | board + active + triage + last lines of watchdog/tick/health/efficiency logs |
| `/watchdog` | last 15 lines of `kanban-watchdog.log` |
| `/tick` | last 10 lines of `quota-governor-tick.log` |
| `/health` | last 5 lines of `cron-health-check.log` |
| `/efficiency` | last 3 lines of `efficiency-ratio.log` |
| `/file?path=<rel>` | read a text file (100 KB cap, binary rejected) — v1.2 |
| `/openapi.json` | the OpenAPI spec itself (also the health probe) |

POST (write side, JSON body):

| endpoint | body |
|----------|------|
| `/create-task` | `{"title", "body", "tags"?, "assignee"?, "triage"?}` → creates kanban task (`--created-by mediator`; `triage:true` lands it in triage). Assignee: explicit `assignee` wins; otherwise first profile without a quota-governor STOP file (`pr-ollama` → `pr-nanogpt` → `pr-opencode` → `pr-vllm`); all stopped → `pr-ollama` fail-open — v1.9 |
| `/move-task` | `{"task_id", "status"}` → move task (ready/triage/blocked/archived/done) |
| `/comment-task` | `{"task_id", "comment"}` → append comment to the task body |
| `/save-idea` | `{"title", "body", "tags"?}` → park a brainstorm idea as JSON under `~/.hermes/data/ideas/` (dir created on first save) — v1.7 |
| `/update-preset` | `{"preset_id", ...fields}` → create/update an adjustment preset (only provided fields) — v1.8 |
| `/update-objective` | `{"name", ...fields}` → create/update an objective; also accepts `nice`, `budget_baseline`, `budget_adjustment_pct`, `governance`, `preset_id` — v1.8 |

GET `/ideas` (v1.7) returns the parked ideas, newest first:

```json
{"count": 2, "ideas_dir": "/home/iinstances/.hermes/data/ideas",
 "ideas": [{"idea_id": "idea_20260917_121530_123", "title": "...",
            "body": "...", "saved_at": "2026-09-17T12:15:30", "tags": ["..."]}]}
```

Ideas are NOT kanban tasks and are NOT part of the bootstrap: they are a
parking lot the mediator consults when it needs them. Every idea file
carries a unique `idea_id` (`idea_<yyyymmdd>_<hhmmss>_<mmm>` — milliseconds,
so lexicographic order = creation order); the reader skips corrupt files
best-effort.

CORS is open (`*`) so browser frontends can call it directly. The server
binds `0.0.0.0:9120` (Open WebUI reaches it from the LAN as
`http://192.168.1.57:9120`); treat it as house-internal infrastructure.

`/file` security model (v1.2): relative paths only (no absolute, no `..`),
served from `scripts/`, `docs/`, `tests/` of this plugin repo plus
`~/.hermes/{scripts,logs,profiles}` and `~/.hermes/config.yaml`; symlinks
are resolved with realpath and must stay inside the allowed roots; paths
containing `credentials`, `secrets`, `ssh`, `id_rsa`, `id_ed25519`, `.env`,
`token`, `apikey`/`api_key` are rejected (403); binaries are rejected (400).

## Health-check integration

`cron-health-check.sh` watches the bridge like any critical cron, via the
heartbeat file:

```
open-webui-bridge|~/.hermes/logs/open-webui-bridge.heartbeat|300|480|bash ~/.hermes/scripts/bridge/open-webui-bridge-cron.sh
```

- heartbeat fresh (≤480 s) → `OK` (the normal state; the wrapper touches it
  every 15 min tick).
- heartbeat stale → `DEAD` → the health-check runs the wrapper, which
  nudges systemd to rebuild the daemon (`systemctl --user start/restart
  hermes-bridge`; legacy `nohup` respawn only when the unit is not
  installed); the restart is logged
  (`CRON DEAD: open-webui-bridge — reiniciado (ok)`) and alarm-JSONL'd.
- DIRECCION-STOP is respected: with the STOP file present, the health-check
  still verifies and alarms but does NOT restart.

## Respawn environment

`env -i` launches the daemon with a RECONSTRUCTED minimal PATH, never
`PATH="$PATH"`: the wrapper may run from cron (`PATH=/usr/bin:/bin`) or the
restricted Hermes worker context, where `~/.local/bin` is absent — and the
bridge shells out to `hermes` (a bare `subprocess.run(["hermes", ...])`) which
lives at `$HOME/.local/bin/hermes`. The respawn passes `HOME`, the minimal
`PATH` (`$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin`), `LANG`,
`HERMES_HOME`, and `BRIDGE_PLUGIN_REPO`; everything else (worker `HERMES_*`,
`PYTHONPATH`, `AO_KANBAN_DB`, ...) is stripped by `env -i`, so the daemon can
mutate the board without delegated-context restrictions.

## Convergence to the canonical path

The supervisor does more than respawn-on-dead. Every tick it classifies the
holder of port 9120 (via `/proc/<pid>/fd` socket inode + cmdline, with
relative paths resolved against `/proc/<pid>/cwd`):

| holder | action |
|--------|--------|
| canonical copy, healthy | silence + heartbeat (exit 0) |
| bridge started from a non-canonical copy (`hermes-bridge/server.py`, repo copy, etc.) | kill holder, respawn from canonical path |
| dead port | respawn from canonical path |
| port held by a process that is NOT a bridge | do NOT kill; report and exit 1 (human escalation) |

This means the success criterion "the bridge runs from
`~/.hermes/scripts/bridge/open-webui-bridge.py`, not from `~/git/hermes-bridge/`"
converges automatically at most one tick (15 min) after any deviation — no
human intervention needed.

Logs: `~/.hermes/logs/open-webui-bridge.log` (stderr only; the daemon is
silent), `~/.hermes/logs/open-webui-bridge.heartbeat` (liveness file).
