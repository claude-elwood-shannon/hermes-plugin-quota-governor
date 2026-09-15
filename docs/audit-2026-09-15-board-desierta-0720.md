# Audit 2026-09-15 (07:20 alert): desert blip inside a live creator chain

Scope: logs of today (`quota-governor-tick.log`, `kanban-watchdog.log`,
`cron-health-check.log`, `cron-alarms.jsonl`, `efficiency-ratio.log`,
`objective-proposer.log`, journalctl of crond, sysstat `sa15`) plus
`kanban.db` tasks/events/runs. Window: 05:10→08:25 CEST (the 00:06–05:10
stretch is covered by `docs/audit-2026-09-15-board-desierta.md`, commit
d55b1dc — referenced, not restated). Trigger: this audit is the deliverable
of the 07:20:02 watchdog auto-task, the third "board desert (quota OK)"
alert of the day (03:05, 05:10, 07:20).

## Verdict on the 07:20 alert

TRUE BLIP, FALSE DESERT at the process level. The autonomous creator chain
(OBJ-CODEQUALITY) was alive across the entire check interval; the watchdog
sampled the one gap between cards:

- 07:16 t_658ecea8 created (worker 0b9a2b…, pin gpt-oss:20b)
- 07:18 t_658ecea8 completed; t_689638ce (OBJ-AUTODEV fix) completed
- 07:20:01 watchdog tick: running=0 AND ready=0 → alert, creates this task
- 07:31 t_7194a479 created by the same worker (next link in the chain)

Gap between last completion (07:18) and next creation (07:31): ~13 minutes,
inside the watchdog's 5-minute sample. Verified via task_events (created /
claimed / spawned / completed timeline) and task authorship
(`created_by=worker` for all four cards). Not a dispatcher war: no reaps in
the window, gateway dispatcher sole claimer all morning (tick log lines
"gateway dispatcher active — NOT respawning", 56 tick lines 06:00–08:11
without gaps).

Structural note: with the creator's cadence (~14–16 min per card) and a
5-min watchdog sampling a running+ready==0 condition, a blip alert fires
roughly every time a card completes and its successor is not yet created.
All three alerts today landed in such gaps; the 2h anti-burst guard
(stamp in /tmp) held (05:10→07:20 = 2h10m). Expected behavior, low noise.

## Classified anomalies

1. **Session window 01:15–03:08 (complements d55b1dc §timeline).** Session
   pct climbed 93.4% (01:06) → 97.0% (01:16) then froze at exactly 97.8%
   with counters frozen (reqs 264/572) for ~1.5h while 1–5 live workers
   kept being reported; at 03:08:25 the window reset (0.4%, reqs 20/592 —
   exactly 20 requests consumed across the reset). Classification:
   provider-side throttle of an exhausted free window — requests rejected
   without consuming quota; local decision logic behaved as designed
   ("run max=1, session near limit"). Weekly pressure none (21.2→24.3%).
   No OOM, no hung tasks, loadavg ≤ 2.15 on 30 CPUs (sar sa15) — the
   plateau was not host-induced.

2. **crond delivery gap 01:25→02:15 (complements d55b1dc root cause).**
   Journal confirms the three */5 jobs (kanban-watchdog, obs-serve,
   open-webui-bridge) fired at 01:25:01 and produced no CMD line again
   until 02:15:01, matching the crontab REPLACE deploy at ~01:25:50 and
   the health-check re-add/reload cycle (restarts logged 01:57 and 02:11;
   first effective fire 02:15). New evidence in favor of the two-level
   self-watch design: the gateway-internal scheduler stayed alive through
   the whole gap — quota-governor-tick.log has zero gaps (10-min cadence)
   and the governor detected the stale health-check log at 01:57 and ran a
   manual pass itself. Defense-in-depth worked as built; the gap cost was
   limited to watchdog/alert coverage, not flight control.

3. **activity cost pinned at $5.0000 ≥ 48h.** Every tick line yesterday
   and today reads `cost=$5.0000` (287/287 lines sampled over 2 days) while
   `request_balance_usd=0.0` / `request_covered_usd=0.0` in observations.
   Classification: monitoring artifact, not spend — the session/weekly
   percentages move independently and the spend-limit STOP never fired
   (write_stop=0 all day; no STOP file). The plancha value saturates the
   pay-as-you-go delta detector (cost>prev_cost can never be true), which
   currently masks real dreno detection. Low urgency, but worth a fix:
   treat exactly-$5.0000 as "cap reached, unknown burn" or source cost
   from the per-request ledger.

4. **Pin-gate reality in the 06:00–08:25 window (corroborates d55b1dc +
   the post-907c5ec4 ficha corrections).** The autonomous creator keeps
   birthing cards with `--model gpt-oss:20b`; outcomes today in this
   window: t_7194a479 completed WITH the pin still set (08:00:26, run
   alive 07:44→08:00), t_658ecea8 completed with pin (07:18), while
   t_6525c7b9 blocked 07:50 and was R2-remediated to deepseek-v4-flash at
   07:55:02, completing 08:00. The "sometimes survives the pin, sometimes
   R2" pattern continues exactly as documented; no new failure mode.

## State at seal (08:25)

- Board: 1 running (this audit), 0 ready, 12 triage (4 are the
  crontab-hardening children from 03:33, still unclaimed), 1 CODEQUALITY
  card mid-flight blocked→remediated, chain expected to continue.
- Quota: session 0.2%, weekly 24.3% — full free quota, max=3, healthy.
- Watchdog: ticks every 5 min, no gaps since 02:15; cron-health-check 7/7 OK.
- efficiency-ratio: 24h verdict flipped CRITICO(0.49)→BAJO(0.5) at 05:00 —
  threshold-crossing artifact, emitter healthy (item 39 closed upstream).

## Sources

- quota-governor-tick.log, kanban-watchdog.log, cron-health-check.log,
  cron-alarms.jsonl, efficiency-ratio.log, objective-proposer.log
  (2026-09-15 bands).
- journalctl -u cron (CMD/RELOAD lines 01:20–02:20), sysstat sa15
  (runq/ldavg 01:00–03:20), kernel log (no OOM hits).
- kanban.db: tasks/task_events/task_runs for t_658ecea8, t_689638ce,
  t_7194a479, t_6525c7b9, t_907c5ec4, t_a0947685 (this task) and
  created_by census for the 12h window.
