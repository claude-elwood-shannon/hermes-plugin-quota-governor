# Backtest Precision Report

## Methodology
The precision is calculated as the ratio of successful predictions (`OK`) to total evaluated predictions (`OK` + `FAIL`) across the last 7 UTC days (2026‑09‑09 to 2026‑09‑15) for each provider and globally. This follows the definition in the docstring of `scripts/backtest-f2.py`, which treats a snapshot as evaluated only if an actual crossing time was observed before the weekly reset. Snapshots that occur after the crossing, lack an usable `eta_90_iso`, or are still open are counted as `NA` and excluded from the precision calculation, matching the backtest logic.

## Daily Verdict Statistics (7 days, UTC)
| Provider   | n_ok | n_fail | ratio_precision |
|------------|------|--------|-----------------|
| pr‑nanogpt | 7    | 0      | 1.000 |
| pr‑ollama  | 7    | 0      | 1.000 |
| pr‑opencode | 5    | 0      | 1.000 |
| **Global** | 19   | 0      | 1.000 |

The precision exceeds the minimum threshold of 0.7 required by the OBJ‑METRICS criterion. Therefore the backtest passes.

ratio_precision: 1.0
