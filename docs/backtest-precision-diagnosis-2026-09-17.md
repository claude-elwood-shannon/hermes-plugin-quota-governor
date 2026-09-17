# Backtest-f2 precision diagnosis — 2026-09-17

Task: t_9e90456f (OBJ-METRICS). Diagnosis only — no predictor changes.

Baseline verdict (`~/.hermes/quota-governor/backtest-f2-verdict.json`,
computed 2026-09-17T02:38:09Z):

```json
{"precision_ratio": 0.18502202643171806, "n_ok": 42, "n_fail": 185,
 "n_open": 16, "computed_at": "2026-09-17T02:38:09Z"}
```

The 0.185 baseline was **reproduced exactly** from the ledger
(`forecast-backtest.jsonl`: 514 `snap`, 1285 `res`, 44 `day` rows): 21 OK
day-cells summing `n_ok=42`, 3 FAIL day-cells (2026-09-14/15/16, all
`pr-ollama`) summing `n_fail=185`.

## 1. FAIL decomposition

### 1.1 `res` status x provider

| provider    | OK  | FAIL | NA (total) | NA `pct_already_past` | NA `no_prediction` | NA `no_cross_before_reset` |
|-------------|-----|------|------------|------------------------|--------------------|----------------------------|
| pr-ollama   | 62  | 185  | 267        | 10                     | 146                | 111                        |
| pr-nanogpt  | 23  | 0    | 491        | 485                    | 6                  | 0                          |
| pr-opencode | 19  | 0    | 238        | 220                    | 18                 | 0                          |
| total       | 104 | 185  | 996        | 715                    | 170                | 111                        |

**All 185 FAILs belong to `pr-ollama`.** pr-nanogpt and pr-opencode never
FAIL: they spend almost the whole week above 90% (`pct_already_past`), so
there is nothing measurable to predict for them.

### 1.2 FAIL error_pct bins x provider

| error_pct bin | pr-ollama | pr-nanogpt | pr-opencode |
|---------------|-----------|------------|-------------|
| 20-40         | 29        | 0          | 0           |
| 40-60         | 23        | 0          | 0           |
| 60-100        | 48        | 0          | 0           |
| 100-200       | 31        | 0          | 0           |
| >=200         | 54        | 0          | 0           |
| total         | **185**   | 0          | 0           |

### 1.3 Shape of the failures

- **Single crossing event.** All 185 FAILs resolve against the ONE actual
  crossing `2026-09-16T23:52:54Z`. They are the snapshots taken on
  Mon-Wed 09-14..09-16 (60+62+63), i.e. after the weekly reset, while
  ollama was still idle.
- **Direction.** 177/185 predicted LATE (eta_90 after the real crossing),
  only 8 EARLY.
- **Mode A — prediction beyond the weekly window (105/185).** The
  predicted eta_90 falls AFTER the reset of the snapshot's own window
  (39 of them >720 h out; worst: year 2069). All 54 `>=200` and all 31
  `100-200` bins are here. Cause: `burn_rate <= ~1e-4 %/min` at snap
  (30 snaps have burn <0.001; pct_now <30 in 122/185) produces
  near-infinite etas. The crossing then happened anyway on Wed 23:52.
- **Mode B — prediction inside the window (80/185).** Bins: 20-40: 29,
  40-60: 23, 60-100: 28. Absolute timing error: median ~58 h
  (q75 ~79 h, max ~96 h), always LATE. The 6 h EMA burn window lags the
  re-acceleration of consumption after the reset.
- **Confidence gate is blind.** All 185 FAIL snaps carry `confidence=3`.
- **Margins are identical across modes** (median ~5.6 days to reset):
  the FAIL/OK split is not a windowing artifact. OK snaps (n=104)
  predict with median absolute error of only ~4 h; 49/104 OKs are taken
  at pct_now >= 70 (late-week, fast-burn regime).

### 1.4 Do the NAs hide failures?

No. The 111 `no_cross_before_reset` rows (all pr-ollama, days 09-08..13)
had NO predicted eta inside their window — zero hidden-fail candidates.
They are in fact **111 correct directional predictions** ("will not reach
90% before reset") that the metric counts as nothing. The `pct_already_past`
(715) and `no_prediction` (170; burn<=0 at snap) rows carry no predictive
value by design and are correctly excluded.

## 2. Dominant causes

1. **Verdict aggregation bias in `backtest-f2.py` (metric bug, biggest
   single lever).** `main()` sums `n_ok` ONLY across day-cells whose
   latest verdict is OK (42 credited) and `n_fail` across FAIL cells
   (185). The 62 OK `res` rows that sit on the three FAIL days — a
   majority of them, e.g. 2026-09-16 has 27 OK vs 63 FAIL — are
   invisible to `precision_ratio`. Any day that fails once zeroes all
   its correct predictions. This alone turns a 104/289 (0.360)
   res-level accuracy into 0.185.
2. **Post-reset idle-regime underestimation (predictor-side, mode A).**
   With pct_now <30 and burn ~1e-6..1e-4 %/min right after the reset,
   `eta_horas()` divides by a near-zero burn and emits etas of months to
   decades. 105 such FAILs (+ the 111 correct no-cross NAs) all come
   from this regime; the single real crossing on Wed 23:52 proves the
   "idle" estimate wrong. 81/185 predicted horizons exceed 1 week.
3. **EMA burn lag during re-acceleration (predictor-side, mode B).**
   The 6 h EMA window (ALFA=0.3, VENTANA_SEG=6h) reacts slowly when
   consumption picks up; mid-week predictions from pct_now 50-90 are
   systematically LATE by ~1-4 days (error_pct 20-100).

## 3. Proposed fixes and estimated impact

Impact computed by re-classifying the existing ledger population with
verdict-file semantics (day-cells, then the same aggregation as
`backtest-f2-verdict.json`). Fairness check for C1: **0 OK snaps** have a
predicted eta beyond their window, so the reclassification only removes
unmeasurable/failed predictions, never credits.

| # | Fix | Where | n_ok | n_fail | precision_ratio |
|---|-----|-------|------|--------|-----------------|
| — | baseline (today) | — | 42 | 185 | 0.185 |
| C2 | Count `n_ok` from ALL resolved day-cells, not only OK-verdict cells | `main()` verdict aggregation | 104 | 185 | **0.360** |
| C1 | Snapshots whose predicted eta_90 lies beyond their own weekly window -> `NA` (`reason="pred_beyond_window"`): not measurable within the margin-normalized error | `evaluate_snapshots()` for new rows + reclassify at aggregation for history | 104 | 80 | **0.565** |
| C3 | Credit `no_cross_before_reset` as a correct directional prediction (counts as OK) | aggregation side | 153 | 80 | **0.657** |
| C4 | Tolerance 20% -> 30% (relaxes the OBJ-24 close criterion) | `TOL_PCT` | 172 | 61 | **0.738** |

Notes:

- C2 is a pure metric-integrity fix: it stops discarding 62 already-earned
  OKs. No new credit created; effect +0.175.
- C1+C2 give 0.565; C1 alone (fair version) equals C1+C2 because no OK is
  excluded. The 80 mode-B FAILs are kept FAIL — they are real timing
  errors, not bookkeeping.
- C1+C2+C3 = **0.657** is the honest ceiling of bookkeeping fixes: the
  predictor is still wrong in the post-reset regime, and the metric then
  says so.
- C4 crosses the 0.7 gate but by relaxing the 20% criterion itself —
  a decision for the operator, not something to slip in. The 61 remaining
  FAILs at tol=30 are still mode-B late errors.
- **The real fix for the residual 80 FAILs is predictor-side and out of
  scope for this card**: a burn-rate floor / robust rolling rate for the
  post-reset regime (fixes mode A at the source: finite etas that are
  wrong-but-measurable), plus a faster-reacting window for mode B. No
  numeric estimate is possible without implementing it; it should be a
  follow-up card.

### Implementation sketch (minimal, no ledger rewrite)

`res` rows are immutable (`resolved` set), so apply C1/C3 as
reclassification at aggregation time (in `day_verdicts()` /
`_day_verdict_phase()` / the verdict-file block of `main()`, joining each
`res` with its snap's `reset` and `eta_90_pred`), and additionally in
`evaluate_snapshots()` so future rows are recorded correctly. Also move
the verdict-file recomputation out of `if new_records:` (or force one
recompute) — otherwise the aggregation fix never reaches
`backtest-f2-verdict.json`, since unchanged day-verdicts emit no new rows.

## 4. Caveats

- Only ~3 days of post-reset history exist for the failing regime; the
  scenario numbers above are this week's decomposition, not a validated
  forecast of future weeks.
- `n_open` in the verdict file (16) is a snapshot taken at 02:38 UTC;
  latest day-rows now show 4 open snaps. Not material to the ratio.
- The whole FAIL population hangs on ONE crossing event; week-over-week
  variance is unmeasured.

## 5. Reproduction

Scripts: `analyze_fails.py`, `analyze_part2.py`, `analyze_part3.py`,
`analyze_part4.py` (task workspace
`~/.hermes/kanban/workspaces/t_9e90456f/`), run with `/usr/bin/python3.12`
against `~/.hermes/quota-governor/forecast-backtest.jsonl`. Key outputs
quoted verbatim in sections 1-3; baseline cell recount:

```
BASELINE original agg: cells={'OK': 21, 'FAIL': 3} n_ok=42 n_fail=185 ratio=0.185
```
