# Cola viva — huecos de la ficha del 14-sep: bloque inicial del bridge (parte 3)

**OBJ-30b** · task t_ca0ebc12 · sucesor estructural de t_d24b99fe · clase C ·
2026-09-14 · rama `workers` (HEAD 9ce4681).

Tercero de la serie: `docs/tick-cola-viva-cierres-14sep.md` (t_bfe1bb8e)
cubro los 13 cierres del dia y `...-parte2.md` (t_d24b99fe) los 3
commits sin ficha de la madrugada/media manana. El cruce residual entre
el log del dia (`git log --since 2026-09-14 00:00`) y los hashes citados
en `docs/` deja 4 commits del bloque inicial del bridge Open WebUI
(madrugada del 14-sep, 00:01-00:10) sin ficha. Este doc los recoge.

## Commits del 14-sep sin ficha previa

### [MEDIATOR] 8b40a4f — integracion del bridge Open WebUI en el plugin repo

- Commit: `8b40a4f` (00:01) · Tarea de origen: t_f24494a4.
- Estado previo: la infraestructura mediador -> bridge -> Hermes era un
  fichero suelto (`~/git/hermes-bridge/server.py`) sin version ni
  tercera copia ni health-check.
- Contenido: `scripts/bridge/open-webui-bridge.py` v1.2 (puerto 9120,
  mismo contrato de API, anade `GET /file` guardado);
  `scripts/bridge/open-webui-bridge-cron.sh` wrapper supervisor con
  patron obs-serve (probe HTTP + heartbeat + convergencia a la ruta
  canonica en `~/.hermes/scripts/bridge/`, mata copias legacy);
  `docs/bridge-open-webui.md`; `test_bridge_deploy_sync.py` (sincronia
  md5 3 copias + spec-version live, 16 checks); bridge anadido a
  `cron-health-check.sh` en 6a posicion via
  `open-webui-bridge.heartbeat`.
- Verificado en vivo: convergencia de la copia legacy a la canonica,
  bucle real de kill/respawn a traves de cron-health-check, 6/6 OK,
  16/16 bridge-sync PASS y suites vecinas verdes (tick 7, zombie 20,
  health 28, repo-sync 75).

### [MEDIATOR] 6022678 — merge de feat/bridge-openwebui-2026-09 a main

- Commit: `6022678` (00:02) · Merge 2-parents de la rama que trae
  8b40a4f; sin contenido propio (747 insertions del commit anterior).
- Cierre de rama: la rama feature quedo fusionada en
  `main`; `origin/feat/bridge-openwebui-2026-09` sigue en origin.

### [MEDIATOR] ba4cb02 — docs: ciclo de vida en dos capas del bridge

- Commit: `ba4cb02` (00:05) · Solo docs
  (`docs/bridge-open-webui.md`, +15/-4).
- El patron heartbeat requiere que el wrapper corra en SU propia linea
  cron (*/5) — si solo lo llamara el verificador cada 15 min, cada tick
  veria un heartbeat rancio (~900s > tolerancia 480s) y flearia DEAD un
  servicio sano. Cazado en vivo en el tick 00:00:01 (504s marcado
  DEAD); arreglado cableando `*/5 open-webui-bridge-cron.sh` al
  crontab del host. Verificador a */15 via cron-health-check.

### [MEDIATOR] 19572ef — fix portabilidad del bridge: cero rutas de host en el modulo

- Commit: `19572ef` (00:10) · Familia TestPortability
  (`test_obj27_f5.py`, `test_objective_budgets.py`) prohibe rutas
  absolutas de host en modulos python para que el repo sea publicable.
  `open-webui-bridge.py` ahora deriva la raiz del repo de plugin desde
  `__file__` (copia repo) o `BRIDGE_PLUGIN_REPO` env (copias
  desplegadas; el wrapper del supervisor la exporta — los wrappers
  fijan rutas por house convention). Bloque docs de cron pasado a
  rutas `~`. `test_bridge_deploy_sync.py` crece a 21 checks con el
  guard de portabilidad.
- Verificado en vivo: respawn desde la ruta canonica con el codigo
  portable; live spec 1.2.0, `/board` y `/file` 200.

## Referencias cruzadas

- `docs/tick-cola-viva-cierres-14sep.md` — ficha de los 13 cierres
  (t_bfe1bb8e); cubre la evolucion posterior del bridge (v1.3.0 47bbe29,
  v1.4.0 e44e787, unidad systemd b227160) pero no este bloque inicial.
- `docs/tick-cola-viva-cierres-14sep-parte2.md` — parte 2 con
  6a954cf (fix respawn PATH) y el merge py2 (63d3b14).
- `docs/bridge-open-webui.md` — doc canonico del bridge (creado en
  8b40a4f, extendido en ba4cb02 y 19572ef).

## Estado de publicacion

- Commit nuevo en rama local `workers` (la rama no existe en origin,
  verificado en las dos tareas predecesoras): push manual por
  convencion del usuario (`git push origin workers`, repo via Tor).
