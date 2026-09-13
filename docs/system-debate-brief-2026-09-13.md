# Brief de debate: sistema de orquestación autónoma "quota-governor"

**Fecha:** 13 de septiembre de 2026
**Propósito:** documento de trabajo para debatir el diseño del sistema con otra IA. Contiene la arquitectura real, la historia de fallos verificada, las decisiones tomadas con su justificación, y las preguntas abiertas. Nada de marketing: incluye los fallos sin adornar porque de ellos salen las preguntas más interestinges.

**Contexto del propietario:** un humano solo, presupuesto acotado (suscripción Ollama Pro + NanoGPT con balance limitado + cuota gratuita de proveedores), que quiere que su sistema de agentes trabaje sin su presencia continua. Su máxima: "con cuota gratis o presupuesto aprobado, la casa no para" — y su crítica recurrente: cada vez que mira, el tablero está vacío. Esa tensión entre la constitución y la realidad es el corazón del debate.

---

## 1. Qué es el sistema

Un plugin de Hermes Agent (runtime de agentes en Python, perfiles por proveedor de LLM) que añade gobernanza de cuota y orquestación kanban:

- **Board kanban** con estados: triage → todo → ready → running → done/blocked/archived. Las tareas son unidades de trabajo con cuerpo estructurado (tags `objective:OBJ-NN | cost:tiny|small|medium | privacy:low | clase:C`).
- **Workers**: sesiones de agente (Hermes CLI) que reclaman tareas del board, trabajan con toolsets (terminal, kanban, memoria) y deben cerrar con `kanban_complete` o `kanban_block`. Protocolo verificable: claim → heartbeat → terminal call.
- **Perfiles por proveedor**: `pr-ollama` (Ollama cloud, suscripción Pro), `pr-nanogpt` (NanoGPT, suscripción + balance), `pr-openrouter` (cuota free), `pr-opencode` (OpenCode Go), `pr-vllm` (vLLM local en GPU 4060 Ti 16GB vía LAN).
- **Quota governor**: daemon + cron que vigila cuota de sesión/semanal, decide spawn/kill de workers, y aplica la constitución del vuelo.
- **Canales de supervisión**: tick cada 10 min (governor), watchdog cada 5 min (kanban-watchdog.sh), respawn de portal cada 5 min (obs-serve-cron.sh), cola-viva embebida en el tick.

**Hardware:** host principal (Linux, where Hermes vive) + host GPU remoto por SSH (4060 Ti 16GB, vLLM con Qwen2.5-7B-Instruct-FP8, rotación de modelos por cron cada 30 min con guardas térmicas 75/85°C).

---

## 2. La constitución del vuelo (ratificada 3 veces por el humano)

1. ¿Cuota gratis disponible? → **VUELA** (rellenar cola, spawnear workers).
2. ¿Sin cuota gratis pero con presupuesto aprobado (balance)? → **VUELA** (gastar con techo).
3. ¿Ni cuota gratis ni presupuesto? → **PARA** — y la frase de parada es un **diagnóstico ganado, no un recitado**: solo se emite tras verificar la realidad (probar cuota, revisar presupuestos). Declarar sequía sin verificación es la falta más grave del sistema.

**Enmienda de incidentes:** cualquier parada anómala es un incidente que hay que nombrar, explicar y reparar. "Cola seca legítima" con trabajo pendiente en bodies cerrados NO es sequía (hubo 28 ticks que la declararon con un maratón a medias — el sistema esclavizó al dueño).

---

## 3. Arquitectura de decisión (lo que funciona)

### 3.1 Cadena de despacho
```
gateway dispatcher (único, embebido en Hermes)
  → reclama ready tasks (assignee + modelo del gate)
  → spawn worker (perfil, modelo, toolsets)
  → worker: claim → heartbeat → trabajo → kanban_complete/block
tick (10 min): concurrencia + cola-viva + presupuesto
watchdog (5 min): clasifica bloqueadas, auto-unblock de transitorios
```

### 3.2 Pin de modelos por perfil (gate)
Cada perfil tiene un modelo de worker y uno interactivo. La matriz MULTI-PROV-10.5 los asignó por precio y capacidad. Regla crítica aprendida con dolor: **verificar el modelo en `/api/tags` (local) antes de pinearlo** — una sonda contra el catálogo cloud sugirió `gpt-oss:20b` para el host local, donde no existe: 4 workers murieron rc=0 sin cerrar.

### 3.3 Coste por objetivo (OBJ-28) y facturación exacta
- Cada tarea lleva tags de coste (`tiny`/`small`/`medium`); el objetivo (OBJ) agrega el gasto real.
- NanoGPT expone `x_nanogpt_pricing`: `costUsd=0` significa cubierto por suscripción; `costUsd>0` drena balance. Es la fuente de verdad para "covered-first" (elegir el modelo que no gasta).
- Presupuesto: balance = techo (5 EUR aprobados para balance NanoGPT); cuota = asignación. WARN al 50%, STOP duro al 100%.

### 3.4 Observabilidad propia ("hiperespacio")
- **Trace JSONL local** (soberanía del dato) con semconv OpenTelemetry (`gen_ai.request.model`, `gen_ai.usage.*`) — el estándar adoptado, no el producto.
- **OTLP exportador opcional** (`OBS_OTLP_ENDPOINT`) — la puerta de salida estándar: "la casa es compatible y conectable; cada uno conecta lo que quiera". Sin Prometheus/Grafana/Loki como dependencias (decisión de diseño, no de ignorancia).
- **Portal local** en :8917 (obs-serve.py, stdlib): gasto por objetivo, supply_ratio, forecast, drill-down a request. Respawn por cron cada 5 min.
- **Morning report 8:00** — el board cuenta su noche sin que nadie pregunte.

### 3.5 Cola-viva (OBJ-30b/44) — el mecanismo de la constitución
Cascade en cada tick (máx 1 acción, idempotente):
1. Asignar perfil a ready sin assignee (nadie reclama lo no asignado).
2. Ready con assignee → cola viva, no tocar.
3. Sucesor estructural de done <24h clase:C sin sucesor abierto (docs de lo no documentado, test de lo nuevo, hardening de lo frágil).
3.5 Sucesores de bodies con partes pendientes (OBJ-39-REBELION: las partes declaradas sin evidencia en tareas selladas generan sucesor propio).
4. Nada de nada → sequía legítima (regla de oro: no filler) — pero con triage ≥5, es INCIDENTE "esperando usuario", no veredicto.

Gates: board idle (live=0), sesión y semanal <80%, sin señal STOP, nunca tocar triage clase A/B (decisión humana).

---

## 4. La historia de fallos (verificada — el valor real del documento)

### 4.1 El doble dispatcher (11-sep)
Dos despachadores vivos a la vez (gateway embebido + daemon `--force`): se spawneaban mutuamente workers y se los mataban como "zombies" cada ~60s. 278 líneas de "reaped 1 zombie" en el log del gateway antes de que nadie lo mirara.
**Lección:** un solo escritor de estado. El guard dual-dispatcher ahora es parte del tick ("gateway dispatcher active — NOT respawning").

### 4.2 El pin del modelo fantasma (12-sep)
`gpt-oss:20b` pineado desde la matriz (verificada contra el catálogo cloud, no contra el host local). Cada worker nacía, intentaba cargar un modelo 404, moría rc=0 sin llamar `kanban_complete`. Llevábamos 3 días persiguiendo el patrón "protocol violation" como si fuera un problema de protocolo.
**Lección:** el patrón de fallo engañoso (rc=0 limpio) era un fallo de configuración. Y la corrección se hizo en el repo + test, no en la memoria del chat.

### 4.3 El toolset kanban ausente (12-sep)
El perfil pr-ollama no tenía `toolsets:` en su config.yaml. Los workers nacían sin la herramienta `kanban_complete` en la mano — incapaces de cerrar aunque hicieran el trabajo. Verificado con probe: 75 tool calls de trabajo real, cero cierre terminal.
**Lección:** el fix duradero va a config/código, nunca a parches de chat. Y "funciona técnicamente" ≠ "es el modelo correcto económicamente" (lección del incidente glm-5.2, ver 4.4).

### 4.4 El incidente glm-5.2 (12-sep)
Ante los crashes, se asignó glm-5.2 (modelo interactivo: $1.40/$4.40 por M tokens) a 7 tareas kanban. Consumió 22.8% de la sesión de 5h en 65 min (~4x lo normal). El trabajo se hizo y el costo monetario fue $0 (cubierto por suscripción Ollama) — pero el mismo error sobre balance NanoGPT habría costado $2-3 reales. La diferencia entre accidente inocuo y fuga fue la suerte del proveedor.
**Lección:** el perfil económico del modelo importa tanto como su capacidad técnica. La corrección del pin (deepseek-v4-flash:cloud, cubierto y probado en 30+ tareas) va al repo, no al chat.

### 4.5 La sequía fantasma (13-sep — el más profundo)
El tick de cola-viva declaraba "cola seca legítima" durante días. Causa raíz: resolvía el kanban.db al **perfil** (`profiles/pr-ollama/kanban.db` — no existe) en vez del **root** (`~/.hermes/kanban.db`). Leía un tablero vacío inventado y verdictaba sequía sobre una base de datos fantasma.
Además, dos reglas estrechas contribuían: solo `clase:C` exacta contaba como padre (la autoqueue genera `clase:C-estructural`), y un sucesor ya DONE bloqueaba al padre para siempre.
**Corregido (13-sep, commit 52c3ede):** fallback a root + C-estructural elegible + done no sella. **Lección:** cuando un sistema afirma un estado negativo ("no hay nada"), verificar primero contra QUÉ está afirmando.

### 4.6 El hard cap parricida (13-sep)
Al desbloquear 8 tareas a la vez, el tick contó live=7 con hard cap=3 (desired+2) y mató 4 workers **sanos y trabajando** a mitad de tarea. Luego, con la cola vacía por los kills, declaró sequía legítima.
**Corregido (A/B/C aprobados):** hard cap=6 en .env del perfil; sucesor real creado por el mecanismo solo; sequía con triage≥5 = incidente con nombre.
**Lección:** los límites de protección pueden ser el mayor depredador del sistema. Y la sequía necesita distinguir "no hay nada que hacer" de "hay trabajo pero espera palabra humana".

### 4.7 Los supervisores sin cron (13-sep)
`obs-serve-cron.sh` existía como supervisor perfecto (probe, respawn, PID file, silencio cuando sano) — pero **nadie lo había registrado en crontab**. El portal murió sin que nada lo notara durante horas.
**Lección:** un supervisor sin su entrada de cron es literatura, no infraestructura. Verificado al instante: agregado a crontab cada 5 min.

### 4.8 El bloqueo "user needed" (12-13 sep)
Las autoqueue generadas por el sistema se bloqueaban con "user needed to set task id" — esperando una interacción humana para un campo que el propio sistema generó. Con el dueño ausente: eternidad.
**Regla nueva:** el sistema no puede bloquear por decisiones que puede tomar solo. O decide, o pregunta directo — nunca esclaviza con trivia.

---

## 5. Las preguntas abiertas (lo que se quiere debatir)

### Sobre la constitución
- **Q1.** ¿Cómo se vería una formulación de la constitución que no dependa de la disciplina de un agente LLM (que olvida, parchea en el chat, se autoconvence)? ¿La expresarías como código puro? ¿Como contrato con tests?
- **Q2.** La frase de parada es un "diagnóstico ganado". ¿Cómo se implementa la "ganancia" del diagnóstico? (Nuestra respuesta actual: el veredicto de sequía debe traer adjunta la query verificada — pero ¿es suficiente?)
- **Q3.** ¿Es correcto que el sistema distinga entre "sequía legítima" y "esperando al usuario" solo con el umbral triage≥5? ¿Qué señal de calidad usarías?

### Sobre economía de agentes
- **Q4.** El "covered-first" (elegir modelos cubiertos por suscripción antes de tocar balance) es una política de dos niveles (gratis→presupuesto). ¿Cómo la expresarías como función de decisión con evidencia (x_nanogpt_pricing costUsd) sin acoplarte a un proveedor?
- **Q5.** Los costes por M token cambian (peak pricing nocturno x2 en algunos). ¿Convendría un planificador que conozca precios y elija el worker model más barato que cumpla la clase de tarea? ¿Cómo evitas el Goodhart (barato incorrecto)?
- **Q6.** La suma de presupuesto humano (5 EUR) es un "techo de confianza" — no un pronóstico. ¿Cómo diseñarías un forecast de gasto que el humano pueda creer sin verificarlo manualmente?

### Sobre autonomía y fronteras
- **Q7.** ¿Qué decisiones debe tomar el sistema solo, y cuáles exigen palabra humana? Nuestra frontera actual: deseos → triage (humano); construir → palabra expresa; todo lo mecánico → automático. ¿Dónde trazas tú la línea y por qué?
- **Q8.** El sistema generó "12 autoqueue duplicadas" sembrándose a sí mismo en bucle sin consumirse. ¿Cómo evitas la burocracia auto-organizada (agents creating work for agents) sin matar la cola viva?
- **Q9.** La columna In Progress con 1 worker y 8 bloqueadas — ¿es un fallo del despachador o del diseño de estados? ¿Simplificarías el board (¿qué estados sobran)?

### Sobre verificabilidad
- **Q10.** "Lo que no verifico, no lo afirmo" — pero el agente que verifica y el que afirma son el mismo. ¿Qué arquitectura de checks usarías (afirmación → evidencia adjunta obligatoria) para que un humano pueda confiar sin re-verificar?
- **Q11.** El morning report es la promesa de que "el board cuenta su noche sin que preguntes". ¿Qué métricas debe traer para que su silencio sea tan informativo como su contenido?
- **Q12.** Trazabilidad OTLP ya está (semconv, exportador opcional). ¿Qué añadirías para responder en una query: "¿por qué este worker murió esta noche?" — con la cadena completa claim→herramientas→cierre?

### Sobre el corazón humano del asunto
- **Q13.** El dueño del sistema agotó su creatividad vigilando un kanban de cartón — pasó de arquitecto a testigo obligatorio de la plomería. ¿Qué principios de diseño UX (no de features) devolverían al humano a su papel de arquitecto?
- **Q14.** ¿Qué señales medibles distinguirían "el sistema funciona sin mí" de "el sistema funciona mientras lo miro"? (Nuestro candidato: correlación entre presencia del dueño y actividad del board — ¿cuál sería el tuyo?)

---

## 6. Estado actual verificable (13-sep 16:00)

- **Commit de referencia:** `8f4870a` (main, pushed) — incluye el fix de la sequía fantasma y el incidente de triage.
- **Tests:** 13/13 en test_tick_cola_viva.
- **Board:** 1 running (sucesor estructural auto-creado), ~5 blocked (3 esperan palabra del usuario, 2 del sistema), 10 triage (deseos del dueño capturados).
- **Cuota:** sesión Ollama 1.5% (ventana fresca), semanal ~68% (resetea en 7d). Balance NanoGPT: 5 EUR de techo, intacto (covered-first funcionando).
- **Supervisión:** tick 10 min, watchdog 5 min, obs-serve 5 min — todos en crontab verificado.
- **Hiperespacio:** :8917 vivo (respawn verificado).
- **GPU:** vLLM systemd --user, Qwen2.5-7B-FP8, cron 30 min rotación, térmicas 42-56°C — ronda viva pero con utilización 0% la mayor parte del día (pregunta abierta: ¿qué trabajo real merece la GPU?).

---

## 7. Convenciones de discusión (para la otra IA)

- El dueño habla español; prefiere **evidencia antes que adjetivos** y respuestas cortas cuando está cansado.
- Toda afirmación de estado debe poder verificarse (comando, query, log). Los números sin fuente no valen.
- Las decisiones del dueño son dirección; las del sistema, ejecución. Confundirlas es la falta que más ha dolido.
- El producto real no es el plugin: es **la liberación del dueño** de la esclavitud de vigilar su propio sueño.
- Fuentes en el repo: `docs/audit-2026-09-12-board-desierta.md`, `docs/incident-2026-09-12-dual-dispatcher.md`, `docs/model-matrix-2026-09.md`, `scripts/tick-cola-viva.py` (la constitución como código), `scripts/quota-gate.py` (pines de modelos con su historia).
