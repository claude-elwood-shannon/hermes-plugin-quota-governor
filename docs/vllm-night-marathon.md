# vLLM Night Marathon — R1-R6 Report

**Date:** 2026-09-11 (night of 10→11 sep)
**Host:** ml-host (LAN), RTX 4060 Ti 16GB, vLLM via systemd `--user`
**Model:** `liodon-ai/Qwen2.5-7B-Instruct-FP8` (compressed-tensors, ctx 32768, hermes parser)
**Worker:** `pr-vllm` — the house's first `/usr/bin/bash` confidential worker ($0/token, zero egress)
**Mandate:** exercise the local worker all night with real tasks, thermal pauses over throughput, no long idle.

---

## 1. Rounds

| Round | Task | Precision / correctness | tok/s (out) | Temp max | JSON failures |
|---|---|---|---|---|---|
| R1 | Classify 10 latest kanban bodies (cost:/privacy: tags) → strict JSON | completed (see §2) | — | recorded | — |
| R2 | Summarize 5 real skills (7-49 KB each) in 3 lines each | 5/5 ok; 3/3 spot-checked claims faithful | 17.3-29.1 | 54 C | 0 |
| R3 | Extract requestId+costUsd from last 50 trace.jsonl lines | **0.98** (49/50 exact; requestId 50/50) | 30.0 | 50 C | 0 |
| R4 | Weekly changelog from `git log --oneline -30` | 21/21 commit hashes real, 0 fabricated | 32.1 | 41 C | n/a |
| R5 | Classify own vllm.err.log (self-observation) | verdict WRONG (see §5) | 9.8 | 42 C | n/a |
| R6 | R5 variation: inject live service state | verdict partially corrected (see §6) | 12.5 | 41 C | n/a |

R6 was a variation of R5 (the round with the most interesting result) — the anti-filler rule selected it deliberately.

## 2. R1 (prior shift, summarized from the task record)

Classification of the 10 most recent kanban bodies against their real `cost:`/`privacy:` tags, run 01:54. The run recorded temperature/thermal metrics; the output artifact landed in `/tmp` and was not retained. **Lesson applied from R2 onward: every marathon round writes to a persistent workspace** (this report is its replacement).

## 3. R2 — large real context

Five homegrown skills (the repo's documentation corpus, 7-49 KB of markdown each) summarized in exactly 3 bullet lines each, one request per skill.

| Skill | Input | Prompt tok | tok/s |
|---|---|---|---|
| hermes-ollama-quota | 18.5 KB | 5 786 | 20.7 |
| hermes-kanban-orchestration | 22.1 KB | 5 988 | 24.1 |
| hermes-quota-aware-dispatch | 49.2 KB | 12 780 | 17.3 |
| kanban-observability | 7.1 KB | 1 892 | 29.1 |
| hermes-acp-integration | 10.5 KB | 2 788 | 26.3 |

Format compliance 5/5 (3 bullets, no preamble). Faithfulness spot-check: the three checkable factual claims (watch is a 0.5 s polling loop, not a WebSocket; OpenRouter key rotation is manual with no API; profiles cannot be hot-swapped mid-ACP-session) all verified against the source files. Throughput degrades gently with context (29 → 17 tok/s from 2k to 13k prompt tokens) but stays usable; 49 KB fits the 32 k window with room to spare.

## 4. R3 — needle extraction

Last 50 lines of the house trace.jsonl (verbatim JSON lines, mixed nulls and floats) → strict JSON array of `{requestId, costUsd}`. Ground truth parsed locally, exact match required.

- **49/50 entries exact; requestId 50/50.** Single miss: entry 36, a 9-decimal float (`0.009495520`) dropped to `null` — a precision-rounding loss, not a hallucination (no invented values anywhere).
- **Zero JSON failures across the marathon.** Strict-array-first-try, no fences, no retries needed. For a 7B at temperature 0, needle extraction into a schema is the strongest observed skill.

## 5. R4 — synthesis

Weekly changelog from the last 30 commit subjects, grouped by theme, hashes cited. Verification: all 21 cited hashes exist in the real commit list — **zero fabricated identifiers**. Themes and groupings coherent. Longest completion of the night (747 tok).

## 6. R5/R6 — self-observation, and the night's real finding

R5: the worker classified its own `vllm.err.log` (39 KB). It got the taxonomy right (KV-cache shortfall, quantization mismatch, HF-hub auth noise, NCCL shutdown warning) but returned **`HEALTH: critical`** — treating boot-time `ValueError`s from the *previous day's model-switch debugging* (both already fixed: ctx now 32768, quantization flag removed) as current faults.

R6 (variation): same log plus **live state injected at query time** (`systemctl is-active` → active; `/v1/models` → serving with `max_model_len 32768`). Verdict improved to `degraded`: it correctly reclassified the KV-cache error as historical, but still claimed the quantization mismatch "persists" while looking at live proof that the server is up.

**Finding:** a 7B reads logs well but does not integrate evidence across time — "error in log" dominates "service demonstrably healthy now". Fix that works with this class of model: pre-digest the temporal question for it (diff the log against a liveness probe, or label each error with a timestamp and the boot it belonged to) instead of asking the model to do the reasoning. Log-health verdicts without such guardrails are **out of the worker's niche**; everything else tested is in.

## 7. Thermal curve

60 health samples across the night (10-min cadence): **41-56 C, zero throttles, zero thermal pauses triggered** (all gates < 75 C before every request; per-round pauses after 5-request batches were respected).

- Idle (day + most of night): 41-47 C, 16-17 W, fan 0%.
- Night peak: 07:00, 56 C @ 105 W / util 100% (R1-era batch).
- This session's rounds peaked at 54 C (largest R2 prompt), returning to 41 C within ~10 min of idle. The 4060 Ti runs this workload class without any fan-curve intervention at 25 C ambient.

## 8. Conclusion — the worker's niche

At $0.00 for ~62.5k prompt + ~2.1k completion tokens, all inside the LAN:

| Strong | Weak |
|---|---|
| Schema-bound extraction (0.98, zero format failures) | Temporal integration (log-history vs live state) |
| Summarization with format constraints (5/5) | Verdicts on unlabeled historical logs |
| Synthesis with verifiable identifiers (0 fabricated hashes) | |
| Changelog/technical drafting | |

**Niche confirmed:** confidential micro/tiny tasks — extraction, summarization, classification, drafting. **Boundary found:** any task whose answer depends on distinguishing "was" from "is" needs the temporal frame supplied externally, or a human/long-model review of the verdict.

**Reproducibility:** round drivers (`r2.py`-`r6.py`, thermal gate built into each), per-round raw outputs (`r2_results.json`, `r3_result.json`, `r4_result.json`, `r5_result.json`, `r6_result.json`), night health log, and the R4 commit list ship with this report in the marathon workspace.

---

*Integrity note: the first R2-R6 successor (t_6066c890) was closed with a placeholder file and no execution; every number above comes from rounds re-run and re-verified this session against ground truth parsed locally (R3), the real commit list (R4), the real log (R5/R6), and the source SKILL.md files (R2).*
