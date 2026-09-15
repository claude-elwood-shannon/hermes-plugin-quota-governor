# Coding Standards

> **Scope:** All versioned code in `hermes-plugin-quota-governor` (plugin root,
> `scripts/`, `capabilities/`) and the cron scripts deployed to the active
> profile (`~/.hermes/profiles/pr-ollama/scripts/`).
> **Status:** In force (Sep 2026). Every rule below is anchored to a real
> example already in the repo — this documents what we do, not what we aspire to.

---

## 1. Language policy (no exceptions)

- **Code, comments, docstrings, commit messages: English.** Commits follow
  Conventional Commits (`feat(capabilities): services-host root-disk rule`,
  `fix(gate): drop the covered-models list from the snapshot`).
- **Internal logs, cron tick lines, kanban task bodies: Spanish** (accents
  included). Example: the liveness-tick comments in
  `scripts/obs/efficiency-ratio-cron.sh:8-11` ("Tick de liveness: SIEMPRE al
  log compartido que vigila cron-health-check.sh").
- Technical identifiers (paths, file names, tags, ids) are copied verbatim in
  any language.

## 2. File naming by type

| Type | Convention | Examples |
|------|-----------|----------|
| Python module (importable) | `snake_case.py` | `quota_governor.py`, `providers.py`, `scripts/tick_body_parts.py` |
| Python CLI script (cron entry point) | `kebab-case.py` | `scripts/obs/efficiency-ratio.py`, `scripts/model-cost-ledger.py`, `scripts/quota-gate.py` |
| Test | `test_<module>.py` (snake) | `test_efficiency_ratio.py`, `scripts/obs/test_trace.py` |
| Shell cron wrapper | `kebab-case-cron.sh` | `scripts/obs/efficiency-ratio-cron.sh`, `scripts/backtest-f2-cron.sh` |
| Shell utility | `kebab-case.sh` | `scripts/cron-health-check.sh` |
| Doc | `kebab-case.md` under `docs/` | `docs/guardrails-autonomous-objectives.md` |

**Pairing rule:** every cron-driven Python script has a matching
`<name>-cron.sh` wrapper (`efficiency-ratio.py` ↔ `efficiency-ratio-cron.sh`);
the wrapper owns logging and liveness, the `.py` owns logic.

## 3. Python

- **Shebang** `#!/usr/bin/python3.12` on executable scripts
  (`scripts/obs/efficiency-ratio.py:1`); none on importable modules
  (`quota_governor.py`).
- **`from __future__ import annotations`** at the top of every module
  (`quota_governor.py:10`).
- **Module docstring** right after the shebang, answering *what + why*, with
  explicit `Usage:` and `Env overrides:` sections when flags/env vars exist
  (`scripts/obs/efficiency-ratio.py:2-50`).
- **Stdlib only** for zero-token observability/audit scripts — no third-party
  imports (`efficiency-ratio.py` header: "zero tokens, stdlib only, read-only
  inputs").
- **Type hints on all public functions**, including return types
  (`quota_governor.py:68` `def _get_hermes_home() -> Path:`);
  `typing.Dict/List/Optional` style is current.
- **Logging** via `logger = logging.getLogger(__name__)`, never bare `print`
  in modules (`quota_governor.py:23`).
- **Functions target < 50 lines.** Longer is a refactor signal, not a ban.
- **Importing kebab-named scripts in tests:** they are not valid module names,
  so tests load them by path with `importlib.util`
  (`test_efficiency_ratio.py:5`).

## 4. Shell (cron scripts)

- Header: `#!/usr/bin/env bash` + `set -euo pipefail`
  (`scripts/quota-governor-tick.sh:5-6`).
- **Header comment states the cost contract:** `no_agent, zero tokens`, and
  the silence contract — "Silent stdout = no change"
  (`quota-governor-tick.sh:3`, `efficiency-ratio-cron.sh:2`).
- **Watchdog/liveness pattern:** every cron run appends ONE timestamped tick
  line to the shared log that `cron-health-check.sh` watches (hardcoded
  `HERMES_HOME_DIR` there — do NOT use `$HERMES_HOME`, which resolves
  differently under a profile), and the script itself stays silent on success
  (`efficiency-ratio-cron.sh:3-13`).
- **`log()` helper** with ISO timestamp appending to `$LOG_FILE`
  (`quota-governor-tick.sh:20`).
- **Fail-open:** on missing config or unreadable sources, log the error and
  `exit 0` so cron does not alert — the watchdog detects death via missing
  ticks, not exit codes (`quota-governor-tick.sh:22-24`).
- **`exec` the Python entry point as the last statement** of the wrapper
  (`efficiency-ratio-cron.sh:17`).

## 5. Git hygiene

- **Never commit runtime state:** `tmp/`, SQLite sidecars `*.db-shm` /
  `*.db-wal`, metrics `*.jsonl` state files, `STOP` signals, `*.pid` files,
  `__pycache__/` (see `.gitignore`).
- Build/editor artifacts (`dist/`, `build/`, `.vscode/`, `*.swp`) and
  `.worktrees/` are excluded too.
- One logical change per commit; the commit body explains *why* when the diff
  does not.

## 6. Documentation

- One topic per file under `docs/`, kebab-case name.
- Header quote block with **Document / Status / Priority / Created**
  (`docs/guardrails-autonomous-objectives.md:3-7`).
- Standards docs, like this one, anchor every rule to a repo path + fragment.

## 7. Verification commands

Compile-check every versioned Python file (run from the repo root):

```bash
for f in $(git ls-files '*.py' | grep -v '\.venv' | grep -v __pycache__); do
  python3 -m py_compile "$f" || echo "FAIL $f"
done
```

Syntax-check every shell script (repo + deployed profile copies):

```bash
for f in $(git ls-files '*.sh'); do bash -n "$f" || echo "FAIL $f"; done
find ~/.hermes/profiles/pr-ollama/scripts -name '*.sh' -exec bash -n {} \;
```

`0 FAIL` lines is the pass condition; failures are recorded, not auto-fixed.
