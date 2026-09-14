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

## ACTUALIZACIÓN t_9e457672 — OpenObserve + Grafana como arquitectura principal

El mediador actualizó el diseño: OpenObserve es el backend unificado
(métricas+logs+traces) y Grafana el frontend; Elasticsearch queda como
aprendizaje legacy y Prometheus como scraper opcional con remote_write hacia
OpenObserve. Convergencia aplicada SIN destruir el despliegue de t_74f5f315
(backup previo en `backup_t_74f5f315_*` dentro del árbol compose).

### Estado final (verificado en vivo 2026-09-14/15)

| Pieza | Estado |
|---|---|
| OpenObserve :5080 | up; remote_write ingiriendo (12x HTTP 200/2min) |
| Grafana :3000 | grafana:9.5.21; plugin `openobserve` cargado; 4 datasources; 7 dashboards |
| Prometheus :9090 | up; scrapea bridge+vLLM y remote_write → OpenObserve (200) |
| Elasticsearch :9200 | up (aprendizaje legacy; el backend principal es OpenObserve) |
| Cadena E2E | Grafana → datasource OpenObserve → `hermes_bridge_up{instance="192.168.1.57:9120"}` con valor real |

Datasources de Grafana: `prometheus` (default, dashboards de t_74f5f315),
`prometheus-oo` (API PromQL de OpenObserve), `openobserve` (plugin) y
`elasticsearch`. Dashboards nuevos: **OpenObserve Ingest**
(`hermes-openobserve-ingest`) y **Bridge Prometheus (OpenObserve)**
(`hermes-bridge-openobserve`), ambos consultando OpenObserve.

### Hallazgos vivos (costaron depuración real — no repetir)

1. **El plugin ID del catalogo NO existe**: `openobserve-openobserve-datasource`
   da 404 en grafana.com (`GF_INSTALL_PLUGINS` habría fallado en silencio). El
   plugin oficial (`openobserve/openobserve-grafana-plugin`, id `openobserve`)
   NO está en el catálogo: se instala con el tarball S3 del propio proyecto
   (`https://zincsearch-releases.s3.us-west-2.amazonaws.com/zo_gp/zo_gp.tar.gz`)
   extraído en `./grafana/plugins/` y montado en `/var/lib/grafana/plugins`,
   más `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=openobserve` (no firmado).
2. **Versión de Grafana**: el plugin declara `grafanaDependency ^9.3.8` —
   `grafana:latest` (12.x) no lo cargaría. Pinea `grafana/grafana:9.5.21` con
   volumen NUEVO (`grafana_data_9_5`): Grafana no soporta downgrade de su DB
   y reusar el volumen de 12.x rompe el arranque.
3. **El plugin es frontend-only**: no trae backend, sus queries van por el
   proxy HTTP de Grafana (`/api/datasources/proxy/...`) y usan SQL contra
   `_search`. Los streams de MÉTRICAS no se exponen por SQL (solo por la API
   PromQL en `/api/{org}/prometheus/api/v1/*`), de ahí el datasource
   `prometheus-oo` para los paneles de métricas. `basicAuthPassword` de
   provisioning va SIEMPRE bajo `secureJsonData` (top-level se ignora
   silenciosamente → auth vacía → 401).
4. **`--enable-feature=expand-env` NO expande `basic_auth.password`** del
   remote_write (capturado en el wire con un sink: envía el literal
   `${ZO_ROOT_USER_PASSWORD}` → 401). Solución aplicada: el password se
   escribe a `/tmp/zo_pw` DENTRO del contenedor al arrancar (command compose
   con `entrypoint: /bin/sh` — la imagen trae `ENTRYPOINT /bin/prometheus` y
   un `command /bin/sh` le llega como argumento inválido) y se referencia con
   `password_file`. El secreto vive SOLO en
   `~/git/docker-compose/openobserve/.env` (chmod 600, symlink como
   `.env` del proyecto observability; Grafana lo consume vía env en
   provisioning).
5. **remote_write a OpenObserve** (docs oficiales): URL
   `http://<host>:5080/api/default/prometheus/api/v1/write` con basic auth;
   verificado 401 sin auth / datos visibles por PromQL con auth.

### Cómo se verifica

```bash
# ingesta (debe listar 200s recientes)
ssh iinstances@192.168.1.23 'docker logs openobserve --since 2m | grep prometheus/api/v1/write | grep -c 200'
# datos reales en el backend unificado
curl -s -u admin@hermes.local:*** 'http://192.168.1.23:5080/api/default/prometheus/api/v1/query?query=hermes_bridge_up'
# cadena completa Grafana → OpenObserve
curl -s -u admin:admin -X POST http://192.168.1.23:3000/api/ds/query \
  -H 'Content-Type: application/json' \
  -d '{"queries":[{"refId":"A","datasource":{"type":"prometheus","uid":"prometheus-oo"},"expr":"hermes_bridge_up","instant":true}],"from":"now-15m","to":"now"}'
```


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
