# Guardarrailes para Objetivos Autónomos (OBJ-17)

> **Documento:** Diseño e implementación de guardarrailes que limitan al governor
> antes de que pueda proponer nuevos objetivos por sí mismo (OBJ-16).
> **Estado:** Implementado (Sep 2026)
> **Prioridad:** P4 — debe completarse ANTES que OBJ-16
> **Creado:** 2026-09-01

---

## 1. Visión General

OBJ-16 habilita al governor para proponer nuevos objetivos basados en patrones
que detecta (bugs recurrentes, oportunidades de mejora). Antes de darle esa
capacidad, necesitamos guardarrailes que limiten qué puede hacer y qué no.

Este documento define los 11 guardarrailes, su implementación técnica
(script `validate-guardrails.py`), y cómo se integran con el task creator.

---

## 2. Los 11 Guardarrailes

Cada guardabroil tiene un ID (GRn), una descripción, su implementación técnica,
y el tipo de validación (estática o dinámica).

### GR1: No más de 5 objetivos activos simultáneamente

**Descripción:** El sistema nunca debe tener más de 5 objetivos con tareas
no terminadas (ready/running/blocked) al mismo tiempo.

**Implementación:** Dinámica. El validador consulta `kanban.db`, agrupa
tareas por tag `objective:OBJ-N`, cuenta objetivos con al menos una tarea
no terminal. Si >= 5, rechaza la propuesta.

**Validación:** `check_max_active_objectives(kanban_db_path) -> GuardrailResult`

### GR2: No tocar NADA fuera del repo del plugin

**Descripción:** Los objetivos propuestos no pueden sugerir modificar archivos
fuera de `REPO/`.

**Implementación:** Estática. El validador analiza el texto del objetivo
propuesto buscando paths de archivo. Cualquier path que no esté dentro del
repo del plugin se rechaza. Excepción: `~/.hermes/` está permitido por GR3.

**Validación:** `check_file_scope(text) -> GuardrailResult`

### GR3: No tocar NADA fuera de ~/.hermes/

**Descripción:** Los objetivos pueden modificar archivos dentro de `~/.hermes/`
(config, scripts, skills, etc.) pero no fuera de él ni fuera del repo del plugin.

**Implementación:** Estática. Complementa GR2. Si el texto menciona paths
absolutos que no están bajo `~/.hermes/` ni bajo el repo del plugin, se rechaza.

**Validación:** `check_file_scope(text) -> GuardrailResult` (combinado con GR2)

### GR4: No proponer objetivos que toquen archivos del sistema

**Descripción:** Archivos críticos del sistema como `.env`, `.ssh/config`,
`/etc/`, `/var/`, `/proc/`, `/sys/` no pueden ser tocados por objetivos
propuestos.

**Implementación:** Estática. Lista negra de paths y patrones. El validador
detecta menciones a estos archivos en el texto del objetivo.

**Validación:** `check_system_files(text) -> GuardrailResult`

### GR5: No proponer objetivos que requieran credenciales nuevas sin aprobación

**Descripción:** Si un objetivo requiere nuevas API keys, tokens, o
credenciales, debe marcarse como "requires_human_approval" y no auto-promocionarse.

**Implementación:** Estática. Detección de palabras clave: "API key",
"credential", "token", "secret", "password", "auth key", "login".

**Validación:** `check_credentials(text) -> GuardrailResult`

### GR6: Máximo 1 objetivo nuevo propuesto por día

**Descripción:** El sistema no puede proponer más de un objetivo nuevo por
día natural. Si ya propuso uno hoy, los demás se rechazan hasta mañana.

**Implementación:** Dinámica. El validador consulta `objective-proposals.jsonl`
un registro append-only de propuestas. Si ya hay una entrada con la fecha de
hoy, rechaza.

**Validación:** `check_daily_proposal_limit(state_file) -> GuardrailResult`

### GR7: No proponer objetivos que modifiquen config.yaml sin aprobación humana

**Descripción:** Cualquier objetivo que sugiera modificar `config.yaml` debe
marcarse como "requires_human_approval".

**Implementación:** Estática. Detección de "config.yaml" o "config.yml"
en el texto del objetivo.

**Validación:** `check_config_yaml(text) -> GuardrailResult`

### GR8: Todo objetivo propuesto entra en triage

**Descripción:** Los objetivos propuestos por el governor NUNCA se promueven
automáticamente a "ready". Siempre entran en "triage" para que el usuario
decida.

**Implementación:** Dinámica. El script que crea la tarea usa el flag
`--triage` o `initial_status: "triage"`. El validador verifica que el
status de la tarea creada sea "triage".

**Validación:** `check_triage_only(task_creation_args) -> GuardrailResult`

### GR9: No proponer objetivos que requieran instalar paquetes sin aprobación

**Descripción:** Si un objetivo requiere `pip install`, `apt install`,
`npm install`, etc., debe marcarse como "requires_human_approval".

**Implementación:** Estática. Detección de comandos de instalación.

**Validación:** `check_package_install(text) -> GuardrailResult`

### GR10: No crear, modificar ni eliminar archivos en ningún otro repo de ~/git/

**Descripción:** El governor solo puede tocar el repo del plugin. Otros repos
en `~/git/` están prohibidos.

**Implementación:** Estática. Detectar paths bajo `~/git/` que no sean
el repo del plugin.

**Validación:** `check_other_repos(text) -> GuardrailResult`

### GR11: No modificar archivos del sistema operativo fuera de ~/.hermes/

**Descripción:** El governor no puede modificar archivos del SO fuera de
`~/.hermes/`. Esto incluye `/etc/`, `/usr/`, `/var/`, `/tmp/` (para
persistencia), etc.

**Implementación:** Estática. Similar a GR4 pero más amplio: cualquier path
absoluto que no esté bajo `~/.hermes/` o el repo del plugin se marca.

**Validación:** `check_os_files(text) -> GuardrailResult` (combinado con GR2/GR3)

---

## 3. Arquitectura de Implementación

```
┌─────────────────────────────────────────────┐
│   autonomous-task-creator (cron, 30m)       │
│                                             │
│   1. quota-gate.py (pre-run script)         │
│   2. Agent reads objectives doc             │
│   3. Agent proposes new objective (OBJ-16)  │
│   4. ⚡ validate-guardrails.py              │
│      ├─ GR1: max 5 active objectives        │
│      ├─ GR2/GR3/GR11: file scope            │
│      ├─ GR4: system files blacklist         │
│      ├─ GR5: credential detection           │
│      ├─ GR6: daily proposal limit           │
│      ├─ GR7: config.yaml detection          │
│      ├─ GR8: triage-only enforcement        │
│      ├─ GR9: package install detection      │
│      └─ GR10: other repos in ~/git/     │
│   5. If all pass → create task in triage    │
│   5b. If any fail → log rejection, skip    │
│   6. Record proposal in proposals.jsonl     │
└─────────────────────────────────────────────┘
```

### Flujo de validación

1. El task creator (o el script que propone objetivos) llama a
   `validate-guardrails.py` con el texto del objetivo propuesto.
2. El script ejecuta los 11 checks.
3. Devuelve JSON con:
   - `allowed: true/false`
   - `violations: [{id, message}]` (vacío si allowed)
   - `warnings: [{id, message}]` (no bloquean pero requieren atención)
   - `requires_human_approval: true/false`
4. Si `allowed: false`, el objetivo no se crea.
5. Si `allowed: true` pero `requires_human_approval: true`, el objetivo
   se crea en triage con un body que marca "REQUIRES HUMAN APPROVAL" y
   la lista de warnings.
6. Si `allowed: true` y `requires_human_approval: false`, el objetivo
   se crea en triage sin warnings.

### Registro de propuestas

Cada propuesta (aprobada o rechazada) se registra en
`~/.hermes/quota-governor/objective-proposals.jsonl`:

```json
{
  "timestamp": "2026-09-01T12:00:00Z",
  "date": "2026-09-01",
  "title": "OBJ-20: Optimizar health checks",
  "allowed": true,
  "violations": [],
  "warnings": [],
  "requires_human_approval": false,
  "task_id": "t_xxx"  // si se creó
}
```

---

## 4. Integración con el task creator

El cron prompt del autonomous-task-creator se actualiza para incluir:

1. Antes de proponer un objetivo nuevo (OBJ-16): ejecutar
   `validate-guardrails.py --title "..." --body "..."` y respetar el veredicto.
2. Si el validador devuelve `allowed: false`, NO crear la tarea.
3. Si devuelve `allowed: true`, crear la tarea en `triage`.
4. Registrar la propuesta en `objective-proposals.jsonl`.

Las guardrails existentes (G1-G6) del task creator siguen activas y se
aplican ANTES de las nuevas (GR1-GR11). Las nuevas son específicas para
la propuesta de objetivos (OBJ-16), no para la creación de tareas
para objetivos existentes.

---

## 5. Paths permitidos y prohibidos

### Paths permitidos (el governor puede proponer touching)

| Path | Guardabroil | Notas |
|------|------------|-------|
| `REPO/**` | GR2 | Repo del plugin |
| `~/.hermes/**` | GR3 | Config, scripts, skills, etc. |
| `~/.hermes/profiles/pr-ollama/**` | GR3 | Perfil activo |
| `~/.hermes/profiles/pr-ollama/scripts/**` | GR3 | Scripts del cron |
| `~/.hermes/profiles/pr-ollama/docs/**` | GR3 | Documentación |

### Paths prohibidos (el governor NUNCA puede proponer touching)

| Path | Guardabroil | Notas |
|------|------------|-------|
| `~/.env` | GR4 | Credenciales del sistema |
| `~/.ssh/config` | GR4 | Config SSH |
| `~/.hermes/profiles/*/config.yaml` | GR7 | Requiere aprobación humana |
| `~/git/<other-repo>/**` | GR10 | Otros repos |
| `/etc/**` | GR4, GR11 | Sistema |
| `/var/**` | GR4, GR11 | Sistema |
| `/proc/**` | GR4, GR11 | Sistema |
| `/sys/**` | GR4, GR11 | Sistema |
| `/usr/**` | GR11 | Sistema |
| `/tmp/**` | GR11 | Temporal (no persistencia) |

### Paths que requieren aprobación humana

| Path/patrón | Guardabroil | Razón |
|-------------|------------|-------|
| `config.yaml` / `config.yml` | GR7 | Config central de Hermes |
| Cualquier mención de "API key", "credential", etc. | GR5 | Credenciales |
| Cualquier `pip install`, `apt install`, etc. | GR9 | Instalación de paquetes |

---

## 6. Script: validate-guardrails.py

**Ubicación:** `~/.hermes/scripts/validate-guardrails.py`
**Sintaxis:**

```bash
# Validar una propuesta de objetivo
python3 validate-guardrails.py \
  --title "OBJ-20: Optimizar health checks" \
  --body "Refactorizar health_checks.py para reducir falsos positivos..." \
  --kanban-db ~/.hermes/kanban.db \
  --state-file ~/.hermes/quota-governor/objective-proposals.jsonl

# Output (JSON en stdout):
# {
#   "allowed": true,
#   "violations": [],
#   "warnings": [],
#   "requires_human_approval": false
# }
```

**Opciones:**
- `--title`: Título del objetivo propuesto (requerido)
- `--body`: Body/descripción del objetivo (requerido)
- `--kanban-db`: Path a kanban.db (default: `~/.hermes/kanban.db`)
- `--state-file`: Path al registro de propuestas (default: `~/.hermes/quota-governor/objective-proposals.jsonl`)
- `--json`: Output JSON (default)
- `--quiet`: Solo exit code (0=allowed, 1=rejected)

---

## 7. Criterio de Completitud

- [x] Documento de guardarrailes diseñado (este documento)
- [x] `validate-guardrails.py` implementado con los 11 checks
- [x] Tests unitarios para cada check
- [x] Prompt del task creator actualizado con instrucciones de validación
- [x] Verificación de que los guardarrailes se respetan (tests pasando)