# OBJ-37 — Anonymous collective spending: status (living doc)

**Date**: 2026-09-12 · **Status**: S1 (research + design) delivered and
committed; **S2 not funded, not built — every next step is the owner's
call.**

## What this objective resolves

The owner's idea (10-sep ~17:15): the plugin may OPTIONALLY feed a
decentralized, shared benchmark of AI-agent costs with the spending
statistics the house generates — **objectives, clients, and individual
amounts never travel**. The value is collective (real prices, typical
budgets, mean durations per task class) and reciprocal: "unite the desire
with the budget and the time". The object of study *is* privacy.

The core principle: share the **statistic of spending**, not the history of
the spender. Data is k-anonymized by aggregation, never anonymized by hand.

## What is safe to share vs what never travels

| Safe | Never travels |
|---|---|
| costUsd, model, provider, tokens, coarse task class, duration, coarse date | objective (tag and text), title, consumer_id, requestId, exact timestamps, profiling frequencies |

Acceptance criterion (S3): the user can be a contributor without anyone —
not the aggregator, not us — reconstructing their objectives, their
clients, or their individual amounts.

## S1 delivered: four architectures evaluated (t_efe864dd)

| # | Architecture | Privacy guarantee | Maturity | Verdict |
|---|---|---|---|---|
| (a) | k-anonymous histograms, no backend | k-anonymity (weak, per-bucket) | high | short path, but weak |
| (b) | Federated secure aggregation (Bonawitz et al.) | strong (MPC, honest-but-curious) | high (research) | robust, heavy |
| (c) | Public k-anonymous dataset (parquet, k≥10, optional ε-DP) | k-anonymity + optional DP | high | **recommended short path** |
| (d) | Tari Ootle confidential resources on-chain | confidentiality by design (Pedersen + Bulletproofs + verifiable ElGamal) | low (testnet Igor, pre-mainnet) | **destination when mature** |

Worker's analysis (user decides): ship (c) now, keep (d) on the roadmap and
re-evaluate when Ootle matures. The design document carries the full risk
arithmetic — what spending patterns can fingerprint, what correlation can
de-anonymize — and the payload schema for S2.

## State of the objective today

- Design committed: `docs/privacy-preserving-telemetry.md` (448 lines,
  9 sections, sources verified from the local Tari checkout and the
  privacy literature; no installs, no outbound network).
- Nothing is installed, nothing is activated: the collective layer is
  opt-in, default OFF, with a dedicated kill-switch (`SHARE-STOP`).

## Pending decisions (all owner's)

1. **Architecture** — adopt (c) now, wait for (d), or pursue (b).
2. **k floor** — k=10 on the joint quasi-identifier (worker's floor) or
   raise to k=20 for stronger, coarser guarantees.
3. **Local DP** — add ε≈1.0 Laplace noise for a formal guarantee, trading
   utility.
4. **What to sacrifice** — consciously accept that some spending signal
   (cost + model + coarse class) is shared.
5. **S2 budget** — the anonymizer prototype is NOT covered by the S1
   budget; preliminary estimate 0.40–0.60 USD.
6. **Opt-in posture** — confirm opt-in, default OFF, `SHARE-STOP` before
   any S2 build.

## Where the deep docs live

- Full design + risk arithmetic + payload schema:
  `docs/privacy-preserving-telemetry.md`
- Objective body (idea origin, phases S1→S3): task t_936a736e; S1
  research task: t_efe864dd.