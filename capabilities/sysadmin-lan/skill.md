# Sysadmin LAN — GPU Host (ml-host) + Services Host

Capacidad modular: gestion sysadmin de hosts de la LAN, acotada por el
manifest de esta carpeta. Los guardrails globales (GR4/GR5/GR11) se mantienen;
la capacidad abre solo excepciones selectivas y limitadas.

## Hosts managed
- **gpu-host** (192.168.1.32) — vLLM inference server, 4060 Ti 16GB
- **services-host** (192.168.1.23) — Docker host: Open WebUI :3001,
  iSponsorBlockTV, stack de observabilidad (Prometheus :9090,
  Elasticsearch :9200, Grafana :3000, OpenObserve :5080)

## Regla permanente del services host: respetar / y usar /data (t_5d573ffc)

El disco raiz del services-host esta al **91%** y `/data` tiene ~163G libres
(Docker Root ya vive en `/data/docker`). Regla permanente para TODA
operacion en este host:

1. **No instalar ni escribir nada en `/`** — toda instalacion, volumen o
   dato nuevo va en `/data`.
2. **Verificar espacio en `/` antes de cualquier operacion** (`df -h / /data`).
3. **Si una operacion requiere espacio en `/`, rechazarla** y reescribirla
   contra `/data`.
4. Los volumenes docker nacen bajo `/data/docker/volumes/` automaticamente
   (Docker Root = `/data/docker`): usar named volumes, nunca binds hacia
   rutas del disco raiz.

La regla es **exigible**: el guard la aplica como deny `write_root_disk`
(escritura absoluta fuera de `/data` y de `~/git/docker-compose/**` →
denied, con prioridad sobre cualquier permiso).

## Que puedes hacer en services-host
- Leer estado: `docker ps`, `docker compose ps`, `free`, `df`, `ss -tln`,
  `journalctl`, `cat /proc/sys/*`
- Contenedores: `docker restart|stop|start <c>`,
  `docker compose restart|stop|start`
- Compose: `docker compose up -d|down|pull|build|ps|logs|config` en
  `~/git/docker-compose/**` (desplegar nuevos servicios incluido)
- Volúmenes: `docker volume create|ls|inspect` (NUNCA prune/rm)
- Logs: `docker logs`, `journalctl` (read-only)
- Escribir SOLO en `~/git/docker-compose/**` (edit_compose_files)

## Que NO puedes hacer en services-host
- Instalar paquetes del sistema (apt/dpkg/snap) — todo via containers
- Cambiar red (iptables, netplan, nmcli, sysctl -w)
- Borrar datos (rm -rf, `docker system prune`, `docker volume rm`)
- Editar credenciales, SSH keys, authorized_keys, .env con secretos
- Escribir fuera de `/data` y de `~/git/docker-compose/**`
  (deny `write_root_disk`: el disco raiz `/` esta al 91% — NO usar)

## Que puedes hacer en gpu-host
- Leer estado: nvidia-smi, systemctl status/is-active, journalctl, free, df, ps
- Gestionar vLLM: `systemctl --user restart|stop|start` de `vllm` y
  `vllm-rounds(.timer)` (reversible, nunca mask/edit)
- Descargar modelos: `huggingface-cli download` (solo publicos sin auth)
- Editar configs de vLLM (superficie /data/ml/**, nunca /etc)
- Leer cualquier fichero (read-only para diagnostico)

## Que NO puedes hacer en gpu-host
- Editar credenciales, SSH keys, authorized_keys, tokens
- Cambiar configuracion de red (iptables, networkd, nmcli, sysctl -w)
- Instalar paquetes del sistema (apt/dpkg/snap)
- Borrar datos (rm, dd, mkfs, shred, truncate)
- Cualquier accion irreversible sin approval del mediador
- SSH a hosts fuera del inventario (hosts.yaml)

## Como se valida una operacion (OBLIGATORIO antes de cada SSH)
El guard es fail-closed: deny primero, luego permissions, si nada encaja →
blocked. Ejecutar siempre:

```
python3 capabilities/sysadmin-lan/guard.py --check-op \
  --host gpu-host --command "<comando remoto>"
```

- Exit 0 (allowed) → ejecutar `ssh <dest> "<comando>"` con `--dest` del JSON.
- Exit 1 denied → comando toca un deny: no ejecutar nunca.
- Exit 1 blocked → no encaja en el manifest: no ejecutar; si hace falta,
  pedir nueva tarea/permission al mediador.

No se permite saltarse el guard (p. ej. ejecutar el ssh sin su exit 0).

## Como acceder
- SSH gpu-host: `ssh hermesuser@192.168.1.32` (key ~/.ssh/id_ed25519)
- SSH services-host: `ssh iinstances@192.168.1.23` (key ~/.ssh/id_ed25519)
- vLLM API: `http://192.168.1.32:8000`
- Observability: `http://192.168.1.23:9090` (Prometheus),
  `http://192.168.1.23:3000` (Grafana, admin/admin initial),
  `http://192.168.1.23:5080` (OpenObserve), `http://192.168.1.57:9120`
  (bridge, `/metrics-prometheus`)
- Endpoints bridge: `GET /capabilities`,
  `GET /capabilities/sysadmin-lan/hosts`,
  `GET /capabilities/sysadmin-lan/status`

## Integracion con vLLM hibrido
- El vLLM hibrido (OBJ-VLLM achieved) ya usa este host.
- Los cambios de modelo deben coordinarse con el sistema de hints [vllm-hint].
- Si cambias el modelo servido, el skill vllm-delegate sigue funcionando
  (la API es la misma).

## Triggers de activacion
El agente carga este skill.md cuando una tarea contiene
`[capability: sysadmin-lan]` en el body, o "gpu-host"/"ml-host" (gpu-host) o
"services-host"/"observability" (services-host) en el titulo, o pertenece al
objetivo OBJ-SYSADMIN (ver manifest.yaml `triggers`).
