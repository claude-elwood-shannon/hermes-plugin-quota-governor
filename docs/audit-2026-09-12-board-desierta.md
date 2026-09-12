# Audit 2026-09-12: board desierta con cuota (watchdog OBJ-39)

Alcance: logs del governor de hoy (`quota-governor-tick.log`, 180 entradas
00:09–14:01), `kanban-watchdog.log`, `gateway.log`, `errors.log`,
`diagnose-crash.log` y eventos/runs de `kanban.db`. Complementa
`incident-2026-09-12-dual-dispatcher.md` (escrito por el run previo de esta
misma auditoría) con su verificación y las anomalías restantes del día.

## Línea de tiempo (CEST)

- 00:09–12:13 — ticks cada 10 min sin gaps (>20 min), cola seca legítima
  (regla de oro: sin filler), estado healthy todo el turno nocturno.
- 11:56–11:57 — diagnose-crash marca crashes sistémicos en t_61fed817 /
  t_3de9e23f / t_33a26e4c (`pid ... not alive`); la guerra dual-dispatcher
  estaba activa. Aux title-gen devuelve HTTP 404 (modelos muertos).
- 13:00:49 — 7 tareas quedan blocked SIN evento `blocked` registrado:
  t_fdbba03c, t_03352224, t_33a26e4c, t_b128cb7d, t_f4c24cca, t_3de9e23f,
  t_61fed817. No hay runs con `gave_up`; diagnose-crash (último run 11:57)
  y board-cleanup (04:00) descartados por horario. Autor no identificado.
- 13:06–13:46 — watchdog dispara 10× "ALERTA board desierto (cuota OK)" y
  crea 10 tareas gemelas de auditoría (9 done, esta auditoría y
  t_35af47ea running). Síntoma del parpadeo 0/0 de la guerra, no bug propio.
- 13:18 — workers de t_f4c24cca / t_3de9e23f mueren ~60s tras spawn (reap
  mutuo entre los dos dispatchers).
- 13:51:36 — el guard de concurrencia del tick mata pid 3842455 = run 682
  de esta auditoría (mató un síntoma mientras la guerra seguía).
- 14:01:43 — guard v2 actúa: "gateway dispatcher active — killed external
  kanban daemon". Última aparición del daemon.
- 13:59–14:03 — t_f4c24cca, t_3de9e23f y t_33a26e4c COMPLETAN; workers
  post-kill sobreviven >5 min (la ventana de muerte de la guerra era ~60s).
  Guerra terminada, verificada por comportamiento.

## Anomalías clasificadas

1. RESUELTA — Guerra dual-dispatcher (causa raíz del día). Daemon externo +
   dispatcher embebido del gateway se cosechaban mutuamente (368 reaps
   zombie desde el 26-ago). Incidente documentado + guard v2 committeado
   (`bf8bddb`) + daemon muerto verificado (`pgrep -f 'kanban daemon'` vacío)
   + supervivencia de workers observada.
2. RESUELTA — Ráfaga de 10 gemelas del watchdog: síntoma de (1). Mitigado
   por el watchdog v3 (contador canónico `kanban stats --json` + máx
   1 sucesor/2h). Test end-to-end 14:08: board viva → sin sucesor.
3. MITIGADA — Blocks sin-evento de 13:00:49 (7). Estado al cierre: 3 ya
   done, t_fdbba03c en triage, 2 human-gated legítimos (t_03352224 PAT
   upstream, t_b128cb7d OBJ-40-RES), t_61fed817 ver abajo. Autor sin
   identificar (abierto): escritura directa a la DB sin evento.
4. NUEVA — Clasificador del watchdog ciego a override envenenado.
   t_61fed817 lleva model_override `liodon-ai/Qwen2.5-7B-Instruct-FP8`
   (HTTP 404: modelo inexistente) y 6 crashes rc=0 consecutivos; su reason
   "Scratch artifact unavailable" encajaba en el patrón de auto-desbloqueo
   → loop infinito crash/unblock. Parche v3.1 en `~/.hermes/scripts/
   kanban-watchdog.sh`: (a) patrón "Scratch artifact unavailable" añadido,
   (b) guard BLOCKED-OVERRIDE: si la task tiene model_override NO se
   auto-desbloquea; se loguea el remedio (`hermes kanban set-model <id>
   none`). Probado 14:08: clasifica BLOCKED-OVERRIDE, human-gated intactos.
5. DEUDA CERRADA — Guard v2 quedó sin commit (el run previo murió rc=0 sin
   terminal-call tras escribirlo). Committeado `bf8bddb` y pusheado; ambas
   copias desplegadas sincronizadas (test_deployed_copies_sync.py 7/7).
6. FALSA ALARMA — "Gap" aparente de ticks 05:00–12:13 no existe: los
   timestamps continúan cada ~10 min (error de ventana al filtrar).

## Estado al cierre

- Cuota: session 20.4%, weekly 59.6% — healthy, sin drenaje de saldo.
- blocked restantes: t_03352224 y t_b128cb7d (human-gated, legítimos);
  t_61fed817 requiere limpieza manual de override (remediación logueada
  por el watchdog; no hay canal de mutación kanban desde este contexto).
- Watchdog v3.1 desplegado y probado; sin sucesores fantasma en cola.