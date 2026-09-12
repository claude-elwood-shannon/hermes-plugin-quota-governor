# docs/ gap audit — objectives 28, 29, 30, 37, 40

**Date:** 2026-09-12 · **Task:** t_b1822be3 (OBJ-31) · **Method:** full
`docs/` inventory on `main` (22 files) diffed against
`feat/obj28-budget-2026-09`, cross-checked against git history (the commits
carrying each objective) and direct README greps.

## Summary

| Objective | Documented on main | Only on the branch | Gap on main |
|---|---|---|---|
| OBJ-28 budget by objective | design (`obj28-budget-by-objective-design.md`) | `obj28-budget-status.md` (living, 65 l) · `obj28-phase0-review.md` (42 l) | living status + phase-0 review (incl. the 7 §4 owner decisions gating phases 1-3) invisible to main readers; the design doc's branch edits (9 l) also ahead |
| OBJ-29 perpetual supply | **nothing** | `obj29-perpetual-supply.md` (107 l) | the whole objective is undocumented on main — layers, shared-root incident, acceptance window; README never mentions supply_ratio or cola viva |
| OBJ-30 innovation fund | **nothing** | `obj30-innovation-fund.md` (88 l) | whole objective undocumented on main; contract v2 exists only as a kanban attachment (t_57c5f529), not in the repo at all |
| OBJ-37 collective spending | deep design (`privacy-preserving-telemetry.md`) | `obj37-collective-spending.md` (living, 70 l) | S1 verdict table (4 architectures) + the 6 pending owner decisions exist only on the branch |
| OBJ-40 local vLLM worker | `vllm-worker-duel.md` · `vllm-night-marathon.md` | stale pre-consolidation copy of the duel doc | gate integration (`ALLOWED_PROFILES`, `4fb7b7d`/`45f4350`) is recorded only in commit messages; main's duel doc still says "integration to gate pending user OK" |

## Cross-cutting findings

1. **372 lines of objective docs exist only on `feat/obj28-budget-2026-09`**
   (obj28 status + review, obj29, obj30, obj37) plus `docs/obs-portal.md`
   (96 l, OBJ-27 F5). A main-only reader sees none of it. Docs commits on
   the branch: `dd2e79c`, `4b54045`, `43ccea4`, `04a9da4`.
2. **README blind spots** (grep-verified, zero hits): no mention of
   `pr-vllm` / the local worker (OBJ-40), of `supply_ratio` / perpetual
   supply (OBJ-29), or of the innovation fund (OBJ-30). The "docs/ carries…"
   paragraph lists only calibration notes, model matrix and design records.
3. **No objective index.** Objective knowledge is scattered across design
   docs, living docs, the capability map and the README; there is no
   `docs/objectives.md` mapping each objective to its status and docs.
4. **Contract v2 not in the repo.** `OBJ-30-contrato-fondo-v2.md` is
   referenced by the OBJ-30 living doc but lives only as a board task
   attachment — unauditable from the repository.
5. Minor: `docs/vllm-worker-duel.md` diverges on the branch, which carries
   the pre-consolidation copy; main's consolidated R1-R5-bis edition
   (`108d467`) is the newer one.

## What would close it

- Merge (or cherry-pick the four docs commits from)
  `feat/obj28-budget-2026-09` → main carries all five objectives' docs.
- README: one paragraph + table rows for the local worker and the supply
  mechanisms.
- `docs/objectives.md`: a single index (objective → status → doc).
- Move/attach contract v2 into the repo (owner decision — it is a signable
  document).

## Method notes

- Branch-only list from `git diff --stat main feat/obj28-budget-2026-09 -- docs/`.
- README blind spots from direct grep (`vllm|pr-vllm|ALLOWED_PROFILES|local worker|innovation|supply` → 0 hits).
- Objective↔doc mapping from commit subjects: `679866b`, `4b54045`,
  `2e41b68`, `43ccea4`, `ebfe3cf`, `04a9da4`, `4fb7b7d`, `45f4350`,
  `e9d5dfa`, `6bf2e6a`, `4452056`, `898a0bc`, `ccf09e6`.