# Privacy-Preserving Collective Telemetry — OBJ-37

> **What this is:** the design of the *collective layer* of OBJ-37 — how a
> house (an AI-agent operator) can contribute *spending statistics* to a
> shared, decentralized benchmark of AI-agent costs **without exposing its
> objectives, its clients, or its individual amounts**. This is a design
> document, not a build: nothing is installed, nothing is activated, and the
> final architecture choice and the S2 budget are decisions for the user.
>
> **Status:** research + design (S1). **Date:** 2026-09-10.
> **Budget:** 0.60 USD approved (WARN 0.30, hard STOP 0.60). **Privacy:** high
> — the object of study *is* privacy.

---

## 0. Executive summary (30 seconds)

Four architectures were evaluated for sharing AI-agent spending statistics
without leaking objectives, clients, or individual amounts:

| # | Architecture | Privacy guarantee | Backend | Maturity | Verdict |
|---|---|---|---|---|---|
| (a) | k-anonymous histograms, no backend | k-anonymity (weak, per-bucket) | none | high | **Short path, but weak** |
| (b) | Federated secure aggregation | strong (MPC, honest-but-curious) | community server | high (research) | Robust, heavy |
| (c) | Public k-anonymous dataset (parquet, k≥10, optional DP) | k-anonymity + optional ε-DP | none (publish) | high | **Recommended short path** |
| (d) | Tari Ootle confidential resources on-chain | **confidentiality by design** (Pedersen + Bulletproofs + verifiable ElGamal) | decentralized (validator nodes) | **low (testnet Igor, pre-mainnet)** | **Destination when mature** |

**Recommendation (worker's analysis, user decides):** **(c) is the short path**
— publish a k-anonymous, optionally differentially-private parquet dataset
that anyone can sum; it fits the house's "publicable" identity, needs no
backend, and is verifiable. **(d) Tari Ootle is the destination** — confidential
resources give *confidentiality by design* at the base layer, which no
k-anonymity scheme can match, but the network is pre-mainnet (testnet Igor,
frequent resets) and carries fees + validator-node complexity. The honest
path is: **ship (c) now, keep (d) on the roadmap and re-evaluate when Ootle
matures.**

---

## 1. The problem, precisely

The house already records a per-request trace (`trace.jsonl`) with fields
like `costUsd`, `model`, `provider`, `tokens`, `consumer_class`, `duration`,
and `date`. The collective layer wants to share **the statistics of spending**
— not the history of the spender.

**What is safe to share:** `costUsd`, `model`, `provider`, `tokens`, task
class, duration, coarse date (day/week).

**What must NEVER travel:** `objective` (tag and text), `title`, `consumer_id`,
`requestId` (re-identifiable), exact timestamps (only coarse-grained),
frequencies that profile.

The core principle: **share the aggregate, not the individual.** Data is
k-anonymized by aggregation, not anonymized by hand. The acceptance
criterion (S3): *the user can be a contributor without anyone — not the
aggregator, not us — reconstructing their objectives, their clients, or their
individual amounts.*

---

## 2. Landscape (S1.1) — what already exists

### 2.1 Public LLM cost benchmarks (the "what's out there" baseline)

- **OpenRouter** publishes aggregate model pricing and usage stats publicly
  (`/api/v1/models` feed, refreshed ~6h). It is a *centralized* aggregator: it
  sees every request it routes. It publishes **prices**, not per-operator
  spending patterns. Not a privacy-preserving telemetry system — it is the
  data source the house already consumes.
- **models.dev** (and derivatives like the Gradually LLM Price Index) aggregate
  public price catalogs (OpenRouter, Pydantic, Portkey, LiteLLM, Helicone,
  etc.) into open price tables. Again: **prices**, not spending behavior.
- **Gap:** nobody publishes *privacy-preserving* **spending statistics** from
  real AI-agent operators. The collective layer is genuinely novel territory —
  which is exactly what makes it citable and differentiating (OBJ-31).

### 2.2 Privacy-preserving telemetry research (the toolbox)

- **k-anonymity** (Sweeney, 2002): a record is indistinguishable from at least
  k−1 others on the quasi-identifier. Applied to telemetry: a histogram bucket
  is only published if it aggregates ≥ k contributors. Weakness: k-anonymity
  is a *syntactic* guarantee — it says nothing about what an attacker can infer
  from *correlations across buckets* or from *auxiliary knowledge*.
- **Differential privacy (DP)** (Dwork et al.): a formal, *semantic* guarantee
  — adding/removing one record changes the output distribution by at most
  e^ε. Two flavors: *central DP* (noise added by a trusted aggregator) and
  *local DP* (noise added on the client before sending — stronger, no trusted
  aggregator needed, but noisier). For telemetry, **local DP** is the right
  frame: the contributor randomizes before anything leaves the house.
- **Secure aggregation** (Bonawitz et al., CCS 2017, ePrint 2017/281): a
  multi-party computation protocol where a server computes the *sum* of
  user-held vectors **without learning any individual contribution**. Uses
  pairwise Diffie-Hellman masks; secure in honest-but-curious and malicious
  settings, robust to user dropout. This is the theoretical backbone of
  architecture (b).
- **STAR** (IETF PPM): a k-anonymity threshold aggregation system — the
  server only learns a value if ≥ k clients submit it (k-heavy-hitters). A
  concrete, standardized k-anonymity aggregation primitive.
- **Nebula** (Brave/Inria): differentially-private histogram estimation on
  distributed clients — clients locally encode so an untrusted server only
  learns values whose multiplicity exceeds a threshold, with (ε,δ)-DP. This is
  the closest published system to what architecture (a)/(c) want to do, and it
  shows the state of the art is *client-side thresholding + DP*.
- **ESA (Encode, Shuffle, Analyze)** (Prochlo): the framework used in real
  telemetry (browsers, OSes) — encode on client, shuffle through a mixnet,
  analyze. The shuffle step is what turns local DP into central-DP-quality
  utility.

**Takeaway for the design:** the mature, production-proven toolbox is
**client-side k-anonymity thresholding + local DP**, optionally with a shuffle
step. Secure aggregation (b) is research-grade but proven; on-chain
confidentiality (d) is the only one that gives *confidentiality by design*.

---

## 3. Risk arithmetic (S1.2) — what an attacker can reconstruct

This is the highest-weight sub-phase: in privacy, the risk lives here, not in
the design. The question is not "is the data anonymized?" but **"what can an
attacker reconstruct from the published aggregate?"**

### 3.1 Fingerprinting by spending pattern

Even with no identifier, a *sequence* of spending events is a fingerprint.
Consider a contributor who, over a week, spends:
- $0.42 on model X (task class "code"),
- $1.10 on model Y (task class "research"),
- $0.05 on model Z (task class "chat").

If the published data is *per-event* (even with coarse timestamps), an
attacker who observes the house's public behavior (e.g. a blog post, a
published doc, a repo activity pattern) can match the *shape* of the spending
curve to a specific contributor. **This is the classic de-anonymization by
correlation** — the same attack that breaks Netflix's anonymized ratings.

**Mitigation:** never publish per-event data. Publish **aggregates over a
window** (day/week) with **coarse buckets** (cost ranges, not exact amounts),
and **suppress any bucket with < k contributors**. The fingerprint lives in
the *joint distribution* of (model × class × cost × time); the more
dimensions you publish, the thinner the buckets and the easier the
fingerprint.

### 3.2 Temporal correlation

Exact timestamps are a re-identification vector: a contributor who is the
*only* one spending on model X at 03:00 on a Tuesday is trivially identified.
Even coarse timestamps correlate: if the house only runs heavy jobs at night
(a cron pattern), the *time-of-day histogram* is a fingerprint.

**Mitigation:** generalize timestamps to day/week granularity, and **drop
time-of-day entirely** (or bucket it so coarsely it carries no signal). The
house's own cron schedule is a strong fingerprint — do not publish it.

### 3.3 Minimum population per bucket (k)

The single most important number. If a bucket (model × class × cost-range)
has fewer than k contributors, publishing it lets an attacker isolate an
individual. Standard practice: **k ≥ 10** (the task's own floor). But k is
not enough on its own:

- **k-anonymity is per-bucket, not global.** A contributor can be in 100
  buckets each with k≥10, yet be the *only* one in the *intersection* of a
  specific (model, class, cost, week) combination. The attacker intersects
  buckets.
- **The k-anonymity paradox:** the more useful the data (finer buckets), the
  thinner the population, the weaker the guarantee. There is a direct
  trade-off between utility and k.

**Mitigation:** enforce k on the *joint* quasi-identifier, not per-column;
suppress low-population cells; and prefer **fewer, coarser buckets** over
many fine ones. When in doubt, coarsen.

### 3.4 What a histogram actually leaks

A histogram of *costs* leaks more than a histogram of *counts*:
- **Cost distribution** reveals the *budget* of contributors — which is
  itself sensitive (the house's budget is a strategic fact).
- **Model mix** reveals *which providers/models* a contributor uses — a
  fingerprint and a competitive signal.
- **Task-class mix** reveals *what kind of work* the contributor does — the
  closest proxy to "objectives" that survives anonymization. A contributor
  who is 90% "code" and 10% "research" is telling the world what it builds.

**The honest conclusion:** you cannot share *spending statistics* and
simultaneously hide *everything*. The design must choose **what dimension to
sacrifice**. The safe choice: share **cost and model and coarse class**, but
**never the joint (model × class × cost × time) at fine granularity**, and
**never per-event**.

### 3.5 The attacker model, stated

- **Passive observer:** reads the public dataset. Can do fingerprinting,
  temporal correlation, bucket intersection. Defeated by: coarse buckets, k≥10
  on the joint identifier, no per-event data, no time-of-day.
- **Active aggregator (if any):** in (b), the aggregator is honest-but-curious
  — it must not learn individual contributions. Defeated by secure aggregation
  (MPC). In (a)/(c) there is no aggregator.
- **Auxiliary-knowledge attacker:** knows the house's public footprint (docs,
  repo, blog). This is the *hardest* to defeat — it is why per-event data and
  fine joint buckets are forbidden, and why **local DP** (noise before
  leaving the house) is the only formal defense against it.

---

## 4. The four architectures (S1.3)

### (a) k-anonymous histograms, no backend

**How it works:** the plugin aggregates its own spending into histograms over
a window (day/week), buckets by (model, class, cost-range), suppresses any
bucket with < k contributors, and publishes the result — optionally via
Tor/relay to hide the source IP. No central server; the histogram is posted
to a public channel (e.g. a repo, a public endpoint).

**Real guarantees:** k-anonymity per bucket (weak, syntactic). No identifier
persists. **No formal DP** unless noise is added. The source IP is a
fingerprint unless Tor/relay is used.

**Risk arithmetic:** vulnerable to fingerprinting across buckets and to
auxiliary-knowledge attacks (the house's public footprint). k≥10 on the joint
identifier mitigates but does not eliminate.

**Implementation cost:** low — a histogram generator + a publish step.
**Contributor friction:** low — one opt-in, one publish.
**Maturity:** high (k-anonymity is well understood).
**Maintenance:** low (no backend to run).

**Verdict:** the simplest, but the weakest guarantee. Fine as a *first
iteration*; not the destination.

### (b) Federated secure aggregation

**How it works:** each house sends *statistical gradients* (not raw data) to a
neutral aggregator server. Secure aggregation (Bonawitz et al.) uses pairwise
Diffie-Hellman masks so the server computes the **sum** without learning any
individual contribution. The server publishes only the aggregate.

**Real guarantees:** strong — MPC-based, secure in honest-but-curious and
malicious settings, robust to dropout. The server learns only the sum, not
the parts. **This is the strongest *computational* guarantee of the four.**

**Risk arithmetic:** the aggregate is still a histogram — the same
fingerprinting/correlation risks apply to the *published aggregate*. Secure
aggregation protects the *channel* (server sees nothing individual), not the
*output* (the aggregate can still leak via joint buckets). So (b) still needs
k-anonymity + DP on the output.

**Implementation cost:** high — a community aggregator server, key
management, the MPC protocol. **Contributor friction:** medium — must
coordinate with the aggregator and other contributors (rounds, masks).
**Maturity:** high in research (CCS 2017, widely cited), low in production
deployment. **Maintenance:** high (a server to run, protocol to keep alive).

**Verdict:** the most robust *channel* guarantee, but the heaviest to run and
it still needs output-side k-anonymity/DP. Overkill for a first version; a
good upgrade path if the collective grows and a trusted aggregator emerges.

### (c) Public k-anonymous dataset (parquet, k≥10, optional DP) — **recommended short path**

**How it works:** each house generates an anonymized dataset from its local
trace (parquet/CSV), applies k-anonymity (k≥10 on the joint quasi-identifier),
optionally adds ε-DP noise, and publishes it. Anyone can sum the datasets.
This is the "portable, publicable" option — it fits the house's identity as a
publicable, privacy-respecting project.

**Real guarantees:** k-anonymity (syntactic) + optional ε-DP (semantic, if
noise is added). No backend, no aggregator, no single point of trust. The
contributor controls exactly what leaves.

**Risk arithmetic:** same fingerprinting/correlation risks as (a), but the
parquet format makes it *easy to sum* and *easy to audit* — a verifiable
artifact. Local DP (noise before publish) is the formal defense against
auxiliary-knowledge attacks. The dataset is a *snapshot*, not a live feed, so
temporal correlation is bounded.

**Implementation cost:** low-medium — an anonymizer (k-anon + optional DP)
over the local trace + a publish step. **Contributor friction:** low — one
opt-in, one publish. **Maturity:** high (k-anonymity, DP, parquet are all
mature). **Maintenance:** low (no backend).

**Verdict:** the **short path**. It is the most portable, the most
publicable, the easiest to verify, and it needs no backend. It is the natural
first version of the collective layer.

### (d) Tari Ootle confidential resources on-chain — **the destination**

**How it works:** Tari Ootle is the Tari Layer-2 smart-contract layer (Rust,
BSD-3-Clause). Spending data is modeled as **confidential resources**
on-chain: a `ResourceType::Confidential` with `MintArg::Confidential`, backed
by **Pedersen commitments** (the amount is hidden), **Bulletproof range
proofs** (proving the value is in range without revealing it), and
**verifiable ElGamal encryption** (`ViewableBalanceProof`) so a designated
view key can decrypt balances without the commitment being opened. A smart
contract (WASM template, `#![no_std]`, `tari_template_lib`) aggregates
contributions collectively — no central server, no single point of trust.

**Real guarantees:** **confidentiality by design at the base layer.** The
amount is a commitment, not a plaintext. This is qualitatively stronger than
k-anonymity: the *value itself* is hidden, not just the identity. The
aggregation is collective (a contract), so there is no aggregator to trust.

**Risk arithmetic:** the *existence* of a contribution and its *timing* are
still on-chain (a transaction is visible even if its amount is confidential).
So the same temporal-correlation risk applies to *when* a house contributes.
The *amount* is protected by the commitment; the *fact of contributing* is
not. This is the honest caveat: on-chain confidentiality hides *values*, not
*activity*.

**Implementation cost:** high — a WASM smart contract, a wallet, validator
nodes, fees. **Contributor friction:** high — must run/use a wallet, pay
fees, interact with the network. **Maturity:** **low** — testnet Igor, which
"sees frequent resets to support the rapid pace of development" (per Tari
docs); pre-mainnet. **Maintenance:** high — the network is evolving fast.

**Verdict:** the **destination** — the only architecture with
*confidentiality by design*. But it is pre-mainnet, fee-bearing, and
complex. **Do not build on it today; re-evaluate when Ootle matures.**

---

## 5. Comparative table of guarantees

| Dimension | (a) k-anon histograms | (b) federated secure-agg | (c) public k-anon dataset | (d) Tari Ootle on-chain |
|---|---|---|---|---|
| **Value confidentiality** | none (plaintext buckets) | none (plaintext aggregate) | none (plaintext buckets) | **yes (Pedersen commitments)** |
| **Identity confidentiality** | k-anonymity (weak) | strong (MPC channel) | k-anonymity (weak) | strong (no identity on-chain) |
| **Formal DP** | optional (local) | optional (on output) | **optional (local, recommended)** | n/a (commitments) |
| **Backend** | none | community server | none | decentralized (validator nodes) |
| **Aggregator trust** | none | honest-but-curious | none | none (collective contract) |
| **Contributor friction** | low | medium | **low** | high (wallet, fees) |
| **Implementation cost** | low | high | **low-medium** | high |
| **Maturity** | high | high (research) / low (prod) | **high** | **low (testnet Igor)** |
| **Maintenance** | low | high | **low** | high |
| **Fees** | none | none | none | **yes** |
| **Fits "publicable house"** | yes | no (needs server) | **yes** | yes (novel, citable) |

---

## 6. The recommended path (worker's analysis — user decides)

**Ship (c) now, keep (d) on the roadmap.**

- **(c) is the short path:** no backend, portable, publicable, verifiable,
  mature. It delivers the collective value (real spending statistics anyone
  can sum) with the house's privacy posture intact. Add **local DP** (noise
  before publish) to get a *formal* guarantee, not just syntactic
  k-anonymity.
- **(d) is the destination:** only it gives *confidentiality by design*.
  Re-evaluate when Tari Ootle leaves testnet Igor and the tooling (wallet,
  contract templates, fees) stabilizes. The house already has Tari repos
  locally, so the evaluation cost is low when the time comes.
- **(b) is the upgrade path** if the collective grows and a trusted aggregator
  emerges — but it is not needed for a first version.

**The honest caveat that applies to all four:** you cannot share spending
statistics and hide *everything*. The design chooses what to sacrifice:
share cost + model + coarse class, never the fine joint distribution, never
per-event data, never time-of-day. That is the price of the collective
benchmark — and it is a price the user must consciously accept.

---

## 7. The payload schema (for S2, the anonymizer prototype)

The S2 prototype generates the anonymized payload from the local
`trace.jsonl`. The filter removes the objective and generalizes timestamps.

```json
{
  "schema_version": 1,
  "window": "2026-09-10",            // day granularity, no time-of-day
  "contributor": null,                // NO persistent identifier
  "buckets": [
    {
      "model": "glm-5.3-flash",
      "provider": "nanogpt",
      "task_class": "code",           // coarse class, NOT objective
      "cost_range": "0.10-0.50",      // coarse bucket, not exact amount
      "count": 12,                    // only if count >= k (k=10)
      "total_cost": 3.42,            // aggregate, not per-event
      "total_tokens": 120000
    }
  ],
  "dp": { "epsilon": 1.0, "mechanism": "laplace" }   // optional local DP
}
```

**Never present:** `objective`, `title`, `consumer_id`, `requestId`, exact
timestamps, time-of-day, per-event rows, fine cost amounts, fine joint
(model × class × cost × time) combinations.

---

## 8. Decisions for the user (numbered)

1. **Architecture choice.** The worker's analysis recommends **(c) public
   k-anonymous dataset (parquet, k≥10, optional local DP) as the short path**,
   with **(d) Tari Ootle as the destination** to re-evaluate when it matures.
   The user decides whether to adopt (c) now, wait for (d), or pursue (b).

2. **k floor.** The task specifies k≥10. The worker recommends **k=10 as the
   floor on the *joint* quasi-identifier** (not per-column), with suppression
   of low-population cells. The user may raise it (k=20) for stronger
   guarantees at the cost of coarser data.

3. **Local DP.** The worker recommends **adding local DP (ε≈1.0, Laplace
   noise before publish)** to get a *formal* guarantee, not just syntactic
   k-anonymity. The user decides whether the noise cost (reduced utility) is
   acceptable.

4. **What to sacrifice.** The design shares cost + model + coarse class, and
   **never** the fine joint distribution, per-event data, or time-of-day. The
   user must consciously accept that *some* spending signal is shared — that
   is the price of the collective benchmark.

5. **S2 budget.** The S2 prototype (the anonymizer over `trace.jsonl`) is
   **NOT included** in this 0.60 USD. Preliminary estimate **0.40–0.60 USD**.
   The user decides whether to fund S2.

6. **Opt-in posture.** The collective layer is **opt-in, default OFF, with a
   dedicated kill-switch (SHARE-STOP)**. The user confirms this posture before
   any S2 build.

---

## 9. Method and sources

- **Tari Ootle facts** verified from the local source checkout
  (`tari-dan`, `tari-smart-contracts`): confidential resources
  (`ResourceType::Confidential`, `MintArg::Confidential`,
  `ConfidentialOutputStatement`), Pedersen commitments, Bulletproof range
  proofs, verifiable ElGamal (`ViewableBalanceProof`), WASM templates
  (`#![no_std]`, `tari_template_lib`), testnet Igor (config presets
  `[igor.p2p.seeds]`, `[igor.ootle_wallet_daemon]`; Tari docs confirm Igor is
  the DAN-layer dev network with frequent resets), BSD-3-Clause license.
- **Secure aggregation:** Bonawitz et al., *Practical Secure Aggregation for
  Privacy-Preserving Machine Learning*, CCS 2017 (ePrint 2017/281).
- **k-anonymity:** Sweeney (2002). **Differential privacy:** Dwork et al.
- **STAR** (IETF PPM, k-anonymity threshold aggregation); **Nebula**
  (Brave/Inria, DP histogram estimation); **ESA/Prochlo** (Encode-Shuffle-
  Analyze).
- **Public cost benchmarks:** OpenRouter `/api/v1/models` feed; models.dev and
  derivatives (Gradually LLM Price Index).

*No installs, no containers, no outbound network beyond documentation were
used in this phase. The localnet evaluation of Tari Ootle is S2 territory and
requires explicit user approval.*
