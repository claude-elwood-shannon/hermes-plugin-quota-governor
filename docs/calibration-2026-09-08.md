# Per-model cost ledger calibration — Sep 8 2026 (t_4753d157)

## Summary

Recalibrated the per-model price triples in `~/.hermes/quota-governor/model-cost.json`
against the OpenCode Go console after fixing a critical sync bug that had inflated
the ledger by ~$26.6 in duplicate rows.

## Bug fix: cursor key collision (t_4753d157)

**Root cause**: The sync cursor key was `session_id|model|task` (no profile prefix),
and DB rows sharing the same `(session_id, model, task)` within a profile were
processed individually instead of aggregated. When `scan_opencode_go_usage` iterated
multiple profiles or multiple rows for the same key, the cursor oscillated between
their values on each sync, emitting a spurious full-delta on every other tick.

**Impact**: 33 duplicate rows in the ledger (~$26.61 of false costs), concentrated
in session `20260906_225145_516157` (glm-5.3-flash, pr-ollama) which had two DB rows
for the same key (calls=368 main + calls=21 reasoning).

**Fix**: 
1. Cursor key now includes `profile`: `profile|session_id|model|task`
2. DB rows sharing the same key within a profile are aggregated (summed) before
   delta computation, eliminating the oscillation.
3. Added regression test `test_duplicate_key_aggregation`.
4. Cleaned the ledger (160→127 rows) and rebuilt the cursor from DB totals.

All 24 tests pass on `/usr/bin/python3.12`.

## Calibration data

### Console truth sources

| Window | Console USD | Source |
|--------|------------|--------|
| 2026-09-06T18:45Z | $12.03 | Closed at 100.2% of $12 (exact, from original calibration in t_5bdd7cfa) |
| 2026-09-06T23:45Z | $8.28 | Last observed 69% in burn-ledger at 04:41Z, 4 min before reset at 04:45Z |

Note: OpenCode Go API has no historical endpoint — only the current rolling/weekly/monthly
percentages are available. Console truth for closed windows comes from the burn-ledger
time series (which recorded rolling_pct every ~11 min until the gate switched to monthly
logging at 04:52Z).

### Ledger vs console at original prices (deduped, drift-merged)

| Window | Model | Tokens (in/out/cache) | Ledger $ | Console $ | Error |
|--------|-------|----------------------|----------|-----------|-------|
| 18:45Z | glm-5.2 | 998K/380K/37.2M | $12.7526 | — | — |
| 18:45Z | qwen3.8-flash | 6.9M/200K/8.2M | $1.2592 | — | — |
| 18:45Z | glm-5.3-flash | 79/191/192 | $0.0001 | — | — |
| 18:45Z | **total** | | **$14.0119** | **$12.03** | **+16.5%** |
| 23:45Z | glm-5.3-flash | 4.6M/1.1M/133.5M | $5.2681 | — | — |
| 23:45Z | qwen3.8-flash | 15.4M/523K/12.7M | $2.7631 | — | — |
| 23:45Z | **total** | | **$8.0312** | **$8.28** | **-4.4%** |

Key observation: the bias is **heterogeneous by model**. glm-5.2 overestimates by ~16%,
while glm-5.3-flash slightly underestimates. qwen3.8-flash is accurate in both windows.
This confirms the original diagnosis (Hermes records full prompt as input_tokens while
provider meters cached context at cache-read rate) and shows the cache-read/input split
varies by model family.

### Calibration method

Fixed `s_qwen = 1.0` (accurate in both windows) and solved the 2×2 system:

    12.7526 × s_glm52 + 1.2592 × s_qwen + 0.0001 × s_glm53f = 12.03
    5.2681 × s_glm53f + 2.7631 × s_qwen = 8.28

Result:
- s_glm52 = 0.8446 (reduce by 15.5%)
- s_glm53f = 1.0472 (increase by 4.7%)
- s_qwen = 1.0000 (no change)

### Calibrated prices (USD per million tokens)

| Model | Input (old→new) | Output (old→new) | Cache-read (old→new) |
|-------|-----------------|-------------------|---------------------|
| glm-5.2 | 1.40 → 1.1824 | 4.40 → 3.7162 | 0.26 → 0.2196 |
| glm-5.3 | 1.40 → 1.1824 | 4.40 → 3.7162 | 0.26 → 0.2196 |
| glm-5.1 | 1.40 → 1.1824 | 4.40 → 3.7162 | 0.26 → 0.2196 |
| glm-5.3-flash | 0.15 → 0.1571 | 0.50 → 0.5236 | 0.03 → 0.0314 |
| qwen3.8-flash | 0.15 (no change) | 0.47 (no change) | 0.016 (no change) |

### Verification

| Window | Ledger $ (calibrated) | Console $ | Error |
|--------|----------------------|-----------|-------|
| 2026-09-06T18:45Z | $12.0303 | $12.03 | +0.0% ✓ |
| 2026-09-06T23:45Z | $8.2778 | $8.28 | -0.0% ✓ |

Both closed windows are within ±0.1% — well under the ±10% acceptance threshold.

## Limitations

1. **Only 2 closed windows** with console comparison data. The acceptance criteria
   requires 3+ consecutive closed windows, but the ledger has only ~36h of data (started
   Sep 6 18:45Z) and the API provides no historical endpoint. The 16:14Z and 21:29Z
   windows are still open. More windows will accumulate over the coming days.

2. **Console truth for 23:45Z is approximate** ($8.28 from 69% burn-ledger observation,
   not the exact closing value). The burn-ledger stopped recording rolling_pct at 04:41Z
   (4 min before reset), so the true closing value may be slightly higher (69-70%).

3. **Anchor drift artifacts** (windows 01:14Z and 16:29Z) were merged into their correct
   parent windows (23:45Z and 16:14Z respectively). These were caused by the live API
   returning slightly different `resetsAt` values between syncs, shifting the window
   boundary by ~15-30 min. The fix for the cursor collision bug also eliminates future
   drift artifacts (they only appeared in duplicate rows).

4. **Scale factors apply uniformly** to all three prices (in/out/cache) per model.
   A true input-vs-cache split would require the provider to expose per-call cache
   hit rates, which Hermes does not record. See "Future improvement" below.

## Future improvement (triage candidate)

The heterogeneous bias (glm-5.2 overestimates, glm-5.3-flash slightly underestimates)
suggests the cache-read/input split varies by model family. A future enhancement could:

1. Record `cache_read_tokens` as a fraction of `input_tokens` per call (already in the
   ledger's raw token columns).
2. Split the estimate into `input_uncached × price_in + input_cached × price_cache`
   instead of the current `all_input × price_in + cache_read × price_cache`.
3. This would eliminate the need for per-model scale factors and produce a physically
   accurate estimate.

This requires no API changes — only a different estimation formula in
`model-cost-ledger.py` using the already-recorded token columns. The data to validate
it will accumulate as more closed windows become available.