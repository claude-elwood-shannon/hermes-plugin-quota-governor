#!/usr/bin/env bash
# efficiency-ratio-cron.sh — P4 wrapper (no_agent, zero tokens).
# Hourly: appends one efficiency_ratio line to metrics-history.jsonl and
# ONE timestamped tick line to the log (liveness signal for
# cron-health-check.sh — the script itself is silent on success,
# watchdog pattern; a quiet log would false-positive DEAD/ZOMBIE).
TS=$(date +'%Y-%m-%d %H:%M:%S')
# Tick de liveness: SIEMPRE al log compartido que vigila cron-health-check.sh
# (HERMES_HOME_DIR hardcodeado alli). No usar HERMES_HOME: bajo el perfil vale
# <perfil> y el tick aterrizaría en un log que el health-check no lee.
LOG=/home/iinstances/.hermes/logs/efficiency-ratio.log
echo "[$TS] efficiency-ratio: tick" >> "$LOG"
echo "[$TS] efficiency-ratio: tick"
# Ratio: regenera la métrica y deja su línea JSON en el mismo log (crontab
# redirige el stdout del wrapper). Sin exec: el gauge de abajo corre después.
/usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/obs/efficiency-ratio.py --verbose
# OBJ-METRICS gauge: racha de días consecutivos con veredicto no-CRITICO
# (criterio de éxito de 3 días). Fail-open: si el gauge falla, el tick y la
# línea del ratio ya están en el log; solo falta el JSON {"streak_days": ...}
# de ese tick. Su salida es distinguible (claves streak_days/
# meets_3_day_criterion, ajenas al registro efficiency_ratio).
/usr/bin/python3.12 /data/git/hermes-plugin-quota-governor/scripts/obs/efficiency-streak.py --json || true
