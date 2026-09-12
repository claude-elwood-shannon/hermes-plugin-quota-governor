# vLLM rounds history — OBJ-43 (ciclo kanban r1..r6)

Registro vivo de las rondas r1..r6 del servicio perpetuo OBJ-41
(`vllm-rounds.timer`, cada 30 min en ml-host) y, desde el 12-sep, de la
auditoria del ciclo kanban (sucesora de t_f4c24cca).

Fuente de verdad: `/data/ml/data/hermes/logs/rounds.jsonl` (ml-host).
Este doc se actualiza con append por cada ronda kanban cerrada.

## Baseline 2026-09-12 (pre-ciclo-kanban, servicio OBJ-41 solo)

Agregado real de `rounds.jsonl` (59 lineas, desde despliegue OBJ-41 del 11-sep):

- Estado: 52 ok, 7 error (VLLM_DOWN durante ventanas de boot/rollback Phi-3
  pre-fix; cero errores desde el OBJ-43-FIX de hoy).
- Rondas por tarea: r1 8, r2 11, r3 9, r4 9, r5 8, r6 8 (+1 legacy `r1`, 5 `none` = probes de servidor caido).
- Modelos comparados (tok/s promedio en rondas ok):

| Modelo | n | latencia media | tok/s medio |
|---|---|---|---|
| Meta-Llama-3.1-8B-Instruct-AWQ-INT4 | 15 | 11.9s | 33.9 |
| Qwen2.5-7B-Instruct-FP8 | 14 | 11.4s | 27.4 |
| deepseek-coder-6.7b-instruct-FP8 | 23 | 27.1s | 23.1 |

- Termica: 41-56C en toda la historia; cero throttles (>=75C) registrados.
- Estado del servidor al cierre del baseline: Qwen2.5-7B-FP8 @ 32768
  (rotacion manual del operador en curso; el ciclo kanban solo audita sobre
  modelos honestos >=64K: Llama-AWQ @ 65536, unico en el registry que pasa
  el gate).

## Ciclo kanban (auditoria r1..r6 con control desde el board)

_(vacio — se llena con append al cerrar cada ronda kanban: fecha, ronda,
modelo servido, temp antes/despues, resultado, task id)_