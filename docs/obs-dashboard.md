# Obs Dashboard — OBJ-32 v0

Una página HTML consultable en el navegador: la casa entera en una
pantalla. Es la recomendación técnica de OBJ-32 tras evaluar Grafana:
NO el stack (server + datasource + provisioning para un host de un solo
inquilino es una granja que contradice la portabilidad), SÍ su espíritu
— un panel consultable. v0 = generador estático, familia de
morning-screen.

## Uso

```bash
# página estática (por defecto junto al trace: obs/dashboard.html)
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-dashboard.py

# página en vivo: regenera en cada request, Ctrl-C y fuera
HERMES_HOME=~/.hermes/profiles/pr-ollama python3 scripts/obs/obs-dashboard.py --serve
# → http://127.0.0.1:8734   (otro puerto: --serve 9000)
```

Sin installs, sin JS, sin CDN, sin dependencias: stdlib de Python. El
HTML también funciona abierto como fichero (`file://`). `--serve` hace
bind SOLO en 127.0.0.1 (privacidad: nada sale del host).

## Qué muestra

1. **KPIs** — gasto real acumulado (trace), hueco sin etiqueta %,
   done 24h, supply_ratio 24h + saldo NanoGPT con su nivel de
   presupuesto (metrics-history, OBJ-29).
2. **Gasto por clase de consumo** — tabla con líneas/gasto/%/barra y
   sparkline de gasto $/día (14 días, inline SVG).
3. **Gasto por objetivo** — top 8; `unattributed` siempre visible en
   rojo con su marca `← sin etiqueta (hueco)`: el hueco se muestra, no
   se esconde.
4. **Forecast + veredicto (gate)** — % de cuota por provider con barra
   y badge del veredicto: `OK` / `REDUCIR` (max_workers=1) /
   `BOARD OFF` / `SIN PROYECCIÓN`; reset semanal con horas restantes.
5. **Board** — recuento por estado, done 24h (con hora) y tareas
   activas.
6. **Alertas** — las mismas alarmas F2 de morning-screen; solo cuando
   hay anomalías (`sin incidencias` si todo va bien, silencio si no hay
   fuentes: patrón watchdog).

## Contrato de fuentes (idéntico a morning-screen)

- `quota-governor/obs/trace.jsonl` — OBJ-27 F0 (consumo canónico)
- `quota-governor/forecast.json` — OBJ-24 F2 (forecast EMA burn)
- `quota-governor/metrics-history.jsonl` — OBJ-29 (supply_ratio, saldo)
- `kanban.db` (root o profile home) — estado del board

Rutas resueltas por `get_hermes_home()` (`HERMES_HOME` o `~/.hermes`).
Solo lectura: el dashboard nunca escribe en las fuentes. El trace vive
bajo el profile home que lo colecta: fija `HERMES_HOME` igual que hace
el cron de morning-screen.

## Fuente única de verdad

El módulo IMPORTA morning-screen (lectores, umbrales F2, regla del
veredicto del gate) en vez de duplicarlos: si mañana cambia un umbral,
cambia en los dos sitios a la vez.

## Archivos

- `scripts/obs/obs-dashboard.py` — generador + `--serve`
- `scripts/obs/test_obs_dashboard.py` — 16 tests, fixtures, sin red,
  `/usr/bin/python3.12`

## Roadmap (decidido en OBJ-32)

- **v1** (solo si se quiere refresco real sin `--serve`): endpoint OTLP
  opcional + página que consulta el JSONL por fetch local. Sin
  Prometheus.
- **v2** (solo casa multi-host): AHI sí, Grafana/SigNoz — la herramienta
  aparece cuando el problema la justifica.
