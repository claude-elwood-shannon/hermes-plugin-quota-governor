# Morning Consumption Screen — Format (OBJ-27 F1)

The morning screen is a **deterministic, no-agent** one-page digest of the
house's overnight activity. It is a pure function of three existing files and
costs zero tokens (free quota). The `morning-report` cron (an LLM job) consumes
this screen as its `script` context and writes only the final narrative
paragraph around it — the LLM reasons, the screen is the ground truth.

## Sources (all read-only)

| Source | Path (under HERMES_HOME) | What it provides |
|--------|--------------------------|------------------|
| Trace | `quota-governor/obs/trace.jsonl` | OBJ-27 F0 canonical consumption trace |
| Forecast | `quota-governor/forecast.json` | OBJ-24 F2 EMA burn-rate forecast |
| Board | `kanban.db` (root or profile home) | task status counts, active tasks, done 24h |

Paths resolve through `get_hermes_home()` (`HERMES_HOME` env or `~/.hermes`).
No absolute host paths in the repo. Times render local (CEST).

## Screen sections

1. **CONSUMO** — trace totals, cumulative real cost (USD), spend by objective
   (top 8), by consumer class, by source. The `unattributed` objective is shown
   explicitly with a `SIN ETIQUETA (hueco)` marker — the join gap is surfaced,
   never hidden.
2. **FORECAST** — per-provider `pct_now`, ETA to 90% (governor stop) and 100%
   (exhaustion), confidence, weekly reset. Followed by **VEREDICTO (gate
   --suggest)**: the decision rule applied to each provider with an ETA —
   `eta_90 < 1h` → BOARD OFF; `eta_90 < reset − 2h` → max_workers=1, cap cost;
   otherwise OK.
3. **BOARD** — total tasks + counts by status, `supply_ratio diario`
   (placeholder until OBJ-29 provides it), `done 24h` (capped list), and active
   tasks (running/ready/blocked, capped).
4. **ALERTAS** — F2 anomalies **only** when present: unattributed spend > 20%,
   crash loop (≥3 in 24h), or burn over threshold. Otherwise `sin incidencias`.

## Hard rule

One screen = one file, not a report. The screen stays under ~40 lines; any
rollup or detail beyond that goes to a separate file. If every source is
missing, the script emits nothing (watchdog pattern — the cron stays silent
rather than alert on a deploy gap).

## Exit / output contract

- Exit code 0 always.
- Empty stdout only when every source is missing.
- Non-empty stdout is the screen, delivered verbatim to the cron's script
  context.

## Files

- `scripts/obs/morning-screen.py` — the screen builder (no_agent).
- `scripts/obs/test_morning_screen.py` — 18 tests, fixtures only, no network,
  `/usr/bin/python3.12`.
- `scripts/obs/morning-screen-cron.sh` — cron wrapper (pins HERMES_HOME to the
  profile where trace + forecast live).
