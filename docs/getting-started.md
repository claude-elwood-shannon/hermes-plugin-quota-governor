# Getting started with self-govern

First flight for newcomers: from a plain Hermes Agent install to a
self-governing workspace — plugin enabled, cron bootstrap registered,
first status check, and the mediator loop.

The one-line pitch:

> Hermes puede hacer cualquier cosa, y self-govern le ayuda a hacerlo de
> forma autónoma, con presupuesto, con calidad, y con observabilidad.

Time budget: ~20 minutes if you already have a Hermes Agent and an Ollama
Cloud API key. The plugin is installed and enabled by the end of step 2;
everything after that turns observation into action.

---

## 0. Prerequisites

- **Hermes Agent** with kanban support (`hermes kanban` works).
- **API keys** in `~/.hermes/.env` (or the profile's `.env` you will run
  under):
  - `OLLAMA_API_KEY` — required; it drives the governor's decision.
  - `NANO_GPT_API_KEY`, `OPENROUTER_API_KEY` — optional, informational.
- Python 3.12+ on the PATH (the cron tick is pure bash + stdlib python3).
- Optional: Tor if your threat model routes GitHub over it.

## 1. Install

From a clone (also runs the 1,000+ test offline suite — no network
needed; each test file runs directly, `unittest discover` is not usable
because some module basenames repeat across dirs):

```bash
git clone https://github.com/claude-elwood-shannon/hermes-plugin-quota-governor
cd hermes-plugin-quota-governor
failed=0
for t in $(find . -name 'test_*.py' -not -path './.git/*'); do
  python3 "$t" >/dev/null 2>&1 || { echo "FAIL: $t"; failed=1; }
done
[ "$failed" -eq 0 ] && echo "suite green"
```

Install and enable in one step:

```bash
hermes plugins install claude-elwood-shannon/hermes-plugin-quota-governor --enable
```

## 2. Enable and verify

If you installed without `--enable` (portable packages install disabled),
or disabled it later:

```bash
hermes plugins list                 # find the plugin: self-govern
hermes plugins enable self-govern   # older checkouts: quota-governor
```

Smoke-test that the plugin loaded:

```bash
hermes plugins doctor   # validates the plugin against runtime contracts
```

Inside any Hermes session, the slash command must answer:

```
/quota-governor status
```

You should see the quota snapshot and the governor's decision (mode,
workers, max task size). If Ollama is unreachable it says so plainly —
fix the key/network before continuing.

> Note: the plugin is being renamed from `quota-governor` to
> `self-govern`. Depending on when you cloned, `plugin.yaml` still names
> it `quota-governor`; the slash command is `/quota-governor` in both
> cases.

## 3. Bootstrap the autonomous layer (crons)

The governor is only half the system; the autonomous loop needs its
zero-token cron layer registered once. These are `no_agent` jobs — they
run scripts directly and never spend tokens.

```bash
# copy the entry points into Hermes' script dir (cron runs scripts from there)
cp scripts/quota-governor-tick.sh ~/.hermes/scripts/
cp scripts/quota-metrics.py ~/.hermes/scripts/
cp scripts/quota-forecast.py ~/.hermes/scripts/

# 10-minute tick: query quota → decide → start/scale/stop the worker daemon
HERMES_HOME=~/.hermes/profiles/<your-profile> hermes cron create "10m" \
  --name quota-governor-tick \
  --script quota-governor-tick.sh --no-agent --deliver local

# 15-minute metrics sampler (zero extra API calls)
hermes cron create "15m" --name quota-metrics \
  --script quota-metrics.py --no-agent --deliver local

# 15-minute forecast, after metrics
hermes cron create "15m" --name quota-forecast \
  --script quota-forecast.py --no-agent --deliver local
```

Adjust `HERMES_HOME` to the profile that owns the flight. The tick reads
provider keys from the profile's `.env` — the current script pins it to
`~/.hermes/profiles/pr-ollama/.env`; if you fly under another profile,
adjust `ENV_FILE` at the top of `quota-governor-tick.sh`. The optional
predictive/backtest layer (F2 gate, F3 budget, backtest) is documented in
`README.md` § "Predictive quota system"; register those three the same
way once the basics fly.

Verify the registrations:

```bash
hermes cron list
```

## 4. Load the mediator skill and open the bridge

The mediator is how a human steers the system between visits — through a
chat frontend (Open WebUI) that talks HTTP to the bridge, and through a
skill the agent session loads to act as mediator's counterpart.

1. **Read the bridge doc** — `docs/bridge-open-webui.md` is the
   self-contained reference (endpoints, three-copies deployment rule,
   security model). The short version:

   ```bash
   # systemd user unit (survives reboot; needs loginctl enable-linger)
   cp scripts/bridge/hermes-bridge.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now hermes-bridge
   curl -s http://localhost:9120/openapi.json | head   # liveness probe
   ```

2. **Load the mediator skill into your session.** Skills live in
   `$HERMES_SKILLS`; the session loads skills from that directory tree at
   startup and a running session picks them up via
   `hermes skills`:

   ```bash
   export HERMES_SKILLS=~/.hermes/profiles/<your-profile>/skills
   hermes skills list          # confirm the mediator skill is visible
   ```

   Then tell the agent to load it by name before mediating — in this
   deployment the mediator workflow (read board via `/bootstrap`,
   create/move/comment tasks, write with evidence) is defined by that
   skill plus the bridge doc.

3. **Bootstrap in one call.** The bridge consolidates the whole initial
   context — plugin info, board stats, objectives, capabilities, health —
   into a single `GET /bootstrap`:

   ```bash
   curl -s http://localhost:9120/bootstrap | python3 -m json.tool
   ```

   That JSON is the "you are here" for any mediator, human or agent.

4. **Point the frontend at the bridge.** Open WebUI's External Tool
   Server points at `http://<host>:9120`; the API is a stable contract
   (see `docs/bridge-open-webui.md` for the endpoint table and the
   three-copies rule).

## 5. The usage model: human + mediator, iterative cycle

- **Human**: owns direction. Ratifies objectives, approves budgets,
  arbitrates what guardrails escalate. Nothing the system proposes can
  leave the plugin repo or `~/.hermes/`, and no new credentials without
  explicit approval (GR1–GR11, see
  `docs/guardrails-autonomous-objectives.md`).
- **Mediator**: the human's hands between visits. It reads `/bootstrap`,
  writes tasks into triage (`POST /create-task`), moves and comments on
  tasks — each write with evidence in the body.
- **Workers**: agent sessions that claim tasks, heartbeat while alive,
  and close with `kanban_complete`/`kanban_block` — never silent rc=0.
- **Governor**: watches quota and lifecycle, scales the daemon, and
  silences creators when a zombie task or a burn anomaly says stop.
- **The cycle**: evidence from every closure feeds the next morning
  report, which feeds the next task wave. Filler is forbidden by design —
  a dry queue with nothing structural to do is a *legitimate* drought,
  and the system says so instead of inventing work.

## 6. First flight checklist

```bash
hermes plugins list                  # plugin present and enabled
hermes cron list                     # tick + metrics + forecast registered
curl -s http://localhost:9120/bootstrap | python3 -m json.tool
/quota-governor status               # inside a Hermes session
/quota-governor decision             # what would the governor do now?
```

Then: create one tiny task through the mediator (or `/bootstrap` +
`POST /create-task`), watch a worker claim it, and read
`~/.hermes/quota-governor/observations.jsonl` to see the lifecycle land.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `status` says Ollama unreachable | `OLLAMA_API_KEY` in the profile's `.env`; `curl` the provider from this host |
| Slash command unknown | `hermes plugins list` — plugin missing or disabled; `hermes plugins enable self-govern` |
| Daemon never spawns workers | tick cron registered? `hermes cron list`; STOP signal present? `~/.hermes/quota-governor/STOP` (`/quota-governor clear-signals`) |
| Bridge not answering | `systemctl --user status hermes-bridge`; `journalctl --user -u hermes-bridge`; monitor cron is `open-webui-bridge-cron.sh` |
| Tests fail on fresh clone | two suites skip outside this deployment; compare against `docs/coding-standards.md` and file an issue |

## Where to go next

- `README.md` — full capability map, decision heuristic, architecture.
- `docs/bridge-open-webui.md` — mediator bridge reference.
- `docs/guardrails-autonomous-objectives.md` — what the system may and
  may not propose by itself.
- `docs/objectives.md` — the objectives index the whole loop orbits.
- `docs/coding-standards.md` — conventions before contributing code.
