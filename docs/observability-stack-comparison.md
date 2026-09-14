# Stack de observabilidad: Prometheus+Elasticsearch+Grafana vs OpenObserve

t_74f5f315 (MEDIATOR 2026-09-14). Ambos desplegados en el services host
(192.168.1.23, capacidad sysadmin-lan) via docker-compose. Las cifras de RAM
son mediciones reales (`docker stats --no-stream`) tomadas el 2026-09-14 con
el stack recién arrancado y scrapeando; no son cifras de folleto.

## Estado desplegado

| Servicio | URL | Imagen | RAM medida |
|---|---|---|---|
| Prometheus | http://192.168.1.23:9090 | prom/prometheus:latest | 35 MiB |
| Elasticsearch | http://192.168.1.23:9200 | elastic 8.15.0 (single-node) | 918 MiB |
| Grafana | http://192.168.1.23:3000 | grafana/grafana:latest | 283 MiB |
| OpenObserve | http://192.168.1.23:5080 | public.ecr.aws/zinclabs/openobserve:latest | 112 MiB |

- Compose: `~/git/docker-compose/observability/` (stack completo) y
  `~/git/docker-compose/openobserve/` (alternativa). Todo el estado en
  volúmenes docker (Docker Root = /data/docker, 159G libres; el disco raíz
  del host está al 91% y queda fuera del stack).
- Prometheus scrapea: hermes-bridge (192.168.1.57:9120 `/metrics-prometheus`,
  30s), vLLM gpu-host (192.168.1.32:8000 `/metrics`, 60s) y a sí mismo.
- Grafana: datasources Prometheus (default) y Elasticsearch provisionados;
  5 dashboards provisionados (Board Overview, Budget & Spending, GPU Health,
  Cron Health, Supply).
- OpenObserve: credenciales root en `~/git/docker-compose/openobserve/.env`
  (chmod 600, NO versionado; OpenObserve exige password fuerte y rechazó la
  de la spec original).
- ES: `discovery.type=single-node` = modo desarrollo, sin bootstrap checks
  (vm.max_map_count del host es 65530 y no hay sudo para subirlo). NO usar
  `es.enforce.bootstrap.checks` — ese setting no existe y tumba el arranque.

## Comparación (tabla del mediador + medición propia)

| Aspecto | Prometheus + Elasticsearch + Grafana | OpenObserve |
|---|---|---|
| Despliegue | 3 servicios separados | 1 solo contenedor |
| Recursos (medido aquí) | ~1.23 GiB RAM (35+918+283) | ~112 MiB RAM (~11x menos) |
| Métricas | Prometheus (pull/scrape) | API compatible Prometheus (pull + push) |
| Logs | Elasticsearch (indexing, search) | API compatible Elasticsearch |
| Traces | Jaeger/Tempo (separado) | OTLP nativo (incluido) |
| Dashboards | Grafana (separado, muy potente) | Integrados (más simples) |
| Persistencia | 3 volúmenes separados | 1 volumen |
| Aprendizaje | Industria estándar, transferible | Moderno, Rust, comunidad creciente |
| Licencia | Open source (Apache 2.0) | Open source (AGPL-3.0 en praktik; revisar antes de uso comercial) |
| Escala | Probado a escala masiva | Más nuevo, menos probado a escala |

## Ventajas de OpenObserve (confirmadas en nuestro despliegue)

- Un servicio y un volumen: gestión mínima (compose de 20 líneas).
- ~112 MiB reales vs ~1.23 GiB del stack completo: ~11x menos RAM medida
  en este host (la spec estimaba 10x; la medición la confirma).
- Logs + métricas + traces unificados en una sola UI, con APIs que hablan
  Prometheus y Elasticsearch — si algún día OpenObserve fuera el único,
  el bridge no necesita cambios (mismo `/metrics-prometheus` scrapeado).
- Sin JVM: arranque en segundos y footprint plano.

## Ventajas del stack tradicional (por qué lo mantenemos como principal)

- Estándar de la industria: documentación, plugins y empleo transferibles —
  es el motivo de aprendizaje declarado por el usuario.
- Grafana es muy superior para dashboards complejos (ya tenemos 5
  provisionados as-code).
- Prometheus pull + alertmanager + ecosistema (exporters para todo) probado
  a escala masiva.
- Elasticsearch: búsqueda de texto completo madura; OpenObserve es más
  joven en ese terreno.

## Recomendación operativa

Convivencia: el stack tradicional como sistema principal (aprendizaje +
dashboards), OpenObserve corriendo como alternativa de evaluación. El
bridge es agnóstico: expone `/metrics-prometheus` y no empuja a ningún
servicio — añadir OpenObserve como segundo scraper del mismo endpoint es
una línea de config, sin tocar el bridge (zero dependencies respetada).

## Pendiente conocido (fuera del alcance de t_74f5f315)

- Elasticsearch no recibe logs todavía (no hay shipper configurado); el
  datasource quedó provisionado para cuando haya índices.
- `vm.max_map_count` del host sin subir (no hay sudo): ES funciona en modo
  desarrollo; para producción real habría que pedir elevación al usuario.
- Disco raíz del host al 91%: vigilado, pero la causa no es este stack
  (Docker Root ya vive en /data).
