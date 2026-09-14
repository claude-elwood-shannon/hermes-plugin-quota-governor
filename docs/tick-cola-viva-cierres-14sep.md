# Cola viva — cierre del 14-sep: docs de lo cerrado recientemente

**OBJ-30b** · task t_bfe1bb8e · clase C · 2026-09-14 · rama `workers` (HEAD d6200ee).

Complemento de `docs/tick-cola-viva.md` y `docs/tick-cola-viva-tests.md`: el 14-sep fue el dia con mas actividad del repo (19 commits) y varias cierres de objetivos quedaron sin ficha en `docs/`. Este doc los recoge, con el commit y la tarea kanban que los respalda (resumenes verificados en `task_runs` del board).

## Indice por objetivo

- **MEDIATOR**: 4 cierre(s)
- **OBJ-13**: 1 cierre(s)
- **OBJ-44**: 6 cierre(s)
- **OBJ-AUTODEV**: 1 cierre(s)
- **OBJ-METRICS**: 1 cierre(s)

## Cierres (en orden de commit / tarea)

### [OBJ-44] autoqueue.py fusionado a main

- Tarea: `t_e96613c1` · Commits: `bfe9b9a (merge --no-ff de la rama obj44-autoqueue-p, e52356e)`
- scripts/autoqueue.py + tests/test_autoqueue.py ya estan en el arbol de main; pytest 12/12, smoke-test dry-run sin mutacion, pusheado a origin/main via Tor (22925f9..bfe9b9a).

### [MEDIATOR] ruta A — objetivos aprobados se despachan contra balance

- Tarea: `t_7626791f` · Commits: `7edf0fa`, `merge 22925f9`
- El gate de cuota del tick bifurca: tarea ready sin assignee con tag objective:OBJ-XX y objetivo active con presupuesto libre se asigna aunque session/weekly >= 80% (gasta del balance del objetivo). Ruta B (resto): exige cuota < 80%. DIRECCION-STOP/STOP bloquean ambas rutas.

### [MEDIATOR] bridge v1.3.0 — 6 herramientas de gobernanza

- Tarea: `(mediador t_3cafd196)` · Commits: `47bbe29`
- GET /task, /tasks (filtros AND-combined), /git-log, /metrics; POST /approve-task (triage->specify->promote->ready + stamp) y /verify-task (extraccion de criterio + escaneo de evidencia -> PASS|FAIL|INCONCLUSIVE|NO_CRITERION|NOT_DONE). Fix: /move-task llamaba un subcomando inexistente.

### [MEDIATOR] bridge v1.4.0 + hermes-backup — endpoints /backup y huecos de backup

- Tarea: `t_6c5233a8` · Commits: `e44e787`
- /backup/snapshots|stats|log|health (solo lectura, restic via env file, payload final _backup_redact); hermes-backup cubre memories/docs de todos los perfiles, audit trails de logs/, y git push --all antes del snapshot.

### [OBJ-44] suite reparada — coleccion y fallos preexistentes

- Tarea: `t_a8a4418c / t_05715900` · Commits: `be95d21`, `e53dd9a`
- shim importlib scripts/tick_cola_viva.py (tests vuelven a colectar), fixture abandon-superseded reparada; 4 fallos privacy congelaban reglas pre-OBJ-43-FIX / pre-OBJ-40 (routing por capacidades; vllm-local confidencial). Tanda canonica: 894 passed / 1 skipped / 0 failed. Push a main OK.

### [MEDIATOR] bridge migrado a unidad systemd de usuario

- Tarea: `t_800f9764` · Commits: `b227160`
- hermes-bridge.service (Restart=always/5s, Linger=yes, PATH minimo reconstruido); el wrapper cron queda como monitor (probe HTTP + heartbeat + nudge systemctl). Verificado con kill -9 real: respawn ~5s. Documentado en docs/bridge-open-webui.md (How it runs).

### [OBJ-44] watchdog: romper bucle de remediacion TTL (R6)

- Tarea: `t_bf9be451 / t_32a71a49` · Commits: `4e661d1 (pusheado)`
- El bucle del 14-sep: tarea re-bloqueada por 'Merge encountered conflicts' cada 5m; R2 limpiaba el override envenenado pero la causa real era un merge conflict. R6(a): re-block con el mismo fingerprint tras remediation verificada -> triage; R6(b): misma remediacion fallida >=2x -> triage. R2 verifica persistencia releyendo la BD (deteccion de rc=0 mentiroso). kanban-watchdog.sh exporta HERMES_BIN (fuera del repo). El merge REAL 63d3b14 (2 parents) cerro t_32a71a49.

### [OBJ-13] perdida declarada: sync de forks t_8f51c92e

- Tarea: `t_a2ac01fd` · Commits: sin commit en este repo
- t_8f51c92e declarada perdida con comentario diagnostico; cierra needs_attention del objetivo.

### [OBJ-44] 13 tareas perdidas diagnosticadas y estampadas

- Tarea: `t_bbd6844e` · Commits: sin commit en este repo
- 12 perdidas superadas estructuralmente estampadas via abandon-superseded.py (superseders verificados); t_ee1f4e1a quedo como perdida REAL sin estampa. weekly-progress: OBJ-44 pasa de needs_attention a in_progress.

### [OBJ-44] perdida real t_ee1f4e1a resuelta por re-creacion (item 41)

- Tarea: `t_854fd824` · Commits: `595ff9d (ficha docs/obj44-skills-review-t_ee1f4e1a-2026-09-14.md)`
- 2 skills revisadas con mediciones vivas 2026-09-14 (hermes-quota-aware-dispatch, hermes-ollama-quota; sha256 en la ficha), estampa manual en abandon-stamps.jsonl con readback verificado. Documentado en docs/obj44-skills-review-t_ee1f4e1a-2026-09-14.md.

### [OBJ-44] hueco de tests del tick: paso 3.6 anti-sequia e incidente (predecesor inmediato)

- Tarea: `t_5ae86c2d` · Commits: `a97c424`
- 2 tests nuevos en tests/test_tick_cola_viva.py; 13 passed. Ya documentado en docs/tick-cola-viva-tests.md (t_5a3eafa6) — aqui solo se referencia.

### [OBJ-METRICS] efficiency-ratio contaba 0 tareas verificadas

- Tarea: `t_10f401d0` · Commits: sin commit en este repo
- El script ahora cuenta todos los objetivos al computar tareas_verificadas; ratio refleja las entradas VERIFIED de verifications.jsonl. Fix desplegado fuera del repo (sin commit visible en este checkout).

### [OBJ-AUTODEV] governor contra balance verificado en vivo

- Tarea: `t_db8ab4ef` · Commits: sin commit en este repo
- Tarea corriendo con weekly 100.02% gracias a presupuesto OBJ-AUTODEV ($0/$3 hoy): budget_check=(True,'ok'). Matiz: nacio pre-asignada, asi que el claim fue del dispatcher del gateway (sin re-chequeo); la ruta A del gate aplica a tareas ready sin assignee.

## Referencias cruzadas

- `docs/bridge-open-webui.md` — ciclo de vida systemd del bridge, endpoints v1.2 en la tabla; los endpoints v1.3/v1.4 (governance + /backup) solo estan en el historial de commits 47bbe29/e44e787 — candidato a ampliar la tabla si el mediador los usa.
- `docs/tick-cola-viva-tests.md` — cobertura del paso 3.6 e incidente (t_5a3eafa6, predecesor inmediato de este doc).
- `docs/obj44-skills-review-t_ee1f4e1a-2026-09-14.md` — ficha de re-creacion de t_ee1f4e1a.

## Estado de publicacion

- Rama local `workers` (no existe en origin, verificado con `git ls-remote --heads` en la tarea predecesora): push manual por convencion del usuario (`git push origin workers`, repo via Tor).

