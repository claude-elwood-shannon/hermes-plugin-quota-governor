# Obs Dashboard — OBJ-32 v0

A consultable HTML page in the browser: the whole house on one screen.
This is OBJ-32's technical recommendation after evaluating Grafana: NOT the
stack (server + datasource + provisioning for a single-tenant host is a
farm that contradicts portability), YES its spirit — a consultable panel.
v0 = static generator, morning-screen family.

## Usage

```bash
# static page (by default next to the trace: obs/dashboard.html)
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-dashboard.py

# live page: regenerates on every request, Ctrl-C and done
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-dashboard.py --serve
# → http://127.0.0.1:8734   (another port: --serve 9000)
```

No installs, no JS, no CDN, no dependencies: Python stdlib. The HTML also
works opened as a file (`file://`). `--serve` binds to 127.0.0.1 ONLY
(privacy: nothing leaves the host).

## What it shows

1. **KPIs** — real accumulated spend (trace), unlabeled-gap %,
   done 24h, supply_ratio 24h + NanoGPT balance with its budget level
   (metrics-history, OBJ-29).
2. **Spend by consumption class** — table with lines/spend/%/bar and a
   $/day spend sparkline (14 days, inline SVG).
3. **Spend by objective** — top 8; `unattributed` always visible in
   red with its `← unlabeled (gap)` marker: the gap is shown, not hidden.
4. **Forecast + verdict (gate)** — quota % per provider with bar and
   verdict badge: `OK` / `REDUCE` (max_workers=1) / `BOARD OFF` /
   `NO PROJECTION`; weekly reset with hours remaining.
5. **Board** — count by state, done 24h (with hour) and active tasks.
6. **Alarms** — the same F2 alarms as morning-screen; only when there
   are anomalies (`no incidents` when all is well, silence when there
   are no sources: watchdog pattern).

## Source contract (identical to morning-screen)

- `quota-governor/obs/trace.jsonl` — OBJ-27 F0 (canonical consumption)
- `quota-governor/forecast.json` — OBJ-24 F2 (EMA burn forecast)
- `quota-governor/metrics-history.jsonl` — OBJ-29 (supply_ratio, balance)
- `kanban.db` (root or profile home) — board state

Paths resolve through `get_hermes_home()` (`HERMES_HOME` or `~/.hermes`).
Read-only: the dashboard never writes to its sources. The trace lives
under the profile home that collects it: set `HERMES_HOME` the same way
the morning-screen cron does.

## Single source of truth

The module IMPORTS morning-screen (readers, F2 thresholds, gate-verdict
rule) instead of duplicating them: if a threshold changes tomorrow, it
changes in both places at once.

## Files

- `scripts/obs/obs-dashboard.py` — generator + `--serve`
- `scripts/obs/test_obs_dashboard.py` — 16 tests, fixtures, no network,
  `/usr/bin/python3.12`

## Roadmap (decided in OBJ-32)

- **v1** (only if real refresh without `--serve` is wanted): optional OTLP
  endpoint + a page querying the JSONL via local fetch. No Prometheus.
- **v2** (only multi-host houses): AHI yes, Grafana/SigNoz — the tool
  appears when the problem justifies it.
