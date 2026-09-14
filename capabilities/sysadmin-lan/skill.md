# Sysadmin LAN — GPU Host (ml-host)

Capacidad modular: gestion sysadmin del host GPU de la LAN, acotada por el
manifest de esta carpeta. Los guardrails globales (GR4/GR5/GR11) se mantienen;
la capacidad abre solo excepciones selectivas y limitadas.

## Host managed
- **gpu-host** (192.168.1.32) — vLLM inference server, 4060 Ti 16GB

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
- SSH: `ssh hermesuser@192.168.1.32` (key ~/.ssh/id_ed25519)
- vLLM API: `http://192.168.1.32:8000`
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
`[capability: sysadmin-lan]` en el body, o "gpu-host"/"ml-host" en el titulo,
o pertenece al objetivo OBJ-SYSADMIN (ver manifest.yaml `triggers`).
