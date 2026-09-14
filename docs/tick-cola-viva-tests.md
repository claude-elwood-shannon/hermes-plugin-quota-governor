# tick-cola-viva.py — tests: paso 3.6 anti-sequía e incidente de sequía

**OBJ-30b** · sucesor estructural de t_5ae86c2d (OBJ-44) · clase C ·
2026-09-14.

Complemento de `docs/tick-cola-viva.md`: documenta la cobertura de tests
cerrada por el commit `a97c424` (rama `workers`) para el paso 3.6
anti-sequía y la rama de incidente del paso 4 — el hueco verificado por la
ficha `obj44-tick-cola-viva-tests-ficha.md` (perfil pr-ollama).

## Qué cubre cada test (`tests/test_tick_cola_viva.py`)

Ambos reutilizan los helpers herméticos del fichero (BD temporal,
`HERMES_HOME=tmp`, ledger en `quota-governor/cola-viva.jsonl`) y no tocan
el script.

- **`test_step36_drought_check_dry_run`** — paso 3.6 con `execute=False` y
  tablero sin candidatos (0 ready, sin cierres clase:C <24h): la única
  decisión es `DRY: cola viva: paso 3.6 anti-sequia evaluado (dry-run)`,
  el ledger registra acción `drought-check`, y no se crea ni asigna nada
  (la BD queda con 0 tareas).

- **`test_step4_triage_incident_vs_legit_drought`** — paso 4 con
  `execute=True` y tablero seco, ambas ramas en un solo test:
  - 5 tareas en `triage` → `INCIDENTE: sequia con 5 tareas en triage — el
    board espera al usuario, no esta seco` (acción `waiting-user`,
    `triage: 5` en el ledger). Rama introducida por el commit `8f4870a`.
  - con <5 (borra 3, quedan 2) → `cola seca legitima: sin trabajo legitimo
    (regla de oro: no filler)` (acción `cola-seca-legitima`).

Con `execute=True` los únicos writes del tick pasan por el CLI `hermes`
(create/assign), que no se invoca en estos escenarios porque no hay ready
ni cierre clase:C reciente: los tests no dependen de red ni del CLI real.

## Verificación

```
.venv/bin/python -m pytest tests/test_tick_cola_viva.py -q
→ 13 passed in 4.14s   (11 existentes + 2 nuevos)
```

Script sin modificar: el diff de `a97c424` toca solo
`tests/test_tick_cola_viva.py` (+61 líneas).

## Estado de publicación

- Commit `a97c424` en rama local `workers`, sobre `origin/main`.
- **Publicado** (re-verificado 2026-09-14 ~19:50): `git ls-remote` muestra
  `refs/heads/workers` en origin y `origin/workers` apunta a `7c47ae9`,
  que contiene tanto `a97c424` como esta ficha (`d6200ee`). El push
  lo ejecutó el usuario manualmente (convención: push siempre manual,
  repo vía Tor).
- Suite en el estado commiteado (re-verificación 2026-09-14 ~19:50):
  `tests/test_tick_cola_viva.py` 19 passed; resto de `tests/` 66 passed;
  raíz (`test_quota_planner.py` + `test_tick_cola_viva.py`) 43 passed.
  0 fallos en las tres tandas.
