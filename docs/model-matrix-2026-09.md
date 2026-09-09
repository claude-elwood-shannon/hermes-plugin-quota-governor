# Matriz necesidad → modelo — Sep 2026 (MULTI-PROV-10.4)

**Tareas fuente**: 10.1 catálogo/precios (t_9c61f54e), 10.2 probes de callability
(t_514c568e, micro-test t_154b29f2, migración worker t_ffd28494), 10.3 coste real
medido (t_f94f8ef2). Estado del gate: `scripts/quota-gate.py`
(`PRIVACY_CAPABILITIES`, `PROFILE_MODELS`, `PROFILE_WORKER_MODELS`,
`PRIVACY_PROVIDER_PREFERENCE`, `GENERAL_PROVIDER_PREFERENCE`).
**Estado**: BORRADOR para aprobación del usuario. La implementación (10.5) no
arranca sin veredicto explícito. No se ejecutó ninguna probe nueva: todos los
datos provienen de las mediciones 2026-09-07/08 ya registradas.

---

## 1. Reglas del gate que enmarcan la matriz

- `PRIVACY_CAPABILITIES` (no modificado en esta fase):
  - `public` → ollama-cloud, nanogpt, openrouter, opencode-go, custom
  - `sensitive` → ollama-cloud, nanogpt, custom (opencode-go EXCLUIDO:
    retención/entrenamiento no auditados)
  - `confidential` → solo custom/local (sin proveedor cloud aplicable)
- Cost tiers de worker: `micro/tiny/small` usan `PROFILE_WORKER_MODELS`;
  `medium+` puede usar `PROFILE_MODELS` (interactivo/calidad).
- Enrutado (OBJ-26, vigente): preference-first con nanogpt primero en general
  (`GENERAL_PROVIDER_PREFERENCE`); en `sensitive` manda
  `PRIVACY_PROVIDER_PREFERENCE` (nanogpt 0, ollama 1). Dentro de nanogpt:
  covered-first con presupuesto de saldo (5 USD/ventana,
  `nanogpt_max_balance_spend_usd`).
- OpenRouter tiene pin en el gate (`z-ai/glm-5.2:free`) pero NO fue catalogado
  ni probeado en 10.1–10.3 ⇒ fuera de la matriz hasta medirse.

## 2. Matriz necesidad × perfil

Campos: necesidad | perfil | modelo primario | precio publicado ($/1M in/out) |
coste real medido | probe OK | alternativa/fallback.
"Coste real medido" = USD por micro-llamada medida (10.3 §5.2) y, donde existe,
USD por llamada/tarea en carga real de worker (ledger 10.3 §5.3, sesgo conocido
+16 % ⇒ cota superior). Fuentes por fila: catálogo 10.1 (model-catalog-2026-09.md),
probes 10.2 (§4 de la misma fuente), coste 10.3 (§5).

### 2.1 Worker barato (`cost:micro/tiny/small`) — pines actuales del gate

| Necesidad | Perfil | Primario (pin gate) | Precio $/1M | Coste real medido | Probe OK | Alternativa / fallback |
|---|---|---|---|---|---|---|
| Worker barato | pr-opencode (opencode-go) | `qwen3.8-flash` | 0.15 / 0.47 | $0.000051/llamada micro; $0.0038–0.0087/llamada en tareas kanban reales (~$0.08–0.20/tarea) | sí (200, 1801 ms, cost=0, 10.2 §4.1) | `glm-5.3-flash` (0.15/0.50; $0.000027/llamada; 200 OK) |
| Worker barato | pr-ollama (ollama-cloud) | `deepseek-v4-flash:0731` | 0.22 / 0.66 off-peak; **peak ×2 12–18 UTC L-V** (0.44/1.32) | $0.000048/llamada micro (medida off-peak) | sí (200, 1058 ms, 10.2 §4.2) | `gpt-oss:20b` (0.07/0.30; $0.000028; 200 OK, sin peak); `glm-5.3-flash`@ollama ($0.000031) |
| Worker barato | pr-nanogpt (nanogpt) | `z-ai/glm-5.3-flash` | 0.075 / 0.25 | $0.000030/llamada micro, cubierto (costUsd=0) | sí (200 + cost=0, 10.2 §4.3; end-to-end en worker real: t_154b29f2 y migración verificada t_ffd28494) | `deepseek/deepseek-v4-flash` (0.14/0.28, cost=0); `qwen/qwen3.5-9b` (0.05/0.15, cost=0) |

Notas de fila:

- pr-opencode: `qwen3.8-flash` es el modelo del plan (siempre activo, sin
  toggle, $30 incluidos/mes). El pin actual lo asigna a worker y deja
  `glm-5.3-flash` como interactivo — inverso al borrador anterior de 10.4
  (t_bef3cbf0), cuya propuesta sigue pendiente de aprobación (§6).
- pr-ollama: el gate pinea `deepseek-v4-flash` (ID real en ollama-cloud:
  `deepseek-v4-flash:0731`). Su peak ×2 cae en horario laboral europeo
  (12–18 UTC L-V): en esa franja la alternativa `gpt-oss:20b` es 3× más barata
  y sin peak. Medición de coste hecha off-peak (10.3 §5.2).
- pr-nanogpt: worker cubierto estable (costUsd=0 en todas las llamadas
  medidas). Ventana real: 60 M tokens de ENTRADA/semana, medidor exacto;
  ~40–60 tareas Hermes/semana con prompts grandes. Cobertura verificada en
  tres capas (gate + perfil config + chat directo, t_ffd28494).

### 2.2 Interactivo / session (`PROFILE_MODELS` del gate)

| Necesidad | Perfil | Primario (pin gate) | Precio $/1M | Coste real medido | Probe OK | Alternativa |
|---|---|---|---|---|---|---|
| Interactivo/session | pr-opencode | `glm-5.3-flash` | 0.15 / 0.50 | $0.000027/llamada micro; ~$0.004/llamada en sesión real | sí (200, 5148 ms, cost=0, 10.2 §4.1) | `qwen3.8-flash` (0.15/0.47; $0.000051; 200 OK) |
| Interactivo/session | pr-ollama | `glm-5.2` | 1.40 / 4.40 (calibrado ledger: 1.18/3.72) | $0.000559/llamada micro; **$0.0426/llamada en sesiones reales de agente** (activity.cost: $4.978/117 req) | sí (200, 1582 ms, 10.2 §4.2) | `glm-5.3` (misma tarifa; $0.000273/llamada) |
| Interactivo/session | pr-nanogpt | `zai-org/glm-5.2` | 0.42 / 1.32 | $0.000019/llamada micro; **cobertura DINÁMICA**: cost>0 a las 03:24 y cost=0 a las 03:39 del 2026-09-07 (10.3 §5.1) | sí (200, 10.2 §4.3) | `z-ai/glm-5.3-flash` (cubierto estable, 4–5× más barato) |
| Interactivo/session | pr-openrouter | `z-ai/glm-5.2:free` | free tier | sin medir (fuera de 10.1–10.3) | sin probe | — |

Advertencias:

- glm-5.2 quedó desterrado de interactivo en pr-opencode tras quemar el 82 %
  de la ventana de 5 h en solitario ($9.84/$12); evidencia en el propio gate
  (comentario INTERACTIVE MODEL WARNING) y en la motivación del epic.
- `zai-org/glm-5.2` en nanogpt pertenece a la lista de suscripción pero cobra
  saldo de forma intermitente ("pertenecer a la lista" ≠ "cubierto"; el
  discriminador fiable es `x_nanogpt_pricing.costUsd`). Verificar antes de
 pinear sesiones largas; el borrador anterior propone sustituirlo (§6).

### 2.3 Calidad (`cost:medium+`: revisión, código complejo, diseño)

| Necesidad | Perfil | Primario | Precio $/1M | Coste real medido | Probe OK | Alternativa |
|---|---|---|---|---|---|---|
| Calidad medium+ | pr-opencode | `kimi-k2.7-code` | 0.95 / 4.00 | $0.000334/llamada | sí (200, 10.2 §4.1) | `qwen3.8-max` (2.00/6.00; $0.000584) |
| Calidad medium+ | pr-ollama | `kimi-k2.7-code` | 0.95 / 4.00 | $0.000321/llamada | sí (200, 10.2 §4.2) | `glm-5.3` (1.40/4.40; $0.000273); tope `kimi-k3` (3.00/15.00; $0.001672) |
| Calidad medium+ | pr-nanogpt | `zai-org/glm-5.2` (cobertura dinámica ⚠️) | 0.42 / 1.32 | $0.000019/llamada | sí (200, 10.2 §4.3) | sin segundo modelo de calidad verificado en nanogpt ⇒ degradar a ollama `glm-5.3` (sensitive-safe) |

### 2.4 Privacidad: público (`privacy:public`, default)

Sin restricción de proveedor: aplican las filas 2.1–2.3. Enrutado vigente
(OBJ-26): preference-first con nanogpt primero, ollama segundo, opencode tercero;
dentro de nanogpt, covered-first con presupuesto de saldo de 5 USD/ventana
(`nanogpt_max_balance_spend_usd`, implementado en OBJ-26/26a).

### 2.5 Privacidad: sensible (`privacy:sensitive` — opencode-go excluido por el gate)

| Necesidad | Perfil | Primario | Precio $/1M | Coste real medido | Probe OK | Alternativa |
|---|---|---|---|---|---|---|
| Worker sensible (preferencia 0) | pr-nanogpt | `z-ai/glm-5.3-flash` | 0.075 / 0.25 | $0.000030/llamada, cubierto | sí (200 + cost=0) | `deepseek/deepseek-v4-flash` (0.14/0.28, cost=0) |
| Worker sensible (preferencia 1) | pr-ollama | `gpt-oss:20b` | 0.07 / 0.30 | $0.000028/llamada | sí (200) | `glm-5.3-flash`@ollama ($0.000031) |
| Calidad sensible | pr-nanogpt | `zai-org/glm-5.2` | 0.42 / 1.32 | $0.000019/llamada (cobertura dinámica ⚠️) | sí (200) | ollama `glm-5.3` (1.40/4.40; $0.000273); verificar costUsd al pinear |
| Confidencial | custom/local únicamente | — | — | — | — | sin fallback cloud: el gate bloquea (correcto, no se toca) |

### 2.6 Exclusiones registradas (modelos catalogados que NO entran)

- opencode-go: familia deepseek-v4 completa (403 RegionError — solo China),
  minimax-m2.7 y gpt-5.6-luna (500), grok-4.6 (401), muse-spark-*-contributor
  (403 DataPolicy, entrenan a Meta), 7 IDs residuales sin precio publicado.
- nanogpt: `meta/muse-spark-1.3-contributor` (503 ×2), `qwen3.5-4b` (402 — no
  cubierto, drena saldo).
- ollama-cloud: sin exclusiones (19/19 callables); `nemotron-3-ultra` fuera de
  workers por latencia (11.9 s en probe).
- OpenRouter: sin datos 10.1–10.3 ⇒ fuera de la matriz.

## 3. Cuellos de ventana reales (deciden, no el precio de lista)

| Perfil | Ventana límite | Efecto sobre la matriz |
|---|---|---|
| pr-opencode | $12/5 h rolling + $30 sem + $60 mes | ~70–100 tareas worker/ventana con flash; el cuello es la ventana, no la tarifa |
| pr-nanogpt | 60 M tokens de ENTRADA/semana (soft cap: pasado el límite sigue sirviendo y quema saldo) | ~40–60 tareas Hermes/semana; cobertura por modelo verificable vía costUsd |
| pr-ollama | sesión 5 h + semanal (fracción sin USD por request) | ~137 llamadas grandes llenan la sesión; `activity.cost` retardado ~35 min — no apto para control en vivo |

## 4. Coste de la ventana actual (ledger calibrado + snapshot del governor)

- Precios calibrados contra consola (docs/calibration-2026-09-08.md, ventanas
  cerradas con error ±0.1 %): glm-5.2 1.1824/3.7162, glm-5.3-flash
  0.1571/0.5236, qwen3.8-flash 0.15/0.47 (in/out $/1M). El ledger sigue siendo
  cota superior (+16 %) hasta acumular más ventanas cerradas.
- Snapshot 2026-09-09 (observaciones del governor): nanogpt semanal 100.05 %
  (agotado ⇒ blend a saldo según OBJ-26, con presupuesto 5 USD/ventana),
  opencode-go rolling 7 % / semanal 98 % / mensual 69 %, ollama semanal 51.8 %.
  Lectura: esta semana el peso recayó en ollama y en el saldo de nanogpt —
  coherente con la matriz covered-first.

## 5. Lecciones de medición que condicionan la matriz

1. El coste real por micro-llamada lo gobierna la verbosidad, no la tarifa:
   `qwen3.7-plus` costó 22× más que `glm-5.3-flash` en la misma tarea trivial.
   Métrica de decisión: $/tarea-tipo medida, no $/1M.
2. "Pertenecer a la lista de suscripción" ≠ "cubierto" en nanogpt: el
   discriminador es el campo `cost`/`x_nanogpt_pricing.costUsd` de la respuesta
   (glm-5.2 osciló entre cobrar y cubrir en 20 minutos).
3. El campo `cost` de opencode-go vale "0" bajo suscripción: no sirve como
   contador; el coste se estima con tokens × precio de lista (ledger, sesgo
   +16 % conocido) y se calibra contra consola en ventanas cerradas.

## 6. Cambios propuestos NO aplicados (pendientes del veredicto del usuario)

Heredados del borrador anterior de 10.4 (t_bef3cbf0), que sigue en triage sin
aprobación. La matriz de este documento usa los pines VIGENTES del gate como
primarios; estas propuestas solo se implementarían en 10.5 con el OK:

1. Worker pr-ollama: `deepseek-v4-flash` → `gpt-oss:20b` (3× más barato y sin
   peak ×2 en horario laboral europeo).
2. Interactivo pr-nanogpt: `zai-org/glm-5.2` → `z-ai/glm-5.3-flash`
   (cubierto estable, 4–5× más barato).
3. Intercambio de roles en pr-opencode: `qwen3.8-flash` a interactivo (modelo
   del plan, $30 incluidos) y `glm-5.3-flash` a worker.
4. Ningún cambio en `PRIVACY_CAPABILITIES`.

## 7. Fuentes

| Dato | Fuente |
|---|---|
| Catálogo, precios, ventanas y cobertura por proveedor (10.1) | t_9c61f54e → model-catalog-2026-09.md (probes y fuentes oficiales accedidas 2026-09-07) |
| Callability por modelo (10.2) | t_514c568e → §4 de la misma fuente; t_154b29f2 (micro-test flash end-to-end) |
| Coste real medido (10.3) | t_f94f8ef2 → §5 de la misma fuente (140 peticiones, 50 modelos); fichas crudas en el workspace de esa tarea |
| Migración y verificación del worker de pr-nanogpt | t_ffd28494 (gate + perfil + chat directo; auditoría de 3 capas) |
| Pines y reglas vigentes | scripts/quota-gate.py: PRIVACY_CAPABILITIES, PROFILE_MODELS, PROFILE_WORKER_MODELS, PRIVACY_PROVIDER_PREFERENCE, GENERAL_PROVIDER_PREFERENCE |
| Precios calibrados del ledger | docs/calibration-2026-09-08.md + model-cost.json |
| Presupuesto de saldo y covered-first (OBJ-26/26a) | commits bb5e99b/9e3ff1c; observaciones del governor (request_balance_usd / request_covered_usd) |
