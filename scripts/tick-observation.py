#!/usr/bin/python3.12
"""tick-observation.py — OBJ-26a follow-up (t_92d7f0d6).

El tick de cron (quota-governor-tick.sh) decide pero nunca persiste una fila
en observations.jsonl — las filas existentes vienen solo de los hooks
in-process (periodic_sample / session_end / task_*), que no corren en ticks
no_agent. Este script APPENDEA la fila del tick, incluyendo los acumuladores
per-request del ledger OBJ-26a:

  quota.request_balance_usd   suma costUsd>0 (paymentSource USD) de la ventana
  quota.request_covered_usd   suma costUsd==0 cubierto por suscripcion

Independencia (NO duplicar): estos campos son acumuladores del LEDGER
per-request (nanogpt-requests.jsonl via x_nanogpt_pricing). Son independientes
de activity_cost (Ollama /api/usage), de window_spent_usd (probe de balance)
y de spent_usd (burn-watchdog) — se reportan por separado, nunca se mezclan.

Privacidad: privacy:low. Solo agregados USD a escala NanoGPT (1e-06); ninguna
fila per-request, contenido, prompt ni modelo viaja a observations.jsonl.

Uso (desde quota-governor-tick.sh, tras la decision):
  python3 tick-observation.py --action run --max-workers 2 \
      --session-pct 12.3 --weekly-pct 45.6 --session-reqs 100 --weekly-reqs 900 \
      --cost 0.0000 --reason "healthy"

Salida: append JSONL a $HERMES_HOME/quota-governor/observations.jsonl
(event=quota_tick). Nunca falla: cualquier error se imprime a stdout y el
exit code es 0 (el tick no debe romperse por observabilidad).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = os.environ.get(
    "PLUGIN_DIR", str(Path(__file__).resolve().parent.parent))
_LEDGER_SCRIPT = os.path.join(
    PLUGIN_DIR, "scripts", "nanogpt-balance-ledger.py")

# Cached ledger module (tests stub this).
_LEDGER = None
_LEDGER_TRIED = False


def _load_ledger():
    """Importlib-load the deployed nanogpt-balance-ledger. None on failure."""
    global _LEDGER, _LEDGER_TRIED
    if _LEDGER_TRIED:
        return _LEDGER
    _LEDGER_TRIED = True
    try:
        spec = importlib.util.spec_from_file_location(
            "nanogpt_balance_ledger_tick", _LEDGER_SCRIPT)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LEDGER = mod
    except Exception:
        _LEDGER = None
    return _LEDGER


def _state_file() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "").strip() \
        or str(Path.home() / ".hermes")
    return Path(hermes_home) / "quota-governor" / "observations.jsonl"


def _request_totals():
    """Cross-profile request-window totals; (None, None) when unavailable.

    homes_read == 0 means no profile home has capture data at all — that is
    "no data" (None), not "zero spend" (0.0).
    """
    mod = _load_ledger()
    if mod is None:
        return None, None
    try:
        totals = mod.request_window_totals_all_homes()
    except Exception:
        return None, None
    if not totals or not totals.get("homes_read"):
        return None, None
    return totals.get("request_balance_usd"), totals.get("request_covered_usd")


def _opt_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_row(args) -> dict:
    balance_usd, covered_usd = _request_totals()
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": "quota_tick",
        "decision": {
            "action": args.action,
            "max_workers": _opt_int(args.max_workers),
            "reason": (args.reason or "")[:200],
        },
        "quota": {
            "ollama_session_pct": _opt_float(args.session_pct),
            "ollama_weekly_pct": _opt_float(args.weekly_pct),
            "ollama_session_requests": _opt_int(args.session_reqs),
            "ollama_weekly_requests": _opt_int(args.weekly_reqs),
            "ollama_activity_cost": _opt_float(args.cost),
            # OBJ-26a follow-up: per-request ledger accumulators (exact task
            # names). None = ledger unavailable (fail-open, schema-stable).
            "request_balance_usd": balance_usd,
            "request_covered_usd": covered_usd,
        },
    }


def _opt_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0])
    p.add_argument("--action", default="")
    p.add_argument("--max-workers", default="")
    p.add_argument("--session-pct", default="")
    p.add_argument("--weekly-pct", default="")
    p.add_argument("--session-reqs", default="")
    p.add_argument("--weekly-reqs", default="")
    p.add_argument("--cost", default="")
    p.add_argument("--reason", default="")
    args = p.parse_args(argv)

    try:
        row = build_row(args)
        out = _state_file()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # observability must never break the tick
        print(f"tick-observation: append failed: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
