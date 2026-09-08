#!/usr/bin/python3.12
"""quota-forecast.py — FASE 2 del sistema predictivo de cuota (OBJ-24).

Predictor EMA no_agent: cada 15m (tras quota-metrics) calcula, por provider,
el burn rate (%/min) con una media móvil exponencial (alfa 0.3) sobre
metrics-history.jsonl (ventana 6h) y proyecta los hitos 90% (stop del
governor) y 100% (agotamiento) para la ventana SEMANAL — la que gobierna el
reset de los 3 providers (lunes ~02:00 CEST).

Entrada:  metrics-history.jsonl (filas de quota-metrics.py)
Salida:   forecast.json
            {provider: {pct_now, burn_rate_pct_per_min, eta_90_iso,
                        eta_100_iso, confidence}}
Stdout:   SOLO en anomalia (patron watchdog). Silencio = forecast OK.

Reglas de decision (consumidas por el gate vía --suggest):
  - ETA_90 < margen hasta el reset (2h de colchon): reducir max_workers a 1
    y marcar max_task_cost en el contexto del creator (forecast_warning).
  - ETA_90 < 1h: wakeAgent:false (board se apaga solo).

Ventana por defecto del predictor: la SEMANAL (weekly). La sesión de Ollama
(5h) y la rolling de OpenCode (5h) se registran pero el hito que dispara
decisiones del creator es el semanal — las cortas se resetean solas cada 5h.

Degradacion elegante: history ausente/corrupta/insuficiente -> providers_ok=0,
forecast.json con {enabled: false} y exit 0. Nunca rompe el cron.

TODO no_agent: cero llamadas API, cero tokens. Lee solo el last-good del tick.
"""
import json
import math
import os
import sys
import time
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser(
    "~/.hermes/profiles/pr-ollama")
STATE_DIR = Path(HERMES_HOME) / "quota-governor"
HISTORY = Path(
    os.environ.get("QUOTA_METRICS_HISTORY",
                   str(Path.home() / ".hermes/profiles/pr-ollama/quota-governor/metrics-history.jsonl")))
OUT = STATE_DIR / "forecast.json"

VENTANA_SEG = 6 * 3600        # ventana de observación 6h
ALFA = 0.3                    # factor EMA
HITO_STOP = 90.0              # % que dispara el stop del governor
HITO_FIN = 100.0              # % = agotamiento de la ventana
RESET_CEST_HORA = 2           # reset semanal lunes ~02:00 CEST
CEST_OFFSET = 2               # UTC+2 (horario de verano)
COLCHON = 2.0                 # margen antes del reset (horas) para reducir
DT_MIN_SEG = 120              # par de muestras con dt menor -> descartado
                              # (last-good de providers se refresca a ritmos
                              # distintos; pares casi simultáneos producen
                              # burn rates absurdos, v. F2 first-run)

# métrica weekly por provider en la fila de metrics-history
METRICAS_WEEKLY = {
    "pr-ollama": "ollama_weekly_pct",
    "pr-nanogpt": "nanogpt_weekly_pct",
    "pr-opencode": "opencode_weekly_pct",
}


def _parse_iso(ts):
    """ISO con Z -> epoch segundos. None si no parsea."""
    import datetime
    try:
        return datetime.datetime.strptime(
            ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_weekly_reset_epoch(now_epoch=None):
    """Próximo reset semanal: lunes 02:00 CEST = 00:00 UTC del lunes."""
    import datetime
    now = datetime.datetime.fromtimestamp(
        now_epoch or time.time(), tz=datetime.timezone.utc)
    dias_hasta_lunes = (0 - now.weekday()) % 7  # lunes=0
    reset = (now + datetime.timedelta(days=dias_hasta_lunes)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    # reset ya pasado hoy (post 00:00 UTC lunes = 02:00 CEST lunes): +7d
    if reset <= now:
        reset += datetime.timedelta(days=7)
    return reset.timestamp()


def ema_burn(puntos, alfa=ALFA, dt_min=DT_MIN_SEG):
    """EMA del burn rate (%/min) sobre [(epoch, pct), ...] ordenado.

    Pares con dt < dt_min se descartan (muestreos casi simultáneos del
    last-good producen rates absurdos). Devuelve (ema, n_puntos_usados).
    """
    ema = None
    prev_t, prev_p = puntos[0]
    n = 0
    for t, p in puntos[1:]:
        dt = t - prev_t
        if dt <= 0:
            continue
        if dt < dt_min:
            # muestreo demasiado denso: avanza la ventana sin computar rate
            prev_t, prev_p = t, p
            continue
        rate = (p - prev_p) / (dt / 60.0)
        ema = rate if ema is None else alfa * rate + (1 - alfa) * ema
        prev_t, prev_p = t, p
        n += 1
    return ema, n


def eta_horas(pct_now, hito, burn):
    """Horas hasta el hito con burn %/min (positivo=consume). None si no aplica."""
    if burn is None or burn <= 0:
        return None
    restante = hito - pct_now
    if restante <= 0:
        return 0.0
    return restante / burn / 60.0


def _conf(n, burn):
    """Confianza heurística: 0 (sin datos) .. 3 (sólida)."""
    if n < 2:
        return 0
    if burn is None:
        return 1
    if abs(burn) < 1e-9:
        return 2
    return 3 if n >= 8 else 2


def main():
    filas = []
    try:
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if isinstance(d, dict) and d.get("ts"):
                    filas.append(d)
            except json.JSONDecodeError:
                continue
    except OSError:
        filas = []

    now_epoch = time.time()
    corte = now_epoch - VENTANA_SEG
    # dedupe por ts (cron puede solaparse): última fila por ts
    por_ts = {}
    for d in filas:
        e = _parse_iso(d["ts"])
        if e is None or e < corte:
            continue
        por_ts[e] = d
    puntos_ts = sorted(por_ts)

    forecast = {"generated_at": _iso(now_epoch),
                "window_hours": VENTANA_SEG // 3600,
                "alpha": ALFA,
                "enabled": bool(puntos_ts),
                "providers": {}}

    reset_epoch = next_weekly_reset_epoch(now_epoch)
    horas_hasta_reset = (reset_epoch - now_epoch) / 3600.0
    forecast["next_weekly_reset_iso"] = _iso(reset_epoch)
    forecast["hours_to_reset"] = round(horas_hasta_reset, 2)

    ok = 0
    for prov, key in METRICAS_WEEKLY.items():
        pares = [(e, por_ts[e].get(key)) for e in puntos_ts
                 if por_ts[e].get(key) is not None]
        if not pares:
            continue
        pct_now = pares[-1][1]
        pct_now = min(float(pct_now), HITO_FIN)
        burn, n = ema_burn(pares)
        eta90 = eta_horas(pct_now, HITO_STOP, burn)
        eta100 = eta_horas(pct_now, HITO_FIN, burn)
        fprov = {
            "pct_now": round(pct_now, 2),
            "burn_rate_pct_per_min": (round(burn, 6) if burn is not None else None),
            "eta_90_iso": (_iso(now_epoch + eta90 * 3600)
                           if eta90 is not None else None),
            "eta_100_iso": (_iso(now_epoch + eta100 * 3600)
                            if eta100 is not None else None),
            "eta_90_hours": (round(eta90, 2) if eta90 is not None else None),
            "eta_100_hours": (round(eta100, 2) if eta100 is not None else None),
            "confidence": _conf(n, burn),
            "samples": len(pares),
            "pairs_used": n,
        }
        forecast[prov] = fprov
        ok += 1

    forecast["providers_ok"] = ok
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(forecast, ensure_ascii=False, indent=1))
        tmp.replace(OUT)
    except OSError as e:
        print(f"quota-forecast: no se pudo escribir {OUT}: {e}")
        return 0

    if ok == 0:
        print("quota-forecast: sin muestras suficientes en la ventana 6h "
              "(history vacia) — forecast.json con enabled:false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())