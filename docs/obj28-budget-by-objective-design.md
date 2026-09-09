# OBJ-28 — Budget by Objective: design, implementation plan, and cost estimate

**Status**: DESIGN (class C — docs/design, promotable under current rules). Nothing is built.
**Date**: 2026-09-10
**Source tasks**: t_d540cee7 (OBJ-28 sketch), t_ee079cfa (OBJ-27 observability), OBJ-24 F3 (budget_check.py), OBJ-26/26a (provider balance budget), OBJ-07 (model fitness).
**Prerequisite**: OBJ-27 Fase 0 (trace with join by `objective:` tag). This design defines the contract between the two.

---

## 1. Architecture

### 1.1 The gap this fills

Three budget layers already exist:

| Layer | Mechanism | Currency | Gate point |
|-------|-----------|----------|------------|
| Provider (OBJ-26) | `nanogpt_max_balance_spend_usd`, `window_usd` in `model-cost.json` | USD balance / window % | dispatch (gate) |
| Task (OBJ-24 F3) | `cost:` tag → `COST_CLASS_PCT` vs free quota | % of provider window | creation + dispatch (`budget_check.py`) |
| **Objective (OBJ-28, this design)** | `objective-budgets.json` rollup by `objective:` tag | **dual: costUsd + quota_pct** | creation + dispatch (new hook) |

The objective is the aggregate container: a single `OBJ-xx` spans many tasks, and no single task-level check can bound the *sum*. OBJ-26's monolith (5 crashes, ~$0.30–0.40 before a human saw the pattern) is the motivating case: a $1 objective budget with a variance rule would have re-scoped at crash 2.

### 1.2 Data model — `objective-budgets.json`

Portable config + state, stored under `get_hermes_home()/quota-governor/` (never absolute paths in the repo). Schema:

```json
{
  "_meta": {
    "version": "1.0",
    "generated_at": "2026-09-10T00:00:00Z",
    "note": "Auto-managed. Manual edits are overwritten by the rollup."
  },
  "objectives": {
    "OBJ-28": {
      "budget_usd": 1.00,
      "budget_quota_pct": 5.0,
      "currency": "usd",
      "status": "open",
      "warn_fraction": 0.7,
      "hard_stop_fraction": 1.0,
      "spent_usd": 0.0,
      "spent_quota_pct": 0.0,
      "spent_currency": "usd",
      "last_rollup_ts": "2026-09-10T00:00:00Z",
      "tasks_total": 0,
      "tasks_done": 0,
      "variance_breach": null,
      "notes": []
    }
  }
}
```

Field semantics:

- **`budget_usd` / `budget_quota_pct`**: the two budget ceilings. Exactly one is the *active* currency (`currency`); the other is informational. The gate asks in the currency the assigned provider bills (see §2).
- **`status`**: `open` → `warn` (spent ≥ `warn_fraction` × budget) → `exhausted` (spent ≥ `hard_stop_fraction` × budget) → `done` (all tasks done, no further spend expected).
- **`spent_usd` / `spent_quota_pct`**: auto-rolled up from the trace (OBJ-27 F0), never hand-edited.
- **`variance_breach`**: set when a single task's actual spend exceeds its `cost:`-class estimate by >3× (see §5). Triggers a human gate.
- **`notes`**: append-only human/agent decisions (extend, re-scope, stop).

### 1.3 Rollup — from the trace

The rollup is a pure function over the OBJ-27 F0 trace (JSONL under `quota-governor/obs/`). Each trace line carries `objective` (the `objective:` tag stamped at creation) plus `model`, `provider`, `tokens`, `costUsd`, `requestId`. The rollup:

1. Reads all trace lines for a given `objective:` tag in the current window.
2. Sums `costUsd` (real, from balance providers) and `quota_pct` (window fraction, from window providers) **separately** — never mixed.
3. Writes `spent_*` back into `objective-budgets.json`.

**Contract with OBJ-27 F0** (this is the interface the two designs must agree on):

- OBJ-27 F0 must emit a `objective` field on every trace line, populated from the `objective:` tag stamped on the task body at creation (not parsed ad-hoc at rollup time — the stamp is the enforcement point, §1.4).
- The rollup consumes the trace **read-only**; it never writes to the trace.
- Unattributed spend (no `objective` field) falls into an explicit `unattributed` bucket that the report shows, never hides — same rule as OBJ-27's `unattributed` consumer class.

### 1.4 Gate hooks — creation and dispatch

Two hooks, both **observer-only by default** (log; no veto until `--enforce`):

**Creation hook** (fires when a task with an `objective:` tag is created):
1. Stamp the `objective:` tag into a structured field (today it is textual and fragile — the sketch's point 2). The stamp is the join key for the rollup.
2. Compute `remaining = budget - spent - est(task)`, where `est(task)` comes from the `cost:` class (F3's `COST_CLASS_PCT`).
3. If `remaining < 0` → log a `warn`/`exhausted` decision (see §3 for the phase-dependent action).

**Dispatch hook** (fires before a worker spawns, alongside the existing gate):
1. Re-read the rollup (fresh).
2. If the objective is `warn` → allow but log; if `exhausted` → do not dispatch; route to the human gate (extend / re-scope / stop) or to the cheapest eligible provider if a re-assign is configured.
3. The dispatch hook asks in the **assigned provider's currency** (§2).

Both hooks are pure additions to the existing `budget_check.py` / gate flow — they do not modify the F3 or OBJ-26 logic, they compose on top.

---

## 2. Dual currency — never compare fractions across providers

The core rule: **a fraction of one provider's window is not comparable to a fraction of another's.** `quota_pct` is only meaningful relative to the window it covers (Ollama session vs OpenCode 5h rolling vs NanoGPT weekly tokens are different denominators). `costUsd` is the only cross-provider-comparable number, and only for balance-billed providers.

Consequences:

- **The gate asks in the currency the assigned provider bills.** For a task assigned to pr-nanogpt (balance/subscription), the gate checks `costUsd` against `budget_usd`. For a task assigned to pr-ollama (window-only, no per-request USD), the gate checks `quota_pct` against `budget_quota_pct`.
- **`budget_usd` and `budget_quota_pct` are both stored**, but only the active one (`currency`) is enforced. The other is a cross-check for the human report.
- **Never sum `quota_pct` across providers** in a single objective's spent figure. If an objective spans providers, the rollup keeps per-provider `spent_quota_pct` sub-buckets and the report shows them separately; only `costUsd` aggregates across providers.
- **`costUsd` is the Goodhart-safe meter** (real money, not tokens — see §5). `quota_pct` is the operational meter for window-billed providers where no per-request USD exists.

---

## 3. Phase plan with estimates

Warm-up-then-scale, the proven F3 pattern. Each phase has a size, suggested provider, USD estimate, quota equivalent, and a verifiable acceptance criterion. Estimates are grounded in measured data: `docs/calibration-2026-09-08.md` (per-model USD), `docs/model-matrix-2026-09.md` (per-task cost), `docs/quota-planner.md` (per-category cost), and the OBJ-07 finding that cheap models can cost *more* per task.

| Phase | Size | Provider | USD est. | Quota equiv. | Acceptance criterion (verifiable) |
|-------|------|----------|----------|--------------|-----------------------------------|
| **0. Observer (7d)** | micro | pr-ollama (deepseek-v4-flash) | ~$0.05–0.10 | <1% of a window | Rollup runs daily for 7d; `objective-budgets.json` populated for ≥3 real objectives; zero false `warn`/`exhausted`; trace `objective` field present on ≥95% of worker lines |
| **1. Calibration** | micro | pr-ollama | ~$0.05 | <1% | Per-objective `spent_usd`/`spent_quota_pct` medians published; budget defaults per class (C→tiny, B/A→proposed) validated against ≥5 done objectives; variance rule tuned |
| **2. Enforce — warn** | tiny | pr-ollama | ~$0.10 | ~1% | `--enforce` with warn-only: objectives flip to `warn` at threshold; no hard-stop yet; ≥7d with zero false `warn` (pattern: watchdog, empty stdout = no decisions) |
| **3. Enforce — hard-stop** | small | pr-ollama | ~$0.15 | ~1–2% | `exhausted` objectives block dispatch; human gate fires (extend/re-scope/stop); ≥7d with zero false hard-stops; variance rule re-scopes a real overrun |

**Total estimated cost: ~$0.35–0.40 USD** across all four phases, all on the cheap worker model (deepseek-v4-flash off-peak, or gpt-oss:20b if the 10.4 proposal is approved). This is within `cost:small` and well under the OBJ-26 monolith's single incident cost.

**Phase gating**: each phase promotes only on its acceptance criterion passing. Phase 0 is the only one that can run observer-only immediately after OBJ-27 F0 lands; phases 1–3 require the user's explicit approval (class B — code + crons).

---

## 4. Decisions for the user

The document ends here. These are the decisions the user must make before implementation tasks are created (class B).

1. **Default budget by class.** Class C (auto-promotable) gets an automatic `tiny` budget (~$0.50 USD or equivalent); class B/A get a *proposed* budget awaiting approval. Confirm the $0.50 tiny default, or set a different figure.

2. **Warn threshold.** `warn_fraction = 0.7` (objective flips to `warn` at 70% of budget). Confirm, or choose 0.5 (matches the provider-level `warn_fraction` in `model-cost.json`).

3. **Hard-stop behavior.** When an objective is `exhausted`: (a) block new dispatch and route to the human gate (extend / re-scope / stop), or (b) auto-reassign to the cheapest eligible provider first, human gate only if none exists. Recommend (a) for the first release — safer, and the variance rule already covers the cheap-model trap.

4. **Interactive chat exclusion.** The judge's interactive chat stays OUT of the objective budget (it is its own consumption, per the sketch). Confirm this exclusion is absolute — objective budgets govern only worker-executed tasks.

5. **Budget extension on exhaustion.** When an objective hits `exhausted`, the extension path is: human approves a new `budget_usd`/`budget_quota_pct` (appended to `notes`), never automatic. Confirm no auto-extension, or set a small auto-extension cap (e.g. +50% once) for class C only.

6. **Variance rule.** A single task whose actual spend exceeds its `cost:`-class estimate by >3× triggers a human review (first breach = review, not punishment — Goodhart mitigation). Confirm the 3× factor, or set another.

7. **Currency per objective.** Each objective declares one active currency (`usd` or `quota_pct`) based on its dominant provider. Confirm the rule "the gate asks in the assigned provider's billing currency" and that mixed-provider objectives keep per-provider `quota_pct` sub-buckets (never summed).

---

## 5. Risks — Goodhart and model fitness, with mitigations

### 5.1 Goodhart: the measured budget becomes the objective

The classic failure: teams optimize the metric, not the goal. Here the risk is that a worker or orchestrator games the budget — e.g. splitting work into many `micro` tasks to dodge a per-task check, or routing to a cheap model that loops.

**Mitigations:**
- **Track `costUsd` real, not tokens.** Tokens are gameable (cache-read vs input split varies by model); real money is not. The rollup sums `costUsd` from balance providers and `quota_pct` from window providers — never token counts as the budget meter.
- **First breach = review, not punishment.** The variance rule (§4.6) treats the first >3× overrun as a signal for human review, not a penalty. This keeps the budget a guardrail, not a target to be gamed around.
- **The report shows the `unattributed` bucket explicitly.** Spend that escapes the `objective:` stamp is visible, not hidden — the same rule OBJ-27 applies to its `unattributed` consumer class. A budget that can be dodged by dropping the tag is a budget that gets dodged; visibility is the first defense.
- **Budget is a blast-radius instrument, not a productivity target.** The sketch's framing: the budget makes class C *safer*, not *smaller*. The design never rewards "under budget" — it only bounds "over budget."

### 5.2 Model fitness: cheap models that loop cost more

OBJ-07's finding: deepseek cost *more* per task than glm on the same work, because it looped. A budget that only looks at price punishes the wrong thing — it would push work to a cheap model that burns the budget in retries.

**Mitigations:**
- **The gate considers fitness, not just price.** The dispatch hook checks the assigned model's *measured* per-task cost (from `model-matrix-2026-09.md` §2.1 and the ledger), not the list price. A cheap-but-looping model is not a valid "cheapest eligible provider" for re-assignment.
- **`est(task)` uses measured per-class cost, not list price.** F3's `COST_CLASS_PCT` is calibrated from real task runs (quota-planner §2.4); the objective gate inherits that calibration rather than re-deriving from $/1M.
- **The variance rule catches loops early.** A looping model drives a single task's spend past 3× its class estimate, which fires the human review gate — before the whole objective budget is consumed.
- **Re-assignment to "cheapest" is fitness-aware.** When the dispatch hook re-assigns an exhausted objective, it picks the cheapest *fit* provider (measured per-task cost on that task type), not the cheapest by list price.

### 5.3 Cross-provider fraction comparison (the dual-currency trap)

Already covered in §2, but worth restating as a risk: summing `quota_pct` across providers produces a meaningless number that can falsely trip or falsely clear a budget. The design's hard rule — per-provider sub-buckets, gate in the assigned provider's currency — is the mitigation, and it is enforced by the rollup schema (no cross-provider `quota_pct` sum field exists).

---

## Sources

| Data | Source |
|------|--------|
| OBJ-28 sketch | t_d540cee7 |
| OBJ-27 F0 trace contract | t_ee079cfa |
| Per-model USD calibration | docs/calibration-2026-09-08.md |
| Need→model matrix, per-task cost | docs/model-matrix-2026-09.md |
| Per-category cost, model fitness (OBJ-07) | docs/quota-planner.md |
| F3 budget_check.py (existing task gate) | scripts/budget_check.py |
| Provider balance budget (OBJ-26/26a) | scripts/nanogpt-balance-ledger.py, model-cost.json |
| Objective progress state | objective-progress.json (weekly-progress.py, OBJ-08) |
