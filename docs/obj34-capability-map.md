# OBJ-34 — Mapa de capacidades y fronteras de la casa

> **Qué es:** mapa priorizado de lo que la casa puede hacer hoy, dónde está
> la frontera exacta de cada área, y qué pieza de crecimiento comprar primero
> con presupuesto declarado. **Estado: mapa, no builds** — nada instalado,
> nada pedido, nada activado. La decisión final es de Andrew.
>
> **Fecha:** 10-sep-2026 · **Fuente de cada dato:** probes reales de hoy
> (método en §7). **Modelo del mapa:** todo lo que entra, entra con contrato:
> coste declarado, criterio verificable, revocable — el mismo formato que el
> resto del fondo.

---

## Resumen ejecutivo (30 segundos)

| # | Pieza candidata | Área | Coste | Riesgo | Clase | Veredicto |
|---|---|---|---|---|---|---|
| 1 | **Visión para QA visual del dashboard (OBJ-32)** | Físicas | **$0** | Bajo | C | ✅ **RECOMENDADA** |
| 2 | QA visual E2E con screenshots (browser+visión) | Físicas | $0 | Bajo | C | Misma pieza, vista pipeline |
| 3 | Visión por HTTP (payload directo a proveedor de visión) | Físicas | ~$0.01/req | Bajo | A | Alternativa si el nativo queda corto |
| 4 | Cierre de OBJ-27 F4b | Observ. | ~$0.10 | Bajo | C | Ya en marcha (tarea hermana) |
| 5 | Rama de memoria por-dominio | Memoria | $0 | Bajo | C | Espera formato de OBJ-31 |
| 6 | Higiene del hueco (etiquetado en el trace) | Observ. | ~$0.05 | Bajo | C | Barato, alto valor |
| 7 | MCP server propio del trace | Nuevos | $0 | Bajo | C | Buen segundo |
| 8 | Proxy OpenAI-compat local | Nuevos | $0 | Medio | A | Espera cliente |
| 9 | ACP a IDEs | Nuevos | $0 | Medio | A | Espera cliente |
| 10 | Bot multi-plataforma vía gateway | Nuevos | $0 | Medio | A | Espera elección |
| 11 | Computer-use (Xorg :10 real) | Físicas | $0 | **Medio-alto** | A | **No recomendar aún** |
| 12 | Voice-in (STT) / TTS en gateway | Físicas | $0 | Medio | A | Tras visión |
| 14 | Salud de memoria en el dashboard | Memoria | ~$0.02 | Bajo | C | Tras OBJ-31 |

**Recomendación:** pieza 1 — activar visión (tool nativo, $0) y su primer
caso de uso: QA visual del dashboard OBJ-32, con contrato cerrado en §6.

---

## 0. Cómo leer este mapa

- **Clase C** = ejecutable hoy con tools nativos del perfil, sin installs ni
  creds nuevas → no requiere aprobación por pieza (aplica el marco OBJ-30).
- **Clase A-adyacente** = requiere install, credencial nueva o tocar config →
  **aprobación por pieza** (GR5/GR7/GR9 ya lo exigen formalmente).
- **Coste** = USD de saldo NanoGPT estimado por ciclo de uso; las piezas $0
  consumen cuota de ventana (tokens %), no saldo. La cuota es el recurso
  escaso real (100.05% semanal usada, reset lun 14-sep 00:00Z).
- **Revocable** = cómo se apaga en una línea, sin dejar estado huérfano.
- Los números del fondo actual, medidos hoy: saldo NanoGPT **$27.50**,
  ventana OBJ-30b: gastado **$3.44 de $5.00** (warn 50% cruzado → estado
  `warn`, la casa funciona pero sin holgura de ventana), forecast gate
  `OK` (53.1% de cuota, ETA_90 en ~70h), 23 cron activos (de 24), board:
  4 running / 1 ready / 11 triage / 1 blocked / 10 done / 364 archived.

---

## 1. Razonamiento largo (lo que ya funciona)

**Estado actual — verificado hoy:**
- **Delegación a subagentes** (delegate_task): aislamiento de contexto real,
  en uso (p.ej. OBJ-33 corriendo como tarea hermana).
- **Kanban como memoria de trabajo**: 385 tareas en el board, 364 archived;
  los OBJ funcionan como contenedores de objetivo con handoff estructurado
  (summary/metadata), dependencias parent→child y memoria entre runs.
- **Skills**: 16 en el perfil, 14 MB — memoria procedimental que carga solo
  cuando aplica.
- **Herramienta base**: 52 subcomandos (`hermes --help`), incluidos
  `mcp`, `acp`, `proxy`, `serve` — senderos ya expuestos, sin estrenar.

**Frontera concreta:** el contexto de un solo turno. Todo lo que cabe en un
turno + skills + board funciona; lo que se necesita *entre* turnos y no vive
en skills/board/memoria, se evapora. La memoria persistente de perfil es hoy
**2.0 KB MEMORY + 1.3 KB USER** (~3.3 KB en total) — la menor de toda la
casa; OBJ-31 es la tarea madre de esta frontera.

**Piezas candidatas:**
- **(5) Rama de memoria por-dominio** (C): una skill `references/` por
  dominio (quota, obs, privacy) cargable on-demand. Habilita: contexto entre
  sesiones sin hinchar el turno. Coste: $0 (tokens de escritura).
  Verificación: la skill carga y responde a una pregunta de dominio.
  Revocable: borrar el dir. **Dependencia: OBJ-31 fija el formato** —
  no empezarla antes.
- **(6) Higiene del hueco** (C): el trace dice que el **100% del gasto
  con coste ($17.69) es `unattributed`** — 1,040 de 1,419 líneas sin tag
  `objective:`. El dashboard lo muestra en rojo (correcto: el hueco se
  muestra, no se esconde), pero sin etiquetas no hay presupuesto por
  objetivo posible (OBJ-28 necesita esto). Coste ~$0.05. Verificación:
  % del hueco < 20% en el dashboard (su propio umbral). Revocable: quitar
  el tag del creator. **Dependencia: OBJ-27 F0/F4b estabilizadas.**

## 2. Capacidades físicas

**Estado actual — verificado hoy:**
- **Browser** (browser_exec, Chromium headless via CDP): funcional —
  navega file:// y http, DOM legible, screenshots.
- **Visión** (vision_analyze): **funcional**. Probe E2E real: un PNG
  sintético (114 bytes) generado en workspace → respuesta correcta (bloque
  rojo, área estimada 30-35%, real 31%). Sin install ni cred nueva.
- **TTS** (text_to_speech, provider edge): **funcional**, gratis, sin
  cred — mp3 en cache de audio del perfil.
- **Computer-use** (cua-driver): tool **presente** en el catálogo;
  **Xorg real en :10** (xrdp) — hardware presente, tool no estrenada.
- **STT/voz-in**: sin canal hoy (gateway sin wiring de audio-in).

**Frontera concreta:** la casa ve y habla (probes verificados), pero
ninguna de las dos capacidades tiene *primer caso de uso real* integrado a
un flujo de la casa. Computer-use existe pero sin contrato de riesgo.

**Piezas candidatas:**
- **(1) Visión para QA visual del dashboard (OBJ-32)** — evaluada en
  detalle en §6. Recomendada.
- **(2) QA visual E2E con screenshots** (C): pipeline completo
  browser→screenshot→vision_analyze, probado hoy en sesión aislada
  (`obj34-iso`): render file:// del dashboard (1265×1103 px) → captura →
  análisis correcto (título, secciones, defectos). Coste $0. Habilita:
  que OBJ-32 v1 (HTML consultable) tenga QA visual periódico y barato sin
  tocar la base. Verificación: un defecto sembrado en un fixture HTML se
  detecta en el análisis. Revocable: borrar la skill.
- **(3) Visión por HTTP** (A-adyacente: toca config → GR7): payload
  imagen→proveedor de visión, ~$0.01/req, ~$0.10-0.30/ventana. Solo si el
  tool nativo queda corto. Revocable: revertir la línea de config.
- **(11) Computer-use** (A): tool presente + Xorg :10 real. Habilita QA de
  apps nativas, automatización de GUI. Riesgo medio-alto (es la pieza con
  más poder de acción física sobre el host; y `xrdp` = superficie de
  acceso remoto). Contrato mínimo: sesión `--print` y dry-run primero,
  never-login, sandboxed session, revocable = no invocar. **No
  recomendar aún**: es la única pieza del mapa que merece su propia
  evaluación de riesgo antes de un primer caso de uso. Post-visión.
- **(12) Voice-in/TTS en gateway** (A): STT a gateway (WhatsApp/Telegram
  de la casa) → respuestas habladas. Coste $0-0.05. Riesgo: medio (superficie
  de mensajería). Verificación: un audio de prueba → transcripción en el log
  del gateway. Revocable: desactivar el cron/gateway wiring. Tras visión.

## 3. Memoria (con OBJ-31)

**Estado actual — verificado hoy:** memories/ = 2 archivos planos,
3.3 KB total, sin locking real (locks `.lock` presentes), sin rotación,
sin per-dominio. El dashboard mide casa (gasto/board/forecast) pero nada
mide la salud de la memoria.

**Frontera concreta:** sin curación, la memoria crece hasta golpear su
límite duro de chars (el prompt del sistema ya avisa "94% / 95% full" en
cada turno). Sin per-dominio, cada sesión paga contexto irrelevante.

**Piezas candidatas:**
- **(14) Salud de memoria en el dashboard** (C, tras OBJ-31): cuando
  OBJ-31 aterrice, un mapa de salud de memoria en el dashboard OBJ-32
  (bytes, % lleno, última rotación) es una pieza natural y barata:
  1 sección en el generador + 1 test. Coste ~$0.02. Verificación: sección
  visible con datos reales. Revocable: quitar la sección del generador.
- La **curación y rotación en sí** es OBJ-31 — no se duplica aquí, solo se
  referencia (out of scope de esta tarea).

## 4. Senderos no explorados

**Estado actual — verificado hoy:** `hermes --help` lista 52 subcomandos.
Del catálogo de tools diferidas del sistema, estos senderos existen y están
sin estrenar en la casa:

| Sendero | Qué habilita | Clase | Coste |
|---|---|---|---|
| **(7) MCP server propio** | El trace/board/skills de la casa consultables desde cualquier cliente MCP externo | C | $0 |
| **(8) Proxy OpenAI-compat** | Cualquier cliente OpenAI-SDK habla con los proveedores de la casa | A | $0 |
| **(9) ACP a IDEs** | Hermes como ACP server dentro de un IDE (Zed, etc.) | A | $0 |
| **(10) Bot multi-plataforma vía gateway** | WhatsApp/Slack/etc. de la casa con la misma casa detrás | A | $0 |

**Frontera concreta:** cuatro senderos expuestos, cero estrenados. Ninguno
tiene un cliente concreto hoy — son capacidad sin demanda, y la regla de la
casa es no acumular tools sin caso de uso. Se listan para que Andrew vea
el sendero completo; la recomendación es **no abrirlos** hasta que exista
el primer cliente real.

**Criterio de apertura:** existe un cliente concreto (IDE usado, bot que
se quiere, cliente MCP que se quiere) → se abre su pieza con contrato;
sin cliente, no se abre.

## 5. Cuidado — el contrato de cada pieza nueva

Toda pieza que entre al fondo hereda el formato de OBJ-30 (y GR5/GR7/GR9
ya lo exigen formalmente para A-adyacentes):

1. **Coste declarado** — USD de saldo estimado por ciclo + moneda
   (saldo vs cuota %).
2. **Criterio verificable** — una frase que un tercero puede comprobar
   (como los probes de hoy: un PNG sintético, un fixture HTML con defecto
   sembrado).
3. **Revocable** — una línea que lo apaga sin estado huérfano.

**Mecanismo:** las piezas se proponen en triage con estos tres campos en el
body; el creator las rechaza si faltan (misma mecánica GR5/GR9). El forecast
gate y burn watchdog siguen siendo los límites duros; una pieza $0 igual
paga cuota, así que *toda* pieza entra con presupuesto de cuota declarado.

---

## 6. Evaluación detallada: visión para QA visual del dashboard (OBJ-32)

**Candidata señalada por la tarea** como primer caso de prueba del marco.
**Evaluación con probes reales de hoy:**

- **Tool nativo funcional** — `vision_analyze` (perfil pr-ollama) responde
  correcto a un PNG sintético (bloque rojo: estimó 30-35% del área, real
  31%). Sin installs, sin creds nuevas.
- **Pipeline E2E probado** — browser aislado (sesión `obj34-iso`) →
  file://dashboard.html (1265×1103 px) → screenshot → análisis correcto:
  título, secciones (KPIs, gasto por clase, gasto por objetivo, forecast,
  board), defectos detectados. **El probe ya produjo 2 hallazgos reales de
  v0**: el título del panel "GASTO — POR CLASE DE CONSUMO" rompe feo en 3
  líneas; y "presupuesto:" sin valor visible junto al badge warn en la
  tarjeta de saldo. QA visual clásico — el pipeline funcionó a la primera.
- **Criterio verificable:** un defecto sembrado en un fixture HTML se
  detecta en el análisis.
- **Coste: $0** de saldo (cuota de tokens; sin calls pagados).
- **Privacidad: baja** — las capturas viven en tmp local
  (~/.config/browser-harness/tmp/) y van al proveedor de visión solo para
  analizar; el dashboard es localhost-only por diseño (bind 127.0.0.1) y
  el HTML no contiene credenciales (verificado en el probe: solo métricas).
- **Revocable:** la skill QA es un dir — `rm -r`; el tool nativo no se
  desinstala porque no se instaló nada.

**Veredicto:** primera pieza. $0, riesgo bajo, caso de uso concreto
(dashboard OBJ-32), pipeline ya verificado E2E y con 2 hallazgos reales el
primer día. La decisión final es de Andrew.

### 6b. Presupuesto propuesto

**Pieza 1 (visión QA dashboard):** $0 de saldo, ~1-2% de cuota semanal
(los probes de hoy costaron $0 y un ciclo QA completo ≈ 3-5 requests).
**Presupuesto propuesto para la ventana:** $0.50 de saldo como margen para
imprevistos + cap de cuota: si el ciclo QA supera 5 requests o $0.20,
STOP duro y reportar. Revocable: no re-agendar el ciclo QA (cron opcional),
sin estado huérfano.

---

## 7. Método (los probes de hoy, replicables)

Todos los datos de este doc salen de probes de hoy (10-sep), reproducibles:

- **Board/estado**: sqlite RO sobre `~/.hermes/kanban.db` (385 tareas:
  4 running / 1 ready / 11 triage / 1 blocked / 10 done / 364 archived).
- **Fondo**: `quota-governor/*.json` (budget-state, forecast, spending-limit)
  + `nanogpt-balance-ledger.jsonl` (saldo $27.50, ventana $3.44/$5.00).
- **Trace**: 1,419 líneas OBJ-27 F0 — 100% del gasto con coste es
  unattributed; clase cron-llm = $17.69 (100%); día más caro 09-sep $2.67.
- **Visión**: PNG sintético (114 B, stdlib) → análisis correcto (30-35%
  estimado vs 31% real).
- **Browser E2E**: dashboard file:// en sesión aislada → screenshot →
  análisis (2 defectos reales de v0 encontrados).
- **TTS**: provider edge, gratis, mp3 en audio-cache del perfil.
- **Xorg**: `pgrep Xorg` → :0 (lightdm) y :10 (xrdp) activos.
- **Herramienta base**: `hermes --help` (52 subcomandos); `mcp`, `acp`,
  `proxy`, `serve` expuestos sin estrenar.
- **Cron**: 24 jobs (23 activos + 1 disabled), en `cron/jobs.json`.

*Un doc de la casa: cada número de §7 se puede verificar con los mismos
probes.*
