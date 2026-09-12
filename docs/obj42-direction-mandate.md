# OBJ-42 — Direction Mandate: DIRECCION-STOP + weekly flight rendition

Ratified 2026-09-12 by the user (verbatim, chat ~10:00 CEST):
"si voy a desperdiciar cuota gratis, te otorgo toda la capacidad agéntica
que quieras para DIRIGIR y que vueles con la materialización de mis ideas
y voluntades".

## Regime (replaces OBJ-30 where contradictory)

- BEFORE: wish → capture → budget → WAIT approval → execute.
- NOW: wish → capture → budget → IMMEDIATE execution on free quota (no
  waiting word); on balance, inside the bounded fund (WARN/STOP).
- User defines the VISION (what should exist) and the MEANS (top-ups,
  subscriptions, fund). Andrew defines WHAT tasks, WHAT order, WHICH
  worker, WHEN — and renders via hiperespacio + morning report.
- Class-A hard limits unchanged: balance spend OUTSIDE the bounded fund,
  irreversible actions (data deletion, upstream pushes), amending this
  constitution itself.

## Enforcement shipped (2026-09-12, task t_a3d13988)

1. DIRECCION-STOP kill switch — `scripts/quota-governor-tick.sh`:
   while `$HERMES_HOME/quota-governor/DIRECCION-STOP` exists the tick is
   INERT (no quota gate, no decision, no daemon touch, no cola viva, no
   dual-dispatcher guard). The flight returns to the pre-OBJ-42 regime.
   Cleared only by the user (`rm <file>`). Independent of the quota STOP
   (fuel guard); this is the user's hand on the switch.
   Live evidence: tick executed with the file present logged
   "DIRECCION-STOP active (mandate halted by user) — tick inert", rc=0,
   before any quota query; without it the same tick ran the full loop.

2. Weekly flight rendition — `scripts/obs/morning-screen.py`
   `build_flight_report()`: renders ONLY Mondays in the morning screen as
   the VUELO section — tasks closed in 7d grouped by objective
   (`objective:OBJ-NN` tag), balance-billed USD (trace costUsd > 0), and
   a 'sin cierres en 7d — verificar' flag when nothing closed (never
   claim a silent board as rest). Zero tokens (pure function of
   kanban.db + trace.jsonl). Consumed by the morning-report LLM cron
   Mondays 08:00 CEST. Tests: TestFlightReport (4, injected `now`),
   suite 22/22.

3. Direction-layer v2 (NOT shipped here, deliberate): dispatcher CLAIM
   gate in hermes-agent gateway (stop claiming new workers while the
   file exists). Touching the gateway would risk a live-flight restart;
   routed as a follow-up task. The tick-level v1 halts all autonomous
   REFILL layers already (cola viva, daemon spawns); the gateway
   dispatcher only claims tasks that ALREADY exist on the board.

## Direction decisions taken under the mandate (rendición)

- Fuel verified by machine 2026-09-12 22:52 CEST: pr-ollama free quota
  session 25.2% / weekly 66.1% → fly. pr-nanogpt weekly 100.0% and
  pr-opencode weekly 100.0% (rate-limited) → dry until Monday ~02:00
  CEST reset; ALL direction traffic routed to pr-ollama; ZERO balance
  touch (class A) during the dry windows.
- Blocked audit 2026-09-12: 2 human gates stay (t_03352224 PAT scope
  upstream PR; t_8b81d86c irreversible archive criteria), 3 machine
  repairs unblocked under the mandate (poisoned overrides / breaker /
  scope steer), pr-vllm lane left untouched (user-owned, only if he
  brings it).
