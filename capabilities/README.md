# Capacidades modulares del plugin

El plugin se extiende mediante capacidades modulares: cada capacidad es un
directorio en `capabilities/` con:

- `manifest.yaml` — permisos, scope, triggers y objetivo asociado
- `skill.md` — documentacion que el agente lee al activarse
- scripts propios si los necesita (p. ej. guard.py)

El inventario de hosts (`hosts.yaml`) NO vive aqui: es un DATO con las IPs
y permisos de esta casa, no codigo compartible (principio plugin=codigo,
~/.hermes/=datos, t_7d102e2b). Vive en
`~/.hermes/data/capabilities/<cap>/hosts.yaml` y el manifest lo apunta
mediante `hosts_file` con ruta absoluta `~/...` (tambien se aceptan rutas
relativas al directorio de la capacidad).

Contrato:

1. Guardrails globales (GR4/GR5/GR11) SIEMPRE activos. El manifest de una
   capacidad abre excepciones selectivas y limitadas, nunca un bypass.
2. Fail-closed: operacion fuera del manifest → bloqueada y logueada
   ("operation not permitted by <cap> manifest").
3. SSH solo a hosts del inventario de la capacidad activa.
4. El guard se invoca ANTES de ejecutar cada operacion SSH:

```
python3 capabilities/<cap>/guard.py --check-op --host <id> --command "<cmd>"
```

5. La capacidad se activa por triggers declarados en su manifest
   (task_body_contains / task_title_contains / objective).

Capacidades instaladas:

| Directorio | Objetivo | Descripcion |
|---|---|---|
| `sysadmin-lan/` | OBJ-SYSADMIN | Gestion sysadmin de hosts en la LAN (gpu-host) |
| `self-governance/` | — | Envoltura de la gobernanza existente (kanban, quota, crons, observability) — sin cambios de comportamiento |
