# Self-governance (capacidad estructural)

Envoltura de lo que ya existe: kanban, quota, crons, observability, supply y
objetivos. NO cambia el comportamiento — solo lo estructura como una capacidad
mas, con triggers e inventario.

## Componentes cubiertos (viven en su sitio de siempre)
- Gobernanza de cuota: `quota_governor.py`, `quota_planner.py`,
  `providers.py`, `scripts/quota-governor-tick.sh`
- Concurrencia y salud: `concurrency_guard.py`, `health_checks.py`
- Bridge: `scripts/bridge/open-webui-bridge.py` (+ supervisor cron)
- Observabilidad: `scripts/obs/` (portal, watchdog, tick-cola-viva)
- Objetivos: tabla `approved_objectives` en kanban.db

## Limites
- `ssh: false`: ninguna operacion SSH bajo esta capacidad; si un objetivo de
  gobernanza necesita tocar gpu-host, lo hace bajo la capacidad sysadmin-lan.
- Sin permisos nuevos: esta capacidad no abre excepciones a GR4/GR5/GR11.
