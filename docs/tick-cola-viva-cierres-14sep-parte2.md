# Cola viva — huecos de la ficha del 14-sep: commits sin ficha (parte 2)

**OBJ-30b** · task t_d24b99fe · sucesor estructural de t_bfe1bb8e · clase C ·
2026-09-14 · rama `workers` (HEAD a0d2c1b).

Complemento de `docs/tick-cola-viva-cierres-14sep.md` (t_bfe1bb8e): esa
ficha recogio los 13 cierres del dia, pero el cruce entre el log del dia
(`git log --since 2026-09-14 00:00`) y los commits citados en `docs/`
deja 3 commits del 14-sep sin ficha que los cubra. Este doc los recoge.

## Commits del 14-sep sin ficha previa

### [MEDIATOR] a17d08c — inventario de objetivos aprobados con presupuesto autonomo

- Commit: `a17d08c` (00:57) · Tarea de origen: cadena MEDIATOR
  (t_7626791f documenta el seguimiento, ruta A, no este commit).
- `scripts/approved_objectives.py`: tabla `approved_objectives` en el
  kanban.db compartido (SQLite nativo, sin gemelo JSON). Seed:
  OBJ-AUTODEV $3/d + OBJ-VLLM $1/d + OBJ-METRICS $0.50/d ($4.50
  preaprobados). Regla de pausa de 3 dias agotados
  (`exhausted_days`/`last_exhausted_day`).
- Gate de presupuesto (§6 del mensaje): el tick retiene tareas tagged
  cuyo objetivo este unknown/paused/exhausted; tabla ausente ->
  retencion fail-safe solo para tagged. Carga del modulo fail-open
  (14 tests siguen pasando).
- Ciclo de vida: achieved (criterios machine-checkables; OBJ-AUTODEV
  perpetual), paused tras 3 dias agotados, reactivacion con
  presupuesto, discarded SOLO por el mediador. Eventos en
  `logs/objective-events.jsonl`.
- Bridge: `GET /objectives` (+filtro status) y
  `POST /update-objective` (INSERT-or-UPDATE; housekeeping intacto) +
  OpenAPI. Pantalla matinal: seccion OBJETIVOS APROBADOS con gasto por
  objetivo y umbrales de color.
- Verificado en vivo: create-task devuelve ID tras el respawn de
  contexto limpio (setsid + env -i, §11), /objectives lista el seed,
  /update-objective actualiza OBJ-METRICS; render TOTAL $0.00/$4.50.

### [MEDIATOR] 6a954cf — fix respawn del bridge: PATH minimo reconstruido

- Commit: `6a954cf` (01:20) · Tarea: cadena del bridge (t_800f9764
  documenta el servicio systemd, no este fix previo).
- Sintoma: `env -i` con `PATH="$PATH"` propagaba un PATH restringido
  (cron: /usr/bin:/bin; contexto worker Hermes: sin ~/.local/bin) al
  daemon re-spawneado. El bridge hace subprocess.run(["hermes", ...]),
  que vive en $HOME/.local/bin/hermes — cada subprocess fallaba con
  FileNotFoundError.
- Fix: el respawn pasa un PATH minimo reconstruido anclado en $HOME mas
  fallback HERMES_HOME; HERMES_*/PYTHONPATH/AO_KANBAN_DB del worker
  siguen stripped.
- Verificado en vivo: kill del bridge, respawn via wrapper bajo
  `env -i PATH=/usr/bin:/bin` (simulacro cron) — daemon mantiene :9120
  y `GET /tasks` ejecuta `hermes kanban list` con exito.

### [OBJ-44] 63d3b14 — merge real del fix cola-viva py2 a main

- Commits: `63d3b14` (10:08, merge 2 parents: b227160 + 5e5b9cb) ·
  Tareas: t_32a71a49 (rehacer el merge REAL — su cierre quedo invalidado
  por la deteccion de falsos merges del watchdog), t_bf9be451 (limpieza
  de probes triage + fix del bucle del watchdog, R6).
- Contenido de la rama fusionada (5e5b9cb, 13-sep): el tick leia un
  kanban.db inexistente tras la resolucion profile-scoped de
  HERMES_HOME y declaraba "cola seca legitima" para siempre -> fallback
  al ~/.hermes/kanban.db raiz; CLASE_C_RE acepta clase:C-estructural
  (los done de autoqueue son padres legitimos); has_open_successor solo
  bloquea por estados abiertos — un sucesor DONE ya no sella a su padre
  para siempre. Verificado: dry-run encuentra sucesor de t_c7e04f98,
  execute creo t_71e0aef9, 13/13 unittests.
- Nota de trazabilidad: `tick-cola-viva-cierres-14sep.md` cita
  `63d3b14` solo como "merge REAL que cerro t_32a71a49" dentro de la
  ficha del watchdog R6; el contenido tecnico de 5e5b9cb (los 3 fixes
  del tick) no tenia ficha propia — este doc la anade.

## Referencias cruzadas

- `docs/tick-cola-viva-cierres-14sep.md` — ficha de los 13 cierres del
  dia (t_bfe1bb8e), predecesor inmediato de este doc.
- `docs/bridge-open-webui.md` — ciclo de vida systemd del bridge
  (t_800f9764); el fix 6a954cf es el fix de respawn previo al servicio.
- `docs/tick-cola-viva.md` — referencia general del tick.

## Estado de publicacion

- Commit nuevo en rama local `workers` (la rama no existe en origin,
  verificado con `git ls-remote --heads` en la tarea predecesora):
  push manual por convencion del usuario (`git push origin workers`,
  repo via Tor).
