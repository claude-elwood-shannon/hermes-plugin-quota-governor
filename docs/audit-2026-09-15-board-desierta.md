# Audit 2026-09-15: board desierta con cuota (watchdog OBJ-39)

Alcance: logs de hoy (`quota-governor-tick.log` 142 líneas 00:06–05:10,
`kanban-watchdog.log`, `cron-health-check.{log,jsonl}`, `diagnose-crash.log`,
`errors.log`, `gateway.log`, journalctl de crond) y eventos/runs de
`kanban.db`. Complementa `audit-2026-09-12-board-desierta.md`.

## Línea de tiempo (CEST)

- 00:06–05:10 — 32 ticks del governor cada ~10 min, sin gaps; session 49%→98%
  (01:16) y reset de ventana ~03:08 (97.8%→0.4%); cost plancha $5.0000.
- 00:03–01:14 — tareas MEDIATOR y auto-creadas fluyen normal (mo=None).
- 01:25→01:57 — GAP del crontab de usuario: los */5 (watchdog, obs-serve,
  open-webui-bridge) no disparan; el governor detecta "cron-health-check log
  stale 2518s" a 01:57 y lanza el health-check manual → reinicia los 3 DEAD.
  Causa raíz: REPLACE del crontab a 01:25:05 y 01:25:50 (journal) durante el
  deploy de observabilidad (t_a9319828, Elasticsearch) que dejó fuera las 3
  líneas */5; el propio health-check las re-añadió a 01:57 y el RELOAD a
  02:15:01 (7/7 OK) las restauró. Desde 02:15: 7/7 OK continuo.
- 02:19–02:47 — t_241c8832 (OBJ-CODEQUALITY) nace con skills fantasma
  `coding-standards-compliance`, crashea rc=1 (run 1139), alguien fija
  override `glm-5.2` (02:21:16, sin comentario ni resumen que lo explique;
  modelo interactivo caro prohibido para workers por G7), run 1140 completa.
  Riesgo de coste, no de disponibilidad.
- 02:50–05:17 — el creador autónomo empieza a fijar `--model gpt-oss:20b`
  (mo al nacer) en sus tareas de pr-ollama: t_ed331493, t_0f22e52e,
  t_9324c501, t_581f33da, t_6cf29b02, t_5cd9ac46, t_ebe7b550, t_d2d6a0d7.
- 03:08:41 — primer block del día con pin gpt-oss:20b (t_ed331493);
  03:25:02 el remediador TTL R2 lo re-fija a deepseek-v4-flash + unblock;
  la tarea completa 4 min después. Mismo patrón en t_0f22e52e, t_9324c501,
  t_6cf29b02, t_ebe7b550 (5 TTL-REMEDIATED "R2 override 'gpt-oss:20b' ->
  pin gate"). t_9324c501 acumuló 4 crashes rc=1 antes de su R2 (04:05) y
  completó a las 04:33; requirió además diagnose-crash por skills fantasma
  `coding-standards` (relicto de antes de la REGLA SKILLS, t_414b108a).
- 03:05:01 y 05:10:01 — 2 alertas "board desierto (cuota OK)"; 2 sucesores
  (t_1a895159 done 03:33, esta auditoría). Guardia anti-ráfaga respetada
  (máx 1/2h). Ambas TRUE POSITIVES en su instante de check; detalles abajo.
- 03:33:30 — el sucesor de 03:05 genera 4 tarjetas en triage blindando el
  deploy de crontab (t_da32e7f9 fusión en vez de REPLACE, t_cec89d77
  py_compile pre-commit, t_5f5b22df kanban_decomposer sin fallback,
  t_a1a2ec4b gpu-health-check path-validation).
- 04:31:05 — t_414b108a endurece el prompt del creador: REGLA SKILLS
  (prohibir campo skills). Eficaz: desde entonces mo/skills fantasma solo
  reaparecen vía el pin de G7 (ver anomalía 1).

## Anomalías clasificadas

1. NUEVA — Contradicción pin-gate vs remediador: G7 del creador autónomo
   OBLIGA a `--model gpt-oss:20b` para pr-ollama ("MULTI-PROV-10.5, sin
   peak pricing") pero `ttl_blocked.py` la tiene en `DEAD_OVERRIDES`
   (commit d9aae7b, 13-sep 17:54) y su DEFAULT_OFFER NO la incluye
   (sí gpt-oss:120b-cloud). Efecto observado: cada tarea con pin nace
   pre-condenada a crash/block → R2 la re-fija a deepseek-v4-flash (con
   peak pricing x2 en ventanas 01-04/06-10 UTC) → completa. Coste: pin
   barato declarado, pin caro pagado; retraso de ~15-25 min por tarea.
   EVIDENCIA CONTRADICTORIA: t_d2d6a0d7 nació 05:17 con mo=gpt-oss:20b y
   COMPLETÓ 05:20 con ese pin (artefacto real: refactor de run() en
   tick-cola-viva.py, 328 líneas, helpers <50, py_compile OK). También
   t_581f33da y t_5cd9ac46 completaron con el pin sin R2. Hipótesis: el
   modelo revivió (ollama-cloud sirve de nuevo gpt-oss:20b) y
   DEAD_OVERRIDES quedó obsoleto; o el crash inicial no era del modelo.
   ACCIÓN REQUERIDA (humano/orquestador): sonda `/api/tags` de ollama-cloud
   y, si gpt-oss:20b está vivo, quitarlo de DEAD_OVERRIDES y añadirlo a
   DEFAULT_OFFER['ollama-cloud']; si está muerto, quitar el pin de G7.
   Con el estado actual el sistema se autorepara, pero cicla crash→R2 en
   cada tarea pinneada.
2. RESUELTA-EN-EL-DÍA — Gap del crontab 01:25→01:57 (ver timeline). Causa
   raíz: deploy de crontab por REPLACE en vez de fusión, disparado por el
   work de t_a9319828. El creador ya generó 4 tarjetas de blindaje en
   triage (t_da32e7f9 etc.). Sin recurrencia tras 02:15.
3. FALSEDAD CONFIRMADA — El "board desierto" de las 2 alertas: la de 03:05
   precede por 47s a la creación de t_ed331493 (03:05:48, y claimed
   03:05:58): el check la vio con razón. La de 05:10:01 se dispara porque
   t_ebe7b550 estaba BLOCKED desde 05:02:34 (count canonical no cuenta
   blocked) y la remediación R2 de su pin llegó 05:10:04 (3s después del
   check; el watchdog hace desierto→crear ANTES de su fase TTL). Consecuencia
   menor: sucesor redundante de auditoría mientras había trabajo real
   bloqueado auto-reparable. Mitigación opcional (no urgente): mover la
   sección 1 del watchdog tras la sección 2/3, o ignorar blocked con pin
   en DEAD_OVERRIDES recién bloqueadas (<15 min).
4. NUEVA — Override `glm-5.2` en t_241c8832 (02:21:16, evento
   model_override_set) sin autor identificable (sin comentario/resumen).
   glm-5.2 es interactivo-caro y está en DEAD_OVERRIDES como prohibido
   para workers; la tarea igual completó (run 1140). La tarea fue además
   la última con skills fantasma (`coding-standards-compliance`), reparada
   por diagnose-crash. Vigilar recurrencia: si vuelve a aparecer un
   override caro sin autor, ampliar auditoría de eventos override_set.
5. DEUDA — 1992 warning de "Auxiliary kanban_decomposer: main provider
   ollama-cloud is unavailable" hoy (8616 el 14-sep, 0 hasta el 12-sep;
   inicio 13-sep 15:32). Los workers completan (no bloquean), pero el
   cliente auxiliar (títulos) degrada a no-op con spam de log cada tick
   del decomposer. Relacionado con anomalía 1: la misma inconsistencia
   pin/oferta sugiere que ollama-cloud estuvo intermitente desde el 13-sep.
6. CONFIRMADA-SANA — Guard v2 dual-dispatcher activo todo el día ("gateway
   dispatcher active — NOT respawning" en 32/32 ticks), sin daemon externo,
   sin pids muertos, copias del repo sincronizadas (diff vacío) y working
   tree limpio. La cura del 12-sep se sostiene.

## Estado al cierre (05:40 CEST)

- Board: 2 running (esta auditoría + t_d2d6a0d7 completando), 12 triage
  (4 nuevas de blindaje de crontab), 0 ready, 0 blocked. 26 tareas creadas
  hoy, 24 completadas hasta el momento del corte.
- Cuota: session 3.9% (ventana renovada ~03:08), weekly 22.7% — healthy;
  cost plancha $5.0000 (no drena saldo).
- Crons: 7/7 OK desde 02:15; crontab íntegro.
- Sin acción automática tomada sobre código o crons desde esta auditoría:
  la anomalía 1 requiere decisión (revivir pin G7 o declarar muerto el
  modelo); la 3 es cosmética; las tarjetas de blindaje ya existen en
  triage para la 2.
