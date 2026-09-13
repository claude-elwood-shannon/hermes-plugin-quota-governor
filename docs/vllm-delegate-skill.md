# vllm-delegate

## Qué es

`vllm-invoke.py` delega subtasks concretas al modelo LOCAL de la GPU
(vLLM en `http://192.168.1.32:8000/v1/chat/completions`, LAN, $0.00/token).
El worker cloud la ejecuta con su toolset `terminal` existente. No es MCP
ni skill de ejecución: es un script + estas reglas.

**El vLLM es una optimización de coste, no una dependencia.** Si está
caído, haces la subtask tú mismo (fail-open). El sistema nunca espera al
GPU para completar una tarea.

## Uso

```bash
# extracción/clasificación con criterio EXPLÍCITO en el prompt (crítico):
~/.hermes/scripts/vllm-invoke.py \
  --prompt 'Clasifica segun este criterio EXACTO: 2xx=OK, 5xx=SERVER_ERROR. Linea: "GET /health 200 120ms". Responde SOLO la etiqueta.' \
  --json --task-id $HERMES_TASK_ID --hint

# resumen acotado:
~/.hermes/scripts/vllm-invoke.py --prompt-file /tmp/doc.txt \
  --max-tokens 2048 --task-id $HERMES_TASK_ID
```

Parámetros: `--prompt` / `--prompt-file` / stdin, `--model` (omitir =
el servido), `--max-tokens` (1024), `--temperature` (0.0, determinismo),
`--timeout` (30s), `--json` (fuerza + valida JSON), `--task-id` (auditoría),
`--hint` (la llamada nace de un `[vllm-hint: ...]` del body).

## Cuándo delegar (evidencia del maratón R1-R6)

| Subtask | ¿Delegar? | Evidencia |
|---|---|---|
| Clasificar texto en 3-5 categorías, criterio explícito | SÍ | R1: 10/10 JSON estricto |
| Extraer campos de un log/documento | SÍ | R3: 0.98 precisión, 50/50 requestId |
| Resumir un documento largo en N líneas | SÍ | R2: 5/5, claims fieles |
| Drafting de changelog técnico corto | SÍ | R4: 21/21 hashes reales, 0 fabricados |
| Razonamiento multi-paso | NO | un 7B no razona bien |
| Decisión con estado temporal (era vs es) | NO | R5/R6: no integra evidencia temporal |
| Escribir código | NO | deepseek-coder-6.7b peor que cloud |
| Nada que necesite >32K contexto | NO | límite de hardware |

**Regla de oro del prompt:** el criterio de clasificación va EN el prompt,
completo y exacto. Sin criterio explícito el 7B inventa (probe real 13-sep:
un "200 OK" sin criterio lo clasificó "error"; con criterio, "OK"). Y el
worker SIEMPRE valida lo crítico (hashes, IDs, números) — la respuesta del
modelo local no es verdad, es propuesta.

## Fallos (fail-open, códigos de salida)

- exit 1 `vLLM unreachable` → haces la subtask tú mismo. No reintentes
  más de una vez en la misma tarea.
- exit 2 `model not served` → reintenta SIN `--model` (usa el servido);
  si vuelve a fallar, hazlo tú.
- exit 3 `invalid response` → la respuesta no es fiable; hazlo tú.
- exit 4 otro error → hazlo tú.

## Pistas `[vllm-hint: ...]` en el body de las tareas

Formato: `[vllm-hint: <acción> <objeto>]`. Son SUGERENCIAS del sistema
(cuando sabe que hay una subtask delegable): si las sigues, invoca
`vllm-invoke` con `--hint` (queda registrado en `vllm-invoke.jsonl` y el
efficiency ratio mide la adopción); si no las sigues o juzgas que el
modelo local no encaja, hazlo tú y no pasa nada.

## Restricciones

1. Solo LAN: sin egress, sin API keys, sin credenciales.
2. DIRECCION-STOP: puedes seguir invocando (herramienta local, no gasta
   presupuesto cloud); no se crean tareas nuevas.
3. No toques el servicio vLLM ni el systemd del GPU host.
4. Zero tokens cloud: la delegación no consume tu cuota.

## Mantenimiento (hardening t_971bb19e + t_ef2376bd)

- Tres copias: viva `~/.hermes/scripts/`, perfil
  `~/.hermes/profiles/pr-ollama/scripts/`, repo `scripts/` (source of
  truth). Suite: `test_vllm_invoke.py` junto al script y en `tests/`;
  correr con `~/.hermes/venvs/pytest-312/bin/python -m pytest
  ~/.hermes/scripts/test_vllm_invoke.py -q`. Todo cambio al script pasa
  la suite ANTES de desplegarse y se backporta al repo (exit codes 0-4,
  esquema jsonl y flags congelados).
- argparse sale con exit 2 en error de uso (mismo código que
  model-not-served): scriptar siempre con prompt explícito.
