# eslabon-perdido.md — MEDIATOR t_4fa0a4b5: el ciclo de autonomía cierra

Fecha: 2026-09-15 · Autor: Hermes (pr-ollama) · Estado: implementado y verificado

## El problema

5 objetivos activos en `approved_objectives` ($5.00/día aprobados) pero cero
tareas creadas bajo ellos. El efficiency ratio llevaba 7 días en CRITICO:
0 tareas verificadas, $25.94 de gasto. La cadena estaba rota en su primer
eslabón:

```
Objetivos aprobados ✅ (tabla approved_objectives en kanban.db)
Governor contra balance ✅ (tick + budget_check §6 en dispatch)
P2 backlog-guard ✅ (cola viva)
P5 dedup ✅ (successor-sig)
BUT: nadie crea tareas bajo objetivos ❌  ← eslabón perdido
```

## Causa raíz (3 roturas encadenadas)

1. **El prompt del cron `autonomous-task-creator` no conocía la tabla.**
   Apuntaba al documento legacy `docs/autonomous-objectives.md` (namespace
   OBJ-0N) y su fuente de trabajo era ese fichero, no
   `approved_objectives`. Además su G5 exigía títulos "OBJ-0N" y el gate
   (`quota-gate.py`) no le pasaba la tabla en el contexto.

2. **El gate no inyectaba el snapshot de objetivos.** El pre-run
   `quota-gate.py` consultaba proveedores pero nunca la tabla: el creator
   no tenía forma de respetar `spent_today < budget_daily` sin una segunda
   lectura (que tampoco estaba en el prompt).

3. **El efficiency ratio era ciego a los nuevos ids.**
   `scripts/obs/efficiency-ratio.py` contaba SOLO tags
   `objective:AUTODEV|AUTOREPAIR` (`BUDGET_OBJECTIVES` hardcodeado). Aunque
   el creator hubiera creado tareas `objective:OBJ-CODEQUALITY`, la métrica
   nunca las habría contado. Lo mismo en `morning-screen.py` (agrupación
   `OBJ-\d+`), `validate-guardrails.py`, `objective-proposer.py` (GR1) y
   `open-webui-bridge.py` (filtro por objetivo).

## Qué se cambió

### 1. Prompt del cron (`hermes cron edit 0b9a2b17116f`, perfil pr-ollama)
- Fuente de trabajo = tabla `approved_objectives` (comando sqlite3 de
  auto-servicio + bloque `objectives` del contexto).
- G5 reescrito: títulos por id de tabla (OBJ-AUTODEV, OBJ-CODEQUALITY,
  OBJ-METRICS, OBJ-SYSADMIN, OBJ-OBSERVABILITY).
- Budget gate por objetivo: verificar `spent_today < budget_daily` antes de
  crear; `[SILENT]` si todos agotados.
- Cuerpo de tarea con `success:` verificable (el ratio solo cuenta tareas
  con criterio declarado y evidenciado).
- Dedup P5 + DIRECCION-STOP explícitos. Zombis/G2/G7/privacy intactos.
- Añadido pr-vllm al mapa G7 (pin `liodon-ai/Qwen2.5-7B-Instruct-FP8`),
  alineado con ALLOWED_PROFILES del gate (OBJ-40).

### 2. `scripts/quota-gate.py` — snapshot de objetivos en el contexto
- `compute_objectives_snapshot()`: filas ACTIVE de la tabla (id, name,
  status, budget_daily, spent_today, description, success_criterion).
- Inyectado como `context.objectives` en AMBAS ramas de salida
  (wakeAgent true/false). Observador: no cambia wakeAgent — la decisión
  [SILENT] es del creator; el enforcement de dispatch vive en
  `approved_objectives.budget_check` (§6, tick).
- Fail-open: tabla ausente → `[]` (el prompt manda leer la tabla a mano o
  callar).

### 3. `scripts/obs/efficiency-ratio.py` — presupuesto dirigido por la tabla
- `budget_objectives(db)`: ids de la tabla + par legacy (AUTODEV,
  AUTOREPAIR). Cache por proceso.
- Regex de objetivo construida desde ese conjunto: cualquier
  `objective:<id-de-tabla>` cuenta como tarea de presupuesto y su gasto
  como strict (via tag directo o join task_id→body). achieved/paused
  siguen contando para el gasto histórico.
- Tabla ausente → par legacy (fail-open, comportamientos anteriores
  intactos).

### 4. Regex OBJ-\d → OBJ-[A-Za-z0-9._-] en los consumidores del namespace
- `scripts/obs/morning-screen.py` (rendición semanal VUELO)
- `scripts/validate-guardrails.py` (GR1 active-objectives)
- `scripts/objective-proposer.py` (2 occurrences)
- `scripts/bridge/open-webui-bridge.py` (OBJ_RE, filtro ?objective=)

## Verificación

- `test_cron_prompt_zombie.py` 20/20 (incluye md5 repo=copias desplegadas;
  copias resincronizadas: ~/.hermes/scripts, perfiles pr-ollama y
  pr-nanogpt).
- `test_efficiency_ratio.py` 12/12 (regresión legacy intacta).
- `test_efficiency_ratio_objectives.py` 7/7 (NUEVO: id de tabla cuenta,
  strict via tag y via task_id, no-tabla excluido, fail-open legacy,
  achieved cuenta gasto, sin tag nunca cuenta).
- `test_zombie_check.py` 19/19; `scripts/obs/test_morning_screen.py`
  28/28; `test_objective_proposer.py` 50/50; `test_validate_guardrails.py`
  54/54; `tests/` 120/120; `test_bridge_file_endpoint.py` 24/24.
- Gate en vivo: `objectives` presente con los 5 activos y presupuesto.
- Tanda completa `--ignore=tests` tiene 1074 errores de COLECCIÓN
  preexistentes (import relativo de `__init__.py` con
  `--confcutdir=tests`); idéntico en HEAD sin cambios (verificado con
  git stash). `tests/test_bridge_governance_endpoints.py` 5b falla por
  estado del board host (t_c7e04f98 archivada); también preexistente.

## Cadena de efecto esperada

Creator crea 1 tarea/tick bajo un objetivo activo con criterio verificable
→ el worker la cierra con evidencia → efficiency-ratio la cuenta (tag de
tabla visible) y atribuye su gasto strict → ratio deja de ser 0/CRITICO →
morning screen agrupa cierres por objetivo → el morning report muestra el
consumo por objetivo con datos reales.
