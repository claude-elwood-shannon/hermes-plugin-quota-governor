# tick_body_parts.py — the body is the program, the tick is the interpreter

**OBJ-39-REBELION (t_7a28c219)** · companion to `tick-cola-viva.py`
(OBJ-30b), links to the constitution of flight `t_cfe8060b`.

Two mechanisms that make the constitution enforceable **without the owner
watching**:

1. **Done-verification against body.** When a task whose body declares
   numbered parts (`R1/R2/...`, `Fase N`, `Paso N`, `Step N`, `Round N`)
   closes, the tick compares the declared parts against the evidence the
   worker left behind: `tasks.result` + `task_runs.summary` + task
   comments (+ restated completions in the body itself, the successor
   convention "R1 (ya hecha...)"). A mention counts only with affirmative
   context — negation lines ("R2-R6 pendientes", "R5 skipped") never
   evidence a part unless a completion word follows nearby.

2. **Successors from bodies.** Pending parts become ONE successor task:
   assignee inherited, header tags inherited, pending part lines copied
   verbatim. The house rule "done is sealed — never reopen" is respected:
   **the successor IS the reopen.** An audit comment (`[done-verify]`) is
   left on the parent so the trail survives.

## Why it exists

On the night of 2026-09-10/11 a multi-part night-marathon task closed
after its first round (run summary: "First round of classification
completed") while its body declared R1-R6. The remaining rounds slept in
the sealed body for hours while quota sat free — and the tick logged
"cola seca legitima" 28 times. Doctrine without a check allowed a
premature done to hide legitimate work. This module is the enforcement
point: **"cola seca legitima" is only true when no closed body holds
unevidenced parts.**

## Cascade position (inside tick-cola-viva.py)

```
1. assign a ready task without assignee          (existing)
2. queue-alive check                             (existing)
3. structural class-C successor                  (existing)
3.5 BODY-PARTS: successors from closed bodies    (NEW — before the drought verdict)
4. cola seca legitima                            (now honest)
```

Loaded lazily from `PLUGIN_DIR/scripts/tick_body_parts.py`; fail-open —
if the module is missing the cascade keeps its pre-OBJ-39 behavior and
the tick never breaks.

## Verification rules (v1)

- **Declared parts**: line-start markers `R<N>`, `R<N>+`, `Fase N`,
  `Paso N`, `Step N`, `Round N` (ES/EN, markdown heading/bullet and short
  parenthetical tolerated). Bare `1. 2. 3.` lists are NOT parts (step
  prose everywhere — matching them would false-positive on every body).
- **Evidence channels**: task result + run summaries + comments
  (trusted: a plain "R4: done" line evidences). The parent BODY only
  self-evidences with a completion word ("R1 (ya hecha...)") — program
  lines never count.
- **Negation cut**: `pendiente/pending/remaining/restante/quedan/
  skipped/...` on the line suppresses its mentions unless a completion
  word (`done/completada/hecha/ejecutada/...`) follows within 40 chars.
- **Range mentions**: `R2-R6` on a clean line evidences the whole range
  (the exact pattern the R2-R6 run summary used); on a negated line it
  evidences nothing.
- **Escape hatch**: a comment `[done-verify-skip] R5: <why>` waives a
  part (documented rationale beats blind re-creation, so anti-filler
  skips terminate the chain). Without the marker a skipped part stays
  pending — that is the point.
- **Guards**: only class-C parents get auto-successors (class A/B get a
  one-time audit comment — user decisions); >20 declared parts = parse
  false-positive, skipped; open successor blocks creation (idempotency);
  one action per tick.

## Live evidence (2026-09-11, the same day it shipped)

The R2-R6 successor (`t_6066c890`, spawned by the earlier structural
mechanism) closed while this module was being built. Its run summary —
"Executed rounds R2-R6 of the vLLM marathon" — was read by the new
verifier at the next scan: range mention + completion word → pending
empty → no false successor. First production pass: negative result
correctly produced.

## Usage

```bash
# findings table against the real board
python3 scripts/tick_body_parts.py --scan

# dry-run: what the cascade would do right now
python3 scripts/tick_body_parts.py

# execute (normally tick-driven via tick-cola-viva.py)
python3 scripts/tick_body_parts.py --execute
```

Exit code 0 always; zero tokens (no LLM); DB access is read-only, all
mutations go through the `hermes kanban` CLI (create/comment); one
ledger line per decision in the shared `quota-governor/cola-viva.jsonl`.

Tests: `test_tick_body_parts.py` (33 cases, fixtures only — including
the exact marathon incident case as a regression test).
