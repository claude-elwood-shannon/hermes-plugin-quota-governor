# Test de modelos 14B en ml-host — 2026-09-14 (t_d89c4334)

Test estructurado de Qwen2.5-14B-Instruct-AWQ contra el baseline del host,
siguiendo el plan del mediador: pre-flight → switch → bench (3 prompts) →
soak 5 min → veredicto → restauración. Todo vía SSH (`hermesuser@192.168.1.32`),
una sesión SSH por fase, con backup de la unit antes de tocar nada.

Nota de alcance: el mediador habla del "modelo 7B actual"; el baseline real
servido en el host es **Llama-3.1-8B-Instruct-AWQ-INT4 @ 65536** (único modelo
honesto >=64K para el gate de Hermes). La comparación es 14B vs ese 8B.

## 1. Pre-flight (22:57 CEST)

| Métrica | Valor |
|---|---|
| Servicio vllm | active (systemd --user) |
| Modelo servido | hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4, max_model_len 65536 |
| VRAM | 14.618 / 16.380 MiB |
| Temperatura | 41 C |
| Timer vllm-rounds | active, cursor=6 (próxima ronda = r1, wrap con SWITCH) |
| Snapshot 14B en disco | Qwen/Qwen2.5-14B-Instruct-AWQ, 3 shards safetensors, 9.4 GB (descargada 16:38-16:53) |

Riesgo mitigado antes de empezar: el timer de rondas se **pausó** durante la
ventana de test (la ronda r1 del wrap habría hecho SWITCH de modelo a mitad de
prueba). Timer restaurado al final.

## 2. Baseline 8B (bench propio: 3 prompts, temp 0)

Prompts: extracción de 6 emails (117 tok in), clasificación de 5 líneas de log
(190 tok in), resumen en 3 líneas (215 tok in). Salida medida con usage del API.

| Tarea | tok/s | Latencia | Precisión |
|---|---|---|---|
| extract | 51,7 | 0,85 s | 6/6 emails exactos |
| classify | 46,6 | 0,43 s | 5/5 correctas |
| summarize | 52,2 | 1,63 s | Correcta pero 85 tok: añade preámbulo y no respeta las 3 líneas |

## 3. Switch a 14B y fallo de parser (lección)

Cambio: `--model Qwen/Qwen2.5-14B-Instruct-AWQ --max-model-len 16384` (el valor
del plan del mediador; sobra VRAM, ver §5). Boot OK en ~85 s.

**Primer intento falló al inferir**: HTTP 500 en el primer chat,
`Llama3JsonToolParser could not locate the bot token '<|python_tag|>' in the
tokenizer`. La unit heredó `--tool-call-parser llama3_json` (de Llama) y ese
parser exige un token que el tokenizer de Qwen2.5 no tiene: moría por petición,
no en el boot. Fix: `--tool-call-parser hermes` (el de la familia Qwen2.5) →
daemon-reload → restart → ready en 50 s.

Lección para el cambio de modelo: **el parser de tool-calls viaja con el MODELO,
no con la unit**. Al cambiar de familia Llama↔Qwen hay que cambiar el parser en
el mismo sed; si no, el servidor "arranca" pero sirve 500.

## 4. Bench 14B (mismo protocolo)

| Tarea | tok/s | Latencia | Precisión |
|---|---|---|---|
| extract | 24,0 | 1,83 s | 6/6 emails exactos |
| classify | 24,9 | 0,80 s | 5/5 correctas |
| summarize | 28,0 | 1,71 s | 48 tok: 3 líneas reales, sin preámbulo (mejor cumplimiento de formato que el 8B) |

## 5. Soak y termal (ventana interrumpida, ver §6)

SOAK-1/2 (repeticiones del bench completo): 28,9-30,2 tok/s, estables.
Monitor nvidia-smi cada 15 s: VRAM 14.314-14.396 MiB bajo carga, temp 41-58 C
(pico 58 C con util 100%), **cero throttles reales** (flag 0x0 bajo carga;
0x1 = GPU_IDLE, benigno en idle).

Capacidad de VRAM confirmada: a 16384 de contexto el 14B ocupa ~14,4/16,4 GB
(con gpu-memory-utilization 0.9). NO cabe a 32768 (KV cache 6,0 GiB > 4,35 GiB
libres → ValueError; límite estimado por vLLM ~23728; confirmado el mismo día
por t_27e6f8f8 con 22016 como máximo práctico).

## 6. INCIDENTE: colisión con el operador (21:08:19 UTC)

En pleno soak, el journal registra un stop/start de vllm que **no era mío**:
otro actor (sesión SSH interactiva viva desde 192.168.1.57, pts/8, abierta el
10-sep) reescribió la unit y restauró Llama-8B a mitad de mi ventana. Evidencia:

- journal 21:05:37Z: restart CON Description "(Qwen2.5-7B-FP8)" → es MI fix del parser (unit aún con Description vieja).
- journal 21:08:19Z: restart CON Description "(Llama-3.1-8B-AWQ)" → restauración del actor.
- SOAK-3/4: Connection refused (el restart del actor); SOAK-5..8 midieron contra el 8B ya restaurado (model field lo delata) → descartadas del test 14B.
- diff de la unit tras el incidente: ExecStart byte-idéntico al baseline (solo cambió la línea Description, ahora coherente con el modelo).
- El timer de rondas NO fue tocado por el actor (rounds-hot.log sin switches nuevos).

Misma IP que la colisión de las 17:37Z documentada en t_27e6f8f8. Decisión: NO
re-switchear — el actor dejó el baseline sirviendo y reintentar solo multiplica
la colisión. El test 14B ya tenía sus 3 prompts + SOAK-1/2 medidos.

## 7. Restauración final (verificada)

- Servicio: active, sirviendo Llama-3.1-8B-AWQ-INT4 @ 65536 (restaurado por el actor; ExecStart verificado byte-idéntico al backup tomado en pre-flight).
- Timer vllm-rounds: reactivado por mí (active) — lo había pausado para la ventana.
- GPU: 14.360 MiB / 43 C al cierre.

## 8. Veredicto

**¿Cabe el 14B en 16 GB?** SÍ, a `--max-model-len 16384` (14,4/16,4 GB, margen
~2 GB, térmica tranquila 41-58 C sin throttles). NO cabe a 32768.

**¿Rinde mejor que el 8B?** En este set de trabajo, NO compensa el cambio:

- Precisión: empate. Las tareas deterministas (extracción 6/6, clasificación 5/5)
  el 8B ya las clava; el 14B no aporta ventaja medible aquí.
- Velocidad: el 14B va ~2x más lento (24-30 tok/s vs 47-52 tok/s; latencia 0,85→1,83 s en extracción).
- Formato: única ventaja observada — el 14B respetó el "resume en 3 líneas"
  (48 tok) donde el 8B se extendió (85 tok con preámbulo).
- Gate Hermes: el 14B a 16384 JAMÁS puede ser worker dispatchado (gate >=64K
  honesto); el único candidato fijo sigue siendo Llama-8B-AWQ @ 65536.

Recomendación: mantener Llama-8B como modelo fijo. El 14B tiene hueco como
modelo bajo demanda (patrón switch/medir/restaurar de t_27e6f8f8) para tareas
que necesiten más razonamiento que el 8B, con parser `hermes` y ctx 16384-22016.
Si algún día se quiere 14B fijo a 32K, hace falta una GPU de 24 GB.

Candidato #2 (Mistral-Nemo-12B-AWQ): no probado — el plan lo reserva para el
caso de que el #1 no arrancara, y arrancó y completó bench.

## 9. Anexos (workspace t_d89c4334)

- `bench_model.py` — bench estandarizado (3 prompts, temp 0, tok/s por usage)
- `bench_14b.jsonl` — salida cruda del bench 14B
- `soak_14b.jsonl` — soak completo (incluye SOAK-5..8 contra el 8B restaurado)
- `monitor_14b.csv` — VRAM/temp/util/throttle cada 15 s
- `switch_to_14b.sh`, `fix_parser.sh`, `restore_and_forensics.sh` — fases
- `preflight.sh`, `host_monitor.sh`, `probe_500.py` — soporte

Los sha256 de todos los anexos están en el comentario REGISTRO de la tarea.
