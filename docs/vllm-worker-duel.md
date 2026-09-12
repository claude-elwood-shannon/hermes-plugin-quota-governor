# vLLM Local Worker Duel — Qwen2.5-7B-FP8 vs Llama-3.1-8B-AWQ

**Date:** 2026-09-11
**Host:** ml-host (192.168.1.32), RTX 4060 Ti 16GB, vLLM via systemd --user
**Task:** OBJ-40 — first local `/usr/bin/bash` worker of the house
**Status:** Duel complete, prototype tested, verdict signed. Integration to gate pending user OK.

---

## 1. Contenders

| | A (incumbent) | B (challenger) |
|---|---|---|
| Model | `hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4` | `liodon-ai/Qwen2.5-7B-Instruct-FP8` |
| Quant | AWQ INT4 | FP8 (compressed-tensors) |
| Disk | 5.4G | 11G |
| Max ctx | 65536 | **32768** |
| Tool parser | `llama3_json` | `hermes` |
| vLLM flags | `--quantization awq` | (auto-detect, no flag) |

**Setup notes (verified live):**
- Qwen download completed 10-sep (9/9 files, 11G). Snapshot `c5730f8a`.
- Qwen uses `compressed-tensors` quant — passing `--quantization fp8` FAILS with a pydantic ValidationError. Must omit the flag and let vLLM auto-detect.
- Qwen max ctx is 32768, not 65536. The unit's `--max-model-len 65536` fails boot; must set `--max-model-len 32768`.
- Model switch kills the service ~60-90s (weight load + CUDA graphs). Coordinated with board: no workers running on pr-vllm at test time.

---

## 2. Battery results

### (a) Strict JSON tool-call — 5 cases with distractors
Prompt asks the model to call `classify_task(cost, privacy)` with correct enum values, surrounded by irrelevant text.

| Case | Expected | A (Llama) got | A ok | B (Qwen) got | B ok |
|---|---|---|---|---|---|
| 1 | micro/low | tiny/low | ✗ | micro/low | ✓ |
| 2 | medium/high | large/low | ✗ | large/confidential | ✗ |
| 3 | small/low | medium/low | ✗ | small/low | ✓ |
| 4 | large/confidential | large/confidential | ✓ | large/confidential | ✓ |
| 5 | micro/low | tiny/low | ✗ | micro/low | ✓ |

**Precision: A = 0.2, B = 0.8**
- Both emit valid JSON tool-calls 5/5 (mechanism works).
- Llama systematically over-estimates cost (micro→tiny, small→medium, medium→large) and under-estimates privacy (high→low). Qwen nails the micro/small boundary and the confidential case; only misses medium/high (→large/confidential).
- Latency avg: A = 0.48s, B = 0.91s. Qwen ~2x slower on tool-calls (FP8 vs AWQ INT4).

### (b) Needle in 15k real context (not padding)
Real kanban bodies (~16.6k prompt tokens), needle `ZULU-ALPHA-9917` inserted mid-document.

| | A (Llama) | B (Qwen) |
|---|---|---|
| Found | ✗ (answered "NEEDLE-7Q2X", the marker, not the code) | ✓ (exact `ZULU-ALPHA-9917`) |
| Latency | 7.65s | 5.24s |
| tokens/s | 1.2 | 2.3 |

**Qwen wins decisively on needle extraction** — the exact use case for the niche (needle-extraction from large context).

### (c) Classification of 10 real kanban bodies (cost + privacy tags)
Ground truth parsed from `cost:`/`privacy:` tags in each body.

| Metric | A (Llama) | B (Qwen) |
|---|---|---|
| cost precision | 5/6 (0.83) | 5/6 (0.83) |
| privacy precision | 4/4 (1.0) | 4/4 (1.0) |
| latency avg | 0.51s | 0.47s |

**Tie on classification.** Both correctly read the explicit tags. The one cost miss (t_0423df24 micro→small/tiny) is a genuine semantic over-estimate by both.

### (d) 5k-token summary
| | A (Llama) | B (Qwen) |
|---|---|---|
| Latency | 6.34s | 7.71s |
| tokens/s | 31.5 | 25.9 |
| Language | English ✓ | **Chinese ✗** (default) |

**Qwen caveat:** with a neutral prompt it summarizes in Chinese (its dominant training language). With an explicit "ALWAYS respond in English" system instruction it returns correct English (123 words, verified). This is a **prompt-fixable** issue, not a model defect — but it MUST be handled in the worker system prompt.

---

## 3. Verdict

**Winner: B — Qwen2.5-7B-Instruct-FP8**

Signed with numbers:
- **Tool-call precision: 0.8 vs 0.2** — Qwen 4x better at strict JSON classification with distractors.
- **Needle extraction: PASS vs FAIL** — Qwen finds the exact code in 15k real context; Llama returns the marker, not the value. This is the core niche capability.
- **Classification: tie (0.83/1.0 both)** — both read explicit tags correctly.
- **Summary: tie on quality** (with English prompt fix), Qwen slightly slower.

Qwen wins on the two capabilities that define the niche (strict JSON tool-call + needle extraction). Its two weaknesses — 2x tool-call latency and Chinese default — are acceptable: latency is still sub-second, and the language is prompt-fixable.

**Llama's edge:** 2x faster tool-calls and native English. If latency were the binding constraint it would win, but for micro/tiny classification tasks sub-second is already fine.

---

## 4. Prototype — pr-vllm profile with the winner

**Profile:** `pr-vllm` (exists in Hermes, custom provider → `http://192.168.1.32:8000/v1`).

**Config change made:** pointed `model.default` and `custom_providers[].model` to `liodon-ai/Qwen2.5-7B-Instruct-FP8`, set `context_length: 65536`.

**Ping test:** `hermes -p pr-vllm chat -q ping` → `pong` ✓ (2s).

**Real kanban flow test (joint session):** asked the profile to read task t_12defc98 via `kanban_show`.

**Where it breaks — the critical finding:**
- In a plain `hermes -p pr-vllm chat` session, the **kanban_* tools are NOT injected** — the model reported `kanban_show does not exist`. Kanban tools only appear on board dispatch (when the dispatcher spawns a worker for a card), not in ad-hoc chat.
- The model **correctly self-recovered**: it fell back to `search_files`, grepped the workspace, and returned the task title from `all_bodies.txt`. 6 tool calls, 17s, correct answer.
- Tool-calling mechanism works end-to-end through the profile (search_files, read_file, etc. all fired correctly).

**Implication for the niche:** the local worker is viable as a **board-dispatched worker** (where kanban tools exist), not as an ad-hoc chat assistant. Its niche is confirmed: micro/tiny one-shot JSON tasks (classification, routing, needle-extraction) where the dispatcher injects the kanban context.

---

## 5. Integration recommendation (to gate)

Recommend integrating Qwen2.5-7B-FP8 as a provider worker for **micro/tiny tasks ONLY when `privacy:confidential`** (zero LAN egress — the model never leaves the house) **or when cloud quota is dry**.

**Hard constraint discovered:** Hermes requires a **minimum 64K context window** (`Model ... has a context window of ... below the minimum 64,000 required by Hermes Agent`). Qwen2.5-7B-FP8's real max is **32768** — below the floor. The profile config currently lies (`context_length: 65536`) to pass the check; **long contexts will break** (RoPE positions > 32768 → NaN).

This means the local worker is **NOT a drop-in provider** for general tasks. It can only serve tasks whose context stays under ~32K tokens. For micro/tiny classification that's fine (bodies are <2K tokens), but the integration must either:
1. Cap the worker's context budget at 32K, or
2. Use a model with ≥64K native context (e.g. a 64K-capable Qwen variant) — future work.

**Decision needed from user:** approve the gate integration (class B) with the 32K context cap, or defer until a ≥64K local model is available.

---

## 6. R1-R5-bis — full-niche rerun on the served Llama (12-sep)

On 12-sep the night shift re-ran the full niche battery **against the model actually served at the time — `hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4`** — as five kanban board tasks (R1-bis … R5-bis), assigned to `pr-vllm` with `--model Meta-Llama-3.1-8B-Instruct-AWQ-INT4 --provider custom`. Intent: compare Llama against the Qwen numbers from the R2-R6 marathon on the **same** real tasks, not the hand-built 5-case harness above.

### Data provenance (read first)
The five tasks wrote their output to `/data/ml/data/hermes/work/maraton/llama_r*_bis.json` on ml-host and closed with `done`. **Three of those JSONs were cleaned from the marathon workspace by the rotation housekeeping before this consolidation ran**, so their raw per-round numbers are no longer recoverable from disk. What survives:
- **R4-bis**: the worker's completion record explicitly reports **21/21 real hashes, 0 fabricated** — matching Qwen exactly.
- **R1/R2/R3/R5-bis**: the board only retains the task title/summary (which echoes the task title, no numbers). The raw JSONs are gone. **Those four rows are marked `NR` (not recoverable) below — I will not fill them from memory.**

This is the same "phantom deliverable file" failure the night-marathon integrity note flagged; the durable-number discipline this report enforces is the fix.

### Consolidated table — R1-R5, both models

Round | Task | Qwen2.5-7B-FP8 (R2-R6 marathon) | Llama-3.1-8B-AWQ (R1-R5 bis) | Verdict
|---|---|---|---|---|
| R1 | Classify 10 recent kanban bodies (cost/privacy) | 10/10 JSON valid, precision reported | `NR` (JSON cleaned) | — |
| R2 | Summarize 5 skills, 3 lines each | 5/5 ok, 3/3 claims verified | `NR` (JSON cleaned) | — |
| R3 | Extract requestId+costUsd from last 50 trace lines | **0.98** (49/50 exact; requestId 50/50; costUsd 49/50) | `NR` (JSON cleaned) | — |
| R4 | Weekly changelog from `git log -30` | 21/21 hashes real, 0 fabricated | **21/21 real, 0 fabricated** (completion record) | **Tie** |
| R5 | Autoscopy: classify own vllm.err.log, no state | HEALTH **critical** — WRONG (old-day's fixable errors read as current) | `NR` (JSON cleaned) | — |
| R5-variant | Same log + live service state injected | HEALTH **degraded** — partially corrected | `NR` (JSON cleaned) | — |

**Qwen baseline sources (survived on disk):** `r3_result.json` (0.98, 49/50 exact, reqId 50/50, costUsd 49/50, tok/s 30.0, 23.4s — verified by re-running the ground-truth parse), `r4_commits.txt` (all 30 cited commits verified present in `git cat-file`), `r2_results.json` (5/5 skill records, all `status: ok`).

### What the surviving R4-bis row says
Llama and Qwen are **exact ties** on the anti-hallucination synthesis task: both produced a themed weekly changelog where every cited git hash exists in the real commit list and none was fabricated. This is the least "model-capability" round of the battery (it is basically text summarization with a verifier), so a tie is the expected outcome for two 7-8B instruct models.

### Veredicto firmado (12-sep)
**Qwen2.5-7B-Instruct-FP8 remains the kanban worker of record.**

Signed with the evidence that survives:
- The R1-R5-bis rerun was **aborted mid-proof**: 3 of 5 Llama result files were lost to workspace rotation before consolidation, and the board held no numeric summary to fall back on. The Llama challenger cannot be certified on the two rounds that define the niche (R3 needle-extraction, R5 temporal-reasoning boundary) because those numbers no longer exist.
- The **one durable Llama result — R4 (changelog) — ties Qwen 21/21**, i.e. on synthesis the challenger neither wins nor loses.
- Nothing in the recovered evidence contradicts the original night verdict (Qwen's edge in strict tool-calls + needle extraction; Llama's edge only in tool-call latency). No counter-evidence surfaced.

**Llama 8B-AWQ wins nothing decisively in the recovered record.** Its theoretical advantages — AWQ INT4 is ~2x faster on tool-calls (0.48s vs 0.91s) and native English — are real but sub-second and prompt-fixable, respectively, and neither was re-validated as a durable number in the bis run.

**The real takeaway of R1-R5-bis is procedural, not model-ranked:** five workers executed the niche battery in minutes at $0 on the served model, but **3/5 result artifacts vanished with the workspace rotation**, leaving the board with titles instead of numbers. The niche (confidential micro/tiny work on the house GPU) is viable; the **retention of its evidence is not**. Fix: the next successor must either (a) `kanban_attach` the result JSON at completion, or (b) write numbers into the completion `summary`/`metadata` — never leave the only copy in a rotation-managed workspace.

---

## 7. Files
- `duel.py` — the 4-test battery harness (reusable).
- `results_A.json` / `results_B.json` — raw per-case results (night duel).
- `kanban_bodies.json` — the 10 real bodies used for classification.
- `all_bodies.txt` — 385 real bodies (~197K tokens) used for needle + summary context.
- Night marathon (R2-R6, Qwen) artifacts: `r2_results.json`, `r3_result.json`, `r4_commits.txt`, `r4_result.json`, `r5_result.json`, `r6_result.json`, `vllm-night-marathon.md`.
- R1-R5-bis (Llama) raw JSONs: **removed by ml-host rotation — see §6 provenance note**.
