# Guardrails for Autonomous Objectives (OBJ-17)

> **Document:** Design and implementation of the guardrails that constrain the
> governor before it can propose new objectives by itself (OBJ-16).
> **Status:** Implemented (Sep 2026)
> **Priority:** P4 — must be completed BEFORE OBJ-16
> **Created:** 2026-09-01

---

## 1. Overview

OBJ-16 enables the governor to propose new objectives based on patterns it
detects (recurring bugs, improvement opportunities). Before giving it that
capability, we need guardrails that bound what it may and may not do.

This document defines the 11 guardrails, their technical implementation
(script `validate-guardrails.py`), and how they integrate with the task
creator.

---

## 2. The 11 Guardrails

Each guardrail has an ID (GRn), a description, its technical implementation,
and its validation type (static or dynamic).

### GR1: No more than 5 active objectives at once

**Description:** The system must never have more than 5 objectives with
non-finished tasks (ready/running/blocked) at the same time.

**Implementation:** Dynamic. The validator queries `kanban.db`, groups
tasks by `objective:OBJ-N` tag, and counts objectives with at least one
non-terminal task. If >= 5, the proposal is rejected.

**Validation:** `check_max_active_objectives(kanban_db_path) -> GuardrailResult`

### GR2: Touch NOTHING outside the plugin repo

**Description:** Proposed objectives may not suggest modifying files outside
the plugin repo.

**Implementation:** Static. The validator scans the proposed objective text
for file paths. Any path not inside the plugin repo is rejected.
Exception: `~/.hermes/` is allowed by GR3.

**Validation:** `check_file_scope(text) -> GuardrailResult`

### GR3: Touch NOTHING outside ~/.hermes/

**Description:** Objectives may modify files inside `~/.hermes/`
(config, scripts, skills, etc.) but not outside it nor outside the plugin repo.

**Implementation:** Static. Complements GR2. If the text mentions absolute
paths not under `~/.hermes/` or the plugin repo, it is rejected.

**Validation:** `check_file_scope(text) -> GuardrailResult` (combined with GR2)

### GR4: Do not propose objectives that touch system files

**Description:** Critical system files such as `.env`, `.ssh/config`,
`/etc/`, `/var/`, `/proc/`, `/sys/` must not be touched by proposed objectives.

**Implementation:** Static. Path and pattern blacklist. The validator detects
mentions of these files in the objective text.

**Validation:** `check_system_files(text) -> GuardrailResult`

### GR5: Do not propose objectives that require new credentials without approval

**Description:** If an objective requires new API keys, tokens, or
credentials, it must be marked as "requires_human_approval" and never
auto-promoted.

**Implementation:** Static. Keyword detection: "API key", "credential",
"token", "secret", "password", "auth key", "login".

**Validation:** `check_credentials(text) -> GuardrailResult`

### GR6: At most 1 new objective proposed per day

**Description:** The system may not propose more than one new objective per
calendar day. If it already proposed one today, all others are rejected
until tomorrow.

**Implementation:** Dynamic. The validator consults `objective-proposals.jsonl`,
an append-only log of proposals. If an entry with today's date already
exists, it rejects.

**Validation:** `check_daily_proposal_limit(state_file) -> GuardrailResult`

### GR7: Do not propose objectives that modify config.yaml without human approval

**Description:** Any objective suggesting a modification of `config.yaml` must
be marked as "requires_human_approval".

**Implementation:** Static. Detection of "config.yaml" or "config.yml"
in the objective text.

**Validation:** `check_config_yaml(text) -> GuardrailResult`

### GR8: Every proposed objective enters triage

**Description:** Objectives proposed by the governor are NEVER automatically
promoted to "ready". They always enter "triage" so the user decides.

**Implementation:** Dynamic. The script that creates the task uses the flag
`--triage` or `initial_status: "triage"`. The validator checks that the
created task's status is "triage".

**Validation:** `check_triage_only(task_creation_args) -> GuardrailResult`

### GR9: Do not propose objectives that install packages without approval

**Description:** If an objective requires `pip install`, `apt install`,
`npm install`, etc., it must be marked as "requires_human_approval".

**Implementation:** Static. Detection of install commands.

**Validation:** `check_package_install(text) -> GuardrailResult`

### GR10: Do not create, modify, or delete files in any other repo

**Description:** The governor may only touch the plugin repo. Other repos
are forbidden.

**Implementation:** Static. Detects paths under the workspace root that are
not the plugin repo.

**Validation:** `check_other_repos(text) -> GuardrailResult`

### GR11: Do not modify OS files outside ~/.hermes/

**Description:** The governor may not modify OS files outside `~/.hermes/`.
This includes `/etc/`, `/usr/`, `/var/`, `/tmp/` (for persistence), etc.

**Implementation:** Static. Similar to GR4 but broader: any absolute path
not under `~/.hermes/` or the plugin repo is flagged.

**Validation:** `check_os_files(text) -> GuardrailResult` (combined with GR2/GR3)

---

## 3. Implementation Architecture

```
┌─────────────────────────────────────────────┐
│   autonomous-task-creator (cron, 30m)       │
│                                             │
│   1. quota-gate.py (pre-run script)         │
│   2. Agent reads objectives doc             │
│   3. Agent proposes new objective (OBJ-16)  │
│   4. ⚡ validate-guardrails.py              │
│      ├─ GR1: max 5 active objectives        │
│      ├─ GR2/GR3/GR11: file scope            │
│      ├─ GR4: system files blacklist         │
│      ├─ GR5: credential detection           │
│      ├─ GR6: daily proposal limit           │
│      ├─ GR7: config.yaml detection          │
│      ├─ GR8: triage-only enforcement        │
│      ├─ GR9: package install detection      │
│      └─ GR10: other repos                   │
│   5. If all pass → create task in triage    │
│   5b. If any fail → log rejection, skip    │
│   6. Record proposal in proposals.jsonl     │
└─────────────────────────────────────────────┘
```

### Validation flow

1. The task creator (or the script proposing objectives) calls
   `validate-guardrails.py` with the proposed objective text.
2. The script runs the 11 checks.
3. It returns JSON with:
   - `allowed: true/false`
   - `violations: [{id, message}]` (empty when allowed)
   - `warnings: [{id, message}]` (non-blocking but worth attention)
   - `requires_human_approval: true/false`
4. If `allowed: false`, the objective is not created.
5. If `allowed: true` but `requires_human_approval: true`, the objective
   is created in triage with a body that marks "REQUIRES HUMAN APPROVAL" and
   the list of warnings.
6. If `allowed: true` and `requires_human_approval: false`, the objective
   is created in triage without warnings.

### Proposal log

Every proposal (approved or rejected) is recorded in
`~/.hermes/quota-governor/objective-proposals.jsonl`:

```json
{
  "timestamp": "2026-09-01T12:00:00Z",
  "title": "OBJ-20: ...",
  "verdict": "allowed|rejected",
  "violations": []
}
```

---

## 4. Integration with the task creator

The autonomous-task-creator cron prompt is updated to include:

1. Before proposing a new objective (OBJ-16): run
   `validate-guardrails.py --title "..." --body "..."` and respect the verdict.
2. If the validator returns `allowed: false`, DO NOT create the task.
3. If it returns `allowed: true`, create the task in `triage`.
4. Record the proposal in `objective-proposals.jsonl`.

The existing guardrails (G1-G6) of the task creator remain active and apply
BEFORE the new ones (GR1-GR11). The new ones are specific to objective
proposals (OBJ-16), not to task creation for existing objectives.

---

## 5. Allowed and forbidden paths

### Allowed paths (the governor may propose touching)

| Path | Guardrail | Notes |
|------|-----------|-------|
| `<plugin-repo>/**` | GR2 | Plugin repo (resolved from the checkout) |
| `~/.hermes/**` | GR3 | Config, scripts, skills, etc. |
| `~/.hermes/profiles/<active-profile>/**` | GR3 | Active profile |
| `~/.hermes/profiles/<active-profile>/scripts/**` | GR3 | Cron scripts |
| `~/.hermes/profiles/<active-profile>/docs/**` | GR3 | Documentation |

### Forbidden paths (the governor must NEVER propose touching)

| Path | Guardrail | Notes |
|------|-----------|-------|
| `~/.env` | GR4 | System credentials |
| `~/.ssh/config` | GR4 | SSH config |
| `~/.hermes/profiles/*/config.yaml` | GR7 | Requires human approval |
| `<other-repos>/**` | GR10 | Other repos |
| `/etc/**` | GR4, GR11 | System |
| `/var/**` | GR4, GR11 | System |
| `/proc/**` | GR4, GR11 | System |
| `/sys/**` | GR4, GR11 | System |
| `/usr/**` | GR11 | System |
| `/tmp/**` | GR11 | Temporal (not persistence) |

### Paths requiring human approval

| Path/pattern | Guardrail | Reason |
|-------------|-----------|--------|
| `config.yaml` / `config.yml` | GR7 | Hermes central config |
| Any mention of "API key", "credential", etc. | GR5 | Credentials |
| Any `pip install`, `apt install`, etc. | GR9 | Package installs |

---

## 6. Script: validate-guardrails.py

**Location:** `scripts/validate-guardrails.py` in the plugin repo.
**Syntax:**

```bash
# Validate an objective proposal
python3 validate-guardrails.py \
  --title "OBJ-20: Optimize health checks" \
  --body "Refactor health_checks.py to reduce false positives..." \
  --kanban-db ~/.hermes/kanban.db \
  --state-file ~/.hermes/quota-governor/objective-proposals.jsonl

# Output (JSON on stdout):
# {
#   "allowed": true,
#   "violations": [],
#   "warnings": [],
#   "requires_human_approval": false
# }
```

**Options:**
- `--title`: Title of the proposed objective (required)
- `--body`: Body/description of the objective (required)
- `--kanban-db`: Path to kanban.db (default: `~/.hermes/kanban.db`)
- `--state-file`: Path to the proposal log (default: `~/.hermes/quota-governor/objective-proposals.jsonl`)
- `--json`: JSON output (default)
- `--quiet`: Exit code only (0=allowed, 1=rejected)

---

## 7. Completion criteria

- [x] Guardrail design document (this document)
- [x] `validate-guardrails.py` implemented with the 11 checks
- [x] Unit tests for every check
- [x] Task-creator prompt updated with validation instructions
