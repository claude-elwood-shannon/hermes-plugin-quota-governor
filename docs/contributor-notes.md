# Contributor notes: hermes-agent, from the inside

Notes from contributing to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
Scope: the OpenCode session-affinity PR (iteration-limit summary header), 2026-09-10.
The point of the exercise was learning the tool we run daily — these are the load-bearing
discoveries, the ones that change how we operate the house.

## What we learned about the core

1. **Affinity is a first-class invariant, not a hack.** `agent/opencode_affinity.py` derives one
   sticky `x-opencode-session` key per conversation (routing scope → ambient conversation
   contextvar → session id) and the module docstring promises it on *every* OpenCode request:
   main turn on every transport, plus auxiliary calls. That promise is enforced by exactly two
   merge points (`build_api_kwargs` and `auxiliary_client._build_call_kwargs`). Anything that
   builds request kwargs outside those seams silently loses the header — our PR closed one such
   path (the max-iterations summary). Lesson: when upstream centralizes a cross-cutting header,
   grep for *direct* `create()`/`build_kwargs()` calls rather than trusting the central helper.

2. **Facade + siblings changes how you patch.** Former god-files are facades plus
   `<stem>_<topic>.py` siblings; siblings late-import the facade so the facade binding is the
   patch seam. "Patch where production reads" — a test that patches the defining module passes
   silently while production reads a stale facade binding. This cost upstream 130+ broken tests
   once; it is documented in the root `AGENTS.md` routing table.

3. **The two invariants upstream reviews everything against:** per-conversation prompt caching
   is sacred (the ONLY sanctioned cache break is compression), and the core is a narrow waist
   (capability lives at the edges; the Footprint Ladder ranks "extend existing code" first).
   Our fix was Rung 1: two merge points, one file, zero new surface. PRs that fight either
   invariant get closed regardless of quality.

4. **Invariant tests, proven red on base.** Upstream wants 1–2 tests per fix asserting a
   behaviour contract, demonstrated failing on `main` before the fix (ours failed with
   `KeyError: 'extra_headers'` — the header absent, exactly the bug). Change-detector tests
   (asserting snapshots: model lists, config versions, counts) are rejected in review.

5. **`scripts/run_tests.sh` is CI parity, not a wrapper.** Credential vars unset, `TZ=UTC`,
   temp `HERMES_HOME`, per-file subprocess isolation (module-level dicts/ContextVars cannot leak
   between files), 600 s per-file cap with one retry in a fresh subprocess. Bare `pytest` on a
   key-bearing machine has caused real "works locally, fails in CI" incidents. Two
   compression suites legitimately exceed the 600 s file cap even on clean `main` — a file that
   shows "no tests ran (timeout before collection)" is not automatically your regression;
   diff against a clean-base run of the same files.

6. **The task body of `agent/AGENTS.md` is the map.** Per-area `AGENTS.md` files (loaded
   automatically when editing inside a subdirectory) carry the invariants that review will
   check. Reading them before writing the diff is cheaper than a review round-trip.

## Mechanics worth remembering

- **Fork-as-laboratory flow that worked:** `git worktree add` off current `origin/main` (the
  main checkout stays untouched) → RED test → fix → GREEN → neighbor files → clean-base A/B on
  suspicious files → live E2E (real relay, forced code path, temp `HERMES_HOME`) → commit
  (Conventional Commits, `fix(area):`) → push fork branch over SSH (Tor-enforced ProxyCommand).
- **E2E beats mocks when the claim is about a remote relay.** The unit tests prove the header
  is merged; the live E2E proves the summary actually lands. The relay's model list changes
  weekly — probe `GET /v1/models` and pick a live model before blaming the code path
  (`glm-5`/`glm-5.2` were disabled at test time; `glm-5.3-flash` served).
- **Fine-grained PAT scope trap:** a fine-grained personal access token can push a branch and
  read a public repo's API, but creating a PR *upstream* needs the repo reachable with
  "Pull requests: write" (classic equivalent: `public_repo`). Symptom:
  `Resource not accessible by personal access token (createPullRequest)` on both GraphQL and
  REST, while everything else works. SSH keys don't help — PRs are API/web only.
- **uv for dev venvs:** `uv venv .venv && uv sync --extra anthropic --extra dev --frozen` gives
  the exact CI-pinned dev environment; the runtime venv that ships with an install has no
  pytest by design.

## Contribution accounting

- Branch `fix/opencode-iteration-summary-session-affinity` (fork), commit `a22e1e4e10`:
  +14/-1 in `agent/chat_completion_helpers.py`, +34 in
  `tests/agent/test_opencode_session_affinity.py`.
- Verification: 8/8 affinity tests green; 44/44 across the three summary-path neighbor files;
  148 tests green across the 5 heaviest `tests/agent/` files with zero regressions vs a
  clean-base A/B; live E2E PASS on both wires (chat + anthropic) against the real relay.
