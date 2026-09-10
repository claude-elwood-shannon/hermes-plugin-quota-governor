# OBJ-34 — Capability and frontier map of the house

> **What it is:** a prioritized map of what the house can do today, where the
> exact frontier of each area lies, and which growth piece to buy first with a
> declared budget. **Status: map, not builds** — nothing installed, nothing
> requested, nothing activated. The final decision is Andrew's.
>
> **Date:** 2026-09-10 · **Source of every number:** live probes from today
> (method in §7). **Map model:** everything that enters, enters with a
> contract: declared cost, verifiable criterion, revocable — the same format
> as the rest of the fund.

---

## Executive summary (30 seconds)

| # | Candidate piece | Area | Cost | Risk | Class | Verdict |
|---|---|---|---|---|---|---|
| 1 | **Vision for visual QA of the dashboard (OBJ-32)** | Physical | **$0** | Low | C | ✅ **RECOMMENDED** |
| 2 | E2E visual QA with screenshots (browser+vision) | Physical | $0 | Low | C | Same piece, pipeline view |
| 3 | Vision over HTTP (direct payload to a vision provider) | Physical | ~$0.01/req | Low | A | Alternative if the native tool falls short |
| 4 | Close-out of OBJ-27 F4b | Observ. | ~$0.10 | Low | C | Already in motion (sister task) |
| 5 | Per-domain memory branch | Memory | $0 | Low | C | Waits for OBJ-31's format |
| 6 | Gap hygiene (tagging in the trace) | Observ. | ~$0.05 | Low | C | Cheap, high value |
| 7 | Own MCP server for the trace | New | $0 | Low | C | Good second |
| 8 | Local OpenAI-compat proxy | New | $0 | Medium | A | Waits for a client |
| 9 | ACP to IDEs | New | $0 | Medium | A | Waits for a client |
| 10 | Multi-platform bot via gateway | New | $0 | Medium | A | Waits for a choice |
| 11 | Computer-use (real Xorg :10) | Physical | $0 | **Medium-high** | A | **Not recommended yet** |
| 12 | Voice-in (STT) / TTS in gateway | Physical | $0 | Medium | A | After vision |
| 14 | Memory health on the dashboard | Memory | ~$0.02 | Low | C | After OBJ-31 |

**Recommendation:** piece 1 — activate vision (native tool, $0) and its first
use case: visual QA of the OBJ-32 dashboard, with the contract closed in §6.

---

## 0. How to read this map

- **Class C** = executable today with the profile's native tools, no installs
  or new credentials → no per-piece approval needed (OBJ-30 framework
  applies).
- **Class A-adjacent** = requires an install, a new credential, or touching
  config → **per-piece approval** (GR5/GR7/GR9 already demand it formally).
- **Cost** = USD of NanoGPT balance estimated per usage cycle; $0 pieces
  consume window quota (tokens %), not balance. Quota is the real scarce
  resource (100.05% of the weekly window used, reset Mon 2026-09-14 00:00Z).
- **Revocable** = how it is switched off in one line, leaving no orphan state.
- Numbers of the fund today, measured today: NanoGPT balance **$27.50**,
  OBJ-30b window: spent **$3.44 of $5.00** (50% warn crossed → state `warn`,
  the house works but with no window slack), forecast gate `OK` (53.1% of
  quota, ETA_90 in ~70h), 23 active crons (of 24), board: 4 running / 1 ready
  / 11 triage / 1 blocked / 10 done / 364 archived.

---

## 1. Long reasoning (what already works)

**Current state — verified today:**
- **Delegation to subagents** (delegate_task): real context isolation,
  in use (e.g. OBJ-33 running as a sister task).
- **Kanban as working memory**: 385 tasks on the board, 364 archived; OBJs
  work as objective containers with structured handoff (summary/metadata),
  parent→child dependencies, and memory across runs.
- **Skills**: 16 in the profile, 14 MB — procedural memory that loads only
  when it applies.
- **Base tool**: 52 subcommands (`hermes --help`), including `mcp`, `acp`,
  `proxy`, `serve` — paths already exposed, never used.

**Concrete frontier:** the single-turn context. Everything that fits in one
turn + skills + board works; what is needed *between* turns and does not live
in skills/board/memory evaporates. The profile's persistent memory is today
**2.0 KB MEMORY + 1.3 KB USER** (~3.3 KB total) — the smallest of the whole
house; OBJ-31 is the mother task of this frontier.

**Candidate pieces:**
- **(5) Per-domain memory branch** (C): one `references/` skill per domain
  (quota, obs, privacy), loadable on demand. Enables: cross-session context
  without bloating the turn. Cost: $0 (writing tokens). Verification: the
  skill loads and answers a domain question. Revocable: delete the dir.
  **Dependency: OBJ-31 fixes the format** — do not start it before.
- **(6) Gap hygiene** (C): the trace says **100% of the spend with cost
  ($17.69) is `unattributed`** — 1,040 of 1,419 lines without an
  `objective:` tag. The dashboard shows it in red (correct: the gap is
  shown, not hidden), but without tags, per-objective budgeting is
  impossible (OBJ-28 needs this). Cost ~$0.05. Verification: gap % < 20% on
  the dashboard (its own threshold). Revocable: remove the tag from the
  creator. **Dependency: OBJ-27 F0/F4b stabilized.**

## 2. Physical capabilities

**Current state — verified today:**
- **Browser** (browser_exec, headless Chromium via CDP): functional —
  navigates file:// and http, readable DOM, screenshots.
- **Vision** (vision_analyze): **functional**. Real E2E probe: a synthetic
  PNG (114 bytes) generated in the workspace → correct answer (red block,
  estimated area 30-35%, actual 31%). No install, no new credential.
- **TTS** (text_to_speech, edge provider): **functional**, free, no
  credential — mp3 in the profile's audio cache.
- **Computer-use** (cua-driver): tool **present** in the catalog; **real
  Xorg on :10** (xrdp) — hardware present, tool never used.
- **STT/voice-in**: no channel today (gateway has no audio-in wiring).

**Concrete frontier:** the house sees and speaks (verified probes), but
neither capability has a *real first use case* integrated into a house flow.
Computer-use exists but with no risk contract.

**Candidate pieces:**
- **(1) Vision for visual QA of the dashboard (OBJ-32)** — evaluated in
  detail in §6. Recommended.
- **(2) E2E visual QA with screenshots** (C): full pipeline
  browser→screenshot→vision_analyze, probed today in an isolated session
  (`obj34-iso`): file:// render of the dashboard (1265×1103 px) → capture →
  correct analysis (title, sections, defects). Cost $0. Enables: OBJ-32 v1
  (queryable HTML) getting periodic, cheap visual QA without touching the
  base. Verification: a seeded defect in an HTML fixture is detected in the
  analysis. Revocable: delete the skill.
- **(3) Vision over HTTP** (A-adjacent: touches config → GR7): image→vision
  provider payload, ~$0.01/req, ~$0.10-0.30/window. Only if the native tool
  falls short. Revocable: revert the config line.
- **(11) Computer-use** (A): tool present + real Xorg :10. Enables QA of
  native apps, GUI automation. Medium-high risk (it is the piece with the
  most physical action power over the host; and `xrdp` = a remote-access
  surface). Minimal contract: `--print` session and dry-run first,
  never-login, sandboxed session, revocable = do not invoke. **Not
  recommended yet**: it is the only piece of the map that deserves its own
  risk evaluation before a first use case. Post-vision.
- **(12) Voice-in/TTS in gateway** (A): STT into the gateway (the house's
  WhatsApp/Telegram) → spoken replies. Cost $0-0.05. Risk: medium
  (messaging surface). Verification: a test audio → transcription in the
  gateway log. Revocable: disable the cron/gateway wiring. After vision.

## 3. Memory (with OBJ-31)

**Current state — verified today:** memories/ = 2 flat files, 3.3 KB total,
no real locking (`.lock` files present), no rotation, no per-domain. The
dashboard measures the house (spend/board/forecast) but nothing measures
memory health.

**Concrete frontier:** without curation, memory grows until it hits its hard
char limit (the system prompt already warns "94% / 95% full" every turn).
Without per-domain, every session pays for irrelevant context.

**Candidate pieces:**
- **(14) Memory health on the dashboard** (C, after OBJ-31): once OBJ-31
  lands, a memory-health map on the OBJ-32 dashboard (bytes, % full, last
  rotation) is a natural, cheap piece: 1 section in the generator + 1 test.
  Cost ~$0.02. Verification: visible section with real data. Revocable:
  remove the section from the generator.
- **Curation and rotation themselves are OBJ-31** — not duplicated here,
  only referenced (out of scope of this task).

## 4. Unexplored paths

**Current state — verified today:** `hermes --help` lists 52 subcommands.
From the system's deferred tool catalog, these paths exist and are unused in
the house:

| Path | What it enables | Class | Cost |
|---|---|---|---|
| **(7) Own MCP server** | The house's trace/board/skills queryable from any external MCP client | C | $0 |
| **(8) OpenAI-compat proxy** | Any OpenAI-SDK client talks to the house's providers | A | $0 |
| **(9) ACP to IDEs** | Hermes as an ACP server inside an IDE (Zed, etc.) | A | $0 |
| **(10) Multi-platform bot via gateway** | The house's WhatsApp/Slack/etc. with the same house behind it | A | $0 |

**Concrete frontier:** four paths exposed, zero used. None has a concrete
client today — they are capacity without demand, and the house rule is not
to accumulate tools without a use case. They are listed so Andrew sees the
full path; the recommendation is **not to open them** until the first real
client exists.

**Opening criterion:** a concrete client exists (an IDE in use, a bot that
is wanted, an MCP client that is wanted) → open its piece with a contract;
without a client, it does not open.

## 5. Care — the contract of every new piece

Every piece that enters the fund inherits the OBJ-30 format (and GR5/GR7/GR9
already demand it formally for A-adjacent pieces):

1. **Declared cost** — estimated USD of balance per cycle + currency
   (balance vs quota %).
2. **Verifiable criterion** — one sentence a third party can check (like
   today's probes: a synthetic PNG, an HTML fixture with a seeded defect).
3. **Revocable** — one line that switches it off with no orphan state.

**Mechanism:** pieces are proposed in triage with these three fields in the
body; the creator rejects them if missing (same GR5/GR9 mechanics). The
forecast gate and burn watchdog remain the hard limits; a $0 piece still
pays quota, so *every* piece enters with a declared quota budget.

---

## 6. Detailed evaluation: vision for visual QA of the dashboard (OBJ-32)

**Candidate flagged by the task** as the framework's first test case.
**Evaluated with live probes today:**

- **Native tool functional** — `vision_analyze` (pr-ollama profile) answers
  correctly to a synthetic PNG (red block: estimated 30-35% of the area,
  actual 31%). No installs, no new credentials.
- **E2E pipeline probed** — isolated browser (session `obj34-iso`) →
  file://dashboard.html (1265×1103 px) → screenshot → correct analysis:
  title, sections (KPIs, spend by class, spend by objective, forecast,
  board), defects detected. **The probe already produced 2 real v0
  findings**: the "GASTO — POR CLASE DE CONSUMO" panel title wraps badly
  into 3 lines; and "presupuesto:" with no visible value next to the warn
  badge on the balance card. Classic visual QA — the pipeline worked on the
  first try.
- **Verifiable criterion:** a seeded defect in an HTML fixture is detected
  in the analysis.
- **Cost: $0** of balance (token quota; no paid calls).
- **Privacy: low** — captures live in local tmp
  (~/.config/browser-harness/tmp/) and go to the vision provider only for
  analysis; the dashboard is localhost-only by design (binds 127.0.0.1) and
  the HTML contains no credentials (verified in the probe: metrics only).
- **Revocable:** the QA skill is a dir — `rm -r`; the native tool is not
  uninstalled because nothing was installed.

**Verdict:** first piece. $0, low risk, concrete use case (OBJ-32
dashboard), pipeline already verified E2E with 2 real findings on day one.
The final decision is Andrew's.

### 6b. Proposed budget

**Piece 1 (dashboard vision QA):** $0 of balance, ~1-2% of weekly quota
(today's probes cost $0 and a full QA cycle ≈ 3-5 requests).
**Proposed window budget:** $0.50 of balance as a margin for the unexpected
+ quota cap: if the QA cycle exceeds 5 requests or $0.20, hard STOP and
report. Revocable: do not re-schedule the QA cycle (optional cron), no
orphan state.

---

## 7. Method (today's probes, replicable)

Every number in this doc comes from today's (2026-09-10) probes, reproducible:

- **Board/state**: read-only sqlite over `~/.hermes/kanban.db` (385 tasks:
  4 running / 1 ready / 11 triage / 1 blocked / 10 done / 364 archived).
- **Fund**: `quota-governor/*.json` (budget-state, forecast, spending-limit)
  + `nanogpt-balance-ledger.jsonl` (balance $27.50, window $3.44/$5.00).
- **Trace**: 1,419 lines OBJ-27 F0 — 100% of spend with cost is
  unattributed; cron-llm class = $17.69 (100%); priciest day 2026-09-09
  $2.67.
- **Vision**: synthetic PNG (114 B, stdlib) → correct analysis (30-35%
  estimated vs 31% actual).
- **Browser E2E**: dashboard file:// in an isolated session → screenshot →
  analysis (2 real v0 defects found).
- **TTS**: edge provider, free, mp3 in the profile's audio cache.
- **Xorg**: `pgrep Xorg` → :0 (lightdm) and :10 (xrdp) active.
- **Base tool**: `hermes --help` (52 subcommands); `mcp`, `acp`, `proxy`,
  `serve` exposed, never used.
- **Cron**: 24 jobs (23 active + 1 disabled), in `cron/jobs.json`.

*A house doc: every number in §7 can be verified with the same probes.*
