# Verification-gap audit — efficiency-ratio (OBJ-METRICS), 2026-09-16

Scope: why does `tareas_verificadas` sit at 7 while 77 budget-objective tasks
closed in 24h? This audit replays the verifier's own logic per task, classifies
every unverified task into exactly one cause, and ranks fixes by impact.
Deliverable of kanban task `t_c4858172` (objective:OBJ-METRICS).

## Anchor data point

Last `kind=efficiency_ratio` line in
`~/.hermes/profiles/pr-ollama/quota-governor/metrics-history.jsonl`:

```
{"ts":"2026-09-16T01:00:01Z","ratio":1.42,"veredicto":"BAJO","ratio_7d":0.29,
"veredicto_7d":"CRITICO","tareas_verificadas":7,"tareas_budget_done_24h":77,
"verifier":"full","gasto_usd":4.934368}
```

## Method

The audit imports the shipped verifier
(`scripts/obs/efficiency-ratio.py`) as a module — no logic is re-implemented —
and calls `verified_done_tasks()` / `declared_criterion()` /
`criterion_evidenced()` per task over the 48h window ending at the anchor
timestamp (the 48h sample is a superset of the 24h one; it contains all 7
verified tasks and 78 budget tasks vs 77 in 24h).

Fidelity check: the replay reproduces the shipped numbers exactly — 78/78
budget tasks found, the same 7 verified, **zero** verdict mismatches. All
further classification is diagnostic only (tokens, headings, prose shape);
it never changes what the verifier counted.

## Result: 71 of 78 unverified, three causes

| Cause | Count | Share of unverified |
|---|---|---|
| `success-no-evidenciado` | 55 | 77% |
| `sin-linea-success` | 14 | 20% |
| `bug-verificador` (false negatives) | 2 | 3% |

Per-objective composition of the 48h sample:
OBJ-CODEQUALITY=61; OBJ-AUTODEV=13; OBJ-METRICS=3; OBJ-OBSERVABILITY=1.

The 7 verified tasks (all OBJ-CODEQUALITY but one OBJ-AUTODEV) share one
recipe: the worker's run summary **pastes the literal output of the
success-criterion check commands** (pytest counts, AST `funcs>50: []` lines,
`missing_docstrings: 0 []`, git hashes). That is exactly the evidence shape
`criterion_evidenced()` rewards.

## Task table (representative 28 of 71 unverified)

All 2 `bug-verificador` and all 14 `sin-linea-success` tasks
are listed; `success-no-evidenciado` is sampled across its token-coverage
range (incl. max/min and the one multi-part case).

| task_id | objective | cause | evidence (brief) |
|---|---|---|---|
| `t_67876b3f` | OBJ-AUTODEV | `bug-verificador` | criterion text under `### Success criterion:` heading; evidence complete in run summary; tolerant-reader sim PASSES the same evidence check once the line is recognized |
| `t_88753960` | OBJ-OBSERVABILITY | `bug-verificador` | criterion text under `### Success criterion:` heading; evidence complete in run summary; tolerant-reader sim PASSES the same evidence check once the line is recognized |
| `t_64d3dfa0` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_d4b0ab04` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_5ab4fa68` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_5e8249f8` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_af2f7f96` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_b5612183` | OBJ-AUTODEV | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_dede9563` | OBJ-CODEQUALITY | `sin-linea-success` | criterion present but unrecognized (bare `### Success criterion:` heading (next-line text)); sim still fails evidence |
| `t_ffc142cd` | OBJ-AUTODEV | `sin-linea-success` | no success/criterion mention anywhere in body |
| `t_4b1d8a84` | OBJ-CODEQUALITY | `sin-linea-success` | criterion present but unrecognized (inline-after-#); sim still fails evidence |
| `t_61dc27d8` | OBJ-CODEQUALITY | `sin-linea-success` | criterion present but unrecognized (inline-after-#); sim still fails evidence |
| `t_bcf8c86a` | OBJ-CODEQUALITY | `sin-linea-success` | criterion present but unrecognized (inline-after-#); sim still fails evidence |
| `t_4e2c0897` | OBJ-CODEQUALITY | `sin-linea-success` | criterion present but unrecognized (inline-after-#); sim still fails evidence |
| `t_414b108a` | OBJ-AUTODEV | `sin-linea-success` | no success/criterion mention anywhere in body |
| `t_f8ef9be9` | OBJ-CODEQUALITY | `sin-linea-success` | no success/criterion mention anywhere in body |
| `t_9324c501` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 8/18 (need 9); output does not reach half coverage |
| `t_9ce41c34` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 0/21 (need 10); output does not reach half coverage |
| `t_4f175acd` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 11/25 (need 12); output does not reach half coverage |
| `t_d443b68b` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 9/25 (need 12); output does not reach half coverage |
| `t_ed331493` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 10/33 (need 16); output does not reach half coverage |
| `t_800f06d4` | OBJ-AUTODEV | `success-no-evidenciado` | declared criterion, token coverage 5/19 (need 9); output does not reach half coverage |
| `t_5f27005b` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 8/37 (need 18); output does not reach half coverage |
| `t_810746a4` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 4/25 (need 12); output does not reach half coverage |
| `t_26fc306e` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 4/28 (need 14); output does not reach half coverage |
| `t_3e93ab59` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 2/24 (need 12); output does not reach half coverage |
| `t_1b91f25a` | OBJ-CODEQUALITY | `success-no-evidenciado` | declared criterion, token coverage 1/30 (need 15); output does not reach half coverage |
| `t_833826c2` | OBJ-CODEQUALITY | `success-no-evidenciado` | multi-part body (4 parts), 4 pending, no evidence found |

## Findings

1. **The dominant failure is evidence shape, not missing work.** Average
   criterion-token coverage of the failing single-part outputs is
   0.21 (needed: 0.5). Outputs summarize the work in prose
   ("refactored into helpers, all tests pass") and under-echo the
   criterion's declared vocabulary — even when they paste some literal
   check output (e.g. `t_9324c501`: helpers extracted, 6 tests passed,
   commit pushed, AST check in the summary — still only 8/18 criterion
   tokens). Spot checks show the work was real; the evidence channel
   simply does not reach the half-coverage bar.

2. **The verifier is blind to two criterion line styles it could read.**
   `SUCCESS_TAG_RE` / `SUCCESS_PROSE_RE` are anchored `^\s*success...`, so:
   - inline `## success: <text>` lines (4 tasks) never
     match — the line starts with markdown hashes;
   - bare headings with the criterion on the NEXT line (9
     tasks: 8 `### Success criterion:` MEDIATOR cards plus
     1 `## success:` card) never match.
   A tolerant reader (heading-aware regex + next-line pickup) recognizes
   13 of the 16 bodies the
   shipped parser sees as criterion-less, and **2 of them pass
   the full evidence check unchanged** — those are verifier false
   negatives, classified `bug-verificador`.

3. **Completion vocabulary is not the bottleneck.** Simulating an
   English-enriched `COMPLETION_RE` (verifi*, success(fully), fixed,
   refactored, pushed, ...) flips **0** additional tasks: no failing task is
   above half token coverage while missing only a completion word.

4. **The criterion-less stragglers are process cards, not refactor work.**
   9 MEDIATOR cards, 1 hand-made fix card
   (`t_414b108a`), 1 circuit-breaker crash card (`t_f8ef9be9`), and the
   remaining inline-hash refactors. The three with no criterion text
   anywhere: `t_ffc142cd`, `t_414b108a`, `t_f8ef9be9`.

## Recommendations (ranked by impact on `tareas_verificadas`)

1. **Evidence block in the worker run summary (highest impact, no card-creator
   change).** Convention for every dispatch profile: end each run summary with
   `EVIDENCE:` followed by the literal stdout of each success-criterion check
   command declared in the card (pytest `-q` tail, AST scan lines, `git log
   -1 --format=%H`, ...). This is what all 7 verified tasks already do. It
   attacks the dominant cause directly (55 of 71 unverified)
   and needs only a worker-output convention — the card creator is untouched.

2. **Teach the verifier the two missing criterion styles** (one small patch to
   `declared_criterion()`): allow leading markdown hashes in
   `SUCCESS_TAG_RE`/`SUCCESS_PROSE_RE`, and when the matched line is a bare
   heading, read the next non-empty line as the criterion (optionally joining
   consecutive list items). Immediate effect: +2 verified tasks
   (honest — their evidence already passes), and the remaining
   11 criterion-bearing bodies get correctly classified as
   `success-no-evidenciado` instead of invisibly missing the declaration.

3. **Extend `COMPLETION_RE` with English completions** (free, one line):
   flips 0 tasks today but removes a systematic Spanish-only bias before
   English-first workers become the majority.

Non-recommendation (recorded on purpose): relaxing the half-coverage bar or
counting tasks without a declared success criterion would inflate the ratio
and break the honesty rule ("a done task with no declaration and no evidence
NEVER counts"). The gap closes by fixing evidence shape, not by weakening the
verifier.
