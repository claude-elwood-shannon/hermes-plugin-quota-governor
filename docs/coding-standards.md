# Coding Standards

> **Scope:** All versioned code in `hermes-plugin-quota-governor` (plugin root,
> `scripts/`, `capabilities/`) and the cron scripts deployed to the active
> profile (`~/.hermes/profiles/pr-ollama/scripts/`).
> **Status:** In force (Sep 2026). Every rule below is anchored to a real
> example already in the repo — this documents what we do, not what we aspire to.

## 1. Language policy (no exceptions)

- **Code, comments, docstrings, commit messages: English.** Commits follow
  Conventional Commits (`fix(gate): unify pr-ollama worker pin to gpt-oss:20b`).
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
| Python CLI script (cron entry point) | `kebab-case.py` | `scripts/obs/efficiency-ratio.py`, `scripts/quota-gate.py` |
| Test | `test_<module>.py` (snake) | `test_efficiency_ratio.py`, `scripts/obs/test_trace.py` |
| Shell cron wrapper | `kebab-case-cron.sh` | `scripts/obs/efficiency-ratio-cron.sh` |
| Shell utility | `kebab-case.sh` | `scripts/cron-health-check.sh` |
| Doc | `kebab-case.md` under `docs/` | `docs/guardrails-autonomous-objectives.md` |

**Pairing rule:** every cron Python script has a matching `<name>-cron.sh`
wrapper; the wrapper owns logging and liveness, the `.py` owns logic.

## 3. Python

- **Shebang** `#!/usr/bin/python3.12` on executable scripts
  (`scripts/obs/efficiency-ratio.py:1`); none on importable modules.
- **`from __future__ import annotations`** at the top of every module
  (`quota_governor.py:10`).
- **Module docstring** right after the shebang answering *what + why*, with
  `Usage:` and `Env overrides:` sections when flags/env vars exist
  (`scripts/obs/efficiency-ratio.py:2-50`).
- **Stdlib only** for zero-token observability/audit scripts
  (`efficiency-ratio.py` header: "zero tokens, stdlib only, read-only inputs").
- **Type hints on all public functions** (`quota_governor.py:68`
  `def _get_hermes_home() -> Path:`).
- **Logging** via `logging.getLogger(__name__)`, never bare `print` in modules
  (`quota_governor.py:23`).
- **Functions ≤ 50 lines.** Longer is a refactor signal: split into private
  helpers with no behavior change, verified against a golden-output harness
  (`29a4185` split the 87-line `build_row()` into 5 helpers; `a420ca5`).
- **Python 3.11 is the syntax floor.** The `py_compile` gate below runs on
  3.11, so PEP 701 nested-quote f-strings are banned even though deployed
  copies execute under 3.12 (regression `0a14278`, repair `d6169a8`).
- **Importing kebab-named scripts in tests:** they are not valid module names,
  so tests load them by path with `importlib.util` (`test_efficiency_ratio.py:5`).

## 4. Shell (cron scripts)

- Header: `#!/usr/bin/env bash` + `set -euo pipefail`
  (`scripts/quota-governor-tick.sh:5-6`).
- **Header comment states the cost contract:** `no_agent, zero tokens` and the
  silence contract (`quota-governor-tick.sh:3`, `efficiency-ratio-cron.sh:2`).
- **Watchdog/liveness pattern:** each run appends ONE timestamped tick to the
  shared log that `cron-health-check.sh` watches (hardcoded `HERMES_HOME_DIR`
  there — never `$HERMES_HOME`, which resolves differently under a profile)
  and the script itself stays silent on success (`efficiency-ratio-cron.sh:3-13`).
- **Fail-open:** on missing config log the error and `exit 0`; the watchdog
  detects death via missing ticks, not exit codes (`quota-governor-tick.sh:22-24`).
- **`exec` the Python entry point as the last statement** of the wrapper
  (`efficiency-ratio-cron.sh:17`).

## 5. Shared utilities

- Importable helpers live at the repo root (`repo_sync_drift.py`: md5 +
  recursive deploy-drift scan used by the cron sync checks, `7f3955e`);
  `scripts/` groups executables by function (`obs/`, `bridge/`, `hermes-backup/`).
- Extract duplicated logic into one module as soon as copies appear:
  `373df60` purged the per-cron deploy-drift stubs into `repo_sync_drift.py`.

## 6. Git hygiene

- **Never commit runtime state:** `tmp/`, SQLite sidecars `*.db-shm` /
  `*.db-wal`, metrics `*.jsonl` state files, `STOP` signals, `*.pid` files,
  `__pycache__/`, `.worktrees/` (see `.gitignore`).
- One logical change per commit; the body explains *why* when the diff does not.
- **Authorship:** the human is the commit Author —
  `Claude Elwood Shannon <claude.el.shannon@proton.me>` — and AI-authored
  work adds the trailer `Co-authored-by: Hermes Agent <agent@nousresearch.com>`
  (see `29a4185`, `ddb95cc`). Set them per commit with
  `git -c user.name=… -c user.email=… commit`; never invent identities.

## 7. Documentation

- One topic per file under `docs/`, kebab-case name; header quote block with
  **Document / Status / Priority / Created**
  (`docs/guardrails-autonomous-objectives.md:3-7`).
- Standards docs, like this one, anchor every rule to a repo path or commit sha.

## 8. Syntax gate (before every commit)

```bash
fail=0
for f in $(git ls-files '*.py'); do python3 -m py_compile "$f" || { echo "FAIL $f"; fail=1; }; done
for f in $(git ls-files '*.sh'); do bash -n "$f" || { echo "FAIL $f"; fail=1; }; done
find ~/.hermes/profiles/pr-ollama/scripts -name '*.sh' -exec bash -n {} \;
```

`0 FAIL` lines is the pass condition; failures are recorded, not auto-fixed.
